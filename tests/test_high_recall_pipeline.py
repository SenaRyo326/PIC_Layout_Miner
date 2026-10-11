"""Recall counterexamples and real-thread pipeline invariants; no external HTTP."""
import base64
import json
import tempfile
import os
import signal
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main

from pic_miner.arxiv import Arxiv
from pic_miner.config import Config, Sanitizer, ModelUnavailable
from pic_miner.http import HTTPClient
from pic_miner.discovery import Discovery
from pic_miner.evidence import Candidate, EvidenceBuilder, FileEntry
from pic_miner.github import GitHub
from pic_miner.judge import Judge, LEVEL_1, LEVEL_2, LEVEL_3, evidence_packets
from pic_miner.pipeline import PipelineCruise, CollectorShutdown
from pic_miner.offline_review import OfflineReview
from pic_miner.report import Reporter
from pic_miner.runner import Runner
from pic_miner.shutdown import Shutdown, StopRequested
from pic_miner.state import State
from pic_miner.storage import StateWriter
from pic_miner.zenodo import Zenodo
from test_pic_miner import FakeGitHubHTTP, FakeModel, assessment, Response
from test_optical import RepositoryHTTP
from test_cruise import paper, record


def negative():
    return {'level': LEVEL_3, 'summary': '传统电子或当前未发现 PIC 设计资源。', 'physical_features': [], 'evidence': []}


def response(value):
    return {'done': True, 'message': {'content': json.dumps(value)}, 'prompt_eval_count': 123,
            'eval_count': 45, 'total_duration': 1000, 'done_reason': 'stop'}


class HighRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root/'state.sqlite3')
        self.sanitizer = Sanitizer(())
        self.logs = []

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def runner(self, model):
        return Runner(self.state, Judge(model, 'local', self.sanitizer),
                      Reporter(self.state, self.root/'hits.md', self.sanitizer, self.logs.append, color=False),
                      'high-recall', self.sanitizer, self.logs.append)

    def test_chinese_pic_with_layout_is_not_dictionary_vetoed(self):
        remote = RepositoryHTTP('硅光芯片版图，包含电极阵列，已经流片',
                                [('README.md', '硅光芯片已流片，包含电极阵列。')], layout=True)
        github = GitHub(remote, self.sanitizer)
        candidate = github.candidate(remote.repo)
        model = Mock(post_json=Mock(return_value=response(assessment(LEVEL_1, quote='硅光芯片版图，包含电极阵列，已经流片'))))
        self.runner(model).process(candidate, github)
        row = self.state.get_scan(('github','42',candidate.version,'high-recall'))
        self.assertEqual(row['status'], 'complete')
        self.assertEqual(json.loads(row['assessment_json'])['level'], LEVEL_1)
        model.post_json.assert_called_once()

    def test_source_only_platform_is_read_without_optical_metadata(self):
        remote = RepositoryHTTP('Device design', [('README.md','Layout tools')],
                                [('design.py', 'platform = "TFLN Pockels device"\nports = electrode_array()')])
        github = GitHub(remote, self.sanitizer)
        candidate = github.candidate(remote.repo)
        evidence = github.evidence(candidate)
        self.assertFalse(evidence['matched'])
        self.assertIn('design.py', evidence['script_paths'])
        self.assertTrue(any('Pockels' in b['text'] for b in evidence['blocks']))
        model = Mock(post_json=Mock(return_value=response(assessment(LEVEL_2, quote='Device design'))))
        self.runner(model).process(candidate, SimpleNamespace(evidence=lambda _: evidence))
        self.assertEqual(model.post_json.call_count, 1)
        self.assertEqual(self.state.dashboard_rows()[0]['level'], LEVEL_2)

    def test_raw_blob_survives_preview_and_offline_reload(self):
        c = Candidate('github','1','v','lab/chip','https://github.com/lab/chip','PIC heater array')
        builder = EvidenceBuilder(c, self.sanitizer)
        raw = 'ordinary text\n'*1500 + 'TAIL_ORIGINAL_SENTINEL'
        builder.add_text_file(FileEntry('README.md'), raw)
        evidence = builder.build()
        key = self.state.begin(c,'p')
        self.state.save_evidence(key,evidence)
        stored = json.loads(self.state.get_scan(key)['evidence_json'])
        self.assertLessEqual(len(stored['text']),10000)
        self.assertNotIn('text', stored['blocks'][1])
        restored = self.state.load_evidence(stored)
        self.assertEqual(restored['blocks'][1]['text'], raw)
        self.state.save_evidence(key,restored)
        self.assertEqual(self.state.connection.execute('SELECT COUNT(*) FROM evidence_blobs').fetchone()[0],len(evidence['blocks']))

    def test_model_reads_additional_windows_instead_of_one_lossy_excerpt(self):
        c = Candidate('github','1','v','lab/chip','https://github.com/lab/chip','PIC toolkit')
        builder = EvidenceBuilder(c,self.sanitizer)
        builder.add_text_file(FileEntry('design.py'), '# ordinary definition\n'*500 + 'RARE_CHIP_MARKER = electrode_array()')
        evidence = builder.build()
        seen = []
        def judge(path,payload):
            text = payload['messages'][1]['content']
            seen.append(text)
            if 'RARE_CHIP_MARKER' in text:
                marker = next(b['id'] for b in evidence['blocks'] if b['path']=='design.py')
                return response(assessment(LEVEL_2,quote='RARE_CHIP_MARKER',section=marker))
            return response(negative())
        result = Judge(SimpleNamespace(post_json=judge),'local',self.sanitizer).assess(evidence)
        self.assertEqual(result['level'],LEVEL_2)
        self.assertGreater(len(seen),1)
        self.assertTrue(any('RARE_CHIP_MARKER' in t for t in seen))
        self.assertTrue(all(len(t.removeprefix('Evidence:\n'))<=3500 for t in seen))

    def test_negative_with_incomplete_coverage_is_review_not_permanent_rejection(self):
        c=Candidate('github','1','v','lab/chip','https://github.com/lab/chip')
        e={'text':'[E1] PIC implementation', 'matched':False,'rules':[],
           'optical_match':False,'inspection_incomplete':True}
        self.runner(Mock(post_json=Mock(return_value=response(negative())))).process(c,SimpleNamespace(evidence=lambda _:e))
        self.assertEqual(self.state.get_scan(('github','1','v','high-recall'))['status'],'needs_review')
        self.assertIn('取证覆盖不足',(self.root/'hits.md').read_text())

    def test_budget_exhaustion_tracks_unread_windows_and_progress(self):
        b=EvidenceBuilder(Candidate('github','1','v','lab/chip','https://github.com/lab/chip'),self.sanitizer)
        b.add('large.py','ordinary\n'*4000)
        e=b.build()
        judge=Judge(Mock(post_json=Mock(return_value=response(negative()))),'local',self.sanitizer,max_chunks=1)
        judge.assess(e)
        first=e['examined_chunks'][:]
        self.assertTrue(judge.coverage_incomplete)
        judge.assess(e)
        self.assertEqual(len(e['examined_chunks']),2)
        self.assertNotEqual(e['examined_chunks'][1],first[0])

    def test_schema_specific_repair_and_telemetry_are_persisted(self):
        client=Mock(post_json=Mock(side_effect=[response('{}'),response(assessment())]))
        judge=Judge(client,'local',self.sanitizer,record_call=self.state.record_model_call)
        judge.trace_context=('github','1','v','p')
        judge.assess({'text':'[E1] heater array'})
        first,second=[c.args[1] for c in client.post_json.call_args_list]
        self.assertEqual(first['format'],'json')
        self.assertEqual(second['format'],'json')
        self.assertIn('上次校验错误',second['messages'][1]['content'])
        rows=self.state.connection.execute('SELECT payload_json FROM model_calls ORDER BY id').fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(json.loads(rows[0][0])['error'])
        self.assertEqual(json.loads(rows[1][0])['prompt_eval_count'],123)
        self.assertFalse(json.loads(rows[1][0])['error'])

    def test_parallel_callers_still_have_one_model_request_in_flight(self):
        lock=threading.Lock()
        active=maximum=0
        def post(path,payload):
            nonlocal active,maximum
            with lock:
                active+=1
                maximum=max(maximum,active)
            time.sleep(.01)
            with lock:
                active-=1
            self.assertEqual(payload['options']['num_ctx'],8192)
            return response(negative())
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:Judge(SimpleNamespace(post_json=post),'local',self.sanitizer).assess({'text':'[E1] PIC tool'}),range(8)))
        self.assertEqual(maximum,1)

    def test_metadata_only_change_invalidates_github_assessment(self):
        github=GitHub(FakeGitHubHTTP(),self.sanitizer)
        repo=github.client.get_json('/repos/lab/chip')
        before=github.candidate(repo)
        repo['description']='PIC open layout now available'
        after=github.candidate(repo)
        self.assertEqual(before.metadata['commit_sha'],after.metadata['commit_sha'])
        self.assertNotEqual(before.version,after.version)

    def test_finished_history_cursor_does_not_rescan_2008_window(self):
        remote=Mock(get_json=Mock(return_value={'items':[],'total_count':0}))
        d=Discovery(self.state,SimpleNamespace(client=remote),None,None,self.logs.append)
        value={'kind':'search','query':'PIC','lane':'history','page':1,'windows':[[1,2]],'watermark':0,'cycle_end':2}
        d._github(value)
        d._github(value)
        self.assertTrue(value['history_complete'])
        remote.get_json.assert_called_once()

    def test_transient_zenodo_503_does_not_trip_permanent_record_fuse(self):
        self.state.init_cruise()
        self.state.enqueue('zenodo','1',{},now=0)
        for _ in range(5):
            self.assertFalse(self.state.settle('zenodo','1',time.time()+30,'Temporary',http_status=503))
        self.assertEqual(self.state.task('zenodo','1')['retry_count'],0)
        for _ in range(3):
            failed=self.state.settle('zenodo','1',time.time()+30,'Gone',http_status=410)
        self.assertTrue(failed)

    def test_old_rule_negative_can_be_reassessed_without_any_source_client(self):
        c=Candidate('github','1','v','lab/chip','https://github.com/lab/chip','硅光芯片版图')
        key=self.state.begin(c,'old-rule-protocol')
        self.state.save_evidence(key,{'text':'[E1] 硅光芯片版图', 'optical_match':False,'matched':False,
                                      'rules':[],'best_assessment':negative(),'examined_chunks':['legacy']})
        self.state.finish(key,negative(),False)
        model=Mock(post_json=Mock(return_value=response(assessment(LEVEL_2,quote='硅光芯片版图'))))
        replay=OfflineReview(self.runner(model),Shutdown(self.logs.append))
        self.assertEqual(replay.run(include_rejected=True),0)
        self.assertEqual(self.state.dashboard_rows()[0]['level'],LEVEL_2)
        self.assertEqual(replay.run(include_rejected=True),0)
        model.post_json.assert_called_once()
        self.assertEqual(json.loads(self.state.get_scan(key)['assessment_json'])['level'],LEVEL_3)

    def test_stop_after_current_chunk_saves_progress_without_starting_more_calls(self):
        b=EvidenceBuilder(Candidate('github','1','v','lab/chip','https://github.com/lab/chip'),self.sanitizer)
        b.add('large.py','ordinary\n'*1000)
        e=b.build()
        client=Mock(post_json=Mock(return_value=response(negative())))
        judge=Judge(client,'local',self.sanitizer,should_stop=lambda:True)
        judge.assess(e)
        self.assertEqual(client.post_json.call_count,1)
        self.assertTrue(judge.coverage_incomplete)
        self.assertEqual(len(e['examined_chunks']),1)

    def test_oversized_model_http_response_is_ambiguous_not_retryable(self):
        client=HTTPClient('http://model.example','Ollama',min_interval=0)
        try:
            with self.assertRaises(ModelUnavailable):
                client._read(Response(200,{'unexpected':'long text'}),2)
        finally:
            client.close()

    def test_later_bad_chunk_does_not_erase_prior_valid_positive(self):
        b=EvidenceBuilder(Candidate('github','1','v','lab/chip','https://github.com/lab/chip','heater array'),self.sanitizer)
        b.add('large.py','ordinary\n'*1000)
        e=b.build()
        client=Mock(post_json=Mock(side_effect=[response(assessment()),response('{}'),response('{}')]))
        judge=Judge(client,'local',self.sanitizer,max_chunks=2)
        self.assertEqual(judge.assess(e)['level'],LEVEL_2)
        self.assertTrue(judge.coverage_incomplete)
        self.assertTrue(e['judgement_notes'])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.path=self.root/'state.sqlite3'
        self.state=StateWriter(self.path)
        self.state.init_cruise()
        self.logs=[]
        self.sanitizer=Sanitizer(())
        self.shutdown=Shutdown(self.logs.append)
        self.github=GitHub(FakeGitHubHTTP(),self.sanitizer,self.logs.append)
        self.zenodo=Zenodo(Mock(),self.sanitizer,self.logs.append)
        self.arxiv=Arxiv(Mock(),Mock(),self.sanitizer,self.logs.append)
        self.discovery=Discovery(self.state,self.github,self.zenodo,self.arxiv,self.logs.append)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def pipeline(self,model):
        runner=Runner(self.state,Judge(model,'local',self.sanitizer),
                      Reporter(self.state,self.root/'hits.md',self.sanitizer,self.logs.append,color=False),
                      'high-recall',self.sanitizer,self.logs.append)
        return PipelineCruise(runner,self.discovery,self.shutdown,idle_interval=.05,
                              sources=('github','arxiv','zenodo'))

    def test_writer_serializes_concurrent_sqlite_operations(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i:self.state.enqueue('github',str(i),{'full_name':'lab/chip'},now=0),range(40)))
        self.assertTrue(all(self.state.task('github',str(i)) is not None for i in range(40)))
        with self.assertRaises(AttributeError):
            _=self.state.connection

    def test_prepared_jobs_survive_restart_and_are_consumed_round_robin(self):
        for source in ('github','arxiv','zenodo'):
            c=Candidate(source,'1','v',source+'/chip','https://example.org/chip','PIC heater array')
            e=EvidenceBuilder(c,self.sanitizer).build()
            self.state.enqueue(source,'1',{},now=0)
            self.state.prepare(c,e)
        self.state.close()
        self.state=StateWriter(self.path)
        self.discovery.state=self.state
        self.assertEqual(self.state.prepared_count('github'),1)
        model=FakeModel()
        self.assertEqual(self.pipeline(model).run({},continuous=False),0)
        self.assertEqual(len(model.calls),3)
        order=[c[1]['messages'][1]['content'] for c in model.calls]
        self.assertTrue(all(source+'/chip' in text for source,text in zip(('github','arxiv','zenodo'),order)))
        self.assertTrue(all(self.state.prepared_count(s)==0 for s in ('github','arxiv','zenodo')))
        self.assertTrue(all(self.state.task(s,'1')['status']!='prepared' for s in ('github','arxiv','zenodo')))

    def test_collectors_make_progress_while_model_is_running_and_exit_keeps_prefetch(self):
        model_started=threading.Event()
        zenodo_fetched=threading.Event()
        release=threading.Event()
        original=self.github.evidence
        def github_evidence(c):
            self.assertTrue(model_started.wait(3))
            self.assertTrue(release.wait(3))
            return original(c)
        def zenodo_evidence(c):
            self.assertTrue(model_started.wait(3))
            result=EvidenceBuilder(c,self.sanitizer).build()
            zenodo_fetched.set()
            return result
        self.github.evidence=github_evidence
        self.zenodo.evidence=zenodo_evidence
        self.arxiv.full_evidence=lambda c:EvidenceBuilder(c,self.sanitizer).build()
        self.state.enqueue('github','42',{'full_name':'lab/chip'},now=0)
        self.state.enqueue('arxiv','2610.00001',paper(),now=0)
        self.state.enqueue('zenodo','123',record(),now=0)
        calls=[]
        def post(path,payload):
            calls.append(payload)
            model_started.set()
            self.assertTrue(zenodo_fetched.wait(3))
            self.shutdown.request()
            release.set()
            return response(assessment())
        try:
            self.assertEqual(self.pipeline(SimpleNamespace(post_json=post)).run({},continuous=True),0)
        finally:
            release.set()
        self.assertEqual(len(calls),1)
        self.assertTrue(zenodo_fetched.is_set())
        self.assertEqual(self.state.prepared_count('zenodo'),1)
        self.assertEqual(self.state.prepared_count('github'),1)
        self.assertTrue(any('arxiv' in row['source'] for row in self.state.dashboard_rows()))

    def test_signal_interrupts_collector_wait_even_during_active_model_item(self):
        stop=threading.Event()
        collector=CollectorShutdown(self.shutdown,stop)
        with self.shutdown.item():
            self.shutdown.request()
            with self.assertRaises(StopRequested):
                collector.wait(100)

    def test_paper_body_link_is_queued_before_model_and_has_provenance(self):
        self.arxiv.content.get_text.return_value='<p>Photonic heater array. '+ 'Physical layout context. '*10 + '<a href="https://github.com/lab/chip">Code Availability</a></p>'
        self.state.enqueue('arxiv','2610.00001',paper(),now=0)
        row=self.state.next_task(time.time(),source='arxiv')
        self.pipeline(FakeModel())._prepare(row)
        self.assertEqual(self.state.prepared_count('arxiv'),1)
        self.assertIsNotNone(self.state.task('github','name:lab/chip'))
        self.state.close()
        local=State(self.path)
        try:
            edge=local.connection.execute('SELECT * FROM resource_links').fetchone()
            self.assertEqual(edge['target_id'],'lab/chip')
            self.assertIn('/abs/',edge['origin_url'])
        finally:
            local.close()
        self.state=StateWriter(self.path)

    def test_model_timeout_preserves_prepared_job_and_never_reposts(self):
        c=Candidate('github','1','v','lab/chip','https://github.com/lab/chip','PIC heater array')
        self.state.enqueue('github','1',{},now=0)
        self.state.prepare(c,EvidenceBuilder(c,self.sanitizer).build())
        model=Mock(post_json=Mock(side_effect=ModelUnavailable('未知在途请求')))
        self.assertEqual(self.pipeline(model).run({},continuous=False),1)
        model.post_json.assert_called_once()
        self.assertEqual(self.state.prepared_count('github'),1)
        self.assertEqual(self.state.get_scan(('github','1','v','high-recall'))['status'],'error')

    def test_actual_sigint_commits_current_model_result_and_stops_collector_wait(self):
        c=Candidate('github','1','v','lab/chip','https://github.com/lab/chip','PIC heater array')
        self.state.enqueue('github','1',{},now=0)
        self.state.prepare(c,EvidenceBuilder(c,self.sanitizer).build())
        def post(path,payload):
            os.kill(os.getpid(),signal.SIGINT)
            time.sleep(.01)
            return response(assessment())
        with self.shutdown.signals():
            self.assertEqual(self.pipeline(SimpleNamespace(post_json=post)).run({},continuous=True),0)
        self.assertTrue(self.shutdown.requested)
        self.assertEqual(self.state.get_scan(('github','1','v','high-recall'))['status'],'complete')
        self.assertEqual(self.state.prepared_count('github'),0)

    def test_one_source_failure_does_not_prevent_other_source_inference(self):
        from pic_miner.config import APIError
        self.github.client.get_json=Mock(side_effect=APIError('GitHub',404))
        self.arxiv.full_evidence=lambda c:EvidenceBuilder(c,self.sanitizer).build()
        self.state.enqueue('github','42',{'full_name':'lab/chip'},now=0)
        self.state.enqueue('arxiv','2610.00001',paper(),now=0)
        model=FakeModel()
        self.assertEqual(self.pipeline(model).run({},continuous=False),1)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(self.state.task('arxiv','2610.00001')['status'],'done')
        self.assertEqual(self.state.task('github','42')['status'],'queued')

    def test_zenodo_redirected_record_is_settled_using_canonical_id(self):
        self.zenodo.record=Mock(return_value=record(456))
        self.state.enqueue('zenodo','123',{'id':'123'},now=0)
        self.assertEqual(self.pipeline(FakeModel()).run({},continuous=False),0)
        self.assertIsNone(self.state.task('zenodo','123'))
        self.assertEqual(self.state.alias('zenodo','123'),'456')
        self.assertEqual(self.state.task('zenodo','456')['status'],'done')
        self.assertEqual(self.state.prepared_count('zenodo'),0)

    def test_alias_merge_cannot_overwrite_an_already_prepared_canonical_job(self):
        c=Candidate('github','42','v','lab/chip','https://github.com/lab/chip','PIC heater array')
        self.state.enqueue('github','42',{'full_name':c.name},now=0)
        self.state.prepare(c,EvidenceBuilder(c,self.sanitizer).build())
        self.state.enqueue('github','name:lab/chip',{'full_name':c.name},now=0)
        self.assertFalse(self.state.promote('github','name:lab/chip','42',{'full_name':c.name},'new'))
        self.assertIsNone(self.state.task('github','name:lab/chip'))
        self.assertEqual(self.state.task('github','42')['status'],'prepared')
        self.assertEqual(self.state.prepared_count('github'),1)

    def test_continuous_reading_requeues_new_windows_until_cached_coverage_is_complete(self):
        c=Candidate('github','42','v','lab/chip','https://github.com/lab/chip','PIC research')
        b=EvidenceBuilder(c,self.sanitizer)
        b.add('large.py','ordinary\n'*600)
        e=b.build()
        self.state.enqueue('github','42',{},now=0)
        self.state.prepare(c,e)
        model=Mock(post_json=Mock(return_value=response(negative())))
        pipe=self.pipeline(model)
        pipe.runner.judge.max_chunks=1
        passes=0
        while self.state.next_prepared('github') is not None:
            pipe._consume(self.state.next_prepared('github'))
            passes+=1
            self.assertLess(passes,10)
        self.assertGreater(passes,1)
        self.assertEqual(model.post_json.call_count,passes)
        final=self.state.get_scan(('github','42','v','high-recall'))
        self.assertEqual(final['status'],'complete')
        self.assertEqual(json.loads(final['assessment_json'])['level'],LEVEL_3)
        self.assertEqual(self.state.task('github','42')['status'],'queued')

    def test_main_wires_writer_collectors_json_mode_and_closes_all_clients(self):
        config=Config(self.root,'http://model.example','unit-model','test-token')
        args=SimpleNamespace(review_offline=False,reassess_offline=False,continuous=False,
                             max_items=1,max_repos=None,retry_review=False)
        model=FakeModel()
        clients=[]
        def client(base,label,*args,**kwargs):
            instance=model if label=='Ollama' else (FakeGitHubHTTP() if label=='GitHub' else Mock())
            clients.append(instance)
            return instance
        with patch.object(main,'ROOT',self.root),patch.object(main,'arguments',return_value=args), \
             patch.object(main,'load_seeds',return_value={'repositories':['lab/chip']}), \
             patch('pic_miner.config.Config.load',return_value=config), \
             patch('pic_miner.http.HTTPClient',side_effect=client),patch.object(main,'log',self.logs.append):
            self.assertEqual(main.main(),0)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(model.calls[0][1]['format'],'json')
        for instance in clients:
            if isinstance(instance,Mock):
                instance.close.assert_called_once()
            else:
                self.assertTrue(instance.closed)
        local=State(self.root/'.pic_miner/state.sqlite3')
        try:
            self.assertEqual(local.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0],1)
            self.assertEqual(len(local.dashboard_rows()),1)
        finally:
            local.close()


if __name__=='__main__':
    unittest.main()
