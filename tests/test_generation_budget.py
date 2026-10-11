"""Regression coverage for thinking starvation and length-stop false negatives."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pic_miner.config import ModelUnavailable, Sanitizer
from pic_miner.evidence import Candidate
from pic_miner.judge import Judge, LEVEL_2
from pic_miner.offline_review import StoredEvidence
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.state import State


def verdict():
    return {'level': LEVEL_2, 'summary': 'PIC 加热器版图资源。',
            'physical_features': ['加热器版图线索'],
            'evidence': [{'id': 'E1', 'quote': 'PIC heater array'}]}


def response(content, reason='stop', thinking=''):
    return {'done': True, 'done_reason': reason, 'eval_count': 2048,
            'prompt_eval_count': 800, 'message': {'content': content, 'thinking': thinking}}


class GenerationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.sanitizer = Sanitizer(())
        self.evidence = {'text': '[E1] metadata\nPIC heater array', 'matched': True, 'rules': []}

    def test_complete_length_limited_json_is_strictly_validated_without_retry(self):
        client = Mock(post_json=Mock(return_value=response(json.dumps(verdict()) + '\n' * 100, 'length')))
        result = Judge(client, 'local', self.sanitizer).assess(self.evidence)
        self.assertEqual(result, verdict())
        client.post_json.assert_called_once()
        self.assertEqual(self.evidence['examined_chunks'], ['legacy'])

    def test_truncated_json_retries_with_more_output_space_and_no_thinking(self):
        calls = []
        client = Mock(post_json=Mock(side_effect=[response('{"level":"LEVEL_2_PROBABLE",', 'length'),
                                                 response(json.dumps(verdict()))]))
        judge = Judge(client, 'local', self.sanitizer, record_call=lambda key, metrics: calls.append(metrics))
        self.assertEqual(judge.assess(self.evidence), verdict())
        requests = [call.args[1] for call in client.post_json.call_args_list]
        self.assertEqual([p['options']['num_predict'] for p in requests], [2048, 4096])
        for p in requests:
            self.assertIs(p['think'], False)
            self.assertEqual(p['options']['num_ctx'], 8192)
            self.assertEqual(p['format'], 'json')
        retry = requests[1]['messages'][1]['content']
        self.assertIn('Token 上限', retry)
        self.assertIn('PIC heater array', retry)
        self.assertLessEqual(len(retry.split('上次校验错误')[0].removeprefix('Evidence:\n')), 1500)
        self.assertTrue(calls[0]['error'])
        self.assertEqual(calls[1]['num_predict'], 4096)
        self.assertFalse(calls[1]['error'])

    def test_length_stop_does_not_bypass_strict_types_or_quote_validation(self):
        for field, value in [('summary', 42), ('evidence', [{'id': 'E1', 'quote': 'invented layout'}]),
                             ('unexpected', True)]:
            with self.subTest(field=field):
                wrong = verdict(); wrong[field] = value
                client = Mock(post_json=Mock(side_effect=[response(json.dumps(wrong), 'length'),
                                                         response(json.dumps(verdict()))]))
                result = Judge(client, 'local', self.sanitizer).assess(dict(self.evidence))
                self.assertEqual(result, verdict())
                self.assertEqual(client.post_json.call_count, 2)

    def test_telemetry_records_thinking_size_without_storing_thought_text(self):
        calls = []
        client = Mock(post_json=Mock(return_value=response(json.dumps(verdict()), thinking='PRIVATE THOUGHT')))
        Judge(client, 'local', self.sanitizer, record_call=lambda key, metrics: calls.append(metrics)).assess(self.evidence)
        self.assertEqual(calls[0]['thinking_chars'], len('PRIVATE THOUGHT'))
        self.assertEqual(calls[0]['content_chars'], len(json.dumps(verdict())))
        self.assertNotIn('PRIVATE THOUGHT', json.dumps(calls))
        self.assertIs(calls[0]['think'], False)

    def test_failed_completed_outputs_preserve_review_and_next_item_can_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / 'state.sqlite3')
            try:
                client = Mock(post_json=Mock(side_effect=[response('{"level":', 'length'),
                    response('{"level":', 'length'), response(json.dumps(verdict()))]))
                runner = Runner(state, Judge(client, 'local', self.sanitizer),
                    Reporter(state, Path(folder) / 'hits.md', self.sanitizer, output=lambda _: None, color=False),
                    'new-protocol', self.sanitizer, output=lambda _: None)
                for item in ('broken', 'valid'):
                    runner.process(Candidate('github', item, 'sha', 'lab/' + item, 'https://github.com/lab/' + item),
                                   StoredEvidence(self.evidence))
                rows = {r['resource_id']: r for r in state.connection.execute('SELECT * FROM scans')}
                self.assertEqual(rows['broken']['status'], 'needs_review')
                self.assertTrue(rows['broken']['evidence_json'])
                self.assertEqual(rows['valid']['status'], 'complete')
                self.assertIn('lab/valid', (Path(folder) / 'hits.md').read_text())
                self.assertEqual(client.post_json.call_count, 3)
            finally:
                state.close()

    def test_unconfirmed_length_stop_never_resubmits(self):
        r = response('{"level":', 'length'); r['done'] = False
        client = Mock(post_json=Mock(return_value=r))
        with self.assertRaises(ModelUnavailable):
            Judge(client, 'local', self.sanitizer).assess(self.evidence)
        client.post_json.assert_called_once()

    def test_paper_and_repository_share_bounded_generation_policy(self):
        for mode in (None, 'paper_metadata', 'paper_fulltext'):
            with self.subTest(mode=mode):
                evidence = dict(self.evidence, reader_mode=mode)
                client = Mock(post_json=Mock(return_value=response(json.dumps(verdict()))))
                Judge(client, 'local', self.sanitizer).assess(evidence)
                payload = client.post_json.call_args.args[1]
                self.assertIs(payload['think'], False)
                self.assertEqual(payload['options']['num_predict'], 2048)
                self.assertEqual(payload['options']['num_ctx'], 8192)


if __name__ == '__main__':
    unittest.main()
