"""Bounded adaptive inference and SQLite-only review replay, without source HTTP."""
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from pic_miner.config import ModelOutputError, ModelUnavailable, Sanitizer
from pic_miner.evidence import Candidate
from pic_miner.judge import (Judge, LEVEL_1, LEVEL_2, LEVEL_3, RETRY_INSTRUCTION,
                             compact_evidence, validate_assessment)
from pic_miner.offline_review import OfflineReview
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.shutdown import Shutdown
from pic_miner.state import REVIEW, State


def result(level=LEVEL_2, quote='PIC heater array', identifier='E1'):
    return {'level': level, 'summary': 'PIC 版图设计资源。',
            'physical_features': ['加热器版图线索'] if level != LEVEL_3 else [],
            'evidence': [{'id': identifier, 'quote': quote}] if level != LEVEL_3 else []}


def response(content):
    return {'done': True, 'message': {'content': content}}


class AdaptiveInferenceTests(unittest.TestCase):
    def setUp(self):
        self.sanitizer = Sanitizer(())

    def test_retry_recompacts_original_evidence_and_ends_with_strict_instruction(self):
        client = Mock(post_json=Mock(side_effect=[response('{"level":'),
            response(json.dumps(result(quote='write_gds("chip.gds")', identifier='E91')))]))
        saved = '[E1] file inventory\n' + '\n'.join('unrelated/very_long_path_' + str(i) for i in range(400))
        saved += '\n[E91] code components/phase_shifter.py\ndef phase_shifter():\n    write_gds("chip.gds")'
        Judge(client, 'test-model', self.sanitizer).assess({'text': saved, 'optical_match': True})
        payloads = [call.args[1] for call in client.post_json.call_args_list]
        for index, payload in enumerate(payloads):
            message = payload['messages'][1]['content']
            evidence = message[len('Evidence:\n'):]
            if index:
                evidence = evidence.split('\n摘要尽量', 1)[0]
                self.assertTrue(message.endswith(RETRY_INSTRUCTION))
            self.assertLessEqual(len(evidence), 1500 if index else 3500)
            self.assertIn('[E91]', evidence)
            self.assertIn('write_gds("chip.gds")', evidence)
            self.assertEqual(payload['options']['num_ctx'], 8192)
            self.assertEqual(payload['options']['num_predict'], 4096 if index else 2048)
            self.assertFalse(payload['stream'])
            self.assertEqual(payload['format'], 'json')
        self.assertEqual(client.post_json.call_count, 2)

    def test_ranked_slices_keep_late_documents_and_component_anchors_with_original_ids(self):
        text = '[E1] file inventory\n' + 'cache/random_path.bin\n' * 400
        text += '\n[E44] Markdown docs/design.md\n' + 'ordinary prose\n' * 400
        text += 'PIC heater array with thermal tuning and electrode pads.\n'
        text += '\n[E57] code examples/phase_shifter.py\n' + '# unrelated\n' * 400
        text += 'def phase_shifter():\n    c.add_port("e1")\n    c.write_gds("chip.gds")\n'
        for budget in (3500, 1500):
            sliced = compact_evidence(text, budget)
            self.assertLessEqual(len(sliced), budget)
            self.assertIn('[E44]', sliced)
            self.assertIn('[E57]', sliced)
            self.assertIn('PIC heater array', sliced)
            self.assertIn('write_gds', sliced)
            validate_assessment(json.dumps(result(quote='PIC heater array', identifier='E44')), sliced)

    def test_harmless_prose_and_json_fences_are_stripped_before_validation(self):
        content = '裁决如下：\n```json\n' + json.dumps(result()) + '\n```\n以上为结果。'
        self.assertEqual(validate_assessment(content, '[E1] PIC heater array')['level'], LEVEL_2)

    def test_compaction_keeps_saved_line_annotations_without_adding_new_ones(self):
        saved = '[E4] metadata\nL1: PIC heater array\nL2: c.write_gds("chip.gds")'
        sliced = compact_evidence(saved, 1500)
        self.assertIn('L1: PIC heater array', sliced)
        self.assertNotIn('L2: L1:', sliced)
        self.assertNotIn('L3: L2:', sliced)
        client = Mock(post_json=Mock(return_value=response(json.dumps(result(quote='L1: PIC heater array', identifier='E4')))))
        self.assertEqual(Judge(client, 'test-model', self.sanitizer).assess({'text': saved})['level'], LEVEL_2)

    def test_quote_of_compaction_artifact_is_rejected_against_original_and_retried(self):
        client = Mock(post_json=Mock(side_effect=[response(json.dumps(result(quote='PIC heater … write_gds'))),
                                                 response(json.dumps(result(quote='PIC heater array')))]))
        saved = '[E1] metadata\nPIC heater array\nwrite_gds("chip.gds")'
        with patch('pic_miner.judge.compact_evidence', return_value='[E1] metadata\nPIC heater array\nPIC heater … write_gds'):
            verdict = Judge(client, 'test-model', self.sanitizer).assess({'text': saved})
        self.assertEqual(verdict['evidence'][0]['quote'], 'PIC heater array')
        self.assertEqual(client.post_json.call_count, 2)

    def test_fallback_preserves_strict_type_duplicate_and_ambiguity_rejections(self):
        wrong = result()
        wrong['level'] = [LEVEL_2]
        variants = [json.dumps(wrong), json.dumps(result()) + '\n' + json.dumps(result()),
                    '{"level":"LEVEL_3_IRRELEVANT","level":"LEVEL_2_PROBABLE"}',
                    '{"wrapper":' + json.dumps(result())]
        for content in variants:
            with self.subTest(content=content), self.assertRaises(ModelOutputError):
                validate_assessment('```json\n' + content + '\n```', '[E1] PIC heater array')

    def test_paper_both_attempts_fit_full_prompt_budget(self):
        client = Mock(post_json=Mock(side_effect=[response('{}'), response(json.dumps(result(LEVEL_3)))]))
        Judge(client, 'test-model', self.sanitizer).assess({
            'text': '[E1] metadata\nPIC theory.\n' + 'Abstract details.\n' * 600,
            'reader_mode': 'paper_metadata', 'optical_match': True})
        for call in client.post_json.call_args_list:
            payload = call.args[1]
            self.assertLessEqual(sum(len(message['content']) for message in payload['messages']), 6500)
            self.assertEqual(payload['options']['num_ctx'], 8192)

    def test_unconfirmed_generation_does_not_start_retry(self):
        client = Mock(post_json=Mock(return_value={'done': False, 'message': {'content': '{}'}}))
        with self.assertRaises(ModelUnavailable):
            Judge(client, 'test-model', self.sanitizer).assess({'text': '[E1] PIC heater array'})
        self.assertEqual(client.post_json.call_count, 1)


class OfflineReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / '.pic_miner' / 'state.sqlite3')
        self.sanitizer, self.logs = Sanitizer(()), []
        self.shutdown = Shutdown(self.logs.append)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def save_review(self, identifier='1', evidence=None, source='github'):
        candidate = Candidate(source, identifier, 'a' * 40, 'lab/pic_' + identifier,
                              'https://github.com/lab/pic_' + identifier,
                              metadata={'commit_sha': 'a' * 40})
        key = self.state.begin(candidate, 'old-protocol')
        if evidence is None:
            evidence = {'text': '[E1] metadata\nPIC heater array', 'matched': True,
                        'rules': ['export_script'], 'optical_match': True, 'paths': ['chip.gds']}
        self.state.save_evidence(key, evidence)
        self.state.needs_review(key, '旧模型两次校验失败。')
        return key

    def replay(self, client):
        runner = Runner(self.state, Judge(client, 'test-model', self.sanitizer),
                        Reporter(self.state, self.root / 'hits.md', self.sanitizer, self.logs.append, color=False),
                        'new-protocol', self.sanitizer, self.logs.append)
        return OfflineReview(runner, self.shutdown)

    def test_six_reviews_resolve_and_preserve_original_full_evidence_and_history(self):
        keys = [self.save_review(str(i)) for i in range(6)]
        original = [self.state.get_scan(key)['evidence_json'] for key in keys]
        levels = [LEVEL_1, LEVEL_2, LEVEL_3, LEVEL_2, LEVEL_1, LEVEL_3]
        client = Mock(post_json=Mock(side_effect=[response(json.dumps(result(level))) for level in levels]))
        self.assertEqual(self.replay(client).run(), 0)
        self.assertEqual(client.post_json.call_count, 6)
        self.assertFalse(self.state.offline_review_scans())
        self.assertEqual([self.state.get_scan(key)['evidence_json'] for key in keys], original)
        for key in keys:
            current = self.state.get_scan(key[:3] + ('new-protocol',))
            self.assertEqual(current['status'], 'complete')
            self.assertEqual(json.loads(current['evidence_json'])['text'], json.loads(self.state.get_scan(key)['evidence_json'])['text'])
            self.assertIn('examined_chunks', json.loads(current['evidence_json']))
            self.assertEqual(current['commit_sha'], 'a' * 40)
        board = (self.root / 'hits.md').read_text()
        self.assertIn('| 4 | 2 | 2 | 0 |', board)
        self.assertEqual(board.count('<!-- pic-hit:'), 4)
        self.assertNotIn('<!-- pic-review:', board)
        self.assertEqual(self.replay(client).run(), 0)
        self.assertEqual(client.post_json.call_count, 6)

    def test_incomplete_rule_miss_evidence_is_rejudged_without_refetch(self):
        self.save_review(evidence={'text': '[E1] metadata\nPIC heater array', 'matched': False,
                                  'rules': [], 'optical_match': True, 'inspection_incomplete': True})
        client = Mock(post_json=Mock(return_value=response(json.dumps(result()))))
        with patch('pic_miner.github.GitHub', side_effect=AssertionError('No source adapter')):
            self.assertEqual(self.replay(client).run(), 0)
        client.post_json.assert_called_once()
        self.assertIn('| 1 | 0 | 1 | 0 |', (self.root / 'hits.md').read_text())

    def test_missing_saved_text_remains_review_with_no_model_call(self):
        self.save_review(evidence={'matched': False, 'rules': []})
        client = Mock()
        self.assertEqual(self.replay(client).run(), 1)
        client.post_json.assert_not_called()
        self.assertEqual(len(self.state.offline_review_scans()), 1)
        self.assertIn('缺少有效的已存文本证据', (self.root / 'hits.md').read_text())

    def test_exhausted_format_retry_stays_review_and_next_item_still_runs(self):
        self.save_review('1')
        self.save_review('2')
        client = Mock(post_json=Mock(side_effect=[response('{}'), response('{}'), response(json.dumps(result()))]))
        self.assertEqual(self.replay(client).run(), 1)
        self.assertEqual(client.post_json.call_count, 3)
        self.assertEqual(len(self.state.offline_review_scans()), 1)
        board = (self.root / 'hits.md').read_text()
        self.assertIn('| 1 | 0 | 1 | 1 |', board)
        self.assertIn('最后原因', board)

    def test_non_optical_review_gets_semantic_model_verdict(self):
        self.save_review(evidence={'text': '[E1] analog electronic ASIC layout', 'matched': True,
                                  'rules': ['layout_file'], 'optical_match': False})
        client = Mock(post_json=Mock(return_value=response(json.dumps(result(LEVEL_3)))))
        self.assertEqual(self.replay(client).run(), 0)
        client.post_json.assert_called_once()
        self.assertFalse(self.state.offline_review_scans())

    def test_transport_failure_stops_remaining_items_and_preserves_review_cards(self):
        self.save_review('1')
        self.save_review('2')
        client = Mock(post_json=Mock(side_effect=ModelUnavailable('生成状态无法确认。')))
        with self.assertRaises(ModelUnavailable):
            self.replay(client).run()
        self.assertEqual(client.post_json.call_count, 1)
        self.assertEqual(len(self.state.offline_review_scans()), 2)
        self.assertIn('| 0 | 0 | 0 | 2 |', (self.root / 'hits.md').read_text())

    def test_sigint_finishes_current_item_before_exiting_and_can_resume(self):
        self.save_review('1')
        self.save_review('2')
        def infer(path, payload):
            self.assertEqual(self.shutdown.active, 1)
            self.shutdown.request(signal.SIGINT)
            return response(json.dumps(result()))
        client = Mock(post_json=Mock(side_effect=infer))
        self.assertEqual(self.replay(client).run(), 0)
        client.post_json.assert_called_once()
        self.assertEqual(len(self.state.offline_review_scans()), 1)
        self.assertIn('| 1 | 0 | 1 | 1 |', (self.root / 'hits.md').read_text())
        self.shutdown.requested = False
        client.post_json.side_effect = None
        client.post_json.return_value = response(json.dumps(result()))
        self.assertEqual(self.replay(client).run(), 0)
        self.assertEqual(client.post_json.call_count, 2)

    def test_limit_preserves_remaining_current_reviews(self):
        self.save_review('1')
        self.save_review('2')
        client = Mock(post_json=Mock(return_value=response(json.dumps(result()))))
        self.assertEqual(self.replay(client).run(max_items=1), 0)
        client.post_json.assert_called_once()
        self.assertEqual(len(self.state.offline_review_scans()), 1)

    def test_main_offline_constructs_only_token_free_ollama_client_and_ignores_seeds(self):
        self.save_review()
        self.state.close()
        config = SimpleNamespace(sanitizer=self.sanitizer, pipeline_key='new-protocol',
                                 ollama_base_url='https://model.test', ollama_model='test-model',
                                 ollama_timeout=300, http_timeout=30, max_retry_wait=30)
        client = Mock(post_json=Mock(return_value=response(json.dumps(result()))))
        args = SimpleNamespace(review_offline=True, max_items=None)
        try:
            with patch.object(main, 'ROOT', self.root), patch.object(main, 'arguments', return_value=args), \
                 patch('pic_miner.config.Config.load', return_value=config), \
                 patch.object(main, 'load_seeds', side_effect=AssertionError('No seeds needed')), \
                 patch('pic_miner.http.HTTPClient', return_value=client) as factory, \
                 patch('pic_miner.github.GitHub', side_effect=AssertionError('No GitHub')), \
                 patch('pic_miner.arxiv.Arxiv', side_effect=AssertionError('No arXiv')), \
                 patch('pic_miner.zenodo.Zenodo', side_effect=AssertionError('No Zenodo')), \
                 patch.object(main, 'log', self.logs.append):
                self.assertEqual(main.main(), 0)
            factory.assert_called_once()
            self.assertEqual(factory.call_args.args[1], 'Ollama')
            self.assertIsNone(factory.call_args.args[2])
            self.assertEqual(factory.call_args.args[3], '')
            client.get_json.assert_not_called()
            client.close.assert_called_once()
        finally:
            self.state = State(self.root / '.pic_miner' / 'state.sqlite3')


if __name__ == '__main__':
    unittest.main()
