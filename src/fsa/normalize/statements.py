"""Build Income Statement / Balance Sheet / Cash Flow Statement from facts + mappings.

Phase 2 responsibility (PLANNING.md Section 5.1 "the period axis rule" and
Section 5.2 "mapping mechanisms").

Critical invariant, quoting PLANNING.md Section 5.1 directly: the fiscal-year
axis must be established ONCE from the anchor line item (revenue), then every
other line item is filled per-period against that fixed axis, leaving an
explicit gap where a period has no fact. Never take "the last N periods this
tag happens to have" -- confirmed to silently misalign series (TSLA D&A
resolves to stale FY2013-2017 data under that naive approach).

Applies all three mapping mechanisms from `mappings/us_gaap.yaml`:
alternatives, per-period stitching (with a STITCH_DISAGREEMENT check where
tags overlap), and composition (summing component tags). A fourth resolution
kind, `derived: true` in the YAML, computes a line item from other
already-resolved canonical line items rather than from tags at all -- used
for `change_in_nwc` (see mappings/us_gaap.yaml for why no tag or safe
composition covers working-capital change, and why the balance-sheet-delta
definition replaced an earlier cash-flow-residual design that silently
absorbed deferred taxes/impairments/disposal gains -- Phase 2 review
correction) and reused by `ltm.py` for the LTM flow column.

A narrow fifth exception, `_backfill_total_liabilities`, fills
`total_liabilities` as `total_assets - total_equity` (also DERIVED) only
when the direct `Liabilities` tag is absent (confirmed: KO never tags it) --
"behind the direct tags" per the Phase 2 review, not a YAML-declared
mechanism, since it is the one canonical item where a same-statement
identity is a safe, exact fallback.

A `LongTermDebt - short_term_debt` backfill for `long_term_debt` was tried
and reverted (Phase 2 review, round 3): `LongTermDebt` is not consistently
"the noncurrent-debt total including the current portion" the way it first
appeared from AAPL alone. Confirmed on TSLA: the identity holds in
2011-2012 (`LongTermDebt` = `LongTermDebtNoncurrent` + `LongTermDebtCurrent`
exactly) but breaks by FY2022, where `short_term_debt` resolves to
`DebtCurrent` (a broader current-debt concept that includes current finance
leases) while `LongTermDebt` excludes finance leases entirely --
subtracting the two then understates FY2022 noncurrent debt by ~99% ($13M
vs. the real ~$1,029M). The "alternatives must be true synonyms" rule
applies to subtraction operands too: the minuend and subtrahend must have
matching scope, and for TSLA post-ASC-842 they do not, with no reliable way
to detect the mismatch generically or recalibrate per period (tag pairings
shift again at other filers/periods). For a filer with no noncurrent tag at
all, `long_term_debt` is therefore left an honest gap, and
`DEBT_CONVENTION_UNKNOWN` (see below) flags it explicitly rather than
guessing. `LineItemSpec.derived_fallback_tag` and its YAML validation were
part of that reverted mechanism and have been removed as unused.

This module also declares the `is_subtotal` line items (`total_debt`,
`net_debt`, `gross_profit`) as empty-celled `LineItem`s -- their actual
values are computed downstream by the Excel layer as native SUM formulas
over their components, not in Python. That matters here because Excel
treats a blank cell as zero: a gap in a component (e.g. a missing
`long_term_debt`) would silently understate its subtotal, overstate equity
value, and produce a wrong implied share price with no visible error. The
Excel layer must render a gap as `#N/A` or another propagating sentinel,
never a blank, when it builds those SUM formulas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from fsa.normalize import facts as facts_mod
from fsa.normalize import ltm as ltm_mod
from fsa.normalize.facts import RawFact
from fsa.normalize.schema import (
    Cell,
    DataWarning,
    FinancialModel,
    FiscalPeriod,
    LineItem,
    Mechanism,
    PeriodType,
    Provenance,
    Sign,
    Statement,
)
from fsa.sec.endpoints import CompanyFactsDoc, CompanyRef, SubmissionsDoc

logger = logging.getLogger("fsa.normalize.statements")

__all__ = [
    "LineItemSpec",
    "MappingError",
    "load_mapping",
    "build_financial_model",
    "DEFAULT_MAPPING_PATH",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAPPING_PATH = _REPO_ROOT / "mappings" / "us_gaap.yaml"

# SIC major group "Finance, Insurance, and Real Estate" (division H), covering
# depository/non-depository credit institutions, brokers, insurance, and real
# estate/REITs -- PLANNING.md Section 5.3.9. JPM (SIC 6021) falls inside this.
_FINANCIAL_SECTOR_SIC_RANGE = range(6000, 6800)

# Overlap-comparison tolerance for the STITCHED mechanism (PLANNING.md
# Section 5.2): two tags reporting the same period are treated as agreeing
# if they're within this relative tolerance. Sized empirically against a
# real, confirmed-benign case (TSLA FY2018 revenue: RevenueFromContract...
# vs. Revenues differ by 0.125% purely from a later filing's rounding to the
# nearest million) while still catching a real disagreement (KO FY2017:
# SalesRevenueGoodsNet vs. the ASC-606-restated Revenues figure differ by
# 2.3% -- ratified as a genuine STITCH_DISAGREEMENT, see the Phase 2 report).
_STITCH_TOLERANCE = Decimal("0.005")

_KNOWN_DERIVED_KEYS = frozenset({"change_in_nwc"})

# `shares_outstanding_dei` is dated to the 10-K/10-Q *cover page* (roughly
# the filing date), not the fiscal period end -- confirmed against every one
# of the 8 required test tickers: e.g. AAPL's FY2025 (ended 2025-09-27) cover
# page shares-outstanding fact is dated 2025-10-17, three weeks later.
# Exact (start, end) matching against the axis (the rule every other line
# item correctly follows, Section 5.1) would therefore *always* miss for
# this one field, regardless of filer -- not a mapping gap, a difference in
# what the field's own date means. Addressed with a documented, narrow
# exception: `cover_page: true` in the YAML resolves the closest cover-page
# snapshot on or shortly after each axis period's end (within
# `_COVER_PAGE_TOLERANCE_DAYS`), never before it and never more than one
# filing cycle late. See the Phase 2 report for why this is flagged as an
# addition to Section 5.2's three mechanisms rather than folded in silently.
_COVER_PAGE_TOLERANCE_DAYS = 120

# `LongTermDebt` is deliberately absent from `long_term_debt`'s own `tags:`
# list in mappings/us_gaap.yaml -- its scope (noncurrent-only vs.
# noncurrent-plus-current, and whether it includes finance leases) is not
# consistent across filers or even across a single filer's own history
# (confirmed on TSLA -- see `_check_debt_convention_unknown`). It is
# referenced here, independently of any LineItemSpec, purely to detect and
# warn about the situation, never to compute a value from it.
_LONG_TERM_DEBT_TOTAL_TAG = "LongTermDebt"


class MappingError(ValueError):
    """Raised for a malformed or self-inconsistent `mappings/us_gaap.yaml`."""


@dataclass(frozen=True)
class LineItemSpec:
    """One parsed entry from `mappings/us_gaap.yaml` (PLANNING.md Section 13.3)."""

    key: str
    label: str
    statement: Statement
    period_type: PeriodType
    sign: Sign
    namespace: str = "us-gaap"
    unit: str | None = None
    anchor: bool = False
    is_subtotal: bool = False
    derived: bool = False
    cover_page: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    compose: tuple[str, ...] = field(default_factory=tuple)


def load_mapping(path: Path | None = None) -> tuple[LineItemSpec, ...]:
    """Load and validate `mappings/us_gaap.yaml`.

    Raises :class:`MappingError` on anything that would silently produce a
    wrong or empty model: no anchor, more than one anchor, a fetchable item
    with no tags at all, a `derived` item this module doesn't know how to
    compute, etc. Fails loudly at load time rather than at first use.
    """
    mapping_path = path if path is not None else DEFAULT_MAPPING_PATH
    try:
        raw = yaml.safe_load(mapping_path.read_text())
    except OSError as exc:
        raise MappingError(f"Could not read mapping file {mapping_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise MappingError(f"Mapping file {mapping_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or not raw:
        raise MappingError(f"Mapping file {mapping_path} did not parse to a non-empty mapping")

    specs: list[LineItemSpec] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise MappingError(f"Mapping entry {key!r} must be a mapping, got {type(entry).__name__}")
        try:
            statement = Statement(entry["statement"])
            period_type = PeriodType(entry["period_type"])
            sign = Sign(entry["sign"])
        except KeyError as exc:
            raise MappingError(f"Mapping entry {key!r} is missing required field {exc}") from exc
        except ValueError as exc:
            raise MappingError(f"Mapping entry {key!r} has an invalid enum value: {exc}") from exc

        is_subtotal = bool(entry.get("is_subtotal", False))
        derived = bool(entry.get("derived", False))
        cover_page = bool(entry.get("cover_page", False))
        anchor = bool(entry.get("anchor", False))
        tags = tuple(entry.get("tags", []) or [])
        compose = tuple(entry.get("compose", []) or [])
        unit = entry.get("unit")
        namespace = entry.get("namespace", "us-gaap")
        label = entry.get("label", key)

        if is_subtotal and derived:
            raise MappingError(f"Mapping entry {key!r} cannot be both is_subtotal and derived")
        if is_subtotal:
            if tags or compose:
                raise MappingError(f"Mapping entry {key!r} is_subtotal=True must not declare tags/compose")
        elif derived:
            if tags or compose:
                raise MappingError(f"Mapping entry {key!r} derived=True must not declare tags/compose")
            if key not in _KNOWN_DERIVED_KEYS:
                raise MappingError(
                    f"Mapping entry {key!r} is derived=True but this module has no derivation "
                    f"logic registered for it (known: {sorted(_KNOWN_DERIVED_KEYS)})"
                )
        else:
            if not tags:
                raise MappingError(f"Mapping entry {key!r} must declare at least one tag (or is_subtotal/derived)")
            if not unit:
                raise MappingError(f"Mapping entry {key!r} must declare a unit")

        if anchor and (is_subtotal or derived):
            raise MappingError(f"Mapping entry {key!r} cannot be the anchor and also is_subtotal/derived")
        if anchor and period_type is not PeriodType.DURATION:
            raise MappingError(f"Anchor entry {key!r} must be period_type=duration (the axis is duration-based)")
        if cover_page and period_type is not PeriodType.INSTANT:
            raise MappingError(f"Mapping entry {key!r} cover_page=True must be period_type=instant")
        if cover_page and (is_subtotal or derived or anchor):
            raise MappingError(f"Mapping entry {key!r} cannot combine cover_page with is_subtotal/derived/anchor")

        specs.append(
            LineItemSpec(
                key=key,
                label=label,
                statement=statement,
                period_type=period_type,
                sign=sign,
                namespace=namespace,
                unit=unit,
                anchor=anchor,
                is_subtotal=is_subtotal,
                derived=derived,
                cover_page=cover_page,
                tags=tags,
                compose=compose,
            )
        )

    anchors = [s for s in specs if s.anchor]
    if len(anchors) != 1:
        raise MappingError(
            f"Mapping file {mapping_path} must declare exactly one anchor:true line item, found {len(anchors)}"
        )

    return tuple(specs)


def _apply_sign(value: Decimal, sign: Sign) -> Decimal:
    return -value if sign is Sign.NEGATED else value


def _extract_all_tag_facts(spec: LineItemSpec, company_facts: CompanyFactsDoc) -> dict[str, list[RawFact]]:
    all_tags = (*spec.tags, *spec.compose)
    result: dict[str, list[RawFact]] = {}
    for tag in all_tags:
        if tag in result:
            continue
        result[tag] = facts_mod.extract_raw_facts(company_facts, namespace=spec.namespace, tag=tag, unit=spec.unit)
    return result


def _values_agree(a: Decimal, b: Decimal, tolerance: Decimal = _STITCH_TOLERANCE) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return a == b
    return (abs(a - b) / denom) <= tolerance


def _missing_cell(unit: str, period_label: str) -> Cell:
    return Cell(
        value=None,
        unit=unit,
        period_label=period_label,
        provenance=Provenance(mechanism=Mechanism.MISSING, tag=None),
    )


def resolve_span(
    spec: LineItemSpec,
    facts_by_tag: dict[str, list[RawFact]],
    *,
    start: date | None,
    end: date,
    period_label: str,
    warnings_out: list[DataWarning] | None = None,
) -> Cell:
    """Resolve one canonical line item's value for one EXACT (start, end)
    span -- the "per-period fill against the fixed axis" rule (PLANNING.md
    Section 5.1). Never approximate-matches; a period with no exact fact is
    a gap, not a nearby substitute.

    Tries `spec.tags` (ALTERNATIVES; overlapping matches are compared and any
    disagreement beyond tolerance is recorded as STITCH_DISAGREEMENT via
    `warnings_out`, still resolving to the highest-priority tag's value),
    then `spec.compose` (COMPOSITION: sum of whichever component tags have a
    value for this exact span), then a gap (MISSING).
    """
    matches: dict[str, RawFact] = {}
    for tag in spec.tags:
        fact = facts_mod.select_exact(facts_by_tag.get(tag, []), start=start, end=end)
        if fact is not None:
            matches[tag] = fact

    if matches:
        winner_tag = next(tag for tag in spec.tags if tag in matches)
        winner_fact = matches[winner_tag]
        if len(matches) == 1:
            mechanism = Mechanism.DIRECT if winner_tag == spec.tags[0] else Mechanism.ALTERNATIVE
        else:
            mechanism = Mechanism.STITCHED
            for tag, fact in matches.items():
                if tag == winner_tag:
                    continue
                if not _values_agree(winner_fact.value, fact.value):
                    if warnings_out is not None:
                        warnings_out.append(
                            DataWarning(
                                code="STITCH_DISAGREEMENT",
                                message=(
                                    f"{spec.key}: tags {winner_tag!r} and {tag!r} disagree for "
                                    f"period {period_label} (resolved using {winner_tag!r})"
                                ),
                                detail={
                                    "key": spec.key,
                                    "period_label": period_label,
                                    "winner_tag": winner_tag,
                                    "winner_value": str(winner_fact.value),
                                    "other_tag": tag,
                                    "other_value": str(fact.value),
                                },
                            )
                        )
        value = _apply_sign(winner_fact.value, spec.sign)
        return Cell(
            value=value,
            unit=spec.unit,
            period_label=period_label,
            provenance=Provenance(
                mechanism=mechanism,
                tag=winner_tag,
                accession=winner_fact.accession,
                filed=winner_fact.filed,
                frame_corroborated=bool(winner_fact.frame),
            ),
        )

    if spec.compose:
        used: dict[str, RawFact] = {}
        for tag in spec.compose:
            fact = facts_mod.select_exact(facts_by_tag.get(tag, []), start=start, end=end)
            if fact is not None:
                used[tag] = fact
        if used:
            total = sum((f.value for f in used.values()), start=Decimal(0))
            value = _apply_sign(total, spec.sign)
            latest_filed = max(f.filed for f in used.values())
            latest_accession = next(f.accession for f in used.values() if f.filed == latest_filed)
            return Cell(
                value=value,
                unit=spec.unit,
                period_label=period_label,
                provenance=Provenance(
                    mechanism=Mechanism.COMPOSED,
                    tag=None,
                    component_tags=tuple(t for t in spec.compose if t in used),
                    accession=latest_accession,
                    filed=latest_filed,
                    frame_corroborated=any(f.frame for f in used.values()),
                ),
            )

    return _missing_cell(spec.unit, period_label)


def resolve_cover_page(
    spec: LineItemSpec, facts_by_tag: dict[str, list[RawFact]], *, period_end: date, period_label: str
) -> Cell:
    """Resolve a `cover_page: true` line item (currently only
    `shares_outstanding_dei`) for one axis period.

    Unlike :func:`resolve_span`, this does not require an exact date match
    -- cover-page facts are dated to the filing, not the fiscal period end
    (see the module-level note by `_COVER_PAGE_TOLERANCE_DAYS`). Picks the
    closest INSTANT fact on or after `period_end`, within tolerance, trying
    `spec.tags` in priority order at each candidate date (no stitching/
    composition for this field -- there is nothing to compose, and
    dei-namespace filers don't multiply-tag it the way us-gaap concepts get
    stitched across accounting-standard transitions).
    """
    best: tuple[int, str, RawFact] | None = None
    for tag in spec.tags:
        for fact in facts_by_tag.get(tag, []):
            if fact.start is not None:
                continue
            delta = (fact.end - period_end).days
            if 0 <= delta <= _COVER_PAGE_TOLERANCE_DAYS:
                if best is None or delta < best[0]:
                    best = (delta, tag, fact)
    if best is None:
        return _missing_cell(spec.unit, period_label)
    _, tag, fact = best
    value = _apply_sign(fact.value, spec.sign)
    mechanism = Mechanism.DIRECT if tag == spec.tags[0] else Mechanism.ALTERNATIVE
    return Cell(
        value=value,
        unit=spec.unit,
        period_label=period_label,
        provenance=Provenance(
            mechanism=mechanism, tag=tag, accession=fact.accession, filed=fact.filed, frame_corroborated=bool(fact.frame)
        ),
    )


def _resolve_annual_series(
    spec: LineItemSpec,
    company_facts: CompanyFactsDoc,
    *,
    fiscal_year_end: str | None,
    warnings_out: list[DataWarning],
) -> tuple[dict[int, Cell], dict[str, list[RawFact]]]:
    """Resolve the anchor's own annual series, grouped by fiscal year label
    (PLANNING.md Section 5.3.4: derive fiscal years from dates + the
    company's fiscal year end, never from `fy`/`fp`).

    Unlike :func:`resolve_span`, this groups candidate facts by *fiscal year*
    rather than requiring an identical (start, end) across tags -- this is
    the one place that's appropriate, since this function's entire job is to
    *establish* the axis, not fill against an already-fixed one. Every other
    line item is resolved via :func:`resolve_span` against the exact
    (start, end) this function settles on for each fiscal year.
    """
    facts_by_tag = _extract_all_tag_facts(spec, company_facts)

    annual_by_year: dict[int, dict[str, RawFact]] = {}
    for tag, fact_list in facts_by_tag.items():
        for fact in fact_list:
            if fact.days is None:
                continue
            if facts_mod.classify_duration_days(fact.days) != "annual":
                continue
            if not facts_mod.fiscal_year_end_matches(fact.end, fiscal_year_end):
                continue
            fy = facts_mod.fiscal_year_for_end(fact.end)
            annual_by_year.setdefault(fy, {})[tag] = fact

    resolved: dict[int, Cell] = {}
    for fy, tag_facts in annual_by_year.items():
        winner_tag = next(tag for tag in spec.tags if tag in tag_facts)
        winner_fact = tag_facts[winner_tag]
        period_label = f"FY{fy}"
        if len(tag_facts) == 1:
            mechanism = Mechanism.DIRECT if winner_tag == spec.tags[0] else Mechanism.ALTERNATIVE
        else:
            mechanism = Mechanism.STITCHED
            for tag, fact in tag_facts.items():
                if tag == winner_tag:
                    continue
                if not _values_agree(winner_fact.value, fact.value):
                    warnings_out.append(
                        DataWarning(
                            code="STITCH_DISAGREEMENT",
                            message=(
                                f"{spec.key}: tags {winner_tag!r} and {tag!r} disagree for "
                                f"{period_label} (resolved using {winner_tag!r})"
                            ),
                            detail={
                                "key": spec.key,
                                "period_label": period_label,
                                "winner_tag": winner_tag,
                                "winner_value": str(winner_fact.value),
                                "other_tag": tag,
                                "other_value": str(fact.value),
                            },
                        )
                    )
        value = _apply_sign(winner_fact.value, spec.sign)
        resolved[fy] = Cell(
            value=value,
            unit=spec.unit,
            period_label=period_label,
            provenance=Provenance(
                mechanism=mechanism,
                tag=winner_tag,
                accession=winner_fact.accession,
                filed=winner_fact.filed,
                frame_corroborated=bool(winner_fact.frame),
            ),
        )
    return resolved, facts_by_tag


def _has_genuine_former_name_change(submissions: SubmissionsDoc) -> bool:
    """Whether `submissions.former_names` records an ACTUAL identity change,
    not merely the current name's own validity segment.

    Confirmed against live data while broadening the registrant-change
    trigger: SEC's `formerNames` array is not purely historical -- it can
    include an entry for the name *currently* in force (e.g. RDDT's sole
    entry is literally {"name": "Reddit, Inc.", ...}, identical to its own
    `entity_name`, with a `to` date that tracks "today"; JPM's list mixes
    three genuine former names with one entry that equally matches its
    current name). Filtering to entries whose `name` differs from the
    current `entity_name` distinguishes a real rename (AAPL, TSLA, JPM) from
    this artifact (RDDT) without relying on parsing/interpreting the `to`
    date, which is the more fragile signal.
    """
    return any(fn.get("name") != submissions.entity_name for fn in submissions.former_names)


def _has_non_usd_data(spec: LineItemSpec, company_facts: CompanyFactsDoc) -> bool:
    us_gaap = company_facts.raw.get("facts", {}).get(spec.namespace, {})
    for tag in spec.tags:
        units = us_gaap.get(tag, {}).get("units", {})
        for unit_key, entries in units.items():
            if unit_key != spec.unit and entries:
                return True
    return False


_NWC_COMPONENT_KEYS = ("accounts_receivable", "inventory", "accounts_payable")


def _nwc_at(cells_by_key: dict[str, dict[str, Cell]], period_label: str) -> tuple[Decimal | None, list[date]]:
    """Net working capital = accounts_receivable + inventory - accounts_payable
    at one period, or (None, []) if any of the three is a gap there."""
    ar_cell = cells_by_key.get("accounts_receivable", {}).get(period_label)
    inv_cell = cells_by_key.get("inventory", {}).get(period_label)
    ap_cell = cells_by_key.get("accounts_payable", {}).get(period_label)
    if ar_cell is None or inv_cell is None or ap_cell is None:
        return None, []
    if ar_cell.value is None or inv_cell.value is None or ap_cell.value is None:
        return None, []
    filed_dates = [c.provenance.filed for c in (ar_cell, inv_cell, ap_cell) if c.provenance.filed is not None]
    return ar_cell.value + inv_cell.value - ap_cell.value, filed_dates


def _derive_change_in_nwc(
    cells_by_key: dict[str, dict[str, Cell]], period_label: str, prior_period_label: str | None, unit: str
) -> Cell:
    """DERIVED: change_in_nwc[t] = nwc[t] - nwc[t-1], where
    nwc = accounts_receivable + inventory - accounts_payable (all
    already-resolved canonical balance-sheet items). Exactly defined by its
    three inputs at each of the two periods -- no residual absorption of
    unrelated non-cash items (Phase 2 review correction; see
    mappings/us_gaap.yaml for the full rationale and why the earlier
    CFO-residual design was wrong).

    ``prior_period_label=None`` (the first axis period, with nothing to
    difference against) is always a gap -- never approximated as 0.
    """
    if prior_period_label is None:
        return _missing_cell(unit, period_label)
    this_nwc, this_filed = _nwc_at(cells_by_key, period_label)
    prior_nwc, prior_filed = _nwc_at(cells_by_key, prior_period_label)
    if this_nwc is None or prior_nwc is None:
        return _missing_cell(unit, period_label)
    filed_dates = this_filed + prior_filed
    return Cell(
        value=this_nwc - prior_nwc,
        unit=unit,
        period_label=period_label,
        provenance=Provenance(
            mechanism=Mechanism.DERIVED,
            tag=None,
            filed=max(filed_dates) if filed_dates else None,
        ),
    )


def _backfill_total_liabilities(cells_by_key: dict[str, dict[str, Cell]], period_labels: list[str], unit: str) -> None:
    """Fill `total_liabilities` as `total_assets - total_equity`
    (Mechanism.DERIVED) for any period where the direct `Liabilities` tag
    resolved to a gap -- confirmed necessary for KO, which never tags
    `Liabilities` at all across its whole 10-year window. Mutates
    `cells_by_key["total_liabilities"]` in place; a no-op if any of the
    three line items isn't present in `cells_by_key` (e.g. a mapping
    variant that dropped one of them). Direct-tag resolutions are left
    untouched -- this only ever fills an existing gap, never overrides a
    real fact."""
    if not all(k in cells_by_key for k in ("total_liabilities", "total_assets", "total_equity")):
        return
    for period_label in period_labels:
        cell = cells_by_key["total_liabilities"].get(period_label)
        if cell is None or cell.value is not None:
            continue
        assets_cell = cells_by_key["total_assets"].get(period_label)
        equity_cell = cells_by_key["total_equity"].get(period_label)
        if assets_cell is None or equity_cell is None:
            continue
        if assets_cell.value is None or equity_cell.value is None:
            continue
        filed_dates = [c.provenance.filed for c in (assets_cell, equity_cell) if c.provenance.filed is not None]
        cells_by_key["total_liabilities"][period_label] = Cell(
            value=assets_cell.value - equity_cell.value,
            unit=unit,
            period_label=period_label,
            provenance=Provenance(
                mechanism=Mechanism.DERIVED,
                tag=None,
                filed=max(filed_dates) if filed_dates else None,
            ),
        )


def _check_debt_convention_unknown(
    cells_by_key: dict[str, dict[str, Cell]],
    periods: list[FiscalPeriod],
    total_debt_tag_facts: list[RawFact],
    total_debt_tag_name: str,
    ticker: str,
) -> DataWarning | None:
    """Detect the situation a reverted `long_term_debt` backfill used to
    paper over silently: a filer with no noncurrent-debt tag at all (so
    `long_term_debt` is a gap) that nonetheless tags `LongTermDebt` for the
    same period.

    That combination was previously "fixed" by computing
    `LongTermDebt - short_term_debt`, on the premise that `LongTermDebt` is
    always the noncurrent-plus-current total. It is not: confirmed on TSLA,
    that identity holds in 2011-2012 but breaks by FY2022, where
    `short_term_debt` resolves to `DebtCurrent` (a broader current-debt
    concept covering current finance leases) while `LongTermDebt` excludes
    finance leases entirely -- the subtraction then understated FY2022
    noncurrent debt by ~99% ($13M computed vs. ~$1,029M actual). There is no
    reliable way to detect or recalibrate for this scope mismatch generically
    (tag pairings shift again at other filers/periods), so automatic
    calibration was deliberately rejected in favor of an honest gap plus this
    explicit warning.

    Returns a single `DataWarning` (not one per period -- the axis-wide
    situation is what matters to the user) if `long_term_debt` has at least
    one gap where a `LongTermDebt` fact exists for that same period, else
    `None`. A no-op (returns `None`) if `long_term_debt` isn't in
    `cells_by_key` at all.
    """
    if "long_term_debt" not in cells_by_key:
        return None
    affected_labels = []
    for period in periods:
        cell = cells_by_key["long_term_debt"].get(period.label)
        if cell is None or cell.value is not None:
            continue
        if facts_mod.select_exact(total_debt_tag_facts, start=None, end=period.end) is not None:
            affected_labels.append(period.label)
    if not affected_labels:
        return None
    return DataWarning(
        code="DEBT_CONVENTION_UNKNOWN",
        message=(
            f"{ticker}: long-term debt cannot be split reliably for "
            f"{', '.join(affected_labels)}. This filer reports no noncurrent "
            f"long-term-debt tag, and `{total_debt_tag_name}` (the only debt "
            "total available) cannot be safely matched in scope to the "
            "available current-debt tag to back it out -- their coverage of "
            "items like finance leases can differ, and that mismatch is not "
            "reliably detectable or calibratable. net_debt is therefore "
            "unavailable for these periods; consult the filing directly."
        ),
        detail={"ticker": ticker, "periods": affected_labels, "total_debt_tag": total_debt_tag_name},
    )


def build_financial_model(
    company: CompanyRef,
    submissions: SubmissionsDoc,
    company_facts: CompanyFactsDoc,
    *,
    historical_years: int,
    mapping_path: Path | None = None,
) -> FinancialModel:
    """Build the canonical :class:`FinancialModel` for one company.

    Implements PLANNING.md Section 5.1 (period axis established once from
    revenue, every other line item filled per-period against it) and Section
    5.2 (all three mapping mechanisms). Warnings ordering follows Section
    5.3.11 explicitly: REGISTRANT_CHANGE_SUSPECTED is checked before
    THIN_HISTORY so a century-old filer whose ticker resolves to a near-empty
    registrant CIK is never misclassified as a young filer.
    """
    specs = load_mapping(mapping_path)
    anchor_spec = next(s for s in specs if s.anchor)
    warnings: list[DataWarning] = []

    annual_series, anchor_facts_by_tag = _resolve_annual_series(
        anchor_spec, company_facts, fiscal_year_end=submissions.fiscal_year_end, warnings_out=warnings
    )
    total_years_available = len(annual_series)

    non_usd = total_years_available == 0 and _has_non_usd_data(anchor_spec, company_facts)
    # Broadened per Phase 2 review: `anchor_periods == 0` alone (XOM) is
    # unambiguous, but a reorg that left two or three periods on the new CIK
    # would otherwise fall straight through to THIN_HISTORY and be
    # misdiagnosed as a young filer. A genuine former-name change (see
    # _has_genuine_former_name_change) is a secondary signal a young,
    # never-renamed IPO filer (CAVA: no formerNames entries at all) doesn't
    # share -- note this is NOT simply "formerNames non-empty": RDDT has one
    # entry, but it is its own current name re-recorded, not a real rename
    # (confirmed live; see that function's docstring). Perfect
    # discrimination isn't achievable with the data Phase 1 exposes; the
    # THIN_HISTORY message below names the registrant-change possibility
    # explicitly for the remaining ambiguous band instead of trying to
    # resolve it silently.
    registrant_change_suspected = (not non_usd) and (
        total_years_available == 0
        or (total_years_available <= 2 and _has_genuine_former_name_change(submissions))
    )

    if non_usd:
        warnings.append(
            DataWarning(
                code="NON_USD",
                message=(
                    f"{company.ticker}: no USD-denominated {anchor_spec.key} facts found, but "
                    "non-USD-denominated facts exist for the same tag(s). This tool supports USD "
                    "domestic filers only (PLANNING.md Section 5.3.7 / non-goals)."
                ),
                detail={"cik": company.cik},
            )
        )
    elif registrant_change_suspected:
        if total_years_available == 0:
            reason = f"has zero annual {anchor_spec.key} periods"
        else:
            reason = (
                f"has only {total_years_available} annual {anchor_spec.key} period(s) on record alongside "
                "a former-name change"
            )
        warnings.append(
            DataWarning(
                code="REGISTRANT_CHANGE_SUSPECTED",
                message=(
                    f"{company.ticker} (CIK {company.cik}) {reason}. This is the signature of a ticker "
                    "resolving to a freshly created registrant CIK while the company's real filing history "
                    "sits under a predecessor CIK (PLANNING.md Section 5.3.11 -- confirmed for XOM: CIK "
                    "2115436 vs. the legacy CIK 34088). Use --cik to target the correct registrant directly."
                ),
                detail={
                    "cik": company.cik,
                    "ticker": company.ticker,
                    "years_available": total_years_available,
                    "former_names": _has_genuine_former_name_change(submissions),
                },
            )
        )
    elif total_years_available < 3:
        warnings.append(
            DataWarning(
                code="THIN_HISTORY",
                message=(
                    f"{company.ticker}: only {total_years_available} annual {anchor_spec.key} period(s) "
                    "available (fewer than 3 years). Likely a recent IPO -- 10-Ks carry tagged prior-year "
                    "comparatives, so available history runs roughly years-public + 2 (PLANNING.md Section "
                    "5.3.10). DCF assumption defaults seeded from this history are weakly supported. A "
                    "registrant/CIK change (PLANNING.md Section 5.3.11) is also a possible cause of a "
                    "short history for an otherwise-established company -- if this ticker should have "
                    "materially more history than shown, try --cik to bypass ticker resolution entirely."
                ),
                detail={"cik": company.cik, "years_available": total_years_available},
            )
        )

    sic_raw = submissions.sic
    if sic_raw is not None:
        try:
            sic_int = int(sic_raw)
        except ValueError:
            sic_int = None
        if sic_int is not None and sic_int in _FINANCIAL_SECTOR_SIC_RANGE:
            warnings.append(
                DataWarning(
                    code="FINANCIAL_SECTOR",
                    message=(
                        f"{company.ticker}: SIC {sic_raw} ({submissions.sic_description}) falls in the "
                        "Finance/Insurance/Real Estate range. An unlevered-FCF DCF is conceptually "
                        "inappropriate for banks, insurers, and REITs (PLANNING.md Decision D4); the model "
                        "is still generated, expect many working-capital-related line items to be gaps."
                    ),
                    detail={"sic": sic_raw, "sic_description": submissions.sic_description},
                )
            )

    fiscal_years_sorted = sorted(annual_series)[-historical_years:] if annual_series else []

    # Build FiscalPeriod objects directly from the facts that won each
    # fiscal year (need start/end, which Cell itself does not carry).
    periods: list[FiscalPeriod] = []
    fact_lookup: dict[int, RawFact] = {}
    for fy in fiscal_years_sorted:
        winner_tag = annual_series[fy].provenance.tag
        # Re-find the exact fact: same tag, same fiscal year classification.
        candidate = next(
            f
            for f in anchor_facts_by_tag[winner_tag]
            if f.days is not None
            and facts_mod.classify_duration_days(f.days) == "annual"
            and facts_mod.fiscal_year_end_matches(f.end, submissions.fiscal_year_end)
            and facts_mod.fiscal_year_for_end(f.end) == fy
        )
        fact_lookup[fy] = candidate
        periods.append(
            FiscalPeriod(
                label=f"FY{fy}",
                fiscal_year=fy,
                end=candidate.end,
                start=candidate.start,
                kind=PeriodType.DURATION,
                is_ltm=False,
                days=candidate.days,
            )
        )

    line_items: list[LineItem] = []
    cells_by_key: dict[str, dict[str, Cell]] = {}

    # Anchor line item's cells, trimmed to the final windowed axis.
    anchor_cells = {f"FY{fy}": annual_series[fy] for fy in fiscal_years_sorted}
    cells_by_key[anchor_spec.key] = anchor_cells
    line_items.append(
        LineItem(
            key=anchor_spec.key,
            label=anchor_spec.label,
            statement=anchor_spec.statement,
            period_type=anchor_spec.period_type,
            sign=anchor_spec.sign,
            is_subtotal=False,
            cells=anchor_cells,
        )
    )

    fetchable_specs = [s for s in specs if not s.anchor and not s.is_subtotal and not s.derived]
    derived_specs = [s for s in specs if s.derived]
    subtotal_specs = [s for s in specs if s.is_subtotal]

    facts_cache: dict[str, dict[str, list[RawFact]]] = {}
    for spec in fetchable_specs:
        facts_by_tag = _extract_all_tag_facts(spec, company_facts)
        facts_cache[spec.key] = facts_by_tag
        item_cells: dict[str, Cell] = {}
        for period in periods:
            if spec.cover_page:
                item_cells[period.label] = resolve_cover_page(
                    spec, facts_by_tag, period_end=period.end, period_label=period.label
                )
                continue
            span_start = None if spec.period_type is PeriodType.INSTANT else period.start
            item_cells[period.label] = resolve_span(
                spec,
                facts_by_tag,
                start=span_start,
                end=period.end,
                period_label=period.label,
                warnings_out=warnings,
            )
        cells_by_key[spec.key] = item_cells
        line_items.append(
            LineItem(
                key=spec.key,
                label=spec.label,
                statement=spec.statement,
                period_type=spec.period_type,
                sign=spec.sign,
                is_subtotal=False,
                cells=item_cells,
            )
        )

    # Direct-tag total_liabilities gaps (confirmed: KO never tags
    # `Liabilities`) are backfilled from the balance-sheet identity
    # total_assets - total_equity, behind the direct tag -- see
    # mappings/us_gaap.yaml's total_liabilities entry and
    # _backfill_total_liabilities's docstring.
    _backfill_total_liabilities(cells_by_key, [p.label for p in periods], "USD")

    for spec in derived_specs:
        item_cells = {}
        for i, period in enumerate(periods):
            prior_label = periods[i - 1].label if i > 0 else None
            item_cells[period.label] = _derive_change_in_nwc(cells_by_key, period.label, prior_label, "USD")
        cells_by_key[spec.key] = item_cells
        line_items.append(
            LineItem(
                key=spec.key,
                label=spec.label,
                statement=spec.statement,
                period_type=spec.period_type,
                sign=spec.sign,
                is_subtotal=False,
                cells=item_cells,
            )
        )

    # -- LTM column (PLANNING.md Section 5, "LTM (trailing twelve months)") --
    if periods:
        latest_fy_period = periods[-1]
        ltm_period, this_span, prior_span = ltm_mod.determine_ltm_axis_period(
            anchor_facts_by_tag, latest_fy_period
        )
        if this_span is not None and prior_span is not None:
            # Confirm the ANCHOR itself resolves at both candidate spans via
            # the full alternatives/stitching machinery (the raw scan in
            # ltm.py only proposes a candidate; this is the real check).
            anchor_this = resolve_span(
                anchor_spec, anchor_facts_by_tag, start=this_span[0], end=this_span[1],
                period_label="_ltm_this_ytd", warnings_out=None,
            )
            anchor_prior = resolve_span(
                anchor_spec, anchor_facts_by_tag, start=prior_span[0], end=prior_span[1],
                period_label="_ltm_prior_ytd", warnings_out=None,
            )
            if anchor_this.value is None or anchor_prior.value is None:
                ltm_period, this_span, prior_span = ltm_mod.fallback_ltm_period(latest_fy_period), None, None

        periods = [*periods, ltm_period]

        is_case_a = this_span is not None and prior_span is not None
        for spec in fetchable_specs:
            facts_by_tag = facts_cache[spec.key]
            fy_cell = cells_by_key[spec.key][latest_fy_period.label]
            if spec.cover_page:
                # Cover-page dating (see resolve_cover_page) already
                # tolerates the fallback (Case B) situation on its own --
                # ltm_period.end equals latest_fy_period.end there, so this
                # naturally finds the same cover-page fact as the FY column.
                ltm_cell = resolve_cover_page(spec, facts_by_tag, period_end=ltm_period.end, period_label=ltm_period.label)
            elif not is_case_a:
                ltm_cell = replace(fy_cell, period_label=ltm_period.label)
            elif spec.period_type is PeriodType.INSTANT:
                ltm_cell = resolve_span(
                    spec, facts_by_tag, start=None, end=ltm_period.end,
                    period_label=ltm_period.label, warnings_out=warnings,
                )
            else:
                this_cell = resolve_span(
                    spec, facts_by_tag, start=this_span[0], end=this_span[1],
                    period_label="_ltm_this_ytd", warnings_out=None,
                )
                prior_cell = resolve_span(
                    spec, facts_by_tag, start=prior_span[0], end=prior_span[1],
                    period_label="_ltm_prior_ytd", warnings_out=None,
                )
                if fy_cell.value is None or this_cell.value is None or prior_cell.value is None:
                    ltm_cell = _missing_cell(spec.unit, ltm_period.label)
                else:
                    ltm_value = ltm_mod.compute_flow_ltm_value(fy_cell.value, this_cell.value, prior_cell.value)
                    filed_dates = [
                        c.provenance.filed
                        for c in (fy_cell, this_cell, prior_cell)
                        if c.provenance.filed is not None
                    ]
                    ltm_cell = Cell(
                        value=ltm_value,
                        unit=spec.unit,
                        period_label=ltm_period.label,
                        provenance=Provenance(
                            mechanism=Mechanism.DERIVED,
                            tag=None,
                            filed=max(filed_dates) if filed_dates else None,
                        ),
                    )
            cells_by_key[spec.key][ltm_period.label] = ltm_cell

        # Same balance-sheet-identity backfill as the historical axis, now
        # for the LTM column specifically (total_assets/total_equity's own
        # LTM cells were just resolved above).
        _backfill_total_liabilities(cells_by_key, [ltm_period.label], "USD")

        for spec in derived_specs:
            if not is_case_a:
                fy_cell = cells_by_key[spec.key][latest_fy_period.label]
                ltm_cell = replace(fy_cell, period_label=ltm_period.label)
            else:
                # "Change since the immediately preceding axis period" --
                # for the LTM column that preceding period is the latest FY,
                # consistent with how every other axis period differences
                # against the one before it.
                ltm_cell = _derive_change_in_nwc(cells_by_key, ltm_period.label, latest_fy_period.label, "USD")
            cells_by_key[spec.key][ltm_period.label] = ltm_cell

        # Anchor's own LTM cell, for completeness/display.
        if not is_case_a:
            anchor_ltm_cell = replace(cells_by_key[anchor_spec.key][latest_fy_period.label], period_label=ltm_period.label)
        else:
            anchor_ltm_value = ltm_mod.compute_flow_ltm_value(
                cells_by_key[anchor_spec.key][latest_fy_period.label].value, anchor_this.value, anchor_prior.value
            )
            anchor_ltm_cell = Cell(
                value=anchor_ltm_value,
                unit=anchor_spec.unit,
                period_label=ltm_period.label,
                provenance=Provenance(mechanism=Mechanism.DERIVED, tag=None),
            )
        cells_by_key[anchor_spec.key][ltm_period.label] = anchor_ltm_cell

        # Rebuild LineItem objects with the LTM cell folded in (LineItem is frozen).
        rebuilt: list[LineItem] = []
        for li in line_items:
            rebuilt.append(replace(li, cells=dict(cells_by_key[li.key])))
        line_items = rebuilt

    # `long_term_debt` gaps are NOT backfilled from `LongTermDebt` -- tried
    # and reverted (see this module's docstring and
    # `_check_debt_convention_unknown`): `LongTermDebt`'s scope relative to
    # `short_term_debt` is not stable enough to subtract safely across a
    # filer's own history, let alone across filers. Surface the situation
    # explicitly instead, over the full final axis (including the LTM
    # column, if one was built above), so the user knows net_debt is
    # unavailable rather than silently wrong.
    total_debt_tag_facts = facts_mod.extract_raw_facts(
        company_facts, namespace="us-gaap", tag=_LONG_TERM_DEBT_TOTAL_TAG, unit="USD"
    )
    debt_convention_warning = _check_debt_convention_unknown(
        cells_by_key, periods, total_debt_tag_facts, _LONG_TERM_DEBT_TOTAL_TAG, company.ticker
    )
    if debt_convention_warning is not None:
        warnings.append(debt_convention_warning)

    for spec in subtotal_specs:
        line_items.append(
            LineItem(
                key=spec.key,
                label=spec.label,
                statement=spec.statement,
                period_type=spec.period_type,
                sign=spec.sign,
                is_subtotal=True,
                cells={},
            )
        )

    return FinancialModel(
        company=company,
        currency="USD",
        fiscal_year_end=submissions.fiscal_year_end or "",
        periods=tuple(periods),
        line_items=tuple(line_items),
        warnings=tuple(warnings),
    )
