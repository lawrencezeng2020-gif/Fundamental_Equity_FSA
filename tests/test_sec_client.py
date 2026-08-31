"""Tests for fsa.sec.client.SecClient: headers, retries, and cache policy.

All HTTP is faked via a tiny in-process fake `requests.Session` -- no network
access, no `responses`/`requests-mock` dependency needed. Real
`requests.Response` objects are constructed directly (status_code, headers,
_content) since that's the object `SecClient` actually consumes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from fsa.config import Settings
from fsa.sec.cache import ResponseCache
from fsa.sec.client import FetchResult, SecClient
from fsa.sec.errors import SecError, SecHttpError, SecRateLimited, SecUnavailable


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        sec_user_agent="Test Runner test.runner@example.com",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        historical_years=10,
        projection_years=5,
        rate_limit_rps=5,
        cache_ttl_hours=24,
        source_path=tmp_path / ".fsa.toml",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def make_response(status_code: int, *, json_body: object = None, headers: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    if json_body is not None:
        resp._content = json.dumps(json_body).encode("utf-8")
    else:
        resp._content = b""
    resp.url = "https://example.invalid/fake"
    return resp


class FakeSession:
    """Replays a canned sequence of responses/exceptions, recording every call."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001 - matches requests.Session.get
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if not self._responses:
            raise AssertionError(f"FakeSession received an unexpected extra request: {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


def make_client(tmp_path, session, *, sleeps=None, rand=lambda: 0.0, **settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    sleep_calls = sleeps if sleeps is not None else []

    def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    client = SecClient(
        settings,
        session=session,
        clock=lambda: 0.0,
        sleep=_sleep,
        rand=rand,
    )
    return client, sleep_calls


# -- User-Agent refusal ---------------------------------------------------


def test_missing_user_agent_raises_before_any_socket_is_opened(tmp_path, monkeypatch):
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("requests.Session() must never be constructed without a User-Agent")

    monkeypatch.setattr("fsa.sec.client.requests.Session", _must_not_be_called)
    settings = make_settings(tmp_path, sec_user_agent="")

    with pytest.raises(SecError, match="sec_user_agent"):
        SecClient(settings)


def test_blank_whitespace_user_agent_also_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fsa.sec.client.requests.Session",
        lambda: (_ for _ in ()).throw(AssertionError("must not open a session")),
    )
    settings = make_settings(tmp_path, sec_user_agent="   ")
    with pytest.raises(SecError):
        SecClient(settings)


def test_every_request_carries_user_agent_and_gzip_headers(tmp_path):
    session = FakeSession([make_response(200, json_body={"ok": True})])
    client, _ = make_client(tmp_path, session)

    client.get_json("https://data.sec.gov/fake.json", cache_key="k1")

    assert len(session.calls) == 1
    headers = session.calls[0]["headers"]
    assert headers["User-Agent"] == "Test Runner test.runner@example.com"
    assert headers["Accept-Encoding"] == "gzip"


# -- Cache policy: miss / hit / TTL expiry / 304 revalidation --------------


def test_cache_miss_fetches_live_and_populates_cache(tmp_path):
    session = FakeSession(
        [make_response(200, json_body={"hello": "world"}, headers={"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"})]
    )
    client, _ = make_client(tmp_path, session)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    assert isinstance(result, FetchResult)
    assert result.data == {"hello": "world"}
    assert result.from_cache is False
    assert result.revalidated is False
    assert result.etag == '"v1"'

    cache = ResponseCache(make_settings(tmp_path).cache_dir)
    entry = cache.get("mykey")
    assert entry is not None
    assert entry.body == {"hello": "world"}
    assert entry.etag == '"v1"'


def test_fresh_cache_hit_makes_zero_network_calls(tmp_path):
    settings = make_settings(tmp_path)
    cache = ResponseCache(settings.cache_dir)
    now = datetime.now(timezone.utc)
    cache.put(
        "mykey",
        body={"cached": True},
        url="https://data.sec.gov/fake.json",
        fetched_at=now,
        last_validated=now,
        etag='"v1"',
        last_modified=None,
    )

    session = FakeSession([])  # any .get() call fails the test
    client, _ = make_client(tmp_path, session)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    assert session.calls == []
    assert result.data == {"cached": True}
    assert result.from_cache is True
    assert result.revalidated is False
    assert result.fetched_at == now


def test_no_cache_flag_bypasses_reads_and_writes(tmp_path):
    settings = make_settings(tmp_path)
    cache = ResponseCache(settings.cache_dir)
    now = datetime.now(timezone.utc)
    cache.put(
        "mykey", body={"cached": True}, url="u", fetched_at=now, last_validated=now, etag=None, last_modified=None
    )

    session = FakeSession([make_response(200, json_body={"live": True})])
    settings2 = make_settings(tmp_path)

    def _sleep(_):
        pass

    client = SecClient(settings2, session=session, use_cache=False, clock=lambda: 0.0, sleep=_sleep)
    result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    # Bypasses the existing cache entry entirely and fetches live...
    assert result.data == {"live": True}
    assert result.from_cache is False
    # ...and must not have overwritten the cache entry either.
    entry_after = cache.get("mykey")
    assert entry_after.body == {"cached": True}


def test_ttl_expiry_triggers_conditional_get_and_304_revalidation(tmp_path, caplog):
    settings = make_settings(tmp_path, cache_ttl_hours=1)
    cache = ResponseCache(settings.cache_dir)
    stale_time = datetime.now(timezone.utc) - timedelta(hours=5)
    cache.put(
        "mykey",
        body={"original": "body"},
        url="https://data.sec.gov/fake.json",
        fetched_at=stale_time,
        last_validated=stale_time,
        etag='"etag-v1"',
        last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
    )

    session = FakeSession([make_response(304, headers={"ETag": '"etag-v1"'})])
    client, _ = make_client(tmp_path, session, cache_ttl_hours=1)

    with caplog.at_level(logging.INFO, logger="fsa.sec.client"):
        result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    # The conditional headers were actually sent.
    assert len(session.calls) == 1
    sent_headers = session.calls[0]["headers"]
    assert sent_headers["If-None-Match"] == '"etag-v1"'
    assert sent_headers["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"

    # A 304 serves the old body, sets revalidated=True, and does NOT update
    # fetched_at (no new body was transferred).
    assert result.data == {"original": "body"}
    assert result.from_cache is True
    assert result.revalidated is True
    assert result.fetched_at == stale_time

    # The cache's freshness clock (last_validated) has been refreshed even
    # though the body did not change -- demonstrated by re-reading and
    # checking it's no longer stale under the same TTL.
    refreshed_entry = cache.get("mykey")
    assert refreshed_entry.fetched_at == stale_time
    assert refreshed_entry.last_validated > stale_time

    # Real log line proving the 304 revalidation, per the Phase 1 acceptance
    # criteria ("conditional GET demonstrably produces a 304").
    assert any("revalidated (304)" in rec.message for rec in caplog.records)


def test_ttl_expiry_with_200_response_repopulates_cache(tmp_path):
    settings = make_settings(tmp_path, cache_ttl_hours=1)
    cache = ResponseCache(settings.cache_dir)
    stale_time = datetime.now(timezone.utc) - timedelta(hours=5)
    cache.put(
        "mykey", body={"old": "body"}, url="u", fetched_at=stale_time, last_validated=stale_time,
        etag='"old-etag"', last_modified=None,
    )

    session = FakeSession([make_response(200, json_body={"new": "body"}, headers={"ETag": '"new-etag"'})])
    client, _ = make_client(tmp_path, session, cache_ttl_hours=1)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    assert result.data == {"new": "body"}
    assert result.from_cache is False
    assert result.revalidated is False

    entry = cache.get("mykey")
    assert entry.body == {"new": "body"}
    assert entry.etag == '"new-etag"'


def test_refresh_flag_forces_revalidation_even_within_ttl(tmp_path):
    settings = make_settings(tmp_path, cache_ttl_hours=24)
    cache = ResponseCache(settings.cache_dir)
    fresh_time = datetime.now(timezone.utc)  # well within a 24h TTL
    cache.put(
        "mykey", body={"cached": True}, url="u", fetched_at=fresh_time, last_validated=fresh_time,
        etag='"v1"', last_modified=None,
    )

    session = FakeSession([make_response(304)])
    settings2 = make_settings(tmp_path, cache_ttl_hours=24)

    def _sleep(_):
        pass

    client = SecClient(settings2, session=session, force_refresh=True, clock=lambda: 0.0, sleep=_sleep)
    result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    # --refresh must still issue a network call despite the entry being fresh.
    assert len(session.calls) == 1
    assert result.revalidated is True


def test_corrupt_cache_entry_degrades_to_live_fetch(tmp_path, caplog):
    settings = make_settings(tmp_path)
    cache_dir = settings.cache_dir
    cache_dir.mkdir(parents=True)
    cache = ResponseCache(cache_dir)
    bad_path = cache._path("mykey")
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"not a gzip file")

    session = FakeSession([make_response(200, json_body={"live": "data"})])
    client, _ = make_client(tmp_path, session)

    with caplog.at_level(logging.WARNING, logger="fsa.sec.cache"):
        result = client.get_json("https://data.sec.gov/fake.json", cache_key="mykey")

    assert result.data == {"live": "data"}
    assert result.from_cache is False
    assert any("corrupt" in rec.message.lower() for rec in caplog.records)


# -- Retries ----------------------------------------------------------------


def test_429_retried_and_honors_retry_after_header(tmp_path):
    session = FakeSession(
        [
            make_response(429, headers={"Retry-After": "2"}),
            make_response(200, json_body={"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client, sleeps = make_client(tmp_path, session, sleeps=sleeps)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert result.data == {"ok": True}
    assert len(session.calls) == 2
    assert sleeps == [2.0]  # Retry-After is authoritative, not computed backoff


def test_5xx_retried_with_exponential_backoff(tmp_path):
    session = FakeSession(
        [
            make_response(503),
            make_response(502),
            make_response(200, json_body={"ok": True}),
        ]
    )
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert result.data == {"ok": True}
    assert len(session.calls) == 3
    assert len(sleeps) == 2
    # base=1s for attempt 1, base=2s for attempt 2 (rand=0 => no jitter)
    assert sleeps[0] == pytest.approx(1.0)
    assert sleeps[1] == pytest.approx(2.0)


def test_404_is_never_retried(tmp_path):
    session = FakeSession([make_response(404)])
    client, sleeps = make_client(tmp_path, session)

    with pytest.raises(SecHttpError) as excinfo:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert excinfo.value.status_code == 404
    assert len(session.calls) == 1
    assert sleeps == []


def test_403_is_retried_once_then_succeeds(tmp_path):
    session = FakeSession(
        [
            make_response(403, json_body={"message": "throttled"}),
            make_response(200, json_body={"ok": True}),
        ]
    )
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    result = client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert result.data == {"ok": True}
    assert len(session.calls) == 2
    assert len(sleeps) == 1


def test_403_is_capped_at_two_attempts_not_five(tmp_path):
    """403 is also exactly what a rejected/missing User-Agent produces, so it
    must not burn through the full 5-attempt budget used for 429/5xx -- it is
    capped at 2 attempts (PLANNING.md Section 4, revised after Phase 1
    review)."""
    session = FakeSession([make_response(403) for _ in range(5)])
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecHttpError) as excinfo:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert excinfo.value.status_code == 403
    assert len(session.calls) == 2  # not 5
    assert len(sleeps) == 1


def test_terminal_403_error_includes_response_body_snippet(tmp_path):
    body = {"message": "Request denied: missing or invalid User-Agent header, see fair access policy."}
    session = FakeSession([make_response(403, json_body=body), make_response(403, json_body=body)])
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecHttpError) as excinfo:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    message = str(excinfo.value)
    assert "missing or invalid User-Agent" in message


def test_terminal_403_body_snippet_is_truncated_and_single_line(tmp_path):
    long_text = "x" * 500
    session = FakeSession(
        [
            make_response(403, json_body={"message": long_text}),
            make_response(403, json_body={"message": long_text}),
        ]
    )
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecHttpError) as excinfo:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    message = str(excinfo.value)
    assert "\n" not in message
    # The raw 500-char payload must not appear verbatim/untruncated.
    assert long_text not in message


def test_429_exhausting_all_retries_raises_sec_rate_limited(tmp_path):
    session = FakeSession([make_response(429) for _ in range(5)])
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecRateLimited):
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert len(session.calls) == 5
    assert len(sleeps) == 4  # backoff happens between attempts, not after the last


def test_5xx_exhausting_all_retries_raises_sec_unavailable(tmp_path):
    session = FakeSession([make_response(500) for _ in range(5)])
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecUnavailable):
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert len(session.calls) == 5


