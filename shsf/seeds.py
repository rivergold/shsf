"""Single-user seed drafts, confirmed snapshots, and conservative paper matching."""
from html.parser import HTMLParser
import json
import re
from urllib.parse import unquote

from .models import compact, digest, json_text, normalize_id, utcnow

MAX_SEEDS = 100


class RevisionConflict(ValueError):
    pass


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value):
    parser = _PlainText()
    parser.feed(str(value or ""))
    return compact("".join(parser.parts))


def normalize_doi(value):
    value = unquote(str(value or "").strip())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value, flags=re.I)
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def arxiv_claim(data):
    candidates = [data.get("url", "")]
    for field in ("extra", "archiveLocation", "archiveID"):
        value = str(data.get(field, ""))
        match = re.search(r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf|html)/)([^\s;]+)", value, re.I)
        if match:
            candidates.append(match.group(1))
        if str(data.get("archive", "")).lower() == "arxiv":
            candidates.append(value)
    doi = normalize_doi(data.get("DOI"))
    if doi and doi.startswith("10.48550/arxiv."):
        candidates.append(doi[len("10.48550/arxiv."):])
    for value in candidates:
        try:
            return normalize_id(str(value))[0]
        except ValueError:
            pass
    return None


def zotero_metadata(item, library_id):
    data = item.get("data", {})
    if data.get("itemType") in ("attachment", "note", "annotation") or data.get("deleted"):
        return None
    title = plain(data.get("title"))[:2000]
    if not title:
        return None
    creators = data.get("creators", [])
    authors = [compact(c.get("name") or " ".join([c.get("firstName", ""), c.get("lastName", "")]))
               for c in creators if isinstance(c, dict) and c.get("creatorType") == "author"]
    year = re.search(r"\b(19\d{2}|20\d{2})\b", str(data.get("date", "")))
    key = str(item.get("key") or data.get("key", ""))
    if not re.fullmatch(r"[A-Z0-9]{8}", key):
        raise ValueError("Zotero 返回了无效条目 ID")
    return {"title": title, "abstract": plain(data.get("abstractNote"))[:50000],
            "authors": authors, "published_year": int(year.group()) if year else None,
            "claimed_arxiv_id": arxiv_claim(data), "doi": normalize_doi(data.get("DOI")),
            "source_kind": "zotero", "library_id": str(library_id), "external_key": key,
            "collections": [str(v) for v in data.get("collections", []) if re.fullmatch(r"[A-Z0-9]{8}", str(v))],
            "source_version": item.get("version", data.get("version"))}


def paper_metadata(row):
    row = dict(row)
    return {"title": row["title"], "abstract": row["abstract"], "authors": json.loads(row["authors_json"]),
            "published_year": int(row["published_at"][:4]), "claimed_arxiv_id": row["arxiv_id"],
            "doi": normalize_doi(row["doi"]), "source_kind": "local", "library_id": "",
            "external_key": row["arxiv_id"], "collections": []}


