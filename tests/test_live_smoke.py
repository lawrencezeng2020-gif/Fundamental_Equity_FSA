"""Live smoke test: exercises fsa.sec against the real SEC EDGAR network.

Deselected by default -- pyproject.toml sets `addopts = -m "not live"`, so a
plain `pytest` run never touches the network. Run explicitly with:

    venv/bin/pytest -m live tests/test_live_smoke.py -v -s

Requires a real, valid `.fsa.toml` (or `~/.fsa/config.toml`) with a real
`sec_user_agent`, since this genuinely calls SEC EDGAR. Uses the real
on-disk response cache (whatever `cache_dir` the local config points at),
which is exactly the point -- repeated local runs of this test should be
fast and 304-revalidated rather than full refetches every time.
"""

from __future__ import annotations

from datetime import date

import pytest

from fsa.config import load_settings
from fsa.sec.client import SecClient
from fsa.sec.endpoints import COMPANY_TICKERS_URL, fetch_company_facts, fetch_submissions, resolve_cik
from fsa.sec.errors import TickerNotFound

pytestmark = pytest.mark.live


def _annual_period_years_for_tags_containing(raw: dict, substring: str) -> set[int]:
    """Calendar years for which any us-gaap tag whose name contains
    `substring` has an annual-duration (~300-380 day) fact. Scans by
    substring rather than one exact tag name because revenue tags vary by
    filer/era (PLANNING.md Section 5.2) -- this only needs to answer "is
    there *any* annual revenue history", not resolve which tag is canonical
    (that's Phase 2's job)."""
    us_gaap = raw.get("facts", {}).get("us-gaap", {})
    years: set[int] = set()
    for tag_name, tag_data in us_gaap.items():
        if substring not in tag_name:
            continue
        for facts in tag_data.get("units", {}).values():
            for fact in facts:
                start, end = fact.get("start"), fact.get("end")
                if not start or not end:
                    continue
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if 300 <= days <= 380:
                    years.add(date.fromisoformat(end).year)
    return years


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT"])
def test_live_resolve_fetch_and_namespace_shape(ticker):
    settings = load_settings()
    with SecClient(settings) as client:
        company = resolve_cik(client, ticker)
        assert company.cik.isdigit()
        assert len(company.cik) == 10

        submissions = fetch_submissions(client, company.cik)
        assert submissions.entity_name
        assert submissions.fiscal_year_end is not None
        assert len(submissions.recent_filings) > 0

        facts = fetch_company_facts(client, company.cik)
        assert "us-gaap" in facts.namespaces
        assert "dei" in facts.namespaces
        assert len(facts.tags("us-gaap")) > 50

        print(
            f"\n{ticker}: cik={company.cik} name={company.name!r} "
            f"namespaces={facts.namespaces} us-gaap tags={len(facts.tags('us-gaap'))}"
        )


def test_live_bogus_ticker_raises_ticker_not_found():
    settings = load_settings()
    with SecClient(settings) as client:
        with pytest.raises(TickerNotFound):
            resolve_cik(client, "ZZZZZZNOPE")


def test_live_conditional_get_produces_a_304_on_second_call():
    """Concrete demonstration of PLANNING.md Section 2's conditional-GET
    behavior against the real endpoint: force revalidation of whatever is
    already cached and confirm SEC returns 304 with an unchanged body."""
    settings = load_settings()
    with SecClient(settings) as client:
        resolve_cik(client, "AAPL")  # ensures the ticker map is cached

    with SecClient(settings, force_refresh=True) as client2:
        result = client2.get_json(COMPANY_TICKERS_URL, cache_key="company_tickers")

    assert result.revalidated is True
    assert result.from_cache is True


def test_live_xom_ticker_resolves_to_a_registrant_with_almost_no_history():
    """Pinned regression for PLANNING.md Section 5.3.11 (added after the
    Phase 1 review flagged the XOM finding as its most important result).

    As of Phase 1, SEC's company_tickers.json resolves ticker XOM to a CIK
    that is a newly created registrant with essentially no XBRL history (no
    annual revenue periods at all), while the CIK holding Exxon's actual
    multi-decade filing history has an *empty* `tickers` list in its own
    submissions -- i.e. it is unreachable by ticker lookup, only by
    `--cik`/`fetch_submissions(cik=...)` directly. The legacy CIK (34088) is
    hardcoded here, not discovered: PLANNING.md Section 12 explicitly defers
    automatic predecessor-CIK discovery, and this module only surfaces the
    detection signal (`SubmissionsDoc.tickers`), it does not act on it.

    This test exists so that if SEC ever backfills history onto the new CIK,
    or changes the ticker mapping, that shows up as a loud test failure here
    instead of a silent surprise the next time this pipeline runs for XOM.
    """
    settings = load_settings()
    legacy_cik = "0000034088"

    with SecClient(settings) as client:
        company = resolve_cik(client, "XOM")
        thin_facts = fetch_company_facts(client, company.cik)
        thin_submissions = fetch_submissions(client, company.cik)

        thin_revenue_years = _annual_period_years_for_tags_containing(thin_facts.raw, "Revenue")
        assert thin_revenue_years == set(), (
            f"Expected XOM's ticker-resolved CIK ({company.cik}) to have zero annual revenue "
            f"periods (the known-thin registrant); found years: {thin_revenue_years}. If SEC "
            "backfilled history onto this CIK, PLANNING.md Section 5.3.11 needs revisiting."
        )

        legacy_submissions = fetch_submissions(client, legacy_cik)
        legacy_facts = fetch_company_facts(client, legacy_cik)
        legacy_revenue_years = _annual_period_years_for_tags_containing(legacy_facts.raw, "Revenue")

        # The detection signal Phase 2 will act on: the CIK with the real
        # history has no ticker associated with it at all.
        assert legacy_submissions.tickers == []
        # The real multi-decade history, confirmed present under the legacy CIK.
        assert len(legacy_revenue_years) >= 10
        assert len(legacy_facts.tags("us-gaap")) > len(thin_facts.tags("us-gaap"))

        print(
            f"\nXOM -> CIK {company.cik} ({thin_submissions.entity_name!r}): "
            f"{len(thin_facts.tags('us-gaap'))} us-gaap tags, "
            f"{len(thin_revenue_years)} annual revenue years, "
            f"tickers={thin_submissions.tickers}\n"
            f"legacy CIK {legacy_cik} ({legacy_submissions.entity_name!r}): "
            f"{len(legacy_facts.tags('us-gaap'))} us-gaap tags, "
            f"{len(legacy_revenue_years)} annual revenue years, "
            f"tickers={legacy_submissions.tickers}"
        )
