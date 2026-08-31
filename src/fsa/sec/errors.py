"""Typed exception hierarchy for the SEC transport/caching layer.

``fsa.cli`` catches these (never a bare exception) and maps them to the
process exit codes defined there. Keeping the hierarchy in its own module
(rather than in ``client.py`` or ``endpoints.py``) avoids a circular import,
since both modules need to raise and catch these types.
"""

from __future__ import annotations


class SecError(Exception):
    """Base class for every error raised by ``fsa.sec``."""


class SecHttpError(SecError):
    """SEC EDGAR returned an HTTP error that is not one of the more specific
    subclasses below (e.g. a non-retryable 4xx, or a 404).

    Carries ``status_code`` and ``url`` so ``cli.py`` / logging can report
    specifics without re-parsing the message string.
    """

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class SecRateLimited(SecHttpError):
    """SEC EDGAR returned 429 on every attempt, including retries."""


class SecUnavailable(SecHttpError):
    """SEC EDGAR was unreachable (5xx on every attempt, or a network/connection
    error) after exhausting retries."""


class TickerNotFound(SecError):
    """A ticker symbol does not appear in SEC's ticker->CIK map.

    Raised by ``fsa.sec.endpoints.resolve_cik`` -- this is a lookup failure
    against data already fetched, not an HTTP error, so it is not a
    ``SecHttpError`` subclass.
    """

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        super().__init__(
            f"Ticker {ticker!r} was not found in SEC's company_tickers.json map. "
            "Check the spelling, or that it is a US domestic filer (SEC EDGAR "
            "does not cover foreign private issuers filing 20-F/40-F)."
        )
