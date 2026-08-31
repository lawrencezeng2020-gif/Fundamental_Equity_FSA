"""Revalidating response cache for SEC EDGAR JSON responses.

PLANNING.md Section 2: "cache, don't warehouse". This stores raw response
bodies plus enough metadata (``ETag`` / ``Last-Modified`` / fetch timestamps)
for ``fsa.sec.client.SecClient`` to issue conditional GETs and enforce the
configured TTL. It has no opinion on freshness policy -- that lives in
``SecClient.get_json`` -- this module is pure storage.

**Backend choice: gzipped JSON files, one per cache key, not SQLite.**
Justification (see the Phase 1 report for the fuller version): the access
pattern is whole-document get/put keyed by a handful of well-known strings
("tickers", "submissions:<cik>", "companyfacts:<cik>") -- there is no
querying across entries, no partial updates, and no concurrent-writer
contention to speak of (one CLI process at a time). A directory of gzipped
JSON files is trivially inspectable (``zcat`` a file to see exactly what was
cached, which matters for the reproducibility/provenance goals in
PLANNING.md Section 2), requires no schema migration story, and degrades
safely: a single corrupt file cannot corrupt anything else, whereas a
damaged SQLite file can jeopardize the whole cache. SQLite would only start
to pay for itself if we needed cross-entry queries or fine-grained eviction,
neither of which applies here.

Corrupt or unreadable entries degrade to a cache miss (never an exception) --
the caller re-fetches live, per the acceptance criteria in the task brief.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("fsa.sec.cache")

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class CacheEntry:
    """A previously cached response, as stored on disk."""

    body: dict
    url: str
    fetched_at: datetime
    last_validated: datetime
    etag: str | None
    last_modified: str | None


class ResponseCache:
    """On-disk revalidating cache for SEC API responses.

    One gzipped JSON file per cache key under ``cache_dir``. Filenames are a
    sanitized version of the cache key plus a short hash suffix (for
    human-inspectability with no realistic collision risk, since cache keys
    are all constructed internally by ``fsa.sec.endpoints``, never taken
    verbatim from untrusted input).
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir

    def _path(self, cache_key: str) -> Path:
        safe = _SAFE_KEY_RE.sub("_", cache_key)
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:8]
        return self._dir / f"{safe}.{digest}.json.gz"

    def get(self, cache_key: str) -> CacheEntry | None:
        """Return the cached entry for ``cache_key``, or ``None`` on a miss.

        Any failure to read/decompress/parse the entry is treated as a miss
        (logged at WARNING) rather than raised -- a damaged cache file must
        never crash the pipeline; it just costs a live fetch.
        """
        path = self._path(cache_key)
        if not path.is_file():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                raw = json.load(fh)
            return CacheEntry(
                body=raw["body"],
                url=raw["url"],
                fetched_at=datetime.fromisoformat(raw["fetched_at"]),
                last_validated=datetime.fromisoformat(raw["last_validated"]),
                etag=raw.get("etag"),
                last_modified=raw.get("last_modified"),
            )
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(
                "Corrupt or unreadable cache entry for key=%s at %s (%s: %s); "
                "treating as a cache miss and refetching live.",
                cache_key,
                path,
                type(exc).__name__,
                exc,
            )
            return None

    def put(
        self,
        cache_key: str,
        *,
        body: dict,
        url: str,
        fetched_at: datetime,
        last_validated: datetime,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Write (or overwrite) the cache entry for ``cache_key``.

        Writes to a temp file and renames into place so a crash mid-write
        cannot leave a half-written file that would later be read as
        "corrupt" -- ``os.replace`` (via ``Path.replace``) is atomic on the
        same filesystem.
        """
        path = self._path(cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "url": url,
            "fetched_at": fetched_at.isoformat(),
            "last_validated": last_validated.isoformat(),
            "etag": etag,
            "last_modified": last_modified,
            "body": body,
        }
        try:
            with gzip.open(tmp_path, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)
