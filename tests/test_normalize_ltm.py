"""Tests for fsa.normalize.ltm: LTM window selection and flow arithmetic
(PLANNING.md Section 5, "LTM (trailing twelve months)")."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fsa.normalize.facts import RawFact
from fsa.normalize.ltm import compute_flow_ltm_value, determine_ltm_axis_period, fallback_ltm_period
from fsa.normalize.schema import FiscalPeriod, PeriodType


def _fact(start, end, val, filed="2024-01-01"):
    return RawFact(value=Decimal(str(val)), start=start, end=end, filed=date.fromisoformat(filed), accession="a", frame=None, form="10-Q")


LATEST_FY = FiscalPeriod(
    label="FY2023", fiscal_year=2023, end=date(2023, 12, 31), start=date(2023, 1, 1),
    kind=PeriodType.DURATION, is_ltm=False, days=364,
)


def test_compute_flow_ltm_value_is_fy_plus_this_ytd_minus_prior_ytd():
    result = compute_flow_ltm_value(Decimal("100"), Decimal("30"), Decimal("25"))
    assert result == Decimal("105")


def test_compute_flow_ltm_value_carries_negative_sign_through_correctly():
    """Capex is stored as a negative number (sign=negated); the LTM formula
    must not need special-casing for that -- it's linear."""
    result = compute_flow_ltm_value(Decimal("-100"), Decimal("-30"), Decimal("-25"))
    assert result == Decimal("-105")


def test_determine_ltm_axis_period_finds_a_genuine_stub_quarter():
    facts_by_tag = {
        "Revenues": [
            _fact(date(2023, 1, 1), date(2023, 12, 31), "1000"),
            _fact(date(2024, 1, 1), date(2024, 9, 30), "800"),  # this-year 9-month YTD
            _fact(date(2023, 1, 1), date(2023, 9, 30), "750"),  # prior-year 9-month YTD (comparative)
        ]
    }
    ltm_period, this_span, prior_span = determine_ltm_axis_period(facts_by_tag, LATEST_FY)
    assert ltm_period.is_ltm is True
    assert ltm_period.label == "LTM"
    assert this_span == (date(2024, 1, 1), date(2024, 9, 30))
    assert prior_span == (date(2023, 1, 1), date(2023, 9, 30))


def test_determine_ltm_axis_period_falls_back_when_no_stub_quarter_exists():
    facts_by_tag = {"Revenues": [_fact(date(2023, 1, 1), date(2023, 12, 31), "1000")]}
    ltm_period, this_span, prior_span = determine_ltm_axis_period(facts_by_tag, LATEST_FY)
    assert ltm_period.is_ltm is False
    assert "fallback" in ltm_period.label
    assert ltm_period.end == LATEST_FY.end
    assert ltm_period.start == LATEST_FY.start
    assert this_span is None
    assert prior_span is None


def test_determine_ltm_axis_period_falls_back_when_no_prior_year_comparative():
    """A this-year stub with no matching prior-year comparative (needed for
    the flow formula) must still fall back honestly rather than half-compute
    an LTM figure."""
    facts_by_tag = {"Revenues": [_fact(date(2024, 1, 1), date(2024, 9, 30), "800")]}
    ltm_period, this_span, prior_span = determine_ltm_axis_period(facts_by_tag, LATEST_FY)
    assert ltm_period.is_ltm is False
    assert this_span is None
    assert prior_span is None


def test_determine_ltm_axis_period_tolerates_52_53_week_drift_in_prior_year():
    """Regression for a real bug found against live AAPL data: a plain
    calendar year-shift of the current YTD span can miss the actual prior-
    year comparative by a day or two for a 52/53-week filer. The prior span
    must be located by searching (within tolerance), not computed blindly."""
    fy = FiscalPeriod(label="FY2025", fiscal_year=2025, end=date(2025, 9, 27), start=date(2024, 9, 29), kind=PeriodType.DURATION, is_ltm=False, days=363)
    facts_by_tag = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            _fact(date(2024, 9, 29), date(2025, 9, 27), "416161000000"),
            _fact(date(2025, 9, 28), date(2026, 6, 27), "364357000000"),  # this-year 9mo YTD
            _fact(date(2024, 9, 29), date(2025, 6, 28), "313695000000"),  # prior comparative starts one day off a naive shift_years
        ]
    }
    ltm_period, this_span, prior_span = determine_ltm_axis_period(facts_by_tag, fy)
    assert this_span == (date(2025, 9, 28), date(2026, 6, 27))
    assert prior_span == (date(2024, 9, 29), date(2025, 6, 28))


def test_fallback_ltm_period_is_not_flagged_is_ltm_and_labels_the_fallback():
    fallback = fallback_ltm_period(LATEST_FY)
    assert fallback.is_ltm is False
    assert LATEST_FY.label in fallback.label
    assert fallback.end == LATEST_FY.end
    assert fallback.start == LATEST_FY.start
