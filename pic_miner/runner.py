"""Sequential analysis/checkpoint/report pipeline, reused by the persistent scheduler."""

import fcntl
import copy
import json
import os
from contextlib import contextmanager
from pathlib import Path

import requests

from .config import NUM_CTX, APIError, MinerError, ModelOutputError, ModelUnavailable
from .evidence import optical_matches, strong_description
from .judge import HIT_LEVELS, LEVEL_3


SOURCE_ERRORS = (MinerError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError,
                 requests.RequestException)


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            raise MinerError("另一个扫描进程正在运行；拒绝并发调用模型。") from None
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class Runner:
    def __init__(self, state, judge, reporter, pipeline_key: str, sanitizer, output=print, retry_review=False):
        self.state = state
        self.judge = judge
        self.reporter = reporter
        self.pipeline_key = pipeline_key
        self.sanitizer = sanitizer
        self.output = output
        self.retry_review = retry_review
        self.stats = {"scanned": 0, "skipped": 0, "rule_miss": 0, "hits": 0, "errors": 0, "review": 0}

    def announce(self, text: str) -> None:
        self.output(self.sanitizer.clean(text))

    def process(self, candidate, adapter, force_judgement=False, continue_reading=False) -> None:
        key = (candidate.source, candidate.resource_id, candidate.version, self.pipeline_key)
        existing = self.state.get_scan(key)
        if existing and not continue_reading and (existing["status"] == "complete" or
                         (existing["status"] == "needs_review" and not self.retry_review)):
            self.stats["skipped"] += 1
            self.state.record_event('skip_unchanged' if existing['status'] == 'complete' else 'skip_review')
            label = "已完成" if existing["status"] == "complete" else "已标记待人工核对"
            self.announce("[SKIP] " + label + "且版本未变：" + candidate.name)
            return
        self.state.begin(candidate, self.pipeline_key)
        self.announce("[" + candidate.source + "] [SCAN] " + candidate.name)
        try:
            if existing and existing["evidence_json"]:
                evidence = self.state.load_evidence(existing["evidence_json"])
                if (not force_judgement and self.retry_review and existing["status"] == "needs_review"
                        and evidence.get("inspection_incomplete")):
                    self.announce("  显式重试覆盖不足条目，重新静态提取证据。")
                    evidence = adapter.evidence(candidate)
                    self.state.save_evidence(key, evidence)
                else:
                    self.announce("  恢复已保存证据，跳过文件读取。")
            else:
                evidence = copy.deepcopy(adapter.evidence(candidate))
                self.state.save_evidence(key, evidence)
            if evidence.get('assessment_protocol') != self.pipeline_key:
                evidence.pop('examined_chunks', None)
                evidence.pop('best_assessment', None)
            evidence['assessment_protocol'] = self.pipeline_key
            descriptive = evidence.get("strong_description", strong_description(evidence["text"]))
            channel = ("版图/导出规则" if evidence["matched"] else
                       ("强描述通道" if descriptive else "语义探索（规则未命中）"))
            if force_judgement:
                channel = '缓存证据研判（无需再次取证）'
            if descriptive and "strong_description" not in evidence["rules"]:
                evidence["rules"].append("strong_description")
                self.state.save_evidence(key, evidence)
            self.announce("[" + candidate.source + "] [FEATURE] " + channel + "，串行调用本地模型（num_ctx=" + str(NUM_CTX) + "）。")
            if hasattr(self.judge, 'trace_context'):
                self.judge.trace_context = key
            assessment = self.judge.assess(evidence)
            self.state.save_evidence(key, evidence)
            if assessment['level'] == LEVEL_3 and (evidence.get('inspection_incomplete') or
                                                   getattr(self.judge, 'coverage_incomplete', False)):
                evidence['review_reason_category'] = 'coverage_incomplete'
                self.state.save_evidence(key, evidence)
                self._review(key, candidate, '当前分块未发现可用资源，但取证覆盖不足；保留线索待补证，未判为无关。')
                return
            is_hit = assessment["level"] in HIT_LEVELS
            self.announce("[" + candidate.source + "] [LEVEL] " + candidate.name + " -> " + assessment["level"])
            self.state.finish(key, assessment, is_hit)
            self.stats["scanned"] += 1
            if is_hit:
                self.stats["hits"] += 1
            else:
                self.announce("  Level 3 无关/否定，已持久化并跳过。")
            self.reporter.flush()
        except ModelUnavailable as error:
            if 'evidence' in locals():
                self.state.save_evidence(key, evidence)
            self.state.fail(key, self.sanitizer.clean(error), reason='model_unavailable')
            self.stats["errors"] += 1
            raise
        except ModelOutputError as error:
            self.state.save_evidence(key, evidence)
            self._review(key, candidate, self.sanitizer.clean(error))
        except SOURCE_ERRORS as error:
            self.state.record_event('source_errors')
            # A reporting failure must not invalidate an already committed model result.
            completed = self.state.get_scan(key)
            if not completed or completed["status"] not in {"complete", "needs_review"}:
                self.state.fail(key, self._safe_error(error), reason='source_error')
            self.stats["errors"] += 1
            self.announce("[WARN] " + candidate.name + "：" + self._safe_error(error))
            # Preserve structured HTTP status for the scheduler's retry budget,
            # including failures while reading Zenodo text attachments.
            if isinstance(error, APIError):
                return error

    def _review(self, key, candidate, reason):
        self.state.needs_review(key, reason)
        self.stats['review'] += 1
        self.announce('[REVIEW] ' + candidate.name + '：' + reason + ' 证据已保存。')
        try:
            self.reporter.flush()
        except SOURCE_ERRORS as error:
            # A failed Markdown publish never downgrades the durable review checkpoint.
            self.stats['errors'] += 1
            self.announce('[WARN] 待核对看板刷新失败：' + self._safe_error(error) + '；重跑会补写。')

    def _safe_error(self, error) -> str:
        if isinstance(error, MinerError):
            return self.sanitizer.clean(error)
        return "来源请求或数据解析失败（" + type(error).__name__ + "）；跳过该项。"

    def run(self, github, zenodo, seeds: dict, max_repos=None) -> int:
        recovered = self.reporter.flush()
        if recovered:
            self.announce("已补写历史待报告命中：" + str(recovered))
        visited = 0
        seen = set()
        github_seeds = [([name], [], "组织 " + name) for name in seeds["organizations"]]
        github_seeds += [([], [name], "仓库 " + name) for name in seeds["repositories"]]
        for organizations, repositories, label in github_seeds:
            if max_repos is not None and visited >= max_repos:
                break
            try:
                for repo in github.repositories(organizations, repositories):
                    identifier = str(repo["id"])
                    if identifier in seen:
                        continue
                    if max_repos is not None and visited >= max_repos:
                        break
                    seen.add(identifier)
                    visited += 1
                    try:
                        candidate = github.candidate(repo)
                    except SOURCE_ERRORS as error:
                        self.stats["errors"] += 1
                        self.announce("[WARN] 获取仓库版本失败：" + self._safe_error(error))
                        continue
                    if candidate is not None:
                        self.process(candidate, github)
            except ModelUnavailable:
                raise
            except SOURCE_ERRORS as error:
                self.stats["errors"] += 1
                self.announce("[WARN] GitHub " + label + "：" + self._safe_error(error))
        for index, query in enumerate(seeds["zenodo_queries"], 1):
            try:
                for candidate in zenodo.candidates([query]):
                    self.process(candidate, zenodo)
            except ModelUnavailable:
                raise
            except SOURCE_ERRORS as error:
                self.stats["errors"] += 1
                self.announce("[WARN] Zenodo 查询 #" + str(index) + "：" + self._safe_error(error))
        self.reporter.flush()
        self.announce("完成：扫描 {scanned}，未变更跳过 {skipped}，规则未命中 {rule_miss}，"
                      "新增命中 {hits}，待人工核对 {review}，失败 {errors}。".format(**self.stats))
        return 1 if self.stats["errors"] or self.stats["review"] else 0
