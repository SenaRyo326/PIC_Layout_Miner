"""Serial review replay from SQLite; no public source adapters or HTTP clients."""
import copy
import json

from .config import ModelUnavailable
from .evidence import Candidate
from .judge import LEVEL_1, LEVEL_2
from .state import REVIEW


class StoredEvidence:
    def __init__(self, evidence):
        self.saved = evidence

    def evidence(self, candidate):
        # Even explicit retries of incomplete evidence never fetch it again.
        return copy.deepcopy(self.saved)


class OfflineReview:
    def __init__(self, runner, shutdown):
        self.runner, self.shutdown, self.state = runner, shutdown, runner.state

    def run(self, max_items=None, include_rejected=False):
        self.runner.reporter.flush()
        rows = (self.state.offline_reassessment_scans(self.runner.pipeline_key) if include_rejected
                else self.state.offline_review_scans())
        if max_items is not None:
            rows = rows[:max_items]
        self.runner.announce('[OFFLINE START] 已存证据重评 ' + str(len(rows)) +
                             ' 项；仅调用本地 Ollama，外部采集请求为零。')
        processed = 0
        previous_retry = self.runner.retry_review
        self.runner.retry_review = True
        try:
            for row in rows:
                if self.shutdown.requested:
                    break
                with self.shutdown.item():
                    processed += 1
                    self.runner.announce('[OFFLINE ' + str(processed) + '/' + str(len(rows)) + '] ' + row['name'])
                    try:
                        evidence = self.state.load_evidence(row['evidence_json'] or '{}')
                    except (ValueError, TypeError, RecursionError):
                        evidence = {}
                    if not isinstance(evidence, dict) or not isinstance(evidence.get('text'), str) or not evidence['text'].strip():
                        old_key = (row['source'], row['resource_id'], row['version'], row['pipeline_key'])
                        self.state.needs_review(old_key, '离线重评缺少有效的已存文本证据；未重新抓取外部资源。')
                        self.runner.stats['review'] += 1
                        self.runner.reporter.flush()
                        continue
                    evidence.setdefault('matched', False)
                    evidence.setdefault('rules', [])
                    if row['pipeline_key'] != self.runner.pipeline_key:
                        evidence.pop('examined_chunks', None)
                        evidence.pop('best_assessment', None)
                    candidate = Candidate(row['source'], row['resource_id'], row['version'], row['name'], row['url'],
                                          metadata={'commit_sha': row['commit_sha']} if row['source'] == 'github' else {})
                    # Preserve the original source version and full evidence in a new protocol checkpoint.
                    self.runner.process(candidate, StoredEvidence(evidence), force_judgement=True)
        except ModelUnavailable:
            self.runner.announce('[OFFLINE STOP] 模型请求状态不明；停止后续重评，已保存结果与其余 Review。')
            raise
        finally:
            self.runner.retry_review = previous_retry
            self.runner.reporter.flush()
            counts = dict(self.state.connection.execute('SELECT level,COUNT(*) FROM report_items GROUP BY level'))
            self.runner.announce('[OFFLINE EXIT] 处理 ' + str(processed) + ' 项；当前看板 Level 1=' + str(counts.get(LEVEL_1, 0)) +
                                 '，Level 2=' + str(counts.get(LEVEL_2, 0)) + '，Review=' + str(counts.get(REVIEW, 0)) + '。')
        return 0 if self.shutdown.requested else (1 if self.runner.stats['errors'] or self.runner.stats['review'] else 0)
