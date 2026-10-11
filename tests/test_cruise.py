"""Offline integration tests for discovery, durability, pacing and real OS signals."""

import io
import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from pic_miner.arxiv import Arxiv, availability_text
from pic_miner.config import APIError, Config, MinerError, ModelUnavailable, Sanitizer
from pic_miner.cruise import Cruise
from pic_miner.discovery import Discovery, key, repo_job
from pic_miner.github import GitHub
from pic_miner.http import HTTPClient
from pic_miner.judge import Judge
from pic_miner.links import external_targets, target_job
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.shutdown import Shutdown, StopRequested
from pic_miner.state import State
from pic_miner.zenodo import Zenodo
from test_pic_miner import FakeGitHubHTTP, FakeModel, Response


def repo(identifier=42, name='lab/chip'):
    return {'id': identifier, 'full_name': name, 'default_branch': 'main',
            'description': 'fabricated PIC heater array', 'pushed_at': '2026-10-08T00:00:00Z'}


def record(identifier=123):
    return {'id': identifier, 'updated': '2026-10-08T00:00:00Z', 'metadata': {
        'title': 'PIC heater array', 'description': 'fabricated photonic layout'},
        'files': [{'key': 'chip.gds', 'size': 100, 'checksum': 'md5:abc'}]}


def paper(identifier='2610.00001v1', updated='2026-10-08T00:00:00Z'):
    return {'id': identifier, 'updated': updated, 'published': updated,
            'title': 'Photonic integrated circuit', 'summary': 'PIC heater array', 'comment': '', 'links': []}


def atom(entries, total=None):
    import html
    return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            '<opensearch:totalResults>' + str(len(entries) if total is None else total) + '</opensearch:totalResults>' +
            ''.join('<entry><id>https://arxiv.org/abs/' + e['id'] + '</id><title>' + e['title'] + '</title>' +
                    '<updated>' + e['updated'] + '</updated><summary>' + html.escape(e['summary']) + '</summary></entry>'
                    for e in entries) + '</feed>')


class CruiseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.logs = []
        self.sanitizer = Sanitizer(())
        self.github_remote = FakeGitHubHTTP(has_layout=True)
        self.zenodo_remote = Mock()
        self.arxiv_api, self.arxiv_content = Mock(), Mock()
        self.arxiv_content.get_text.return_value = '<p>Photonic integrated circuit heater array. ' + 'Physical design context. ' * 8 + '</p>' 
        self.discovery = Discovery(self.state, GitHub(self.github_remote, self.sanitizer, self.logs.append),
                                   Zenodo(self.zenodo_remote, self.sanitizer, self.logs.append),
                                   Arxiv(self.arxiv_api, self.arxiv_content, self.sanitizer, self.logs.append), self.logs.append)
        self.model = FakeModel()
        self.runner = Runner(self.state, Judge(self.model, 'test-model', self.sanitizer),
                             Reporter(self.state, self.root / 'hits.md', self.sanitizer, output=self.logs.append, color=False),
                             'test-pipeline', self.sanitizer, output=self.logs.append)
        self.shutdown = Shutdown(self.logs.append)
        self.cruise = Cruise(self.runner, self.discovery, self.shutdown, sources=Cruise.SUPPORTED_SOURCES)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add_cursor(self, source, value):
        self.state.add_cursor(source, 'test', value)
        return self.state.cursor(source, 'test')

    def test_default_cli_is_continuous_and_once_is_explicit(self):
        with patch('sys.argv', ['main.py']):
            self.assertTrue(main.arguments().continuous)
        with patch('sys.argv', ['main.py', '--once', '--max-items', '3']):
            args = main.arguments()
            self.assertFalse(args.continuous)
            self.assertEqual(args.max_items, 3)

    def test_task_identity_and_due_queries_use_indexes(self):
        self.state.enqueue('github', '42', repo(), 'same')
        self.state.enqueue('github', '42', repo(), 'same')
        self.assertEqual(self.state.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        plans = [self.state.connection.execute(sql, params).fetchall() for sql, params in [
            ('EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE source=? AND item_id=?', ('github', '42')),
            ("EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE status='queued' AND due_at<=? ORDER BY due_at,source,item_id LIMIT 1", (time.time(),)),
            ('EXPLAIN QUERY PLAN SELECT * FROM cursors WHERE enabled=1 AND due_at<=? ORDER BY due_at,source,cursor_id LIMIT 1', (time.time(),))]]
        for rows in plans:
            details = ' '.join(row['detail'] for row in rows)
            self.assertIn('INDEX', details)
            self.assertNotIn('SCAN tasks', details)

    def test_unchanged_discovery_does_not_reset_backoff_or_repeat_model(self):
        job = repo_job(repo())
        self.state.enqueue(*job)
        self.state.settle('github', '42', time.time() + 600, 'transient')
        previous = self.state.task('github', '42')['due_at']
        self.state.enqueue(*job)
        self.assertEqual(self.state.task('github', '42')['due_at'], previous)
        changed = repo()
        changed['pushed_at'] = '2026-10-09T00:00:00Z'
        self.state.enqueue(*repo_job(changed))
        self.assertLess(self.state.task('github', '42')['due_at'], previous)

    def test_processing_tasks_recover_on_reopen(self):
        self.state.enqueue(*repo_job(repo()))
        self.state.claim('github', '42')
        self.state.close()
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.assertEqual(self.state.task('github', '42')['status'], 'queued')
        self.assertEqual(self.state.connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_page_queue_and_cursor_move_are_atomic(self):
        row = self.add_cursor('github', {'kind': 'organization', 'value': 'lab', 'page': 1})
        with self.assertRaises(TypeError):
            self.state.page_commit(row, {'page': 2}, [repo_job(repo()), ('github', 'bad', {'unserializable': object()}, '')], time.time())
        self.assertIsNone(self.state.task('github', '42'))
        self.assertEqual(json.loads(self.state.cursor('github', 'test')['state_json'])['page'], 1)

    def test_organization_resumes_saved_page_without_restarting(self):
        row = self.add_cursor('github', {'kind': 'organization', 'value': 'lab', 'page': 2})
        self.discovery.tick(row)
        self.assertEqual(self.github_remote.calls[0][1]['page'], 2)
        self.assertIsNotNone(self.state.task('github', '42'))

    def test_search_uses_updated_desc_forks_and_splits_cap(self):
        remote = Mock()
        remote.get_json.return_value = {'items': [], 'total_count': 1500, 'incomplete_results': False}
        self.discovery.github.client = remote
        row = self.add_cursor('github', {'kind': 'search', 'query': 'phidl', 'lane': 'history', 'page': 1,
                                         'windows': [[100, 200]], 'watermark': 0, 'cycle_end': 200})
        self.discovery.tick(row)
        params = remote.get_json.call_args.args[1]
        self.assertEqual((params['sort'], params['order']), ('updated', 'desc'))
        self.assertIn('fork:true', params['q'])
        current = json.loads(self.state.cursor('github', 'test')['state_json'])
        self.assertEqual(current['windows'], [[151, 200], [100, 150]])
        self.assertEqual(current['watermark'], 0)

    def test_incomplete_search_does_not_advance_or_queue_partial_page(self):
        self.discovery.github.client = Mock()
        self.discovery.github.client.get_json.return_value = {'items': [repo()], 'total_count': 1, 'incomplete_results': True}
        row = self.add_cursor('github', {'kind': 'search', 'query': 'phidl', 'lane': 'updates', 'page': 3,
                                         'windows': [[100, 200]], 'watermark': 90, 'cycle_end': 200})
        with self.assertRaises(MinerError):
            self.discovery.tick(row)
        self.assertEqual(json.loads(self.state.cursor('github', 'test')['state_json'])['page'], 3)
        self.assertIsNone(self.state.task('github', '42'))

    def test_paginated_search_queues_100_and_only_advances_page(self):
        self.discovery.github.client = Mock()
        self.discovery.github.client.get_json.return_value = {'items': [repo(i, 'lab/r' + str(i)) for i in range(100)],
                                                             'total_count': 101, 'incomplete_results': False}
        row = self.add_cursor('github', {'kind': 'search', 'query': 'phidl', 'lane': 'history', 'page': 1,
                                         'windows': [[100, 200]], 'watermark': 0, 'cycle_end': 200})
        self.discovery.tick(row)
        self.assertEqual(json.loads(self.state.cursor('github', 'test')['state_json'])['page'], 2)
        self.assertEqual(self.state.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 100)

    def test_topology_forks_and_downstream_queries_have_separate_cursors(self):
        self.discovery.configure({'topology_repositories': ['gdsfactory/gdsfactory']})
        rows = self.state.connection.execute('SELECT state_json FROM cursors').fetchall()
        values = [json.loads(row[0]) for row in rows]
        self.assertEqual(len(values), 3)
        fork = next(v for v in values if v['kind'] == 'forks')
        self.assertEqual(fork['value'], 'gdsfactory/gdsfactory')
        for item in values:
            if item['kind'] == 'search':
                self.assertIn('in:readme', item['query'])

    def test_forks_page_injects_canonical_repository_ids(self):
        remote = Mock()
        remote.get_json.return_value = [repo()]
        self.discovery.github.client = remote
        row = self.add_cursor('github', {'kind': 'forks', 'value': 'lab/upstream', 'page': 2})
        self.discovery.tick(row)
        self.assertEqual(remote.get_json.call_args.args[0], '/repos/lab/upstream/forks')
        self.assertEqual(remote.get_json.call_args.args[1]['sort'], 'newest')
        self.assertIsNotNone(self.state.task('github', '42'))

    def test_link_extractor_whitelists_hosts_ids_and_strips_paths(self):
        result = external_targets('https://github.com/lab/chip/blob/main/a.py '
                                  'https://github.com/lab/chip.git https://doi.org/10.5281/zenodo.123 '
                                  'https://zenodo.org/records/123/files/a.gds '
                                  'https://github.com.evil.invalid/lab/chip https://secret@github.com/lab/private '
                                  'https://github.com/topics/photonics')
        self.assertEqual(result, [('github', 'lab/chip'), ('zenodo', '123')])

    def test_alias_resolves_duplicate_url_and_preserves_existing_hint(self):
        self.state.enqueue(*repo_job(repo()))
        self.state.remember_alias('github', 'lab/chip', '42')
        job = target_job(self.state, 'github', 'LAB/CHIP')
        self.assertEqual(job[1], '42')
        self.assertEqual(job[3], repo()['pushed_at'])

    def test_arxiv_atom_parsing_and_request_parameters(self):
        self.arxiv_api.get_text.return_value = atom([paper()])
        entries, total = self.discovery.arxiv.search('cat:physics.optics', 50)
        self.assertEqual(total, 1)
        self.assertEqual(entries[0]['id'], '2610.00001v1')
        params = self.arxiv_api.get_text.call_args.kwargs['params']
        self.assertEqual(params['start'], 50)
        self.assertEqual(params['sortBy'], 'lastUpdatedDate')
        self.assertEqual(params['sortOrder'], 'descending')

    def test_arxiv_refuses_entities_and_nonpaper_error_feed(self):
        for text in ('<!DOCTYPE x [<!ENTITY y "z">]><feed/>', '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/api/errors</id></entry></feed>'):
            self.arxiv_api.get_text.return_value = text
            with self.assertRaises(MinerError):
                self.discovery.arxiv.search('x', 0)

    def test_arxiv_feed_defers_links_until_verdict_and_keeps_watermark(self):
        p = paper()
        p['summary'] += ' https://github.com/lab/chip https://zenodo.org/records/123'
        self.discovery.arxiv.search = Mock(return_value=([p], 1))
        row = self.add_cursor('arxiv', {'kind': 'search', 'query': 'pic', 'start': 50, 'watermark': '', 'top': ''})
        self.discovery.tick(row)
        self.assertIsNone(self.state.task('github', 'name:lab/chip'))
        self.assertIsNone(self.state.task('zenodo', '123'))
        self.assertIsNotNone(self.state.task('arxiv', '2610.00001'))
        current = json.loads(self.state.cursor('arxiv', 'test')['state_json'])
        self.assertEqual(current['start'], 0)
        self.assertEqual(current['watermark'], p['updated'])

    def test_arxiv_pagination_keeps_unfinished_watermark(self):
        self.discovery.arxiv.search = Mock(return_value=([paper('2610.' + str(i).zfill(5) + 'v1') for i in range(50)], 80))
        row = self.add_cursor('arxiv', {'kind': 'search', 'query': 'pic', 'start': 0, 'watermark': '', 'top': ''})
        self.discovery.tick(row)
        current = json.loads(self.state.cursor('arxiv', 'test')['state_json'])
        self.assertEqual(current['start'], 50)
        self.assertEqual(current['watermark'], '')

    def test_arxiv_body_links_and_availability_claim_enter_pipeline(self):
        self.arxiv_content.get_text.return_value = ('<html><script>https://github.com/evil/script</script>'
            '<p>Photonic integrated circuit heater array.</p><h2>Code Availability</h2>'
            '<p>The open-source GDS layouts are publicly available for the photonic chip.</p>'
            '<a href="https://github.com/lab/chip">Code</a><a href="https://doi.org/10.5281/zenodo.123">Data</a></html>')
        candidate = self.discovery.arxiv.candidate(paper())
        evidence = self.discovery.arxiv.full_evidence(candidate)
        self.assertTrue(evidence['matched'])
        self.assertIn('paper_open_layout_claim', evidence['rules'])
        self.assertEqual(evidence['external_targets'], [('github', 'lab/chip'), ('zenodo', '123')])
        self.assertIn('Code Availability', evidence['text'])
        self.assertLessEqual(len(evidence['text']), 10000)

    def test_no_link_open_layout_paper_is_judged_and_reported(self):
        self.arxiv_content.get_text.return_value = ('<p>Photonic integrated circuit heater array fabricated with electrodes.</p>'
            '<h2>Data Availability</h2><p>Our GDS layouts are publicly available under an open-source license.</p>')
        p = paper()
        p['summary'] += '. Our GDS layouts are publicly available under an open-source license.'
        self.state.enqueue('arxiv', '2610.00001', p, 'v1')
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn('arxiv', (self.root / 'hits.md').read_text())
        self.assertEqual(self.model.calls[0][1]['options']['num_ctx'], 8192)

    def test_arxiv_failed_body_is_reviewed_without_poisoning_next_item(self):
        self.discovery.arxiv.evidence = self.discovery.arxiv.full_evidence
        self.arxiv_content.get_text.side_effect = APIError('arXiv', 404)
        self.arxiv_content.get_bytes.side_effect = APIError('arXiv', 404)
        p = paper()
        p['summary'] = 'We study a photonic integrated circuit.'
        self.state.enqueue('arxiv', '2610.00001', p, 'v1')
        self.state.enqueue('zenodo', '123', record(), 'v1')
        self.assertEqual(self.cruise.run({}, continuous=False), 1)
        scan = self.state.connection.execute("SELECT status FROM scans WHERE source='arxiv'").fetchone()
        self.assertEqual(scan[0], 'needs_review')
        self.assertTrue((self.root / 'hits.md').exists())

    def test_zenodo_uses_bounded_updated_window_and_resumes_page(self):
        self.zenodo_remote.get_json.return_value = {'hits': {'hits': [record()]}}
        row = self.add_cursor('zenodo', {'kind': 'search', 'query': 'gdsfactory', 'page': 3, 'watermark': 1000, 'cycle_end': 2000})
        self.discovery.tick(row)
        params = self.zenodo_remote.get_json.call_args.args[1]
        self.assertEqual(params['page'], 3)
        self.assertIn('updated:[', params['q'])
        current = json.loads(self.state.cursor('zenodo', 'test')['state_json'])
        self.assertEqual(current['watermark'], 2000)
        self.assertEqual(current['page'], 1)
        self.assertIsNotNone(self.state.task('zenodo', '123'))

    def test_source_failure_isolated_from_zenodo_and_github_tasks(self):
        self.github_remote.get_json = Mock(side_effect=APIError('GitHub', 404))
        self.zenodo_remote.get_json.return_value = {'hits': {'hits': [record()]}}
        self.assertEqual(self.cruise.run({'organizations': ['missing'], 'zenodo_queries': ['photonic']}, continuous=False), 1)
        self.assertIn('zenodo', (self.root / 'hits.md').read_text())
        cursor = self.state.cursor('github', key('organization', 'missing'))
        self.assertEqual(json.loads(cursor['state_json'])['page'], 1)
        self.assertGreater(cursor['due_at'], time.time())

    def test_repo_recheck_unchanged_sha_skips_files_and_model(self):
        self.state.enqueue(*target_job(self.state, 'github', 'lab/chip'))
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(self.state.task('github', 'name:lab/chip'))
        self.assertIsNotNone(self.state.task('github', '42'))
        self.state.settle('github', '42', 0)
        self.github_remote.calls.clear()
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(any('/git/trees/' in path or '/git/blobs/' in path for path, _ in self.github_remote.calls))
        self.github_remote.sha = 'd' * 40
        self.state.settle('github', '42', 0)
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        self.assertEqual(len(self.model.calls), 2)

    def test_real_sigint_finishes_current_model_commit_report_and_stops_next(self):
        self.state.enqueue(*target_job(self.state, 'github', 'lab/chip'))
        self.state.enqueue('zenodo', '123', record(), 'v1')
        original = self.model.post_json
        def generating(path, payload):
            os.kill(os.getpid(), signal.SIGINT)
            return original(path, payload)
        self.model.post_json = generating
        with self.shutdown.signals():
            self.assertEqual(self.cruise.run({}, continuous=True), 0)
        self.assertEqual(len(self.model.calls), 1)
        result = self.state.connection.execute('SELECT status FROM scans').fetchone()[0]
        self.assertEqual(result, 'complete')
        self.assertTrue((self.root / 'hits.md').exists())
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'queued')
        self.assertEqual(self.state.connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertTrue(any('当前条目已安全落库' in line for line in self.logs))

    def test_sigterm_handler_restored_and_idle_wait_interrupts(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.shutdown.signals():
            os.kill(os.getpid(), signal.SIGTERM)
            with self.assertRaises(StopRequested):
                self.shutdown.wait(600)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_active_item_wait_does_not_abort_after_signal(self):
        self.shutdown.request()
        with self.shutdown.item(), patch('pic_miner.shutdown.time.monotonic', side_effect=[0, 0, 2]), patch('pic_miner.shutdown.time.sleep'):
            self.shutdown.wait(1)
        self.assertTrue(self.shutdown.requested)

    def test_model_disconnect_stops_scheduler_before_next_task(self):
        self.state.enqueue(*target_job(self.state, 'github', 'lab/chip'))
        self.state.enqueue('zenodo', '123', record(), 'v1')
        self.model.post_json = Mock(side_effect=ModelUnavailable('模型状态不明'))
        with self.assertRaises(ModelUnavailable):
            self.cruise.run({}, continuous=False)
        self.assertEqual(self.model.post_json.call_count, 1)
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'queued')

    def test_rate_limit_waits_past_legacy_budget_then_resumes(self):
        controller = Mock()
        client = HTTPClient('https://api.github.com', 'GitHub', min_interval=0, max_wait=1,
                            continuous_rate_wait=True, shutdown=controller)
        client.session.request = Mock(side_effect=[Response(403, headers={
            'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(time.time() + 20), 'Retry-After': '3'}),
            Response(body={'ok': True})])
        try:
            self.assertEqual(client.get_json('/repos/lab/chip'), {'ok': True})
            self.assertEqual(client.session.request.call_count, 2)
            self.assertGreater(controller.wait.call_args.args[0], 19)
            self.assertTrue(controller.wait.call_args.kwargs['countdown'])
        finally:
            client.close()

    def test_permission_403_is_not_retried_as_rate_limit(self):
        client = HTTPClient('https://api.github.com', 'GitHub', min_interval=0, continuous_rate_wait=True)
        client.session.request = Mock(return_value=Response(403, body={'message': 'Resource not accessible'}))
        try:
            with self.assertRaises(APIError):
                client.get_json('/repos/lab/chip')
            self.assertEqual(client.session.request.call_count, 1)
        finally:
            client.close()

    def test_remaining_zero_search_header_does_not_block_core_bucket(self):
        controller = Mock()
        client = HTTPClient('https://api.github.com', 'GitHub', min_interval=0, continuous_rate_wait=True, shutdown=controller)
        client.session.request = Mock(side_effect=[Response(body={}, headers={
            'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(time.time() + 20), 'X-RateLimit-Resource': 'search'}), Response(body={})])
        try:
            client.get_json('/search/repositories')
            controller.wait.reset_mock()
            client.get_json('/repos/lab/chip')
            controller.wait.assert_not_called()
        finally:
            client.close()

    def test_config_intervals_loaded_without_exposing_secret_values(self):
        with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'https://model.invalid', 'OLLAMA_MODEL': 'test', 'GITHUB_TOKEN': 'private-token',
                                     'DISCOVERY_INTERVAL': '120', 'REPO_RECHECK_INTERVAL': '600'}, clear=True):
            config = Config.load(self.root)
        self.assertEqual(config.discovery_interval, 120)
        self.assertEqual(config.repo_recheck_interval, 600)
        self.assertNotIn('private-token', repr(config))
        with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'https://model.invalid', 'OLLAMA_MODEL': 'test', 'GITHUB_TOKEN': 'private-token',
                                     'IDLE_INTERVAL': 'nan'}, clear=True), self.assertRaises(MinerError):
            Config.load(self.root)

    def test_removed_channels_disabled_and_readded_cursor_keeps_page(self):
        self.discovery.configure({'github_queries': ['phidl']})
        identifier = key('search-history', 'phidl')
        row = self.state.cursor('github', identifier)
        value = json.loads(row['state_json'])
        value['page'] = 7
        self.state.page_commit(row, value, [], 0)
        self.discovery.configure({'zenodo_queries': ['photonic']})
        self.assertEqual(self.state.cursor('github', identifier)['enabled'], 0)
        self.assertEqual(self.state.next_cursor(time.time())['source'], 'zenodo')
        self.discovery.configure({'github_queries': ['phidl']})
        current = self.state.cursor('github', identifier)
        self.assertEqual(current['enabled'], 1)
        self.assertEqual(json.loads(current['state_json'])['page'], 7)

    def test_arxiv_history_and_new_paper_lanes_do_not_block_each_other(self):
        self.discovery.configure({'arxiv_queries': ['photonic']})
        values = [json.loads(r[0]) for r in self.state.connection.execute('SELECT state_json FROM cursors').fetchall()]
        self.assertEqual({v['lane'] for v in values}, {'history', 'updates'})
        self.assertEqual(next(v for v in values if v['lane'] == 'history')['watermark'], '')
        self.assertTrue(next(v for v in values if v['lane'] == 'updates')['watermark'])

    def test_retry_review_explicitly_requeues_completed_paper_task(self):
        self.discovery.arxiv.evidence = self.discovery.arxiv.full_evidence
        self.arxiv_content.get_text.side_effect = APIError('arXiv', 404)
        self.arxiv_content.get_bytes.side_effect = APIError('arXiv', 404)
        p = paper()
        p['summary'] = 'We study photonic integrated circuits.'
        self.state.enqueue('arxiv', '2610.00001', p, 'v1')
        self.assertEqual(self.cruise.run({}, continuous=False), 1)
        self.assertEqual(self.state.task('arxiv', '2610.00001')['status'], 'done')
        self.state.requeue_reviews('test-pipeline')
        self.assertEqual(self.state.task('arxiv', '2610.00001')['status'], 'queued')
        # Evidence remains persisted for an explicit retry; incomplete coverage may be refreshed.
        self.assertIsNotNone(self.state.get_scan(('arxiv', '2610.00001', self.discovery.arxiv.candidate(p).version, 'test-pipeline'))['evidence_json'])

    def test_availability_claim_can_span_pdf_line_breaks(self):
        from pic_miner.arxiv import CLAIM_PATTERN
        self.assertTrue(CLAIM_PATTERN.search('The photonic layouts are\npublicly available as open-source files.'))
        self.assertIn('publicly available', availability_text('Data Availability\nThe photonic layouts are\npublicly available.'))

    def test_pdf_fallback_extracts_real_text_and_link_annotation(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject, DecodedStreamObject
        except ImportError:
            self.skipTest('Install requirements.txt to test the PDF fallback.')
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        contents = DecodedStreamObject()
        contents.set_data(b'BT /F1 12 Tf 40 700 Td (Photonic integrated circuit heater array.) Tj 0 -20 Td '
                          b'(Data Availability: Our GDS layouts are publicly available as open-source files.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(contents)
        annotation = DictionaryObject({NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([FloatObject(x) for x in (40, 650, 200, 680)]),
            NameObject('/A'): writer._add_object(DictionaryObject({NameObject('/S'): NameObject('/URI'),
                                                                  NameObject('/URI'): TextStringObject('https://github.com/lab/chip')}))})
        page[NameObject('/Annots')] = ArrayObject([writer._add_object(annotation)])
        buf = io.BytesIO()
        writer.write(buf)
        self.arxiv_content.get_text.side_effect = APIError('arXiv', 404)
        self.arxiv_content.get_bytes.return_value = buf.getvalue()
        evidence = self.discovery.arxiv.full_evidence(self.discovery.arxiv.candidate(paper()))
        self.assertTrue(evidence['matched'])
        self.assertFalse(evidence['inspection_incomplete'])
        self.assertIn(('github', 'lab/chip'), evidence['external_targets'])
        self.assertIn('Our GDS layouts', evidence['text'])
        self.assertEqual(self.arxiv_content.get_bytes.call_args.kwargs['max_bytes'], 8 * 1024 * 1024)

    def test_pdf_page_limit_and_corrupt_pdf_are_safe_review_evidence(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('Install requirements.txt to test the PDF fallback.')
        writer = PdfWriter()
        for _ in range(121):
            writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        with self.assertRaises(MinerError):
            self.discovery.arxiv._pdf_text(buf.getvalue())
        self.arxiv_content.get_text.side_effect = APIError('arXiv', 404)
        self.arxiv_content.get_bytes.return_value = b'not a PDF'
        evidence = self.discovery.arxiv.full_evidence(self.discovery.arxiv.candidate(paper()))
        self.assertTrue(evidence['inspection_incomplete'])

    def test_failed_429_wait_keeps_cursor_unmoved_when_shutdown_requested(self):
        controller = Shutdown(self.logs.append)
        controller.request()
        client = HTTPClient('https://api.github.com', 'GitHub', min_interval=0, continuous_rate_wait=True, shutdown=controller)
        client.session.request = Mock(return_value=Response(429, headers={'Retry-After': '100'}))
        try:
            with self.assertRaises(StopRequested):
                client.get_json('/repos/lab/chip')
            self.assertEqual(client.session.request.call_count, 1)
        finally:
            client.close()

    def test_explicit_review_retry_refetches_unavailable_paper_body(self):
        self.discovery.arxiv.evidence = self.discovery.arxiv.full_evidence
        self.arxiv_content.get_text.side_effect = APIError('arXiv', 404)
        self.arxiv_content.get_bytes.side_effect = APIError('arXiv', 404)
        p = paper()
        p['summary'] = 'Photonic integrated circuit heater array.'
        self.state.enqueue('arxiv', '2610.00001', p, 'v1')
        self.model.level = 'LEVEL_3_IRRELEVANT'
        self.assertEqual(self.cruise.run({}, continuous=False), 1)
        self.model.level = 'LEVEL_2_PROBABLE'
        self.arxiv_content.get_text.side_effect = None
        self.arxiv_content.get_text.return_value = ('<h2>Data Availability</h2>'
            '<p>Our photonic GDS layouts are publicly available under an open-source license for the heater array.</p>')
        self.runner.retry_review = True
        self.cruise.run({}, continuous=False)
        self.assertEqual(len(self.model.calls), 2)
        result = self.state.connection.execute("SELECT status FROM scans WHERE source='arxiv'").fetchone()
        self.assertEqual(result[0], 'complete')

    def test_invalid_pdf_parser_warnings_do_not_expose_remote_text(self):
        try:
            import pypdf
        except ImportError:
            self.skipTest('Install requirements.txt to test PDF logging.')
        from contextlib import redirect_stderr
        stream = io.StringIO()
        with redirect_stderr(stream):
            with self.assertRaises(Exception):
                self.discovery.arxiv._pdf_text(b'PRIVATE_REMOTE_CONTENT invalid PDF')
        self.assertEqual(stream.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
