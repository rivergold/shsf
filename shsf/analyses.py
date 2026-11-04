"""Persisted, explicit, cached research analysis jobs. One model task at a time."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

from .agent_config import AgentError,environment
from .analysis_reports import PROMPT_VERSION,NOTES_VERSION,chunks,notes_prompt,report_prompt,validate_notes,validate_report
from .models import digest,json_text,normalize_id,utcnow
from .paper_html import PaperHTML,PARSER
from .translations import cached_abstract


class Analyses:
    def __init__(self,database,agent,owner='local',html=None):
        self.db,self.agent,self.owner=database,agent,owner
        self.html=html or PaperHTML(database,agent.auth.store.directory.parent)
        self.lock=threading.RLock();self.pool=None

    def recover(self):
        # This application runs one local server. Never replay a paid request
        # automatically after a restart with an uncertain prior outcome.
        with self.db.transaction() as tx:
            tx.execute("UPDATE analysis_jobs SET status='failed',stage=?,error=?,updated_at=? WHERE owner_key=? AND status IN ('queued','running')",
                       ('任务中断','服务已重启，任务未完成。已完成的分段笔记保留，请手动重试。',utcnow(),self.owner))

    def close(self):
        if self.pool:self.pool.shutdown(wait=False,cancel_futures=True)

    @staticmethod
    def aid(value):
        if not isinstance(value,str):raise AgentError('论文 ID 无效。')
        try:return normalize_id(value)[0]
        except ValueError:raise AgentError('论文 ID 无效。') from None

    def _paper(self,tx,aid):
        row=tx.execute('SELECT * FROM papers WHERE arxiv_id=? AND metadata_deleted=0',(aid,)).fetchone()
        if not row:raise AgentError('论文不在当前可用的本地库中。')
        return dict(row)

    def _public(self,row):
        if not row:return None
        row=dict(row)
        result={k:row[k] for k in ('job_id','arxiv_id','kind','status','stage','progress','channel','model','error','created_at','updated_at','paper_input_hash')}
        result['report']=json.loads(row['report_json']) if row['report_json'] else None
        return result

    def get(self,job_id):
        if not isinstance(job_id,str) or len(job_id)!=64:raise AgentError('任务 ID 无效。')
        with self.db.transaction() as tx:
            row=tx.execute('SELECT * FROM analysis_jobs WHERE job_id=? AND owner_key=?',(job_id,self.owner)).fetchone()
        if not row:raise AgentError('任务不存在。')
        return self._public(row)

    def records(self,query='',status='all',favorites=False,page=1):
        if not isinstance(query,str) or len(query)>300 or status not in ('all','succeeded','failed') or not isinstance(favorites,bool) or type(page) is not int or not 1<=page<=100000:
            raise AgentError('精读记录筛选参数无效。')
        # Rank before filtering so an older success cannot hide a newer failure.
        cte="""WITH ranked AS (
            SELECT j.job_id,j.arxiv_id,j.status,j.stage,j.progress,j.error,j.created_at,j.updated_at,
                p.title,CASE WHEN f.arxiv_id IS NULL THEN 0 ELSE 1 END AS favorite,
                ROW_NUMBER() OVER (PARTITION BY j.arxiv_id ORDER BY j.updated_at DESC,j.created_at DESC,j.job_id DESC) AS rn
            FROM analysis_jobs j JOIN papers p ON p.arxiv_id=j.arxiv_id
            LEFT JOIN reading_favorites f ON f.owner_key=j.owner_key AND f.arxiv_id=j.arxiv_id
            WHERE j.owner_key=? AND j.kind='read' AND p.metadata_deleted=0
        ) """
        where='rn=1';args=[self.owner]
        if query.strip():
            escaped=query.strip().lower().replace('!','!!').replace('%','!%').replace('_','!_')
            where+=" AND LOWER(title) LIKE ? ESCAPE '!'";args.append('%'+escaped+'%')
        if favorites:where+=' AND favorite=1'
        order=' ORDER BY updated_at DESC,job_id DESC'
        with self.db.transaction() as tx:
            active=[dict(r) for r in tx.execute(cte+"SELECT * FROM ranked WHERE "+where+" AND status IN ('queued','running')"+order,args).fetchall()]
            history=where+" AND status IN ('succeeded','failed')"
            history_args=list(args)
            if status!='all':history+=' AND status=?';history_args.append(status)
            total=tx.execute(cte+'SELECT COUNT(*) AS total FROM ranked WHERE '+history,history_args).fetchone()['total']
            pages=max(1,(total+19)//20);page=min(page,pages)
            items=[dict(r) for r in tx.execute(cte+'SELECT * FROM ranked WHERE '+history+order+' LIMIT 20 OFFSET ?',history_args+[(page-1)*20]).fetchall()]
        for item in active+items:
            item.pop('rn',None);item['favorite']=bool(item['favorite'])
        return {'active':active,'items':items,'total':total,'page':page,'pages':pages,'page_size':20}

    def favorite(self,arxiv_id,value):
        aid=self.aid(arxiv_id)
        if not isinstance(value,bool):raise AgentError('收藏状态无效。')
        with self.db.transaction() as tx:
            self._paper(tx,aid)
            if not tx.execute("SELECT job_id FROM analysis_jobs WHERE owner_key=? AND arxiv_id=? AND kind='read' LIMIT 1",(self.owner,aid)).fetchone():
                raise AgentError('该论文没有精读记录。')
            if value:
                tx.execute('INSERT INTO reading_favorites(owner_key,arxiv_id,created_at) VALUES (?,?,?) ON CONFLICT(owner_key,arxiv_id) DO NOTHING',(self.owner,aid,utcnow()))
            else:tx.execute('DELETE FROM reading_favorites WHERE owner_key=? AND arxiv_id=?',(self.owner,aid))
        return {'arxiv_id':aid,'favorite':value}

    def paper(self,arxiv_id):
        aid=self.aid(arxiv_id)
        with self.db.transaction() as tx:
            p=self._paper(tx,aid)
            jobs=tx.execute('SELECT * FROM analysis_jobs WHERE owner_key=? AND arxiv_id=? ORDER BY updated_at DESC,created_at DESC,job_id DESC LIMIT 12',(self.owner,aid)).fetchall()
            saved=tx.execute("SELECT * FROM analysis_jobs WHERE owner_key=? AND arxiv_id=? AND kind='read' AND status='succeeded' AND report_json IS NOT NULL ORDER BY updated_at DESC,job_id DESC LIMIT 1",(self.owner,aid)).fetchone()
            paper={k:p[k] for k in ('arxiv_id','version','title','abstract','published_at','input_hash')}
            paper.update(cached_abstract(tx,self.owner,p))
        return {'paper':paper,
                'saved_read':self._public(saved),'authors':json.loads(p['authors_json']),'jobs':[self._public(j) for j in jobs]}

    def _context(self,tx,paper,batch_id,kind):
        profile_path=Path(getattr(self.agent,'profile_path',None) or environment(self.agent.root).get('SHSF_PROFILE','config/robotics.json'))
        if not profile_path.is_absolute():profile_path=self.agent.root/profile_path
        try:profile=json.loads(profile_path.read_text())
        except (OSError,ValueError):raise AgentError('研究画像配置无法读取，请检查 SHSF_PROFILE。') from None
        if not isinstance(profile,dict):raise AgentError('研究画像需要是 JSON 对象。')
        profile={k:profile.get(k) for k in ('name','description','topics','quality_preferences','preferred_venues')}
        if len(json_text(profile))>20000:raise AgentError('研究画像过长，请缩小配置内容。')
        if batch_id:
            row=tx.execute('''SELECT i.snapshot_json FROM recommendation_items i JOIN recommendation_batches b ON b.batch_id=i.batch_id
                WHERE b.owner_key=? AND b.batch_id=? AND i.arxiv_id=?''',(self.owner,batch_id,paper['arxiv_id'])).fetchone()
        else:
            row=tx.execute('''SELECT i.snapshot_json FROM recommendation_items i JOIN recommendation_batches b ON b.batch_id=i.batch_id
                WHERE b.owner_key=? AND i.arxiv_id=? ORDER BY b.created_at DESC LIMIT 1''',(self.owner,paper['arxiv_id'])).fetchone()
        if kind=='explain' and not row:raise AgentError('该论文没有推荐记录，请先从每日推荐中选择论文。')
        recommendation=json.loads(row['snapshot_json']) if row else {}
        sources=[{'id':'A1','label':'目标论文标题与摘要','url':'https://arxiv.org/abs/'+paper['arxiv_id'],
                  'text':paper['title']+'\n'+paper['abstract']}]
        related=[]
        for index,reason in enumerate(recommendation.get('reasons',[])[:3],1):
            seed=tx.execute('SELECT title,abstract,input_hash FROM papers WHERE arxiv_id=? AND metadata_deleted=0',(reason['arxiv_id'],)).fetchone()
            if not seed:continue
            source={'id':'S'+str(index),'label':'相近种子：'+seed['title'],'url':'https://arxiv.org/abs/'+reason['arxiv_id'],
                    'text':seed['title']+'\n'+seed['abstract']}
            sources.append(source)
            related.append({'arxiv_id':reason['arxiv_id'],'title':seed['title'],'similarity':reason['similarity'],'input_hash':seed['input_hash']})
        confirmed=[json.loads(r['metadata_snapshot_json']) for r in tx.execute('SELECT metadata_snapshot_json FROM confirmed_seeds WHERE owner_key=? ORDER BY seed_id',(self.owner,)).fetchall()]
        context={'paper':{k:paper[k] for k in ('arxiv_id','version','title','abstract','input_hash')},'profile':profile,
                 'confirmed_seed_titles':[s['title'][:200] for s in confirmed],'related_seeds':related,
                 'similarity_is_not_probability':True}
        if len(json_text(context))+len(json_text(sources))>50000:raise AgentError('输入资料过长，当前任务未发送到模型。')
        return context,sources

    def start(self,arxiv_id,kind,batch_id=None,retry=False):
        aid=self.aid(arxiv_id)
        if kind not in ('explain','read') or not isinstance(retry,bool):raise AgentError('任务类型或重试参数无效。')
        if batch_id is not None and (not isinstance(batch_id,str) or len(batch_id)!=64):raise AgentError('推荐批次 ID 无效。')
        route=self.agent.execution(kind)
        with self.lock,self.db.transaction(immediate=True) as tx:
            tx.execute('SELECT owner_key FROM seed_workspaces WHERE owner_key=?'+(' FOR UPDATE' if tx.postgres else ''),(self.owner,)).fetchone()
            paper=self._paper(tx,aid);context,sources=self._context(tx,paper,batch_id,kind)
            cache=digest(json_text([kind,paper['input_hash'],paper['version'],PROMPT_VERSION,PARSER,route.fingerprint(),context,sources]))
            job_id=digest(json_text([self.owner,cache]));stamp=utcnow()
            row=tx.execute('SELECT * FROM analysis_jobs WHERE owner_key=? AND cache_key=?',(self.owner,cache)).fetchone()
            if row and (row['status']!='failed' or not retry):return self._public(row)
            request={'paper':paper,'context':context,'sources':sources}
            if row:
                tx.execute("UPDATE analysis_jobs SET status='queued',stage=?,progress=0,error=NULL,updated_at=? WHERE job_id=?",('等待生成',stamp,job_id))
            else:
                tx.execute('''INSERT INTO analysis_jobs(job_id,owner_key,arxiv_id,kind,cache_key,paper_input_hash,status,stage,progress,channel,model,request_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'queued',?,0,?,?,?,?,?)''',
                    (job_id,self.owner,aid,kind,cache,paper['input_hash'],'等待生成',route.mode,route.model,json_text(request),stamp,stamp))
        with self.lock:
            if self.pool is None:self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='shsf-reading')
            self.pool.submit(self._run,job_id,route)
        return self.get(job_id)

    def _update(self,job_id,**values):
        values['updated_at']=utcnow()
        with self.db.transaction() as tx:
            tx.execute('UPDATE analysis_jobs SET '+','.join(k+'=?' for k in values)+' WHERE job_id=? AND owner_key=?',list(values.values())+[job_id,self.owner])

    def _run(self,job_id,route):
        try:
            self._update(job_id,status='running',stage='准备资料',progress=5)
            with self.db.transaction() as tx:job=dict(tx.execute('SELECT * FROM analysis_jobs WHERE job_id=?',(job_id,)).fetchone())
            request=json.loads(job['request_json']);kind=job['kind'];context=request['context'];sources=request['sources']
            partial=json.loads(job['partial_json'])
            metadata={'basis':'abstract_only','visual_analysis':False,'prompt_version':PROMPT_VERSION,'source_characters':sum(len(s['text']) for s in sources),'chunks':1}
            notes=None
            if kind=='read':
                self._update(job_id,stage='获取官方 arXiv HTML',progress=10)
                document=self.html.get(request['paper']);sources=document['sources']
                batches=chunks(sources)
                metadata.update(basis='official_html_text',source_hash=document['source_hash'],html_url=document['url'],
                                parser=PARSER,source_characters=document['characters'],chunks=len(batches),source_blocks=len(sources))
                if partial.get('source_hash')!=document['source_hash'] or partial.get('notes_version',NOTES_VERSION)!=NOTES_VERSION:partial={'source_hash':document['source_hash'],'notes':{}}
                partial.setdefault('notes',{})
                partial['notes_version']=NOTES_VERSION
                if len(batches)>1:
                    # A final-report prompt change must not force another full
                    # reading. Reuse only matching document/model notes and
                    # revalidate every quote against its exact source segment.
                    if len(partial['notes'])<len(batches):
                        with self.db.transaction() as tx:
                            previous=tx.execute('''SELECT job_id,partial_json FROM analysis_jobs
                                WHERE owner_key=? AND arxiv_id=? AND kind='read' AND paper_input_hash=?
                                AND channel=? AND model=? AND job_id<>? ORDER BY created_at DESC LIMIT 10''',
                                (self.owner,job['arxiv_id'],job['paper_input_hash'],route.mode,route.model,job_id)).fetchall()
                        for old in previous:
                            saved=json.loads(old['partial_json'])
                            if saved.get('source_hash')!=document['source_hash'] or saved.get('notes_version',NOTES_VERSION)!=NOTES_VERSION or not isinstance(saved.get('notes'),dict):continue
                            for index,batch in enumerate(batches):
                                key=str(index)
                                if key in partial['notes'] or key not in saved['notes']:continue
                                try:checked=validate_notes(json_text({'notes':saved['notes'][key]}),batch)
                                except AgentError:continue
                                partial['notes'][key]=checked
                                partial.setdefault('reused_notes_from',{})[key]=old['job_id']
                        self._update(job_id,partial_json=json_text(partial))
                    notes=[]
                    for index,batch in enumerate(batches):
                        self._update(job_id,stage=f'分析正文分段 {index+1}/{len(batches)}',progress=15+int(60*index/len(batches)))
                        key=str(index)
                        if key not in partial['notes']:
                            partial['notes'][key]=validate_notes(route.complete(notes_prompt(batch)),batch)
                            self._update(job_id,partial_json=json_text(partial))
                        notes.extend(validate_notes(json_text({'notes':partial['notes'][key]}),batch))
            self._update(job_id,stage='生成并校验报告',progress=80)
            raw_report=partial.get('report_validation',{}).get('response')
            if raw_report:
                try:validate_report(raw_report,sources,kind,evidence=notes)
                except AgentError:raw_report=None
            if not raw_report:raw_report=route.complete(report_prompt(kind,context,sources,notes))
            try:report=validate_report(raw_report,sources,kind,evidence=notes)
            except AgentError as exc:
                partial['report_validation']={'error':str(exc),'response':raw_report[:120000]}
                self._update(job_id,partial_json=json_text(partial))
                raise
            partial.pop('report_validation',None)
            metadata['reused_chunks']=len(partial.get('reused_notes_from',{}))
            if notes is not None:metadata['evidence_count']=len(notes)
            report['metadata']=dict(metadata,channel=route.mode,model=route.model,generated_at=utcnow(),arxiv_id=job['arxiv_id'],version=request['paper']['version'],paper_input_hash=job['paper_input_hash'])
            self._update(job_id,status='succeeded',stage='已完成',progress=100,report_json=json_text(report),partial_json=json_text(partial),error=None)
        except AgentError as exc:self._update(job_id,status='failed',stage='生成未完成',error=str(exc))
        except Exception:self._update(job_id,status='failed',stage='生成未完成',error='任务发生错误，未保存未经校验的报告。请检查配置后手动重试。')
