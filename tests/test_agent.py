import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlsplit
from urllib.request import Request,urlopen

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from shsf.agent_config import AgentConfig,AgentError,environment
from shsf.agent_service import AgentService
from shsf.agent_transport import Transport
from shsf.chatgpt_auth import ChatGPTAuth,AUTH,RESOURCE,TOKEN


class ConfigTests(unittest.TestCase):
    def test_env_precedence_reload_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            path=Path(tmp)/'.env'
            path.write_text('SHSF_LLM_API_KEY="private-$literal"\nSHSF_LLM_MODEL=model-a\nSHSF_LLM_BASE_URL=https://example.org/v1\n')
            config=AgentConfig.load(tmp)
            self.assertEqual(config.api_key,'private-$literal')
            self.assertTrue(config.public()['ready'])
            self.assertNotIn('private-$literal',repr(config)+json.dumps(config.public()))
            with patch.dict(os.environ,{'SHSF_LLM_MODEL':'env-model'}):
                self.assertEqual(AgentConfig.load(tmp).read_model,'env-model')
            service=AgentService(tmp)
            path.write_text(path.read_text().replace('model-a','model-b'))
            self.assertEqual(service.reload()['api']['read_model'],'model-b')

    def test_invalid_urls_and_timeout_do_not_expose_secret(self):
        for url in ('https://user:secret@example.org/v1','https://example.org/v1?key=secret','http://remote.example/v1','file:///tmp/key'):
            with self.subTest(url=url),tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'SHSF_LLM_BASE_URL':url},clear=True):
                config=AgentConfig.load(tmp);self.assertTrue(config.error)
                self.assertNotIn('secret',json.dumps(config.public()))
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'SHSF_LLM_TIMEOUT':'abc-secret'},clear=True):
            self.assertNotIn('abc-secret',AgentConfig.load(tmp).error)


class Response(io.BytesIO):pass
class Opener:
    def __init__(self,body):self.body=body;self.requests=[]
    def open(self,request,timeout):self.requests.append(request);return Response(self.body)

class TransportTests(unittest.TestCase):
    def test_api_key_and_payload(self):
        opener=Opener(b'{"choices":[{"message":{"content":"SHSF"}}]}')
        self.assertEqual(Transport(opener).complete('https://example.org/v1','paper-model','hello','secret','chat_completions'),'SHSF')
        req=opener.requests[0];self.assertEqual(req.get_header('Authorization'),'Bearer secret')
        self.assertNotIn('secret',req.full_url+req.data.decode())

    def test_stream_requires_terminal_success(self):
        def sse(event):return ('data: '+json.dumps(event)+'\n\n').encode()
        delta=sse({'type':'response.output_text.delta','delta':'SHSF'})
        done=sse({'type':'response.completed','response':{}})
        opener=Opener(delta+done)
        self.assertEqual(Transport(opener).complete(RESOURCE,'model','hello','access'),'SHSF')
        body=json.loads(opener.requests[0].data);self.assertFalse(body['store']);self.assertTrue(body['stream'])
        self.assertNotIn('max_output_tokens',body)
        for data in (delta,delta+sse({'type':'response.failed'}),delta+sse({'type':'response.incomplete'})):
            with self.assertRaises(AgentError):Transport(Opener(data)).complete(RESOURCE,'model','hello','access')

    def test_provider_error_is_sanitized(self):
        class Failed:
            def open(self,*args,**kwargs):raise HTTPError('https://secret-url',401,'Bearer secret',{},None)
        with self.assertRaises(AgentError) as error:Transport(Failed()).json('https://example.org',token='secret')
        self.assertNotIn('secret',str(error.exception))


