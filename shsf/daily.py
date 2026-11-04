"""Resumable, single-host daily metadata -> embeddings -> recommendation job."""
import fcntl
from pathlib import Path
import uuid

from .embeddings import SpecterEncoder, run_worker
from .ingestion import category_set
from .models import digest, json_text, utcnow
from .recommendations import Recommendations, today


def run_daily(database, ingest, spec, data_dir, max_pages=50, batches=500, batch_size=8,
              encoder=None, force=False):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'daily-update.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        day = today()
        with database.transaction(immediate=True) as tx:
            done = tx.execute("SELECT run_id FROM daily_update_runs WHERE day=? AND status='completed' LIMIT 1", (day,)).fetchone()
            if done and not force:
                return {'status': 'already_completed', 'day': day}
            # A held OS lock cannot survive process exit. Recover the visible
            # state after a killed worker without replaying model API requests.
            tx.execute("UPDATE daily_update_runs SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                       (utcnow(), '上次更新进程已退出，将从已提交数据继续。'))
            run_id = uuid.uuid4().hex
            tx.execute('INSERT INTO daily_update_runs VALUES (?,?,?,?,?,?,?,?)',
                       (run_id, day, 'running', 'announcements', utcnow(), None, '{}', None))
        summary = {}
        stage = 'announcements'

        def checkpoint(next_stage):
            nonlocal stage
            stage = next_stage
            with database.transaction() as tx:
                tx.execute('UPDATE daily_update_runs SET stage=?,summary_json=? WHERE run_id=?',
                           (stage, json_text(summary), run_id))
            print(json_text({'event': 'daily_update', 'run_id': run_id, 'stage': stage}), flush=True)

        try:
            checkpoint('announcements')
            summary['announcements'] = ingest.import_announcements()
            checkpoint('incremental')
            # Reuse initialized OAI scopes, preserving their existing watermarks.
            # Uninitialized adjacent scopes get latest new papers via RSS; their
            # historical backfill is a separate operation, never started here.
            summary['incremental'] = {}
            for category in dict.fromkeys(ingest.profile.data['core_categories'] + ingest.profile.data['adjacent_categories']):
                key = digest(json_text(['oai-raw-v1', category_set(category), ingest.profile.fingerprint]))
                state = database.get_state(key)
                if state and (state.get('complete') or state.get('mode') == 'incremental'):
                    result = ingest.harvest(category, max_pages, incremental=True)
                    summary['incremental'][category] = result
                    if not result['state']['complete']:
                        raise RuntimeError('Incremental page budget exhausted; next run resumes the saved token')
                else:
                    summary['incremental'][category] = {'status': 'not_initialized', 'latest_source': 'RSS'}
            checkpoint('embeddings')
            summary['embeddings'] = run_worker(database, spec,
                encoder or SpecterEncoder(spec, directory / 'models', 'cpu'), batches, batch_size)
            if summary['embeddings']['failed']:
                raise RuntimeError('Embedding worker failed')
            with database.transaction() as tx:
                missing = tx.execute('''SELECT COUNT(*) AS n FROM papers p LEFT JOIN paper_embeddings e
                    ON e.arxiv_id=p.arxiv_id AND e.input_hash=p.input_hash AND e.model_key=?
                    WHERE p.metadata_deleted=0 AND e.arxiv_id IS NULL''', (spec.key,)).fetchone()['n']
            if missing:
                raise RuntimeError(f'{missing} current paper embeddings are not ready; retry before generating')
            checkpoint('recommendations')
            recs = Recommendations(database, spec=spec)
            current = recs.get(day)
            if current['context']['ready_seeds']:
                result = recs.generate(day, during_update=True)
                summary['recommendations'] = {'batch_id': result['batch']['batch_id'],
                    'items': len(result['batch']['items']), 'context': result['batch']['context']}
            else:
                summary['recommendations'] = {'status': 'skipped', 'reason': 'no_ready_confirmed_seeds'}
            with database.transaction() as tx:
                tx.execute("UPDATE daily_update_runs SET status='completed',stage='completed',finished_at=?,summary_json=? WHERE run_id=?",
                           (utcnow(), json_text(summary), run_id))
            return {'status': 'completed', 'day': day, 'run_id': run_id, **summary}
        except BaseException as exc:
            # Persist a safe error: upstream errors may contain proxy credentials
            # or connection strings. Details are diagnosed using stage logs.
            error = f'{stage}: {type(exc).__name__}；本次更新未完成，稍后自动重试。'
            with database.transaction() as tx:
                tx.execute("UPDATE daily_update_runs SET status='failed',finished_at=?,summary_json=?,error=? WHERE run_id=?",
                           (utcnow(), json_text(summary), error, run_id))
            raise RuntimeError(error) from None
