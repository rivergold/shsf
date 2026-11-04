"""Bounded HTTP transport with no redirects and no raw provider error exposure."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from .agent_config import AgentError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Transport:
    def __init__(self,opener=None,timeout=60):
        self.opener=opener or build_opener(NoRedirect());self.timeout=timeout

    def open(self,url,body=None,token=None,form=False):
        headers={'Accept':'application/json','User-Agent':'SHSF/0.1'}
        if token:headers['Authorization']='Bearer '+token
        data=None
        if body is not None:
            data=urlencode(body).encode() if form else json.dumps(body).encode()
            headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
        try:return self.opener.open(Request(url,data=data,headers=headers),timeout=self.timeout)
        except HTTPError as exc:
            messages={401:'认证失败，请检查密钥或重新登录。',403:'账户或工作区未授权此操作。',429:'额度或请求频率受限，请稍后重试。'}
            raise AgentError(messages.get(exc.code,f'服务返回 HTTP {exc.code}，请检查通道、地址和模型配置。')) from None
        except (URLError,TimeoutError,OSError):raise AgentError('无法连接模型服务，请检查地址、网络或超时配置。') from None

    def json(self,url,body=None,token=None,form=False,empty=False):
        with self.open(url,body,token,form) as response:
            raw=response.read(4_000_001)
        if empty and not raw:return {}
        if len(raw)>4_000_000:raise AgentError('模型服务响应过大。')
        try:return json.loads(raw)
        except (ValueError,TypeError,UnicodeDecodeError):raise AgentError('模型服务返回了无法解析的响应。') from None

    def complete(self,base,model,prompt,token=None,protocol='responses',subscription=False):
        if protocol=='chat_completions':
            result=self.json(base+'/chat/completions',{'model':model,'messages':[{'role':'user','content':prompt}]},token)
            try:text=result['choices'][0]['message']['content']
            except (KeyError,TypeError,IndexError):raise AgentError('服务未返回有效文本。') from None
        else:
            body={'model':model,'input':[{'role':'user','content':prompt}],'store':False,'stream':True}
            parts=[];completed=False;consumed=0;frame=[]
            try:
                with self.open(base+'/responses',body,token) as response:
                    while True:
                        raw=response.readline(1_000_001)
                        if not raw:break
                        consumed+=len(raw)
                        if len(raw)>1_000_000 or consumed>16_000_000:raise AgentError('模型输出超过当前长度限制。')
                        line=raw.decode('utf-8').strip()
                        if line.startswith('data:'):frame.append(line[5:].strip())
                        if line or not frame:continue
                        payload='\n'.join(frame);frame=[]
                        if payload=='[DONE]':continue
                        event=json.loads(payload);kind=event.get('type')
                        if kind=='response.output_text.delta':parts.append(event.get('delta',''))
                        elif kind in ('response.failed','response.incomplete','error'):
                            raise AgentError('模型请求未完成，可能存在权限或额度限制；未切换计费通道。')
                        elif kind=='response.completed':
                            completed=True
                            if not parts:
                                for output in event.get('response',{}).get('output',[]):
                                    for content in output.get('content',[]):
                                        if content.get('type')=='output_text':parts.append(content.get('text',''))
                            break
            except (ValueError,UnicodeDecodeError,TimeoutError,OSError) as exc:
                if isinstance(exc,AgentError):raise
                raise AgentError('模型响应流中断或格式无效，请重试。') from None
            if not completed:raise AgentError('响应流结束但未确认完成；请重试。')
            text=''.join(parts)
        if not isinstance(text,str) or not text.strip():raise AgentError('模型未返回有效文本。')
        return text
