"""Single-worker persistent scheduler: alternate discovery and processing fairly."""

import json
import time
from datetime import datetime
from urllib.parse import quote

from .config import APIError, MinerError, ModelUnavailable
from .links import target_job
from .judge import HIT_LEVELS
from .runner import SOURCE_ERRORS
from .shutdown import StopRequested


class Cruise:
    # Pause Zenodo scheduling without removing its adapter, seeds or persisted work.
    SUPPORTED_SOURCES = ('github', 'arxiv', 'zenodo')
    SOURCES = ('github', 'arxiv')
    def __init__(self, runner, discovery, shutdown, recheck_interval=2.5, idle_interval=.25, sources=None):
        self.SOURCES = self.active_sources(sources)
        self.runner = runner
        self.state = runner.state
        self.discovery = discovery
        self.shutdown = shutdown
        self.recheck_interval = max(recheck_interval, 2.5)
        self.idle_interval = min(idle_interval, .25)
        self.next_source = 0

    @classmethod
    def active_sources(cls, sources=None):
        active = cls.SOURCES if sources is None else tuple(sources)
        if (not active or len(set(active)) != len(active)
                or any(source not in cls.SUPPORTED_SOURCES for source in active)):
            raise MinerError('启用来源必须为非空且不重复的 GitHub/arXiv/Zenodo 子集。')
        return active

    def _ready_cursor(self, now, exclude=()):
        # Filter in indexed queries, including when paused-source cursors are due.
        rows = [self.state.next_discovery_cursor(now, exclude, source=source) for source in self.SOURCES]
        return min((row for row in rows if row is not None),
                   key=lambda row: (row['due_at'], row['source'], row['cursor_id']), default=None)

    def _ready_task(self, now, exclude=()):
        for offset in range(len(self.SOURCES)):
            source = self.SOURCES[(self.next_source + offset) % len(self.SOURCES)]
            row = self.state.next_task(now, exclude, source=source)
            if row is not None:
                return row
        return None

    @staticmethod
    def backoff(failures):
        return min(21600, 30 * 2 ** min(failures, 10))

    def _retry_task(self, source, identifier, row, reason, http_error=None):
        # Platform embargoes and model failures do not consume the record budget.
        status = http_error.status if (source == 'zenodo' and isinstance(http_error, APIError)
                                      and http_error.status != 429) else None
        failures = row['retry_count'] if status is not None else row['failures']
        failed = self.state.settle(source, identifier, time.time() + self.backoff(failures),
                                   reason, http_status=status)
        if failed:
            self.runner.announce('[' + source + '] [FAILED] ' + identifier +
                                 ' 连续三次 HTTP 失败，已熔断；不再自动入队。' + reason)
        else:
            self.runner.announce('[' + source + '] [WARN] ' + reason + ' 已保存任务重试断点。')

    def _task(self, row):
        source, identifier = row['source'], row['item_id']
        if not self.state.claim(source, identifier):
            return
        self.runner.announce('[' + source + '] [TARGET] ' + identifier)
        try:
            payload = json.loads(row['payload_json'])
            adapter = getattr(self.discovery, source)
            if source == 'github':
                name = payload['full_name']
                # Fresh metadata handles default-branch changes; HEAD alone gates expensive work.
                repo = adapter.client.get_json('/repos/' + quote(name, safe='/'))
                canonical = str(repo['id'])
                if identifier.isdigit() and canonical != identifier:
                    raise MinerError('仓库名称已被其他 ID 使用；保留原任务，等待发现源更新元数据。')
                self.state.remember_alias(source, repo['full_name'], canonical)
                self.state.remember_alias(source, name, canonical)
                self.state.promote(source, identifier, canonical, repo, str(repo.get('pushed_at') or ''))
                identifier = canonical
                candidate = adapter.candidate(repo)
            elif source == 'zenodo':
                record = payload if 'metadata' in payload else adapter.record(identifier)
                candidate = adapter.candidate(record)
            elif source == 'arxiv':
                candidate = adapter.candidate(payload)
            else:
                raise MinerError('任务来源无效。')
            if candidate is None:
                self.state.settle(source, identifier, time.time() + self.recheck_interval)
                return
            if source == 'arxiv':
                from .offline_review import StoredEvidence
                cached = self.state.get_scan((source, candidate.resource_id, candidate.version, self.runner.pipeline_key))
                if cached is None or not cached['evidence_json'] or (
                        self.runner.retry_review and cached['status'] == 'needs_review'):
                    adapter = StoredEvidence(adapter.full_evidence(candidate))
            processing_error = self.runner.process(candidate, adapter)
            key = (source, candidate.resource_id, candidate.version, self.runner.pipeline_key)
            result = self.state.get_scan(key)
            if result is None or result['status'] not in {'complete', 'needs_review'}:
                error = result['error'] if result is not None else '扫描未完成。'
                self._retry_task(source, identifier, row, error, processing_error)
                return
            # Link extraction is saved with evidence and re-used after a crash, before settling the task.
            evidence = json.loads(result['evidence_json'] or '{}')
            assessment = json.loads(result['assessment_json'] or '{}')
            targets = evidence.get('external_targets', [])
            for linked_source, item in targets:
                self.state.enqueue(*target_job(self.state, linked_source, item))
            self.state.settle(source, identifier,
                              time.time() + self.recheck_interval if source == 'github' else None)
        except ModelUnavailable:
            self.state.settle(source, identifier, time.time() + self.recheck_interval,
                              '模型请求状态不明；停止巡航，需确认服务器请求结束后再启动。')
            raise
        except SOURCE_ERRORS as error:
            reason = self.runner._safe_error(error)
            self.runner.stats['errors'] += 1
            self._retry_task(source, identifier, row, reason, error)

    def run(self, seeds, continuous=True, max_repos=None, max_items=None):
        self.state.init_cruise()
        self.next_source = self.state.task_rotation() % len(self.SOURCES)
        self.discovery.configure(seeds)
        self.state.activate_cruise(time.time())
        if self.runner.retry_review:
            self.state.requeue_reviews(self.runner.pipeline_key)
        self.runner.reporter.flush()
        visited, repos = 0, 0
        since_discovery = 8
        once_tasks = set()
        # Once means one page per currently configured discovery cursor, then ready tasks.
        once_cursors = set()
        self.runner.announce('[START] ' + ('持续巡航' if continuous else '单轮采矿') +
                             '；启用来源：' + ', '.join(self.SOURCES) + '；单 worker、单模型并发。Ctrl+C/SIGTERM 等待当前条目提交后退出。')
        try:
            while not self.shutdown.requested:
                if (max_repos is not None and repos >= max_repos) or (max_items is not None and visited >= max_items):
                    break
                worked = False
                ready_task = self._ready_task(time.time(), () if continuous else once_tasks)
                cursor = (self._ready_cursor(time.time(), () if continuous else once_cursors)
                          if ready_task is None or since_discovery >= 8 else None)
                cursor_key = (cursor['source'], cursor['cursor_id']) if cursor else None
                if cursor and (continuous or cursor_key not in once_cursors):
                    try:
                        self.discovery.tick(cursor)
                    except StopRequested:
                        break
                    except SOURCE_ERRORS as error:
                        reason = self.runner._safe_error(error)
                        self.state.cursor_fail(cursor, reason, time.time() + self.backoff(cursor['failures']))
                        self.runner.stats['errors'] += 1
                        self.runner.announce('[' + cursor['source'] + '] [WARN] ' + reason + ' 游标保留；继续其他来源。')
                    since_discovery = 0
                    once_cursors.add(cursor_key)
                    worked = True
                if self.shutdown.requested:
                    break
                task = self._ready_task(time.time(), () if continuous else once_tasks)
                if task:
                    # From here, even a signal during HTTP/model I/O cannot interrupt the final commit.
                    with self.shutdown.item():
                        self._task(task)
                        self.next_source = (self.SOURCES.index(task['source']) + 1) % len(self.SOURCES)
                        self.state.save_task_rotation(self.next_source)
                    if not continuous:
                        once_tasks.add((task["source"], task["item_id"]))
                    if not continuous and task["source"] == "github":
                        alias = self.state.alias("github", json.loads(task["payload_json"]).get("full_name", ""))
                        if alias:
                            once_tasks.add(("github", alias))
                    since_discovery += 1
                    visited += 1
                    repos += int(task['source'] == 'github')
                    worked = True
                if not worked:
                    if not continuous:
                        break
                    wake = self.state.wake_time()
                    delay = min(self.idle_interval, max(.05, wake - time.time())) if wake is not None else self.idle_interval
                    self.runner.announce('[SLEEP] 队列暂空；下次检查 ' + str(round(delay, 1)) + ' 秒后；' +
                                         ('最早游标/任务 ' + datetime.fromtimestamp(wake).isoformat(timespec='seconds') if wake else '暂无计划任务'))
                    self.shutdown.wait(delay)
        except StopRequested:
            self.shutdown.requested = True
        finally:
            # Every active item has exited its context, and its SQLite transaction has completed.
            self.runner.reporter.flush()
            self.runner.announce('[EXIT] ' + ('收到退出信号；当前条目已安全落库。' if self.shutdown.requested else '本轮结束。') +
                                 '处理任务 ' + str(visited) + '；扫描 {scanned}，未变更 {skipped}，命中 {hits}，'
                                 '待核对 {review}，告警 {errors}。队列与游标已保存。'.format(**self.runner.stats))
        return 0 if self.shutdown.requested else (1 if self.runner.stats['errors'] or self.runner.stats['review'] else 0)
