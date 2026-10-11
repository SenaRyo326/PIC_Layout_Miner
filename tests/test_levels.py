"""Three-tier reports, release-only repositories, LFS and saturated巡航 regressions."""

import base64
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pic_miner.config import APIError, MinerError, ModelOutputError
from pic_miner.evidence import Candidate, strong_description
from pic_miner.github import GitHub
from pic_miner.http import HTTPClient
from pic_miner.judge import Judge, LEVEL_1, LEVEL_2, LEVEL_3, validate_assessment
from pic_miner.links import external_targets, target_job
from pic_miner.physical import attribute_rules, hosted_links, layout_asset, lfs_pointer, tracked_lfs
import test_cruise as cruise_fixture
import test_pic_miner as miner_fixture
from test_cruise import repo
from test_pic_miner import assessment, FakeModel


class TierTests(unittest.TestCase):
    setUp = miner_fixture.MinerTests.setUp
    tearDown = miner_fixture.MinerTests.tearDown
    runner = miner_fixture.MinerTests.runner
    def test_three_levels_strictly_validate_and_float_field_is_rejected(self):
        for level in (LEVEL_1, LEVEL_2, LEVEL_3):
            result = validate_assessment(json.dumps(assessment(level)), '[E1] heater array')
            self.assertEqual(result['level'], level)
            self.assertNotIn('confidence', result)
        for level in (.3, 1, True, None, ['LEVEL_1_CONFIRMED'], 'LEVEL_4'):
            with self.subTest(level=level), self.assertRaises(ModelOutputError):
                validate_assessment(json.dumps(assessment(level)), '[E1] heater array')
        legacy = assessment()
        legacy['confidence'] = .3
        with self.assertRaises(ModelOutputError):
            validate_assessment(json.dumps(legacy), '[E1] heater array')

    def test_hits_require_features_and_quotes_but_irrelevant_can_omit(self):
        for level in (LEVEL_1, LEVEL_2):
            value = assessment(level)
            value['evidence'] = []
            with self.assertRaises(ModelOutputError):
                validate_assessment(json.dumps(value), '[E1] heater array')
            value = assessment(level)
            value['physical_features'] = []
            with self.assertRaises(ModelOutputError):
                validate_assessment(json.dumps(value), '[E1] heater array')
        value = assessment(LEVEL_3)
        value.update(physical_features=[], evidence=[])
        self.assertEqual(validate_assessment(json.dumps(value), '')['level'], LEVEL_3)

    def test_features_raw_shapes_and_summary_limits_are_checked(self):
        for features in ('heater', [1], [''], ['x' * 101], ['multi\nline'], [None]):
            value = assessment()
            value['physical_features'] = features
            with self.subTest(features=features), self.assertRaises(ModelOutputError):
                validate_assessment(json.dumps(value), '[E1] heater array')
        value = assessment()
        value['summary'] = 'x' * 141
        with self.assertRaises(ModelOutputError):
            validate_assessment(json.dumps(value), '[E1] heater array')

    def test_claim_without_static_entity_is_downgraded(self):
        result = Judge(FakeModel(LEVEL_1), 'test', self.sanitizer).assess(
            {'text': '[E1] heater array', 'physical_layout_confirmed': False})
        self.assertEqual(result['level'], LEVEL_2)

    def test_board_counts_groups_latest_versions_and_eight_line_cards(self):
        runner = self.runner(FakeModel())
        adapter = Mock()
        adapter.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': ['layout_file'],
                                        'paths': ['chip.gds'], 'physical_layout_confirmed': True}
        first = Candidate('github', '1', 'a'*40, 'lab/chip', 'https://github.com/lab/chip')
        second = Candidate('arxiv', '2610.00001', 'v1', 'Paper', 'https://arxiv.org/abs/2610.00001')
        runner.judge = Judge(FakeModel(LEVEL_1), 'test', self.sanitizer)
        runner.process(first, adapter)
        runner.judge = Judge(FakeModel(LEVEL_2), 'test', self.sanitizer)
        runner.process(second, adapter)
        first.version = 'b'*40
        runner.process(first, adapter)
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 2 | 0 | 2 |', text)
        self.assertEqual(text.count('<!-- pic-hit:'), 2)
        self.assertNotIn('a'*40, text)
        self.assertNotIn('<pre>', text)
        self.assertNotIn('unknown', text)
        self.assertNotIn('confidence', text)
        self.assertIn('[arXiv] [2610.00001]', text)
        for row in self.state.dashboard_rows():
            self.assertLessEqual(len(runner.reporter.render(row).splitlines()), 8)

    def test_level_three_removes_previous_hit_and_updates_board(self):
        adapter = Mock()
        adapter.evidence.return_value = {'text': '[E1] heater array', 'matched': True, 'rules': [], 'paths': []}
        candidate = Candidate('github', '1', 'v1', 'lab/chip', 'https://github.com/lab/chip')
        runner = self.runner(FakeModel(LEVEL_2))
        runner.process(candidate, adapter)
        self.assertEqual(len(self.state.dashboard_rows()), 1)
        runner.judge = Judge(FakeModel(LEVEL_3), 'test', self.sanitizer)
        candidate.version = 'v2'
        runner.process(candidate, adapter)
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 0 | 0 | 0 |', text)
        self.assertNotIn('lab/chip', text)
        self.assertEqual(json.loads(self.state.get_scan(('github','1','v2','test-pipeline'))['assessment_json'])['level'], LEVEL_3)

    def test_dashboard_failure_is_recovered_after_reopen(self):
        candidate = Candidate('github', '1', 'v1', 'lab/chip', 'https://github.com/lab/chip')
        key = self.state.begin(candidate, 'test-pipeline')
        self.state.save_evidence(key, {'text': '[E1] heater array', 'rules': [], 'paths': []})
        self.state.finish(key, assessment(), True)
        reporter = self.runner(FakeModel()).reporter
        with patch('pic_miner.report.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(MinerError):
                reporter.flush()
        self.assertNotEqual(self.state.dashboard_revision()['revision'], self.state.dashboard_revision()['published'])
        reporter.flush()
        self.assertEqual(self.state.dashboard_revision()['revision'], self.state.dashboard_revision()['published'])
        self.assertEqual(len(self.state.pending_hits()), 0)

    def test_legacy_probability_is_archived_as_unverified_level_two(self):
        candidate = Candidate('github', '1', 'old', 'lab/old', 'https://github.com/lab/old')
        key = self.state.begin(candidate, 'old-pipeline')
        self.state.save_evidence(key, {'text': '<pre>PIC heater array raw</pre>', 'rules': ['layout_file'], 'paths': ['old.gds']})
        # Simulate a pre-upgrade database rather than accepting old output in the judge.
        with self.state.connection:
            self.state.connection.execute("UPDATE scans SET status='complete',scanned_at='2026-10-08T00:00:00Z',is_hit=1,assessment_json=?",
                                          (json.dumps({'confidence':.99,'summary':'历史候选','evidence':[]}),))
        reporter = self.runner(FakeModel()).reporter
        reporter.flush()
        text = (self.root/'hits.md').read_text()
        self.assertIn('| 1 | 0 | 1 |', text)
        self.assertIn('历史记录待三档复核', text)
        self.assertNotIn('99%', text)
        self.assertNotIn('<pre>', text)

    def test_lfs_parser_keeps_real_size_and_rejects_bad_pointers(self):
        raw = 'version https://git-lfs.github.com/spec/v1\noid sha256:' + 'a'*64 + '\nsize 123456789\n'
        self.assertEqual(lfs_pointer(raw)['original_size'], 123456789)
        for invalid in (raw.replace('size 123456789','size 0'), raw.replace('a'*64,'bad'), raw.replace('version ', 'notversion '), b'\x00\xff'):
            self.assertIsNone(lfs_pointer(invalid))

    def test_attribute_scope_negation_and_explicit_untracking(self):
        rules = attribute_rules('.gitattributes', '*.gds filter=lfs\nkeep.gds -filter\n#comment\n')
        rules += attribute_rules('sub/.gitattributes', '*.oas filter=lfs\n')
        self.assertTrue(tracked_lfs('deep/chip.gds', rules))
        self.assertFalse(tracked_lfs('keep.gds', rules))
        self.assertTrue(tracked_lfs('sub/chip.oas', rules))
        self.assertFalse(tracked_lfs('else/chip.oas', rules))

    def test_layout_asset_filter_retains_layout_archives_not_generic_packages(self):
        for name in ('chip.GDS','chip.oas','chip.gds.gz','mask_layout.zip','photonic_tapeout.tar.gz'):
            self.assertTrue(layout_asset(name))
        for name in ('application.zip','sdk.tar.gz','photos.png'):
            self.assertFalse(layout_asset(name))

    def test_osf_cloud_and_bare_doi_are_static_hosted_clues(self):
        text = 'Our photonic GDS layout is hosted at https://osf.io/abcd/ https://drive.google.com/file/d/id/view and DOI 10.5281/zenodo.12345'
        hosts = hosted_links(text)
        self.assertEqual({h['kind'] for h in hosts}, {'OSF','Google Drive','Zenodo DOI'})
        self.assertTrue(all(h['layout_claim'] for h in hosts))
        self.assertIn(('zenodo','12345'), external_targets(text))
        self.assertFalse(hosted_links('https://osf.io.evil.invalid/a https://secret@osf.io/a'))

    def test_layout_tool_library_can_enter_without_export_or_manufacture(self):
        self.assertTrue(strong_description('PHIDL is a library for constructing GDS layouts.'))
        self.assertTrue(strong_description('Photonic layout generator toolkit.'))
        self.assertFalse(strong_description('PIC helper library without anything else.'))


class PhysicalCruiseTests(unittest.TestCase):
    setUp = cruise_fixture.CruiseTests.setUp
    tearDown = cruise_fixture.CruiseTests.tearDown
    add_cursor = cruise_fixture.CruiseTests.add_cursor
    def remote(self, files, blobs, releases=None, empty=False):
        def request(path, params=None, **kwargs):
            if path.endswith('/releases'):
                return releases or []
            if '/git/ref/heads/' in path:
                if empty:
                    raise APIError('GitHub',409)
                return {'object':{'type':'commit','sha':'a'*40}}
            if '/git/trees/' in path:
                return {'tree':files,'truncated':False}
            if '/git/blobs/' in path:
                raw = blobs[path.rsplit('/',1)[1]]
                return {'encoding':'base64','content':base64.b64encode(raw).decode()}
            if path == '/repos/lab/chip':
                return repo()
            raise AssertionError(path)
        return Mock(get_json=Mock(side_effect=request))

    def test_empty_repository_is_model_judged_without_assuming_description_is_layout(self):
        remote = self.remote([], {}, empty=True)
        github = GitHub(remote,self.sanitizer)
        r=repo();r['size']=0
        candidate=github.candidate(r)
        self.assertTrue(candidate.metadata['no_commit'])
        evidence=github.evidence(candidate)
        self.assertFalse(evidence['matched'])
        self.assertFalse(evidence['strong_description'])
        self.model.level = LEVEL_3
        self.runner.process(candidate,github)
        self.assertEqual(len(self.model.calls),1)
        self.assertIn('扫描与过滤漏斗', (self.root/'hits.md').read_text())
        self.assertNotIn('<!-- pic-hit:', (self.root/'hits.md').read_text())

    def test_release_only_layout_is_not_discarded_when_head_missing(self):
        releases=[{'id':1,'tag_name':'v1','assets':[{'id':2,'name':'chip.gds','size':1024,
                                                     'browser_download_url':'https://github.com/lab/chip/releases/download/v1/chip.gds'}]}]
        remote=self.remote([],{},releases,empty=True)
        github=GitHub(remote,self.sanitizer)
        r=repo();r['size']=0
        candidate=github.candidate(r)
        evidence=github.evidence(candidate)
        self.assertTrue(evidence['matched'])
        self.assertTrue(evidence['physical_layout_confirmed'])
        self.assertIn('release_asset',evidence['rules'])
        self.assertIn('chip.gds',evidence['text'])
        self.assertFalse(any('/git/trees/' in call.args[0] for call in remote.get_json.call_args_list))

    def test_release_changes_version_without_changing_commit_tree(self):
        assets=[{'id':1,'assets':[{'id':2,'name':'mask_layout.zip','size':1000}]}]
        remote=self.remote([],{},assets)
        github=GitHub(remote,self.sanitizer)
        first=github.candidate(repo())
        assets[0]['assets'][0]['size']=2000
        second=github.candidate(repo())
        self.assertNotEqual(first.version,second.version)
        github.files(second)
        self.assertEqual(remote.get_json.call_args.args[0],'/repos/lab/chip/git/trees/'+'a'*40)
        evidence=github.evidence(second)
        self.assertFalse(evidence['physical_layout_confirmed'])
        self.assertTrue(evidence['matched'])

    def test_lfs_pointer_and_attributes_supply_original_size_without_download(self):
        raw=('version https://git-lfs.github.com/spec/v1\noid sha256:'+'b'*64+'\nsize 123456789\n').encode()
        files=[{'path':'.gitattributes','sha':'attrs','size':20,'type':'blob'},
               {'path':'mask.gds','sha':'pointer','size':len(raw),'type':'blob'}]
        remote=self.remote(files,{'attrs':b'*.gds filter=lfs\n','pointer':raw})
        github=GitHub(remote,self.sanitizer)
        evidence=github.evidence(github.candidate(repo()))
        self.assertEqual(evidence['lfs_objects'][0]['original_size'],123456789)
        self.assertTrue(evidence['physical_layout_confirmed'])
        self.assertIn('123456789',evidence['text'])
        self.assertTrue(all('/git/' in call.args[0] or call.args[0].endswith('/releases') for call in remote.get_json.call_args_list))

    def test_untracked_small_gds_lfs_pointer_is_still_recognized(self):
        raw=('version https://git-lfs.github.com/spec/v1\noid sha256:'+'c'*64+'\nsize 987654\n').encode()
        remote=self.remote([{'path':'chip.gds','sha':'pointer','size':len(raw),'type':'blob'}],{'pointer':raw})
        evidence=GitHub(remote,self.sanitizer).evidence(GitHub(remote,self.sanitizer).candidate(repo()))
        self.assertEqual(evidence['lfs_objects'][0]['original_size'],987654)

    def test_small_placeholder_gds_is_only_a_clue_not_confirmed_entity(self):
        remote=self.remote([{'path':'chip.gds','sha':'small','size':11,'type':'blob'}],{'small':b'placeholder'})
        github=GitHub(remote,self.sanitizer)
        evidence=github.evidence(github.candidate(repo()))
        self.assertTrue(evidence['matched'])
        self.assertFalse(evidence['physical_layout_confirmed'])

    def test_large_layout_is_retained_without_binary_download(self):
        remote=self.remote([{'path':'chip.oas','sha':'huge','size':5000000000,'type':'blob'}],{})
        github=GitHub(remote,self.sanitizer)
        evidence=github.evidence(github.candidate(repo()))
        self.assertTrue(evidence['physical_layout_confirmed'])
        self.assertEqual(evidence['pointers'][0]['size'],5000000000)
        self.assertFalse(any('/git/blobs/' in call.args[0] for call in remote.get_json.call_args_list))

    def test_readme_external_layout_storage_enters_model_gate(self):
        text=b'Photonic layouts and GDS files are hosted at https://osf.io/abcd/ and DOI 10.5281/zenodo.999'
        remote=self.remote([{'path':'README.md','sha':'readme','size':len(text),'type':'blob'}],{'readme':text})
        github=GitHub(remote,self.sanitizer)
        evidence=github.evidence(github.candidate(repo()))
        self.assertIn('external_hosted_layout',evidence['rules'])
        self.assertTrue(evidence['matched'])
        self.assertIn(('zenodo','999'),evidence['external_targets'])
        self.assertFalse(evidence['physical_layout_confirmed'])

    def test_completed_source_cycles_use_short_safe_intervals(self):
        self.discovery.github.client=Mock()
        self.discovery.github.client.get_json.return_value={'items':[],'total_count':0,'incomplete_results':False}
        _,delay=self.discovery._github({'kind':'search','query':'phidl','lane':'history','page':1,
                                        'windows':[[100,200]],'watermark':0,'cycle_end':200})
        self.assertEqual(delay,2.5)
        self.discovery.arxiv.search=Mock(return_value=([],0))
        _,delay=self.discovery._arxiv({'kind':'search','query':'pic','start':0,'watermark':'','top':''})
        self.assertEqual(delay,3.5)
        self.zenodo_remote.get_json.return_value={'hits':{'hits':[]}}
        _,delay=self.discovery._zenodo({'kind':'search','query':'pic','page':1,'watermark':0})
        self.assertEqual(delay,2.5)

    def test_old_healthy_cooldowns_are_normalized_but_failures_preserved(self):
        self.state.enqueue(*target_job(self.state,'github','lab/chip'))
        self.state.settle('github','name:lab/chip',time.time()+7*86400)
        row=self.add_cursor('github',{'kind':'organization','value':'lab','page':4})
        self.state.page_commit(row,json.loads(row['state_json']),[],time.time()+7*86400)
        self.state.enqueue('github','broken',{'full_name':'lab/broken'},'x')
        self.state.settle('github','broken',time.time()+1000,'failed')
        now=time.time();self.state.activate_cruise(now)
        self.assertLessEqual(self.state.task('github','name:lab/chip')['due_at'],now+2.5)
        self.assertLessEqual(self.state.cursor('github','test')['due_at'],now+3.5)
        self.assertGreater(self.state.task('github','broken')['due_at'],now+900)

    def test_ready_tasks_progress_before_next_discovery_batch(self):
        self.state.enqueue(*target_job(self.state,'github','lab/chip'))
        self.discovery.tick=Mock()
        original=self.cruise._task
        def process(row):
            original(row)
            self.shutdown.request()
        self.cruise._task=process
        self.cruise.run({},continuous=True)
        self.assertEqual(len(self.model.calls),1)
        self.discovery.tick.assert_not_called()

    def test_shared_arxiv_pacer_enforces_interval_across_clients(self):
        pacer=SimpleNamespace(last_request=time.monotonic())
        controller=Mock()
        client=HTTPClient('https://arxiv.org','arXiv fulltext',min_interval=3.5,shutdown=controller,pacer=pacer)
        try:
            client._pace('https://arxiv.org/html/2610.00001')
            self.assertGreater(controller.wait.call_args.args[0],3.4)
        finally:
            client.close()

    def test_release_and_asset_pagination_are_complete(self):
        github=GitHub(Mock(),self.sanitizer)
        release={'id':1,'assets':[{'name':'chip.gds','size':10,'id':i} for i in range(30)]}
        github.client.get_json.side_effect=[
            [release]+[{'id':i,'assets':[]} for i in range(2,101)],
            [{'name':'chip.gds','size':10,'id':i} for i in range(100)],
            [{'name':'last.oas','size':20,'id':100}],
            [{'id':101,'assets':[{'name':'extra.gds.gz','size':50,'id':101}]}]]
        assets=github.releases('lab/chip')
        self.assertEqual(len(assets),102)
        calls=github.client.get_json.call_args_list
        self.assertEqual(calls[2].args[1]['page'],2)
        self.assertEqual(calls[3].args[1]['page'],2)
        self.assertIn('/releases/1/assets',calls[1].args[0])

    def test_release_listing_order_does_not_change_resource_fingerprint(self):
        releases=[{'id':1,'assets':[{'id':2,'name':'a.gds','size':100},{'id':3,'name':'b.oas','size':200}]}]
        github=GitHub(self.remote([],{},releases),self.sanitizer)
        first=github.candidate(repo())
        releases[0]['assets'].reverse()
        self.assertEqual(first.version,github.candidate(repo()).version)

    def test_head_with_empty_tree_still_rejects_manufacturing_advertisement(self):
        github=GitHub(self.remote([],{}),self.sanitizer)
        evidence=github.evidence(github.candidate(repo()))
        self.assertFalse(evidence['strong_description'])
        self.assertFalse(evidence['matched'])

    def test_compressed_small_layout_header_is_verified_without_unbounded_read(self):
        import gzip
        from pic_miner.physical import binary_layout
        self.assertTrue(binary_layout(gzip.compress(b'\x00\x06\x00\x02\x00\x06')))
        self.assertFalse(binary_layout(b'\x1f\x8bbad'))

    def test_lfs_duplicate_size_or_overflow_is_not_confirmed(self):
        raw='version https://git-lfs.github.com/spec/v1\noid sha256:'+'a'*64+'\nsize 100\n'
        self.assertIsNone(lfs_pointer(raw+'size 200\n'))
        self.assertIsNone(lfs_pointer(raw.replace('size 100','size '+str(2**64))))


if __name__=='__main__':
    unittest.main()
