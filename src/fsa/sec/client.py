"""SEC EDGAR HTTP client: session, rate limiter, retries, conditional GET.

Implements PLANNING.md Section 4's SEC-compliance rules:
    - ``User-Agent: Name email@domain`` on every request, refused *before*
      opening a socket if unset (SEC returns 403 without it).
    - A thread-safe token-bucket rate limiter capped at ``rate_limit_rps``.
    - Exponential backoff with jitter on 429/5xx (and network/connection
      errors), up to 5 attempts, honoring ``Retry-After``. 403 also retries,
      but is capped at 2 attempts (see below). A 404 is never retried.
    - ``Accept-Encoding: gzip``.
    - A single pooled, keep-alive ``requests.Session``.
    - A revalidating response cache (``fsa.sec.cache.ResponseCache``): a
      fresh hit returns without any network call; a stale hit issues a
      conditional GET and either gets a 304 (TTL refreshed, no body
      transferred) or a fresh 200 (cache repopulated). Note: in practice
      ``data.sec.gov`` (``submissions``/``companyfacts``) sends no ``ETag``
      or ``Last-Modified`` at all, so 304 revalidation only ever applies to
      the static ``www.sec.gov/files/company_tickers.json`` -- confirmed
      live and corrected in PLANNING.md Section 2.
    - Every request logged: endpoint, status, bytes, cache hit/miss/
      revalidated, retry attempts (DEBUG under ``--verbose``).

Retry scope (PLANNING.md Section 4, as revised after Phase 1 review): 429/5xx
retry up to 5 attempts. 403 retries too -- SEC has historically used it for
throttling -- but is capped at 2 attempts, because 403 is also exactly what a
bad/missing User-Agent produces, and that is not a transient condition
retrying will fix. The terminal 403 error includes a snippet of the response
body so a UA/policy rejection is diagnosable rather than looking like a
flaky network. 404 is never retried.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

from fsa.config import Settings
from fsa.sec.cache import ResponseCache
from fsa.sec.errors import SecError, SecHttpError, SecRateLimited, SecUnavailable
from fsa.sec.ratelimit import TokenBucketLimiter

logger = logging.getLogger("fsa.sec.client")

_RETRYABLE_STATUSES = {429}
_MAX_ATTEMPTS = 5
_MAX_403_ATTEMPTS = 2
_BODY_SNIPPET_LIMIT = 200
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one logical ``get_json`` call.

    ``fetched_at`` is when the body currently being returned was actually
    retrieved from SEC over the network -- *not* when this call happened to
    serve it. A pure cache hit or a 304 revalidation both return the
    timestamp of the original 200 response, since no new body was
    transferred in either case.
    """

    data: dict
    url: str
    fetched_at: datetime
    from_cache: bool
    revalidated: bool
    etag: str | None