def test_connection_error_is_retried_then_raises_sec_unavailable(tmp_path):
    session = FakeSession([requests.exceptions.ConnectionError("boom") for _ in range(5)])
    client, sleeps = make_client(tmp_path, session, rand=lambda: 0.0)

    with pytest.raises(SecUnavailable):
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert len(session.calls) == 5
    assert len(sleeps) == 4


def test_non_retryable_4xx_raises_immediately(tmp_path):
    session = FakeSession([make_response(400)])
    client, sleeps = make_client(tmp_path, session)

    with pytest.raises(SecHttpError) as excinfo:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert excinfo.value.status_code == 400
    assert len(session.calls) == 1
    assert sleeps == []


# -- Context manager ---------------------------------------------------


def test_context_manager_closes_a_self_created_session(tmp_path, monkeypatch):
    """When SecClient creates its own requests.Session (no `session=` kwarg
    passed), closing the client (directly or via context manager) must close
    that session."""
    closed = {"value": False}

    class TrackingSession(FakeSession):
        def __init__(self):
            super().__init__([make_response(200, json_body={"ok": True})])

        def close(self):
            closed["value"] = True

    monkeypatch.setattr("fsa.sec.client.requests.Session", TrackingSession)
    settings = make_settings(tmp_path)

    def _sleep(_):
        pass

    with SecClient(settings, clock=lambda: 0.0, sleep=_sleep) as client:
        client.get_json("https://data.sec.gov/fake.json", cache_key="k")

    assert closed["value"] is True


def test_close_does_not_close_a_caller_provided_session(tmp_path):
    """A session passed in via `session=` is owned by the caller; SecClient
    must not close it out from under them."""
    closed = {"value": False}

    class TrackingSession(FakeSession):
        def close(self):
            closed["value"] = True

    session = TrackingSession([make_response(200, json_body={"ok": True})])
    client, _ = make_client(tmp_path, session)
    client.get_json("https://data.sec.gov/fake.json", cache_key="k")
    client.close()

    assert closed["value"] is False
