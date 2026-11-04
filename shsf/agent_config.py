"""Configuration is read on the server; secrets never enter public status."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import dotenv_values


class AgentError(ValueError):
    pass


def environment(root=None):
    values=dotenv_values(Path(root or Path.cwd())/'.env',interpolate=False)
    return {**{k:v for k,v in values.items() if v is not None},**os.environ}


@dataclass(frozen=True)
class AgentConfig:
    mode: str = 'api'
    base_url: str = 'https://api.openai.com/v1'
    api_key: str = field(default='',repr=False)
    protocol: str = 'chat_completions'
    model: str = ''
    explain_model: str = ''
    read_model: str = ''
    timeout: int = 60
    error: str = ''

    @classmethod
    def load(cls,root=None):
        env=environment(root)
        try:
            mode=env.get('SHSF_AGENT_MODE','api').strip()
            provider=env.get('SHSF_LLM_PROVIDER','openai_compatible').strip()
            protocol=env.get('SHSF_LLM_PROTOCOL','chat_completions').strip()
            base=env.get('SHSF_LLM_BASE_URL','https://api.openai.com/v1').strip().rstrip('/')
            url=urlsplit(base)
            if mode not in ('api','chatgpt') or provider!='openai_compatible' or protocol not in ('chat_completions','responses'):
                raise AgentError('Agent 通道或 API 协议配置无效。')
            if not url.hostname or url.username or url.password or url.query or url.fragment or url.scheme not in ('http','https'):
                raise AgentError('BASE_URL 需要是不含账号、密码或查询参数的 HTTP(S) 基础地址。')
            if url.scheme=='http' and url.hostname not in ('127.0.0.1','localhost','::1'):
                raise AgentError('远程 API 地址需要使用 HTTPS；本机模型可使用 HTTP。')
            key=env.get('SHSF_LLM_API_KEY','').strip()
            if '\n' in key or '\r' in key:raise AgentError('API Key 格式无效。')
            model=env.get('SHSF_LLM_MODEL','').strip()
            explain=env.get('SHSF_AGENT_EXPLAIN_MODEL',model).strip() or model
            read=env.get('SHSF_AGENT_READ_MODEL',model).strip() or model
            if any(len(v)>200 or (v and not re.fullmatch(r'[A-Za-z0-9_.:/-]+',v)) for v in (model,explain,read)):
                raise AgentError('模型名称格式无效。')
            timeout=int(env.get('SHSF_LLM_TIMEOUT','60'))
            if not 5<=timeout<=180:raise AgentError('连接超时需要在 5–180 秒之间。')
            return cls(mode,base,key,protocol,model,explain,read,timeout)
        except (ValueError,TypeError) as exc:
            message=str(exc) if isinstance(exc,AgentError) else 'Agent 配置格式无效，请检查 .env。'
            return cls(error=message)

    def public(self):
        local=urlsplit(self.base_url).hostname in ('127.0.0.1','localhost','::1')
        missing=[]
        if not self.api_key and not local:missing.append('SHSF_LLM_API_KEY')
        if not self.explain_model:missing.append('SHSF_AGENT_EXPLAIN_MODEL / SHSF_LLM_MODEL')
        if not self.read_model:missing.append('SHSF_AGENT_READ_MODEL / SHSF_LLM_MODEL')
        return {'base_url':self.base_url,'protocol':self.protocol,'api_key_configured':bool(self.api_key),
                'explain_model':self.explain_model,'read_model':self.read_model,'timeout':self.timeout,
                'ready':not self.error and not missing,'missing':missing,'error':self.error}
