"""Tests for fsa.normalize.facts: fact extraction, dedupe, and period
classification (PLANNING.md Section 5.3, pitfalls 2, 4, 5, 7)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fsa.normalize.facts import (
    RawFact,
    classify_duration_days,
    dedupe_facts,
    extract_raw_facts,
    fiscal_year_end_matches,
    fiscal_year_for_end,
    select_exact,
    shift_years,
)
from fsa.sec.endpoints import CompanyFactsDoc


def _fact(start, end, val, filed, accn="acc", frame=None, form="10-K"):
    return RawFact(value=Decimal(str(val)), start=start, end=end, filed=filed, accession=accn, frame=frame, form=form)


# -- Pitfall 2: restatements / duplicate facts -- dedupe by (start, end), latest filed wins --


def test_dedupe_facts_takes_latest_filed():
    facts = [
        _fact(date(2018, 1, 1), date(2018, 12, 31), "31856000000", date(2019, 2, 21), accn="a1"),
        _fact(date(2018, 1, 1), date(2018, 12, 31), "34300000000", date(2020, 2, 24), accn="a2"),
        _fact(date(2018, 1, 1), date(2018, 12, 31), "34300000000", date(2021, 2, 25), accn="a3", frame="CY2018"),
    ]
    result = dedupe_facts(facts)
    assert len(result) == 1
    assert result[0].value == Decimal("34300000000")
    assert result[0].filed == date(2021, 2, 25)
    assert result[0].accession == "a3"


def test_dedupe_facts_frame_corroborated_tiebreak_on_equal_filed_date():
    same_day = date(2021, 2, 25)
    facts = [
        _fact(date(2018, 1, 1), date(2018, 12, 31), "100", same_day, accn="no-frame", frame=None),
        _fact(date(2018, 1, 1), date(2018, 12, 31), "200", same_day, accn="with-frame", frame="CY2018"),
    ]
    result = dedupe_facts(facts)
    assert len(result) == 1
    assert result[0].accession == "with-frame"


def test_dedupe_facts_keeps_distinct_periods_separate():
    facts = [
        _fact(date(2017, 1, 1), date(2017, 12, 31), "100", date(2018, 2, 1)),
        _fact(date(2018, 1, 1), date(2018, 12, 31), "200", date(2019, 2, 1)),
    ]
    result = dedupe_facts(facts)
    assert len(result) == 2
    assert [f.end for f in result] == [date(2017, 12, 31), date(2018, 12, 31)]


def test_extract_raw_facts_returns_empty_list_for_absent_tag():
    doc = CompanyFactsDoc(raw={"facts": {"us-gaap": {}}}, cik="1", entity_name="X", namespaces=["us-gaap"])
    assert extract_raw_facts(doc, namespace="us-gaap", tag="NoSuchTag", unit="USD") == []


def test_extract_raw_facts_ignores_facts_missing_required_fields():
    doc = CompanyFactsDoc(
        raw={
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {"end": "2023-12-31", "start": "2023-01-01", "val": 100},  # missing 'filed'
                                {"start": "2023-01-01", "val": 100, "filed": "2024-02-01"},  # missing 'end'
                                {"end": "2023-12-31", "start": "2023-01-01", "val": 100, "filed": "2024-02-01"},
                            ]
                        }
                    }
                }
            }
        },
        cik="1", entity_name="X", namespaces=["us-gaap"],
    )
    result = extract_raw_facts(doc, namespace="us-gaap", tag="Revenues", unit="USD")
    assert len(result) == 1
    assert result[0].value == Decimal("100")


def test_extract_raw_facts_never_uses_float_for_the_value():
    """PLANNING.md Section 5: use Decimal, never float. Confirm a JSON float
    (EPS values arrive this way) round-trips exactly via its string form,
    not via a Decimal(float(...)) construction that would launder binary
    imprecision back in."""
    doc = CompanyFactsDoc(
        raw={"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 6.13, "filed": "2024-02-01"}
        ]}}}}},
        cik="1", entity_name="X", namespaces=["us-gaap"],
    )
    result = extract_raw_facts(doc, namespace="us-gaap", tag="EarningsPerShareDiluted", unit="USD/shares")
    assert isinstance(result[0].value, Decimal)
    assert result[0].value == Decimal("6.13")


# -- Pitfall 3: duration vs. instant --


def test_raw_fact_period_type_from_presence_of_start():
    instant = _fact(None, date(2023, 12, 31), "1", date(2024, 1, 1))
    duration = _fact(date(2023, 1, 1), date(2023, 12, 31), "1", date(2024, 1, 1))
    from fsa.normalize.schema import PeriodType

    assert instant.period_type is PeriodType.INSTANT
    assert duration.period_type is PeriodType.DURATION
    assert instant.days is None
    assert duration.days == 364


# -- Pitfall 4: fiscal calendars -- never derive from fy/fp, only start/end + submissions FYE --


def test_fiscal_year_for_end_uses_calendar_year_of_end_date():
    assert fiscal_year_for_end(date(2024, 6, 30)) == 2024  # MSFT-style: FYE June 30, labeled by the ending year
    assert fiscal_year_for_end(date(2025, 9, 27)) == 2025  # AAPL-style 52/53-week close


def test_fiscal_year_end_matches_tolerates_52_53_week_drift():
    # AAPL's declared FYE is "0928" (last Saturday of September, drifts a few
    # days year to year) -- a real close on 2025-09-27 must still match.
    assert fiscal_year_end_matches(date(2025, 9, 27), "0928") is True
    assert fiscal_year_end_matches(date(2025, 9, 27), "0928", tolerance_days=10) is True


def test_fiscal_year_end_matches_rejects_a_date_nowhere_near_the_fye():
    assert fiscal_year_end_matches(date(2025, 3, 31), "1231", tolerance_days=10) is False


def test_fiscal_year_end_matches_permissive_when_fye_unknown():
    assert fiscal_year_end_matches(date(2025, 3, 31), None) is True
    assert fiscal_year_end_matches(date(2025, 3, 31), "") is True


def test_shift_years_handles_leap_day():
    assert shift_years(date(2024, 2, 29), -1) == date(2023, 2, 28)
    assert shift_years(date(2023, 6, 30), 1) == date(2024, 6, 30)


# -- Pitfall 5: annual vs. quarterly classification with tolerance windows --


def test_classify_duration_days_annual_window():
    assert classify_duration_days(365) == "annual"
    assert classify_duration_days(350) == "annual"  # lower tolerance bound
    assert classify_duration_days(380) == "annual"  # upper tolerance bound
    assert classify_duration_days(364) == "annual"  # 52-week filer
    assert classify_duration_days(371) == "annual"  # 53-week filer


def test_classify_duration_days_quarterly_window():
    assert classify_duration_days(91) == "quarterly"
    assert classify_duration_days(80) == "quarterly"
    assert classify_duration_days(100) == "quarterly"


def test_classify_duration_days_other_for_half_year_and_ytd_stubs():
    assert classify_duration_days(181) == "other"  # half-year YTD
    assert classify_duration_days(273) == "other"  # 9-month YTD
    assert classify_duration_days(349) == "other"  # just outside the annual band
    assert classify_duration_days(30) == "other"


# -- Pitfall 7: units -- never mix USD / USD-per-share / shares --


def test_extract_raw_facts_only_reads_the_requested_unit():
    doc = CompanyFactsDoc(
        raw={
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [{"start": "2023-01-01", "end": "2023-12-31", "val": 6.13, "filed": "2024-02-01"}],
                            "USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": 999999, "filed": "2024-02-01"}],
                        }
                    }
                }
            }
        },
        cik="1", entity_name="X", namespaces=["us-gaap"],
    )
    usd_per_share = extract_raw_facts(doc, namespace="us-gaap", tag="EarningsPerShareDiluted", unit="USD/shares")
    usd = extract_raw_facts(doc, namespace="us-gaap", tag="EarningsPerShareDiluted", unit="USD")
    assert [f.value for f in usd_per_share] == [Decimal("6.13")]
    assert [f.value for f in usd] == [Decimal("999999")]


def test_select_exact_matches_only_the_exact_span_never_approximate():
    """PLANNING.md Section 5.1: never take 'the nearest' period -- an
    off-by-a-day mismatch must be a gap, not a silent substitute."""
    facts = [_fact(date(2023, 1, 1), date(2023, 12, 31), "100", date(2024, 1, 1))]
    assert select_exact(facts, start=date(2023, 1, 1), end=date(2023, 12, 31)) is not None
    assert select_exact(facts, start=date(2023, 1, 2), end=date(2023, 12, 31)) is None
    assert select_exact(facts, start=date(2023, 1, 1), end=date(2023, 12, 30)) is None
