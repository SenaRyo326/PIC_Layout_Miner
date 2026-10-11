"""Asset counterexamples, faithful citations and durable, nonduplicating funnel metrics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pic_miner.config import ModelOutputError, Sanitizer
from pic_miner.evidence import Candidate, EvidenceBuilder, FileEntry
from pic_miner.judge import Judge, LEVEL_1, LEVEL_2, LEVEL_3, compact_evidence, quote_candidates, validate_assessment
from pic_miner.offline_review import StoredEvidence
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.scope import resource_scope
from pic_miner.state import State
from pic_miner.storage import StateWriter


def verdict(level=LEVEL_1, quote='PIC heater array', identifier='E1'):
    return {'level': level, 'summary': 'PIC 物理版图资源。', 'physical_features': ['版图线索'],
            'evidence': [{'id': identifier, 'quote': quote}]}


def response(value):
    return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(value)}}


class PrecisionTests(unittest.TestCase):
    def setUp(self):
        self.sanitizer = Sanitizer(())

    def package(self, description, path, code):
        candidate = Candidate('github', '1', 'v', 'lab/project', 'https://github.com/lab/project', description)
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.add_text_file(FileEntry(path), code)
        return builder.build()

    def test_native_gdstk_test_export_cannot_become_pic_level_one(self):
        code = 'import gdstk\nlib = gdstk.Library()\nlib.write_gds("test.gds")'
        evidence = self.package('GDSTK is a GDSII/OASIS geometry library.', 'tests/test_library.py', code)
        client = Mock(post_json=Mock(return_value=response(verdict(quote='lib.write_gds("test.gds")', identifier='E2'))))
        result = Judge(client, 'local', self.sanitizer).assess(evidence)
        self.assertEqual(result['level'], LEVEL_3)
        self.assertEqual(evidence['decision_reason'], 'low_level_tool')

    def test_native_engine_with_teaching_geometry_never_confirms_level_one(self):
        candidate = Candidate('github', '1', 'v', 'heitzmann/gdstk', 'https://github.com/heitzmann/gdstk',
                              'GDSTK is a GDSII geometry library.')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.add_text_file(FileEntry('docs/layout.py'), 'waveguide = 1\nc.add_polygon(points)\nc.write_gds("demo.gds")')
        evidence = builder.build()
        client = Mock(post_json=Mock(return_value=response(verdict(quote='c.write_gds("demo.gds")', identifier='E2'))))
        self.assertEqual(Judge(client, 'local', self.sanitizer).assess(evidence)['level'], LEVEL_2)

    def test_fixture_citation_cannot_confirm_level_one_in_an_optical_library(self):
        evidence = self.package('Photonic layout toolkit.', 'tests/test_heater.py', 'c.write_gds("test.gds")')
        client = Mock(post_json=Mock(return_value=response(verdict(quote='c.write_gds("test.gds")', identifier='E2'))))
        self.assertEqual(Judge(client, 'local', self.sanitizer).assess(evidence)['level'], LEVEL_2)

    def test_pytorch_optical_weights_and_heater_parameters_are_not_design_assets(self):
        evidence = self.package('PyTorch photonic neural network training and calibration.', 'calibration.py',
                                'import torch\n# heater phase parameters\nweights = torch.nn.Parameter(torch.ones(4,4))')
        client = Mock(post_json=Mock(return_value=response(verdict(LEVEL_2, quote='heater phase parameters', identifier='E2'))))
        self.assertEqual(Judge(client, 'local', self.sanitizer).assess(evidence)['level'], LEVEL_3)
        self.assertEqual(evidence['decision_reason'], 'algorithm_only')

    def test_mixed_tensor_project_with_real_pic_geometry_is_retained(self):
        evidence = self.package('PyTorch photonic neural network training and calibration.', 'layout.py',
                                'import gdsfactory as gf\nc = gf.Component()\n# waveguide heater array\nc.add_port("e1")\nc.write_gds("chip.gds")')
        client = Mock(post_json=Mock(return_value=response(verdict(quote='waveguide heater array', identifier='E2'))))
        self.assertEqual(Judge(client, 'local', self.sanitizer).assess(evidence)['level'], LEVEL_1)
        self.assertIsNone(resource_scope(evidence)['exclusion_reason'])

    def test_incomplete_inspection_does_not_trigger_static_algorithm_veto(self):
        evidence = self.package('PyTorch photonic neural network training and calibration.', 'calibration.py', 'import torch')
        evidence['inspection_incomplete'] = True
        self.assertIsNone(resource_scope(evidence)['exclusion_reason'])

    def test_sparse_chinese_layout_is_not_keyword_rejected(self):
        evidence = self.package('硅光芯片开放版图，含电极阵列与相移器。', 'design.py', '芯片版图 = 电极阵列()')
        self.assertIsNone(resource_scope(evidence)['exclusion_reason'])
        client = Mock(post_json=Mock(return_value=response(verdict(quote='硅光芯片开放版图', identifier='E1'))))
        self.assertEqual(Judge(client, 'local', self.sanitizer).assess(evidence)['level'], LEVEL_1)

    def test_preserved_extra_sources_do_not_shift_preview_quote_ids(self):
        candidate = Candidate('github', '1', 'v', 'lab/chip', 'https://github.com/lab/chip')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        builder.preserve('docs/raw.md', 'Full PIC source retained before compact excerpts.')
        builder.add('design.py', 'c.write_gds("chip.gds")')
        evidence = builder.build()
        original = next(block for block in evidence['blocks'] if block['path'] == 'design.py')
        self.assertIn('[' + original['id'] + '] design.py', evidence['text'])
        validate_assessment(json.dumps(verdict(quote='c.write_gds("chip.gds")', identifier=original['id'])), evidence['text'])

    def test_large_e_number_remapping_stays_inside_preview_character_budget(self):
        candidate = Candidate('github', '1', 'v', 'lab/chip', 'https://github.com/lab/chip')
        builder = EvidenceBuilder(candidate, self.sanitizer)
        for index in range(110):
            builder.preserve('docs/source' + str(index) + '.md', 'Original source ' + str(index))
        for index in range(20):
            builder.add('design' + str(index) + '.py', 'PIC heater array write_gds("chip.gds")\n' * 20)
        evidence = builder.build(limit=1000)
        self.assertLessEqual(len(evidence['text']), 1000)
        self.assertIn('[E112]', evidence['text'])

    def test_exact_compaction_never_rewrites_long_line_quoted_by_model(self):
        raw = 'ordinary ' * 400 + 'phase_shifter(x=10, heater=True); write_gds("chip.gds")' + ' trailing ' * 400
        evidence = '[E9] design.py\n' + raw
        compact = compact_evidence(evidence, 900)
        self.assertIn('write_gds("chip.gds")', compact)
        for suggestion in quote_candidates(compact):
            self.assertIn(suggestion['quote'], raw)
            self.assertEqual(suggestion['id'], 'E9')
        self.assertNotIn('…', compact)
        self.assertLessEqual(len(compact), 900)

    def test_header_only_quote_is_repaired_using_body_candidates_not_whitespace_coercion(self):
        evidence = {'text': '[E1] design.py\nPIC heater array',
                    'blocks': [{'id': 'E1', 'path': 'design.py', 'text': 'PIC heater array'}]}
        client = Mock(post_json=Mock(side_effect=[response(verdict(quote='design.py')),
                                                 response(verdict())]))
        result = Judge(client, 'local', self.sanitizer).assess(evidence)
        self.assertEqual(result['evidence'][0]['quote'], 'PIC heater array')
        retry = client.post_json.call_args.args[1]['messages'][1]['content']
        self.assertIn('原文复制候选', retry)
        self.assertIn('"quote": "PIC heater array"', retry)
        self.assertNotIn('"quote": "design.py"', retry)
        with self.assertRaises(ModelOutputError):
            validate_assessment(json.dumps(verdict(quote='PIC  heater array')), evidence['text'])


class FunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.state = State(self.root/'state.sqlite3'); self.sanitizer = Sanitizer(())

    def tearDown(self):
        self.state.close(); self.tmp.cleanup()

    def begin(self, item='1', version='v', evidence=None, protocol='p'):
        candidate = Candidate('github', item, version, 'lab/'+item, 'https://github.com/lab/'+item, 'PIC layout')
        key = self.state.begin(candidate, protocol)
        self.state.save_evidence(key, evidence or {'text': '[E1] PIC heater array', 'matched': True, 'rules': []})
        return key

    def test_filtered_resources_are_counted_once_across_versions_and_promotions(self):
        e = {'text': '[E1] native GDS library', 'decision_reason': 'low_level_tool'}
        key = self.begin(evidence=e)
        self.state.finish(key, verdict(LEVEL_3, quote='native GDS library'), False)
        self.state.finish(key, verdict(LEVEL_3, quote='native GDS library'), False)
        metrics = self.state.funnel_metrics()
        self.assertEqual(metrics['resources'], 1)
        self.assertEqual(metrics['outcome:filtered:low_level_tool'], 1)
        key = self.begin(version='new')
        self.state.finish(key, verdict(LEVEL_2), True)
        metrics = self.state.funnel_metrics()
        self.assertEqual(metrics['resources'], 1)
        self.assertEqual(metrics['outcome:filtered:low_level_tool'], 0)
        self.assertEqual(metrics['outcome:level2'], 1)

    def test_recovered_quote_failure_is_an_event_not_a_review_resource(self):
        client = Mock(post_json=Mock(side_effect=[response(verdict(quote='hallucinated quotation')),
                                                 response(verdict(LEVEL_2))]))
        runner = Runner(self.state, Judge(client, 'local', self.sanitizer, record_call=self.state.record_model_call),
                        Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None),
                        'p', self.sanitizer, output=lambda _: None)
        candidate = Candidate('github', '1', 'v', 'lab/chip', 'https://github.com/lab/chip')
        evidence = {'text': '[E1] metadata\nPIC heater array', 'matched': True, 'rules': []}
        runner.process(candidate, StoredEvidence(evidence)); runner.process(candidate, StoredEvidence(evidence))
        metrics = self.state.funnel_metrics()
        self.assertEqual(metrics['event:model_requests'], 2)
        self.assertEqual(metrics['event:quote_validation'], 1)
        self.assertEqual(metrics['event:skip_unchanged'], 1)
        self.assertEqual(metrics['outcome:level2'], 1)
        self.assertEqual(sum(v for k,v in metrics.items() if k.startswith('outcome:review:')), 0)

    def test_negative_only_run_still_publishes_reason_funnel_without_hit_cards(self):
        key = self.begin(evidence={'text': '[E1] tensor-only training', 'decision_reason': 'algorithm_only'})
        self.state.finish(key, verdict(LEVEL_3, quote='tensor-only training'), False)
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None)
        reporter.flush(); text = (self.root/'hits.md').read_text()
        self.assertIn('扫描与过滤漏斗', text)
        self.assertIn('纯算法/训练/校准代码', text)
        self.assertNotIn('<!-- pic-hit:', text)
        self.assertIn('| 1 | 0 | 1 | 0 | 0 | 0 |', text)

    def funnel_values(self, text):
        lines = text.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith('| 扫描资源总量'))
        return [int(cell.strip()) for cell in lines[header + 2].strip('|').split('|')]

    def test_twelve_resource_regression_has_three_hits_not_four(self):
        for index in range(3):
            self.state.finish(self.begin(item=str(index)), verdict(LEVEL_2), True)
        key = self.begin(item='filtered', evidence={'text': '[E1] electronic IC', 'decision_reason': 'non_pic'})
        self.state.finish(key, verdict(LEVEL_3), False)
        for index in range(8):
            self.state.needs_review(self.begin(item='review' + str(index)), '证据覆盖不足')
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None)
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        values = self.funnel_values(text)
        self.assertEqual(values, [12, 3, 1, 8, 0, 0])
        self.assertEqual(values[0], sum(values[1:]))
        self.assertIn('| 3 | 0 | 3 | 8 |', text)
        self.assertNotIn('已完成判定', text)

    def test_all_outcomes_partition_total_after_review_promotions_and_restarts(self):
        self.state.finish(self.begin(item='l1'), verdict(LEVEL_1), True)
        self.state.finish(self.begin(item='l2'), verdict(LEVEL_2), True)
        self.state.finish(self.begin(item='filtered'), verdict(LEVEL_3), False)
        review = self.begin(item='review')
        self.state.needs_review(review, '证据覆盖不足')
        self.state.fail(self.begin(item='failed'), 'source failed')
        self.begin(item='reading')
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None)
        reporter.flush()
        self.assertEqual(self.funnel_values((self.root/'hits.md').read_text()), [6, 2, 1, 1, 1, 1])
        self.state.finish(review, verdict(LEVEL_2), True)
        self.state.close()
        self.state = State(self.root/'state.sqlite3')
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None)
        reporter.flush()
        values = self.funnel_values((self.root/'hits.md').read_text())
        self.assertEqual(values, [6, 3, 1, 0, 1, 1])
        self.assertEqual(values[0], sum(values[1:]))

    def test_clean_old_dashboard_is_upgraded_without_changing_database_counters(self):
        self.state.finish(self.begin(), verdict(LEVEL_2), True)
        reporter = Reporter(self.state, self.root/'hits.md', self.sanitizer, output=lambda _: None)
        reporter.flush()
        before = self.state.funnel_metrics()
        path = self.root/'hits.md'
        old = path.read_text().replace('layout-v4', 'layout-v3').replace('已命中（L1+L2）', '已完成判定')
        path.write_text(old + '\n人工备注保留。\n')
        Reporter(self.state, path, self.sanitizer, output=lambda _: None).flush()
        self.assertIn('layout-v4', path.read_text())
        self.assertNotIn('已完成判定', path.read_text())
        self.assertIn('人工备注保留。', path.read_text())
        self.assertEqual(before, self.state.funnel_metrics())

    def test_review_failure_counts_move_when_review_resolves(self):
        key = self.begin(evidence={'text': '[E1] PIC heater array', 'validation_failure_kind': 'quote_validation'})
        self.state.needs_review(key, '引用无法校验')
        self.assertEqual(self.state.funnel_metrics()['outcome:review:quote_validation'], 1)
        self.state.finish(key, verdict(LEVEL_2), True)
        metrics = self.state.funnel_metrics()
        self.assertEqual(metrics['outcome:review:quote_validation'], 0)
        self.assertEqual(metrics['outcome:level2'], 1)

    def test_metric_query_never_walks_scans_and_restart_preserves_counters(self):
        key = self.begin(); self.state.finish(key, verdict(LEVEL_2), True)
        before = self.state.funnel_metrics(); queries=[]
        self.state.connection.set_trace_callback(queries.append)
        self.assertEqual(before, self.state.funnel_metrics())
        self.assertFalse(any('scans' in q.lower() for q in queries))
        self.state.close(); self.state=State(self.root/'state.sqlite3')
        self.assertEqual(before, self.state.funnel_metrics())

    def test_legacy_migration_uses_latest_resource_state_without_fabricated_reasons(self):
        key = self.begin(); self.state.finish(key, verdict(LEVEL_3), False)
        key = self.begin(version='new'); self.state.finish(key, verdict(LEVEL_2), True)
        key = self.begin(item='2'); self.state.finish(key, verdict(LEVEL_3), False)
        self.state.connection.executescript("DROP TABLE resource_outcomes; DROP TABLE funnel_counters; DELETE FROM report_meta WHERE key='funnel-v1';")
        self.state.close(); self.state=State(self.root/'state.sqlite3')
        metrics = self.state.funnel_metrics()
        self.assertEqual(metrics['resources'], 2)
        self.assertEqual(metrics['outcome:level2'], 1)
        self.assertEqual(metrics['outcome:filtered:legacy_unclassified'], 1)
        self.assertNotIn('event:model_requests', metrics)

    def test_transaction_failure_cannot_commit_partial_outcome_or_counters(self):
        key = self.begin(); before = self.state.funnel_metrics()
        with patch.object(self.state, '_sync_report', side_effect=RuntimeError('publish preparation failed')):
            with self.assertRaises(RuntimeError): self.state.finish(key, verdict(LEVEL_2), True)
        self.assertEqual(before, self.state.funnel_metrics())
        self.assertEqual(self.state.get_scan(key)['status'], 'evidence_ready')

    def test_writer_supports_metrics_from_collector_and_inference_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        writer = StateWriter(self.root/'writer.sqlite3')
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: writer.record_event('skip_unchanged'), range(20)))
            self.assertEqual(writer.funnel_metrics()['event:skip_unchanged'], 20)
        finally: writer.close()


if __name__ == '__main__': unittest.main()
