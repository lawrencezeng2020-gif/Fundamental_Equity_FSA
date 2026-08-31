"""Typed access to specific SEC EDGAR endpoints: tickers, submissions, companyfacts.

Phase 1 scope only: this resolves the three endpoints into lightly-typed
documents and does **not** interpret XBRL facts (no statement building, no
tag mapping, no LTM -- that is Phase 2, per the task brief's scope boundary).

Endpoints:
    - Ticker -> CIK: ``https://www.sec.gov/files/company_tickers.json``
      (large, changes rarely -- cached like any other response).
    - ``submissions``: ``https://data.sec.gov/submissions/CIK##########.json``
      -- company metadata (fiscal year end, SIC code) and filing history.
    - ``companyfacts``: ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json``
      -- full tagged XBRL fact history, returned as-is (``raw``) for Phase 2
      to consume; this module only surfaces namespace/tag *names*.

Class-share tickers (confirmed empirically against the live endpoint while
building this module): SEC's ``company_tickers.json`` represents them with a
**hyphen**, e.g. ``BRK-B`` / ``BRK-A`` for Berkshire Hathaway -- never a dot.
Retail/press convention often writes ``BRK.B``. ``resolve_cik`` normalizes
both directions (dot<->hyphen) so either spelling resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fsa.sec.client import SecClient
from fsa.sec.errors import TickerNotFound

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik}.json"
_COMPANYFACTS_URL_TMPL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


@dataclass(frozen=True)
class CompanyRef:
    """A resolved ticker -> company identity."""

    ticker: str
    cik: str  # 10-digit, zero-padded
    name: str


@dataclass(frozen=True)
class FilingRef:
    """One entry from a company's SEC filing history."""

    form: str
    accession: str
    filing_date: date
    period_end: date | None
    primary_doc_url: str


@dataclass(frozen=True)
class SubmissionsDoc:
    """Lightly-typed view over the ``submissions`` payload.

    ``raw`` is retained in full so later phases (or the ``Info`` sheet) can
    reach fields this module doesn't surface, without a second fetch.

    ``tickers`` and ``former_names`` are surfaced as a detection *signal*,
    not a verdict -- PLANNING.md Section 5.3.11 (added after the XOM finding
    during Phase 1: SEC's ticker map can point at a freshly created
    registrant CIK holding almost no history, while the CIK with the real
    multi-decade history has an empty ``tickers`` list and isn't reachable
    by ticker lookup at all). An empty ``tickers`` list here is the tell.
    Phase 2 owns deciding what to do with that (e.g. raising
    ``REGISTRANT_CHANGE_SUSPECTED``) and PLANNING.md Section 12 explicitly
    defers automatic predecessor-CIK discovery -- this module only exposes
    the raw signal, it does not interpret it.
    """

    raw: dict
    cik: str
    entity_name: str
    sic: str | None
    sic_description: str | None
    fiscal_year_end: str | None  # "MMDD" as reported, e.g. "0930"
    tickers: list[str]  # from submissions.tickers; empty is the registrant-change tell
    former_names: list[dict]  # raw submissions.formerNames entries ({"name", "from", "to"}), as-is
    recent_filings: list[FilingRef]


@dataclass(frozen=True)
class CompanyFactsDoc:
    """Lightly-typed view over the ``companyfacts`` payload.

    Deliberately does **not** interpret facts -- ``tags()`` returns tag
    *names* only, for the namespace investigation and for Phase 2 to know
    what's available before it does any mapping. ``raw`` carries the full
    fact history through untouched.
    """

    raw: dict
    cik: str
    entity_name: str
    namespaces: list[str]  # e.g. ["dei", "us-gaap"] -- see PLANNING.md Section 5.3.8

    def tags(self, namespace: str) -> list[str]:
        """Tag names present under ``namespace``, sorted. Empty list if the
        namespace isn't present at all (never raises for an unknown one --
        "this filer doesn't use srt" is normal, not an error)."""
        return sorted(self.raw.get("facts", {}).get(namespace, {}).keys())


def _pad_cik(raw: object) -> str:
    return str(raw).strip().zfill(10)


