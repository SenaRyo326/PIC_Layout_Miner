"""Enabled independently paced collectors feed a durable, single-inference lane."""

import json
import threading
import time
from urllib.parse import quote

from .config import APIError, MinerError, ModelUnavailable, ModelRequestRejected
from .cruise import Cruise
from .evidence import Candidate, FileEntry
from .links import target_job
from .offline_review import StoredEvidence
from .runner import SOURCE_ERRORS
from .shutdown import StopRequested


class CollectorShutdown:
    """Collector waits must stop even while the inference lane finishes an item."""
    def __init__(self, shutdown, stop):
        self.shutdown, self.stop = shutdown, stop

    @property
    def requested(self):
        return self.shutdown.requested or self.stop.is_set()

    def wait(self, seconds, label='采集等待', countdown=False):
        deadline = time.monotonic() + max(0, seconds)
        last = None
        while True:
            if self.requested:
                raise StopRequested('采集通道已保存断点。')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            whole = int(remaining + .999)
            if countdown and whole != last:
                self.shutdown.output('[WAIT] ' + label + '：剩余 ' + str(whole) + ' 秒')
                last = whole
            self.stop.wait(min(.25, remaining))


class PipelineCruise:
    SOURCES = Cruise.SOURCES

    def __init__(self, runner, discovery, shutdown, recheck_interval=1800,
                 idle_interval=.25, prefetch_per_source=8, sources=None):
        self.SOURCES = Cruise.active_sources(sources)
        self.runner, self.state, self.discovery, self.shutdown = runner, runner.state, discovery, shutdown
        self.recheck_interval, self.idle_interval = max(60, recheck_interval), max(.05, idle_interval)
        self.prefetch_per_source = prefetch_per_source
        self.stop = threading.Event()
        self.collector_shutdown = CollectorShutdown(shutdown, self.stop)
        self.threads = []
        self.lock = threading.Lock()
        self.collection_errors = self.collection_skipped = 0
        self.fatal = None
        self.next_source = 0
        self.continuous = True

    def _candidate(self, source, identifier, payload):
        adapter = getattr(self.discovery, source)
        if source == 'github':
            name = payload['full_name']
            repo = adapter.client.get_json('/repos/' + quote(name, safe='/'))
            canonical = str(repo['id'])
            if identifier.isdigit() and canonical != identifier:
                raise MinerError('仓库名称与原始 ID 不符；等待发现源更新。')
            self.state.remember_alias(source, name, canonical)
            self.state.remember_alias(source, repo['full_name'], canonical)
            if not self.state.promote(source, identifier, canonical, repo, str(repo.get('pushed_at') or '')):
                return None, None
            return adapter.candidate(repo), canonical
        if source == 'zenodo':
            record = payload if 'metadata' in payload else adapter.record(identifier)
            candidate = adapter.candidate(record)
            canonical = candidate.resource_id
            self.state.remember_alias(source, identifier, canonical)
            if not self.state.promote(source, identifier, canonical, record, candidate.version):
                return None, None
            return candidate, canonical
        return adapter.candidate(payload), identifier

    def _retry(self, source, identifier, row, error):
        status = error.status if isinstance(error, APIError) else None
        permanent = source == 'zenodo' and status is not None and 300 <= status < 500 and status != 429
        failures = row['retry_count'] if permanent else row['failures']
        self.state.record_event('source_errors')
        reason = self.runner._safe_error(error)
        failed = self.state.settle(source, identifier, time.time() + Cruise.backoff(failures), reason,
                                   http_status=status if permanent else None)
        with self.lock:
            self.collection_errors += 1
        self.runner.announce('[' + source + '] [' + ('FAILED' if failed else 'RETRY') + '] ' + reason)

    def _prepare(self, row):
        source, identifier = row['source'], row['item_id']
        if not self.state.claim(source, identifier):
            return
        try:
            self.runner.announce('[' + source + '] [FETCH] ' + identifier)
            candidate, identifier = self._candidate(source, identifier, json.loads(row['payload_json']))
            if candidate is None:
                if identifier is not None:
                    self.state.settle(source, identifier, time.time() + self.recheck_interval)
                return
            key = (source, candidate.resource_id, candidate.version, self.runner.pipeline_key)
            existing = self.state.get_scan(key)
            if existing and (existing['status'] == 'complete' or
                             (existing['status'] == 'needs_review' and not self.runner.retry_review)):
                self.state.record_event('skip_unchanged' if existing['status'] == 'complete' else 'skip_review')
                self.state.settle(source, identifier, time.time() + self.recheck_interval if source == 'github' else None)
                with self.lock:
                    self.collection_skipped += 1
                self.runner.announce('[' + source + '] [SKIP] 版本/研判策略未变：' + candidate.name)
                return
            if existing and existing['evidence_json'] and not (
                    self.runner.retry_review and existing['status'] == 'needs_review'):
                evidence = self.state.load_evidence(existing['evidence_json'])
            else:
                adapter = getattr(self.discovery, source)
                evidence = adapter.full_evidence(candidate) if source == 'arxiv' else adapter.evidence(candidate)
            self.state.prepare(candidate, evidence)
            self.state.save_links(candidate, evidence.get('external_targets', []))
            for linked_source, item in evidence.get('external_targets', []):
                self.state.enqueue(*target_job(self.state, linked_source, item))
            self.runner.announce('[' + source + '] [READY] ' + candidate.name +
                                 '；缓存分块 ' + str(len(evidence.get('blocks', []))))
        except StopRequested:
            self.state.settle(source, identifier, time.time())
            raise
        except SOURCE_ERRORS as error:
            self._retry(source, identifier, row, error)

    def _collect(self, source, continuous):
        once_tasks, once_cursors = set(), set()
        since_discovery = 0
        try:
            while not self.collector_shutdown.requested:
                prepared = self.state.prepared_count(source)
                if prepared >= self.prefetch_per_source:
                    self.stop.wait(.1)
                    continue
                row = self.state.next_task(time.time(), () if continuous else once_tasks, source=source)
                cursor = self.state.next_discovery_cursor(time.time(), () if continuous else once_cursors, source=source)
                # Give durable tasks a head start, but rotate discovery quickly when
                # unchanged/error targets fail to replenish an empty inference buffer.
                quantum = 2 if prepared == 0 else 4
                if cursor is not None and (row is None or since_discovery >= quantum):
                    try:
                        self.discovery.tick(cursor)
                    except StopRequested:
                        raise
                    except SOURCE_ERRORS as error:
                        reason = self.runner._safe_error(error)
                        self.state.cursor_fail(cursor, reason, time.time() + Cruise.backoff(cursor['failures']))
                        with self.lock:
                            self.collection_errors += 1
                        self.runner.announce('[' + source + '] [DISCOVERY WARN] ' + reason)
                    once_cursors.add((source, cursor['cursor_id']))
                    since_discovery = 0
                    continue
                if row is not None:
                    self._prepare(row)
                    once_tasks.add((source, row['item_id']))
                    alias = self.state.alias(source, json.loads(row['payload_json']).get('full_name', ''))
                    if alias:
                        once_tasks.add((source, alias))
                    since_discovery += 1
                    continue
                if not continuous:
                    return
                self.stop.wait(self.idle_interval)
        except StopRequested:
            return
        except Exception as error:
            # Unexpected local/storage failures stop safely rather than silently kill a producer.
            with self.lock:
                self.fatal = MinerError('采集通道异常：' + type(error).__name__ + '；已保留断点。')
            self.stop.set()

    def _next(self):
        for offset in range(len(self.SOURCES)):
            source = self.SOURCES[(self.next_source + offset) % len(self.SOURCES)]
            row = self.state.next_prepared(source)
            if row is not None:
                return row
        return None

    def _consume(self, row):
        raw = json.loads(row['candidate_json'])
        raw['files'] = [FileEntry(**entry) for entry in raw.get('files', [])]
        candidate = Candidate(**raw)
        evidence = self.state.load_evidence(row['evidence_json'])
        already_read = len(evidence.get('examined_chunks', []))
        self.runner.process(candidate, StoredEvidence(evidence), force_judgement=True,
                            continue_reading=evidence.get('continue_reading', False))
        key = (candidate.source, candidate.resource_id, candidate.version, self.runner.pipeline_key)
        result = self.state.get_scan(key)
        if result is None or result['status'] not in {'complete', 'needs_review'}:
            self.state.finish_prepared(row['source'], row['item_id'], time.time() + 30,
                                       result['error'] if result else '研判未完成。')
            return
        updated = self.state.load_evidence(result['evidence_json'] or '{}')
        assessment = json.loads(result['assessment_json'] or '{}')
        if (self.continuous and getattr(self.runner.judge, 'coverage_incomplete', False)
                and assessment.get('level') != 'LEVEL_1_CONFIRMED'
                and not updated.get('validation_failure_kind')
                and len(updated.get('examined_chunks', [])) > already_read and updated.get('blocks')):
            # A bounded reading quantum returns to round-robin rather than abandoning
            # unread cached material or monopolizing inference for the whole repository.
            updated['continue_reading'] = True
            self.state.prepare(candidate, updated)
            self.runner.announce('[' + row['source'] + '] [CONTINUE] ' + candidate.name + '；已读进度保存，剩余分块继续轮询。')
            return
        # Discovery links are evidence, not a consequence of a positive model verdict.
        for source, item in evidence.get('external_targets', []):
            self.state.enqueue(*target_job(self.state, source, item))
        self.state.finish_prepared(row['source'], row['item_id'],
                                   time.time() + self.recheck_interval if row['source'] == 'github' else None)

    def run(self, seeds, continuous=True, max_repos=None, max_items=None):
        self.continuous = continuous
        self.state.init_cruise()
        self.next_source = self.state.task_rotation() % len(self.SOURCES)
        self.discovery.configure(seeds)
        self.state.activate_discovery(time.time(), self.discovery.interval, self.SOURCES)
        self.state.requeue_legacy(self.runner.pipeline_key)
        if self.runner.retry_review:
            self.state.requeue_reviews(self.runner.pipeline_key)
        # Each public client belongs to exactly one collector, preserving its rate budget.
        for source in self.SOURCES:
            adapter = getattr(self.discovery, source)
            for name in ('client', 'content'):
                client = getattr(adapter, name, None)
                if client is not None:
                    client.shutdown = self.collector_shutdown
        self.runner.reporter.flush()
        self.runner.announce('[START] 启用采集：' + ', '.join(self.SOURCES) +
                             ' + SQLite writer + 单并发分块研判；未启用来源保留断点，不采集或消费。')
        visited = repos = 0
        model_paused = False
        last_idle_log = 0
        try:
            for source in self.SOURCES:
                thread = threading.Thread(target=self._collect, args=(source, continuous), name=source + '-collector')
                self.threads.append(thread)
                thread.start()
            while not self.shutdown.requested and not self.stop.is_set():
                if (max_items is not None and visited >= max_items) or (max_repos is not None and repos >= max_repos):
                    break
                row = None if model_paused else self._next()
                if row is not None:
                    try:
                        with self.shutdown.item():
                            self._consume(row)
                    except ModelUnavailable as error:
                        # An ambiguous timeout may still occupy the GPU. Never issue another POST.
                        model_paused = True
                        if isinstance(error, ModelRequestRejected):
                            self.runner.announce('[MODEL REJECTED] ' + self.runner._safe_error(error) +
                                                 '；修正配置后重启，采集继续至缓存上限。')
                        else:
                            self.runner.announce('[MODEL PAUSED] ' + self.runner._safe_error(error) +
                                                 '；采集继续至缓存上限，重启前确认服务器请求结束。')
                        if not continuous:
                            break
                        continue
                    self.next_source = (self.SOURCES.index(row['source']) + 1) % len(self.SOURCES)
                    self.state.save_task_rotation(self.next_source)
                    visited += 1
                    repos += int(row['source'] == 'github')
                    continue
                if not continuous and not any(thread.is_alive() for thread in self.threads):
                    break
                if time.monotonic() - last_idle_log >= 30:
                    self.runner.reporter.flush()
                    self.runner.announce('[QUEUE] ' + ('模型通道暂停；' if model_paused else '等待已准备证据；') +
                                         '，'.join(source + '=' + str(self.state.prepared_count(source)) for source in self.SOURCES))
                    last_idle_log = time.monotonic()
                self.stop.wait(self.idle_interval)
        finally:
            self.stop.set()
            for thread in self.threads:
                thread.join()
            self.runner.stats['errors'] += self.collection_errors
            self.runner.stats['skipped'] += self.collection_skipped
            self.runner.reporter.flush()
            self.runner.announce('[EXIT] 当前研判已提交，预取证据与游标已保存；处理 ' + str(visited) +
                                 '；扫描 {scanned}，未变更 {skipped}，命中 {hits}，待核对 {review}，告警 {errors}。'.format(**self.runner.stats))
        if self.fatal:
            raise self.fatal
        return 0 if self.shutdown.requested else int(model_paused or bool(self.runner.stats['errors'] or self.runner.stats['review']))
