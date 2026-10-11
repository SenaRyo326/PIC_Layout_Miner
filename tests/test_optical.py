"""Offline regressions for optical veto, bounded documents, fairness and Paper Reader."""
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_cruise as cruise_fixtures
import test_pic_miner as miner_fixtures
from pic_miner.arxiv import Arxiv
from pic_miner.config import ModelOutputError, Sanitizer
from pic_miner.cruise import Cruise
from pic_miner.evidence import Candidate, EvidenceBuilder, FileEntry, markdown_slices, optical_matches
from pic_miner.github import GitHub, MAX_DOCUMENT_FILES, MAX_SCRIPT_FILES
from pic_miner.judge import Judge, LEVEL_1, LEVEL_2, LEVEL_3, SYSTEM_PROMPT, validate_assessment
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.state import State


class RepositoryHTTP(miner_fixtures.FakeGitHubHTTP):
    def __init__(self, description, documents=(), scripts=(), layout=False, name='lab/design'):
        super().__init__(has_layout=layout)
        self.repo = {'id': 42, 'full_name': name, 'default_branch': 'main', 'description': description,
                     'size': 100, 'topics': []}
        self.sources = dict(documents) | dict(scripts)
        self.source_shas = {format(index + 1, '040x'): (path, raw) for index, (path, raw) in enumerate(self.sources.items())}

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith('/releases'):
            return []
        if '/git/ref/' in path:
            return {'object': {'type': 'commit', 'sha': self.sha}}
        if '/git/trees/' in path:
            tree = [{'type': 'blob', 'path': name, 'sha': sha, 'size': len(raw.encode())}
                    for sha, (name, raw) in self.source_shas.items()]
            if self.has_layout:
                tree.append({'type': 'blob', 'path': 'analog.gds', 'sha': 'f' * 40, 'size': 9000})
            return {'tree': tree, 'truncated': False}
        if '/git/blobs/' in path:
            raw = self.source_shas[path.rsplit('/', 1)[1]][1]
            return {'encoding': 'base64', 'content': base64.b64encode(raw.encode()).decode()}
        return self.repo


class OpticalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'state.sqlite3')
        self.sanitizer = Sanitizer(())
        self.model = miner_fixtures.FakeModel()
        self.logs = []
        self.runner = Runner(self.state, Judge(self.model, 'test-model', self.sanitizer),
                             Reporter(self.state, self.root / 'hits.md', self.sanitizer, self.logs.append, color=False),
                             'test-optical-pipeline', self.sanitizer, self.logs.append)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def inspect(self, remote):
        github = GitHub(remote, self.sanitizer, self.logs.append)
        candidate = github.candidate(remote.repo)
        return github, candidate, github.evidence(candidate)

    def test_sageroute_is_rejected_by_semantic_model_and_source_is_read(self):
        remote = RepositoryHTTP('Analog IC routing for VLSI and ASIC design.',
                                [('README.md', 'SAGERoute is a routing framework for analog integrated circuits. GDSII exports and metal pads.')],
                                [('router.py', "write_gds('analog.gds')")], layout=True, name='PKU-IDEA/SAGERoute')
        self.model.level = LEVEL_3
        github, candidate, evidence = self.inspect(remote)
        self.assertFalse(evidence['optical_match'])
        self.runner.process(candidate, Mock(evidence=Mock(return_value=evidence)))
        row = self.state.get_scan(('github', '42', candidate.version, self.runner.pipeline_key))
        self.assertEqual(json.loads(row['assessment_json'])['level'], LEVEL_3)
        self.assertEqual(row['status'], 'complete')
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn('扫描与过滤漏斗', (self.root/'hits.md').read_text())
        self.assertNotIn('<!-- pic-hit:', (self.root/'hits.md').read_text())
        self.assertTrue(any('/git/blobs/' in path and path.endswith('0000000000000000000000000000000000000002')
                             for path, _ in remote.calls))

    def test_semantic_negative_removes_previous_false_positive_dashboard_item(self):
        candidate = Candidate('github', '42', 'old', 'PKU-IDEA/SAGERoute', 'https://github.com/PKU-IDEA/SAGERoute')
        key = self.state.begin(candidate, 'old-pipeline')
        self.state.save_evidence(key, {'text': '[E1] analog IC', 'paths': ['analog.gds']})
        self.state.finish(key, miner_fixtures.assessment(LEVEL_2), True)
        self.runner.reporter.flush()
        candidate.version = 'new'
        self.model.level = LEVEL_3
        self.runner.process(candidate, Mock(evidence=Mock(return_value={
            'text': '[E1] ASIC analog IC and GDS', 'matched': True, 'rules': ['layout_file'],
            'optical_match': False, 'paths': ['analog.gds']})))
        self.assertNotIn('SAGERoute', (self.root / 'hits.md').read_text())
        self.assertEqual(len(self.model.calls), 1)

    def test_keyword_boundaries_and_case_variants(self):
        self.assertFalse(optical_matches('picture application spice typical epic asic docs/pictures/chip.md'))
        for text in ['PIC', 'silicon photonics', 'phase_shifter', 'ring-resonator', 'grating_couplers',
                     'MZI', 'Waveguides', 'GDSFACTORY', 'SiEPIC', 'heater', 'photodetectors']:
            with self.subTest(text=text):
                self.assertTrue(optical_matches(text))

    def test_optical_path_outside_truncated_inventory_is_still_matched(self):
        candidate = Candidate('github', '1', 'v', 'asic/router', 'https://github.com/asic/router')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.inspect_tree([FileEntry('chip' + str(i) + '.gds', size=5000) for i in range(130)] +
                             [FileEntry('zzzz/optical/device.txt')])
        self.assertTrue(builder.build()['optical_match'])

    def test_judge_uses_model_even_without_optical_dictionary_match(self):
        self.model.level = LEVEL_3
        result = self.runner.judge.assess({'text': '[E1] Analog GDS layout', 'optical_match': False})
        self.assertEqual(result['level'], LEVEL_3)
        self.assertEqual(len(self.model.calls), 1)

    def test_startup_does_not_hide_legacy_hit_using_dictionary_absence(self):
        self.state.init_cruise()
        self.state.connection.execute("DELETE FROM report_meta WHERE key LIKE 'optical-audit:%'")
        self.state.connection.commit()
        candidate = Candidate('github', '42', 'old', 'PKU-IDEA/SAGERoute', 'https://github.com/PKU-IDEA/SAGERoute')
        key = self.state.begin(candidate, 'old-pipeline')
        self.state.save_evidence(key, {'text': '[E1] Analog IC VLSI router GDS metal pads', 'paths': ['ota.gds']})
        self.state.finish(key, miner_fixtures.assessment(LEVEL_2), True)
        self.state.enqueue('github', '42', {'full_name': candidate.name}, 'old', now=0)
        self.state.settle('github', '42', time.time()+10000)
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, self.logs.append, color=False)
        reporter.flush()
        self.assertEqual(len(self.state.dashboard_rows()), 1)
        self.assertGreater(self.state.task('github','42')['due_at'], time.time())
        self.assertEqual(self.state.get_scan(key)['status'], 'complete')
        self.assertEqual(self.state.audit_optical_reports(), 0)
        self.assertFalse(self.model.calls)

    def test_legacy_optical_reports_survive_startup_audit(self):
        self.state.connection.execute("DELETE FROM report_meta WHERE key LIKE 'optical-audit:%'")
        self.state.connection.commit()
        candidate = Candidate('github', '42', 'v', 'gdsfactory/gdsfactory', 'https://github.com/gdsfactory/gdsfactory')
        key = self.state.begin(candidate, 'old-pipeline')
        self.state.save_evidence(key, {'text':'[E1] Photonic waveguide heater array', 'paths':['chip.gds']})
        self.state.finish(key, miner_fixtures.assessment(), True)
        Reporter(self.state, self.root/'hits.md', self.sanitizer, self.logs.append, color=False).flush()
        self.assertEqual(len(self.state.dashboard_rows()), 1)

    def test_system_prompt_first_line_has_electronic_veto(self):
        line = SYSTEM_PROMPT.splitlines()[0]
        self.assertIn('仅关注光子集成电路', line)
        self.assertIn(LEVEL_3, line)
        self.assertIn('严禁归入 Level 1 或 Level 2', line)

    def test_subdirectory_markdown_unlocks_optical_gate(self):
        remote = RepositoryHTTP('Layout utilities', [('README.md', 'Component library'),
            ('docs/devices/phase_shifter.md', '# Device\n\nPhotonic waveguide phase shifter with heater array, electrode and pad ports.\n\nUse c.write_gds("chip.gds") to export.')])
        _, _, evidence = self.inspect(remote)
        self.assertTrue(evidence['optical_match'])
        self.assertIn('docs/devices/phase_shifter.md', evidence['document_paths'])
        self.assertIn('heater array', evidence['text'])
        self.assertIn('write_gds', evidence['text'])
        self.assertLessEqual(evidence['document_chars'], 3000)

    def test_late_readme_anchors_survive_generic_prefix(self):
        raw = ('Unrelated installation text\n\n' * 10000) + '# Photonic device\n\nheater array optical phase shifter metal ports write_gds("chip.gds")'
        document = markdown_slices([('README.md', raw)])
        self.assertLessEqual(len(document), 3000)
        self.assertIn('heater array', document)
        self.assertIn('write_gds', document)

    def test_document_budget_spans_multiple_files(self):
        documents = [('README.md', '# PIC toolkit\n\nphotonic layout library'),
                     ('docs/pdk.md', '# PDK\n\nwaveguide heater electrode ports'),
                     ('examples/export.md', '# Exports\n\ncomponent.write_gds("chip.gds")')]
        result = markdown_slices(documents)
        for path, _ in documents:
            self.assertIn(path, result)
        self.assertLessEqual(len(result), 3000)

    def test_document_download_failure_does_not_abort_other_files(self):
        remote = RepositoryHTTP('PIC layout library', [('README.md', 'Broken'),
             ('docs/heater.md', 'Photonic waveguide heater array thermal tuning.')])
        github = GitHub(remote, self.sanitizer)
        candidate = github.candidate(remote.repo)
        original = github.text
        def text(resource, entry):
            if entry.path == 'README.md':
                raise ValueError('bad remote content')
            return original(resource, entry)
        github.text = text
        evidence = github.evidence(candidate)
        self.assertTrue(evidence['inspection_incomplete'])
        self.assertTrue(evidence['optical_match'])
        self.assertIn('heater array', evidence['text'])

    def test_large_repository_caps_documents_and_script_requests(self):
        remote = RepositoryHTTP('gdsfactory photonic layout library heater array',
                                [('docs/layout' + str(i) + '.md', 'PIC toolkit') for i in range(60)],
                                [('generic/lib' + str(i) + '.py', 'value = 1') for i in range(1000)])
        _, _, evidence = self.inspect(remote)
        self.assertEqual(len(evidence['document_paths']), MAX_DOCUMENT_FILES)
        self.assertLessEqual(sum('/git/blobs/' in p for p, _ in remote.calls), MAX_DOCUMENT_FILES + MAX_SCRIPT_FILES)
        self.assertTrue(evidence['strong_description'])
        self.assertTrue(evidence['inspection_incomplete'])
        self.assertLessEqual(len(evidence['text']), 10000)
        self.runner.process(Candidate('github', 'x', 'v', 'gdsfactory/large', 'https://github.com/gdsfactory/large'),
                            Mock(evidence=Mock(return_value=evidence)))
        self.assertEqual(len(self.model.calls), 8)

    def test_json_wrappers_and_thoughts_are_tolerated_without_type_coercion(self):
        raw = json.dumps(miner_fixtures.assessment())
        for wrapped in ['```json\n' + raw + '\n```', '<think>reasoning</think>\n' + raw,
                        'Result follows:\n' + raw + '\nCompleted.', '\ufeff' + raw]:
            with self.subTest(wrapped=wrapped[:20]):
                self.assertEqual(validate_assessment(wrapped, '[E1] heater array')['level'], LEVEL_2)

    def test_wrapped_wrong_types_extra_fields_and_ambiguous_json_still_fail(self):
        raw = miner_fixtures.assessment()
        bad = [dict(raw, level=[LEVEL_2]), dict(raw, confidence=.8), dict(raw, summary=['bad']),
               dict(raw, evidence=[{'id': 'E1', 'quote': 'invented'}])]
        for value in bad:
            with self.assertRaises(ModelOutputError):
                validate_assessment('```json\n' + json.dumps(value) + '\n```', '[E1] heater array')
        for content in [json.dumps(raw) + '\n' + json.dumps(raw), json.dumps(raw)[:-8],
                        '{"level":"LEVEL_3_IRRELEVANT","level":"LEVEL_2_PROBABLE"}',
                        '{"wrapper": ' + json.dumps(raw)]:
            with self.assertRaises(ModelOutputError):
                validate_assessment(content, '[E1] heater array')

    def test_wrapped_valid_response_uses_one_request_and_preserves_safety_options(self):
        model = Mock(post_json=Mock(return_value={'done': True, 'message': {
            'content': '```json\n' + json.dumps(miner_fixtures.assessment()) + '\n```'}}))
        Judge(model, 'test', self.sanitizer).assess({'text': '[E1] heater array' + 'x' * 20000})
        self.assertEqual(model.post_json.call_count, 1)
        self.assertEqual(model.post_json.call_args.args[1]['options']['num_ctx'], 8192)
        self.assertLessEqual(len(model.post_json.call_args.args[1]['messages'][1]['content']), 10050)


