"""Exercise the actual HTTP import flow against a fixture Zotero transport."""
import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from shsf.web import Application, make_server
from shsf.zotero import ZoteroClient, ZoteroError
import test_seeds as fixtures
from test_seeds import Opener, Response, item


class FixtureOpener(Opener):
    def open(self, request, timeout):
        if '/items' in request.full_url:
            self.requests.append(request)
            return Response([item(url='https://arxiv.org/abs/2303.04137'),
                             item(key='PENDING1', title='New humanoid policy')], '2')
        return super().open(request, timeout)


class ZoteroFlowTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.StoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.addpaper()
        self.db = fixture.db

    def test_connect_preview_import_persist_and_disconnect(self):
        server = make_server(self.db, 0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        transport = FixtureOpener()
        def request(path, body=None):
            req = Request(base + path)
            if body is not None:
                req = Request(base + path, data=json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json', 'X-SHSF-Token': server.app.token})
            with urlopen(req) as response:
                return json.load(response)

        with patch('shsf.web.ZoteroClient', side_effect=lambda key: ZoteroClient(key, transport)):
            connected = request('/api/zotero/connect', {'api_key': 'a' * 24})
        self.assertEqual(connected['user_id'], '123')
        self.assertNotIn('a' * 24, json.dumps(connected))
        collections = request('/api/zotero/collections')
        self.assertEqual(collections['items'][0]['name'], 'Robotics')
        preview = request('/api/zotero/items?collection=COLLECT1')
        self.assertEqual([p['match_status'] for p in preview['items']], ['matched', 'unmatched'])
        self.assertEqual(request('/api/bootstrap')['state']['items'], [])
        imported = request('/api/zotero/import', {'item_keys': ['ABCDEFGH', 'PENDING1'], 'expected_revision': 0})
        self.assertEqual(imported['state']['matched'], 1)
        self.assertEqual(imported['state']['pending'], 1)
        self.assertEqual(len(request('/api/bootstrap')['state']['items']), 2)
        # A second import must merge the same provenance, not create more seeds.
        imported = request('/api/zotero/import', {'item_keys': ['ABCDEFGH', 'PENDING1'], 'expected_revision': 1})
        self.assertEqual(len(imported['state']['items']), 2)
        self.assertTrue(any('/items?itemKey=' in req.full_url for req in transport.requests))
        request('/api/zotero/disconnect', {})
        self.assertIsNone(server.app.zotero)
        self.assertFalse(request('/api/bootstrap')['zotero']['connected'])
        for table in ['seed_items', 'seed_sources', 'seed_workspaces']:
            with self.db.transaction() as tx:
                rows = [dict(row) for row in tx.execute('SELECT * FROM ' + table).fetchall()]
            self.assertNotIn('a' * 24, json.dumps(rows))

    def test_disconnect_cancels_inflight_connection(self):
        app = Application(self.db)
        entered, resume = threading.Event(), threading.Event()
        errors = []
        class SlowClient:
            def __init__(self, key): pass
            def connect(self, user_id):
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError('test timeout')
                return {'connected': True}
        def connect():
            try: app.dispatch('POST', '/api/zotero/connect', {}, {'api_key': 'a' * 24})
            except Exception as exc: errors.append(exc)
        with patch('shsf.web.ZoteroClient', SlowClient):
            thread = threading.Thread(target=connect)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                app.dispatch('POST', '/api/zotero/disconnect', {}, {})
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(app.zotero)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ZoteroError)

    def test_disconnect_during_fetch_prevents_import(self):
        app = Application(self.db)
        class DisconnectingClient:
            user_id = '123'
            def selected(self, keys):
                app.dispatch('POST', '/api/zotero/disconnect', {}, {})
                return [item()]
        app.zotero = DisconnectingClient()
        with self.assertRaises(ZoteroError):
            app.dispatch('POST', '/api/zotero/import', {}, {'item_keys': ['ABCDEFGH'], 'expected_revision': 0})
        self.assertEqual(app.store.state()['items'], [])
