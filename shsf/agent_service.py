"""Agent configuration and explicit connection checks; no automatic billing fallback."""
from pathlib import Path
import threading
from .agent_config import AgentConfig,AgentError
from .agent_transport import Transport
from .chatgpt_auth import ChatGPTAuth,RESOURCE


class AgentService:
    def __init__(self,root=None,directory=None,auth=None,profile_path=None):
        self.root=Path(root or Path.cwd())
        self.profile_path=profile_path
        self.config=AgentConfig.load(self.root)
        self.auth=auth or ChatGPTAuth(directory or self.root/'.data/pipeline/agent')
        self.lock=threading.RLock()
        self.last_test=None

    def settings(self):
        with self.auth.store.transaction() as data:return dict(data.get('settings',{}))

    def status(self):
        settings=self.settings()
        return {'mode':settings.get('mode',self.config.mode),'api':self.config.public(),
                'chatgpt':self.auth.status(),'chatgpt_explain_model':settings.get('chatgpt_explain_model',''),
                'chatgpt_read_model':settings.get('chatgpt_read_model',''),
                'last_test':self.last_test,'automatic_fallback':False}

    def reload(self):
        with self.lock:
            self.config=AgentConfig.load(self.root);self.last_test=None
        return self.status()

    def save(self,body):
        mode=body.get('mode')
        if mode not in ('api','chatgpt'):raise AgentError('请选择 API 或 ChatGPT 通道。')
        choices={}
        if mode=='chatgpt':
            for task in ('explain','read'):
                value=body.get('chatgpt_'+task+'_model','')
                if not isinstance(value,str) or len(value)>200:raise AgentError('模型选择无效。')
                choices['chatgpt_'+task+'_model']=value
            if any(choices.values()):
                models={x['id'] for x in self.auth.models()}
                if any(value and value not in models for value in choices.values()):raise AgentError('选中的模型不在当前账户模型列表中。')
        with self.auth.store.transaction() as data:
            data.setdefault('settings',{}).update(mode=mode,**choices)
        self.last_test=None
        return self.status()

    def models(self):return {'models':self.auth.models()}

    def test(self,task='explain'):
        if task not in ('explain','read'):raise AgentError('请选择推荐解读或单篇精读。')
        with self.lock:
            self.last_test=None
            config=self.config;settings=self.settings();mode=settings.get('mode',config.mode)
            prompt='Connection test only. Reply with the single word SHSF.'
            if mode=='api':
                info=config.public()
                if not info['ready']:raise AgentError(config.error or 'API 配置不完整，请先在 .env 设置密钥和模型。')
                model=config.explain_model if task=='explain' else config.read_model
                text=Transport(timeout=config.timeout).complete(config.base_url,model,prompt,config.api_key or None,config.protocol)
            else:
                model=settings.get('chatgpt_'+task+'_model','')
                if not model:raise AgentError('请先读取账户模型列表并保存模型选择。')
                models={x['id'] for x in self.auth.models()}
                if model not in models:raise AgentError('当前账户不再提供所选模型，请重新选择。')
                token,_=self.auth.access()
                text=self.auth.transport.complete(RESOURCE,model,prompt,token,'responses',subscription=True)
            self.last_test={'ok':True,'mode':mode,'task':task,'model':model,'reply':text[:200]}
            return self.last_test

    def execution(self,task):
        """Capture the chosen provider/model for a task; never persist secrets."""
        if task not in ('explain','read'):raise AgentError('任务类型无效。')
        with self.lock:
            config=self.config;settings=self.settings();mode=settings.get('mode',config.mode)
            if mode=='api':
                if not config.public()['ready']:raise AgentError(config.error or '请先在 Agent 设置中配置 API 密钥和模型，或连接 ChatGPT。')
                model=config.explain_model if task=='explain' else config.read_model
                return ExecutionRoute(mode,model,config.base_url,config.protocol,config.timeout,config.api_key)
            status=self.auth.status();model=settings.get('chatgpt_'+task+'_model','')
            if not status['connected']:raise AgentError('ChatGPT 尚未连接，请在 Agent 设置中登录。')
            if not status['sharing']:raise AgentError('ChatGPT 已连接，但尚未授权套餐使用，请重新登录并授权。')
            if not model:
                label='单篇精读' if task=='read' else '推荐解读'
                raise AgentError(f'ChatGPT 已连接并授权，但尚未保存{label}模型。请在 Agent 设置中读取模型列表、选择{label}模型并点击“保存模型选择”。')
            return ExecutionRoute(mode,model,RESOURCE,'responses',config.timeout,auth=self.auth,client_id=status['active'])


class ExecutionRoute:
    def __init__(self,mode,model,base_url,protocol,timeout,api_key=None,auth=None,client_id=None):
        self.mode,self.model,self.base_url,self.protocol,self.timeout=mode,model,base_url,protocol,timeout
        self._api_key,self._auth,self._client_id=api_key,auth,client_id
        self._validated=False

    def fingerprint(self):
        from .models import digest,json_text
        return digest(json_text([self.mode,self.model,self.base_url,self.protocol,self._client_id]))

    def complete(self,prompt):
        if self.mode=='api':
            return Transport(timeout=self.timeout).complete(self.base_url,self.model,prompt,self._api_key or None,self.protocol)
        # An account change stops this task instead of spending another account's allowance.
        token,client=self._auth.access()
        if client!=self._client_id:raise AgentError('ChatGPT 账户已切换，本次任务已停止，请重新生成。')
        if not self._validated:
            if self.model not in {m['id'] for m in self._auth.models()}:raise AgentError('当前账户不支持所选模型，请重新配置。')
            self._validated=True
        token,client=self._auth.access()
        if client!=self._client_id:raise AgentError('ChatGPT 账户已切换，本次任务已停止。')
        return self._auth.transport.complete(RESOURCE,self.model,prompt,token,'responses',subscription=True)
