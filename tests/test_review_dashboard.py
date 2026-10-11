"""Pending Review is a durable dashboard state, never an accepted hit or a model level."""
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pic_miner.config import MinerError, ModelOutputError, Sanitizer
from pic_miner.evidence import Candidate
from pic_miner.judge import Judge, LEVEL_1, LEVEL_2, LEVEL_3
from pic_miner.report import END, LAYOUT_VERSION, Reporter
from pic_miner.runner import Runner
from pic_miner.state import REVIEW, State
import test_pic_miner as fixtures


class ReviewDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root/'state.sqlite3')
        self.logs = []
        self.sanitizer = Sanitizer(())

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def candidate(self, identifier='1', version=None, name='gdsfactory/gdsfactory'):
        return Candidate('github', identifier, version or 'a'*40, name, 'https://github.com/' + name,
                         'PIC layout library', {'commit_sha': 'a'*40})

    def evidence(self):
        return {'text': '[E1] PIC heater array\nc.write_gds("chip.gds")\nc.add_port("e1")',
                'matched': True, 'optical_match': True, 'optical_keywords': ['gdsfactory','heater'],
                'rules': ['export_script','strong_description'], 'paths': ['chip.gds'],
                'script_paths': ['design.py'], 'document_paths': ['docs/pdk.md'],
                'pointers': [], 'inspection_incomplete': False}

    def begin(self, candidate=None):
        candidate = candidate or self.candidate()
        key = self.state.begin(candidate, 'test-pipeline')
        self.state.save_evidence(key, self.evidence())
        return key

    def reporter(self):
        return Reporter(self.state, self.root/'hits.md', self.sanitizer, self.logs.append, color=False)

    def review(self, candidate=None, reason='模型输出两次无法校验，需要人工核对。'):
        key = self.begin(candidate)
        self.state.needs_review(key, reason)
        return key

    def test_review_only_board_has_separate_count_reasons_features_and_clickable_paths(self):
        for i, name in enumerate(['gdsfactory/gdsfactory', 'gdsfactory/cspdk', 'amccaugh/phidl']):
            self.review(self.candidate(str(i), name=name))
        self.reporter().flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('待人工核对 (Review)', text)
        self.assertIn('| 0 | 0 | 0 | 3 |', text)
        self.assertIn('## ⚠️ 待核对核心线索 (Pending Review)', text)
        self.assertEqual(text.count('<!-- pic-review:'), 3)
        self.assertEqual(text.count('<!-- pic-hit:'), 0)
        self.assertIn('模型输出两次无法校验', text)
        self.assertIn('静态导出脚本线索', text)
        self.assertIn('/blob/' + 'a'*40 + '/chip.gds', text)
        self.assertNotIn('<pre>', text)
        self.assertFalse(self.state.pending_hits())
        self.assertFalse(any('[HIT ' in log for log in self.logs))

    def test_review_appendix_is_after_both_hit_sections_and_counts_stay_correct(self):
        self.review()
        key = self.begin(self.candidate('2', name='lab/confirmed'))
        self.state.finish(key, fixtures.assessment(LEVEL_1), True)
        self.reporter().flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 1 | 1 | 0 | 1 |', text)
        self.assertLess(text.index('## 【实时统计看板】'), text.index('## 🌟'))
        self.assertLess(text.index('## 🌟'), text.index('## 🔍'))
        self.assertLess(text.index('## 🔍'), text.index('## ⚠️'))
        self.assertLess(text.index('## ⚠️'), text.index(END))

    def test_section_numbers_are_independent_and_follow_newest_first_sorting(self):
        for level, tag in ((LEVEL_1, 'L1'), (LEVEL_2, 'L2'), (REVIEW, 'R')):
            for index in (1, 2):
                with patch('pic_miner.state.utc_now', return_value='2026-10-0' + str(index) + 'T00:00:00+00:00'):
                    candidate = self.candidate(tag + str(index), name='lab/' + tag + str(index))
                    key = self.begin(candidate)
                    if level == REVIEW:
                        self.state.needs_review(key, '待人工核对')
                    else:
                        self.state.finish(key, fixtures.assessment(level), True)
        reporter = self.reporter()
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 4 | 2 | 2 | 2 |', text)
        for tag in ('L1', 'L2', 'R'):
            first = '### [' + tag + '-01] [GitHub] [lab/' + tag + '2]('
            second = '### [' + tag + '-02] [GitHub] [lab/' + tag + '1]('
            self.assertEqual(text.count(first), 1)
            self.assertEqual(text.count(second), 1)
            self.assertLess(text.index(first), text.index(second))
            self.assertNotIn('### [' + tag + '-03]', text)
        before = (self.root/'hits.md').read_bytes()
        reporter.flush()
        self.assertEqual(before, (self.root/'hits.md').read_bytes())

    def test_clean_published_legacy_board_is_reformatted_without_new_hits(self):
        self.review()
        reporter = self.reporter()
        reporter.flush()
        path = self.root/'hits.md'
        text = path.read_text().replace(LAYOUT_VERSION + '\n', '').replace('### [R-01] ', '### ')
        path.write_text(text + '\n人工备注：保留。\n')
        before = self.state.dashboard_revision()
        self.assertEqual(before['revision'], before['published'])
        self.assertFalse(self.state.pending_hits())
        self.reporter().flush()
        upgraded = path.read_text()
        self.assertIn(LAYOUT_VERSION, upgraded)
        self.assertIn('### [R-01] [GitHub]', upgraded)
        self.assertIn('人工备注：保留。', upgraded)
        self.assertLess(upgraded.index('## 🔍'), upgraded.index('## ⚠️'))
        self.assertEqual(before, self.state.dashboard_revision())
        self.assertEqual(len(self.state.dashboard_rows()), 1)

    def test_review_resolving_to_level_two_replaces_card_without_duplicate(self):
        key = self.review()
        reporter = self.reporter()
        reporter.flush()
        self.state.finish(key, fixtures.assessment(LEVEL_2), True)
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 1 | 0 | 1 | 0 |', text)
        self.assertEqual(text.count('<!-- pic-hit:'), 1)
        self.assertNotIn('<!-- pic-review:', text)

    def test_review_resolving_to_level_three_disappears(self):
        key = self.review()
        reporter = self.reporter()
        reporter.flush()
        self.state.finish(key, fixtures.assessment(LEVEL_3), False)
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 0 | 0 | 0 | 0 |', text)
        self.assertNotIn('gdsfactory/gdsfactory', text)

    def test_new_review_version_moves_previous_hit_into_review_only(self):
        candidate = self.candidate()
        key = self.begin(candidate)
        self.state.finish(key, fixtures.assessment(LEVEL_2), True)
        reporter = self.reporter()
        reporter.flush()
        candidate.version = 'b'*40
        self.review(candidate, '新版本的静态覆盖不足。')
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 0 | 0 | 0 | 1 |', text)
        self.assertEqual(text.count('<!-- pic-review:'), 1)
        self.assertNotIn('<!-- pic-hit:', text)

    def test_legacy_review_statuses_are_imported_with_saved_reason_and_missing_timestamp(self):
        for i, status in enumerate(['needs_review','REVIEW','review']):
            key = self.begin(self.candidate(str(i), name='lab/review'+str(i)))
            with self.state.connection:
                self.state.connection.execute('UPDATE scans SET status=?,scanned_at=NULL,assessment_json=? WHERE source=? AND resource_id=? AND version=? AND pipeline_key=?',
                    (status, json.dumps({'review_required': True,'reason':'旧记录待人工确认'})) + key)
        self.reporter().flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 0 | 0 | 0 | 3 |', text)
        self.assertEqual(text.count('旧记录待人工确认'), 3)
        self.assertEqual(self.state.connection.execute("SELECT COUNT(*) FROM scans WHERE is_hit=1").fetchone()[0], 0)

    def test_migration_does_not_restore_resolved_historical_review(self):
        with patch('pic_miner.state.utc_now', return_value='2026-10-01T00:00:00+00:00'):
            self.review()
        with patch('pic_miner.state.utc_now', return_value='2026-10-02T00:00:00+00:00'):
            key = self.begin(self.candidate(version='b'*40))
            self.state.finish(key, fixtures.assessment(LEVEL_2), True)
        reporter = self.reporter()
        reporter.flush()
        self.assertNotIn('<!-- pic-review:', (self.root/'hits.md').read_text())
        self.assertIn('| 1 | 0 | 1 | 0 |', (self.root/'hits.md').read_text())

    def test_migration_keeps_latest_review_over_old_legacy_hit(self):
        with patch('pic_miner.state.utc_now', return_value='2026-10-01T00:00:00+00:00'):
            key = self.begin()
            self.state.finish(key, fixtures.assessment(LEVEL_2), True)
        with patch('pic_miner.state.utc_now', return_value='2026-10-02T00:00:00+00:00'):
            self.review(self.candidate(version='b'*40))
        self.reporter().flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 0 | 0 | 0 | 1 |', text)
        self.assertNotIn('<!-- pic-hit:', text)

    def test_model_failure_immediately_publishes_review_without_additional_model_calls(self):
        reporter = self.reporter()
        model = Mock(post_json=Mock(return_value={'done': True,'message':{'content':'{}'}}))
        runner = Runner(self.state, Judge(model, 'test', self.sanitizer), reporter, 'test-pipeline', self.sanitizer, self.logs.append)
        candidate = self.candidate()
        adapter = Mock(evidence=Mock(return_value=self.evidence()))
        runner.process(candidate, adapter)
        self.assertTrue((self.root/'hits.md').exists())
        self.assertIn('模型输出连续两次校验失败', (self.root/'hits.md').read_text())
        self.assertEqual(model.post_json.call_count, 2)
        before = (self.root/'hits.md').read_bytes()
        runner.process(candidate, adapter)
        reporter.flush()
        self.assertEqual(model.post_json.call_count, 2)
        self.assertEqual((self.root/'hits.md').read_text().count('<!-- pic-review:'), 1)
        self.assertEqual(self.state.funnel_metrics()['event:skip_review'], 1)

    def test_incomplete_negative_verdict_publishes_review_after_model(self):
        reporter = self.reporter()
        judge = Mock(assess=Mock(return_value={'level': 'LEVEL_3_IRRELEVANT', 'summary': '当前证据不足', 'physical_features': [], 'evidence': []}))
        runner = Runner(self.state, judge, reporter, 'test-pipeline', self.sanitizer, self.logs.append)
        evidence = self.evidence()
        evidence.update(matched=False, strong_description=False, inspection_incomplete=True)
        runner.process(self.candidate(), Mock(evidence=Mock(return_value=evidence)))
        self.assertIn('取证覆盖不足', (self.root/'hits.md').read_text())
        judge.assess.assert_called_once()

    def test_review_write_failure_preserves_checkpoint_and_recovers_after_restart(self):
        reporter = self.reporter()
        runner = Runner(self.state, Mock(assess=Mock(side_effect=ModelOutputError('输出类型错误'))), reporter,
                        'test-pipeline', self.sanitizer, self.logs.append)
        candidate = self.candidate()
        with patch('pic_miner.report.os.replace', side_effect=OSError('disk')):
            runner.process(candidate, Mock(evidence=Mock(return_value=self.evidence())))
        self.assertEqual(self.state.get_scan(('github','1',candidate.version,'test-pipeline'))['status'], 'needs_review')
        self.assertNotEqual(self.state.dashboard_revision()['revision'], self.state.dashboard_revision()['published'])
        self.state.close()
        self.state = State(self.root/'state.sqlite3')
        self.reporter().flush()
        self.assertIn('输出类型错误', (self.root/'hits.md').read_text())
        self.assertEqual(self.state.dashboard_revision()['revision'], self.state.dashboard_revision()['published'])

    def test_review_with_no_evidence_has_clear_fallback_and_compact_card(self):
        key = self.state.begin(self.candidate(), 'test-pipeline')
        self.state.needs_review(key, '证据尚未完整提取')
        reporter = self.reporter()
        reporter.flush()
        row = self.state.dashboard_rows()[0]
        self.assertEqual(row['level'], REVIEW)
        self.assertLessEqual(len(reporter.render(row).splitlines()), 8)
        self.assertIn('证据尚未完整提取', (self.root/'hits.md').read_text())

    def test_review_reason_excerpt_and_urls_are_sanitized_and_escaped(self):
        secret = secrets.token_hex(16)
        self.sanitizer = Sanitizer((secret,))
        candidate = self.candidate()
        candidate.url = 'javascript:alert(1)'
        key = self.begin(candidate)
        evidence = self.evidence()
        evidence['text'] += '\n<script>danger</script> ' + secret
        self.state.save_evidence(key, evidence)
        self.state.needs_review(key, '<script>bad</script> ' + secret)
        self.reporter().flush()
        text = (self.root/'hits.md').read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('javascript:alert', text)
        self.assertIn('REDACTED', text)

    def test_deleted_board_is_rebuilt_from_review_state_and_notes_survive(self):
        self.review()
        (self.root/'hits.md').write_text('人工备注：优先检查电极。\n')
        reporter = self.reporter()
        reporter.flush()
        self.assertIn('人工备注：优先检查电极。', (self.root/'hits.md').read_text())
        before = (self.root/'hits.md').read_bytes()
        reporter.flush()
        self.assertEqual(before, (self.root/'hits.md').read_bytes())
        (self.root/'hits.md').unlink()
        reporter.flush()
        self.assertIn('<!-- pic-review:', (self.root/'hits.md').read_text())


if __name__ == '__main__':
    unittest.main()
