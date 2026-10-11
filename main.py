#!/usr/bin/env python3
"""Persistent PIC resource mining and source-free SQLite review replay."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent


def arguments():
    parser = argparse.ArgumentParser(description='PIC 地毯式发现与巡航，持久化队列，串行研判并实时更新 hits.md 看板。')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--continuous', dest='continuous', action='store_true', help='持续巡航（默认）')
    mode.add_argument('--once', dest='continuous', action='store_false', help='每个发现游标读取一页，再处理当前就绪任务后退出')
    mode.add_argument('--review-offline', action='store_true', help='只用 SQLite 已存证据重评当前 Review；不读取种子、不访问外部采集 API')
    mode.add_argument('--reassess-offline', action='store_true', help='用已存证据重评旧协议的无关/Review；跳过已按当前协议解决的条目')
    parser.set_defaults(continuous=True)
    parser.add_argument('--seeds', type=Path, default=ROOT / 'seeds.json', help='JSON 种子与查询配置')
    parser.add_argument('--org', action='append', default=[], help='组织；与 --repo 一起替换默认 GitHub 组织/仓库种子')
    parser.add_argument('--repo', action='append', default=[], help='owner/repository；可重复')
    parser.add_argument('--zenodo-query', action='append', default=[], help='追加 Zenodo 查询')
    parser.add_argument('--github-query', action='append', default=[], help='追加 GitHub 全网搜索关键词')
    parser.add_argument('--arxiv-query', action='append', default=[], help='追加 arXiv 官方搜索表达式')
    parser.add_argument('--max-repos', type=int, help='消费此数量的 GitHub 研判批次后退出；不计采集线程的未变更跳过')
    parser.add_argument('--max-items', type=int, help='消费此数量的研判批次后退出；大资源可分多批轮询')
    parser.add_argument('--retry-review', action='store_true', help='显式重新研判已标记待核对的条目')
    args = parser.parse_args()
    if any(value is not None and value <= 0 for value in (args.max_repos, args.max_items)):
        parser.error('--max-repos / --max-items 必须为正整数')
    return args


def load_seeds(args, error_type):
    try:
        content = json.loads(args.seeds.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise error_type('无法读取种子 JSON；请检查 --seeds 指定的文件。') from None
    from pic_miner.seeds import normalize_seeds
    result = normalize_seeds(content, error_type)
    if args.org or args.repo:
        result['organizations'] = args.org
        result['repositories'] = args.repo
    for option, field in (('zenodo_query', 'zenodo_queries'), ('github_query', 'github_queries'), ('arxiv_query', 'arxiv_queries')):
        result[field] = list(dict.fromkeys(result[field] + getattr(args, option, [])))
    if not any(result.values()):
        raise error_type('请至少指定一个发现渠道或仓库种子。')
    return result


def log(message):
    print(message, flush=True)


def main() -> int:
    args = arguments()
    try:
        from pic_miner.arxiv import Arxiv
        from pic_miner.config import Config, MinerError
        from pic_miner.pipeline import PipelineCruise
        from pic_miner.storage import StateWriter
        from pic_miner.discovery import Discovery
        from pic_miner.github import GitHub
        from pic_miner.http import HTTPClient
        from pic_miner.judge import Judge
        from pic_miner.offline_review import OfflineReview
        from pic_miner.report import Reporter
        from pic_miner.runner import Runner, run_lock
        from pic_miner.shutdown import Shutdown
        from pic_miner.state import State
        from pic_miner.zenodo import Zenodo
    except ImportError:
        print('依赖尚未安装。请先运行：python -m pip install -r requirements.txt', file=sys.stderr)
        return 2
    state = None
    clients = []
    sanitizer = None
    try:
        config = Config.load(ROOT)
        sanitizer = config.sanitizer
        offline = getattr(args, 'review_offline', False) or getattr(args, 'reassess_offline', False)
        seeds = None if offline else load_seeds(args, MinerError)
        shutdown = Shutdown(lambda text: log(sanitizer.clean(text)))
        with run_lock(ROOT / '.pic_miner' / 'run.lock'), shutdown.signals():
            state = (State if offline else StateWriter)(ROOT / '.pic_miner' / 'state.sqlite3')
            def client(base, label, token='', timeout=None, interval=2.5, public=True, pacer=None):
                instance = HTTPClient(base, label, state if public else None, token,
                                      config.http_timeout if timeout is None else timeout,
                                      min_interval=interval, max_wait=config.max_retry_wait,
                                      continuous_rate_wait=public, shutdown=shutdown,
                                      announce=lambda text: log(sanitizer.clean(text)), pacer=pacer, sanitizer=sanitizer)
                clients.append(instance)
                return instance
            if offline:
                model_http = client(config.ollama_base_url, 'Ollama', timeout=config.ollama_timeout, interval=0, public=False)
                runner = Runner(state, Judge(model_http, config.ollama_model, sanitizer,
                                             max_chunks=getattr(config, 'model_chunks', 8),
                                             keep_alive=getattr(config, 'model_keep_alive', '-1'),
                                             record_call=state.record_model_call,
                                             should_stop=lambda: shutdown.requested,
                                             output=lambda text: log(sanitizer.clean(text))),
                                Reporter(state, ROOT / 'hits.md', sanitizer, output=log),
                                config.pipeline_key, sanitizer, output=log, retry_review=True)
                return OfflineReview(runner, shutdown).run(getattr(args, 'max_items', None),
                                                         include_rejected=getattr(args, 'reassess_offline', False))
            github_http = client(config.github_api_url, 'GitHub', config.github_token)
            zenodo_http = client(config.zenodo_api_url, 'Zenodo', interval=2.5)
            model_http = client(config.ollama_base_url, 'Ollama', timeout=config.ollama_timeout, interval=0, public=False)
            arxiv_pacer = SimpleNamespace(last_request=0.0)
            arxiv_api = client(config.arxiv_api_url, 'arXiv', interval=3.5, pacer=arxiv_pacer)
            arxiv_content = client(config.arxiv_content_url, 'arXiv fulltext', interval=3.5, pacer=arxiv_pacer)
            runner = Runner(state, Judge(model_http, config.ollama_model, sanitizer,
                                         max_chunks=config.model_chunks, keep_alive=config.model_keep_alive,
                                         record_call=state.record_model_call,
                                         should_stop=lambda: shutdown.requested,
                                         output=lambda text: log(sanitizer.clean(text))),
                            Reporter(state, ROOT / 'hits.md', sanitizer, output=log),
                            config.pipeline_key, sanitizer, output=log,
                            retry_review=getattr(args, 'retry_review', False))
            discovery = Discovery(state, GitHub(github_http, sanitizer, runner.announce),
                                  Zenodo(zenodo_http, sanitizer, runner.announce),
                                  Arxiv(arxiv_api, arxiv_content, sanitizer, runner.announce),
                                  runner.announce, config.discovery_interval, config.discovery_overlap)
            return PipelineCruise(runner, discovery, shutdown, config.repo_recheck_interval, config.idle_interval,
                                  config.prefetch_per_source).run(
                seeds, getattr(args, 'continuous', True), args.max_repos, getattr(args, 'max_items', None))
    except MinerError as error:
        text = sanitizer.clean(error) if sanitizer else str(error)
        print('[ERROR] ' + text, file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print('初始化阶段已中止；已保留 SQLite 断点。', file=sys.stderr, flush=True)
        return 130
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print('[ERROR] 本地文件或远端数据格式异常（' + type(error).__name__ + '）；已保留断点。', file=sys.stderr, flush=True)
        return 1
    finally:
        for instance in clients:
            instance.close()
        if state is not None:
            state.close()


if __name__ == '__main__':
    sys.exit(main())
