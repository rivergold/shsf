import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import os

from shsf.agent_config import AgentError
from shsf.analysis_reports import validate_report,validate_notes,HEADINGS,report_prompt,evidence_catalog
from shsf.analyses import Analyses
from shsf.models import json_text
from shsf.paper_html import parse_html
from shsf.recommendations import Recommendations
import test_seeds as fixtures


def html_document(long=False):
    paragraph='The robot learns whole body control with rigorous real robot experiments and reports important limitations. '
    multiplier=110 if long else 3
    return ('<html><article class="ltx_document"><h1>Robot paper</h1><div class="ltx_abstract"><p>Only abstract</p></div><section id="S1"><h2>1 Method</h2><div id="S1.p1"><p>'+paragraph*multiplier+'</p></div><div id="S1.p2"><p>'+paragraph*multiplier+'</p></div><h2>2 Results</h2><p>'+paragraph*multiplier+'</p><figure id="F1"><figcaption>Figure 1: robot experiments in a controlled environment.</figcaption></figure><script>steal secret()</script></section></article></html>').encode()


class Route:
    mode='api';model='fixture-model'
    def __init__(self):self.calls=[];self.fail=False;self.fail_final=False
    def fingerprint(self):return 'fixture-route'
    def complete(self,prompt):
        self.calls.append(prompt)
        if self.fail:raise AgentError('模拟模型服务失败')
        data=json.loads(prompt.split('资料 JSON：\n')[-1])
        if isinstance(data,list):
            source=data[0];return json_text({'notes':[{'text':'片段描述机器人控制与实验。','source_id':source['id'],'quote':source['text'][:40]}]})
        if self.fail_final:raise AgentError('模拟汇总失败')
        notes=data.get('chunk_notes')
        if notes:citation={'evidence_id':notes[0]['evidence_id']}
        else:
            source=data['sources'][0];citation={'source_id':source['id'],'quote':source['text'][:40]}
        return json_text({'title':'测试报告','sections':[{'heading':name,'claims':[{'text':'这是有来源支持的分析。','kind':'paper','citations':[citation]}]} for name in HEADINGS['explain' if data['basis']=='abstract_only' else 'read']]})


class HTMLFixture:
    def __init__(self,long=False):self.long=long;self.failed=False;self.calls=0
    def get(self,paper):
        self.calls+=1
        if self.failed:raise AgentError('该版本尚无可用的官方 arXiv HTML。')
        return parse_html(html_document(self.long),'https://arxiv.org/html/'+paper['arxiv_id']+'v1')


