"""Frozen SPECTER2 baseline: seed-neighbor recall, top-3 cosine, daily snapshots."""
from datetime import date, datetime, timedelta
import json
import math
from zoneinfo import ZoneInfo

from .embeddings import EmbeddingSpec
from .models import compact, digest, json_text, utcnow
from .seeds import normalize_doi
from .translations import cached_abstract

ALGORITHM = 'specter2-latest-release-diversity-v3'


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def vector(value):
    values = json.loads(value) if isinstance(value, str) else list(value)
    norm = math.sqrt(sum(x*x for x in values))
    if not norm or not all(math.isfinite(x) for x in values):
        raise ValueError('无效向量。')
    return [x/norm for x in values]


def cosine(a, b):
    return sum(x*y for x,y in zip(a,b))


def diversify(candidates, limit, day):
    """Target six latest-release papers; fill the remainder from other work."""
    ordered = sorted(candidates, key=lambda x: (-x['similarity'], x['arxiv_id']))
    pool_ids = {p['arxiv_id'] for p in ordered[:250]}
    pool_ids.update(p['arxiv_id'] for p in [p for p in ordered if p.get('is_latest_release')][:100])
    remaining = [p for p in ordered if p['arxiv_id'] in pool_ids]
    picked, titles, dois, counts = [], set(), set(), {}
    while len(picked) < limit:
        recent = [p for p in remaining if p.get('is_latest_release')]
        latest_count = sum(bool(p.get('is_latest_release')) for p in picked)
        others = [p for p in remaining if not p.get('is_latest_release')]
        if latest_count < min(6,limit) and recent:
            pool = recent
        else:
            # Aim for six, while still filling a useful batch if the other
            # candidate pool is too small after exclusions and deduplication.
            pool = others or remaining
        if not pool:
            break
        def utility(p):
            redundancy = max((cosine(p['_vector'], q['_vector']) for q in picked), default=0)
            return p['similarity'] - .10*redundancy - .025*counts.get(p['reasons'][0]['arxiv_id'],0)
        best = max(pool, key=lambda p: (utility(p),p['arxiv_id']))
        remaining.remove(best)
        title, doi = compact(best['title']).lower(), normalize_doi(best.get('doi'))
        if title in titles or (doi and doi in dois):
            continue
        titles.add(title)
        if doi: dois.add(doi)
        picked.append(best)
        source = best['reasons'][0]['arxiv_id']
        counts[source] = counts.get(source,0)+1
    return sorted(picked, key=lambda p: (-p['similarity'],p['arxiv_id']))


