from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def compact(text):
    return " ".join((text or "").split())


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_id(value):
    value = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf|html|src)/", "", value.strip())
    value = re.sub(r"^(?:oai:arXiv.org:|arxiv:)", "", value, flags=re.I)
    value = re.sub(r"\.pdf$", "", value.split("?")[0].split("#")[0])
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]+/\d{7})(?:v([1-9]\d*))?", value)
    if not match:
        raise ValueError(f"Invalid arXiv ID: {value!r}")
    return match[1], int(match[2]) if match[2] else None


@dataclass
class Paper:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: str
    updated_at: str
    version: int | None = None
    doi: str | None = None
    license: str | None = None
    comments: str | None = None
    journal_ref: str | None = None
    datestamp: str | None = None
    html_url: str | None = None
    versions: list[dict] = field(default_factory=list)

    @property
    def input_hash(self):
        # This is the canonical input contract; separators/pooling are versioned
        # separately by the encoder configuration.
        return digest(json_text([self.title, self.abstract]))

    def to_dict(self):
        return asdict(self)
