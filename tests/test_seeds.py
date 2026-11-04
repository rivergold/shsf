import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shsf.database import Database
from shsf.embeddings import EmbeddingSpec
from shsf.ingestion import Ingester
from shsf.arxiv import Page
from shsf.models import Paper
from shsf.profile import Profile
from shsf.seeds import SeedStore, RevisionConflict, zotero_metadata
from shsf.web import make_server
from shsf.zotero import ZoteroClient, ZoteroError


def item(key='ABCDEFGH', title='Diffusion Policy', **data):
    return {'key':key,'version':3,'data':dict(itemType='journalArticle',title=title,abstractNote='<p>Robot learning</p>',creators=[{'creatorType':'author','name':'Cheng Chi'}],date='2023',collections=['COLLECT1'],**data)}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Database('sqlite:///'+self.tmp.name+'/test.db'); self.db.migrate()
        self.store=SeedStore(self.db)

    def addpaper(self, aid='2303.04137', title='Diffusion Policy', doi=None):
        spec=EmbeddingSpec(); profile=Profile(Path(__file__).resolve().parents[1]/'config/robotics.json')
        ingester=Ingester(self.db,profile,spec,None,self.tmp.name)
        ingester.import_page(Page([Paper(arxiv_id=aid,version=1,title=title,abstract='Robot learning with action diffusion',authors=['Cheng Chi'],categories=['cs.RO'],published_at='2023-03-07',updated_at='2023-03-07',doi=doi)]),'/test/raw')

    def test_search_literal_ids_and_author(self):
        self.addpaper(doi='10.1234/policy')
        for q in ['diffusion','https://arxiv.org/abs/2303.04137v2','https://doi.org/10.1234/policy']:
            self.assertEqual(self.store.search(q)['total'],1)
        self.assertEqual(self.store.search('%')['total'],0)
        self.assertEqual(self.store.search('Chi','author')['total'],1)
        self.assertEqual(self.store.search('unknown','identifier')['total'],0)

    def test_local_zotero_dedupe_snapshot_and_conflict(self):
        self.addpaper(); first=self.store.add_local('2303.04137',0)
        result=self.store.import_zotero([item(url='https://arxiv.org/abs/2303.04137')],'123',first['revision'])
        self.assertEqual(len(result['state']['items']),1)
        self.assertEqual(len(result['state']['items'][0]['sources']),2)
        with self.assertRaises(RevisionConflict):self.store.remove('anything',0)
        confirmed=self.store.confirm(result['state']['revision']);self.assertTrue(confirmed['confirmed'])
        saved=SeedStore(self.db).state();self.assertEqual(saved,confirmed)
        draft=self.store.remove(saved['items'][0]['seed_id'],saved['revision']);self.assertFalse(draft['confirmed'])
        with self.db.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) AS n FROM confirmed_seeds').fetchone()['n'],1)

    def test_pending_matches_when_paper_arrives(self):
        state=self.store.import_zotero([item()],'123',0)['state'];self.assertEqual(state['pending'],1)
        self.addpaper();state=self.store.add_local('2303.04137',state['revision'])
        self.assertEqual(len(state['items']),1);self.assertEqual(state['matched'],1)

    def test_resolved_source_merges_existing_selection(self):
        state=self.store.import_zotero([item(title='An earlier working title')],'123',0)['state']
        self.addpaper();state=self.store.add_local('2303.04137',state['revision'])
        self.assertEqual(len(state['items']),2)
        state=self.store.import_zotero([item(url='https://arxiv.org/abs/2303.04137')],'123',state['revision'])['state']
        self.assertEqual(len(state['items']),1)
        self.assertEqual(len(state['items'][0]['sources']),2)

    def test_import_limit_rolls_back_entire_batch(self):
        batch=[item(key=f'{i:08d}',title=f'Unique paper {i}') for i in range(101)]
        with self.assertRaises(ValueError):self.store.import_zotero(batch,'123',0)
        self.assertEqual(self.store.state()['items'],[])
        self.assertEqual(self.store.state()['revision'],0)

    def test_conflicting_identifiers_stay_pending(self):
        self.addpaper();preview=self.store.preview([item(url='https://arxiv.org/abs/2401.00001')],'123')
        self.assertIsNone(preview[0]['arxiv_id'])

    def test_import_rollback_and_filtered_html(self):
        with self.assertRaises(ValueError):self.store.import_zotero([item(),item(key='bad')],'123',0)
        self.assertEqual(self.store.state()['revision'],0);self.assertEqual(self.store.state()['items'],[])
        self.assertEqual(zotero_metadata(item(title='<b>Title</b><script>bad()</script>'),'123')['title'],'Title')
        self.assertIsNone(zotero_metadata({'data':{'itemType':'attachment'}},'123'))

    def test_http_security_and_persistence(self):
        self.addpaper();server=make_server(self.db,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        with urlopen(base) as response:self.assertIn(server.app.token,response.read().decode())
        def post(token,rev):
            return urlopen(Request(base+'/api/seeds/add',data=json.dumps({'arxiv_id':'2303.04137','expected_revision':rev}).encode(),headers={'Content-Type':'application/json','X-SHSF-Token':token}))
        with self.assertRaises(HTTPError) as error:post('bad',0)
        self.assertEqual(error.exception.code,403)
        with post(server.app.token,0) as response:self.assertEqual(json.load(response)['state']['matched'],1)
        with self.assertRaises(HTTPError) as error:post(server.app.token,0)
        self.assertEqual(error.exception.code,409)
        with self.assertRaises(HTTPError):urlopen(Request(base+'/api/bootstrap',headers={'Host':'evil.example'}))


class Response(io.BytesIO):
    def __init__(self,data,total='0'):
        super().__init__(json.dumps(data).encode());self.headers={'Total-Results':total}

class Opener:
    def __init__(self):self.requests=[]
    def open(self,request,timeout):
        self.requests.append(request)
        if request.full_url.endswith('/keys/current'):return Response({'userID':123,'username':'reader','access':{'user':{'library':True}}})
        if '/collections?' in request.full_url:return Response([{'key':'COLLECT1','data':{'name':'Robotics'}}],'1')
        return Response([item()],'1')

class ZoteroTests(unittest.TestCase):
    def test_readonly_credentials_and_pagination(self):
        opener=Opener();client=ZoteroClient('a'*24,opener);self.assertEqual(client.connect()['user_id'],'123')
        self.assertEqual(client.collections()['items'][0]['name'],'Robotics')
        client.items('COLLECT1',50);self.assertEqual(len(client.selected(['ABCDEFGH'])),1)
        for req in opener.requests:
            self.assertEqual(req.get_method(),'GET');self.assertNotIn('a'*24,req.full_url)
            self.assertEqual(req.get_header('Zotero-api-key'),'a'*24)
        self.assertIn('start=50',opener.requests[-2].full_url)
        with self.assertRaises(ZoteroError):client.items('../evil')
        with self.assertRaises(ZoteroError):client.connect('999')
        client._backoff({'Backoff':'30'})
        with self.assertRaises(ZoteroError):client.items()

@unittest.skipUnless(os.getenv('SHSF_TEST_DATABASE_URL'), 'requires isolated PostgreSQL test database')
class PostgresStoreTests(StoreTests):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Database(os.environ['SHSF_TEST_DATABASE_URL']);self.db.migrate()
        def clean():
            with self.db.transaction() as tx:
                tx.execute('TRUNCATE seed_workspaces,seed_items,papers,embedding_models,ingestion_jobs CASCADE')
        clean();self.addCleanup(clean)
        self.store=SeedStore(self.db)
