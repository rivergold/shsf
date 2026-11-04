from datetime import date, timedelta
import json
from pathlib import Path

from .arxiv import ArxivError, parse_atom, parse_oai, parse_announcements
from .http import archive_response
from .models import digest, json_text, utcnow

OAI_ENDPOINT = "https://oaipmh.arxiv.org/oai"
ATOM_ENDPOINT = "https://export.arxiv.org/api/query"


def category_set(category):
    if category == "cs.SY":
        category = "eess.SY"
    group = category.split(".")[0]
    return f"{group}:{group}:{category.split('.')[-1]}"


class Ingester:
    def __init__(self, database, profile, model, client, data_dir):
        self.db, self.profile, self.model = database, profile, model
        self.client, self.data_dir = client, Path(data_dir)
        self.db.register_model(model)

    def import_announcements(self):
        categories = list(dict.fromkeys(self.profile.data["core_categories"] + self.profile.data["adjacent_categories"]))
        body, url = self.client.get("https://rss.arxiv.org/rss/" + "+".join(categories), {})
        raw = archive_response(self.data_dir, body, url)
        release_day, papers, has_items = parse_announcements(body)
        # Empty weekend/holiday feeds must not replace the last real release.
        if not has_items:
            return {"release_day": None, "new_papers": 0, "empty_feed": True}
        selected = [p.arxiv_id for p in papers if self.profile.classify(p)["admitted"]]
        admitted = set()
        inserted = 0
        for start in range(0, len(selected), 50):
            ids = selected[start:start + 50]
            metadata, metadata_url = self.client.get(ATOM_ENDPOINT, {"id_list": ",".join(ids), "max_results": len(ids)})
            metadata_raw = archive_response(self.data_dir, metadata, metadata_url)
            page = parse_atom(metadata)
            if {p.arxiv_id for p in page.papers} != set(ids):
                raise ValueError("Atom did not return every requested announcement; retry later")
            inserted += self.import_page(page, metadata_raw)["inserted"]
            admitted.update(p.arxiv_id for p in page.papers if self.profile.classify(p)["admitted"])
        # Publish membership only after ALL metadata chunks succeeded. Partial
        # imports are safe to replay and must not look like a complete release.
        with self.db.transaction(immediate=True) as tx:
            tx.execute('''INSERT INTO arxiv_releases VALUES (?,?,?,?) ON CONFLICT(release_day)
                DO UPDATE SET categories_json=excluded.categories_json,
                raw_response_path=excluded.raw_response_path,fetched_at=excluded.fetched_at''',
                (release_day, json_text(categories), str(raw), utcnow()))
            tx.execute('DELETE FROM arxiv_release_items WHERE release_day=?', (release_day,))
            for aid in sorted(admitted):
                tx.execute('INSERT INTO arxiv_release_items VALUES (?,?)', (release_day, aid))
        return {"release_day": release_day, "new_papers": len(admitted), "inserted": inserted,
                "categories": categories, "raw_path": str(raw)}

    def import_page(self, page, raw_path, force=False):
        admitted, inserted = 0, 0
        with self.db.transaction(immediate=True) as tx:
            for paper in page.papers:
                match = self.profile.classify(paper)
                if not match["admitted"] and not force:
                    continue
                if force and not match["admitted"]:
                    match.update(reason="explicit_import", priority=1)
                inserted += self.db.save_paper(tx, paper, match, self.profile.fingerprint, raw_path, self.model.key)
                admitted += 1
        return {"seen": len(page.papers), "admitted": admitted, "inserted": inserted}

    def import_ids(self, ids):
        from .models import normalize_id
        normalized = []
        for value in ids:
            aid, version = normalize_id(value)
            normalized.append(aid + (f"v{version}" if version else ""))
        if len(normalized) > 50:
            raise ValueError("Import at most 50 IDs per request")
        body, url = self.client.get(ATOM_ENDPOINT, {"id_list": ",".join(normalized), "max_results": len(normalized)})
        raw = archive_response(self.data_dir, body, url)
        page = parse_atom(body)
        result = self.import_page(page, raw, force=True)
        result["raw_path"] = str(raw)
        result["papers"] = [p.arxiv_id for p in page.papers]
        return result

    def import_recent(self, category="cs.RO", limit=100):
        """Bootstrap a useful recent slice while chronological OAI continues.

        This is explicitly a bounded snapshot, not a substitute for a complete
        historical harvest or an incremental synchronization watermark.
        """
        allowed = self.profile.data["core_categories"] + self.profile.data["adjacent_categories"]
        if category not in allowed:
            raise ValueError("Category is not in this profile's configured scope")
        if not 1 <= limit <= 1000:
            raise ValueError("Recent snapshot limit must be between 1 and 1000")
        body, url = self.client.get(ATOM_ENDPOINT, {"search_query": "cat:" + category,
                                                  "start": 0, "max_results": limit,
                                                  "sortBy": "submittedDate", "sortOrder": "descending"})
        raw = archive_response(self.data_dir, body, url)
        page = parse_atom(body)
        result = self.import_page(page, raw)
        return dict(result, scope="bounded_recent_snapshot", category=category,
                    requested=limit, available_total=page.total, raw_path=str(raw),
                    advances_incremental_watermark=False)

    def replay(self, path, force=False):
        body = Path(path).read_bytes()
        raw = archive_response(self.data_dir, body, "local-replay:" + str(Path(path).resolve()))
        try:
            page = parse_oai(body)
        except ValueError:
            page = parse_atom(body)
        return self.import_page(page, raw, force)

    def harvest(self, category="cs.RO", max_pages=1, incremental=False, restart=False):
        set_spec = category_set(category)
        key = digest(json_text(["oai-raw-v1", set_spec, self.profile.fingerprint]))
        state = self.db.get_state(key)
        if state and state.get("complete") and not incremental and not restart:
            return {"message": "Historical harvest already complete; use --incremental", "state": state}
        if incremental and (not state or not state.get("complete")):
            if not state or not state.get("mode") == "incremental":
                raise ValueError("Finish the historical harvest before starting incremental sync")
        if restart or state is None or (incremental and state.get("complete")):
            previous_watermark = state.get("watermark") if state else None
            from_date = None
            if incremental:
                # Inclusive overlap tolerates same-day updates and replay.
                from_date = (date.fromisoformat(previous_watermark[:10]) - timedelta(days=2)).isoformat()
            state = {"mode": "incremental" if incremental else "historical", "complete": False,
                     "token": None, "from_date": from_date, "anchor": None, "pages": 0,
                     "seen": 0, "admitted": 0, "watermark": previous_watermark}
        total = {"pages": 0, "seen": 0, "admitted": 0, "inserted": 0, "deleted": 0}
        recovered = False
        for _ in range(max_pages):
            expected = self.db.get_state(key)
            params = {"verb": "ListRecords"}
            if state.get("token"):
                params["resumptionToken"] = state["token"]
            else:
                params.update(metadataPrefix="arXivRaw", set=set_spec)
                if state.get("from_date"):
                    params["from"] = state["from_date"]
            body, url = self.client.get(OAI_ENDPOINT, params)
            raw = archive_response(self.data_dir, body, url)
            try:
                page = parse_oai(body)
            except ArxivError as exc:
                if exc.code != "badResumptionToken" or recovered or not state.get("token"):
                    raise
                # A token is not a durable checkpoint: restart the SAME scope
                # (not the largest datestamp seen, which could skip records).
                state.update(token=None, anchor=None, pages=0, seen=0, admitted=0)
                with self.db.transaction(immediate=True) as tx:
                    self._check_state(tx, key, expected)
                    self.db.save_state(tx, key, self.profile.fingerprint, set_spec, state)
                recovered = True
                # The bounded invocation returns; next invocation continues
                # safely from the persisted reset rather than silently looping.
                return dict(total, message="Expired token reset; rerun the same command", state=state)
            if not page.response_date:
                raise ValueError("OAI responseDate missing; cannot checkpoint safely")
            seen, admitted, inserted = len(page.papers) + len(page.deleted_ids), 0, 0
            with self.db.transaction(immediate=True) as tx:
                self._check_state(tx, key, expected)
                for paper in page.papers:
                    match = self.profile.classify(paper)
                    # Existing papers must be updated even when revised text
                    # no longer matches the lexical admission filter.
                    exists = tx.execute("SELECT 1 FROM papers WHERE arxiv_id=?", (paper.arxiv_id,)).fetchone()
                    if not match["admitted"] and not exists:
                        continue
                    inserted += self.db.save_paper(tx, paper, match, self.profile.fingerprint, raw, self.model.key)
                    admitted += 1
                for aid in page.deleted_ids:
                    tx.execute("UPDATE papers SET metadata_deleted=1,fetched_at=? WHERE arxiv_id=?", (utcnow(), aid))
                    tx.execute("UPDATE embedding_jobs SET status='superseded',updated_at=? WHERE arxiv_id=? AND status IN ('pending','retry','running')", (utcnow(), aid))
                state["anchor"] = state.get("anchor") or page.response_date
                state.update(token=page.next_token, complete=not bool(page.next_token),
                             pages=state["pages"] + 1, seen=state["seen"] + seen,
                             admitted=state["admitted"] + admitted)
                if state["complete"]:
                    # Advance only after all pages, using the start of the run,
                    # so modifications during a long harvest are fetched again.
                    state["watermark"] = state["anchor"][:10]
                self.db.save_state(tx, key, self.profile.fingerprint, set_spec, state)
                tx.execute("INSERT INTO ingestion_pages VALUES (?,?,?,?,?,?) ON CONFLICT(page_key) DO NOTHING",
                           (digest(key + digest(body.decode("utf-8"))), key, str(raw), utcnow(), seen, admitted))
            total["pages"] += 1
            total["seen"] += seen
            total["admitted"] += admitted
            total["inserted"] += inserted
            total["deleted"] += len(page.deleted_ids)
            print(json_text({"event": "harvest_page", "category": category, **total}), flush=True)
            if state["complete"]:
                break
        return dict(total, state=state)

    @staticmethod
    def _check_state(tx, key, expected):
        if tx.postgres:
            # Also protects the initially absent row; SELECT FOR UPDATE alone
            # cannot lock a row that has not been inserted yet.
            tx.execute("SELECT pg_advisory_xact_lock(?)", (int(key[:15], 16),))
        current = tx.execute("SELECT state_json FROM ingestion_jobs WHERE job_key=?" +
                             (" FOR UPDATE" if tx.postgres else ""), (key,)).fetchone()
        actual = json.loads(current["state_json"]) if current else None
        if actual != expected:
            raise RuntimeError("Another harvester advanced this scope. Rerun to resume its committed checkpoint.")