class FairPaperTests(unittest.TestCase):
    setUp = cruise_fixtures.CruiseTests.setUp
    tearDown = cruise_fixtures.CruiseTests.tearDown

    def enqueue_sources(self, count=3):
        for i in range(count):
            self.state.enqueue('github', str(100+i), {'full_name': 'lab/chip'}, now=0)
            self.state.enqueue('arxiv', '2610.' + str(i).zfill(5), cruise_fixtures.paper(), now=100)
            self.state.enqueue('zenodo', str(200+i), cruise_fixtures.record(), now=200)

    def fake_task(self, row):
        self.order.append(row['source'])
        self.state.settle(row['source'], row['item_id'])

    def test_round_robin_does_not_let_older_github_tasks_starve_other_sources(self):
        self.enqueue_sources()
        self.order = []
        self.cruise._task = self.fake_task
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        self.assertEqual(self.order, ['github', 'arxiv', 'zenodo'] * 3)

    def test_round_robin_skips_empty_and_delayed_sources(self):
        self.state.enqueue('github', '1', {}, now=0)
        self.state.enqueue('github', '2', {}, now=0)
        self.state.enqueue('arxiv', 'delayed', {}, now=time.time()+3600)
        self.state.enqueue('zenodo', '3', {}, now=0)
        self.order = []
        self.cruise._task = self.fake_task
        self.cruise.run({}, continuous=False)
        self.assertEqual(self.order, ['github', 'zenodo', 'github'])

    def test_round_robin_position_resumes_after_restart(self):
        self.enqueue_sources(count=2)
        self.order = []
        self.cruise._task = self.fake_task
        self.cruise.run({}, continuous=False, max_items=1)
        self.assertEqual(self.state.task_rotation(), 1)
        resumed = Cruise(self.runner, self.discovery, self.shutdown, sources=Cruise.SUPPORTED_SOURCES)
        resumed._task = self.fake_task
        resumed.run({}, continuous=False, max_items=2)
        self.assertEqual(self.order, ['github', 'arxiv', 'zenodo'])

    def test_round_robin_source_selection_uses_covering_due_index(self):
        self.enqueue_sources()
        plan = self.state.connection.execute("EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE status='queued' AND due_at<=? AND source=? ORDER BY due_at,source,item_id LIMIT 1", (time.time(), 'arxiv')).fetchall()
        self.assertIn('task_source_due', ' '.join(row[3] for row in plan))
        self.assertEqual(self.state.next_task(time.time(), [('arxiv','2610.00000')], source='arxiv')['item_id'], '2610.00001')

    def test_light_paper_reader_preserves_title_abstract_comments_and_links(self):
        p = cruise_fixtures.paper()
        p['comment'] = 'The open-source photonic PDK and code are publicly available.'
        p['links'] = ['https://github.com/lab/chip', 'https://doi.org/10.5281/zenodo.123']
        evidence = self.discovery.arxiv.evidence(self.discovery.arxiv.candidate(p))
        self.assertTrue(evidence['matched'])
        self.assertEqual(evidence['external_targets'], [('github','lab/chip'), ('zenodo','123')])
        self.assertIn('Title:', evidence['text'])
        self.assertIn('Abstract:', evidence['text'])
        self.assertIn('Comments:', evidence['text'])
        self.assertLessEqual(len(evidence['text']), 2400)
        self.arxiv_content.get_text.assert_not_called()
        self.arxiv_content.get_bytes.assert_not_called()

    def test_theoretical_paper_receives_fulltext_and_semantic_negative_verdict(self):
        p = cruise_fixtures.paper()
        p['summary'] = 'We study a theoretical optical routing algorithm with numerical simulations.'
        self.model.level = LEVEL_3
        self.state.enqueue('arxiv', '2610.00001', p)
        self.assertEqual(self.cruise.run({}, continuous=False), 0)
        row = self.state.connection.execute("SELECT * FROM scans WHERE source='arxiv'").fetchone()
        self.assertEqual(json.loads(row['assessment_json'])['level'], LEVEL_3)
        self.assertEqual(len(self.model.calls), 1)
        self.arxiv_content.get_text.assert_called_once()
        self.arxiv_content.get_bytes.assert_not_called()

    def test_electronic_paper_is_model_rejected_but_link_is_not_lost(self):
        p = cruise_fixtures.paper()
        p.update(title='Analog IC routing', summary='Open-source ASIC GDS layout and metal pads.',
                 links=['https://github.com/analog/router'])
        self.model.level = LEVEL_3
        self.state.enqueue('arxiv', '2610.00001', p)
        self.cruise.run({}, continuous=False, max_items=1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNotNone(self.state.task('github', 'name:analog/router'))

    def test_paper_hit_feeds_github_zenodo_and_publishes_dashboard(self):
        p = cruise_fixtures.paper()
        p['links'] = ['https://github.com/lab/chip', 'https://zenodo.org/records/123']
        self.state.enqueue('arxiv', '2610.00001', p)
        self.assertEqual(self.cruise.run({}, continuous=False, max_items=1), 0)
        self.assertIsNotNone(self.state.task('github', 'name:lab/chip'))
        self.assertIsNotNone(self.state.task('zenodo', '123'))
        self.assertIn('[arXiv]', (self.root / 'hits.md').read_text())
        self.assertLessEqual(len(self.model.calls[0][1]['messages'][1]['content']), 2500)
        self.assertLessEqual(sum(len(message['content']) for message in self.model.calls[0][1]['messages']), 6500)

    def test_level3_paper_still_preserves_discovered_links_without_report(self):
        p = cruise_fixtures.paper()
        p['links'] = ['https://github.com/theory/algorithm']
        self.model.level = LEVEL_3
        self.state.enqueue('arxiv', '2610.00001', p)
        self.cruise.run({}, continuous=False, max_items=1)
        self.assertIsNotNone(self.state.task('github','name:theory/algorithm'))
        self.assertIn('扫描与过滤漏斗', (self.root/'hits.md').read_text())
        self.assertNotIn('<!-- pic-hit:', (self.root/'hits.md').read_text())

    def test_long_paper_keeps_late_open_statement_and_links_within_budget(self):
        p = cruise_fixtures.paper()
        p['summary'] = 'Numerical methods. ' * 10000 + 'Our photonic GDS layouts are publicly available.'
        p['comment'] = 'Open code: https://github.com/lab/chip'
        evidence = self.discovery.arxiv.evidence(self.discovery.arxiv.candidate(p))
        self.assertLessEqual(len(evidence['text']), 2400)
        self.assertIn('publicly available', evidence['text'])
        self.assertIn(('github','lab/chip'), evidence['external_targets'])

    def test_paper_model_retry_user_prompt_stays_under_2500(self):
        raw = json.dumps(miner_fixtures.assessment())
        client = Mock(post_json=Mock(side_effect=[{'done': True, 'message': {'content': '{}'}},
                                                   {'done': True, 'message': {'content': raw}}]))
        evidence = {'text': '[E1] heater array' + 'x' * 10000, 'reader_mode': 'paper_metadata'}
        Judge(client, 'test', self.sanitizer).assess(evidence)
        self.assertEqual(client.post_json.call_count, 2)
        self.assertTrue(all(len(call.args[1]['messages'][1]['content']) <= 2500 for call in client.post_json.call_args_list))
        self.assertTrue(all(sum(len(message['content']) for message in call.args[1]['messages']) <= 6500
                            for call in client.post_json.call_args_list))


if __name__ == '__main__':
    unittest.main()
