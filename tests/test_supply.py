"""Offline counterexamples for fair supply, finite history and recoverable paper depth."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
import test_cruise as cruise_fixtures
import test_high_recall_pipeline as pipeline_fixtures
from pic_miner.config import Config, MinerError, Sanitizer
from pic_miner.discovery import key
from pic_miner.evidence import Candidate, EvidenceBuilder
from pic_miner.pipeline import PipelineCruise
from pic_miner.seeds import normalize_seeds
from pic_miner.state import State
from test_pic_miner import FakeModel


class KeywordTests(unittest.TestCase):
    def test_groups_interleave_and_deduplicate_with_flat_queries(self):
        seeds = normalize_seeds({'github_queries': ['flat', ' a '], 'github_query_groups': {
            'devices': ['a', 'b', 'c'], 'platforms': ['d', 'e']},
            'arxiv_query_groups': {'papers': ['all:photonic']}})
        self.assertEqual(seeds['github_queries'], ['flat', 'a', 'd', 'b', 'e', 'c'])
        self.assertEqual(seeds['arxiv_queries'], ['all:photonic'])

    def test_old_seed_contract_still_works(self):
        seeds = normalize_seeds({'repositories': ['lab/chip'], 'github_queries': ['photonic']})
        self.assertEqual(seeds['repositories'], ['lab/chip'])
        self.assertEqual(seeds['github_queries'], ['photonic'])
        self.assertEqual(seeds['arxiv_queries'], [])

    def test_invalid_groups_are_rejected_before_collection(self):
        values = [{'github_query_groups': []}, {'github_query_groups': {'': ['PIC']}},
                  {'github_query_groups': {'PIC': []}}, {'arxiv_query_groups': {'x': ['']}},
                  {'arxiv_query_groups': {'x': [3]}}, {'unexpected': ['x']}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(MinerError):
                normalize_seeds(value)

    def test_cli_merges_groups_and_extra_queries_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'seeds.json'
            path.write_text(json.dumps({'github_query_groups': {'devices': ['PIC']}}))
            args = SimpleNamespace(seeds=path, org=[], repo=[], github_query=['PIC', 'heater photonic'],
                                   arxiv_query=[], zenodo_query=[])
            self.assertEqual(main.load_seeds(args, MinerError)['github_queries'], ['PIC', 'heater photonic'])

    def test_bundled_keywords_cover_materials_devices_manufacturing_and_papers(self):
        root = Path(__file__).resolve().parents[1]
        seeds = normalize_seeds(json.loads((root / 'seeds.json').read_text()))
        self.assertGreaterEqual(len(seeds['github_queries']), 125)
        self.assertGreaterEqual(len(seeds['arxiv_queries']), 24)
        text = ' '.join(seeds['github_queries']).lower()
        for marker in ('tfln', 'electrode', 'photonic mesh', 'mask design', '硅光'):
            self.assertIn(marker, text)

    def test_new_defaults_keep_safeguards_and_existing_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'OLLAMA_BASE_URL': 'http://model.invalid', 'OLLAMA_MODEL': 'test',
            'GITHUB_TOKEN': 'private-test-token'}, clear=True):
            config = Config.load(Path(tmp))
            self.assertEqual(config.discovery_interval, 2.5)
            self.assertEqual(config.prefetch_per_source, 8)
            with patch.dict(os.environ, {'DISCOVERY_INTERVAL': '30', 'PREFETCH_PER_SOURCE': '4'}):
                config = Config.load(Path(tmp))
                self.assertEqual(config.discovery_interval, 30)
                self.assertEqual(config.prefetch_per_source, 4)


class CursorSupplyTests(unittest.TestCase):
    setUp = cruise_fixtures.CruiseTests.setUp
    tearDown = cruise_fixtures.CruiseTests.tearDown

    def test_due_queries_rotate_despite_different_age_and_restart(self):
        for name, due in [('a', 20), ('b', 0), ('c', 1)]:
            self.state.add_cursor('github', name, {'kind': 'organization', 'value': name, 'page': 1}, now=due)
        seen = []
        for _ in range(2):
            row = self.state.next_discovery_cursor(100, source='github')
            seen.append(row['cursor_id'])
            self.state.page_commit(row, json.loads(row['state_json']), [], 0)
        self.state.close()
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        for _ in range(4):
            row = self.state.next_discovery_cursor(100, source='github')
            seen.append(row['cursor_id'])
            self.state.page_commit(row, json.loads(row['state_json']), [], 0)
        self.assertEqual(seen, ['a', 'b', 'c', 'a', 'b', 'c'])

    def test_rotation_skips_delayed_disabled_and_once_excluded_queries(self):
        for name in ('a', 'b', 'c'):
            self.state.add_cursor('github', name, {'kind': 'organization', 'value': name, 'page': 1})
        self.state.cursor_fail(self.state.cursor('github', 'b'), 'retry', 1000)
        self.assertEqual(self.state.next_discovery_cursor(100, source='github')['cursor_id'], 'c')
        self.assertEqual(self.state.next_discovery_cursor(100, [('github', 'c')], source='github')['cursor_id'], 'a')
        self.state.page_commit(self.state.cursor('github', 'a'), {'lane': 'history', 'history_complete': True}, [], 0)
        self.assertIsNone(self.state.next_discovery_cursor(100, [('github', 'c')], source='github'))

    def test_query_position_and_page_enqueue_roll_back_together(self):
        self.state.add_cursor('github', 'a', {'kind': 'search'})
        self.state.add_cursor('github', 'b', {'kind': 'search'})
        self.state.page_commit(self.state.cursor('github', 'a'), {'kind': 'search'}, [], 0)
        row = self.state.next_discovery_cursor(100, source='github')
        self.assertEqual(row['cursor_id'], 'b')
        with self.assertRaises(TypeError):
            self.state.page_commit(row, {'lane': 'history', 'history_complete': True},
                                   [('github', '42', {}, ''), ('github', 'bad', {'bad': object()}, '')], 0)
        self.assertIsNone(self.state.task('github', '42'))
        self.assertEqual(self.state.cursor('github', 'b')['enabled'], 1)
        self.assertEqual(self.state.next_discovery_cursor(100, source='github')['cursor_id'], 'b')

    def test_new_rotation_query_uses_source_index(self):
        plan = self.state.connection.execute('EXPLAIN QUERY PLAN SELECT * FROM cursors '
            'WHERE source=? AND enabled=1 AND due_at<=? AND cursor_id>? ORDER BY cursor_id LIMIT 1',
            ('github', 100, 'a')).fetchall()
        detail = ' '.join(row[3] for row in plan)
        self.assertIn('cursor_source_rotation', detail)
        self.assertNotIn('SCAN cursors', detail)

    def test_completed_github_history_stays_dormant_after_configuration_and_restart(self):
        self.discovery.configure({'github_queries': ['PIC']})
        identifier = key('search-history', 'PIC')
        row = self.state.cursor('github', identifier)
        value = json.loads(row['state_json'])
        value.update(windows=[[100, 200]], cycle_end=200)
        self.state.page_commit(row, value, [], 0)
        self.discovery.github.client = Mock(get_json=Mock(return_value={'items': [], 'total_count': 0}))
        self.discovery.tick(self.state.cursor('github', identifier))
        self.assertEqual(self.state.cursor('github', identifier)['enabled'], 0)
        self.assertTrue(any('历史覆盖完成' in line for line in self.logs))
        self.discovery.configure({'github_queries': ['PIC']})
        self.assertEqual(self.state.cursor('github', identifier)['enabled'], 0)
        self.assertEqual(self.state.next_discovery_cursor(time.time(), source='github')['cursor_id'],
                         key('search-updates', 'PIC'))
        self.discovery.github.client.get_json.assert_called_once()
        self.assertFalse(any('休眠 3600' in line for line in self.logs))

    def test_only_healthy_active_cooldowns_are_shortened(self):
        for source, name in [('github', 'healthy'), ('arxiv', 'failed'), ('zenodo', 'paused')]:
            self.state.add_cursor(source, name, {'kind': 'search'}, now=3600)
        self.state.cursor_fail(self.state.cursor('arxiv', 'failed'), 'API embargo', 7200)
        self.state.activate_discovery(100, 2.5, ('github', 'arxiv'))
        self.assertEqual(self.state.cursor('github', 'healthy')['due_at'], 103.5)
        self.assertEqual(self.state.cursor('arxiv', 'failed')['due_at'], 7200)
        self.assertEqual(self.state.cursor('zenodo', 'paused')['due_at'], 3600)


class PaperDepthTests(unittest.TestCase):
    setUp = cruise_fixtures.CruiseTests.setUp
    tearDown = cruise_fixtures.CruiseTests.tearDown

    def cursor(self, **changes):
        value = {'kind': 'search', 'query': 'all:photonic', 'lane': 'history', 'start': 0,
                 'watermark': '', 'top': '', 'windowed_history': True,
                 'windows': [[100, 200]], 'history_end': 200}
        value.update(changes)
        self.state.add_cursor('arxiv', 'history', value)
        return self.state.cursor('arxiv', 'history')

    def test_large_history_splits_without_prematurely_queueing_an_incomplete_page(self):
        self.discovery.arxiv.search = Mock(return_value=([cruise_fixtures.paper()], 1500))
        self.discovery.tick(self.cursor())
        saved = json.loads(self.state.cursor('arxiv', 'history')['state_json'])
        self.assertEqual(saved['windows'], [[151, 200], [100, 150]])
        self.assertEqual(saved['start'], 0)
        self.assertIsNone(self.state.task('arxiv', '2610.00001'))
        self.assertIn('submittedDate:[197001010140 TO 197001010320]',
                      self.discovery.arxiv.search.call_args.args[0])

    def test_old_watermark_never_discards_older_history_and_page_resumes_after_restart(self):
        papers = [cruise_fixtures.paper('2610.' + str(i).zfill(5) + 'v1', updated='2010-01-01T00:00:00Z')
                  for i in range(80)]
        self.discovery.arxiv.search = Mock(return_value=(papers[:50], 80))
        self.discovery.tick(self.cursor(watermark='2027-01-01T00:00:00Z'))
        saved = json.loads(self.state.cursor('arxiv', 'history')['state_json'])
        self.assertEqual(saved['start'], 50)
        self.assertEqual(saved['windows'], [[100, 200]])
        query = self.discovery.arxiv.search.call_args.args[0]
        self.state.close()
        self.state = State(self.root / 'state.sqlite3')
        self.state.init_cruise()
        self.discovery.state = self.state
        self.discovery.arxiv.search.return_value = (papers[50:], 80)
        self.discovery.tick(self.state.cursor('arxiv', 'history'))
        self.assertEqual(self.discovery.arxiv.search.call_args.args, (query, 50, 50))
        self.assertEqual(self.discovery.arxiv.search.call_args.kwargs, {'sort_by': 'submittedDate'})
        self.assertEqual(self.state.connection.execute("SELECT COUNT(*) FROM tasks WHERE source='arxiv'").fetchone()[0], 80)
        self.assertEqual(self.state.cursor('arxiv', 'history')['enabled'], 0)
        self.assertTrue(json.loads(self.state.cursor('arxiv', 'history')['state_json'])['history_complete'])

    def test_completed_window_advances_to_older_window_instead_of_latest_page(self):
        self.discovery.arxiv.search = Mock(return_value=([cruise_fixtures.paper()], 1))
        self.discovery.tick(self.cursor(windows=[[151, 200], [100, 150]]))
        saved = json.loads(self.state.cursor('arxiv', 'history')['state_json'])
        self.assertEqual(saved['windows'], [[100, 150]])
        self.assertEqual(saved['start'], 0)
        self.assertNotIn('history_complete', saved)
        self.discovery.tick(self.state.cursor('arxiv', 'history'))
        self.assertIn('submittedDate:[197001010140 TO 197001010230]',
                      self.discovery.arxiv.search.call_args.args[0])

    def test_inconsistent_empty_page_is_retryable_without_losing_window_or_tasks(self):
        row = self.cursor(start=50)
        before = dict(row)
        self.discovery.arxiv.search = Mock(return_value=([], 80))
        with self.assertRaises(MinerError):
            self.discovery.tick(row)
        self.assertEqual(dict(self.state.cursor('arxiv', 'history')), before)

    def test_malformed_historical_paper_does_not_advance_window(self):
        row = self.cursor()
        before = dict(row)
        bad = cruise_fixtures.paper()
        bad['updated'] = 'not-a-date'
        self.discovery.arxiv.search = Mock(return_value=([bad], 1))
        with self.assertRaises(ValueError):
            self.discovery.tick(row)
        self.assertEqual(dict(self.state.cursor('arxiv', 'history')), before)

    def test_legacy_history_cursor_is_reanchored_once_and_not_shallow_update_filtered(self):
        value = {'kind': 'search', 'lane': 'history', 'query': 'all:photonic', 'start': 9000,
                 'watermark': '2027-01-01T00:00:00Z', 'top': ''}
        self.state.add_cursor('arxiv', 'history', value)
        self.discovery.arxiv.search = Mock(return_value=([cruise_fixtures.paper()], 1))
        self.discovery.tick(self.state.cursor('arxiv', 'history'))
        self.assertEqual(self.discovery.arxiv.search.call_args.args[1], 0)
        self.assertIn('submittedDate:[199101010000 TO ', self.discovery.arxiv.search.call_args.args[0])
        self.assertIsNotNone(self.state.task('arxiv', '2610.00001'))
        self.assertTrue(json.loads(self.state.cursor('arxiv', 'history')['state_json'])['windowed_history'])

    def test_arxiv_request_uses_stable_submitted_order_only_for_history(self):
        self.arxiv_api.get_text.return_value = cruise_fixtures.atom([])
        self.discovery.arxiv.search('all:photonic', 0, sort_by='submittedDate')
        self.assertEqual(self.arxiv_api.get_text.call_args.kwargs['params']['sortBy'], 'submittedDate')
        self.discovery.arxiv.search('all:photonic', 0)
        self.assertEqual(self.arxiv_api.get_text.call_args.kwargs['params']['sortBy'], 'lastUpdatedDate')

    def test_completed_paper_history_does_not_issue_more_api_calls(self):
        self.discovery.arxiv.search = Mock()
        self.state.add_cursor('arxiv', key('search-history', 'all:photonic'),
                              {'lane': 'history', 'history_complete': True, 'query': 'all:photonic'})
        self.discovery.configure({'arxiv_queries': ['all:photonic']})
        self.assertEqual(self.state.cursor('arxiv', key('search-history', 'all:photonic'))['enabled'], 0)
        self.assertEqual(self.state.next_discovery_cursor(time.time(), source='arxiv')['cursor_id'],
                         key('search-updates', 'all:photonic'))
        self.discovery.arxiv.search.assert_not_called()


class PipelineSupplyTests(unittest.TestCase):
    setUp = pipeline_fixtures.PipelineTests.setUp
    tearDown = pipeline_fixtures.PipelineTests.tearDown

    def pipeline(self, model):
        runner = pipeline_fixtures.PipelineTests.pipeline(self, model).runner
        return PipelineCruise(runner, self.discovery, self.shutdown, idle_interval=.05)

    def test_empty_buffer_prepares_existing_tasks_before_discovery_and_rotates_after_two_skips(self):
        for i in range(3):
            self.state.enqueue('github', str(i), {'full_name': 'lab/r' + str(i)}, now=0)
        for name in ('a', 'b'):
            self.state.add_cursor('github', name, {'kind': 'search'})
        pipeline = self.pipeline(FakeModel())
        events = []
        def prepare(row):
            events.append('task:' + row['item_id'])
            self.state.settle('github', row['item_id'])
        pipeline._prepare = prepare
        self.discovery.tick = lambda row: events.append('query:' + row['cursor_id'])
        pipeline._collect('github', False)
        self.assertEqual(events, ['task:0', 'task:1', 'query:a', 'task:2', 'query:b'])

    def test_full_buffer_applies_backpressure_without_issuing_discovery_requests(self):
        pipeline = self.pipeline(FakeModel())
        pipeline.prefetch_per_source = 2
        for i in range(2):
            candidate = Candidate('github', str(i), 'v', 'lab/chip', 'https://example.org', 'PIC heater array')
            self.state.enqueue('github', str(i), {}, now=0)
            self.state.prepare(candidate, EvidenceBuilder(candidate, self.sanitizer).build())
        self.discovery.tick = Mock()
        with patch.object(pipeline.stop, 'wait', side_effect=lambda delay: pipeline.stop.set()):
            pipeline._collect('github', True)
        self.discovery.tick.assert_not_called()
        self.assertEqual(self.state.prepared_count('github'), 2)

    def test_empty_github_query_cannot_block_another_query_or_paper_inference(self):
        original = self.github.client.get_json
        searched = []
        def github(path, params=None, **kwargs):
            if path == '/search/repositories':
                searched.append(params['q'])
                records = [] if 'first_empty' in params['q'] else [cruise_fixtures.repo()]
                return {'items': records, 'total_count': len(records), 'incomplete_results': False}
            return original(path, params, **kwargs)
        self.github.client.get_json = github
        self.arxiv.search = Mock(return_value=([cruise_fixtures.paper()], 1))
        self.arxiv.full_evidence = lambda candidate: EvidenceBuilder(candidate, self.sanitizer).build()
        model = FakeModel()
        pipeline = self.pipeline(model)
        self.assertEqual(pipeline.run({'github_query_groups': {'first': ['first_empty'], 'second': ['second_hit']},
                                      'arxiv_query_groups': {'papers': ['all:photonic']}}, continuous=False), 0)
        self.assertTrue(any('first_empty' in query for query in searched))
        self.assertTrue(any('second_hit' in query for query in searched))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.state.task('github', '42')['status'], 'queued')
        self.assertEqual(self.state.task('arxiv', '2610.00001')['status'], 'done')
        self.assertEqual([thread.name for thread in pipeline.threads], ['github-collector', 'arxiv-collector'])
        self.assertFalse(self.zenodo.client.mock_calls)
        for _, payload in model.calls:
            self.assertEqual(payload['format'], 'json')
            self.assertEqual(payload['options']['num_ctx'], 8192)
