"""Offline integration tests for resource discovery and irreversible failure edges."""

import base64
import io
import json
import math
import os
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import main
from pic_miner.config import Config, MAX_EVIDENCE_CHARS, MAX_SOURCE_BYTES, MinerError, ModelOutputError, ModelUnavailable, Sanitizer
from pic_miner.evidence import Candidate, EvidenceBuilder, FileEntry, is_layout, plain_text, snippets, strong_description
from pic_miner.github import GitHub
from pic_miner.http import HTTPClient
from pic_miner.judge import Judge, validate_assessment
from pic_miner.report import Reporter
from pic_miner.runner import Runner, run_lock
from pic_miner.state import State
from pic_miner.zenodo import Zenodo


def assessment(level='LEVEL_2_PROBABLE', quote="heater array", section="E1"):
    return {"level": level, "summary": "多器件 PIC，含热调谐线索，值得检查。",
            "physical_features": ["热调谐与电极线索"],
            "evidence": [{"id": section, "quote": quote}]}


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = json.dumps(body).encode() if body is not None else b""
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeGitHubHTTP:
    def __init__(self, has_layout=False, script=None):
        self.calls = []
        self.sha = "a" * 40
        self.has_layout = has_layout
        self.script = script or "import gdsfactory as gf\nc = gf.Component()\nc.write_gds('chip.gds')\n"

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        repo = {"id": 42, "full_name": "lab/chip", "default_branch": "main", "size": 0,
                "description": "fabricated PIC heater array", "topics": ["photonics"]}
        if path.startswith("/orgs/"):
            return [repo]
        if path == "/repos/lab/chip":
            return repo
        if path.endswith("/releases"):
            return []
        if "/git/ref/heads/" in path:
            return {"object": {"type": "commit", "sha": self.sha}}
        if "/git/trees/" in path:
            tree = [{"path": "design.py", "type": "blob", "sha": "b" * 40, "size": len(self.script)}]
            if self.has_layout:
                tree.append({"path": "chip.gds", "type": "blob", "sha": "c" * 40, "size": 500})
            return {"tree": tree, "truncated": False}
        if "/git/blobs/" in path:
            return {"encoding": "base64", "content": base64.b64encode(self.script.encode()).decode()}
        raise AssertionError("Unexpected fake request path")

    def close(self):
        self.closed = True


class FakeModel:
    def __init__(self, level="LEVEL_2_PROBABLE"):
        self.calls = []
        self.level = level

    def post_json(self, path, payload):
        self.calls.append((path, payload))
        value = assessment(self.level)
        if self.level == "LEVEL_3_IRRELEVANT":
            value.update(physical_features=[], evidence=[])
        return {"done": True, "message": {"content": json.dumps(value, ensure_ascii=False)}}

    def close(self):
        self.closed = True


class MinerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = State(self.root / "state.sqlite3")
        self.sanitizer = Sanitizer(())
        self.messages = []

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def runner(self, model):
        reporter = Reporter(self.state, self.root / "hits.md", self.sanitizer,
                            output=self.messages.append, color=False)
        return Runner(self.state, Judge(model, "test-model", self.sanitizer), reporter,
                      "test-pipeline", self.sanitizer, self.messages.append)

    def test_full_script_pipeline_and_unchanged_checkpoint_across_reopen(self):
        github_http = FakeGitHubHTTP()
        github = GitHub(github_http, self.sanitizer, self.messages.append)
        model = FakeModel("LEVEL_2_PROBABLE")
        seeds = {"organizations": ["lab"], "repositories": ["lab/chip"], "zenodo_queries": []}
        zenodo = Mock()
        zenodo.candidates.return_value = []
        runner = self.runner(model)
        self.assertEqual(runner.run(github, zenodo, seeds), 0)
        self.assertEqual(len(model.calls), 1)
        self.assertIn("Level 2", (self.root / "hits.md").read_text())
        self.state.close()
        self.state = State(self.root / "state.sqlite3")
        github_http.calls.clear()
        self.assertEqual(self.runner(model).run(github, zenodo, seeds), 0)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(any("/git/trees/" in path or "/git/blobs/" in path for path, _ in github_http.calls))
        self.assertEqual((self.root / "hits.md").read_text().count("<!-- pic-hit:"), 1)
        row = self.state.connection.execute("SELECT commit_sha,scanned_at,status FROM scans").fetchone()
        self.assertEqual(row["commit_sha"], "a" * 40)
        self.assertIsNotNone(row["scanned_at"])
        self.assertEqual(row["status"], "complete")

    def test_changed_commit_is_scanned_again(self):
        remote = FakeGitHubHTTP(has_layout=True)
        adapter = GitHub(remote, self.sanitizer)
        repo = remote.get_json("/repos/lab/chip")
        model = FakeModel()
        runner = self.runner(model)
        runner.process(adapter.candidate(repo), adapter)
        remote.sha = "d" * 40
        runner.process(adapter.candidate(repo), adapter)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual((self.root / "hits.md").read_text().count("<!-- pic-hit:"), 1)

    def test_rule_miss_is_model_judged_and_checkpointed(self):
        remote = FakeGitHubHTTP(script="print('unrelated example')")
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json("/repos/lab/chip"))
        candidate.description = "PIC helper library"
        model = FakeModel("LEVEL_3_IRRELEVANT")
        runner = self.runner(model)
        runner.process(candidate, adapter)
        runner.process(candidate, adapter)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(runner.stats["rule_miss"], 0)
        self.assertEqual(runner.stats["skipped"], 1)
        self.assertIn('扫描与过滤漏斗', (self.root/'hits.md').read_text())
        self.assertNotIn('<!-- pic-hit:', (self.root/'hits.md').read_text())

    def test_model_failure_resumes_without_refetching_files(self):
        remote = FakeGitHubHTTP(has_layout=True)
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json("/repos/lab/chip"))
        failed = Mock()
        failed.post_json.side_effect = ModelUnavailable("model unavailable")
        with self.assertRaises(ModelUnavailable):
            self.runner(failed).process(candidate, adapter)
        key = ("github", "42", candidate.version, "test-pipeline")
        row = self.state.get_scan(key)
        self.assertEqual(row["status"], "error")
        self.assertIsNotNone(row["evidence_json"])
        remote.calls.clear()
        self.runner(FakeModel()).process(candidate, adapter)
        self.assertEqual(remote.calls, [])
        self.assertEqual(self.state.get_scan(key)["status"], "complete")

    def test_report_failure_preserves_completed_model_result(self):
        remote = FakeGitHubHTTP(has_layout=True)
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json("/repos/lab/chip"))
        model = FakeModel()
        runner = self.runner(model)
        with patch("pic_miner.report.os.replace", side_effect=OSError("disk unavailable")):
            runner.process(candidate, adapter)
        row = self.state.get_scan(("github", "42", candidate.version, "test-pipeline"))
        self.assertEqual(row["status"], "complete")
        self.assertIsNone(row["reported_at"])
        runner.reporter.flush()
        runner.process(candidate, adapter)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(self.state.pending_hits()), 0)

    def test_crash_after_report_before_database_ack_is_idempotent(self):
        candidate = Candidate("github", "1", "a" * 40, "lab/chip", "https://github.com/lab/chip")
        key = self.state.begin(candidate, "test-pipeline")
        self.state.save_evidence(key, {"text": "[E1] heater array", "rules": ["layout_file"], "paths": []})
        self.state.finish(key, assessment(), True)
        reporter = self.runner(FakeModel()).reporter
        with patch.object(self.state, "mark_reported", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                reporter.flush()
        previous = (self.root / "hits.md").read_bytes()
        reporter.flush()
        self.assertEqual(previous, (self.root / "hits.md").read_bytes())
        self.assertEqual(len(self.state.pending_hits()), 0)

    def test_report_preserves_existing_user_content_and_escapes_external_html(self):
        path = self.root / "hits.md"
        path.write_text("User notes without newline", encoding="utf-8")
        candidate = Candidate("github", "1", "a" * 40, "lab/chip", "https://github.com/lab/chip")
        key = self.state.begin(candidate, "test-pipeline")
        self.state.save_evidence(key, {"text": "<script>unsafe</script>", "rules": ["layout_file"], "paths": []})
        self.state.finish(key, assessment(), True)
        self.runner(FakeModel()).reporter.flush()
        text = path.read_text()
        self.assertIn("User notes without newline", text)
        self.assertIn("【实时统计看板】", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("unsafe", text)
        self.assertNotIn("<pre>", text)

    def test_num_ctx_and_evidence_hard_limit_on_every_request(self):
        model = FakeModel()
        text = "[E1] heater array\n" + "x" * 30_000
        Judge(model, "test-model", self.sanitizer).assess({"text": text})
        payload = model.calls[0][1]
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload['format'], 'json')
        self.assertFalse(payload["stream"])
        self.assertLessEqual(len(payload["messages"][1]["content"].split("Evidence:\n", 1)[1]), 3500)

    def test_invalid_completed_json_retries_serially_with_same_safeguards(self):
        model = Mock()
        model.post_json.side_effect = [
            {"done": True, "message": {"content": "invalid"}},
            {"done": True, "message": {"content": json.dumps(assessment())}}]
        Judge(model, "test-model", self.sanitizer).assess({"text": "[E1] heater array"})
        self.assertEqual(model.post_json.call_count, 2)
        for call in model.post_json.call_args_list:
            self.assertEqual(call.args[1]["options"]["num_ctx"], 8192)
            self.assertEqual(call.args[1]["format"], "json")

    def test_no_followup_request_without_completed_generation(self):
        model = Mock()
        model.post_json.return_value = {"done": False, "message": {"content": "{}"}}
        with self.assertRaises(ModelUnavailable):
            Judge(model, "test-model", self.sanitizer).assess({"text": "[E1] heater array"})
        self.assertEqual(model.post_json.call_count, 1)

    def test_model_lock_prevents_overlapping_calls(self):
        model = FakeModel()
        active = 0
        maximum = 0
        counter_lock = threading.Lock()
        original = model.post_json

        def slow_request(path, payload):
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            response = original(path, payload)
            with counter_lock:
                active -= 1
            return response

        model.post_json = slow_request
        judge = Judge(model, "test-model", self.sanitizer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(judge.assess, {"text": "[E1] heater array"}) for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(maximum, 1)

    def test_second_cli_lock_is_rejected_and_released(self):
        path = self.root / "run.lock"
        with run_lock(path):
            with self.assertRaises(MinerError):
                with run_lock(path):
                    self.fail("second lock acquired")
        with run_lock(path):
            self.assertTrue(path.exists())

    def test_levels_and_quotes_are_validated(self):
        for level in (True, "0.3", -0.1, 30, math.nan, math.inf, ["LEVEL_1_CONFIRMED"], "UNKNOWN"):
            with self.subTest(level=level), self.assertRaises(MinerError):
                validate_assessment(json.dumps(assessment(level)), "[E1] heater array")
        with self.assertRaises(MinerError):
            validate_assessment(json.dumps(assessment(quote="invented quote")), "[E1] heater array")
        with self.assertRaises(MinerError):
            validate_assessment(json.dumps(assessment(section="E99")), "[E1] heater array")

    def test_level_three_is_saved_without_report(self):
        remote = FakeGitHubHTTP(has_layout=True)
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json("/repos/lab/chip"))
        self.runner(FakeModel("LEVEL_3_IRRELEVANT")).process(candidate, adapter)
        row = self.state.get_scan(("github", "42", candidate.version, "test-pipeline"))
        self.assertEqual(row["is_hit"], 0)
        self.assertEqual(row["status"], "complete")
        self.assertIn('扫描与过滤漏斗', (self.root/'hits.md').read_text())
        self.assertNotIn('<!-- pic-hit:', (self.root/'hits.md').read_text())

    def test_untrusted_script_is_never_executed(self):
        marker = self.root / "executed.txt"
        code = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\nlib.write_gds('chip.gds')"
        remote = FakeGitHubHTTP(script=code)
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json("/repos/lab/chip"))
        self.assertTrue(adapter.evidence(candidate)["matched"])
        self.assertFalse(marker.exists())

    def test_notebook_only_source_is_used_not_outputs(self):
        raw = json.dumps({"cells": [{"source": ["c.write_gds('chip.gds')"],
                                     "outputs": [{"text": "large image dump"}]}]})
        text = plain_text("chip.ipynb", raw)
        self.assertIn("write_gds", text)
        self.assertNotIn("large image dump", text)

    def test_evidence_builder_is_bounded_and_layout_extensions_are_case_insensitive(self):
        candidate = Candidate("github", "1", "a" * 40, "lab/chip", "https://github.com/lab/chip", "heater array")
        builder = EvidenceBuilder(candidate, self.sanitizer)
        self.assertTrue(builder.inspect_tree([FileEntry("CHIP.OASIS"), FileEntry("chip.gds.gz")]))
        for _ in range(10):
            builder.add("script", "x" * 4000)
        result = builder.build()
        self.assertLessEqual(len(result["text"]), MAX_EVIDENCE_CHARS)
        self.assertTrue(result["truncated"])
        self.assertIn("coverage and limitations", result["text"])
        self.assertTrue(is_layout("mask.GDSII"))

    def test_truncated_tree_falls_back_to_nonrecursive_subtrees(self):
        client = Mock()
        client.get_json.side_effect = [
            {"tree": [], "truncated": True},
            {"tree": [{"path": "design", "sha": "b" * 40, "type": "tree"}], "truncated": False},
            {"tree": [{"path": "chip.oas", "sha": "c" * 40, "type": "blob", "size": 500}], "truncated": False}]
        candidate = Candidate("github", "1", "a" * 40, "lab/chip", "https://github.com/lab/chip")
        entries, _ = GitHub(client, self.sanitizer).files(candidate)
        self.assertEqual(entries[0].path, "design/chip.oas")
        self.assertEqual(client.get_json.call_count, 3)

    def test_incomplete_subtree_is_not_a_negative_checkpoint(self):
        client = Mock()
        client.get_json.side_effect = [{"tree": [], "truncated": True}, {"tree": [], "truncated": True}]
        candidate = Candidate("github", "1", "a" * 40, "lab/chip", "https://github.com/lab/chip")
        with self.assertRaises(MinerError):
            GitHub(client, self.sanitizer).evidence(candidate)

    def test_organization_pagination_and_identity_dedup(self):
        client = Mock()
        first = [{"id": index, "full_name": "lab/repo" + str(index)} for index in range(100)]
        client.get_json.side_effect = [first, [first[0], {"id": 100, "full_name": "lab/last"}], first[0]]
        result = list(GitHub(client, self.sanitizer, self.messages.append).repositories(["lab"], ["lab/repo0"]))
        self.assertEqual(len(result), 101)
        self.assertEqual(client.get_json.call_args_list[1].args[1]["page"], 2)

    def test_conditional_304_reuses_payload_and_keeps_endpoint_out_of_cache_key(self):
        client = HTTPClient("https://api.github.com", "GitHub", self.state, min_interval=0)
        client.session.request = Mock(side_effect=[Response(body={"sha": "a" * 40}, headers={"ETag": '"etag"'}), Response(304)])
        try:
            first = client.get_json("/repos/lab/chip/commits/main")
            self.assertEqual(first, client.get_json("/repos/lab/chip/commits/main"))
            headers = client.session.request.call_args.kwargs["headers"]
            self.assertEqual(headers["If-None-Match"], '"etag"')
            cache_key = self.state.connection.execute("SELECT cache_key FROM http_cache").fetchone()[0]
            self.assertNotIn("github", cache_key)
        finally:
            client.close()

    def test_http_rejects_cross_origin_and_redirect_without_forwarding_token(self):
        token = secrets.token_hex(16)
        client = HTTPClient("https://api.github.com", "GitHub", token=token, min_interval=0)
        client.session.request = Mock(return_value=Response(302, headers={"Location": "https://other.invalid"}))
        try:
            with self.assertRaises(MinerError):
                client.get_json("https://other.invalid/secret")
            self.assertEqual(client.session.request.call_count, 0)
            with self.assertRaises(MinerError):
                client.get_json("/redirect")
            self.assertFalse(client.session.request.call_args.kwargs["allow_redirects"])
            self.assertFalse(client.session.trust_env)
        finally:
            client.close()

    def test_rate_limit_with_long_wait_is_deferred_not_busy_retried(self):
        client = HTTPClient("https://api.github.com", "GitHub", min_interval=0, max_wait=1)
        client.session.request = Mock(return_value=Response(429, headers={"Retry-After": "60"}))
        try:
            with self.assertRaises(MinerError):
                client.get_json("/repos/lab/chip")
            self.assertEqual(client.session.request.call_count, 1)
        finally:
            client.close()

    def test_short_rate_limit_retry_succeeds(self):
        client = HTTPClient("https://api.github.com", "GitHub", min_interval=0)
        client.session.request = Mock(side_effect=[Response(429, headers={"Retry-After": "0"}), Response(body={"ok": True})])
        try:
            self.assertEqual(client.get_json("/repos/lab/chip"), {"ok": True})
            self.assertEqual(client.session.request.call_count, 2)
        finally:
            client.close()

    def test_model_transport_timeout_is_not_retried_or_logged(self):
        client = HTTPClient("https://model.invalid", "Ollama", min_interval=0)
        client.session.request = Mock(side_effect=requests.Timeout("private endpoint and credential"))
        try:
            with self.assertRaises(ModelUnavailable) as raised:
                client.post_json("/api/chat", {"options": {"num_ctx": 8192}})
            self.assertEqual(client.session.request.call_count, 1)
            self.assertNotIn("private endpoint", str(raised.exception))
        finally:
            client.close()

    def test_config_uses_dotenv_and_does_not_display_credentials(self):
        token = secrets.token_hex(16)
        endpoint = "https://private-model.invalid"
        (self.root / ".env").write_text("OLLAMA_BASE_URL=" + endpoint + "\nOLLAMA_MODEL=test-model\nGITHUB_TOKEN=" + token)
        with patch.dict(os.environ, {}, clear=True):
            config = Config.load(self.root)
        self.assertEqual(config.github_token, token)
        self.assertNotIn(token, repr(config))
        self.assertNotIn(endpoint, repr(config))
        cleaned = config.sanitizer.clean(token + " " + endpoint + " private-model.invalid\x1b[31m")
        self.assertNotIn(token, cleaned)
        self.assertNotIn("private-model.invalid", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_missing_configuration_error_only_lists_names(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(MinerError) as raised:
            Config.load(self.root)
        self.assertIn("OLLAMA_BASE_URL", str(raised.exception))

    def test_invalid_or_credential_bearing_endpoint_is_rejected(self):
        for endpoint in ("https://user:secret@model.invalid", "http://model.invalid:bad", "https://model.invalid?token=secret"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {
                    "OLLAMA_BASE_URL": endpoint, "OLLAMA_MODEL": "test", "GITHUB_TOKEN": secrets.token_hex(16)}, clear=True):
                with self.assertRaises(MinerError) as raised:
                    Config.load(self.root)
                self.assertNotIn("secret", str(raised.exception))

    def test_zenodo_layout_enters_same_pipeline_and_versions_metadata_edits(self):
        record = {"id": 123, "updated": "2026-10-08", "metadata": {
            "title": "PIC heater array", "description": "fabricated chip", "keywords": ["photonics"]},
            "files": [{"key": "chip.oas", "size": 1000, "checksum": "md5:abc", "links": {}}]}
        zenodo = Zenodo(Mock(), self.sanitizer)
        candidate = zenodo.candidate(record)
        self.runner(FakeModel()).process(candidate, zenodo)
        self.assertIn("zenodo", (self.root / "hits.md").read_text())
        old = candidate.version
        record["metadata"]["description"] = "new manufacturing information"
        self.assertNotEqual(old, zenodo.candidate(record).version)

    def test_cli_missing_env_exits_cleanly_without_traceback(self):
        error_stream = io.StringIO()
        with patch.object(main, "ROOT", self.root), patch.object(main, "arguments", return_value=Mock()), \
                patch.dict(os.environ, {}, clear=True), redirect_stderr(error_stream):
            self.assertEqual(main.main(), 1)
        self.assertNotIn("Traceback", error_stream.getvalue())

    def test_main_wires_dotenv_seeds_database_and_report_end_to_end(self):
        token = secrets.token_hex(16)
        (self.root / ".env").write_text("OLLAMA_BASE_URL=https://model.invalid\nOLLAMA_MODEL=test-model\nGITHUB_TOKEN=" + token)
        seeds_path = self.root / "seeds.json"
        seeds_path.write_text(json.dumps({"organizations": [], "repositories": ["lab/chip"]}))
        args = main.argparse.Namespace(seeds=seeds_path, org=[], repo=[], zenodo_query=[], max_repos=None, continuous=False)
        github_http = FakeGitHubHTTP(has_layout=True)
        model = FakeModel()
        zenodo_http = Mock()
        stream = io.StringIO()
        with patch.object(main, "ROOT", self.root), patch.object(main, "arguments", return_value=args), \
                patch("pic_miner.http.HTTPClient", side_effect=[github_http, zenodo_http, model, Mock(), Mock()]), \
                patch.dict(os.environ, {}, clear=True), redirect_stdout(stream):
            self.assertEqual(main.main(), 0)
        self.assertTrue((self.root / ".pic_miner" / "state.sqlite3").exists())
        self.assertIn("[HIT", stream.getvalue())
        self.assertNotIn(token, stream.getvalue())
        self.assertTrue((self.root / "hits.md").exists())
        self.assertEqual(len(model.calls), 1)

    def test_branch_with_slash_uses_small_reference_request(self):
        remote = FakeGitHubHTTP()
        repo = remote.get_json("/repos/lab/chip")
        repo["default_branch"] = "design/tapeout"
        candidate = GitHub(remote, self.sanitizer).candidate(repo)
        self.assertEqual(candidate.metadata['commit_sha'], remote.sha)
        self.assertTrue(candidate.version.startswith(remote.sha + ':meta:'))
        self.assertEqual(remote.calls[-1][0], "/repos/lab/chip/git/ref/heads/design/tapeout")

    def test_late_export_ports_and_electrical_anchors_survive_noise(self):
        code = '\n'.join(['# photonics helper documentation'] * 150 + [
            "h = gf.components.heater()", "top.add_port('e1', port=h.ports['e1'])",
            "h.connect('e2', pad.ports['e1'])", "# immediately before export", "top.write_gds('chip.gds')"])
        text = snippets(code, 1600)
        for anchor in ('heater()', 'add_port', '.ports', '.connect(', 'write_gds', 'immediately before export'):
            self.assertIn(anchor, text)
        self.assertLessEqual(len(text), 1600)

    def test_global_budget_preserves_anchors_in_later_files(self):
        candidate = Candidate('github', '1', 'a'*40, 'lab/chip', 'https://github.com/lab/chip', 'PIC heater array')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.inspect_tree([FileEntry('chip.gds')])
        for index in range(6):
            builder.add_file(FileEntry('notes'+str(index)+'.py'), '\n'.join(['# photonics helper'] * 300))
        builder.add_file(FileEntry('late.py'), "h = heater()\ntop.add_port('e1', port=h.ports['e1'])\ntop.write_gds('chip.gds')")
        evidence = builder.build()
        self.assertLessEqual(len(evidence['text']), 10_000)
        for anchor in ('heater()', 'add_port', 'write_gds', 'late.py'):
            self.assertIn(anchor, evidence['text'])
        self.assertTrue(evidence['truncated'])

    def test_many_exports_cannot_starve_the_only_electrical_port(self):
        code = '\n'.join(["lib.write_gds('cell'+str(index)+'.gds')" for index in range(1000)] +
                         ["h = phase_shifter()", "top.add_port('e1', port=h.ports['e1'])"])
        text = snippets(code, 1600)
        self.assertIn('write_gds', text)
        self.assertIn('phase_shifter', text)
        self.assertIn('add_port', text)
        self.assertLessEqual(len(text), 1600)

    def test_long_line_retains_anchors_after_long_prefix(self):
        code = 'x'*2000 + "; top.write_gds('chip.gds'); " + 'y'*2000 + "; top.add_port('e1', port=h.ports['e1'])"
        text = snippets(code, 1200)
        self.assertIn('write_gds', text)
        self.assertIn('add_port', text)
        self.assertIn('.ports', text)
        self.assertLessEqual(len(text), 1200)

    def test_generic_writes_and_port_references_do_not_starve_declarations(self):
        code = '\n'.join(["log.write('message'); x = helper.ports['o1']" for _ in range(1000)] +
                         ["top.add_port('e1', port=h.ports['e1'])", "top.write_gds('chip.gds')"])
        text = snippets(code, 1600)
        self.assertIn('write_gds', text)
        self.assertIn('add_port', text)

    def test_notebook_discards_markdown_and_image_outputs(self):
        raw = json.dumps({'cells': [
            {'cell_type': 'markdown', 'source': ["lib.write_gds('fake.gds')"]},
            {'cell_type': 'code', 'source': ['heater = build_heater()'],
             'outputs': [{'data': {'image/png': 'IMAGE_BASE64_SENTINEL'*10000}, 'text': ['write_gds']}]}]})
        text = plain_text('chip.ipynb', raw)
        self.assertIn('build_heater', text)
        self.assertNotIn('fake.gds', text)
        self.assertNotIn('IMAGE_BASE64_SENTINEL', text)
        self.assertNotIn('write_gds', text)

    def test_invalid_notebook_never_falls_back_to_raw_output(self):
        for raw in ('{"cells": invalid', '{"cells": "image dump"}', '{"cells": [{"source": [null]}]}'):
            with self.subTest(raw=raw), self.assertRaises(MinerError):
                plain_text('chip.ipynb', raw)

    def test_large_notebook_is_cleaned_before_regex_and_model_evidence(self):
        raw = json.dumps({'cells': [
            {'cell_type': 'code', 'source': ['helper = 1'],
             'outputs': [{'data': {'image/png': 'IMAGE_BASE64_SENTINEL'*15000}}]},
            {'cell_type': 'code', 'source': ["h = heater()\ntop.add_port('e1', port=h.ports['e1'])\ntop.write_gds('chip.gds')"]}]})
        self.assertGreater(len(raw), 131_072)
        remote = FakeGitHubHTTP()
        remote.script = raw
        original = remote.get_json

        def notebook_tree(path, params=None):
            value = original(path, params)
            if '/git/trees/' in path:
                value['tree'][0]['path'] = 'chip.ipynb'
            return value

        remote.get_json = notebook_tree
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json('/repos/lab/chip'))
        evidence = adapter.evidence(candidate)
        self.assertTrue(evidence['matched'])
        self.assertFalse(evidence['inspection_incomplete'])
        self.assertIn('write_gds', evidence['text'])
        self.assertIn('add_port', evidence['text'])
        self.assertNotIn('IMAGE_BASE64_SENTINEL', evidence['text'])
        self.assertLessEqual(len(evidence['text']), 10_000)

    def test_large_python_script_retains_tail_export(self):
        code = '# helper code\n' * 12000 + "h = heater()\ntop.add_port('e1', port=h.ports['e1'])\ntop.write_gds('chip.gds')"
        self.assertGreater(len(code), 131_072)
        remote = FakeGitHubHTTP(script=code)
        adapter = GitHub(remote, self.sanitizer)
        candidate = adapter.candidate(remote.get_json('/repos/lab/chip'))
        evidence = adapter.evidence(candidate)
        self.assertTrue(evidence['matched'])
        self.assertIn('write_gds', evidence['text'])
        self.assertIn('add_port', evidence['text'])
        self.assertFalse(evidence['inspection_incomplete'])

    def test_source_beyond_safety_limit_is_review_checkpoint_not_endless_retry(self):
        candidate = Candidate('github', '1', 'a'*40, 'lab/chip', 'https://github.com/lab/chip', 'PIC project')
        adapter = GitHub(Mock(), self.sanitizer)
        adapter.files = Mock(return_value=([FileEntry('huge.ipynb', 'b'*40, MAX_SOURCE_BYTES+1)], 0))
        model = FakeModel("LEVEL_3_IRRELEVANT")
        runner = self.runner(model)
        runner.process(candidate, adapter)
        runner.process(candidate, adapter)
        row = self.state.get_scan(('github', '1', 'a'*40, 'test-pipeline'))
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(adapter.files.call_count, 1)
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(json.loads(row['assessment_json'])['review_required'])

    def test_raw_dimension_types_cannot_raise_unhandled_type_error(self):
        for value in (['supported'], {'state': 'supported'}, 1, True, None):
            data = assessment()
            data['level'] = value
            with self.subTest(value=value), self.assertRaises(ModelOutputError):
                validate_assessment(json.dumps(data), '[E1] heater array')

    def test_pydantic_constraints_are_retried_without_leaking_input(self):
        bad = assessment()
        bad['summary'] = 'x'*1201
        model = Mock()
        model.post_json.side_effect = [
            {'done': True, 'message': {'content': json.dumps(bad)}},
            {'done': True, 'message': {'content': json.dumps(assessment())}}]
        result = Judge(model, 'test-model', self.sanitizer).assess({'text': '[E1] heater array'})
        self.assertEqual(result['level'], 'LEVEL_2_PROBABLE')
        self.assertEqual(model.post_json.call_count, 2)

    def test_wrong_field_shape_retries_and_then_succeeds(self):
        bad = assessment()
        bad['level'] = ['LEVEL_1_CONFIRMED']
        model = Mock()
        model.post_json.side_effect = [
            {'done': True, 'message': {'content': json.dumps(bad)}},
            {'done': True, 'message': {'content': json.dumps(assessment())}}]
        Judge(model, 'test-model', self.sanitizer).assess({'text': '[E1] heater array'})
        self.assertEqual(model.post_json.call_count, 2)
        for call in model.post_json.call_args_list:
            self.assertEqual(call.args[1]['options']['num_ctx'], 8192)

    def test_exhausted_validation_is_reviewed_and_next_candidate_continues(self):
        first = Candidate('github', '1', 'a'*40, 'lab/first', 'https://github.com/lab/first')
        second = Candidate('github', '2', 'b'*40, 'lab/second', 'https://github.com/lab/second')
        adapter = Mock()
        adapter.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': ['export_script'], 'paths': []}
        bad = assessment()
        bad['level'] = ['LEVEL_1_CONFIRMED']
        model = Mock()
        model.post_json.side_effect = [
            {'done': True, 'message': {'content': json.dumps(bad)}},
            {'done': True, 'message': {'content': json.dumps(bad)}},
            {'done': True, 'message': {'content': json.dumps(assessment())}}]
        runner = self.runner(model)
        runner.process(first, adapter)
        runner.process(first, adapter)
        runner.process(second, adapter)
        row = self.state.get_scan(('github', '1', 'a'*40, 'test-pipeline'))
        self.assertEqual(row['status'], 'needs_review')
        self.assertTrue(json.loads(row['assessment_json'])['review_required'])
        self.assertEqual(model.post_json.call_count, 3)
        self.assertEqual(runner.stats['review'], 1)
        self.assertIn('lab/second', (self.root/'hits.md').read_text())

    def test_explicit_review_retry_reuses_saved_evidence(self):
        candidate = Candidate('github', '1', 'a'*40, 'lab/chip', 'https://github.com/lab/chip')
        key = self.state.begin(candidate, 'test-pipeline')
        self.state.save_evidence(key, {'text': '[E1] heater array', 'matched': True, 'rules': ['export_script'], 'paths': []})
        self.state.needs_review(key, 'bad model format')
        adapter = Mock()
        model = FakeModel()
        runner = self.runner(model)
        runner.retry_review = True
        runner.process(candidate, adapter)
        self.assertEqual(adapter.evidence.call_count, 0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.state.get_scan(key)['status'], 'complete')

    def test_strong_metadata_enters_model_without_gds_or_export(self):
        remote = FakeGitHubHTTP(script="print('no export')")
        adapter = GitHub(remote, self.sanitizer)
        repo = remote.get_json('/repos/lab/chip')
        repo['description'] = 'PIC heater array fabricated in an MPW tapeout'
        candidate = adapter.candidate(repo)
        model = FakeModel()
        runner = self.runner(model)
        runner.process(candidate, adapter)
        self.assertEqual(len(model.calls), 1)
        row = self.state.get_scan(('github', '42', candidate.version, 'test-pipeline'))
        self.assertIn('strong_description', json.loads(row['evidence_json'])['rules'])
        self.assertTrue(any('强描述通道' in message for message in self.messages))

    def test_late_readme_description_is_retained_and_eligible(self):
        candidate = Candidate('github', '1', 'a'*40, 'lab/chip', 'https://github.com/lab/chip', 'PIC heater array')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.inspect_tree([])
        builder.add_file(FileEntry('README.md'), '\n'.join(['intro text']*1000 + ['Photonic PIC MPW tapeout with thermal tuning and multi-channel electrodes']))
        evidence = builder.build()
        self.assertFalse(evidence['matched'])
        self.assertTrue(evidence['strong_description'])
        self.assertIn('MPW tapeout', evidence['text'])
        adapter = Mock()
        adapter.evidence.return_value = evidence
        model = FakeModel()
        self.runner(model).process(candidate, adapter)
        self.assertGreater(len(model.calls), 1)
        self.assertLessEqual(len(model.calls), 8)

    def test_generic_tapeout_does_not_nominate_unrelated_electronic_repository(self):
        self.assertFalse(strong_description('A CMOS tapeout example'))
        for text in ('PIC MPW tapeout', 'multi-channel electrodes', 'thermal tuning', '硅光芯片流片'):
            with self.subTest(text=text):
                self.assertTrue(strong_description(text))

    def test_seed_timeouts_and_404_do_not_block_later_seeds_or_zenodo(self):
        remote = FakeGitHubHTTP(has_layout=True)
        original = remote.get_json

        def failing_seeds(path, params=None):
            if path.startswith('/orgs/broken/'):
                raise requests.Timeout('untrusted private error details')
            if path == '/repos/missing/chip':
                from pic_miner.config import APIError
                raise APIError('GitHub', 404)
            return original(path, params)

        remote.get_json = failing_seeds
        github = GitHub(remote, self.sanitizer, self.messages.append)
        zenodo = Mock()
        zenodo.candidates.return_value = [Candidate('zenodo', '123', 'z'*40, 'Zenodo heater array', 'https://zenodo.org/records/123')]
        zenodo.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': ['layout_file'], 'paths': []}
        runner = self.runner(FakeModel())
        code = runner.run(github, zenodo, {'organizations': ['broken', 'good'], 'repositories': ['missing/chip', 'lab/chip'], 'zenodo_queries': ['photonics']})
        self.assertEqual(code, 1)
        self.assertEqual(runner.stats['errors'], 2)
        self.assertEqual(runner.stats['hits'], 2)
        self.assertTrue(zenodo.candidates.called)
        self.assertFalse(any('untrusted private' in message for message in self.messages))

    def test_failure_during_seed_pagination_does_not_block_next_seed(self):
        github = Mock()
        repo1 = {'id': '1'}
        repo2 = {'id': '2'}

        def first_page_then_fail():
            yield repo1
            raise ValueError('malformed next page')

        def repositories(organizations, explicit):
            return first_page_then_fail() if organizations == ['first'] else iter([repo2])

        github.repositories.side_effect = repositories
        github.candidate.side_effect = lambda repo: Candidate('github', repo['id'], 'a'*40, 'lab/chip'+repo['id'], 'https://github.com/lab/chip')
        github.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': ['layout_file'], 'paths': []}
        zenodo = Mock()
        runner = self.runner(FakeModel())
        runner.run(github, zenodo, {'organizations': ['first', 'second'], 'repositories': [], 'zenodo_queries': []})
        self.assertEqual(runner.stats['hits'], 2)
        self.assertEqual(runner.stats['errors'], 1)

    def test_failed_zenodo_query_does_not_block_next_query(self):
        github = Mock()
        zenodo = Mock()
        candidate = Candidate('zenodo', '123', 'z'*40, 'PIC heater array', 'https://zenodo.org/records/123')

        def candidates(queries):
            if queries == ['broken']:
                raise TypeError('invalid record')
            return iter([candidate])

        zenodo.candidates.side_effect = candidates
        zenodo.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': ['layout_file'], 'paths': []}
        runner = self.runner(FakeModel())
        runner.run(github, zenodo, {'organizations': [], 'repositories': [], 'zenodo_queries': ['broken', 'good']})
        self.assertEqual(runner.stats['errors'], 1)
        self.assertEqual(runner.stats['hits'], 1)

    def test_model_disconnect_still_stops_across_seed_boundaries(self):
        remote = FakeGitHubHTTP(has_layout=True)
        github = GitHub(remote, self.sanitizer, self.messages.append)
        zenodo = Mock()
        model = Mock()
        model.post_json.side_effect = ModelUnavailable('request still uncertain')
        runner = self.runner(model)
        with self.assertRaises(ModelUnavailable):
            runner.run(github, zenodo, {'organizations': ['first', 'second'], 'repositories': [], 'zenodo_queries': ['photonics']})
        self.assertEqual(model.post_json.call_count, 1)
        self.assertFalse(zenodo.candidates.called)

    def test_large_zenodo_notebook_uses_same_cleaning_and_budget(self):
        raw = json.dumps({'cells': [{'cell_type': 'code', 'source': ["top.write_gds('chip.gds')"],
                                     'outputs': [{'data': {'image/png': 'Z_IMAGE_SENTINEL'*15000}}]}]})
        client = Mock()
        client.get_text.return_value = raw
        zenodo = Zenodo(client, self.sanitizer)
        candidate = Candidate('zenodo', '123', 'z'*40, 'PIC heater array', 'https://zenodo.org/records/123',
                              files=[FileEntry('chip.ipynb', size=len(raw), download_url='https://zenodo.org/records/123/files/chip.ipynb')])
        evidence = zenodo.evidence(candidate)
        self.assertTrue(evidence['matched'])
        self.assertNotIn('Z_IMAGE_SENTINEL', evidence['text'])
        self.assertIn('write_gds', evidence['text'])
        self.assertEqual(client.get_text.call_args.kwargs['max_bytes'], MAX_SOURCE_BYTES)
        self.assertLessEqual(len(evidence['text']), 10_000)


if __name__ == "__main__":
    unittest.main()
