"""Tests for fsa.sec.endpoints: ticker resolution, submissions, companyfacts.

Exercises the real `SecClient` against a `FakeSession` serving the trimmed,
committed fixtures under tests/fixtures/ -- offline, deterministic, and a
faithful end-to-end path (client + endpoints together) rather than mocking
`get_json` itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from fsa.config import Settings
from fsa.sec.client import SecClient
from fsa.sec.endpoints import (
    COMPANY_TICKERS_URL,
    CompanyFactsDoc,
    CompanyRef,
    SubmissionsDoc,
    fetch_company_facts,
    fetch_submissions,
    resolve_cik,
)
from fsa.sec.errors import TickerNotFound

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        sec_user_agent="Test Runner test.runner@example.com",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        historical_years=10,
        projection_years=5,
        rate_limit_rps=5,
        cache_ttl_hours=24,
        source_path=tmp_path / ".fsa.toml",
    )


def make_response(json_body: dict) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp.headers = requests.structures.CaseInsensitiveDict({})
    resp._content = json.dumps(json_body).encode("utf-8")
    resp.url = "https://example.invalid"
    return resp


class FakeSession:
    def __init__(self, url_to_body: dict[str, dict]) -> None:
        self._url_to_body = url_to_body
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append(url)
        if url not in self._url_to_body:
            raise AssertionError(f"unexpected URL requested: {url}")
        return make_response(self._url_to_body[url])

    def close(self) -> None:
        pass


def make_client(tmp_path: Path, url_to_body: dict[str, dict]) -> SecClient:
    settings = make_settings(tmp_path)
    session = FakeSession(url_to_body)
    return SecClient(settings, session=session, clock=lambda: 0.0, sleep=lambda s: None)


# -- resolve_cik --------------------------------------------------------


def test_resolve_cik_exact_match(tmp_path):
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    ref = resolve_cik(client, "AAPL")

    assert ref == CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")


def test_resolve_cik_is_case_insensitive(tmp_path):
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    ref = resolve_cik(client, "aapl")

    assert ref.ticker == "AAPL"
    assert ref.cik == "0000320193"


def test_resolve_cik_dot_form_resolves_to_secs_hyphen_form(tmp_path):
    """SEC's company_tickers.json spells class shares with a hyphen
    (`BRK-B`), never a dot -- confirmed against the live endpoint while
    building this client. Retail/press convention often writes `BRK.B`;
    resolve_cik must accept that spelling too."""
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    ref = resolve_cik(client, "BRK.B")

    assert ref.ticker == "BRK-B"
    assert ref.cik == "0001067983"


def test_resolve_cik_hyphen_form_also_resolves(tmp_path):
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    ref = resolve_cik(client, "BRK-B")
    assert ref.cik == "0001067983"


def test_resolve_cik_lowercase_dot_form(tmp_path):
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    ref = resolve_cik(client, "brk.b")
    assert ref.cik == "0001067983"


def test_resolve_cik_unknown_ticker_raises_ticker_not_found(tmp_path):
    tickers = load_fixture("company_tickers_trimmed.json")
    client = make_client(tmp_path, {COMPANY_TICKERS_URL: tickers})

    with pytest.raises(TickerNotFound) as excinfo:
        resolve_cik(client, "ZZZZZZNOPE")

    assert "ZZZZZZNOPE" in str(excinfo.value)


def test_resolve_cik_caches_the_ticker_map(tmp_path):
    """The ticker map is large and changes rarely -- a second resolve_cik
    call must be served from cache, not fetched again."""
    tickers = load_fixture("company_tickers_trimmed.json")
    settings = make_settings(tmp_path)
    session = FakeSession({COMPANY_TICKERS_URL: tickers})
    client = SecClient(settings, session=session, clock=lambda: 0.0, sleep=lambda s: None)

    resolve_cik(client, "AAPL")
    resolve_cik(client, "MSFT")

    assert session.calls == [COMPANY_TICKERS_URL]  # only fetched once


# -- fetch_submissions ----------------------------------------------------


def test_fetch_submissions_parses_metadata_and_filings(tmp_path):
    submissions = load_fixture("submissions_AAPL.json")
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "0000320193")

    assert isinstance(doc, SubmissionsDoc)
    assert doc.cik == "0000320193"
    assert doc.entity_name == submissions["name"]
    assert doc.sic == submissions["sic"]
    assert doc.sic_description == submissions["sicDescription"]
    assert doc.fiscal_year_end == submissions["fiscalYearEnd"]
    assert len(doc.recent_filings) == len(submissions["filings"]["recent"]["form"])
    assert doc.raw == submissions


def test_fetch_submissions_surfaces_tickers_and_former_names(tmp_path):
    """PLANNING.md Section 5.3.11 (the XOM finding): an empty `tickers` list
    is the registrant-change tell Phase 2 will act on. This module only has
    to surface the raw signal faithfully, not interpret it."""
    submissions = load_fixture("submissions_AAPL.json")
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "0000320193")

    assert doc.tickers == submissions["tickers"]
    assert doc.tickers == ["AAPL"]
    assert doc.former_names == submissions["formerNames"]
    assert doc.former_names[0]["name"] == "APPLE INC"


def test_fetch_submissions_tickers_defaults_to_empty_list_when_absent(tmp_path):
    submissions = load_fixture("submissions_AAPL.json")
    submissions["tickers"] = []
    submissions["formerNames"] = []
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "0000320193")

    assert doc.tickers == []
    assert doc.former_names == []


def test_fetch_submissions_accepts_an_unpadded_cik(tmp_path):
    submissions = load_fixture("submissions_AAPL.json")
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "320193")
    assert doc.cik == "0000320193"


def test_filing_ref_fields_and_primary_doc_url(tmp_path):
    submissions = load_fixture("submissions_AAPL.json")
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "0000320193")
    ten_k = next(f for f in doc.recent_filings if f.form == "10-K")

    assert ten_k.accession == "0000320193-25-000079"
    assert ten_k.period_end is not None
    assert ten_k.primary_doc_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )


def test_filing_ref_period_end_none_when_report_date_blank(tmp_path):
    submissions = load_fixture("submissions_AAPL.json")
    submissions["filings"]["recent"]["reportDate"][0] = ""
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = make_client(tmp_path, {url: submissions})

    doc = fetch_submissions(client, "0000320193")
    assert doc.recent_filings[0].period_end is None


# -- fetch_company_facts ----------------------------------------------------


def test_fetch_company_facts_parses_namespaces(tmp_path):
    facts = load_fixture("companyfacts_AAPL.json")
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    client = make_client(tmp_path, {url: facts})

    doc = fetch_company_facts(client, "0000320193")

    assert isinstance(doc, CompanyFactsDoc)
    assert doc.cik == "0000320193"
    assert doc.entity_name == facts["entityName"]
    assert doc.namespaces == sorted(facts["facts"].keys())
    assert "dei" in doc.namespaces
    assert "us-gaap" in doc.namespaces


def test_company_facts_tags_returns_tag_names_only(tmp_path):
    facts = load_fixture("companyfacts_AAPL.json")
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    client = make_client(tmp_path, {url: facts})

    doc = fetch_company_facts(client, "0000320193")

    gaap_tags = doc.tags("us-gaap")
    assert gaap_tags == sorted(facts["facts"]["us-gaap"].keys())
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in gaap_tags
    # Phase 1 must not interpret facts -- confirm we got names, not values.
    assert all(isinstance(tag, str) for tag in gaap_tags)


def test_company_facts_tags_for_absent_namespace_is_empty_not_an_error(tmp_path):
    facts = load_fixture("companyfacts_AAPL.json")
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    client = make_client(tmp_path, {url: facts})

    doc = fetch_company_facts(client, "0000320193")
    assert doc.tags("ecd") == []
