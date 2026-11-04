"""Official Sign in with ChatGPT for local apps: PKCE, OIDC, protected profiles."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import urlencode,urlsplit,parse_qs
import uuid

import jwt
from .agent_config import AgentError
from .agent_transport import Transport

AUTH='https://auth.openai.com'
RESOURCE='https://api.openai.com/v1'
TOKEN=AUTH+'/api/accounts/oauth/token'
SCOPE='openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'


class PrivateStore:
    def __init__(self,directory):self.directory=Path(directory)

    @contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.directory.is_symlink():raise AgentError('凭据目录不能是符号链接。')
        self.directory.chmod(0o700)
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(self.directory/'auth.lock',flags,0o600)
        with os.fdopen(fd,'a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=self.directory/'chatgpt.json'
            if path.is_symlink():raise AgentError('凭据文件不能是符号链接。')
            if path.exists():
                if path.stat().st_mode & 0o077:raise AgentError('凭据文件权限需要为 0600。')
                try:data=json.loads(path.read_text())
                except (ValueError,OSError):raise AgentError('本机凭据文件无法读取。') from None
            else:data={'host_id':'urn:uuid:'+str(uuid.uuid4()),'profiles':{},'active':None}
            yield data
            fd,name=tempfile.mkstemp(dir=self.directory,prefix='.chatgpt-')
            try:
                with os.fdopen(fd,'w') as out:
                    json.dump(data,out);out.flush();os.fsync(out.fileno())
                os.replace(name,path)
            finally:
                if os.path.exists(name):os.unlink(name)


class ChatGPTAuth:
    def __init__(self,directory,transport=None):
        self.store=PrivateStore(directory);self.transport=transport or Transport(timeout=25)
        self.lock=threading.RLock();self.pending=None;self.last_error='';self.callback_uri=None
        self.catalogs={}

    def status(self):
        with self.lock,self.store.transaction() as data:
            if self.pending and self.pending['deadline']<time.time():
                self.pending=None;self.last_error='登录已超时，请重新发起。'
            profiles=[]
            for client,p in data['profiles'].items():
                profiles.append({'id':client,'label':p.get('email') or 'ChatGPT · '+client[-8:],
                                 'connected':bool(p.get('access_token')),'sharing':bool(p.get('access_token') and 'chatgpt.tokens.use.direct' in p.get('scopes',[]))})
            active=data['profiles'].get(data['active'],{})
            return {'profiles':profiles,'active':data['active'],'connected':bool(active.get('access_token')),
                    'sharing':bool(active.get('access_token') and 'chatgpt.tokens.use.direct' in active.get('scopes',[])),
                    'pending':bool(self.pending),'error':self.last_error}

    def start(self,profile_id=None):
        if not self.callback_uri:raise AgentError('登录回调尚未启动。')
        with self.lock,self.store.transaction() as data:
            if profile_id and profile_id not in data['profiles']:raise AgentError('账户不存在。')
            verifier=secrets.token_urlsafe(48)
            state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32)
            self.pending={'state':state,'nonce':nonce,'verifier':verifier,'client_id':profile_id,
                          'deadline':time.time()+600,'redirect_uri':self.callback_uri}
            params={'client_id':profile_id or 'dynamic_agent_client','ext_agent_host_id':data['host_id'],
                    'response_type':'code','redirect_uri':self.callback_uri,'scope':SCOPE,'resource':RESOURCE,
                    'state':state,'nonce':nonce,'code_challenge_method':'S256',
                    'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
            if not profile_id:params['agent_name_hint']='SHSF'
            self.last_error=''
            return {'authorization_url':AUTH+'/api/accounts/authorize?'+urlencode(params),'expires_in':600}

    def discovery(self):
        meta=self.transport.json(AUTH+'/.well-known/openid-configuration')
        if meta.get('issuer')!=AUTH:raise AgentError('身份服务信息校验失败。')
        return meta

    def verify_identity(self,token,client,nonce=None):
        try:
            meta=self.discovery();url=urlsplit(meta['jwks_uri'])
            if url.scheme!='https' or url.hostname!='auth.openai.com' or url.username:
                raise AgentError('身份服务签名地址无效。')
            jwks=self.transport.json(meta['jwks_uri'])
            header=jwt.get_unverified_header(token)
            key=next(k for k in jwks['keys'] if k.get('kid')==header.get('kid'))
            required=['iss','aud','exp','iat','sub']+(['nonce'] if nonce else [])
            claims=jwt.decode(token,jwt.PyJWK.from_dict(key).key,algorithms=['RS256','ES256'],
                              audience=client,issuer=AUTH,leeway=30,options={'require':required})
            if nonce and not hmac.compare_digest(str(claims['nonce']),nonce):raise AgentError('登录 nonce 校验失败。')
            if not claims['sub']:raise AgentError('登录身份缺少账户标识。')
            if isinstance(claims['aud'],list) and len(claims['aud'])>1 and claims.get('azp')!=client:
                raise AgentError('登录客户端校验失败。')
            return claims
        except (jwt.PyJWTError,KeyError,ValueError,TypeError,StopIteration) as exc:
            if isinstance(exc,AgentError):raise
            raise AgentError('登录身份签名、有效期或账户校验失败。') from None

    @staticmethod
    def credentials(result,previous=None):
        previous=previous or {}
        if not isinstance(result.get('access_token'),str) or not result['access_token'] or str(result.get('token_type','')).lower()!='bearer':
            raise AgentError('登录服务没有返回有效凭据。')
        try:lifetime=int(result['expires_in'])
        except (KeyError,ValueError,TypeError):raise AgentError('登录凭据缺少有效期。') from None
        if not 0<lifetime<=86400:raise AgentError('登录凭据有效期无效。')
        scopes=result.get('scope')
        if scopes is not None and not isinstance(scopes,str):raise AgentError('授权范围格式无效。')
        return {**previous,'access_token':result['access_token'],
                'refresh_token':result.get('refresh_token',previous.get('refresh_token')),
                'id_token':result.get('id_token',previous.get('id_token')),
                'scopes':scopes.split() if scopes is not None else previous.get('scopes',[]),
                'expires_at':time.time()+lifetime}

    def complete_remote(self, callback_url):
        """Relay a pasted loopback callback without fetching the supplied URL."""
        if not isinstance(callback_url, str) or not 1 <= len(callback_url) <= 8192:
            raise AgentError('请粘贴完整的授权回调地址。')
        try:
            url = urlsplit(callback_url.strip())
            expected = urlsplit(self.callback_uri or '')
            if not expected.netloc or (url.scheme, url.netloc, url.path) != (expected.scheme, expected.netloc, expected.path) or url.fragment:
                raise ValueError()
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=20)
            if not query.get('state', [''])[0] or any(len(values) != 1 for values in query.values()):
                raise ValueError()
            if not query.get('code', [''])[0] and not query.get('error', [''])[0]:
                raise ValueError()
        except ValueError:
            raise AgentError('回调地址格式不正确，请复制浏览器地址栏中完整的 127.0.0.1 授权返回地址。') from None
        # The existing callback enforces expiry, state, PKCE, nonce, signature,
        # audience, and one-time use; token exchange retains the ORIGINAL URI.
        return self.callback(query)

    def callback(self,query):
        with self.lock:
            pending=self.pending
            value=lambda key:query.get(key,[''])[0]
            if not pending or pending['deadline']<time.time() or not hmac.compare_digest(value('state'),pending['state']):
                raise AgentError('登录请求已过期或 state 不匹配，请从设置页重新登录。')
            self.pending=None
            try:
                if value('error'):raise AgentError('登录未获授权，请返回设置重新连接。')
                client=value('client_id') or pending['client_id']
                if not client or client=='dynamic_agent_client' or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',client):
                    raise AgentError('登录回调没有返回有效客户端 ID。')
                if pending['client_id'] and pending['client_id']!=client:raise AgentError('登录回调与选中的账户不一致。')
                if not value('code'):raise AgentError('登录回调缺少授权码。')
                # Retain the issued registration even if the one-time code
                # exchange fails; never overwrite an existing verified identity.
                with self.store.transaction() as data:
                    data['profiles'].setdefault(client,{})
                result=self.transport.json(TOKEN,{'grant_type':'authorization_code','client_id':client,
                    'code':value('code'),'code_verifier':pending['verifier'],'redirect_uri':pending['redirect_uri'],
                    'resource':RESOURCE},form=True)
                claims=self.verify_identity(result.get('id_token',''),client,pending['nonce'])
                with self.store.transaction() as data:
                    previous=data['profiles'].get(client,{})
                    if previous.get('subject') and previous['subject']!=claims['sub']:
                        raise AgentError('返回的账户身份与原连接不一致。')
                    profile=self.credentials(result)
                    profile.update(client_id=client,subject=claims['sub'],issuer=AUTH,email=claims.get('email',''))
                    data['profiles'][client]=profile;data['active']=client
                self.catalogs.clear();self.last_error=''
            except AgentError as exc:
                self.last_error=str(exc);raise
        return self.status()

    def select(self,profile_id):
        with self.lock,self.store.transaction() as data:
            if profile_id not in data['profiles']:raise AgentError('账户不存在。')
            data['active']=profile_id;self.catalogs.clear()
        return self.status()

    def access(self):
        # Cross-process file locking serializes rotating refresh tokens.
        with self.lock,self.store.transaction() as data:
            client=data['active'];p=data['profiles'].get(client,{})
            if not p.get('access_token'):raise AgentError('请先连接 ChatGPT。')
            if p.get('expires_at',0)<=time.time()+60:
                if not p.get('refresh_token'):raise AgentError('登录已过期，请重新连接。')
                result=self.transport.json(TOKEN,{'grant_type':'refresh_token','client_id':client,
                    'refresh_token':p['refresh_token'],'resource':RESOURCE},form=True)
                refreshed=self.credentials(result,p)
                if result.get('id_token'):
                    claims=self.verify_identity(result['id_token'],client)
                    if claims['sub']!=p['subject']:raise AgentError('刷新后的身份不一致，请重新连接。')
                data['profiles'][client]=p=refreshed
            if 'chatgpt.tokens.use.direct' not in p.get('scopes',[]):raise AgentError('该账户只完成身份登录，尚未授权使用 ChatGPT 套餐。')
            return p['access_token'],client

    def models(self):
        token,client=self.access()
        result=self.transport.json(RESOURCE+'/models',token=token)
        models=[{'id':x['slug'],'name':x.get('display_name') or x['slug']} for x in result.get('models',[])
                if x.get('visibility')=='list' and isinstance(x.get('slug'),str)]
        if not models:raise AgentError('账户未返回可用模型，请检查套餐或工作区权限。')
        with self.lock:self.catalogs[client]=models
        return models

    def disconnect(self,profile_id=None):
        revoked=True
        with self.lock:
            self.pending=None;self.last_error=''
            with self.store.transaction() as data:
                client=profile_id or data['active'];p=data['profiles'].get(client,{})
                refresh=p.get('refresh_token')
                if refresh:
                    try:
                        endpoint=self.discovery().get('revocation_endpoint','')
                        url=urlsplit(endpoint)
                        if url.scheme!='https' or url.hostname!='auth.openai.com':raise AgentError('撤销地址无效。')
                        for attempt in range(2):
                            try:
                                self.transport.json(endpoint,{'token':refresh,'token_type_hint':'refresh_token','client_id':client},form=True,empty=True)
                                break
                            except AgentError:
                                if attempt:raise
                                time.sleep(.25)
                    except AgentError:revoked=False
                if client in data['profiles']:
                    data['profiles'][client]={k:v for k,v in p.items() if k in ('client_id','subject','issuer','email')}
                if data['active']==client:data['active']=None
            self.catalogs.clear()
        return {'remote_revoked':revoked,'message':'已退出并清除本机凭据。' if revoked else '本机凭据已清除，未能确认远程撤销。请在 ChatGPT 设置中断开 SHSF。'}
