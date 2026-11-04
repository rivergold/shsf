import fcntl
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


class HttpClient:
    """One rate-limited lane shared by CLI processes using the same data dir."""
    def __init__(self, data_dir, user_agent, interval=3.1):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.user_agent, self.interval = user_agent, interval

    def get(self, endpoint, params):
        url = endpoint + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                      "Accept": "application/xml,application/atom+xml"})
        with (self.directory / "arxiv-http.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for attempt in range(4):
                lock.seek(0)
                previous = lock.read().strip()
                time.sleep(max(0, self.interval - (time.time() - float(previous or 0))))
                lock.seek(0)
                lock.truncate()
                lock.write(str(time.time()))
                lock.flush()
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        body = response.read(32 * 1024 * 1024 + 1)
                    if len(body) > 32 * 1024 * 1024:
                        raise RuntimeError("arXiv response exceeds the 32 MiB safety limit")
                    return body, url
                except urllib.error.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise
                    after = exc.headers.get("Retry-After", "")
                    try:
                        delay = float(after)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(after) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            delay = 0
                    # If the requested pause is too long for an interactive
                    # command, stop; never cap it and retry too early.
                    if delay > 300:
                        raise RuntimeError(f"arXiv requested a {delay:.0f}s pause; retry this command later") from exc
                    wait = max(delay, 5 * 2 ** attempt)
                    print(json.dumps({"event": "arxiv_retry", "attempt": attempt + 1,
                                      "http_status": exc.code, "wait_seconds": wait}), file=sys.stderr, flush=True)
                    time.sleep(wait)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if attempt == 3:
                        raise
                    wait = 5 * 2 ** attempt
                    print(json.dumps({"event": "arxiv_retry", "attempt": attempt + 1,
                                      "error": type(exc).__name__, "wait_seconds": wait}), file=sys.stderr, flush=True)
                    time.sleep(wait)


def archive_response(directory, body, url):
    import hashlib
    import uuid
    from .models import utcnow
    folder = Path(directory) / "raw" / "arxiv"
    folder.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(body).hexdigest()
    target = folder / (content_hash + ".xml")
    if not target.exists():
        temporary = folder / (uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, target)
    # Each request has a separate manifest even when content is deduplicated.
    manifest = folder / (uuid.uuid4().hex + ".json")
    manifest.write_text(json.dumps({"url": url, "fetched_at": utcnow(), "sha256": content_hash,
                                    "response_path": str(target.resolve())}, indent=2))
    return target.resolve()
