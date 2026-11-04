"""Official arXiv HTML only. Extract source blocks and stable evidence anchors."""
import fcntl
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError,URLError
from urllib.parse import quote
from urllib.request import Request,build_opener
import uuid

from bs4 import BeautifulSoup
from .agent_config import AgentError
from .agent_transport import NoRedirect
from .models import compact,json_text,normalize_id

PARSER='arxiv-html-blocks-v1'
MAX_CHARS=250_000


def parse_html(raw,url):
    soup=BeautifulSoup(raw,'html.parser')
    article=soup.select_one('article.ltx_document, .ltx_document')
    if not article:raise AgentError('该页面没有可识别的 arXiv HTML 正文，无法生成全文精读。')
    for tag in article.select('script,style,nav,footer,aside,.ltx_abstract,.ltx_bibliography,.ltx_authors,.ltx_dates,.ltx_acknowledgements'):
        tag.decompose()
    for math in article.find_all('math'):
        annotation=math.find('annotation',attrs={'encoding':'application/x-tex'})
        text=math.get('alttext') or (annotation.get_text() if annotation else math.get_text(' ',strip=True))
        math.replace_with(' '+text+' ')
    sources=[];heading='正文';body_count=0
    for node in article.find_all(['h2','h3','h4','p','figcaption','table','div']):
        classes=node.get('class',[])
        if node.name=='div' and 'ltx_equation' not in classes and 'ltx_equationgroup' not in classes:continue
        if node.name.startswith('h'):
            heading=compact(node.get_text(' ',strip=True))[:240];continue
        if node.find_parent(['table','figcaption']) or (node.name=='p' and node.find_parent('figure')):continue
        if node.find_parent(class_='ltx_equationgroup'):continue
        text=compact(node.get_text(' ',strip=True))
        if len(text)<20:continue
        anchor=node.get('id')
        if not anchor:
            parent=node.find_parent(attrs={'id':True});anchor=parent.get('id') if parent else None
        if anchor and len(anchor)>500:anchor=None
        label=heading
        if node.name=='figcaption':label=heading+' · 图注（未分析图像）'
        if node.name=='table':label=heading+' · 表格/公式'
        if node.name=='p':body_count+=1
        for offset in range(0,len(text),6000):
            sources.append({'id':f'P{len(sources)+1}','label':label,'text':text[offset:offset+6000],
                            'url':url+('#'+quote(anchor,safe='') if anchor else '')})
    length=sum(len(s['text']) for s in sources)
    if body_count<2 or length<500:raise AgentError('HTML 正文不足，无法将此页面作为全文精读依据。')
    if length>MAX_CHARS:raise AgentError('论文正文超出当前精读长度上限，未截断或生成不完整的全文报告。')
    return {'parser':PARSER,'url':url,'sources':sources,'characters':length,
            'visual_analysis':False,'source_hash':hashlib.sha256(raw).hexdigest()}


class PaperHTML:
    def __init__(self,database,data_dir,opener=None):
        self.db=database;self.directory=Path(data_dir)/'contents';self.opener=opener or build_opener(NoRedirect())

    def get(self,paper):
        aid,_=normalize_id(paper['arxiv_id']);version=paper.get('version')
        if not isinstance(version,int) or version<1:raise AgentError('论文缺少确定的 arXiv 版本，请先更新元数据。')
        version_key=f'v{version}';url=f'https://arxiv.org/html/{aid}{version_key}'
        with self.db.transaction() as tx:
            cached=tx.execute('SELECT * FROM paper_contents WHERE arxiv_id=? AND version_key=?',(aid,version_key)).fetchone()
        if cached and cached['status']=='ready' and cached['parser_version']==PARSER and cached['parsed_path']:
            path=Path(cached['parsed_path'])
            if path.resolve().is_relative_to(self.directory.resolve()) and path.exists():
                data=json.loads(path.read_text())
                if data.get('parser')==PARSER:return data
        self.directory.mkdir(parents=True,exist_ok=True)
        # A shared lane across web requests, with no credential-bearing requests.
        with (self.directory/'arxiv-html.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);lock.seek(0)
            previous=float(lock.read().strip() or 0)
            time.sleep(max(0,3.1-(time.time()-previous)))
            lock.seek(0);lock.truncate();lock.write(str(time.time()));lock.flush()
            try:
                request=Request(url,headers={'User-Agent':'SHSF/0.1 (personal paper reading)','Accept':'text/html'})
                with self.opener.open(request,timeout=45) as response:
                    if 'text/html' not in response.headers.get('Content-Type',''):raise AgentError('官方地址未返回 HTML 正文。')
                    raw=response.read(32_000_001)
                if len(raw)>32_000_000:raise AgentError('官方 HTML 超出当前下载大小限制。')
            except HTTPError as exc:
                message='该版本尚无可用的官方 arXiv HTML。可先使用摘要解读。' if exc.code in (404,410) else f'官方 HTML 暂不可访问（HTTP {exc.code}），请稍后重试。'
                raise AgentError(message) from None
            except (URLError,TimeoutError,OSError):raise AgentError('获取官方 arXiv HTML 失败，请检查网络后重试。') from None
        document=parse_html(raw,url)
        prefix=hashlib.sha256((aid+version_key+document['source_hash']+PARSER).encode()).hexdigest()
        paths=[self.directory/(prefix+'.html'),self.directory/(prefix+'.json')]
        for path,data in zip(paths,[raw,json_text(document).encode()]):
            temp=path.with_name(uuid.uuid4().hex+'.tmp');temp.write_bytes(data);temp.replace(path)
        with self.db.transaction() as tx:
            tx.execute('''INSERT INTO paper_contents(arxiv_id,version_key,html_url,status,raw_html_path,parsed_path,parser_version,content_hash,last_error)
                VALUES (?,?,?,'ready',?,?,?,?,NULL) ON CONFLICT(arxiv_id,version_key) DO UPDATE SET html_url=excluded.html_url,
                status='ready',raw_html_path=excluded.raw_html_path,parsed_path=excluded.parsed_path,
                parser_version=excluded.parser_version,content_hash=excluded.content_hash,last_error=NULL''',
                (aid,version_key,url,str(paths[0].resolve()),str(paths[1].resolve()),PARSER,document['source_hash']))
        return document