class AuthTransport:
    def __init__(self,key):
        self.key=key;self.nonce='';self.calls=[];self.subject='reader';self.scopes='openid chatgpt.tokens.use.direct';self.bad_nonce=False;self.fail_revoke=False
    def json(self,url,body=None,token=None,form=False,empty=False):
        self.calls.append((url,body,token))
        if url.endswith('/.well-known/openid-configuration'):
            return {'issuer':AUTH,'jwks_uri':AUTH+'/.well-known/jwks.json','revocation_endpoint':AUTH+'/oauth/revoke'}
        if url.endswith('/jwks.json'):
            key=json.loads(RSAAlgorithm.to_jwk(self.key.public_key()));key['kid']='test';return {'keys':[key]}
        if url==TOKEN:
            if body['grant_type']=='refresh_token':return {'access_token':'refreshed-access','refresh_token':'rotated-refresh','token_type':'Bearer','expires_in':3600}
            claims={'iss':AUTH,'sub':self.subject,'aud':body['client_id'],'iat':int(time.time()),'exp':int(time.time())+3600,'nonce':'wrong' if self.bad_nonce else self.nonce,'email':'reader@example.org'}
            return {'access_token':'private-access','refresh_token':'private-refresh','id_token':jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'test'}),'token_type':'Bearer','expires_in':3600,'scope':self.scopes}
        if url.endswith('/oauth/revoke'):
            if self.fail_revoke:raise AgentError('network failure')
            return {}
        if url==RESOURCE+'/models':return {'models':[{'slug':'paper-model','display_name':'Paper model','visibility':'list'},{'slug':'hidden','visibility':'hidden'}]}
        raise AssertionError(url)
    def complete(self,*args,**kwargs):return 'SHSF'


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.transport=AuthTransport(self.key);self.auth=ChatGPTAuth(Path(self.tmp.name)/'auth',self.transport)
        self.auth.callback_uri='http://127.0.0.1:12345/auth/callback'
    def login(self,client='oaiapp_test'):
        start=self.auth.start();query=parse_qs(urlsplit(start['authorization_url']).query)
        self.transport.nonce=query['nonce'][0]
        return self.auth.callback({'state':query['state'],'client_id':[client],'code':['test-code']})

    def test_pkce_state_signature_and_protected_storage(self):
        status=self.login();self.assertTrue(status['sharing'])
        self.assertEqual(status['active'],'oaiapp_test')
        self.assertNotIn('private-access',json.dumps(status))
        path=self.auth.store.directory/'chatgpt.json'
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        initial=json.loads(path.read_text())
        self.assertTrue(initial['host_id'].startswith('urn:uuid:'))
        self.assertEqual(ChatGPTAuth(self.auth.store.directory).status()['active'],'oaiapp_test')
        request=self.auth.start('oaiapp_test');query=parse_qs(urlsplit(request['authorization_url']).query)
        self.assertEqual(query['ext_agent_host_id'][0],initial['host_id']);self.assertNotIn('agent_name_hint',query)
        verifier=self.auth.pending['verifier'];import hashlib
        self.assertEqual(query['code_challenge'][0],base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
        with self.assertRaises(AgentError):self.auth.callback({'state':['wrong'],'code':['code']})

    def test_missing_scope_cannot_infer(self):
        self.transport.scopes='openid email';self.assertFalse(self.login()['sharing'])
        with self.assertRaises(AgentError):self.auth.access()

    def test_execution_distinguishes_login_permission_and_missing_model(self):
        service=AgentService(self.tmp.name,auth=self.auth)
        with self.auth.store.transaction() as data:data.setdefault('settings',{})['mode']='chatgpt'
        with self.assertRaisesRegex(AgentError,'尚未连接'):
            service.execution('read')
        self.transport.scopes='openid email';self.login()
        with self.assertRaisesRegex(AgentError,'尚未授权套餐使用'):
            service.execution('read')
        self.transport.scopes='openid profile email offline_access resource.invoke chatgpt.tokens.use.direct';self.login()
        with self.assertRaisesRegex(AgentError,'尚未保存单篇精读模型'):
            service.execution('read')
        with self.assertRaisesRegex(AgentError,'尚未保存推荐解读模型'):
            service.execution('explain')
        service.save({'mode':'chatgpt','chatgpt_read_model':'paper-model','chatgpt_explain_model':'paper-model'})
        self.assertEqual(service.execution('read').model,'paper-model')

    def test_invalid_nonce_and_returning_identity_not_accepted(self):
        self.transport.bad_nonce=True
        with self.assertRaises(AgentError):self.login()
        self.assertFalse(self.auth.status()['connected'])
        self.transport.bad_nonce=False;self.login()
        request=self.auth.start('oaiapp_test');query=parse_qs(urlsplit(request['authorization_url']).query)
        self.transport.nonce=query['nonce'][0];self.transport.subject='different-reader'
        with self.assertRaises(AgentError):self.auth.callback({'state':query['state'],'code':['code']})
        with self.auth.store.transaction() as data:self.assertEqual(data['profiles']['oaiapp_test']['subject'],'reader')

    def test_callback_replay_and_cancelled_attempt(self):
        request=self.auth.start();query=parse_qs(urlsplit(request['authorization_url']).query)
        self.transport.nonce=query['nonce'][0]
        callback={'state':query['state'],'client_id':['oaiapp_test'],'code':['code']}
        self.auth.callback(callback)
        with self.assertRaises(AgentError):self.auth.callback(callback)
        request=self.auth.start();query=parse_qs(urlsplit(request['authorization_url']).query)
        self.auth.disconnect()
        with self.assertRaises(AgentError):self.auth.callback({'state':query['state'],'code':['code']})

    def test_remote_completion_preserves_original_redirect_and_rejects_replay(self):
        from urllib.parse import urlencode
        start=self.auth.start();query=parse_qs(urlsplit(start['authorization_url']).query)
        self.transport.nonce=query['nonce'][0]
        verifier=self.auth.pending['verifier']
        returned=self.auth.callback_uri+'?'+urlencode({'state':query['state'][0],'client_id':'oaiapp_test','code':'manual-code'})
        self.assertTrue(self.auth.complete_remote(returned)['sharing'])
        exchange=next(body for url,body,_ in self.transport.calls if url==TOKEN)
        self.assertEqual(exchange['redirect_uri'],query['redirect_uri'][0])
        self.assertEqual(exchange['code_verifier'],verifier)
        self.assertNotIn('manual-code',(self.auth.store.directory/'chatgpt.json').read_text())
        with self.assertRaises(AgentError):self.auth.complete_remote(returned)

    def test_remote_completion_validates_url_before_consuming_pending_login(self):
        from urllib.parse import urlencode
        start=self.auth.start();query=parse_qs(urlsplit(start['authorization_url']).query)
        parameters=urlencode({'state':query['state'][0],'client_id':'oaiapp_test','code':'manual-code'})
        expected=self.auth.callback_uri
        for value in [None, '', 'x'*8193, 'https://evil.example/auth/callback?'+parameters,
                      expected.replace('12345','12346')+'?'+parameters,
                      expected.replace('/auth/callback','/other')+'?'+parameters,
                      expected+'?'+parameters+'&state=duplicate', expected+'?'+parameters+'#fragment',
                      expected+'?state='+query['state'][0]]:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(AgentError):self.auth.complete_remote(value)
                self.assertIsNotNone(self.auth.pending)
        self.assertFalse(any(url==TOKEN for url,_,_ in self.transport.calls))

    def test_remote_completion_checks_state_and_expiry(self):
        from urllib.parse import urlencode
        start=self.auth.start();query=parse_qs(urlsplit(start['authorization_url']).query)
        def returned(state):
            return self.auth.callback_uri+'?'+urlencode({'state':state,'client_id':'oaiapp_test','code':'manual-code'})
        with self.assertRaises(AgentError):self.auth.complete_remote(returned('wrong-state'))
        self.assertIsNotNone(self.auth.pending)
        self.auth.pending['deadline']=time.time()-1
        with self.assertRaises(AgentError):self.auth.complete_remote(returned(query['state'][0]))
        self.assertFalse(any(url==TOKEN for url,_,_ in self.transport.calls))

    def test_remote_completion_http_requires_same_origin_and_page_token(self):
        from urllib.parse import urlencode
        import test_seeds as fixtures
        from shsf.web import make_server
        fixture=fixtures.StoreTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ,{},clear=True):service=AgentService(self.tmp.name,auth=self.auth)
        server=make_server(fixture.db,0,service,host='0.0.0.0');self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        origin=f'http://shsf.example:{server.server_port}'
        start=self.auth.start();query=parse_qs(urlsplit(start['authorization_url']).query)
        self.transport.nonce=query['nonce'][0]
        returned=self.auth.callback_uri+'?'+urlencode({'state':query['state'][0],'client_id':'oaiapp_test','code':'manual-code'})
        payload=json.dumps({'callback_url':returned}).encode()
        headers={'Content-Type':'application/json','Host':f'shsf.example:{server.server_port}', 'Origin':origin}
        for overrides in [{}, {'X-SHSF-Token':server.app.token,'Origin':'http://other.example'}]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base+'/api/agent/chatgpt/complete',data=payload,headers={**headers,**overrides}))
            self.assertEqual(error.exception.code,403)
            self.assertIsNotNone(self.auth.pending)
        with urlopen(Request(base+'/api/agent/chatgpt/complete',data=payload,headers={**headers,'X-SHSF-Token':server.app.token})) as response:
            result=json.load(response)
        self.assertTrue(result['sharing'])
        self.assertNotIn('private-access',json.dumps(result))

    def test_refresh_rotation_and_models(self):
        self.login()
        with self.auth.store.transaction() as data:data['profiles']['oaiapp_test']['expires_at']=0
        token,client=self.auth.access();self.assertEqual(token,'refreshed-access');self.assertEqual(client,'oaiapp_test')
        with self.auth.store.transaction() as data:self.assertEqual(data['profiles'][client]['refresh_token'],'rotated-refresh')
        calls=[body for url,body,_ in self.transport.calls if url==TOKEN and body['grant_type']=='refresh_token']
        self.assertEqual(calls[0]['client_id'],'oaiapp_test');self.assertNotIn('scope',calls[0])
        self.assertEqual(self.auth.models(),[{'id':'paper-model','name':'Paper model'}])

    def test_logout_clears_credentials_on_remote_failure(self):
        self.login();self.transport.fail_revoke=True
        self.assertFalse(self.auth.disconnect()['remote_revoked'])
        raw=(self.auth.store.directory/'chatgpt.json').read_text()
        self.assertNotIn('private-access',raw);self.assertNotIn('private-refresh',raw)
        self.assertIn('oaiapp_test',raw);self.assertFalse(self.auth.status()['connected'])

    def test_no_fallback_and_model_selection_is_explicit(self):
        self.login();service=AgentService(self.tmp.name,auth=self.auth)
        service.save({'mode':'chatgpt','chatgpt_explain_model':'paper-model','chatgpt_read_model':'paper-model'})
        self.assertFalse(service.status()['automatic_fallback'])
        self.assertEqual(service.test()['mode'],'chatgpt')
        self.auth.disconnect()
        with self.assertRaises(AgentError):service.test()
        self.assertIsNone(service.status()['last_test'])
        with self.assertRaises(AgentError):service.save({'mode':'chatgpt','chatgpt_read_model':'unavailable'})

    def test_invalid_signature_and_expiry_are_rejected(self):
        other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims={'iss':AUTH,'sub':'reader','aud':'oaiapp_test','iat':int(time.time())-500,'exp':int(time.time())+300,'nonce':'nonce'}
        forged=jwt.encode(claims,other,algorithm='RS256',headers={'kid':'test'})
        with self.assertRaises(AgentError):self.auth.verify_identity(forged,'oaiapp_test','nonce')
        claims['exp']=int(time.time())-300
        expired=jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'test'})
        with self.assertRaises(AgentError):self.auth.verify_identity(expired,'oaiapp_test','nonce')

    def test_multiple_accounts_remain_separate(self):
        self.login('oaiapp_first');self.login('oaiapp_second')
        self.assertEqual(len(self.auth.status()['profiles']),2)
        self.auth.select('oaiapp_first')
        self.assertEqual(self.auth.access()[1],'oaiapp_first')
        self.auth.disconnect('oaiapp_second')
        self.assertTrue(self.auth.status()['sharing'])

    def test_http_configuration_and_oauth_callback_flow(self):
        import test_seeds as fixtures
        from shsf.web import make_server
        fixture=fixtures.StoreTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=Path(self.tmp.name)
        (root/'.env').write_text('SHSF_LLM_API_KEY=private-test-key\nSHSF_LLM_MODEL=paper-model\nSHSF_LLM_BASE_URL=https://example.org/v1\n')
        with patch.dict(os.environ,{},clear=True):service=AgentService(root,auth=self.auth)
        server=make_server(fixture.db,0,service);self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,body=None):
            req=Request(base+path)
            if body is not None:req=Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-SHSF-Token':server.app.token})
            with urlopen(req) as response:return json.load(response)
        self.assertNotIn('private-test-key',json.dumps(request('/api/agent')))
        with patch('shsf.agent_service.Transport',return_value=Transport(Opener(b'{"choices":[{"message":{"content":"SHSF"}}]}'))):
            self.assertEqual(request('/api/agent/test',{})['reply'],'SHSF')
        start=request('/api/agent/chatgpt/start',{})
        query=parse_qs(urlsplit(start['authorization_url']).query);self.transport.nonce=query['nonce'][0]
        from urllib.parse import urlencode
        with urlopen(base+'/auth/callback?'+urlencode({'state':query['state'][0],'client_id':'oaiapp_test','code':'fixture-code'})) as response:
            self.assertEqual(response.status,200);self.assertIn('Agent 设置',response.read().decode())
        self.assertTrue(request('/api/agent')['chatgpt']['sharing'])
        self.assertEqual(request('/api/agent/chatgpt/models',{})['models'][0]['id'],'paper-model')
        request('/api/agent/settings',{'mode':'chatgpt','chatgpt_explain_model':'paper-model','chatgpt_read_model':'paper-model'})
        self.assertEqual(request('/api/agent/test',{})['mode'],'chatgpt')
        self.assertTrue(request('/api/agent/chatgpt/disconnect',{})['remote_revoked'])
        self.assertFalse(request('/api/agent')['chatgpt']['connected'])

    def test_analysis_route_stops_when_chatgpt_account_changes(self):
        self.login('oaiapp_first');service=AgentService(self.tmp.name,auth=self.auth)
        service.save({'mode':'chatgpt','chatgpt_explain_model':'paper-model','chatgpt_read_model':'paper-model'})
        route=service.execution('read')
        self.login('oaiapp_second')
        with patch.object(self.transport,'complete',return_value='unexpected') as complete:
            with self.assertRaises(AgentError):route.complete('Read paper')
            complete.assert_not_called()

    def test_analysis_route_keeps_explicit_api_configuration(self):
        root=Path(self.tmp.name)
        (root/'.env').write_text('SHSF_LLM_API_KEY=old-private-key\nSHSF_LLM_MODEL=model-a\nSHSF_LLM_BASE_URL=https://example.org/v1\n')
        with patch.dict(os.environ,{},clear=True):service=AgentService(root,auth=self.auth)
        route=service.execution('read')
        service.config=AgentConfig(model='other',explain_model='other',read_model='other',api_key='new-private-key')
        opener=Opener(b'{"choices":[{"message":{"content":"report"}}]}')
        with patch('shsf.agent_service.Transport',return_value=Transport(opener)):
            self.assertEqual(route.complete('Read paper'),'report')
        request=opener.requests[0]
        self.assertEqual(request.get_header('Authorization'),'Bearer old-private-key')
        self.assertEqual(json.loads(request.data)['model'],'model-a')
        self.assertNotIn('old-private-key',route.fingerprint())