class AnalysisContract:
    def setup_analysis(self):
        self.fixture=fixtures.StoreTests();self.fixture.tmp=self.tmp;self.fixture.db=self.db
        fixtures.SeedStore(self.db)
        self.fixture.addpaper()
        self.route=Route();root=Path(self.tmp.name)
        (root/'config').mkdir();(root/'config/robotics.json').write_text(json_text({'description':'humanoid control','topics':[]}))
        self.agent=SimpleNamespace(root=root,auth=SimpleNamespace(store=SimpleNamespace(directory=root/'pipeline/agent')),execution=lambda kind:self.route)
        self.html=HTMLFixture();self.service=Analyses(self.db,self.agent,html=self.html);self.addCleanup(self.service.close)
        # An authentic recommendation context is required for recommendation explanation.
        with self.db.transaction() as tx:
            tx.execute('''INSERT INTO recommendation_batches VALUES (?,?,?,?,?,?,?)''',('batch','local','2026-10-25','profile','model','2026-10-25T00:00:00Z','{}'))
            tx.execute('INSERT INTO recommendation_items VALUES (?,?,?,?)',('batch',1,'2303.04137',json_text({'reasons':[]})))

    def wait(self,job):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.service.get(job['job_id'])
            if result['status'] in ('succeeded','failed'):return result
            time.sleep(.01)
        self.fail('analysis timeout')

    def add_record(self,index,*,status='succeeded',kind='read',owner='local',aid=None,report=None):
        aid=aid or f'2303.{50000+index}'
        self.fixture.addpaper(aid,title=f'Robot paper {index}')
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO analysis_jobs(job_id,owner_key,arxiv_id,kind,cache_key,paper_input_hash,status,stage,progress,channel,model,request_json,report_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(index).zfill(64),owner,aid,kind,f'cache-{owner}-{index}','input',status,'stage',25,'api','fixture','{}',report,'2026-10-01T00:00:00Z',f'2026-10-{index%28+1:02d}T00:00:00Z'))
        return aid

    def test_records_pagination_deduplication_filters_and_persistent_favorites(self):
        for index in range(1,23):self.add_record(index)
        aid=self.add_record(23,status='failed',aid='2303.50001')
        self.add_record(24,status='running')
        self.add_record(25,kind='explain')
        result=self.service.records()
        self.assertEqual(result['total'],22);self.assertEqual(len(result['items']),20)
        self.assertEqual(len(result['active']),1)
        self.assertEqual(len(self.service.records(page=2)['items']),2)
        self.assertEqual(self.service.records(page=999)['page'],2)
        failed=self.service.records(status='failed')
        self.assertEqual(failed['total'],1);self.assertEqual(failed['items'][0]['arxiv_id'],aid)
        self.assertNotIn(aid,[r['arxiv_id'] for r in self.service.records(status='succeeded')['items']])
        self.service.favorite(aid,True);self.service.favorite(aid,True)
        other=Analyses(self.db,self.agent,html=self.html)
        favorites=other.records(favorites=True)
        self.assertEqual(favorites['total'],1);self.assertTrue(favorites['items'][0]['favorite'])
        self.assertEqual(other.records(query='ROBOT PAPER 23')['total'],1)
        self.assertEqual(other.records(query='%')['total'],0)
        self.assertEqual(other.records(query='_')['total'],0)
        self.service.favorite(aid,False)
        self.assertEqual(self.service.records(favorites=True)['total'],0)
        for kwargs in ({'page':0},{'page':True},{'status':'running'},{'favorites':'true'},{'query':'x'*301}):
            with self.assertRaises(AgentError):self.service.records(**kwargs)
        with self.assertRaises(AgentError):self.service.favorite(aid,'true')
        self.assertNotIn('report',result['items'][0])

    def test_records_owner_isolation_and_saved_success_beyond_recent_jobs(self):
        from shsf.seeds import SeedStore
        SeedStore(self.db,owner='other')
        aid=self.add_record(1,report='{"title":"saved"}')
        for index in range(2,16):self.add_record(index,status='failed',aid=aid)
        self.add_record(16,owner='other')
        self.assertEqual(self.service.records()['total'],1)
        paper=self.service.paper(aid)
        self.assertEqual(len(paper['jobs']),12)
        self.assertEqual(paper['saved_read']['report']['title'],'saved')
        self.assertEqual(paper['jobs'][0]['status'],'failed')
        other=Analyses(self.db,self.agent,owner='other',html=self.html)
        self.assertEqual(other.records()['total'],1)
        with self.assertRaises(AgentError):other.favorite(aid,True)

    def test_explain_is_abstract_only_and_cached(self):
        job=self.wait(self.service.start('2303.04137','explain'))
        self.assertEqual(job['status'],'succeeded',job['error'])
        self.assertEqual(job['report']['metadata']['basis'],'abstract_only')
        self.assertEqual(self.html.calls,0)
        self.assertEqual(self.service.start('2303.04137','explain')['job_id'],job['job_id'])
        self.assertEqual(len(self.route.calls),1)
        self.assertEqual(self.service.paper('2303.04137')['jobs'][0]['job_id'],job['job_id'])

    def test_read_uses_html_and_citations(self):
        result=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(result['status'],'succeeded',result['error'])
        self.assertEqual(result['report']['metadata']['basis'],'official_html_text')
        self.assertFalse(result['report']['metadata']['visual_analysis'])
        self.assertTrue(result['report']['sources'][0]['url'].startswith('https://arxiv.org/html/'))

    def test_long_paper_notes_cover_all_chunks(self):
        self.html.long=True
        result=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(result['status'],'succeeded',result['error'])
        chunks=result['report']['metadata']['chunks'];self.assertGreater(chunks,1)
        self.assertEqual(len(self.route.calls),chunks+1)
        with self.db.transaction() as tx:
            partial=json.loads(tx.execute('SELECT partial_json FROM analysis_jobs WHERE job_id=?',(result['job_id'],)).fetchone()['partial_json'])
        self.assertEqual(len(partial['notes']),chunks)

    def test_retry_reuses_completed_chunk_notes(self):
        self.html.long=True;self.route.fail_final=True
        failed=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(failed['status'],'failed')
        calls=len(self.route.calls);self.route.fail_final=False
        complete=self.wait(self.service.start('2303.04137','read',retry=True))
        self.assertEqual(complete['status'],'succeeded',complete['error'])
        self.assertEqual(len(self.route.calls),calls+1)

    def test_final_prompt_change_reuses_verified_legacy_notes(self):
        self.html.long=True;self.route.fail_final=True
        with patch('shsf.analyses.PROMPT_VERSION','legacy-report-prompt'):
            failed=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(failed['status'],'failed')
        with self.db.transaction() as tx:
            partial=json.loads(tx.execute('SELECT partial_json FROM analysis_jobs WHERE job_id=?',(failed['job_id'],)).fetchone()['partial_json'])
            partial.pop('notes_version',None)
            tx.execute('UPDATE analysis_jobs SET partial_json=? WHERE job_id=?',(json_text(partial),failed['job_id']))
        calls=len(self.route.calls);self.route.fail_final=False
        complete=self.wait(self.service.start('2303.04137','read'))
        self.assertNotEqual(complete['job_id'],failed['job_id'])
        self.assertEqual(complete['status'],'succeeded',complete['error'])
        self.assertEqual(len(self.route.calls),calls+1)
        self.assertEqual(complete['report']['metadata']['reused_chunks'],complete['report']['metadata']['chunks'])

    def test_invalid_cached_notes_are_recomputed_not_trusted(self):
        self.html.long=True;self.route.fail_final=True
        with patch('shsf.analyses.PROMPT_VERSION','legacy-report-prompt'):
            failed=self.wait(self.service.start('2303.04137','read'))
        with self.db.transaction() as tx:
            partial=json.loads(tx.execute('SELECT partial_json FROM analysis_jobs WHERE job_id=?',(failed['job_id'],)).fetchone()['partial_json'])
            partial['notes']['0'][0]['quote']='An invented passage that never appeared in the paper.'
            tx.execute('UPDATE analysis_jobs SET partial_json=? WHERE job_id=?',(json_text(partial),failed['job_id']))
        calls=len(self.route.calls);self.route.fail_final=False
        complete=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(complete['status'],'succeeded',complete['error'])
        self.assertEqual(len(self.route.calls),calls+2)
        self.assertEqual(complete['report']['metadata']['reused_chunks'],complete['report']['metadata']['chunks']-1)

    def test_invalid_final_evidence_id_retains_diagnostics_and_no_report(self):
        self.html.long=True
        original=self.route.complete
        def invalid(prompt):
            text=original(prompt)
            data=json.loads(text)
            if 'sections' in data:data['sections'][0]['claims'][0]['citations']=[{'evidence_id':'E999'}]
            return json_text(data)
        self.route.complete=invalid
        failed=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(failed['status'],'failed');self.assertIsNone(failed['report'])
        self.assertIn('核心问题与贡献',failed['error'])
        with self.db.transaction() as tx:
            partial=json.loads(tx.execute('SELECT partial_json FROM analysis_jobs WHERE job_id=?',(failed['job_id'],)).fetchone()['partial_json'])
        self.assertIn('E999',partial['report_validation']['response'])
        self.route.complete=original
        complete=self.wait(self.service.start('2303.04137','read',retry=True))
        self.assertEqual(complete['status'],'succeeded',complete['error'])
        with self.db.transaction() as tx:
            partial=json.loads(tx.execute('SELECT partial_json FROM analysis_jobs WHERE job_id=?',(complete['job_id'],)).fetchone()['partial_json'])
        self.assertNotIn('report_validation',partial)

    def test_retry_revalidates_saved_report_without_model_call(self):
        with patch('shsf.analyses.validate_report',side_effect=AgentError('旧版引用限制')):
            failed=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(failed['status'],'failed')
        calls=len(self.route.calls)
        complete=self.wait(self.service.start('2303.04137','read',retry=True))
        self.assertEqual(complete['status'],'succeeded',complete['error'])
        self.assertEqual(len(self.route.calls),calls)

    def test_official_html_is_cached_without_another_download(self):
        import io
        from shsf.paper_html import PaperHTML
        class Response(io.BytesIO):
            headers={'Content-Type':'text/html; charset=utf-8'}
        class Opener:
            calls=0
            def open(self,request,timeout):
                self.calls+=1
                self.url=request.full_url
                return Response(html_document())
        opener=Opener();reader=PaperHTML(self.db,self.tmp.name,opener)
        with self.db.transaction() as tx:paper=dict(tx.execute('SELECT * FROM papers WHERE arxiv_id=?',('2303.04137',)).fetchone())
        first=reader.get(paper);second=reader.get(paper)
        self.assertEqual(first,second);self.assertEqual(opener.calls,1)
        self.assertEqual(opener.url,'https://arxiv.org/html/2303.04137v1')

    def test_unavailable_html_never_becomes_full_read(self):
        self.html.failed=True
        result=self.wait(self.service.start('2303.04137','read'))
        self.assertEqual(result['status'],'failed');self.assertIsNone(result['report']);self.assertEqual(self.route.calls,[])
        self.html.failed=False
        retried=self.wait(self.service.start('2303.04137','read',retry=True))
        self.assertEqual(retried['status'],'succeeded',retried['error'])

    def test_failure_requires_explicit_retry_and_context_change_invalidates_cache(self):
        self.route.fail=True
        failed=self.wait(self.service.start('2303.04137','explain'))
        self.assertEqual(failed['status'],'failed')
        self.route.fail=False
        self.assertEqual(self.service.start('2303.04137','explain')['status'],'failed')
        success=self.wait(self.service.start('2303.04137','explain',retry=True));self.assertEqual(success['status'],'succeeded')
        with self.db.transaction() as tx:tx.execute("UPDATE papers SET input_hash='changed',abstract='Updated paper abstract and content.' WHERE arxiv_id=?",('2303.04137',))
        new=self.wait(self.service.start('2303.04137','explain'))
        self.assertNotEqual(new['job_id'],success['job_id'])

    def test_http_start_poll_and_readback(self):
        import threading
        from urllib.request import Request,urlopen
        from shsf.web import make_server
        server=make_server(self.db,0,self.agent);server.app.analyses=self.service
        self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        request=Request(base+'/api/analysis/start',data=json_text({'arxiv_id':'2303.04137','kind':'read'}).encode(),headers={'Content-Type':'application/json','X-SHSF-Token':server.app.token})
        with urlopen(request) as response:job=json.load(response)
        done=self.wait(job);self.assertEqual(done['status'],'succeeded',done['error'])
        with urlopen(base+'/api/analysis/job?job_id='+done['job_id']) as response:self.assertEqual(json.load(response)['report'],done['report'])
        with urlopen(base+'/api/analysis/paper?arxiv_id=2303.04137') as response:self.assertTrue(json.load(response)['jobs'])
        with urlopen(base+'/api/analysis/records') as response:
            records=json.load(response);self.assertEqual(records['total'],1)
        request=Request(base+'/api/analysis/favorite',data=json_text({'arxiv_id':'2303.04137','favorite':True}).encode(),headers={'Content-Type':'application/json','X-SHSF-Token':server.app.token})
        with urlopen(request) as response:self.assertTrue(json.load(response)['favorite'])
        with urlopen(base+'/api/analysis/records?favorites=true') as response:self.assertEqual(json.load(response)['total'],1)


    def test_restart_does_not_automatically_repeat_inference(self):
        job=self.wait(self.service.start('2303.04137','read'))
        with self.db.transaction() as tx:tx.execute("UPDATE analysis_jobs SET status='running' WHERE job_id=?",(job['job_id'],))
        calls=len(self.route.calls);self.service.recover()
        self.assertEqual(self.service.get(job['job_id'])['status'],'failed');self.assertEqual(len(self.route.calls),calls)


