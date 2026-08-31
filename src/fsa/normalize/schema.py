"""Canonical Statement / LineItem / Period models.

This is the spine of the project (PLANNING.md Section 13.2): Phases 3-4 render
it, Phase 7 validates against it. Specified by the orchestrator, implemented
here exactly as specified, with the six invariants enforced at *construction*
time via ``__post_init__`` rather than left to be caught only by tests --
per the Phase 2 task brief, a wrong number that fails loudly beats one that
is merely flagged in a test file nobody re-runs.

Two deliberate, minimal additions beyond the literal Section 13.2 sketch,
both called out in the Phase 2 report:

1. ``DATA_WARNING_CODES`` -- ``DataWarning.code`` is typed ``str`` per spec
   (not a formal ``Enum``), but is validated at construction against the
   five codes Section 13.2 itself enumerates in a comment, so a typo'd code
   fails loudly rather than silently producing an unrecognized warning the
   ``Info`` sheet can't render sensibly.
2. ``Provenance.tag is None`` is documented in Section 13.2 as occurring
   "when COMPOSED or MISSING". ``Mechanism.DERIVED`` (used for the LTM
   flow-period computation and the ``change_in_nwc`` derivation,
   see ``ltm.py`` / ``statements.py``) is likewise not resolved from a single
   named tag, so it is treated the same way here: ``tag`` may be ``None`` for
   COMPOSED, DERIVED, or MISSING, and must be set for DIRECT/ALTERNATIVE/
   STITCHED (each of those, by definition, names the one tag that won).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Mapping

from fsa.sec.endpoints import CompanyRef

__all__ = [
    "Statement",
    "PeriodType",
    "Mechanism",
    "Sign",
    "FiscalPeriod",
    "Provenance",
    "Cell",
    "LineItem",
    "DataWarning",
    "FinancialModel",
    "SchemaError",
    "VALID_UNITS",
    "DATA_WARNING_CODES",
]


class SchemaError(ValueError):
    """Raised when a canonical-schema invariant (PLANNING.md Section 13.2) is
    violated at construction time. A ``ValueError`` subclass (not a bare
    ``ValueError``) so callers can catch schema violations specifically,
    distinct from e.g. a plain bad-argument error elsewhere in the codebase.
    """


class Statement(Enum):
    INCOME = "income"
    BALANCE = "balance"
    CASHFLOW = "cashflow"
    SUPPLEMENTAL = "supplemental"


class PeriodType(Enum):
    DURATION = "duration"
    INSTANT = "instant"


class Mechanism(Enum):
    DIRECT = "direct"
    ALTERNATIVE = "alternative"
    STITCHED = "stitched"
    COMPOSED = "composed"
    DERIVED = "derived"
    MISSING = "missing"


class Sign(Enum):
    AS_REPORTED = "as_reported"
    NEGATED = "negated"


# The three unit keys XBRL facts are ever selected under in this project
# (PLANNING.md Section 5.3.7). Never mix them within one canonical line item.
VALID_UNITS = frozenset({"USD", "USD/shares", "shares"})

# The five codes PLANNING.md Section 13.2 enumerates for DataWarning.code,
# plus DEBT_CONVENTION_UNKNOWN (Phase 2 review, round 3 addition): raised
# when `long_term_debt` is a gap for a filer that nonetheless tags
# `LongTermDebt`, because that tag's scope cannot be safely matched to the
# available current-debt tag to split it (see
# `_check_debt_convention_unknown` in statements.py for the full story --
# a reverted automatic-subtraction fix silently understated TSLA's FY2022
# noncurrent debt by ~99% for exactly this reason).
DATA_WARNING_CODES = frozenset(
    {
        "FINANCIAL_SECTOR",
        "THIN_HISTORY",
        "STITCH_DISAGREEMENT",
        "NON_USD",
        "REGISTRANT_CHANGE_SUSPECTED",
        "DEBT_CONVENTION_UNKNOWN",
    }
)

# Tags may resolve via a named mechanism without naming a single tag.
_TAGLESS_MECHANISMS = frozenset({Mechanism.COMPOSED, Mechanism.DERIVED, Mechanism.MISSING})


@dataclass(frozen=True)
class FiscalPeriod:
    """One column of the period axis (PLANNING.md Section 13.2).

    For the ``periods`` axis actually built by ``statements.py`` (anchored on
    revenue, a DURATION line item), every entry here has ``kind=DURATION``
    and a non-``None`` ``start``. ``start: date | None`` is nonetheless part
    of the general contract (an INSTANT period, conceptually "as of `end`",
    is representable) since the mechanism this dataclass describes is not
    inherently duration-only.
    """

    label: str
    fiscal_year: int
    end: date
    start: date | None
    kind: PeriodType
    is_ltm: bool
    days: int | None

    def __post_init__(self) -> None:
        if not self.label:
            raise SchemaError("FiscalPeriod.label must not be empty")
        if self.kind is PeriodType.INSTANT:
            if self.start is not None:
                raise SchemaError(
                    f"FiscalPeriod {self.label!r} is INSTANT but has start={self.start} "
                    "(instant periods have no start date)"
                )
            if self.days is not None:
                raise SchemaError(
                    f"FiscalPeriod {self.label!r} is INSTANT but has days={self.days} set"
                )
        else:  # DURATION
            if self.start is None:
                raise SchemaError(
                    f"FiscalPeriod {self.label!r} is DURATION but start is None"
                )
            if self.start >= self.end:
                raise SchemaError(
                    f"FiscalPeriod {self.label!r} has start={self.start} >= end={self.end}"
                )
            if self.days is not None and self.days != (self.end - self.start).days:
                raise SchemaError(
                    f"FiscalPeriod {self.label!r} declares days={self.days} but "
                    f"end-start = {(self.end - self.start).days}"
                )


@dataclass(frozen=True)
class Provenance:
    """Records which mapping mechanism (PLANNING.md Section 5.2) resolved a
    cell, so the workbook's provenance column (and this project's own
    correctness) can be audited rather than trusted blindly."""

    mechanism: Mechanism
    tag: str | None
    component_tags: tuple[str, ...] = field(default_factory=tuple)
    accession: str | None = None
    filed: date | None = None
    frame_corroborated: bool = False

    def __post_init__(self) -> None:
        if self.mechanism in _TAGLESS_MECHANISMS:
            if self.tag is not None:
                raise SchemaError(
                    f"Provenance.tag must be None for mechanism={self.mechanism.value}, "
                    f"got {self.tag!r}"
                )
        else:
            if not self.tag:
                raise SchemaError(
                    f"Provenance.tag must be set for mechanism={self.mechanism.value}"
                )
        if self.mechanism is Mechanism.COMPOSED and not self.component_tags:
            raise SchemaError("Provenance.component_tags must be non-empty for COMPOSED")
        if self.mechanism is not Mechanism.COMPOSED and self.component_tags:
            raise SchemaError(
                f"Provenance.component_tags must be empty for mechanism={self.mechanism.value}, "
                f"got {self.component_tags!r}"
            )
        if self.mechanism is Mechanism.MISSING:
            if self.accession is not None or self.filed is not None or self.frame_corroborated:
                raise SchemaError("Provenance for a MISSING cell must carry no source metadata")


@dataclass(frozen=True)
class Cell:
    """One value in one (line item, period) slot.

    ``value is None`` means GAP -- it is never ``Decimal(0)`` standing in for
    absence (PLANNING.md Section 5.3.8 / invariant 2). A *reported* zero
    (e.g. genuinely $0 interest expense for a debt-free company-year) is a
    real ``Decimal("0")`` with a real, non-MISSING ``Provenance`` -- the
    invariant is about fabricated zeros, not legitimate ones.
    """

    value: Decimal | None
    unit: str
    period_label: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.unit not in VALID_UNITS:
            raise SchemaError(f"Cell.unit must be one of {sorted(VALID_UNITS)}, got {self.unit!r}")
        if not self.period_label:
            raise SchemaError("Cell.period_label must not be empty")
        if self.value is not None:
            # Explicitly reject float/int/bool -- Decimal must be constructed
            # deliberately from the source string, never coerced silently
            # from a binary float (PLANNING.md Section 5.3, "use Decimal,
            # never float", invariant 5).
            if not isinstance(self.value, Decimal):
                raise SchemaError(
                    f"Cell.value must be a Decimal or None, got {type(self.value).__name__} "
                    f"({self.value!r}). Monetary values are never float (PLANNING.md Section 5)."
                )
        # Bidirectional tie between "no value" and "the MISSING mechanism":
        # a gap is always MISSING, and MISSING is always a gap. This is what
        # makes invariant 2 mechanically true rather than merely conventional.
        value_is_gap = self.value is None
        mechanism_is_missing = self.provenance.mechanism is Mechanism.MISSING
        if value_is_gap != mechanism_is_missing:
            raise SchemaError(
                f"Cell for period {self.period_label!r}: value is "
                f"{'None' if value_is_gap else 'set'} but provenance.mechanism="
                f"{self.provenance.mechanism.value} -- these must agree "
                "(a gap is always MISSING and MISSING is always a gap)"
            )


@dataclass(frozen=True)
class LineItem:
    """One row of a statement, populated across the shared period axis.

    ``is_subtotal=True`` items carry no fetched values (invariant 4) -- Phase
    3 writes them as Excel formulas summing their components, so ``cells``
    must be empty here, not populated-then-ignored.
    """

    key: str
    label: str
    statement: Statement
    period_type: PeriodType
    sign: Sign
    is_subtotal: bool
    cells: Mapping[str, Cell] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise SchemaError("LineItem.key must not be empty")
        if not self.label:
            raise SchemaError("LineItem.label must not be empty")
        if self.is_subtotal and self.cells:
            raise SchemaError(
                f"LineItem {self.key!r} is_subtotal=True but carries {len(self.cells)} "
                "fetched cell(s) -- subtotals are Excel formulas, not fetched values "
                "(PLANNING.md Section 13.2 invariant 4)"
            )
        for period_label, cell in self.cells.items():
            if cell.period_label != period_label:
                raise SchemaError(
                    f"LineItem {self.key!r}: cells key {period_label!r} does not match "
                    f"Cell.period_label {cell.period_label!r}"
                )


def _validate_data_warning_code(code: str) -> None:
    if code not in DATA_WARNING_CODES:
        raise SchemaError(
            f"DataWarning.code {code!r} is not one of the recognized codes "
            f"{sorted(DATA_WARNING_CODES)} (PLANNING.md Section 13.2)"
        )


@dataclass(frozen=True)
class DataWarning:
    code: str
    message: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_data_warning_code(self.code)
        if not self.message:
            raise SchemaError("DataWarning.message must not be empty")


@dataclass(frozen=True)
class FinancialModel:
    """The fully-built canonical model: Phase 2's entire output.

    Enforces, at construction, the invariant that requires looking at
    ``periods`` and ``line_items`` *together* (the one a single dataclass's
    own ``__post_init__`` cannot see on its own):

    - Invariant 1: every ``LineItem.cells`` key must name a period that
      actually exists on the shared axis -- a line item cannot introduce a
      period of its own.

    Invariant 6 (DURATION and INSTANT never mixed within a line item) is
    enforced structurally rather than by cross-checking against the axis
    here: every ``LineItem`` is built from exactly one ``LineItemSpec``
    (``mappings/us_gaap.yaml``), which declares a single ``period_type``,
    and ``facts.py``/``statements.py`` always extract and match facts
    consistently for that declared type (INSTANT specs query by ``end``
    alone; DURATION specs require an exact ``(start, end)``). Note that the
    *axis* itself (``periods``) is uniformly DURATION-shaped, since it is
    established once from the anchor (revenue, a DURATION line item,
    Section 5.1) -- an INSTANT line item validly reads only each axis
    period's ``end`` date, which is not a violation of this invariant, so
    the axis's own ``FiscalPeriod.kind`` is deliberately *not*
    cross-checked against ``LineItem.period_type`` here.
    """

    company: CompanyRef
    currency: str
    fiscal_year_end: str
    periods: tuple[FiscalPeriod, ...]
    line_items: tuple[LineItem, ...]
    warnings: tuple[DataWarning, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Deliberately NOT enforced: "periods must be non-empty" is not one
        # of PLANNING.md Section 13.2's six invariants. A genuinely
        # registrant-change-suspected company (zero resolvable anchor
        # periods -- PLANNING.md Section 5.3.11, the XOM finding) must still
        # produce a valid, warned FinancialModel rather than fail
        # construction outright; the REGISTRANT_CHANGE_SUSPECTED warning is
        # the signal, not a raised exception.
        if not self.currency:
            raise SchemaError("FinancialModel.currency must not be empty")

        labels = [p.label for p in self.periods]
        if len(labels) != len(set(labels)):
            raise SchemaError(f"FinancialModel.periods has duplicate labels: {labels}")

        ltm_flags = [p.is_ltm for p in self.periods]
        if sum(ltm_flags) > 1:
            raise SchemaError("FinancialModel.periods has more than one is_ltm=True period")
        if any(ltm_flags) and not ltm_flags[-1]:
            raise SchemaError(
                "FinancialModel.periods: an is_ltm=True period must be last on the axis "
                f"(labels={labels})"
            )

        period_by_label = {p.label: p for p in self.periods}

        keys = [li.key for li in self.line_items]
        if len(keys) != len(set(keys)):
            raise SchemaError(f"FinancialModel.line_items has duplicate keys: {keys}")

        for line_item in self.line_items:
            for period_label, cell in line_item.cells.items():
                axis_period = period_by_label.get(period_label)
                if axis_period is None:
                    raise SchemaError(
                        f"LineItem {line_item.key!r} has a cell for period "
                        f"{period_label!r}, which is not on FinancialModel.periods "
                        f"({labels}). A line item may not introduce its own period "
                        "(PLANNING.md Section 13.2 invariant 1)."
                    )
