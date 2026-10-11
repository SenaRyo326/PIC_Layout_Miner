"""Exercise Ollama wire types and safe diagnostics through the real HTTP wrapper."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import test_high_recall_pipeline as pipeline_fixtures
from pic_miner.config import Config, MinerError, ModelUnavailable, ModelRequestRejected, Sanitizer
from pic_miner.evidence import Candidate, EvidenceBuilder
from pic_miner.http import HTTPClient
from pic_miner.judge import Judge
from pic_miner.pipeline import PipelineCruise
from test_pic_miner import Response, assessment


class OllamaRequestTests(unittest.TestCase):
    def client(self, **kwargs):
        client = HTTPClient('http://private-model.invalid:12345', 'Ollama', min_interval=0, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_keep_alive_numbers_are_numeric_on_first_request_and_validation_retry(self):
        for configured, expected in [('-1', -1), ('0', 0), ('3600', 3600), (-1, -1), (0, 0),
                                     ('5m', '5m'), ('24h', '24h'), ('30s', '30s')]:
            with self.subTest(configured=configured):
                client = self.client()
                seen = []
                def server(method, url, **kwargs):
                    payload = kwargs['json']
                    seen.append(payload)
                    if payload['keep_alive'] != expected or type(payload['keep_alive']) is not type(expected):
                        return Response(400, {'error': 'time: missing unit in duration'})
                    content = '{}' if len(seen) == 1 else json.dumps(assessment())
                    return Response(body={'done': True, 'message': {'content': content}})
                client.session.request = Mock(side_effect=server)
                result = Judge(client, 'local', Sanitizer(()), keep_alive=configured).assess({'text': '[E1] heater array'})
                self.assertEqual(result['level'], 'LEVEL_2_PROBABLE')
                self.assertEqual(len(seen), 2)
                for payload in seen:
                    self.assertEqual(payload['format'], 'json')
                    self.assertEqual(payload['options']['num_ctx'], 8192)

    def test_invalid_keep_alive_fails_before_network(self):
        client = self.client()
        client.session.request = Mock()
        for value in (True, False, [], {}, '-1m', 'forever', '1.5', -2, None):
            with self.subTest(value=value), self.assertRaises(MinerError):
                Judge(client, 'local', Sanitizer(()), keep_alive=value)
        client.session.request.assert_not_called()

    def test_dotenv_numeric_seconds_are_supported_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / '.env').write_text('OLLAMA_BASE_URL=http://model.invalid\nOLLAMA_MODEL=local\n'
                                      'GITHUB_TOKEN=private-token\nMODEL_KEEP_ALIVE=3600\n')
            config = Config.load(root)
            self.assertEqual(Judge(Mock(), config.ollama_model, config.sanitizer,
                                   keep_alive=config.model_keep_alive).keep_alive, 3600)

    def test_400_retains_bounded_error_and_never_reposts(self):
        client = self.client()
        remote = Response(400, {'error': 'time: missing unit in duration "-1"',
                                'request': {'private': 'DO_NOT_LOG_THIS'}})
        client.session.request = Mock(return_value=remote)
        with self.assertRaises(ModelRequestRejected) as raised:
            client.post_json('/api/chat', {})
        self.assertEqual(raised.exception.status, 400)
        self.assertIn('missing unit', str(raised.exception))
        self.assertNotIn('DO_NOT_LOG_THIS', str(raised.exception))
        self.assertTrue(remote.closed)
        client.session.request.assert_called_once()

    def test_error_credentials_endpoints_and_terminal_controls_are_redacted_before_truncation(self):
        private = 'private-shared-token'
        client = self.client(sanitizer=Sanitizer((private, 'private-model.invalid:12345')))
        error = ('invalid duration; ' + private + ' http://other-host.invalid:9876/path '
                 'private-model.invalid:12345 10.20.30.40:9999 Bearer secretbearer '
                 'api_key=hiddenkey \x1b[31m\n' + 'long-message ' * 1000)
        client.session.request = Mock(return_value=Response(400, {'error': error}))
        with self.assertRaises(ModelRequestRejected) as raised:
            client.post_json('/api/chat', {})
        message = str(raised.exception)
        for secret in (private, 'other-host.invalid', 'private-model.invalid', '10.20.30.40',
                       'secretbearer', 'hiddenkey', '\x1b', '\n'):
            self.assertNotIn(secret, message)
        self.assertIn('invalid duration', message)
        self.assertLess(len(message), 800)

    def test_default_client_also_hides_its_endpoint_and_token(self):
        client = self.client(token='header-token')
        client.session.request = Mock(return_value=Response(400, {
            'error': 'bad request to private-model.invalid:12345 with header-token'}))
        with self.assertRaises(ModelRequestRejected) as raised:
            client.post_json('/api/chat', {})
        self.assertNotIn('private-model.invalid', str(raised.exception))
        self.assertNotIn('header-token', str(raised.exception))

    def test_non_json_oversized_or_missing_error_fields_never_dump_raw_body(self):
        cases = []
        remote = Response(400)
        remote.body = b'<html>private secret response</html>'
        cases.append(remote)
        cases.append(Response(400, {'error': 'private secret response' * 10000}))
        cases.append(Response(400, {'echo': 'private secret response'}))
        cases.append(Response(400, {'error': {'secret': 'private secret response'}}))
        for remote in cases:
            with self.subTest(body_type=type(remote.body)):
                client = self.client()
                client.session.request = Mock(return_value=remote)
                with self.assertRaises(ModelRequestRejected) as raised:
                    client.post_json('/api/chat', {})
                self.assertNotIn('private secret response', str(raised.exception))
                self.assertTrue(remote.closed)
                client.session.request.assert_called_once()

    def test_transport_timeouts_still_pause_without_retry_or_private_trace(self):
        client = self.client()
        client.session.request = Mock(side_effect=requests.Timeout('private endpoint detail'))
        with self.assertRaises(ModelUnavailable) as raised:
            client.post_json('/api/chat', {})
        self.assertNotIsInstance(raised.exception, ModelRequestRejected)
        self.assertNotIn('private endpoint', str(raised.exception))
        client.session.request.assert_called_once()


class RejectedPipelineTests(unittest.TestCase):
    setUp = pipeline_fixtures.PipelineTests.setUp
    tearDown = pipeline_fixtures.PipelineTests.tearDown

    def test_definitive_rejection_preserves_prepared_evidence_and_logs_cause_without_repost(self):
        candidate = Candidate('github', '1', 'v', 'lab/chip', 'https://example.org', 'PIC heater array')
        self.state.enqueue('github', '1', {}, now=0)
        self.state.prepare(candidate, EvidenceBuilder(candidate, self.sanitizer).build())
        client = HTTPClient('http://model.invalid', 'Ollama', min_interval=0, sanitizer=self.sanitizer)
        self.addCleanup(client.close)
        client.session.request = Mock(return_value=Response(400, {'error': 'unsupported request parameter'}))
        runner = pipeline_fixtures.PipelineTests.pipeline(self, client).runner
        pipeline = PipelineCruise(runner, self.discovery, self.shutdown, idle_interval=.05)
        self.assertEqual(pipeline.run({}, continuous=False), 1)
        client.session.request.assert_called_once()
        self.assertEqual(self.state.prepared_count('github'), 1)
        stored = self.state.get_scan(('github', '1', 'v', 'high-recall'))
        self.assertEqual(stored['status'], 'error')
        self.assertTrue(stored['evidence_json'])
        self.assertIn('unsupported request parameter', stored['error'])
        self.assertTrue(any('[MODEL REJECTED]' in message for message in self.logs))
        self.assertFalse(any('重启前确认服务器请求结束' in message for message in self.logs))
