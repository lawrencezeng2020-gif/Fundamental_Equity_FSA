"""Tests for fsa.sec.cache.ResponseCache -- pure on-disk storage layer.

Freshness/TTL/revalidation *policy* lives in SecClient (tested in
test_sec_client.py); this file only covers get/put round-tripping and
graceful degradation on a damaged cache entry.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone

from fsa.sec.cache import ResponseCache


def test_get_on_empty_cache_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.get("some-key") is None


def test_put_then_get_round_trips_all_fields(tmp_path):
    cache = ResponseCache(tmp_path)
    fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_validated = datetime(2026, 1, 2, tzinfo=timezone.utc)

    cache.put(
        "submissions:0000320193",
        body={"name": "Apple Inc."},
        url="https://data.sec.gov/submissions/CIK0000320193.json",
        fetched_at=fetched_at,
        last_validated=last_validated,
        etag='"abc123"',
        last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
    )

    entry = cache.get("submissions:0000320193")
    assert entry is not None
    assert entry.body == {"name": "Apple Inc."}
    assert entry.url == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert entry.fetched_at == fetched_at
    assert entry.last_validated == last_validated
    assert entry.etag == '"abc123"'
    assert entry.last_modified == "Wed, 01 Jan 2026 00:00:00 GMT"


def test_put_tolerates_missing_etag_and_last_modified(tmp_path):
    cache = ResponseCache(tmp_path)
    now = datetime.now(timezone.utc)
    cache.put("k", body={"v": 1}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None)

    entry = cache.get("k")
    assert entry.etag is None
    assert entry.last_modified is None


def test_put_overwrites_existing_entry(tmp_path):
    cache = ResponseCache(tmp_path)
    now = datetime.now(timezone.utc)
    cache.put("k", body={"v": 1}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None)
    cache.put("k", body={"v": 2}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None)

    entry = cache.get("k")
    assert entry.body == {"v": 2}


def test_cache_files_are_gzip_and_human_inspectable(tmp_path):
    cache = ResponseCache(tmp_path)
    now = datetime.now(timezone.utc)
    cache.put(
        "companyfacts:0000320193", body={"x": 1}, url="u", fetched_at=now, last_validated=now,
        etag=None, last_modified=None,
    )

    files = list(tmp_path.glob("companyfacts_0000320193.*.json.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["body"] == {"x": 1}


def test_corrupt_gzip_degrades_to_cache_miss_with_warning(tmp_path, caplog):
    cache = ResponseCache(tmp_path)
    now = datetime.now(timezone.utc)
    cache.put("k", body={"v": 1}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None)

    path = cache._path("k")
    path.write_bytes(b"this is not gzip data at all, just garbage bytes")

    with caplog.at_level(logging.WARNING):
        entry = cache.get("k")

    assert entry is None
    assert any("corrupt" in rec.message.lower() for rec in caplog.records)


def test_valid_gzip_but_invalid_json_degrades_to_cache_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    path = cache._path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("not valid json {{{")

    assert cache.get("k") is None


def test_valid_json_missing_required_field_degrades_to_cache_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    path = cache._path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"body": {}}, fh)  # missing url/fetched_at/last_validated

    assert cache.get("k") is None


def test_unparseable_timestamp_degrades_to_cache_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    path = cache._path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(
            {
                "url": "u",
                "fetched_at": "not-a-timestamp",
                "last_validated": "also-not-a-timestamp",
                "etag": None,
                "last_modified": None,
                "body": {},
            },
            fh,
        )

    assert cache.get("k") is None


def test_corrupt_entry_does_not_prevent_a_fresh_put_afterward(tmp_path):
    """Recovery path: after a corrupt read, put()/get() must work normally
    again -- one damaged file must not wedge the cache directory."""
    cache = ResponseCache(tmp_path)
    path = cache._path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage")
    assert cache.get("k") is None

    now = datetime.now(timezone.utc)
    cache.put("k", body={"recovered": True}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None)
    entry = cache.get("k")
    assert entry.body == {"recovered": True}
