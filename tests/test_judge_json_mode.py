"""Ollama receives simple JSON mode; Python still rejects invalid verdicts."""
import json
import unittest
from unittest.mock import Mock, patch

from pic_miner.config import Sanitizer
from pic_miner.judge import Assessment, Judge, LEVEL_2


def verdict():
    return {'level': LEVEL_2, 'summary': 'PIC 加热器版图候选。',
            'physical_features': ['加热器版图'],
            'evidence': [{'id': 'E1', 'quote': 'PIC heater array'}]}


def response(value):
    return {'done': True, 'message': {'content': json.dumps(value)}}


class JsonModeTests(unittest.TestCase):
    def test_repository_and_paper_requests_use_json_without_generating_remote_schema(self):
        for mode in (None, 'paper_metadata', 'paper_fulltext'):
            with self.subTest(mode=mode):
                def post(path, payload):
                    self.assertEqual(path, '/api/chat')
                    self.assertEqual(payload['format'], 'json')
                    prompt = payload['messages'][0]['content']
                    for field in ('level', 'summary', 'physical_features', 'evidence', 'id', 'quote'):
                        self.assertIn('"' + field + '"', prompt)
                    for level in ('LEVEL_1_CONFIRMED', 'LEVEL_2_PROBABLE', 'LEVEL_3_IRRELEVANT'):
                        self.assertIn(level, prompt)
                    return response(verdict())
                client = Mock(post_json=Mock(side_effect=post))
                evidence = {'text': '[E1] metadata\nPIC heater array', 'reader_mode': mode}
                with patch.object(Assessment, 'model_json_schema', side_effect=AssertionError('remote schema generated')):
                    result = Judge(client, 'local', Sanitizer(())).assess(evidence)
                self.assertEqual(result['level'], LEVEL_2)
                client.post_json.assert_called_once()

    def test_json_mode_keeps_pydantic_type_length_and_extra_field_checks_and_quote_verification(self):
        invalid = []
        item = verdict()
        item['summary'] = 123
        invalid.append(item)
        item = verdict()
        item['summary'] = 'x' * 141
        invalid.append(item)
        item = verdict()
        item['unexpected'] = True
        invalid.append(item)
        item = verdict()
        item['evidence'] = [{'id': 'E1', 'quote': 'fabricated statement'}]
        invalid.append(item)
        for item in invalid:
            with self.subTest(item=item):
                client = Mock(post_json=Mock(side_effect=[response(item), response(verdict())]))
                result = Judge(client, 'local', Sanitizer(())).assess({'text': '[E1] metadata\nPIC heater array'})
                self.assertEqual(result, verdict())
                self.assertEqual(client.post_json.call_count, 2)
                for call in client.post_json.call_args_list:
                    self.assertEqual(call.args[1]['format'], 'json')
                self.assertIn('上次校验错误', client.post_json.call_args.args[1]['messages'][1]['content'])