class Recommendations:
    def __init__(self, database, owner='local', spec=None):
        self.db, self.owner, self.spec = database, owner, spec or EmbeddingSpec()

    def _context(self, tx, day=None):
        ws = tx.execute('SELECT * FROM seed_workspaces WHERE owner_key=?',(self.owner,)).fetchone()
        rows = tx.execute('''SELECT c.*,i.arxiv_id AS linked_id FROM confirmed_seeds c
            JOIN seed_items i ON i.seed_id=c.seed_id WHERE c.owner_key=? ORDER BY c.seed_id''',(self.owner,)).fetchall()
        seeds, excluded_ids, excluded_titles, excluded_dois = [], set(), set(), set()
        snapshots = []
        for r in rows:
            meta = json.loads(r['metadata_snapshot_json'])
            snapshots.append([r['seed_id'],r['confirmed_at'],meta])
            aid = meta.get('arxiv_id') or meta.get('claimed_arxiv_id') or r['linked_id']
            excluded_titles.add(compact(meta['title']).lower())
            doi = normalize_doi(meta.get('doi'))
            if doi: excluded_dois.add(doi)
            if not aid: continue
            excluded_ids.add(aid)
            record = tx.execute('''SELECT e.embedding,p.input_hash,p.title FROM paper_embeddings e
                JOIN papers p ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                WHERE e.arxiv_id=? AND e.model_key=? AND p.metadata_deleted=0''',(aid,self.spec.key)).fetchone()
            if record:
                seeds.append({'arxiv_id':aid,'title':record['title'],'input_hash':record['input_hash'],'_vector':vector(record['embedding'])})
        total=tx.execute('SELECT COUNT(*) AS n FROM papers WHERE metadata_deleted=0').fetchone()['n']
        covered=tx.execute('''SELECT COUNT(*) AS n FROM paper_embeddings e JOIN papers p
            ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash WHERE e.model_key=? AND p.metadata_deleted=0''',(self.spec.key,)).fetchone()['n']
        release=tx.execute('SELECT MAX(release_day) AS day FROM arxiv_releases WHERE release_day<=?', (day or today(),)).fetchone()['day']
        context={'algorithm':ALGORITHM,'model_key':self.spec.key,'confirmed_seeds':len(rows),'ready_seeds':len(seeds),
                 'latest_release_day':release,'latest_target':6,
                 'pending_seeds':len(rows)-len(seeds),'draft_changed':bool(ws and ws['revision']!=ws['confirmed_revision']),
                 'corpus_papers':total,'corpus_vectors':covered,'coverage':covered/total if total else 0,
                 'profile_key':digest(json_text([ALGORITHM,snapshots,release])), 'trained':False}
        return context,seeds,excluded_ids,excluded_titles,excluded_dois

    def _batch(self,tx,day,context):
        row=tx.execute('''SELECT * FROM recommendation_batches WHERE owner_key=? AND day=? AND profile_key=? AND model_key=?''',
                       (self.owner,day,context['profile_key'],self.spec.key)).fetchone()
        if not row:return None
        items=[]
        for r in tx.execute('''SELECT r.snapshot_json,f.interest,f.is_read,p.metadata_deleted FROM recommendation_items r
            JOIN papers p ON p.arxiv_id=r.arxiv_id LEFT JOIN paper_feedback f ON f.arxiv_id=r.arxiv_id AND f.owner_key=?
            WHERE r.batch_id=? ORDER BY r.rank''',(self.owner,row['batch_id'])).fetchall():
            data=json.loads(r['snapshot_json'])
            data.update(cached_abstract(tx,self.owner,data))
            data['feedback']={'interest':r['interest'] or 'none','is_read':bool(r['is_read'])}
            data['source_deleted']=bool(r['metadata_deleted'])
            items.append(data)
        return {'batch_id':row['batch_id'],'day':row['day'],'created_at':row['created_at'],
                'context':json.loads(row['context_json']),'items':items}

    def get(self,day=None):
        day=day or today()
        with self.db.transaction() as tx:
            if tx.postgres: tx.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            context,*_=self._context(tx,day)
            update=tx.execute('SELECT status,stage,started_at,finished_at,error FROM daily_update_runs ORDER BY started_at DESC LIMIT 1').fetchone()
            return {'context':context,'batch':self._batch(tx,day,context),'day':day,
                    'update':dict(update) if update else None}

    def _candidates(self,tx,seeds,day,release_day=None):
        # Per-seed recall prevents a large dominant interest from swallowing
        # all smaller interests. Exact cosine ranking uses the same seed set.
        fields='p.arxiv_id,p.title,p.abstract,p.authors_json,p.categories_json,p.published_at,p.doi'
        latest_ids={r['arxiv_id'] for r in tx.execute('SELECT arxiv_id FROM arxiv_release_items WHERE release_day=?', (release_day,)).fetchall()} if release_day else set()
        if tx.postgres:
            ids=set()
            for seed in seeds:
                for latest_only in (False,True) if latest_ids else (False,):
                    where=' AND p.arxiv_id IN (SELECT arxiv_id FROM arxiv_release_items WHERE release_day=?)' if latest_only else ''
                    args=[json_text(seed['_vector']),self.spec.key,day+'T23:59:59Z']+([release_day] if latest_only else [])
                    rows=tx.execute('''SELECT p.arxiv_id FROM paper_embeddings e JOIN papers p ON p.arxiv_id=e.arxiv_id
                        AND p.input_hash=e.input_hash WHERE e.model_key=? AND p.metadata_deleted=0 AND p.published_at<=?'''+where+
                        ' ORDER BY e.embedding <=> CAST(? AS vector),p.arxiv_id LIMIT 150',args[1:]+args[:1]).fetchall()
                    ids.update(r['arxiv_id'] for r in rows)
            if not ids:return []
            refs=','.join('(?,CAST(? AS vector))' for _ in seeds)
            args=[]
            for seed in seeds:args.extend([seed['arxiv_id'],json_text(seed['_vector'])])
            args += [self.spec.key]+sorted(ids)
            rows=tx.execute(f'''WITH refs(aid,embedding) AS (VALUES {refs})
                SELECT {fields},e.embedding::text AS embedding,d.similarity,d.reasons
                FROM paper_embeddings e JOIN papers p ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash
                CROSS JOIN LATERAL (SELECT AVG(s.sim) AS similarity,
                    json_agg(json_build_object('arxiv_id',s.aid,'similarity',s.sim) ORDER BY s.sim DESC,s.aid) AS reasons
                    FROM (SELECT aid,1-(e.embedding <=> refs.embedding) AS sim FROM refs ORDER BY sim DESC,aid LIMIT 3) s) d
                WHERE e.model_key=? AND p.metadata_deleted=0 AND p.arxiv_id IN ({','.join('?' for _ in ids)})
                ORDER BY d.similarity DESC,p.arxiv_id''',args).fetchall()
        else:
            rows=tx.execute(f'''SELECT {fields},e.embedding FROM paper_embeddings e JOIN papers p
                ON p.arxiv_id=e.arxiv_id AND p.input_hash=e.input_hash WHERE e.model_key=? AND p.metadata_deleted=0
                AND p.published_at<=?''',(self.spec.key,day+'T23:59:59Z')).fetchall()
        output=[]
        titles={s['arxiv_id']:s['title'] for s in seeds}
        for row in rows:
            p=dict(row);p['_vector']=vector(p.pop('embedding'))
            p['is_latest_release']=p['arxiv_id'] in latest_ids
            p['announcement_day']=release_day if p['is_latest_release'] else None
            reasons=p.get('reasons') or sorted([{'arxiv_id':s['arxiv_id'],'similarity':cosine(p['_vector'],s['_vector'])} for s in seeds],key=lambda r:(-r['similarity'],r['arxiv_id']))[:3]
            p['similarity']=float(p.get('similarity') if p.get('similarity') is not None else sum(r['similarity'] for r in reasons)/len(reasons))
            p['reasons']=[dict(r,title=titles[r['arxiv_id']]) for r in reasons]
            p['authors']=json.loads(p.pop('authors_json'));p['categories']=json.loads(p.pop('categories_json'))
            output.append(p)
        return output

    def generate(self,day=None,limit=20, during_update=False):
        day=day or today()
        date.fromisoformat(day)
        if not 1<=limit<=30:raise ValueError('推荐数量需要在 1–30 之间。')
        with self.db.transaction(immediate=True) as tx:
            if not during_update and tx.execute("SELECT 1 FROM daily_update_runs WHERE status='running' LIMIT 1").fetchone():
                raise ValueError('每日更新正在进行，请在数据和向量更新完成后生成推荐。')
            tx.execute('SELECT owner_key FROM seed_workspaces WHERE owner_key=?'+(' FOR UPDATE' if tx.postgres else ''),(self.owner,)).fetchone()
            context,seeds,ids,titles,dois=self._context(tx,day)
            existing=self._batch(tx,day,context)
            if existing:return {'context':context,'batch':existing,'day':day}
            if not context['confirmed_seeds']:raise ValueError('请先确认种子论文。')
            if not seeds:raise ValueError('已确认种子尚无有效向量，请先补齐 embedding。')
            excluded=set(ids)
            for row in tx.execute('SELECT arxiv_id FROM paper_feedback WHERE owner_key=? AND (interest<>? OR is_read=1)',(self.owner,'none')).fetchall():excluded.add(row['arxiv_id'])
            cutoff=(date.fromisoformat(day)-timedelta(days=30)).isoformat()
            for row in tx.execute('''SELECT i.arxiv_id FROM recommendation_items i JOIN recommendation_batches b
                ON b.batch_id=i.batch_id WHERE b.owner_key=? AND b.day>=? AND b.day<?''',(self.owner,cutoff,day)).fetchall():excluded.add(row['arxiv_id'])
            candidates=[p for p in self._candidates(tx,seeds,day,context['latest_release_day']) if p['arxiv_id'] not in excluded
                        and compact(p['title']).lower() not in titles and normalize_doi(p.get('doi')) not in dois
                        and p['similarity']>0]
            chosen=diversify(candidates,limit,day)
            batch_id=digest(json_text([self.owner,day,context['profile_key'],self.spec.key]))
            stamp=utcnow()
            context['scored_candidates']=len(candidates)
            context['latest_selected']=sum(bool(p.get('is_latest_release')) for p in chosen)
            context['latest_eligible']=sum(bool(p.get('is_latest_release')) for p in candidates)
            context['seed_vectors']=[{k:v for k,v in s.items() if k!='_vector'} for s in seeds]
            tx.execute('INSERT INTO recommendation_batches VALUES (?,?,?,?,?,?,?)',(batch_id,self.owner,day,context['profile_key'],self.spec.key,stamp,json_text(context)))
            for rank,p in enumerate(chosen,1):
                p.pop('_vector')
                p['rank']=rank
                tx.execute('INSERT INTO recommendation_items VALUES (?,?,?,?)',(batch_id,rank,p['arxiv_id'],json_text(p)))
            return {'context':context,'batch':self._batch(tx,day,context),'day':day}

    def feedback(self,arxiv_id,interest=None,is_read=None):
        if interest not in (None,'none','like','dislike') or (is_read is not None and not isinstance(is_read,bool)):
            raise ValueError('反馈值无效。')
        if interest is None and is_read is None:raise ValueError('请选择反馈。')
        with self.db.transaction(immediate=True) as tx:
            # Only papers actually presented to this user can receive feedback.
            seen=tx.execute('''SELECT 1 FROM recommendation_items i JOIN recommendation_batches b ON b.batch_id=i.batch_id
                WHERE b.owner_key=? AND i.arxiv_id=? LIMIT 1''',(self.owner,arxiv_id)).fetchone()
            if not seen:raise ValueError('该论文不在你的推荐记录中。')
            tx.execute('INSERT INTO paper_feedback VALUES (?,?,?,?,?) ON CONFLICT(owner_key,arxiv_id) DO NOTHING',
                       (self.owner,arxiv_id,'none',0,utcnow()))
            row=tx.execute('SELECT * FROM paper_feedback WHERE owner_key=? AND arxiv_id=?'+(' FOR UPDATE' if tx.postgres else ''),(self.owner,arxiv_id)).fetchone()
            interest=row['interest'] if interest is None else interest
            read=int(row['is_read'] if is_read is None else is_read)
            tx.execute('UPDATE paper_feedback SET interest=?,is_read=?,updated_at=? WHERE owner_key=? AND arxiv_id=?',
                       (interest,read,utcnow(),self.owner,arxiv_id))
            return {'arxiv_id':arxiv_id,'interest':interest,'is_read':bool(read)}