class SecClient:
    """Pooled, rate-limited, retrying, caching SEC EDGAR HTTP client.

    Usable as a context manager::

        with SecClient(settings) as client:
            result = client.get_json(url, cache_key="submissions:CIK0000320193")
    """

    def __init__(
        self,
        settings: Settings,
        *,
        use_cache: bool = True,
        force_refresh: bool = False,
        session: requests.Session | None = None,
        cache: ResponseCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        # Refuse to make any request -- indeed, refuse to even finish
        # constructing the client -- if the User-Agent is missing. This must
        # happen before a `requests.Session` (and therefore any socket) is
        # created. In normal operation `fsa.config.load_settings` already
        # guarantees a valid, non-empty `sec_user_agent`; this is defense in
        # depth for callers that construct a `Settings` directly (tests, or
        # future callers) bypassing that validation.
        if not settings.sec_user_agent or not settings.sec_user_agent.strip():
            raise SecError(
                "Refusing to contact SEC EDGAR: sec_user_agent is not configured. "
                "SEC returns HTTP 403 for every request without a "
                "'Name email@domain' User-Agent header (PLANNING.md Section 4)."
            )

        self._settings = settings
        self._use_cache = use_cache
        self._force_refresh = force_refresh
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._cache = cache if cache is not None else ResponseCache(settings.cache_dir)
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._limiter = TokenBucketLimiter(settings.rate_limit_rps, clock=clock, sleep=sleep)

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    # -- public API ---------------------------------------------------

    def get_json(self, url: str, *, cache_key: str) -> FetchResult:
        """Fetch a JSON document, transparently applying the cache policy.

        Freshness policy (PLANNING.md Section 2):
            - Cache disabled (``use_cache=False``): always a live fetch, no
              read or write of the cache.
            - Cache enabled, entry present, not force-refreshing, and within
              ``cache_ttl_hours``: served straight from cache, no network
              call at all.
            - Cache enabled and (entry stale OR ``force_refresh=True``): a
              conditional GET is issued if we have an ``ETag``/
              ``Last-Modified`` to revalidate against; a 304 refreshes the
              cache's freshness clock without transferring a body, a 200
              repopulates it.
            - Cache enabled, no entry: a plain live fetch, then a cache
              write (unless ``use_cache`` writes are also suppressed, which
              they are not independently of reads in this implementation --
              ``--no-cache`` bypasses both per the task brief).
        """
        entry = self._cache.get(cache_key) if self._use_cache else None
        now = datetime.now(timezone.utc)

        if entry is not None and not self._force_refresh:
            age_hours = (now - entry.last_validated).total_seconds() / 3600.0
            if age_hours < self._settings.cache_ttl_hours:
                logger.info(
                    "cache hit key=%s url=%s age_h=%.2f ttl_h=%s",
                    cache_key,
                    url,
                    age_hours,
                    self._settings.cache_ttl_hours,
                )
                return FetchResult(
                    data=entry.body,
                    url=entry.url,
                    fetched_at=entry.fetched_at,
                    from_cache=True,
                    revalidated=False,
                    etag=entry.etag,
                )
            logger.debug(
                "cache stale key=%s url=%s age_h=%.2f ttl_h=%s; revalidating",
                cache_key,
                url,
                age_hours,
                self._settings.cache_ttl_hours,
            )
        elif entry is not None:
            logger.debug("force-refresh requested key=%s url=%s; revalidating", cache_key, url)
        else:
            logger.info("cache miss key=%s url=%s", cache_key, url)

        headers = self._base_headers()
        if entry is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified

        response = self._request_with_retries(url, headers=headers)

        if response.status_code == 304:
            assert entry is not None  # a 304 is impossible without conditional headers
            etag = response.headers.get("ETag", entry.etag)
            last_modified = response.headers.get("Last-Modified", entry.last_modified)
            if self._use_cache:
                self._cache.put(
                    cache_key,
                    body=entry.body,
                    url=url,
                    fetched_at=entry.fetched_at,
                    last_validated=now,
                    etag=etag,
                    last_modified=last_modified,
                )
            logger.info("revalidated (304) key=%s url=%s bytes=0", cache_key, url)
            return FetchResult(
                data=entry.body,
                url=url,
                fetched_at=entry.fetched_at,
                from_cache=True,
                revalidated=True,
                etag=etag,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise SecHttpError(
                f"SEC response for {url} was not valid JSON: {exc}", status_code=response.status_code, url=url
            ) from exc

        fetched_at = now
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if self._use_cache:
            self._cache.put(
                cache_key,
                body=body,
                url=url,
                fetched_at=fetched_at,
                last_validated=fetched_at,
                etag=etag,
                last_modified=last_modified,
            )
        logger.info(
            "fetched (200) key=%s url=%s bytes=%d", cache_key, url, len(response.content)
        )
        return FetchResult(
            data=body,
            url=url,
            fetched_at=fetched_at,
            from_cache=False,
            revalidated=False,
            etag=etag,
        )

    # -- internals ------------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._settings.sec_user_agent,
            "Accept-Encoding": "gzip",
        }

    def _request_with_retries(self, url: str, *, headers: dict[str, str]) -> requests.Response:
        last_exc: Exception | None = None
        attempts_403 = 0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._limiter.acquire()
            try:
                response = self._session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "network error attempt=%d/%d url=%s error=%s", attempt, _MAX_ATTEMPTS, url, exc
                )
                if attempt == _MAX_ATTEMPTS:
                    raise SecUnavailable(
                        f"Network error contacting SEC EDGAR after {attempt} attempts: {exc}", url=url
                    ) from exc
                self._sleep_backoff(attempt, retry_after_header=None)
                continue

            status = response.status_code

            if status == 404:
                # Never retried -- a 404 means the resource genuinely doesn't
                # exist (e.g. a stale/invalid CIK), not a transient failure.
                raise SecHttpError(f"SEC returned 404 Not Found for {url}", status_code=404, url=url)

            if status == 403:
                # 403 is capped at _MAX_403_ATTEMPTS (2), independent of the
                # 5-attempt budget for 429/5xx: SEC has historically returned
                # 403 for throttling (transient), but 403 is also exactly
                # what a rejected/missing User-Agent produces (permanent).
                # Retrying that a full 5 times would just be slow to fail.
                attempts_403 += 1
                logger.warning(
                    "retryable status=403 attempt=%d/%d url=%s", attempts_403, _MAX_403_ATTEMPTS, url
                )
                if attempts_403 >= _MAX_403_ATTEMPTS or attempt == _MAX_ATTEMPTS:
                    snippet = self._body_snippet(response)
                    raise SecHttpError(
                        f"SEC returned HTTP 403 for {url} after {attempts_403} attempt(s). "
                        "403 means either transient throttling or a rejected/missing "
                        "User-Agent (check sec_user_agent if this persists). "
                        f"Response body: {snippet!r}",
                        status_code=403,
                        url=url,
                    )
                self._sleep_backoff(attempt, retry_after_header=response.headers.get("Retry-After"))
                continue

            if status in _RETRYABLE_STATUSES or 500 <= status < 600:
                logger.warning(
                    "retryable status=%d attempt=%d/%d url=%s", status, attempt, _MAX_ATTEMPTS, url
                )
                if attempt == _MAX_ATTEMPTS:
                    error_cls = SecRateLimited if status == 429 else SecUnavailable
                    raise error_cls(
                        f"SEC returned HTTP {status} for {url} after {attempt} attempts",
                        status_code=status,
                        url=url,
                    )
                self._sleep_backoff(attempt, retry_after_header=response.headers.get("Retry-After"))
                continue

            if status == 304 or response.ok:
                return response

            raise SecHttpError(
                f"SEC returned unexpected HTTP {status} for {url}", status_code=status, url=url
            )

        # Unreachable: the loop above always returns or raises on the final
        # attempt. Kept as a defensive fallback so a future refactor that
        # breaks that invariant fails loudly instead of returning None.
        raise SecUnavailable(f"Exhausted retries contacting {url} with no response", url=url) from last_exc

    def _sleep_backoff(self, attempt: int, *, retry_after_header: str | None) -> None:
        delay = None
        if retry_after_header:
            delay = self._parse_retry_after(retry_after_header)
        if delay is None:
            delay = self._compute_backoff(attempt)
        logger.debug("backing off %.2fs before retry (attempt %d/%d)", delay, attempt, _MAX_ATTEMPTS)
        self._sleep(delay)

    def _compute_backoff(self, attempt: int) -> float:
        base = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        jitter = base * 0.25 * self._rand()
        return base + jitter

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        # Retry-After is either an integer number of seconds, or an HTTP-date.
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _body_snippet(response: requests.Response, limit: int = _BODY_SNIPPET_LIMIT) -> str:
        """A short, single-line excerpt of a response body for error messages.

        Used on a terminal 403 so a User-Agent/policy rejection is
        diagnosable from the error text itself, rather than looking
        indistinguishable from a flaky network failure. Decodes raw bytes
        directly (never touches ``response.text``'s encoding-sniffing) since
        this only needs to be readable, not exact -- and must never itself
        raise on a body that isn't valid text.
        """
        try:
            content = response.content
        except Exception:  # noqa: BLE001 - this is a best-effort diagnostic, never fatal
            return ""
        encoding = response.encoding or "utf-8"
        try:
            text = content.decode(encoding, errors="replace")
        except LookupError:
            text = content.decode("utf-8", errors="replace")
        text = " ".join(text.split())  # collapse whitespace/newlines to one line
        if len(text) > limit:
            text = text[:limit] + "..."
        return text
