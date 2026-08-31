"""Live smoke tests for fsa.normalize against the real SEC EDGAR network.

Deselected by default (pyproject.toml: `addopts = -m "not live"`). Run with:

    venv/bin/pytest -m live tests/test_normalize_live.py -v -s

Requires a real `.fsa.toml` with a valid `sec_user_agent`. Uses the on-disk
response cache, so repeat runs are fast and 304-revalidated rather than
full refetches (PLANNING.md Section 2) -- these tests build the full
FinancialModel from whatever SEC serves *right now*, so a few figures here
(e.g. exact LTM values) will drift over time as new quarters are filed;
that is expected and is why the offline suite (test_normalize_statements.py)
pins its assertions against committed, trimmed fixtures instead.
"""

from __future__ import annotations

import pytest

from fsa.config import load_settings
from fsa.normalize.schema import Mechanism
from fsa.normalize.statements import build_financial_model
from fsa.sec.client import SecClient
from fsa.sec.endpoints import fetch_company_facts, fetch_submissions, resolve_cik

pytestmark = pytest.mark.live


def _build(ticker: str, *, cik: str | None = None, historical_years: int = 10):
    settings = load_settings()
    with SecClient(settings) as client:
        if cik is not None:
            resolved_cik = cik
        else:
            company = resolve_cik(client, ticker)
            resolved_cik = company.cik
        submissions = fetch_submissions(client, resolved_cik)
        company_facts = fetch_company_facts(client, resolved_cik)
        from fsa.sec.endpoints import CompanyRef

        company = CompanyRef(ticker=ticker, cik=resolved_cik, name=submissions.entity_name)
        return build_financial_model(company, submissions, company_facts, historical_years=historical_years)


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "KO", "TSLA", "JPM", "RDDT", "CAVA"])
def test_live_builds_a_model_with_a_full_period_axis(ticker):
    model = _build(ticker)
    non_ltm = [p for p in model.periods if not p.label.startswith("LTM")]
    assert len(non_ltm) >= 3, f"{ticker}: expected at least 3 annual periods, got {len(non_ltm)}"
    revenue = next(li for li in model.line_items if li.key == "revenue")
    for period in non_ltm:
        assert revenue.cells[period.label].value is not None
    print(f"\n{ticker}: periods={[p.label for p in model.periods]} warnings={[w.code for w in model.warnings]}")


def test_live_msft_d_and_a_still_resolves_via_composition():
    model = _build("MSFT")
    da = next(li for li in model.line_items if li.key == "d_and_a")
    for label, cell in da.cells.items():
        if label.startswith("LTM"):
            continue
        assert cell.value is not None
        assert cell.provenance.mechanism is Mechanism.COMPOSED


def test_live_tsla_d_and_a_never_stale_from_2013_2017():
    model = _build("TSLA")
    da = next(li for li in model.line_items if li.key == "d_and_a")
    for label, cell in da.cells.items():
        if label.startswith("LTM"):
            continue
        fy = int(label.replace("FY", ""))
        if fy >= 2018 and cell.provenance.tag is not None:
            assert cell.provenance.tag != "DepreciationDepletionAndAmortization"


def test_live_ko_fy2018_revenue_is_the_restated_figure():
    from decimal import Decimal

    model = _build("KO")
    revenue = next(li for li in model.line_items if li.key == "revenue")
    if "FY2018" in revenue.cells:
        assert revenue.cells["FY2018"].value == Decimal("34300000000")


def test_live_xom_raises_registrant_change_suspected():
    model = _build("XOM")
    codes = [w.code for w in model.warnings]
    assert "REGISTRANT_CHANGE_SUSPECTED" in codes
    assert "THIN_HISTORY" not in codes
