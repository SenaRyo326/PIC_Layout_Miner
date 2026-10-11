"""Paused collectors must not fetch, poll or consume their persisted resources."""
import json
import unittest
from unittest.mock import Mock

import test_cruise as cruise_fixtures
import test_high_recall_pipeline as pipeline_fixtures
from pic_miner.config import MinerError
from pic_miner.cruise import Cruise
from pic_miner.discovery import key
from pic_miner.evidence import Candidate, EvidenceBuilder
from pic_miner.pipeline import PipelineCruise
from test_pic_miner import FakeModel


class PipelinePauseTests(unittest.TestCase):
    setUp = pipeline_fixtures.PipelineTests.setUp
    tearDown = pipeline_fixtures.PipelineTests.tearDown

    def pipeline(self, model, sources=None):
        runner = pipeline_fixtures.PipelineTests.pipeline(self, model).runner
        return PipelineCruise(runner, self.discovery, self.shutdown,
                              idle_interval=.05, sources=sources)

    def prepare(self, source, identifier='1'):
        candidate = Candidate(source, identifier, 'v', source + '/chip',
                              'https://example.org/chip', 'PIC heater array')
        self.state.enqueue(source, identifier, {}, now=0)
        self.state.prepare(candidate, EvidenceBuilder(candidate, self.sanitizer).build())

    def test_default_starts_two_collectors_and_leaves_zenodo_queue_and_cache(self):
        for source in Cruise.SUPPORTED_SOURCES:
            self.prepare(source)
        self.state.enqueue('zenodo', '123', cruise_fixtures.record(), now=0)
        prepared_before = dict(self.state.next_prepared('zenodo'))
        queued_before = dict(self.state.task('zenodo', '123'))
        self.state.save_task_rotation(2)  # Resume a cursor saved by the former three-source scheduler.
        self.discovery.tick = Mock()
        self.zenodo.record = Mock(side_effect=AssertionError('paused Zenodo fetched'))
        self.zenodo.evidence = Mock(side_effect=AssertionError('paused Zenodo inspected'))
        value = {'kind': 'search', 'query': 'photonic', 'page': 47, 'watermark': 1234}
        self.state.add_cursor('zenodo', key('search', 'photonic'), value)
        cursor_before = dict(self.state.cursor('zenodo', key('search', 'photonic')))
        model = FakeModel()
        pipeline = self.pipeline(model)
        self.assertEqual(pipeline.run({'github_queries': ['photonic'], 'arxiv_queries': ['photonic'],
                                      'zenodo_queries': ['photonic']}, continuous=False), 0)
        self.assertEqual([t.name for t in pipeline.threads], ['github-collector', 'arxiv-collector'])
        self.assertTrue(all(not t.is_alive() for t in pipeline.threads))
        self.assertEqual(len(model.calls), 2)
        for source, call in zip(('github', 'arxiv'), model.calls):
            self.assertIn(source + '/chip', call[1]['messages'][1]['content'])
            self.assertEqual(call[1]['options']['num_ctx'], 8192)
        self.assertEqual(dict(self.state.next_prepared('zenodo')), prepared_before)
        self.assertEqual(dict(self.state.task('zenodo', '123')), queued_before)
        self.assertEqual(dict(self.state.cursor('zenodo', key('search', 'photonic'))), cursor_before)
        self.assertEqual({call.args[0]['source'] for call in self.discovery.tick.call_args_list},
                         {'github', 'arxiv'})
        self.zenodo.record.assert_not_called()
        self.zenodo.evidence.assert_not_called()
        self.assertFalse(self.zenodo.client.mock_calls)
        self.assertEqual(pipeline.SOURCES, ('github', 'arxiv'))

    def test_only_paused_work_exits_once_without_model_requests(self):
        self.prepare('zenodo')
        self.state.enqueue('zenodo', '123', cruise_fixtures.record(), now=0)
        model = FakeModel()
        pipeline = self.pipeline(model)
        self.assertEqual(pipeline.run({'zenodo_queries': ['photonic']}, continuous=False), 0)
        self.assertEqual(model.calls, [])
        self.assertEqual(self.state.prepared_count('zenodo'), 1)
        self.assertEqual(self.state.task('zenodo', '123')['status'], 'queued')
        self.assertFalse(self.zenodo.client.mock_calls)

    def test_explicit_reenable_consumes_saved_zenodo_without_schema_change(self):
        self.prepare('zenodo')
        model = FakeModel()
        pipeline = self.pipeline(model, sources=Cruise.SUPPORTED_SOURCES)
        self.assertEqual(pipeline.run({}, continuous=False), 0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.state.prepared_count('zenodo'), 0)
        self.assertEqual(self.state.task('zenodo', '1')['status'], 'done')
        self.assertEqual([t.name for t in pipeline.threads],
                         ['github-collector', 'arxiv-collector', 'zenodo-collector'])

    def test_invalid_source_policy_rejected(self):
        for sources in ((), ('github', 'github'), ('other',)):
            with self.subTest(sources=sources), self.assertRaises(MinerError):
                self.pipeline(FakeModel(), sources=sources)


class LegacyCruisePauseTests(unittest.TestCase):
    setUp = cruise_fixtures.CruiseTests.setUp
    tearDown = cruise_fixtures.CruiseTests.tearDown

    def test_default_skips_zenodo_tasks_and_discovery_even_when_cursor_is_enabled(self):
        cruise = Cruise(self.runner, self.discovery, self.shutdown)
        self.assertEqual(cruise.SOURCES, ('github', 'arxiv'))
        self.state.enqueue('github', '42', {}, now=0)
        self.state.enqueue('arxiv', 'paper', {}, now=0)
        self.state.enqueue('zenodo', '123', cruise_fixtures.record(), now=0)
        before = dict(self.state.task('zenodo', '123'))
        value = {'kind': 'search', 'query': 'photonic', 'page': 47, 'watermark': 1234}
        self.state.add_cursor('zenodo', key('search', 'photonic'), value)
        self.discovery.tick = Mock()
        order = []
        def task(row):
            order.append(row['source'])
            self.state.settle(row['source'], row['item_id'])
        cruise._task = task
        self.assertEqual(cruise.run({'github_queries': ['photonic'], 'arxiv_queries': ['photonic'],
                                    'zenodo_queries': ['photonic']}, continuous=False), 0)
        self.assertEqual(order, ['github', 'arxiv'])
        self.assertEqual(dict(self.state.task('zenodo', '123')), before)
        self.assertEqual(json.loads(self.state.cursor('zenodo', key('search', 'photonic'))['state_json']), value)
        self.assertEqual({call.args[0]['source'] for call in self.discovery.tick.call_args_list},
                         {'github', 'arxiv'})
        self.assertFalse(self.zenodo_remote.mock_calls)

    def test_paused_only_cursor_is_not_returned(self):
        self.state.add_cursor('zenodo', 'only', {'kind': 'search', 'query': 'photonic'})
        cruise = Cruise(self.runner, self.discovery, self.shutdown)
        self.assertIsNone(cruise._ready_cursor(10))
        restored = Cruise(self.runner, self.discovery, self.shutdown, sources=Cruise.SUPPORTED_SOURCES)
        self.assertEqual(restored._ready_cursor(10)['source'], 'zenodo')
