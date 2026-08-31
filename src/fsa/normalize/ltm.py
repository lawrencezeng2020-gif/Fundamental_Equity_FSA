"""Trailing-twelve-month (LTM) derivation.

Phase 2 responsibility (PLANNING.md Section 5, "LTM (trailing twelve months)"):
    - Flow items (IS, CF): LTM = most recent FY + latest YTD-through-Qn -
      prior-year YTD-through-Qn.
    - Stock items (BS): most recent quarterly instant, used as-is.
    - If quarterly data is insufficient, fall back to the latest FY and label
      the column accordingly -- never present a partial period as if it were
      annualized.

No single XBRL fact is ever tagged as a trailing-twelve-month figure, so a
flow item's LTM cell is always a Python computation over three already-
resolved facts (Mechanism.DERIVED), never a direct tag lookup. A stock
item's LTM cell, by contrast, genuinely is just the most recent instant fact
used as-is -- resolved the same way as any other period via
`statements.resolve_span`, not computed here.

This module only decides *which dates* define the LTM window (by scanning
the anchor's own raw facts for the most recent post-FY stub period) and does
the flow arithmetic once the three values are known; `statements.py` owns
confirming those dates actually resolve via the full alternatives/stitching
machinery (not just the anchor's raw facts) and building `Cell`/`Provenance`
objects, to avoid this module depending on `statements.py` (which imports
this module) and creating a cycle.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fsa.normalize.facts import RawFact, shift_years
from fsa.normalize.schema import FiscalPeriod, PeriodType

__all__ = ["determine_ltm_axis_period", "fallback_ltm_period", "compute_flow_ltm_value"]

# A candidate "YTD-through-Qn" stub period must be strictly shorter than a
# fiscal year but long enough to be a real reporting period (not, say, a
# same-day correction artifact) -- roughly one to three quarters.
_MIN_STUB_DAYS = 60
_MAX_STUB_DAYS = 300

# Tolerance for locating the prior-year comparative YTD span. A naive
# calendar year-shift of the current YTD's (start, end) is *not* reliable
# for 52/53-week filers: confirmed against real AAPL data, whose FY2025
# starts 2024-09-29 while a plain `shift_years` of its FY2026 Q3 YTD start
# (2025-09-28) back one year lands on 2024-09-28 -- one day off the filer's
# actual prior fiscal year start, which fails an exact-match lookup outright
# and would wrongly force the Case-B fallback for a filer with perfectly
# good quarterly data. Instead, the prior comparative is *searched for*
# among the anchor's own facts, allowing a few days of drift on both the end
# date and the period's own length.
_PRIOR_YEAR_END_TOLERANCE_DAYS = 10
_PRIOR_YEAR_LENGTH_TOLERANCE_DAYS = 10


def determine_ltm_axis_period(
    anchor_facts_by_tag: dict[str, list[RawFact]], latest_fy: FiscalPeriod
) -> tuple[FiscalPeriod, tuple[date, date] | None, tuple[date, date] | None]:
    """Propose the LTM period's dates from the anchor's own raw facts.

    Scans every anchor tag for a duration fact starting exactly the day
    after the latest fiscal year's end (i.e. a YTD-through-Qn stub for the
    *next*, in-progress fiscal year) and takes the one with the latest end
    date -- the most recently reported quarter. Returns
    ``(ltm_period, this_year_span, prior_year_span)``; the two spans are
    ``None`` when no such stub period exists at all (a plain fallback:
    ``statements.py`` still separately confirms the proposed spans actually
    resolve through the full mapping mechanisms before treating this as
    final -- this function only proposes a candidate).
    """
    next_fy_start = latest_fy.end + timedelta(days=1)
    candidates: list[RawFact] = []
    for fact_list in anchor_facts_by_tag.values():
        for fact in fact_list:
            if fact.start != next_fy_start or fact.end <= latest_fy.end:
                continue
            days = (fact.end - fact.start).days
            if _MIN_STUB_DAYS <= days <= _MAX_STUB_DAYS:
                candidates.append(fact)

    if not candidates:
        return fallback_ltm_period(latest_fy), None, None

    best = max(candidates, key=lambda f: f.end)
    this_start, this_end = best.start, best.end
    this_days = (this_end - this_start).days

    target_prior_end = shift_years(this_end, -1)
    prior_candidates = []
    for fact_list in anchor_facts_by_tag.values():
        for fact in fact_list:
            if fact.start is None:
                continue
            fact_days = (fact.end - fact.start).days
            end_delta = abs((fact.end - target_prior_end).days)
            length_delta = abs(fact_days - this_days)
            if end_delta <= _PRIOR_YEAR_END_TOLERANCE_DAYS and length_delta <= _PRIOR_YEAR_LENGTH_TOLERANCE_DAYS:
                prior_candidates.append((end_delta, fact))

    if not prior_candidates:
        return fallback_ltm_period(latest_fy), None, None

    _, prior_fact = min(prior_candidates, key=lambda pair: pair[0])
    prior_start, prior_end = prior_fact.start, prior_fact.end

    ltm_period = FiscalPeriod(
        label="LTM",
        fiscal_year=this_end.year,
        end=this_end,
        start=shift_years(this_end, -1),
        kind=PeriodType.DURATION,
        is_ltm=True,
        days=(this_end - shift_years(this_end, -1)).days,
    )
    return ltm_period, (this_start, this_end), (prior_start, prior_end)


def fallback_ltm_period(latest_fy: FiscalPeriod) -> FiscalPeriod:
    """The Case-B fallback: no usable post-FY quarterly stub exists, so the
    "LTM" column is explicitly labeled as a stand-in for the latest FY
    rather than presented as a genuine trailing-twelve-month figure
    (PLANNING.md: "never present a partial period as if it were
    annualized"). ``is_ltm=False`` reflects that this period is not
    actually a trailing-twelve-month window -- it is the latest FY, exactly.
    """
    return FiscalPeriod(
        label=f"LTM (fallback: {latest_fy.label})",
        fiscal_year=latest_fy.fiscal_year,
        end=latest_fy.end,
        start=latest_fy.start,
        kind=PeriodType.DURATION,
        is_ltm=False,
        days=latest_fy.days,
    )


def compute_flow_ltm_value(fy_value: Decimal, this_ytd_value: Decimal, prior_ytd_value: Decimal) -> Decimal:
    """LTM = latest FY + this-year YTD-through-Qn - prior-year YTD-through-Qn.

    Pure arithmetic; sign convention (e.g. capex stored as a negative
    outflow) is already applied uniformly to all three inputs by whichever
    resolution produced them, so it carries through correctly here without
    this function needing to know about signs at all.
    """
    return fy_value + this_ytd_value - prior_ytd_value