def _like(value):
    return "%" + value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SeedStore:
    def __init__(self, database, owner="local"):
        self.db, self.owner = database, owner
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO seed_workspaces(owner_key) VALUES (?) ON CONFLICT(owner_key) DO NOTHING", (owner,))

    def _lock(self, tx, expected):
        row = tx.execute("SELECT * FROM seed_workspaces WHERE owner_key=?" +
                         (" FOR UPDATE" if tx.postgres else ""), (self.owner,)).fetchone()
        if not isinstance(expected, int) or isinstance(expected, bool) or row["revision"] != expected:
            raise RevisionConflict("种子列表已在其他页面更新，已重新读取，请再操作一次。")
        return row

    def _advance(self, tx):
        tx.execute("UPDATE seed_workspaces SET revision=revision+1 WHERE owner_key=?", (self.owner,))

    @staticmethod
    def match(tx, meta):
        claimed, doi = meta.get("claimed_arxiv_id"), meta.get("doi")
        if claimed:
            row = tx.execute("SELECT * FROM papers WHERE arxiv_id=? AND metadata_deleted=0", (claimed,)).fetchone()
            if row:
                return row, "matched"
        if doi:
            rows = tx.execute("SELECT * FROM papers WHERE LOWER(doi)=? AND metadata_deleted=0", (doi,)).fetchall()
            rows = [r for r in rows if not claimed or r["arxiv_id"] == claimed]
            if len(rows) == 1:
                return rows[0], "matched"
            if len(rows) > 1:
                return None, "ambiguous"
        # Exact title only. Conflicting explicit identifiers never get silently
        # replaced by a fuzzy/semantic title guess.
        rows = tx.execute("SELECT * FROM papers WHERE LOWER(title)=? AND metadata_deleted=0",
                          (compact(meta["title"]).lower(),)).fetchall()
        compatible = [r for r in rows if (not claimed or r["arxiv_id"] == claimed) and
                      (not doi or not r["doi"] or normalize_doi(r["doi"]) == doi)]
        if len(compatible) == 1:
            return compatible[0], "matched"
        return None, "ambiguous" if rows else "unmatched"

    def preview(self, items, library_id):
        output = []
        with self.db.transaction() as tx:
            for item in items:
                meta = zotero_metadata(item, library_id)
                if meta:
                    paper, status = self.match(tx, meta)
                    output.append(dict(meta, arxiv_id=paper["arxiv_id"] if paper else None, match_status=status))
        return output

    def _upsert(self, tx, meta):
        paper, status = self.match(tx, meta)
        aid = paper["arxiv_id"] if paper else None
        identity = ("arxiv:" + (aid or meta["claimed_arxiv_id"])) if aid or meta.get("claimed_arxiv_id") else (
            "doi:" + meta["doi"] if meta.get("doi") else "source:" + ":".join([meta["source_kind"], meta["library_id"], meta["external_key"]]))
        existing = tx.execute("""SELECT i.* FROM seed_sources s JOIN seed_items i ON i.seed_id=s.seed_id
            WHERE s.source_kind=? AND s.library_id=? AND s.external_key=?""",
            (meta["source_kind"], meta["library_id"], meta["external_key"])).fetchone()
        canonical = tx.execute("SELECT * FROM seed_items WHERE identity_key=?", (identity,)).fetchone()
        if canonical and existing and canonical["seed_id"] != existing["seed_id"]:
            old_id = existing["seed_id"]
            # Keep confirmed snapshots intact; merge only the editable selection
            # and provenance into the resolved canonical paper.
            tx.execute("""INSERT INTO seed_selection(owner_key,seed_id,selected_at)
                SELECT owner_key,?,selected_at FROM seed_selection WHERE seed_id=?
                ON CONFLICT(owner_key,seed_id) DO NOTHING""", (canonical["seed_id"], old_id))
            tx.execute("DELETE FROM seed_selection WHERE seed_id=?", (old_id,))
            tx.execute("UPDATE seed_sources SET seed_id=? WHERE seed_id=?", (canonical["seed_id"], old_id))
        if canonical:
            existing = canonical
        if existing is None and meta.get("doi"):
            candidates = tx.execute("SELECT * FROM seed_items WHERE doi=?", (meta["doi"],)).fetchall()
            compatible = [r for r in candidates if not r["arxiv_id"] or not aid or r["arxiv_id"] == aid]
            if len(compatible) == 1:
                existing = compatible[0]
        if existing is None and aid:
            candidates = tx.execute("SELECT * FROM seed_items WHERE arxiv_id IS NULL AND LOWER(title)=?",
                                    (compact(meta["title"]).lower(),)).fetchall()
            compatible = [r for r in candidates if (not r["claimed_arxiv_id"] or r["claimed_arxiv_id"] == aid)
                          and (not r["doi"] or not meta.get("doi") or r["doi"] == meta["doi"])]
            if len(compatible) == 1:
                existing = compatible[0]
        # Preserve a strong local link when a later sparse source item does
        # not carry the identifiers originally used to resolve it.
        if existing and existing["arxiv_id"] and not paper:
            linked = tx.execute("SELECT * FROM papers WHERE arxiv_id=? AND metadata_deleted=0", (existing["arxiv_id"],)).fetchone()
            if linked:
                paper, aid, status = linked, linked["arxiv_id"], "matched"
                identity = "arxiv:" + aid
        seed_id = existing["seed_id"] if existing else digest(identity)
        stamp = utcnow()
        title = paper["title"] if paper else meta["title"]
        abstract = paper["abstract"] if paper else meta["abstract"]
        authors = paper["authors_json"] if paper else json_text(meta["authors"])
        year = int(paper["published_at"][:4]) if paper else meta["published_year"]
        tx.execute("""INSERT INTO seed_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(seed_id) DO UPDATE SET
            identity_key=excluded.identity_key,arxiv_id=excluded.arxiv_id,claimed_arxiv_id=excluded.claimed_arxiv_id,
            doi=excluded.doi,title=excluded.title,abstract=excluded.abstract,authors_json=excluded.authors_json,
            published_year=excluded.published_year,match_status=excluded.match_status,updated_at=excluded.updated_at""",
            (seed_id, identity, aid, meta.get("claimed_arxiv_id"), meta.get("doi") or (normalize_doi(paper["doi"]) if paper else None),
             title, abstract, authors, year, status, existing["created_at"] if existing else stamp, stamp))
        source_id = digest(json_text([meta["source_kind"], meta["library_id"], meta["external_key"]]))
        tx.execute("""INSERT INTO seed_sources VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_kind,library_id,external_key)
            DO UPDATE SET seed_id=excluded.seed_id,collections_json=excluded.collections_json,
            metadata_json=excluded.metadata_json,imported_at=excluded.imported_at""",
            (source_id, seed_id, meta["source_kind"], meta["library_id"], meta["external_key"], json_text(meta["collections"]), json_text(meta), stamp))
        tx.execute("INSERT INTO seed_selection VALUES (?,?,?) ON CONFLICT(owner_key,seed_id) DO NOTHING", (self.owner, seed_id, stamp))
        return seed_id

    def state(self):
        with self.db.transaction() as tx:
            if tx.postgres:
                tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            workspace = dict(tx.execute("SELECT * FROM seed_workspaces WHERE owner_key=?", (self.owner,)).fetchone())
            items = []
            rows = tx.execute("""SELECT i.*,s.selected_at,p.metadata_deleted FROM seed_selection s JOIN seed_items i
                ON i.seed_id=s.seed_id LEFT JOIN papers p ON p.arxiv_id=i.arxiv_id
                WHERE s.owner_key=? ORDER BY s.selected_at,i.seed_id""", (self.owner,)).fetchall()
            for row in rows:
                data = dict(row)
                data["authors"] = json.loads(data.pop("authors_json"))
                data["sources"] = [dict(r) for r in tx.execute("SELECT source_kind,library_id,external_key,collections_json FROM seed_sources WHERE seed_id=?", (row["seed_id"],)).fetchall()]
                if data["metadata_deleted"]:
                    data["match_status"] = "source_deleted"
                data.pop("identity_key")
                items.append(data)
            return dict(workspace, items=items, maximum=MAX_SEEDS,
                        matched=sum(p["match_status"] == "matched" for p in items),
                        pending=sum(p["match_status"] != "matched" for p in items),
                        confirmed=workspace["confirmed_revision"] == workspace["revision"])

    def add_local(self, arxiv_id, expected):
        with self.db.transaction(immediate=True) as tx:
            self._lock(tx, expected)
            paper = tx.execute("SELECT * FROM papers WHERE arxiv_id=? AND metadata_deleted=0", (arxiv_id,)).fetchone()
            if not paper:
                raise ValueError("论文不在当前可用的本地库中。")
            self._upsert(tx, paper_metadata(paper))
            self._check_limit(tx)
            self._advance(tx)
        return self.state()

    def remove(self, seed_id, expected):
        with self.db.transaction(immediate=True) as tx:
            self._lock(tx, expected)
            tx.execute("DELETE FROM seed_selection WHERE owner_key=? AND seed_id=?", (self.owner, seed_id))
            self._advance(tx)
        return self.state()

    def import_zotero(self, items, library_id, expected):
        imported, ignored = set(), 0
        with self.db.transaction(immediate=True) as tx:
            self._lock(tx, expected)
            for item in items:
                meta = zotero_metadata(item, library_id)
                if meta is None:
                    ignored += 1
                else:
                    imported.add(self._upsert(tx, meta))
            self._check_limit(tx)
            self._advance(tx)
        return {"state": self.state(), "imported": len(imported), "ignored": ignored}

    def _check_limit(self, tx):
        count = tx.execute("SELECT COUNT(*) AS n FROM seed_selection WHERE owner_key=?", (self.owner,)).fetchone()["n"]
        if count > MAX_SEEDS:
            raise ValueError(f"最多选择 {MAX_SEEDS} 篇种子论文；本次操作没有保存。")

    def confirm(self, expected):
        with self.db.transaction(immediate=True) as tx:
            self._lock(tx, expected)
            rows = tx.execute("""SELECT i.*,p.input_hash FROM seed_selection s JOIN seed_items i ON i.seed_id=s.seed_id
                LEFT JOIN papers p ON p.arxiv_id=i.arxiv_id WHERE s.owner_key=?""", (self.owner,)).fetchall()
            if not rows:
                raise ValueError("请先选择至少一篇种子论文。")
            tx.execute("DELETE FROM confirmed_seeds WHERE owner_key=?", (self.owner,))
            stamp = utcnow()
            for row in rows:
                tx.execute("INSERT INTO confirmed_seeds VALUES (?,?,?,?,?)", (self.owner, row["seed_id"], json_text(dict(row)), row["input_hash"], stamp))
            tx.execute("UPDATE seed_workspaces SET confirmed_revision=revision,confirmed_at=? WHERE owner_key=?", (stamp, self.owner))
        return self.state()

    def search(self, query="", mode="all", sort="relevance", page=1, page_size=10):
        if mode not in ("all", "author", "identifier") or sort not in ("relevance", "recent"):
            raise ValueError("检索方式或排序无效。")
        if not isinstance(query, str) or len(query) > 300 or not 1 <= page <= 100000:
            raise ValueError("检索条件无效。")
        query = compact(query)
        aid = None
        try:
            aid = normalize_id(query)[0]
        except ValueError:
            pass
        doi = normalize_doi(query)
        if doi and doi.startswith("10.48550/arxiv."):
            try:
                aid = normalize_id(doi[len("10.48550/arxiv."):])[0]
            except ValueError:
                pass
        clauses, args = ["p.metadata_deleted=0"], []
        if aid:
            clauses.append("p.arxiv_id=?")
            args.append(aid)
        elif doi:
            clauses.append("LOWER(p.doi)=?")
            args.append(doi)
        elif mode == "identifier" and query:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        else:
            tokens = query.split()[:12]
            for token in tokens:
                fields = ["p.authors_json"] if mode == "author" else ["p.title", "p.abstract"]
                clauses.append("(" + " OR ".join(f"LOWER({field}) LIKE ? ESCAPE '\\'" for field in fields) + ")")
                args.extend([_like(token)] * len(fields))
        where = " AND ".join(clauses)
        score_args = [_like(query), _like(query)]
        score = "CASE WHEN LOWER(p.title) LIKE ? ESCAPE '\\' THEN 20 ELSE 0 END + CASE WHEN LOWER(p.abstract) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
        order = "p.published_at DESC,p.arxiv_id" if sort == "recent" or not query else "rank_score DESC,p.published_at DESC,p.arxiv_id"
        with self.db.transaction() as tx:
            if tx.postgres:
                tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = tx.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {where}", args).fetchone()["n"]
            rows = tx.execute(f"""SELECT p.arxiv_id,p.version,p.title,p.abstract,p.authors_json,p.categories_json,
                p.published_at,p.doi,({score}) AS rank_score FROM papers p WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""", score_args + args + [page_size, (page - 1) * page_size]).fetchall()
            items = []
            for row in rows:
                data = dict(row)
                data["authors"] = json.loads(data.pop("authors_json"))
                data["categories"] = json.loads(data.pop("categories_json"))
                data.pop("rank_score")
                items.append(data)
        return {"items": items, "total": total, "page": page, "page_size": page_size}
