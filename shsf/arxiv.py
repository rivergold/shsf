"""Strict Atom and OAI-PMH parsers; fetching is kept independent of storage."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re

from defusedxml import ElementTree as ET

from .models import Paper, compact, normalize_id

ATOM = "http://www.w3.org/2005/Atom"
OAI = "http://www.openarchives.org/OAI/2.0/"
NS = {"a": ATOM, "x": "http://arxiv.org/schemas/atom", "o": OAI,
      "s": "http://a9.com/-/spec/opensearch/1.1/"}


class ArxivError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"arXiv {code}: {message}")
        self.code = code


@dataclass
class Page:
    papers: list[Paper]
    next_token: str | None = None
    response_date: str | None = None
    datestamps: list[str] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    total: int | None = None


def parse_atom(content):
    root = ET.fromstring(content)
    if root.tag != f"{{{ATOM}}}feed":
        raise ValueError("Expected an Atom feed, not an HTML/error page")
    papers = []
    for entry in root.findall("a:entry", NS):
        raw_id = entry.findtext("a:id", "", NS)
        if "/api/errors" in raw_id:
            raise ArxivError("query", entry.findtext("a:summary", "", NS))
        aid, version = normalize_id(raw_id)
        html_url = next((e.get("href") for e in entry.findall("a:link", NS)
                         if e.get("type") == "text/html" and "/html/" in e.get("href", "")), None)
        papers.append(Paper(
            arxiv_id=aid, version=version,
            title=compact(entry.findtext("a:title", "", NS)),
            abstract=compact(entry.findtext("a:summary", "", NS)),
            authors=[compact(a.findtext("a:name", "", NS)) for a in entry.findall("a:author", NS)],
            categories=list(dict.fromkeys(e.get("term") for e in entry.findall("a:category", NS) if e.get("term"))),
            published_at=entry.findtext("a:published", "", NS),
            updated_at=entry.findtext("a:updated", "", NS),
            doi=entry.findtext("x:doi", None, NS), comments=entry.findtext("x:comment", None, NS),
            journal_ref=entry.findtext("x:journal_ref", None, NS), html_url=html_url,
        ))
    return Page(papers, total=int(root.findtext("s:totalResults", str(len(papers)), NS)))


def _date(value):
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat().replace("+00:00", "Z")


def parse_announcements(content):
    """RSS announcement dates are distinct from Atom submission timestamps.

    Return lightweight metadata for filtering only; canonical Paper records
    must subsequently be fetched from Atom. Replacements and cross-listings
    of existing papers do not qualify as newly released papers.
    """
    root = ET.fromstring(content)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("Expected an arXiv RSS feed")
    release_day = parsedate_to_datetime(channel.findtext("pubDate", "")).date().isoformat()
    items = channel.findall("item")
    papers = {}
    for item in items:
        kind = item.findtext("x:announce_type", "", NS).strip()
        if kind not in {"new", "cross", "replace", "replace-cross"}:
            raise ValueError("Unknown arXiv announcement type")
        if kind != "new":
            continue
        aid, version = normalize_id(item.findtext("guid", ""))
        announced = parsedate_to_datetime(item.findtext("pubDate", "")).date().isoformat()
        if announced != release_day:
            raise ValueError("RSS item does not belong to the channel announcement day")
        description = item.findtext("description", "")
        match = re.match(r"arXiv:\S+\s+Announce Type:\s*new\s+Abstract:\s*(.*)", description, re.S)
        if not match:
            raise ValueError("arXiv RSS abstract missing")
        papers[aid] = Paper(
            arxiv_id=aid, version=version, title=compact(item.findtext("title", "")),
            abstract=compact(match[1]), authors=[],
            categories=[c.text.strip() for c in item.findall("category") if c.text],
            # These dates are only for admission filtering; never saved as
            # canonical submission/version dates in the papers table.
            published_at=release_day, updated_at=release_day,
        )
    return release_day, list(papers.values()), bool(items)


def parse_oai(content):
    root = ET.fromstring(content)
    if root.tag != f"{{{OAI}}}OAI-PMH":
        raise ValueError("Expected an OAI-PMH response")
    error = root.find("o:error", NS)
    if error is not None and error.get("code") != "noRecordsMatch":
        raise ArxivError(error.get("code"), error.text or "")
    page = Page([], response_date=root.findtext("o:responseDate", None, NS))
    token = root.find(".//o:resumptionToken", NS)
    page.next_token = token.text.strip() if token is not None and token.text else None
    for rec in root.findall(".//o:record", NS):
        header = rec.find("o:header", NS)
        if header is None:
            raise ValueError("OAI record has no header")
        stamp = header.findtext("o:datestamp", "", NS)
        page.datestamps.append(stamp)
        if header.get("status") == "deleted":
            page.deleted_ids.append(normalize_id(header.findtext("o:identifier", "", NS))[0])
            continue
        metadata = rec.find("o:metadata", NS)
        if metadata is None or len(metadata) != 1:
            raise ValueError("OAI record has no unique metadata payload")
        payload = metadata[0]
        namespace, kind = payload.tag[1:].split("}")
        if kind not in ("arXiv", "arXivRaw"):
            raise ValueError(f"Unsupported metadata format: {kind}")
        def txt(key, default=None):
            return payload.findtext(f"{{{namespace}}}{key}", default)
        versions = []
        for version in payload.findall(f"{{{namespace}}}version"):
            versions.append({"version": int(version.get("version").lstrip("v")),
                             "date": _date(version.findtext(f"{{{namespace}}}date", ""))})
        versions.sort(key=lambda v: v["version"])
        if kind == "arXivRaw":
            # Raw author names are unstructured; preserve exactly rather than
            # incorrectly splitting group authors or surname-first names.
            authors = [compact(txt("authors", ""))]
        else:
            authors = []
            for author in payload.findall(f"{{{namespace}}}authors/{{{namespace}}}author"):
                key = compact(author.findtext(f"{{{namespace}}}keyname", ""))
                fore = compact(author.findtext(f"{{{namespace}}}forenames", ""))
                suffix = compact(author.findtext(f"{{{namespace}}}suffix", ""))
                authors.append(compact(" ".join([fore, key, suffix])))
        page.papers.append(Paper(
            arxiv_id=normalize_id(txt("id", ""))[0],
            version=versions[-1]["version"] if versions else None,
            title=compact(txt("title", "")), abstract=compact(txt("abstract", "")),
            authors=authors, categories=["eess.SY" if c == "cs.SY" else c for c in txt("categories", "").split()],
            published_at=versions[0]["date"] if versions else _date(txt("created", "")),
            updated_at=versions[-1]["date"] if versions else _date(txt("updated", txt("created", ""))),
            doi=txt("doi"), license=txt("license"), comments=txt("comments"),
            journal_ref=txt("journal-ref"), datestamp=stamp, versions=versions,
        ))
    return page
