"""Fact selection, deduplication, and period classification.

Phase 2 responsibility (PLANNING.md Section 5.3, items 2 and 4-7):
    - Restatement dedupe: same (start, end) period reported multiple times
      with different `filed` dates/`accn` -- take the latest `filed`; a
      `frame` key is corroborating evidence. Retain the accession number.
    - Derive periods from `start`/`end` dates and the company's fiscal year
      end (from `submissions`), never from the unreliable `fy`/`fp` fields.
    - Classify annual vs quarterly durations with tolerance windows
      (~350-380 days for a fiscal year -- generous enough to cover
      PLANNING.md's own tighter 358-378 day estimate plus 52/53-week
      filers; ~80-100 days for a quarter).
    - Classify DURATION vs INSTANT (has `start` vs. `end`-only).

This module deliberately knows nothing about canonical line items or mapping
mechanisms (alternatives/stitching/composition) -- that is `statements.py`.
It only turns one raw us-gaap/dei tag's fact list into a clean, deduplicated,
classified set of `RawFact`s that `statements.py` can reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from fsa.normalize.schema import PeriodType
from fsa.sec.endpoints import CompanyFactsDoc

__all__ = [
    "RawFact",
    "extract_raw_facts",
    "dedupe_facts",
    "period_type_of",
    "classify_duration_days",
    "fiscal_year_end_matches",
    "fiscal_year_for_end",
    "shift_years",
    "select_exact",
]

# Tolerance windows (PLANNING.md Section 5.3.5). PLANNING.md's own
# empirically-derived range is 358-378 days; this is intentionally a few
# days wider on both sides as defense in depth for 52/53-week filers whose
# short fiscal years can run a little outside that band without actually
# being a different kind of period.
_ANNUAL_MIN_DAYS = 350
_ANNUAL_MAX_DAYS = 380
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

# Tolerance for matching a period's end date against the company's declared
# fiscal-year-end (MMDD, from `submissions`) -- accommodates 52/53-week
# filers whose actual close date drifts a few days year to year (e.g. "the
# last Saturday in September") without abandoning the FYE-derived check
# altogether (PLANNING.md Section 5.3.4: derive periods from dates and the
# submitted fiscal year end, never fy/fp).
_FYE_MATCH_TOLERANCE_DAYS = 10


@dataclass(frozen=True)
class RawFact:
    """One deduplicated XBRL fact for a single (tag, unit).

    ``start is None`` marks an INSTANT fact; both set marks DURATION.
    """

    value: Decimal
    start: date | None
    end: date
    filed: date
    accession: str | None
    frame: str | None
    form: str | None

    @property
    def period_type(self) -> PeriodType:
        return PeriodType.INSTANT if self.start is None else PeriodType.DURATION

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


def _to_decimal(raw_val: object) -> Decimal | None:
    """Convert a raw JSON numeric value to Decimal via its *string* form.

    Never via ``Decimal(float(...))`` -- that would launder a float's binary
    imprecision straight back in. ``json.loads`` in Python already yields
    ``int`` for whole numbers in these payloads (XBRL facts are integers in
    the reporting currency's minor-unit-free form), so ``str(raw_val)`` is
    exact for the ``int`` case and a faithful decimal literal for ``float``
    (e.g. EPS values, which do arrive as JSON floats like ``6.13``).
    """
    if raw_val is None:
        return None
    try:
        return Decimal(str(raw_val))
    except InvalidOperation:
        return None


def extract_raw_facts(company_facts: CompanyFactsDoc, *, namespace: str, tag: str, unit: str) -> list[RawFact]:
    """Every raw fact for one (namespace, tag, unit), deduplicated.

    Returns facts sorted by ``end`` (ties broken by ``start``), one entry per
    unique ``(start, end)`` -- see :func:`dedupe_facts` for the tie-break
    rule. Returns an empty list (never raises) when the tag or unit simply
    isn't present for this filer -- "this filer doesn't use this tag" is the
    normal case, not an error (mirrors ``CompanyFactsDoc.tags()``'s own
    convention).
    """
    tag_data = company_facts.raw.get("facts", {}).get(namespace, {}).get(tag, {})
    unit_facts = tag_data.get("units", {}).get(unit, [])

    parsed: list[RawFact] = []
    for entry in unit_facts:
        value = _to_decimal(entry.get("val"))
        end_raw = entry.get("end")
        filed_raw = entry.get("filed")
        if value is None or not end_raw or not filed_raw:
            continue
        start_raw = entry.get("start")
        try:
            end = date.fromisoformat(end_raw)
            filed = date.fromisoformat(filed_raw)
            start = date.fromisoformat(start_raw) if start_raw else None
        except ValueError:
            continue
        parsed.append(
            RawFact(
                value=value,
                start=start,
                end=end,
                filed=filed,
                accession=entry.get("accn"),
                frame=entry.get("frame"),
                form=entry.get("form"),
            )
        )
    return dedupe_facts(parsed)


def dedupe_facts(facts: list[RawFact]) -> list[RawFact]:
    """Deduplicate by (start, end), taking the fact with the latest `filed`.

    PLANNING.md Section 5.3.2: the same period is often reported multiple
    times across filings (restatements, or simply being carried as a prior-
    year comparative in each subsequent filing). Latest `filed` wins. Where
    the latest `filed` is tied across more than one entry for the same
    period (seen in practice when a fact is refiled the same day across
    related documents), a `frame`-corroborated entry is preferred as SEC's
    own signal of canonical status for that calendar period; otherwise the
    first one encountered is kept, deterministically (stable sort).
    """
    best: dict[tuple[date | None, date], RawFact] = {}
    for fact in facts:
        key = (fact.start, fact.end)
        current = best.get(key)
        if current is None:
            best[key] = fact
            continue
        if fact.filed > current.filed:
            best[key] = fact
        elif fact.filed == current.filed and fact.frame and not current.frame:
            best[key] = fact
        # else: keep `current` -- either strictly newer already, or an exact
        # tie with no frame-based tiebreak available, in which case the
        # first-seen entry is kept for determinism.
    return sorted(best.values(), key=lambda f: (f.end, f.start or date.min))


def period_type_of(fact: RawFact) -> PeriodType:
    return fact.period_type


def classify_duration_days(days: int) -> str:
    """Classify a DURATION fact's day-count as "annual", "quarterly", or
    "other" (PLANNING.md Section 5.3.5). "other" covers half-year and
    three-quarter YTD spans, which are real and used by `ltm.py`'s
    stitching, but are neither an annual nor a quarterly period on their
    own."""
    if _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
        return "annual"
    if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
        return "quarterly"
    return "other"


def fiscal_year_end_matches(end: date, fiscal_year_end_mmdd: str | None, *, tolerance_days: int = _FYE_MATCH_TOLERANCE_DAYS) -> bool:
    """Whether `end` falls within `tolerance_days` of the company's declared
    fiscal-year-end month/day (from `submissions.fiscalYearEnd`, "MMDD").

    Guards against misclassifying a stray ~365-day duration fact (e.g. a
    non-standard disclosure window) as a fiscal year when its end date has
    nothing to do with the filer's actual fiscal calendar. If the company's
    FYE isn't available at all, this permissively returns True (day-count
    alone still applies) rather than discarding otherwise-good data over a
    missing metadata field.
    """
    if not fiscal_year_end_mmdd or len(fiscal_year_end_mmdd) != 4 or not fiscal_year_end_mmdd.isdigit():
        return True
    month, day = int(fiscal_year_end_mmdd[:2]), int(fiscal_year_end_mmdd[2:])
    best_delta = None
    for year in (end.year - 1, end.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            # e.g. FYE "0229" in a non-leap year -- fall back to the 28th.
            candidate = date(year, month, min(day, 28))
        delta = abs((end - candidate).days)
        if best_delta is None or delta < best_delta:
            best_delta = delta
    return best_delta is not None and best_delta <= tolerance_days


def fiscal_year_for_end(end: date) -> int:
    """The fiscal year label for a period ending on `end`.

    Simplification, flagged in the Phase 2 report: this project's full test-
    ticker set (AAPL, MSFT, KO, TSLA, JPM, RDDT, CAVA, XOM) all label their
    fiscal year by the calendar year in which it *ends* -- true even for
    MSFT (FYE June 30: "fiscal 2024" ended 2024-06-30) and for AAPL's
    52/53-week September close. A retail-style filer whose fiscal year ends
    in late January/early February and is conventionally labeled by the
    *prior* calendar year (e.g. "fiscal 2023" ending January 2024) would
    need a different rule; none of the required test tickers have that
    calendar, and PLANNING.md Section 5.3.4 forbids deriving this from the
    unreliable `fy`/`fp` fields, which would otherwise be the natural place
    to resolve that ambiguity. Judgment call -- flagged for the orchestrator.
    """
    return end.year


def shift_years(d: date, years: int) -> date:
    """`d` shifted by whole `years`, handling Feb 29 by clamping to the 28th."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def select_exact(facts: list[RawFact], *, start: date | None, end: date) -> RawFact | None:
    """The single deduplicated fact matching this exact (start, end).

    Exact match only -- never "nearest" or "last N" -- because approximate
    matching is exactly the bug class Section 5.1 warns about (a line item
    silently drifting off the axis established by the anchor).
    """
    for fact in facts:
        if fact.start == start and fact.end == end:
            return fact
    return None