def _primary_doc_url(cik_padded: str, accession: str, primary_document: str) -> str:
    if not primary_document:
        return ""
    cik_no_leading_zeros = str(int(cik_padded))
    accession_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_no_leading_zeros}/"
        f"{accession_no_dashes}/{primary_document}"
    )


def resolve_cik(client: SecClient, ticker: str) -> CompanyRef:
    """Resolve a ticker symbol to its company identity via SEC's ticker map.

    Case-insensitive. Class-share tickers are matched regardless of whether
    the caller spells the separator as a dot or a hyphen (``BRK.B`` and
    ``BRK-B`` both resolve) -- SEC's own map uses the hyphen form.

    Raises:
        TickerNotFound: the ticker (in any spelling variant tried) does not
            appear in SEC's map.
    """
    result = client.get_json(COMPANY_TICKERS_URL, cache_key="company_tickers")
    by_ticker: dict[str, dict] = {}
    for entry in result.data.values():
        by_ticker[str(entry["ticker"]).upper()] = entry

    normalized = ticker.strip().upper()
    candidates = [normalized]
    if "." in normalized:
        candidates.append(normalized.replace(".", "-"))
    if "-" in normalized:
        candidates.append(normalized.replace("-", "."))

    for candidate in candidates:
        entry = by_ticker.get(candidate)
        if entry is not None:
            return CompanyRef(
                ticker=str(entry["ticker"]),
                cik=_pad_cik(entry["cik_str"]),
                name=str(entry["title"]),
            )
    raise TickerNotFound(ticker)


def _parse_recent_filings(cik_padded: str, recent: dict) -> list[FilingRef]:
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_documents = recent.get("primaryDocument", [])

    filings: list[FilingRef] = []
    for i in range(len(forms)):
        report_date_raw = report_dates[i] if i < len(report_dates) else ""
        primary_document = primary_documents[i] if i < len(primary_documents) else ""
        accession = accessions[i]
        filings.append(
            FilingRef(
                form=forms[i],
                accession=accession,
                filing_date=date.fromisoformat(filing_dates[i]),
                period_end=date.fromisoformat(report_date_raw) if report_date_raw else None,
                primary_doc_url=_primary_doc_url(cik_padded, accession, primary_document),
            )
        )
    return filings


def fetch_submissions(client: SecClient, cik: str) -> SubmissionsDoc:
    """Fetch and lightly parse the ``submissions`` document for ``cik``."""
    padded = _pad_cik(cik)
    url = _SUBMISSIONS_URL_TMPL.format(cik=padded)
    result = client.get_json(url, cache_key=f"submissions:{padded}")
    raw = result.data
    recent = raw.get("filings", {}).get("recent", {})
    return SubmissionsDoc(
        raw=raw,
        cik=padded,
        # NOTE: the submissions payload's company-name field is `name`, not
        # `entityName` -- that's the companyfacts payload's key. Confirmed
        # against a live response while building this module; easy to get
        # backwards since both endpoints describe the same company.
        entity_name=str(raw.get("name") or ""),
        sic=raw.get("sic"),
        sic_description=raw.get("sicDescription"),
        fiscal_year_end=raw.get("fiscalYearEnd"),
        tickers=list(raw.get("tickers") or []),
        former_names=list(raw.get("formerNames") or []),
        recent_filings=_parse_recent_filings(padded, recent),
    )


def fetch_company_facts(client: SecClient, cik: str) -> CompanyFactsDoc:
    """Fetch and lightly parse the ``companyfacts`` document for ``cik``.

    Returns raw fact dictionaries plus namespace/tag metadata only -- no
    fact interpretation. Statement-building is Phase 2's job.
    """
    padded = _pad_cik(cik)
    url = _COMPANYFACTS_URL_TMPL.format(cik=padded)
    result = client.get_json(url, cache_key=f"companyfacts:{padded}")
    raw = result.data
    namespaces = sorted(raw.get("facts", {}).keys())
    return CompanyFactsDoc(
        raw=raw,
        cik=padded,
        entity_name=str(raw.get("entityName") or ""),
        namespaces=namespaces,
    )