class SQLiteAnalysisTests(AnalysisContract,unittest.TestCase):
    def setUp(self):
        from shsf.database import Database
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database('sqlite:///'+self.tmp.name+'/analysis.db');self.db.migrate();self.setup_analysis()


@unittest.skipUnless(os.getenv('SHSF_TEST_DATABASE_URL'),'requires isolated PostgreSQL')
class PostgresAnalysisTests(AnalysisContract,unittest.TestCase):
    def setUp(self):
        from shsf.database import Database
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(os.environ['SHSF_TEST_DATABASE_URL']);self.db.migrate()
        def clean():
            with self.db.transaction() as tx:tx.execute('TRUNCATE seed_workspaces,seed_items,papers,embedding_models,ingestion_jobs CASCADE')
        clean();self.addCleanup(clean);self.setup_analysis()


class EvidenceTests(unittest.TestCase):
    def test_html_excludes_abstract_scripts_and_has_original_anchors(self):
        document=parse_html(html_document(),'https://arxiv.org/html/2303.04137v1')
        text=json_text(document)
        self.assertNotIn('steal secret',text);self.assertNotIn('Only abstract',text)
        self.assertTrue(any(s['url'].endswith('#S1.p1') for s in document['sources']))
        self.assertTrue(any('图注' in s['label'] for s in document['sources']))
        with self.assertRaises(AgentError):parse_html(b'<html><p>error page</p></html>','https://arxiv.org/html/2303.04137v1')

    def test_fabricated_quotes_and_sources_are_rejected(self):
        sources=[{'id':'P1','text':'Real experimental evidence from the original paper.','label':'Method','url':'https://arxiv.org/html/2303.04137v1#S1'}]
        with self.assertRaises(AgentError):validate_notes(json_text({'notes':[{'text':'note','source_id':'P1','quote':'Made up experimental evidence'}]}),sources)
        with self.assertRaises(AgentError):validate_notes(json_text({'notes':[{'text':'note','source_id':'P2','quote':'Real experimental evidence'}]}),sources)

    def test_five_valid_citations_are_accepted_and_missing_citation_has_location(self):
        sources=[{'id':f'P{i}','text':'Real experimental evidence from the paper.','label':'Method','url':'https://arxiv.org/'} for i in range(5)]
        citations=[{'source_id':s['id'],'quote':s['text']} for s in sources]
        report={'title':'报告','sections':[{'heading':h,'claims':[{'text':'分析','kind':'inference','citations':citations}]} for h in HEADINGS['read']]}
        checked=validate_report(json_text(report),sources,'read')
        self.assertEqual(len(checked['sections'][0]['claims'][0]['citations']),5)
        claim=report['sections'][0]['claims'][0]
        claim.update(kind='paper',citations=[])
        with self.assertRaisesRegex(AgentError,'核心问题与贡献.*第 1 条.*作者陈述缺少原文引用'):
            validate_report(json_text(report),sources,'read')
        claim.update(kind='inference',citations=None)
        with self.assertRaisesRegex(AgentError,'引用必须是列表'):
            validate_report(json_text(report),sources,'read')

    def test_synthesis_resolves_only_verified_evidence_ids(self):
        sources=[{'id':'P1','text':'Real experimental evidence from the original paper.','label':'Method','url':'https://arxiv.org/html/2303.04137v1#S1'}]
        notes=[{'text':'原文证据笔记。','source_id':'P1','quote':'Real experimental evidence'}]
        catalog=evidence_catalog(notes,sources)
        self.assertEqual(catalog[0]['evidence_id'],'E1')
        prompt=report_prompt('read',{},sources,notes)
        self.assertIn('evidence_id',prompt)
        report={'title':'精读报告','sections':[{'heading':heading,'claims':[{'text':'有证据支持的作者陈述。','kind':'paper','citations':[{'evidence_id':'E1'}]}]} for heading in HEADINGS['read']]}
        checked=validate_report(json_text(report),sources,'read',evidence=notes)
        citation=checked['sections'][0]['claims'][0]['citations'][0]
        self.assertEqual(citation,{'source_id':'P1','quote':'Real experimental evidence'})
        for invalid in [{'evidence_id':'E999'},{'evidence_id':'E1','quote':'Invented quote'},{'source_id':'P1','quote':'Real experimental evidence'}]:
            report['sections'][0]['claims'][0]['citations']=[invalid]
            with self.assertRaises(AgentError):validate_report(json_text(report),sources,'read',evidence=notes)
        notes[0]['quote']='Invented experimental evidence'
        with self.assertRaises(AgentError):evidence_catalog(notes,sources)
