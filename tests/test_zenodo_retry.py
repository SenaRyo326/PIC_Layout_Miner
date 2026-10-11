"""Real Requests redirect machinery with an offline transport, plus durable task fuses."""
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from pic_miner.config import APIError, MinerError, ModelUnavailable, Sanitizer
from pic_miner.cruise import Cruise
from pic_miner.http import HTTPClient
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.shutdown import Shutdown
from pic_miner.state import State
from pic_miner.zenodo import Zenodo


def record():
    return {'id': 123, 'updated': '2026-10-08', 'metadata': {'title': 'PIC heater array',
            'description': 'fabricated photonic layout'},
            'files': [{'key': 'chip.gds', 'size': 1000, 'checksum': 'md5:abc'}]}


class Transport(requests.adapters.BaseAdapter):
    def __init__(self, responses, repeat=False):
        self.responses, self.repeat, self.sent = responses, repeat, []

    def send(self, request, **kwargs):
        self.sent.append(request)
        status, headers, body = self.responses[0 if self.repeat else len(self.sent) - 1]
        response = requests.Response()
        response.status_code, response.headers = status, requests.structures.CaseInsensitiveDict(headers)
        response.request, response.url = request, request.url
        response._content = json.dumps(body).encode() if body is not None else b''
        response.raw = io.BytesIO(response._content)
        response._content_consumed = True
        return response

    def close(self):
        return None


class ZenodoRedirectTests(unittest.TestCase):
    def client(self, responses, repeat=False, base='https://zenodo.org/api/', token=''):
        client = HTTPClient(base, 'Zenodo', min_interval=0, token=token)
        self.addCleanup(client.close)
        transport = Transport(responses, repeat)
        client.session.mount('https://', transport)
        client.session.mount('http://', transport)
        return client, transport

    def test_records_detail_302_follows_relative_location_and_parses_record(self):
        client, transport = self.client([(302, {'Location': '/api/records/124'}, None), (200, {}, record())])
        with patch.object(client.session, 'request', wraps=client.session.request) as request:
            parsed = client.get_json('/records/123/')
        self.assertEqual(parsed['id'], 123)
        self.assertEqual([item.url for item in transport.sent],
                         ['https://zenodo.org/api/records/123', 'https://zenodo.org/api/records/124'])
        self.assertTrue(request.call_args.kwargs['allow_redirects'])
        self.assertEqual(Zenodo(client, Sanitizer(())).candidate(parsed).source, 'zenodo')

    def test_http_www_base_and_web_record_url_are_canonicalized_before_network(self):
        client, transport = self.client([(200, {}, record())], base='http://www.zenodo.org/')
        self.assertEqual(client.base_url, 'https://zenodo.org/api')
        client.get_json('http://www.zenodo.org/record/123/')
        self.assertEqual(transport.sent[0].url, 'https://zenodo.org/api/records/123')

    def test_attachment_path_is_not_rewritten_to_record_rest_endpoint(self):
        client, transport = self.client([(200, {}, {'code': 'static only'})])
        client.get_text('https://zenodo.org/records/123/files/design.py?download=1')
        self.assertEqual(transport.sent[0].url, 'https://zenodo.org/records/123/files/design.py?download=1')

    def test_redirect_chain_uses_shared_pacing_for_every_hop(self):
        client, transport = self.client([(302, {'Location': '/api/records/124'}, None),
                                          (307, {'Location': '/api/records/125'}, None), (200, {}, record())])
        with patch.object(client, '_pace') as pacing:
            client.get_json('/records/123')
        self.assertEqual(pacing.call_count, 3)
        self.assertEqual(len(transport.sent), 3)

    def test_cross_host_credentials_and_https_downgrade_are_blocked_before_following(self):
        for location in ['https://untrusted.test/api/records/123', 'http://zenodo.org/api/records/123',
                         'https://user:secret@zenodo.org/api/records/123']:
            with self.subTest(location=location):
                client, transport = self.client([(302, {'Location': location}, None)], token='private-test-token')
                with self.assertRaises(APIError) as error:
                    client.get_json('/records/123')
                self.assertEqual(error.exception.status, 302)
                self.assertEqual(len(transport.sent), 1)
                self.assertNotIn(location, str(error.exception))
                self.assertNotIn('private-test-token', str(error.exception))

    def test_redirect_loop_is_bounded_and_has_structured_http_failure(self):
        client, transport = self.client([(302, {'Location': '/api/records/123'}, None)], repeat=True)
        with self.assertRaises(APIError) as error:
            client.get_json('/records/123')
        self.assertEqual(error.exception.status, 302)
        self.assertLessEqual(len(transport.sent), 6)
        self.assertEqual(client.session.max_redirects, 5)

    def test_github_and_ollama_keep_redirects_disabled(self):
        for label, method in [('GitHub', 'GET'), ('Ollama', 'POST')]:
            client = HTTPClient('https://service.test', label, min_interval=0)
            self.addCleanup(client.close)
            transport = Transport([(302, {'Location': '/next'}, None)])
            client.session.mount('https://', transport)
            with patch.object(client.session, 'request', wraps=client.session.request) as request:
                with self.assertRaises(MinerError):
                    client._request(method, 'https://service.test/first')
            self.assertFalse(request.call_args.kwargs['allow_redirects'])
            self.assertEqual(len(transport.sent), 1)

    def test_zenodo_server_error_retains_http_status_after_transport_retries(self):
        client, transport = self.client([(503, {}, None)], repeat=True)
        with patch.object(client, '_pace'), self.assertRaises(APIError) as error:
            client.get_json('/records/123')
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(len(transport.sent), 4)

    def test_real_http_rate_limit_retains_embargo_without_api_failure_classification(self):
        client, transport = self.client([(429, {'Retry-After': '60'}, None)], repeat=True)
        client.max_wait = 0
        with self.assertRaises(MinerError) as error:
            client.get_json('/records/123')
        self.assertNotIsInstance(error.exception, APIError)
        self.assertGreaterEqual(client.not_before, time.time() + 59)
        self.assertEqual(len(transport.sent), 1)

    def test_record_lookup_normalizes_trailing_slash_and_rejects_invalid_identifier(self):
        remote = Mock(get_json=Mock(return_value=record()))
        adapter = Zenodo(remote, Sanitizer(()))
        adapter.record('123/')
        remote.get_json.assert_called_once_with('/records/123')
        for invalid in ['../123', 'https://other.test/123', '１２３']:
            with self.assertRaises(MinerError):
                adapter.record(invalid)
        remote.get_json.assert_called_once()


class ZenodoTaskFuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.logs, self.sanitizer = [], Sanitizer(())
        self.remote = Mock()
        self.judge = Mock(assess=Mock(return_value={'level': 'LEVEL_2_PROBABLE', 'summary': 'PIC 版图资源',
            'physical_features': ['GDS 文件'], 'evidence': []}))
        self.runner = Runner(self.state, self.judge,
            Reporter(self.state, self.root / 'hits.md', self.sanitizer, self.logs.append, color=False),
            'test-pipeline', self.sanitizer, self.logs.append)
        self.discovery = SimpleNamespace(zenodo=Zenodo(self.remote, self.sanitizer), github=Mock(), arxiv=Mock())
        self.cruise = Cruise(self.runner, self.discovery, Shutdown(self.logs.append))
        self.state.enqueue('zenodo', '123', {'id': 123}, 'v1', now=0)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def attempt(self):
        self.cruise._task(self.state.task('zenodo', '123'))

    def test_302_404_and_410_fuse_on_third_attempt_and_leave_round_robin_queue(self):
        for status in [302, 404, 410]:
            with self.subTest(status=status):
                with self.state.connection:
                    self.state.connection.execute("UPDATE tasks SET status='queued',retry_count=0,failures=0,error=NULL WHERE source='zenodo'")
                self.remote.get_json.side_effect = APIError('Zenodo', status)
                for attempt in range(1, 4):
                    self.attempt()
                    row = self.state.task('zenodo', '123')
                    self.assertEqual(row['retry_count'], attempt)
                    self.assertEqual(row['last_http_status'], status)
                    self.assertEqual(row['status'], 'FAILED' if attempt == 3 else 'queued')
                self.assertIsNone(self.cruise._ready_task(time.time() + 100000))
        self.judge.assess.assert_not_called()
        self.assertTrue(any('[FAILED]' in line for line in self.logs))

    def test_failed_task_survives_restart_new_discovery_hints_and_review_requeue(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 410)
        for _ in range(3):
            self.attempt()
        before = dict(self.state.task('zenodo', '123'))
        self.state.close()
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.state.activate_cruise(time.time())
        self.state.enqueue('zenodo', '123', record(), 'new-version', now=0)
        self.state.requeue_reviews('test-pipeline')
        self.assertEqual(dict(self.state.task('zenodo', '123')), before)
        self.assertIsNone(self.state.next_task(time.time() + 100000, source='zenodo'))

    def test_rediscovery_does_not_reset_http_counter_or_bypass_backoff(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 404)
        for hint in ['version-2', '', 'version-3']:
            self.attempt()
            before = self.state.task('zenodo', '123')
            self.state.enqueue('zenodo', '123', {'id': 123}, hint, now=0)
            after = self.state.task('zenodo', '123')
            self.assertEqual(before['retry_count'], after['retry_count'])
            self.assertEqual(before['due_at'], after['due_at'])
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'FAILED')

    def test_http_backoff_uses_new_budget_instead_of_legacy_large_failure_count(self):
        with self.state.connection:
            self.state.connection.execute("UPDATE tasks SET failures=10 WHERE source='zenodo'")
        self.remote.get_json.side_effect = APIError('Zenodo', 404)
        with patch('pic_miner.cruise.time.time', return_value=1000):
            self.attempt()
            self.assertEqual(self.state.task('zenodo', '123')['due_at'], 1030)
            self.attempt()
            self.assertEqual(self.state.task('zenodo', '123')['due_at'], 1060)

    def test_success_after_two_http_failures_resets_counter_and_completes(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 404)
        self.attempt()
        self.attempt()
        self.remote.get_json.side_effect = None
        self.remote.get_json.return_value = record()
        self.attempt()
        row = self.state.task('zenodo', '123')
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['retry_count'], 0)
        self.assertEqual(row['failures'], 0)
        self.assertIsNone(row['last_http_status'])
        self.assertIn('<!-- pic-hit:', (self.root / 'hits.md').read_text())

    def test_attachment_http_errors_also_consume_record_budget(self):
        payload = record()
        payload['files'] = [{'key': 'README.md', 'size': 100, 'links': {
            'content': 'https://zenodo.org/records/123/files/README.md'}}]
        self.state.enqueue('zenodo', '123', payload, 'v2', now=0)
        self.remote.get_text.side_effect = APIError('Zenodo', 404)
        for _ in range(3):
            self.attempt()
        self.assertEqual(self.remote.get_text.call_count, 3)
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'FAILED')
        self.assertEqual(self.runner.stats['errors'], 3)
        self.judge.assess.assert_not_called()

    def test_rate_limit_does_not_exhaust_record_failure_budget(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 429)
        for _ in range(4):
            self.attempt()
        row = self.state.task('zenodo', '123')
        self.assertEqual(row['retry_count'], 0)
        self.assertEqual(row['status'], 'queued')

    def test_stale_task_reference_cannot_reclaim_failed_item(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 410)
        stale = self.state.task('zenodo', '123')
        for _ in range(3):
            self.attempt()
        self.cruise._task(stale)
        self.assertEqual(self.remote.get_json.call_count, 3)
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'FAILED')

    def test_model_unavailable_still_stops_run_without_http_fuse(self):
        self.remote.get_json.return_value = record()
        self.judge.assess.side_effect = ModelUnavailable('请求未确认结束。')
        with self.assertRaises(ModelUnavailable):
            self.attempt()
        self.assertEqual(self.state.task('zenodo', '123')['retry_count'], 0)

    def test_retry_counter_persists_between_attempts_and_invalid_target_does_not_block_other_sources(self):
        self.remote.get_json.side_effect = APIError('Zenodo', 410)
        self.attempt()
        self.state.close()
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.runner.state = self.cruise.state = self.state
        self.runner.reporter.state = self.state
        self.assertEqual(self.state.task('zenodo', '123')['retry_count'], 1)
        self.attempt()
        self.attempt()
        self.state.enqueue('github', '42', {}, now=0)
        self.state.enqueue('arxiv', '2610.00001', {}, now=0)
        self.assertEqual(self.cruise._ready_task(time.time())['source'], 'github')
        self.cruise.next_source = 1
        self.assertEqual(self.cruise._ready_task(time.time())['source'], 'arxiv')

    def test_old_schema_is_migrated_without_losing_tasks_and_302_backoff_is_repaired_once(self):
        old_path = self.root / 'legacy.sqlite3'
        db = sqlite3.connect(str(old_path))
        db.executescript("""CREATE TABLE tasks(source TEXT,item_id TEXT,payload_json TEXT,hint TEXT,
            status TEXT,due_at REAL,updated_at TEXT,failures INTEGER DEFAULT 0,error TEXT,
            PRIMARY KEY(source,item_id));""")
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)', ('zenodo', '999', '{"id":999}', '',
            'queued', time.time()+21600, '2026-10-08', 7, 'Zenodo HTTP 302；未记录为扫描完成。'))
        db.commit()
        db.close()
        migrated = State(old_path)
        try:
            self.assertIn('retry_count', [row['name'] for row in migrated.connection.execute('PRAGMA table_info(tasks)')])
            migrated.init_cruise()
            self.assertEqual(migrated.task('zenodo', '999')['due_at'], 0)
            self.assertEqual(migrated.task('zenodo', '999')['failures'], 7)
            migrated.settle('zenodo', '999', 12345, 'Zenodo HTTP 302；未记录为扫描完成。', http_status=302)
            migrated.init_cruise()
            self.assertEqual(migrated.task('zenodo', '999')['due_at'], 12345)
            self.assertEqual(migrated.task('zenodo', '999')['retry_count'], 1)
        finally:
            migrated.close()


if __name__ == '__main__':
    unittest.main()
