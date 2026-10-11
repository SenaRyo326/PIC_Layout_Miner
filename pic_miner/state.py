"""SQLite checkpoints and a local report outbox."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REVIEW = 'REVIEW'
REVIEW_STATUSES = ('needs_review', 'REVIEW', 'review')
ZENODO_HTTP_RETRY_LIMIT = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS scans (
                source TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                version TEXT NOT NULL,
                pipeline_key TEXT NOT NULL,
                commit_sha TEXT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT NOT NULL,
                scanned_at TEXT,
                evidence_json TEXT,
                assessment_json TEXT,
                is_hit INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                reported_at TEXT,
                PRIMARY KEY (source, resource_id, version, pipeline_key)
            );
            CREATE INDEX IF NOT EXISTS scan_outbox ON scans(is_hit, reported_at, status);
            CREATE INDEX IF NOT EXISTS scan_review_report ON scans(status,source,resource_id);
            CREATE INDEX IF NOT EXISTS scan_latest_terminal ON scans(source,resource_id,COALESCE(scanned_at,started_at) DESC);
            CREATE TABLE IF NOT EXISTS report_items (
                source TEXT NOT NULL, item_id TEXT NOT NULL, level TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(source,item_id)
            );
            CREATE INDEX IF NOT EXISTS report_order ON report_items(level,updated_at DESC,source,item_id);
            CREATE TABLE IF NOT EXISTS report_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO report_meta VALUES('revision','0');
            INSERT OR IGNORE INTO report_meta VALUES('published','0');
            CREATE TABLE IF NOT EXISTS http_cache (
                cache_key TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_blobs (
                sha256 TEXT PRIMARY KEY, text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY, source TEXT, item_id TEXT, version TEXT,
                pipeline_key TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS model_call_item ON model_calls(source,item_id,created_at);
            CREATE TABLE IF NOT EXISTS resource_links (
                source TEXT NOT NULL, item_id TEXT NOT NULL, version TEXT NOT NULL,
                target_source TEXT NOT NULL, target_id TEXT NOT NULL, origin_url TEXT NOT NULL,
                PRIMARY KEY(source,item_id,version,target_source,target_id)
            );
        """)

        self._init_metrics()
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
            self._migrate_task_http()

    def _init_metrics(self):
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS resource_outcomes (
                source TEXT NOT NULL, item_id TEXT NOT NULL, bucket TEXT NOT NULL,
                PRIMARY KEY(source,item_id)
            );
            CREATE TABLE IF NOT EXISTS funnel_counters (
                name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0 CHECK(value>=0)
            );
        """)
        if self.connection.execute("SELECT 1 FROM report_meta WHERE key='funnel-v1'").fetchone():
            return
        # One-time historical backfill. Normal refresh reads only fixed-size counters.
        with self.connection:
            rows = self.connection.execute("""SELECT * FROM (
                SELECT scans.*,ROW_NUMBER() OVER (PARTITION BY source,resource_id
                    ORDER BY COALESCE(scanned_at,started_at) DESC,rowid DESC) AS rank
                FROM scans) WHERE rank=1""").fetchall()
            for row in rows:
                self._update_outcome(row)
            self.connection.execute("INSERT INTO report_meta VALUES('funnel-v1','1')")

    def _bump_counter(self, name, delta=1):
        updated = self.connection.execute('UPDATE funnel_counters SET value=value+? WHERE name=?', (delta, name)).rowcount
        if not updated:
            self.connection.execute('INSERT INTO funnel_counters VALUES(?,?)', (name, delta))
        self.connection.execute("UPDATE report_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        self.connection.execute("INSERT INTO report_meta VALUES('updated_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_now(),))

    def record_event(self, reason):
        allowed = {'analysis_batches', 'skip_unchanged', 'skip_review', 'source_errors',
                   'model_requests', 'quote_validation', 'output_validation', 'model_unavailable'}
        if reason not in allowed:
            raise ValueError('Unknown pipeline metric category')
        with self.connection:
            self._bump_counter('event:' + reason)

    def _update_outcome(self, row, reason=None):
        from .judge import LEVEL_1, LEVEL_2
        from .scope import REASONS, decision_reason
        if row is None:
            return
        status = row['status']
        try:
            raw = json.loads(row['evidence_json'] or '{}')
        except (ValueError, TypeError, RecursionError):
            raw = {}
        try:
            assessment = json.loads(row['assessment_json'] or '{}')
        except (ValueError, TypeError, RecursionError):
            assessment = {}
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(assessment, dict):
            assessment = {}
        if status == 'complete':
            level = assessment.get('level')
            if level == LEVEL_1:
                bucket = 'level1'
            elif level == LEVEL_2 or row['is_hit']:
                bucket = 'level2'
            else:
                category = raw.get('decision_reason') or decision_reason(assessment.get('summary', ''))
                if category == 'other_irrelevant' and not raw.get('decision_reason'):
                    category = 'legacy_unclassified'
                bucket = 'filtered:' + (category if isinstance(category, str) and category in REASONS else 'other_irrelevant')
        elif status in REVIEW_STATUSES:
            category = raw.get('validation_failure_kind') or raw.get('review_reason_category')
            if not category:
                message = row['error'] or assessment.get('reason') or ''
                category = ('quote_validation' if '引用' in message else
                            'output_validation' if '校验' in message else
                            'coverage_incomplete' if '覆盖' in message else 'review_other')
            bucket = 'review:' + (category if isinstance(category, str) and category in REASONS else 'review_other')
        elif status == 'error':
            category = reason or raw.get('error_category')
            bucket = 'error:' + (category if category in ('model_unavailable', 'source_error') else 'source_error')
        else:
            bucket = 'reading'
        key = (row['source'], row['resource_id'])
        previous = self.connection.execute('SELECT bucket FROM resource_outcomes WHERE source=? AND item_id=?', key).fetchone()
        if previous and previous[0] == bucket:
            return
        if previous:
            self._bump_counter('outcome:' + previous[0], -1)
        else:
            self._bump_counter('resources')
        self._bump_counter('outcome:' + bucket)
        self.connection.execute("""INSERT INTO resource_outcomes VALUES(?,?,?)
            ON CONFLICT(source,item_id) DO UPDATE SET bucket=excluded.bucket""", key + (bucket,))

    def funnel_metrics(self):
        return {row['name']: row['value'] for row in
                self.connection.execute('SELECT name,value FROM funnel_counters')}

    def close(self) -> None:
        self.connection.close()

    def get_scan(self, key: tuple) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM scans WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?",
            key).fetchone()

    def begin(self, candidate, pipeline_key: str) -> tuple:
        key = (candidate.source, candidate.resource_id, candidate.version, pipeline_key)
        with self.connection:
            self.connection.execute("""
                INSERT INTO scans(source,resource_id,version,pipeline_key,commit_sha,name,url,started_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(source,resource_id,version,pipeline_key)
                DO UPDATE SET name=excluded.name,url=excluded.url
            """, key + (candidate.metadata.get("commit_sha", candidate.version) if candidate.source == "github" else None,
                       candidate.name, candidate.url, utc_now()))
            previous = self.connection.execute('SELECT 1 FROM resource_outcomes WHERE source=? AND item_id=?', key[:2]).fetchone()
            if not previous:
                self._update_outcome(self.get_scan(key))
            self._bump_counter('event:analysis_batches')
        return key

    def save_evidence(self, key: tuple, evidence: dict) -> None:
        with self.connection:
            compact = self._store_blocks(evidence)
            self.connection.execute("""
                UPDATE scans SET evidence_json=?,status='evidence_ready',error=NULL
                WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?
            """, (json.dumps(compact, ensure_ascii=False),) + key)

    def _store_blocks(self, evidence):
        import hashlib
        result = dict(evidence)
        result['blocks'] = []
        for block in evidence.get('blocks', []):
            stored = dict(block)
            if 'text' in stored:
                content = stored.pop('text')
                digest = hashlib.sha256(content.encode()).hexdigest()
                self.connection.execute('INSERT OR IGNORE INTO evidence_blobs VALUES(?,?)', (digest, content))
                stored['blob_sha256'] = digest
            result['blocks'].append(stored)
        return result

    def load_evidence(self, saved):
        result = json.loads(saved) if isinstance(saved, str) else dict(saved)
        blocks = []
        for block in result.get('blocks', []):
            block = dict(block)
            if 'text' not in block and block.get('blob_sha256'):
                row = self.connection.execute('SELECT text FROM evidence_blobs WHERE sha256=?',
                                              (block['blob_sha256'],)).fetchone()
                if row is None:
                    result['inspection_incomplete'] = True
                    continue
                block['text'] = row[0]
            blocks.append(block)
        result['blocks'] = blocks
        return result

    def record_model_call(self, key, payload):
        with self.connection:
            self.connection.execute('''INSERT INTO model_calls(source,item_id,version,pipeline_key,created_at,payload_json)
                VALUES(?,?,?,?,?,?)''', tuple(key or (None,) * 4) + (utc_now(), json.dumps(payload, ensure_ascii=False)))
            self._bump_counter('event:model_requests')
            category = payload.get('error_category')
            if payload.get('error'):
                category = category or ('quote_validation' if '引用' in payload['error'] else 'output_validation')
                if category in {'quote_validation', 'output_validation', 'model_unavailable'}:
                    self._bump_counter('event:' + category)

    def dashboard_updated_at(self):
        row = self.connection.execute("SELECT value FROM report_meta WHERE key='updated_at'").fetchone()
        return row[0] if row else None

    def save_links(self, candidate, targets):
        with self.connection:
            for source, item_id in targets:
                self.connection.execute('INSERT OR IGNORE INTO resource_links VALUES(?,?,?,?,?,?)',
                    (candidate.source, candidate.resource_id, candidate.version, source, item_id, candidate.url))

    def finish(self, key: tuple, assessment: Optional[dict], is_hit: bool) -> None:
        with self.connection:
            self.connection.execute("""
                UPDATE scans SET status='complete',scanned_at=?,assessment_json=?,is_hit=?,error=NULL
                WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?
            """, (utc_now(), json.dumps(assessment, ensure_ascii=False) if assessment else None,
                   int(is_hit)) + key)
            self._sync_report(key, assessment)
            self._update_outcome(self.get_scan(key))

    def fail(self, key: tuple, safe_error: str, reason=None) -> None:
        with self.connection:
            self.connection.execute("""
                UPDATE scans SET status='error',error=?
                WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?
            """, (safe_error,) + key)
            self._update_outcome(self.get_scan(key), reason)

    def needs_review(self, key: tuple, safe_reason: str) -> None:
        """A terminal review checkpoint avoids retrying unchanged bad input forever."""
        assessment = {"status": "failed", "review_required": True, "reason": safe_reason}
        with self.connection:
            self.connection.execute("""
                UPDATE scans SET status='needs_review',scanned_at=?,assessment_json=?,
                is_hit=0,error=?,reported_at=NULL
                WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?
            """, (utc_now(), json.dumps(assessment, ensure_ascii=False), safe_reason) + key)
            self._sync_report(key, assessment, review=True)
            self._update_outcome(self.get_scan(key))

    def pending_hits(self):
        return self.connection.execute("""
            SELECT * FROM scans WHERE status='complete' AND is_hit=1 AND reported_at IS NULL
            ORDER BY scanned_at,source,resource_id
        """).fetchall()

    def mark_reported(self, key: tuple) -> None:
        with self.connection:
            self.connection.execute("""
                UPDATE scans SET reported_at=?
                WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?
            """, (utc_now(),) + key)

    def cache_get(self, key: str):
        return self.connection.execute("SELECT * FROM http_cache WHERE cache_key=?", (key,)).fetchone()

    def cache_put(self, key: str, etag: Optional[str], modified: Optional[str], payload) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT INTO http_cache(cache_key,etag,last_modified,payload) VALUES(?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET etag=excluded.etag,
                last_modified=excluded.last_modified,payload=excluded.payload
            """, (key, etag, modified, json.dumps(payload, ensure_ascii=False)))

    def init_cruise(self) -> None:
        """Additive migration: preserve all existing scans and report acknowledgments."""
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                source TEXT NOT NULL, item_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, hint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued', due_at REAL NOT NULL,
                updated_at TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                error TEXT, PRIMARY KEY(source,item_id)
            );
            CREATE INDEX IF NOT EXISTS task_due ON tasks(status,due_at,source,item_id);
            CREATE INDEX IF NOT EXISTS task_source_due ON tasks(status,source,due_at,item_id);
            CREATE TABLE IF NOT EXISTS scheduler_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases (
                source TEXT NOT NULL, alias TEXT NOT NULL, item_id TEXT NOT NULL,
                PRIMARY KEY(source,alias)
            );
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT NOT NULL, cursor_id TEXT NOT NULL,
                state_json TEXT NOT NULL, due_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                failures INTEGER NOT NULL DEFAULT 0, error TEXT,
                PRIMARY KEY(source,cursor_id)
            );
            CREATE INDEX IF NOT EXISTS cursor_due ON cursors(enabled,due_at,source,cursor_id);
            CREATE INDEX IF NOT EXISTS cursor_source_rotation ON cursors(source,enabled,cursor_id,due_at);
            CREATE INDEX IF NOT EXISTS scan_reviews ON scans(status,pipeline_key,source,resource_id);
            CREATE TABLE IF NOT EXISTS prepared (
                source TEXT NOT NULL, item_id TEXT NOT NULL, candidate_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(source,item_id)
            );
            CREATE INDEX IF NOT EXISTS prepared_order ON prepared(source,created_at,item_id);
        """)
        self._migrate_task_http()
        with self.connection:
            self.connection.execute("UPDATE tasks SET status='queued' WHERE status='processing'")
            # Give old redirect-only failures one fresh chance with the repaired
            # transport, instead of retaining their possibly hours-long backoff.
            marker = 'zenodo-redirect-retry-v1'
            if not self.connection.execute('SELECT 1 FROM scheduler_meta WHERE key=?', (marker,)).fetchone():
                self.connection.execute("""UPDATE tasks SET due_at=0,retry_count=0,last_http_status=NULL
                    WHERE source='zenodo' AND status='queued' AND error=?""",
                                        ('Zenodo HTTP 302；未记录为扫描完成。',))
                self.connection.execute('INSERT INTO scheduler_meta VALUES(?,?)', (marker, '1'))

    def _migrate_task_http(self):
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(tasks)')}
        with self.connection:
            if 'retry_count' not in columns:
                self.connection.execute('ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0')
            if 'last_http_status' not in columns:
                self.connection.execute('ALTER TABLE tasks ADD COLUMN last_http_status INTEGER')

    def task(self, source, item_id):
        return self.connection.execute("SELECT * FROM tasks WHERE source=? AND item_id=?",
                                       (source, str(item_id))).fetchone()

    def _enqueue(self, source, item_id, payload, hint, now):
        self.connection.execute("""
            INSERT INTO tasks(source,item_id,payload_json,hint,due_at,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(source,item_id) DO UPDATE SET
                payload_json=CASE WHEN tasks.status IN ('processing','prepared','FAILED') THEN tasks.payload_json ELSE excluded.payload_json END,
                status=CASE WHEN tasks.hint<>excluded.hint AND tasks.status NOT IN ('processing','prepared','FAILED')
                            AND NOT (tasks.source='zenodo' AND tasks.error IS NOT NULL)
                            THEN 'queued' ELSE tasks.status END,
                due_at=CASE WHEN tasks.hint<>excluded.hint AND tasks.status NOT IN ('processing','prepared','FAILED')
                            AND NOT (tasks.source='zenodo' AND tasks.error IS NOT NULL)
                            THEN excluded.due_at ELSE tasks.due_at END,
                hint=CASE WHEN tasks.status IN ('processing','prepared','FAILED') THEN tasks.hint ELSE excluded.hint END,
                retry_count=CASE WHEN tasks.source<>'zenodo' AND tasks.hint<>excluded.hint AND tasks.status NOT IN ('processing','prepared','FAILED')
                                 THEN 0 ELSE tasks.retry_count END,
                last_http_status=CASE WHEN tasks.source<>'zenodo' AND tasks.hint<>excluded.hint AND tasks.status NOT IN ('processing','prepared','FAILED')
                                      THEN NULL ELSE tasks.last_http_status END,
                updated_at=CASE WHEN tasks.status='FAILED' THEN tasks.updated_at ELSE excluded.updated_at END
        """, (source, str(item_id), json.dumps(payload, ensure_ascii=False), str(hint), now, utc_now()))

    def enqueue(self, source, item_id, payload, hint="", now=None):
        import time
        with self.connection:
            self._enqueue(source, item_id, payload, hint, time.time() if now is None else now)

    def alias(self, source, alias):
        row = self.connection.execute("SELECT item_id FROM aliases WHERE source=? AND alias=?",
                                      (source, alias.lower())).fetchone()
        return row[0] if row else None

    def remember_alias(self, source, alias, item_id):
        with self.connection:
            self.connection.execute("""INSERT INTO aliases(source,alias,item_id) VALUES(?,?,?)
                ON CONFLICT(source,alias) DO UPDATE SET item_id=excluded.item_id""",
                                    (source, alias.lower(), str(item_id)))

    def promote(self, source, old_id, item_id, payload, hint):
        """Merge unresolved URLs into a canonical platform ID without duplicate model calls."""
        import time
        if str(old_id) == str(item_id):
            return True
        with self.connection:
            existing = self.task(source, item_id)
            if existing and existing['status'] in {'prepared','processing','FAILED'}:
                self.connection.execute('DELETE FROM tasks WHERE source=? AND item_id=?', (source, str(old_id)))
                return False
            self._enqueue(source, item_id, payload, hint, time.time())
            self.connection.execute("UPDATE tasks SET status='processing' WHERE source=? AND item_id=?",
                                    (source, str(item_id)))
            self.connection.execute("DELETE FROM tasks WHERE source=? AND item_id=?", (source, str(old_id)))
            return True

    def prepare(self, candidate, evidence):
        from dataclasses import asdict
        with self.connection:
            compact = self._store_blocks(evidence)
            self.connection.execute('''INSERT INTO prepared VALUES(?,?,?,?,?)
                ON CONFLICT(source,item_id) DO UPDATE SET candidate_json=excluded.candidate_json,
                evidence_json=excluded.evidence_json,created_at=excluded.created_at''',
                (candidate.source, candidate.resource_id, json.dumps(asdict(candidate), ensure_ascii=False),
                 json.dumps(compact, ensure_ascii=False), utc_now()))
            self.connection.execute("UPDATE tasks SET status='prepared' WHERE source=? AND item_id=?",
                                    (candidate.source, candidate.resource_id))

    def next_prepared(self, source):
        return self.connection.execute('SELECT * FROM prepared WHERE source=? ORDER BY created_at,item_id LIMIT 1',
                                       (source,)).fetchone()

    def prepared_count(self, source):
        return self.connection.execute('SELECT COUNT(*) FROM prepared WHERE source=?', (source,)).fetchone()[0]

    def finish_prepared(self, source, item_id, due_at=None, error=None):
        with self.connection:
            self.connection.execute('DELETE FROM prepared WHERE source=? AND item_id=?', (source, item_id))
            self.settle(source, item_id, due_at, error)

    def requeue_legacy(self, pipeline_key):
        """Once per protocol: let prior rule-negative/review targets reach the new worker."""
        marker = 'recall-requeue:' + pipeline_key
        with self.connection:
            if self.connection.execute('SELECT 1 FROM scheduler_meta WHERE key=?', (marker,)).fetchone():
                return
            self.connection.execute('''UPDATE tasks SET status='queued',due_at=0
                WHERE status NOT IN ('FAILED','processing','prepared') AND EXISTS (
                  SELECT 1 FROM scans WHERE scans.source=tasks.source AND scans.resource_id=tasks.item_id)
                AND NOT EXISTS (SELECT 1 FROM scans WHERE scans.source=tasks.source
                  AND scans.resource_id=tasks.item_id AND scans.pipeline_key=?)''', (pipeline_key,))
            self.connection.execute('INSERT INTO scheduler_meta VALUES(?,?)', (marker, '1'))

    def next_task(self, now, exclude=(), source=None):
        suffix, params = '', [now]
        if source is not None:
            suffix += ' AND source=?'
            params.append(source)
        if exclude:
            suffix += ' AND (source,item_id) NOT IN (' + ','.join('(?,?)' for _ in exclude) + ')'
            for source, identifier in sorted(exclude):
                params.extend([source, identifier])
        return self.connection.execute("SELECT * FROM tasks WHERE status='queued' AND due_at<=?" + suffix +
                                       ' ORDER BY due_at,source,item_id LIMIT 1', params).fetchone()

    def task_rotation(self):
        row = self.connection.execute("SELECT value FROM scheduler_meta WHERE key='next_source'").fetchone()
        try:
            return int(row[0]) % 3 if row else 0
        except (TypeError, ValueError):
            return 0

    def save_task_rotation(self, position):
        with self.connection:
            self.connection.execute("INSERT INTO scheduler_meta(key,value) VALUES('next_source',?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(position % 3),))

    def activate_cruise(self, now):
        # Startup-only normalization: remove stale healthy multi-hour/day cooldowns.
        # Genuine failure backoff and server rate-limit embargoes remain intact.
        with self.connection:
            self.connection.execute("UPDATE cursors SET due_at=MIN(due_at,?) WHERE enabled=1 AND error IS NULL", (now + 3.5,))
            self.connection.execute("UPDATE tasks SET due_at=MIN(due_at,?) WHERE status='queued' AND error IS NULL", (now + 2.5,))

    def claim(self, source, item_id):
        with self.connection:
            result = self.connection.execute("UPDATE tasks SET status='processing' WHERE source=? AND item_id=? AND status='queued'",
                                             (source, str(item_id)))
            return result.rowcount == 1

    def settle(self, source, item_id, due_at=None, error=None, http_status=None):
        with self.connection:
            row = self.task(source, item_id)
            if row is None or row['status'] == 'FAILED':
                return row is not None and row['status'] == 'FAILED'
            counted = source == 'zenodo' and http_status is not None and 300 <= http_status < 500 and http_status != 429
            retries = row['retry_count'] + int(counted) if error else 0
            failed = source == 'zenodo' and bool(error) and retries >= ZENODO_HTTP_RETRY_LIMIT
            status = 'FAILED' if failed else ('queued' if due_at is not None else 'done')
            last_status = http_status if counted else (row['last_http_status'] if error else None)
            self.connection.execute("""UPDATE tasks SET status=?,due_at=?,error=?,
                failures=?,retry_count=?,last_http_status=?,updated_at=?
                WHERE source=? AND item_id=?""",
                                    (status, 0 if failed else (due_at or 0), error,
                                     row['failures'] + 1 if error else 0, retries, last_status,
                                     utc_now(), source, str(item_id)))
            return failed

    def disable_cursors(self):
        with self.connection:
            self.connection.execute("UPDATE cursors SET enabled=0 WHERE enabled=1")

    def requeue_reviews(self, pipeline_key):
        with self.connection:
            self.connection.execute("""UPDATE tasks SET status='queued',due_at=0 WHERE status<>'FAILED' AND (source,item_id) IN (
                SELECT source,resource_id FROM scans WHERE status='needs_review' AND pipeline_key=?)""", (pipeline_key,))

    def add_cursor(self, source, cursor_id, value, now=0):
        existing = self.cursor(source, cursor_id)
        saved = json.loads(existing['state_json']) if existing is not None else value
        enabled = int(not (saved.get('lane') == 'history' and saved.get('history_complete')))
        with self.connection:
            self.connection.execute("""INSERT INTO cursors(source,cursor_id,state_json,due_at,enabled)
                VALUES(?,?,?,?,?) ON CONFLICT(source,cursor_id) DO UPDATE SET enabled=excluded.enabled""",
                                    (source, cursor_id, json.dumps(value, ensure_ascii=False), now, enabled))

    def activate_discovery(self, now, interval, sources):
        """Remove legacy healthy cooldowns without touching errors or paused sources."""
        if not sources:
            return
        with self.connection:
            self.connection.execute("UPDATE cursors SET due_at=MIN(due_at,?) WHERE enabled=1 AND error IS NULL "
                                    "AND source IN (" + ','.join('?' for _ in sources) + ')',
                                    (now + max(3.5, interval), *sources))

    def next_discovery_cursor(self, now, exclude=(), source='github'):
        """Persistent per-source round robin among due cursors; a long query cannot monopolize discovery."""
        position = self.connection.execute('SELECT value FROM scheduler_meta WHERE key=?',
                                           ('discovery_cursor:' + source,)).fetchone()
        suffix, params = '', [source, now]
        if exclude:
            suffix += ' AND (source,cursor_id) NOT IN (' + ','.join('(?,?)' for _ in exclude) + ')'
            for excluded_source, identifier in sorted(exclude):
                params.extend([excluded_source, identifier])
        query = 'SELECT * FROM cursors WHERE source=? AND enabled=1 AND due_at<=?' + suffix
        if position is not None:
            row = self.connection.execute(query + ' AND cursor_id>? ORDER BY cursor_id LIMIT 1',
                                          (*params, position[0])).fetchone()
            if row is not None:
                return row
        return self.connection.execute(query + ' ORDER BY cursor_id LIMIT 1', params).fetchone()

    def _remember_discovery(self, row):
        self.connection.execute('INSERT INTO scheduler_meta(key,value) VALUES(?,?) '
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                ('discovery_cursor:' + row['source'], row['cursor_id']))

    def next_cursor(self, now, exclude=(), source=None):
        suffix = ""
        params = [now]
        if source is not None:
            suffix += ' AND source=?'
            params.append(source)
        if exclude:
            suffix += " AND (source,cursor_id) NOT IN (" + ",".join("(?,?)" for _ in exclude) + ")"
            for source, identifier in sorted(exclude):
                params.extend([source, identifier])
        return self.connection.execute("SELECT * FROM cursors WHERE enabled=1 AND due_at<=?" + suffix +
                                       " ORDER BY due_at,source,cursor_id LIMIT 1", params).fetchone()

    def cursor(self, source, cursor_id):
        return self.connection.execute("SELECT * FROM cursors WHERE source=? AND cursor_id=?",
                                       (source, cursor_id)).fetchone()

    def page_commit(self, row, value, jobs, due_at):
        """Queue a complete page and move its cursor in the SAME transaction."""
        import time
        with self.connection:
            for source, identifier, payload, hint in jobs:
                self._enqueue(source, identifier, payload, hint, time.time())
            finished = value.get('lane') == 'history' and bool(value.get('history_complete'))
            self.connection.execute("""UPDATE cursors SET state_json=?,due_at=?,failures=0,error=NULL,
                enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE source=? AND cursor_id=?""",
                                    (json.dumps(value, ensure_ascii=False), due_at, finished, row['source'], row['cursor_id']))
            self._remember_discovery(row)

    def cursor_fail(self, row, error, due_at):
        with self.connection:
            self.connection.execute("""UPDATE cursors SET error=?,failures=failures+1,due_at=?
                WHERE source=? AND cursor_id=?""", (error, due_at, row['source'], row['cursor_id']))
            self._remember_discovery(row)

    def wake_time(self):
        task = self.connection.execute("SELECT due_at FROM tasks WHERE status='queued' ORDER BY due_at LIMIT 1").fetchone()
        cursor = self.connection.execute("SELECT due_at FROM cursors WHERE enabled=1 ORDER BY due_at LIMIT 1").fetchone()
        values = [row[0] for row in (task, cursor) if row is not None]
        return min(values) if values else None

    def _sync_report(self, key, assessment, migrated=False, review=False):
        from .judge import HIT_LEVELS
        source, identifier = key[:2]
        if not review and (not assessment or assessment.get('level') not in HIT_LEVELS):
            changed = self.connection.execute('DELETE FROM report_items WHERE source=? AND item_id=?', (source, identifier)).rowcount
        else:
            row = self.get_scan(key)
            raw = json.loads(row['evidence_json'] or '{}')
            compact = {field: raw.get(field, []) for field in ('rules', 'paths', 'pointers', 'script_paths', 'document_paths', 'lfs_objects', 'external_hosts')}
            if review:
                from .evidence import ELECTRICAL_PATTERN, LAYOUT_EXPORT_PATTERN, MANUFACTURING_PATTERN, PORT_PATTERN, snippets
                labels = {'layout_file': '发现 GDS/OAS 文件路径', 'export_script': '静态导出脚本线索',
                          'release_asset': 'Release 版图附件线索', 'layout_archive': '版图压缩包线索',
                          'lfs_layout_object': 'Git LFS 原始版图对象指针', 'external_hosted_layout': '外部托管版图入口',
                          'paper_open_layout_claim': '论文开放版图声明', 'paper_open_design_resource': '论文公开代码/PDK/数据入口',
                          'strong_description': '强制造/版图工具描述'}
                features = [labels[rule] for rule in compact['rules'] if isinstance(rule, str) and rule in labels]
                text = raw.get('text', '')
                for label, pattern in [('文档/源码提及 GDS/OAS 导出', LAYOUT_EXPORT_PATTERN),
                                       ('文档/源码提及加热、相移、电极或电气器件', ELECTRICAL_PATTERN),
                                       ('文档/源码含端口定义线索', PORT_PATTERN),
                                       ('文本含流片、制造或热调谐描述', MANUFACTURING_PATTERN)]:
                    if pattern.search(text):
                        features.append(label)
                optical = raw.get('optical_keywords', [])
                if optical:
                    features.append('光学关键词：' + '、'.join(str(word) for word in optical[:6]))
                compact['review_features'] = list(dict.fromkeys(features))[:5]
                compact['review_excerpt'] = snippets(text, 900)
            payload = {'name': row['name'], 'url': row['url'], 'version': row['version'],
                       'pipeline_key': row['pipeline_key'], 'assessment': assessment, 'evidence': compact,
                       'migrated': migrated, 'commit_sha': row['commit_sha']}
            self.connection.execute("""INSERT INTO report_items(source,item_id,level,updated_at,payload_json)
                VALUES(?,?,?,?,?) ON CONFLICT(source,item_id) DO UPDATE SET level=excluded.level,
                updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                                    (source, identifier, REVIEW if review else assessment['level'], row['scanned_at'] or row['started_at'], json.dumps(payload, ensure_ascii=False)))
            changed = 1
        if changed:
            self.connection.execute("UPDATE report_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            self.connection.execute("INSERT INTO report_meta(key,value) VALUES('updated_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_now(),))

    def migrate_reports(self):
        from .judge import LEVEL_2
        if self.connection.execute("SELECT 1 FROM report_meta WHERE key='legacy_migrated'").fetchone():
            return
        with self.connection:
            # One-time archival migration, not a polling-time task-table traversal.
            rows = self.connection.execute("""SELECT * FROM scans WHERE status='complete' AND is_hit=1
                ORDER BY scanned_at,source,resource_id""").fetchall()
            for row in rows:
                raw = json.loads(row['assessment_json'] or '{}')
                migrated = 'level' not in raw
                if migrated:
                    raw = {'level': LEVEL_2, 'summary': str(raw.get('summary') or '历史候选，待三档体系核对。')[:140],
                           'physical_features': ['历史物理设计线索，尚待新协议复核'], 'evidence': raw.get('evidence', [])[:3]}
                key = (row['source'], row['resource_id'], row['version'], row['pipeline_key'])
                self._sync_report(key, raw, migrated=migrated)
            self.connection.execute("INSERT INTO report_meta VALUES('legacy_migrated','1')")

    def migrate_review_reports(self):
        """Import existing pending reviews once, excluding resolved historical versions."""
        marker = 'review-dashboard-v1'
        if self.connection.execute('SELECT 1 FROM report_meta WHERE key=?', (marker,)).fetchone():
            return
        with self.connection:
            resources = self.connection.execute("""SELECT DISTINCT source,resource_id FROM scans
                WHERE status IN ('needs_review','REVIEW','review')""").fetchall()
            for source, identifier in resources:
                latest = self.connection.execute("""SELECT * FROM scans WHERE source=? AND resource_id=?
                    AND status IN ('complete','needs_review','REVIEW','review')
                    ORDER BY COALESCE(scanned_at,started_at) DESC,rowid DESC LIMIT 1""", (source, identifier)).fetchone()
                if latest is None or latest['status'] not in REVIEW_STATUSES:
                    continue
                try:
                    saved = json.loads(latest['assessment_json'] or '{}')
                except (ValueError, TypeError):
                    saved = {}
                reason = latest['error'] or (saved.get('reason') if isinstance(saved, dict) else None)
                if not isinstance(reason, str) or not reason.strip():
                    reason = '研判未完成或静态覆盖不足，需要人工核对。'
                assessment = {'status': 'failed', 'review_required': True, 'reason': reason}
                key = (source, identifier, latest['version'], latest['pipeline_key'])
                self._sync_report(key, assessment, review=True)
            # Rendering changes need a publish even if the database has no reviews.
            self.connection.execute("UPDATE report_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            self.connection.execute('INSERT INTO report_meta VALUES(?,?)', (marker, '1'))

    def audit_optical_reports(self):
        """Keyword absence is not a domain verdict. Retain legacy hits for model review."""
        return 0

    def dashboard_revision(self):
        rows = self.connection.execute("SELECT key,value FROM report_meta WHERE key IN ('revision','published')").fetchall()
        return {row['key']: int(row['value']) for row in rows}

    def dashboard_rows(self):
        return self.connection.execute('SELECT * FROM report_items ORDER BY level,updated_at DESC,source,item_id').fetchall()

    def dashboard_item(self, source, item_id):
        return self.connection.execute('SELECT payload_json FROM report_items WHERE source=? AND item_id=?',
                                       (source, item_id)).fetchone()

    def offline_review_scans(self):
        """Current dashboard reviews only, with an indexed lookup of saved full evidence."""
        rows = self.connection.execute("SELECT source,item_id,payload_json FROM report_items WHERE level=? "
                                       "ORDER BY updated_at DESC,source,item_id", (REVIEW,)).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            scan = self.get_scan((row['source'], row['item_id'], payload['version'], payload['pipeline_key']))
            if scan is not None and scan['status'] in REVIEW_STATUSES:
                result.append(scan)
        return result

    def offline_reassessment_scans(self, pipeline_key):
        """One current old-protocol rejection/review per resource; never replays resolved history."""
        return self.connection.execute('''SELECT s.* FROM scans s
            WHERE s.pipeline_key<>? AND (s.status IN ('needs_review','REVIEW','review')
                OR (s.status='complete' AND s.is_hit=0))
            AND s.rowid=(SELECT t.rowid FROM scans t WHERE t.source=s.source
                AND t.resource_id=s.resource_id AND t.status IN ('complete','needs_review','REVIEW','review')
                ORDER BY COALESCE(t.scanned_at,t.started_at) DESC,t.rowid DESC LIMIT 1)
            ORDER BY COALESCE(s.scanned_at,s.started_at) DESC,s.source,s.resource_id''', (pipeline_key,)).fetchall()

    def mark_dashboard_published(self, revision):
        with self.connection:
            self.connection.execute("UPDATE report_meta SET value=? WHERE key='published'", (str(revision),))
