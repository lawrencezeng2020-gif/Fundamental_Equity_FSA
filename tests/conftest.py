"""Shared pytest fixtures for the FSA test suite."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fsa.sec.endpoints import CompanyFactsDoc, CompanyRef, FilingRef, SubmissionsDoc

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_normalize_company(ticker: str, *, cik: str | None = None) -> tuple[CompanyRef, SubmissionsDoc, CompanyFactsDoc]:
    """Load one Phase 2 test company from the trimmed, committed
    `norm_companyfacts_<TICKER>.json` / `norm_submissions_<TICKER>.json`
    fixtures (see the Phase 2 report for how they were built: real SEC data,
    trimmed to only the us-gaap/dei tags `mappings/us_gaap.yaml` references,
    keeping full restatement history for annual-duration facts and recent
    sub-annual facts for LTM, so the fixtures faithfully exercise dedupe/
    stitching/composition without committing multi-MB blobs).

    `ticker` names the fixture files; `cik` (default: derived from the
    fixture's own `cik` field) lets a caller deliberately mismatch them --
    used nowhere currently, but keeps this loader honest about the two being
    independent, the way `--cik` is in the real CLI.
    """
    subs_raw = json.loads((FIXTURES_DIR / f"norm_submissions_{ticker}.json").read_text())
    facts_raw = json.loads((FIXTURES_DIR / f"norm_companyfacts_{ticker}.json").read_text())
    resolved_cik = cik if cik is not None else str(facts_raw["cik"]).zfill(10)
    company = CompanyRef(ticker=ticker, cik=resolved_cik, name=subs_raw["name"])
    submissions = SubmissionsDoc(
        raw=subs_raw,
        cik=resolved_cik,
        entity_name=subs_raw["name"],
        sic=subs_raw.get("sic"),
        sic_description=subs_raw.get("sicDescription"),
        fiscal_year_end=subs_raw.get("fiscalYearEnd"),
        tickers=subs_raw.get("tickers", []),
        former_names=subs_raw.get("formerNames", []),
        recent_filings=[],
    )
    company_facts = CompanyFactsDoc(
        raw=facts_raw,
        cik=resolved_cik,
        entity_name=facts_raw.get("entityName") or "",
        namespaces=sorted(facts_raw.get("facts", {}).keys()),
    )
    return company, submissions, company_facts


@pytest.fixture
def tmp_config_file(tmp_path: Path):
    """Factory fixture: write a minimal valid .fsa.toml-style file under tmp_path.

    Returns a callable so individual tests can override specific keys while
    keeping a valid `sec_user_agent` by default.
    """

    def _make(**overrides: object) -> Path:
        values = {
            "sec_user_agent": "Test Runner test.runner@example.com",
        }
        values.update(overrides)

        lines = []
        for key, value in values.items():
            if value is None:
                continue
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            elif isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            else:
                lines.append(f"{key} = {value}")

        config_path = tmp_path / ".fsa.toml"
        config_path.write_text("\n".join(lines) + "\n")
        return config_path

    return _make


@pytest.fixture
def mock_sec_calls(monkeypatch):
    """Monkeypatch fsa.cli's resolve_cik/fetch_submissions/fetch_company_facts
    so `fsa.cli.main` can be exercised end-to-end (argument parsing, config
    resolution, summary printing, exit codes) with zero network access.

    Returns the canned CompanyRef so tests can reference its fields if
    needed. `fsa.sec.client.SecClient` itself is still constructed for real
    (its __init__ does no I/O), only the three endpoint calls are faked.
    """
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    submissions = SubmissionsDoc(
        raw={},
        cik="0000320193",
        entity_name="Apple Inc.",
        sic="3571",
        sic_description="Electronic Computers",
        fiscal_year_end="0928",
        tickers=["AAPL"],
        former_names=[],
        recent_filings=[
            FilingRef(
                form="10-K",
                accession="0000320193-25-000079",
                filing_date=date(2025, 10, 31),
                period_end=date(2025, 9, 27),
                primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm",
            ),
            FilingRef(
                form="10-Q",
                accession="0000320193-26-000020",
                filing_date=date(2026, 7, 31),
                period_end=date(2026, 6, 27),
                primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm",
            ),
        ],
    )
    company_facts = CompanyFactsDoc(
        raw={"facts": {"dei": {"EntityCommonStockSharesOutstanding": {}}, "us-gaap": {"Assets": {}, "Revenues": {}}}},
        cik="0000320193",
        entity_name="Apple Inc.",
        namespaces=["dei", "us-gaap"],
    )
    monkeypatch.setattr("fsa.cli.resolve_cik", lambda client, ticker: company)
    monkeypatch.setattr("fsa.cli.fetch_submissions", lambda client, cik: submissions)
    monkeypatch.setattr("fsa.cli.fetch_company_facts", lambda client, cik: company_facts)
    return company
