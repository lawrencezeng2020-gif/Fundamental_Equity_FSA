"""Tests for fsa.normalize.schema: the canonical model and its six
invariants (PLANNING.md Section 13.2). Each invariant gets a dedicated test
that fails construction loudly, per the Phase 2 task brief."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fsa.normalize.facts import RawFact, select_exact
from fsa.normalize.schema import (
    Cell,
    DataWarning,
    FinancialModel,
    FiscalPeriod,
    LineItem,
    Mechanism,
    PeriodType,
    Provenance,
    SchemaError,
    Sign,
    Statement,
)
from fsa.sec.endpoints import CompanyRef

COMPANY = CompanyRef(ticker="TEST", cik="0000000001", name="Test Co")

FY2023 = FiscalPeriod(
    label="FY2023", fiscal_year=2023, end=date(2023, 12, 31), start=date(2023, 1, 1),
    kind=PeriodType.DURATION, is_ltm=False, days=364,
)
FY2024 = FiscalPeriod(
    label="FY2024", fiscal_year=2024, end=date(2024, 12, 31), start=date(2024, 1, 1),
    kind=PeriodType.DURATION, is_ltm=False, days=365,
)


def _direct_cell(value: str, period_label: str, unit: str = "USD") -> Cell:
    return Cell(
        value=Decimal(value),
        unit=unit,
        period_label=period_label,
        provenance=Provenance(mechanism=Mechanism.DIRECT, tag="SomeTag", accession="0000000001-24-000001", filed=date(2024, 2, 1)),
    )


def _missing_cell(period_label: str, unit: str = "USD") -> Cell:
    return Cell(value=None, unit=unit, period_label=period_label, provenance=Provenance(mechanism=Mechanism.MISSING, tag=None))


# -- Invariant 1: periods built once from the anchor; no line item introduces its own period --


def test_invariant1_line_item_cell_for_unknown_period_raises():
    revenue = LineItem(
        key="revenue", label="Revenue", statement=Statement.INCOME, period_type=PeriodType.DURATION,
        sign=Sign.AS_REPORTED, is_subtotal=False,
        cells={"FY2023": _direct_cell("100", "FY2023"), "FY2099": _direct_cell("999", "FY2099")},
    )
    with pytest.raises(SchemaError, match="not on FinancialModel.periods"):
        FinancialModel(
            company=COMPANY, currency="USD", fiscal_year_end="1231",
            periods=(FY2023,), line_items=(revenue,), warnings=(),
        )


def test_invariant1_line_item_cells_matching_axis_is_valid():
    revenue = LineItem(
        key="revenue", label="Revenue", statement=Statement.INCOME, period_type=PeriodType.DURATION,
        sign=Sign.AS_REPORTED, is_subtotal=False,
        cells={"FY2023": _direct_cell("100", "FY2023")},
    )
    model = FinancialModel(
        company=COMPANY, currency="USD", fiscal_year_end="1231",
        periods=(FY2023,), line_items=(revenue,), warnings=(),
    )
    assert model.periods[0].label == "FY2023"


# -- Invariant 2: a missing value is None, never Decimal(0) --


def test_invariant2_missing_cell_must_have_none_value():
    with pytest.raises(SchemaError, match="agree"):
        Cell(value=Decimal(0), unit="USD", period_label="FY2023", provenance=Provenance(mechanism=Mechanism.MISSING, tag=None))


def test_invariant2_a_real_reported_zero_is_allowed_and_distinct_from_missing():
    """A genuine reported $0 (e.g. no interest expense that year) is a real
    Decimal(0) with a non-MISSING mechanism -- invariant 2 is about
    fabricated zeros standing in for absence, not legitimate ones."""
    cell = _direct_cell("0", "FY2023")
    assert cell.value == Decimal(0)
    assert cell.provenance.mechanism is not Mechanism.MISSING


def test_invariant2_none_value_requires_missing_mechanism():
    with pytest.raises(SchemaError, match="agree"):
        Cell(value=None, unit="USD", period_label="FY2023", provenance=Provenance(mechanism=Mechanism.DIRECT, tag="Foo"))


# -- Invariant 3: every non-missing Cell carries a Provenance naming its mechanism --


def test_invariant3_direct_alternative_stitched_must_name_a_tag():
    for mechanism in (Mechanism.DIRECT, Mechanism.ALTERNATIVE, Mechanism.STITCHED):
        with pytest.raises(SchemaError, match="tag must be set"):
            Provenance(mechanism=mechanism, tag=None)


def test_invariant3_composed_must_name_component_tags_not_a_single_tag():
    with pytest.raises(SchemaError, match="tag must be None"):
        Provenance(mechanism=Mechanism.COMPOSED, tag="SomeTag", component_tags=("A", "B"))
    with pytest.raises(SchemaError, match="component_tags must be non-empty"):
        Provenance(mechanism=Mechanism.COMPOSED, tag=None, component_tags=())


def test_invariant3_every_cell_has_a_provenance_object():
    cell = _direct_cell("42", "FY2023")
    assert isinstance(cell.provenance, Provenance)
    assert cell.provenance.mechanism is Mechanism.DIRECT


# -- Invariant 4: is_subtotal=True items carry no fetched values --


def test_invariant4_subtotal_with_cells_raises():
    with pytest.raises(SchemaError, match="is_subtotal=True but carries"):
        LineItem(
            key="gross_profit", label="Gross profit", statement=Statement.INCOME,
            period_type=PeriodType.DURATION, sign=Sign.AS_REPORTED, is_subtotal=True,
            cells={"FY2023": _direct_cell("50", "FY2023")},
        )


def test_invariant4_subtotal_with_empty_cells_is_valid():
    line_item = LineItem(
        key="gross_profit", label="Gross profit", statement=Statement.INCOME,
        period_type=PeriodType.DURATION, sign=Sign.AS_REPORTED, is_subtotal=True, cells={},
    )
    assert line_item.cells == {}


# -- Invariant 5: monetary values are Decimal, never float --


def test_invariant5_float_value_raises():
    with pytest.raises(SchemaError, match="never float"):
        Cell(value=100.0, unit="USD", period_label="FY2023", provenance=Provenance(mechanism=Mechanism.DIRECT, tag="Foo"))


def test_invariant5_int_value_also_raises_not_silently_coerced():
    with pytest.raises(SchemaError):
        Cell(value=100, unit="USD", period_label="FY2023", provenance=Provenance(mechanism=Mechanism.DIRECT, tag="Foo"))


def test_invariant5_decimal_value_is_accepted():
    cell = _direct_cell("100", "FY2023")
    assert isinstance(cell.value, Decimal)


# -- Invariant 6: DURATION and INSTANT are never mixed within a line item --
#
# Enforced structurally (see schema.FinancialModel docstring): a LineItem is
# always built from one LineItemSpec with one declared period_type, and
# facts.py's period-matching never crosses the two. Demonstrated directly at
# that primitive: querying with start=None (an INSTANT lookup) must never
# return a DURATION fact even if one happens to share the same `end`, and
# vice versa.


def test_invariant6_instant_query_never_returns_a_duration_fact():
    duration_fact = RawFact(
        value=Decimal("100"), start=date(2023, 1, 1), end=date(2023, 12, 31),
        filed=date(2024, 1, 1), accession="acc1", frame=None, form="10-K",
    )
    instant_fact = RawFact(
        value=Decimal("200"), start=None, end=date(2023, 12, 31),
        filed=date(2024, 1, 1), accession="acc2", frame=None, form="10-K",
    )
    facts = [duration_fact, instant_fact]

    instant_result = select_exact(facts, start=None, end=date(2023, 12, 31))
    assert instant_result is instant_fact

    duration_result = select_exact(facts, start=date(2023, 1, 1), end=date(2023, 12, 31))
    assert duration_result is duration_fact


def test_invariant6_line_item_period_type_is_fixed_and_single():
    """A LineItem declares exactly one period_type; PLANNING.md Section
    13.2's schema has no per-Cell period-type field, so "never mixed" is a
    property of how the LineItem itself is built (one spec, one type), not
    something expressible as a mixed state within a single LineItem."""
    revenue = LineItem(
        key="revenue", label="Revenue", statement=Statement.INCOME, period_type=PeriodType.DURATION,
        sign=Sign.AS_REPORTED, is_subtotal=False, cells={"FY2023": _direct_cell("100", "FY2023")},
    )
    assert revenue.period_type is PeriodType.DURATION
    cash = LineItem(
        key="cash_and_equivalents", label="Cash", statement=Statement.BALANCE, period_type=PeriodType.INSTANT,
        sign=Sign.AS_REPORTED, is_subtotal=False, cells={"FY2023": _direct_cell("100", "FY2023")},
    )
    assert cash.period_type is PeriodType.INSTANT


# -- Other schema-level checks (not one of the six, but load-bearing) --


def test_fiscal_period_instant_rejects_start():
    with pytest.raises(SchemaError):
        FiscalPeriod(label="Q1", fiscal_year=2023, end=date(2023, 3, 31), start=date(2023, 1, 1), kind=PeriodType.INSTANT, is_ltm=False, days=None)


def test_fiscal_period_duration_requires_start_before_end():
    with pytest.raises(SchemaError):
        FiscalPeriod(label="FY2023", fiscal_year=2023, end=date(2023, 1, 1), start=date(2023, 12, 31), kind=PeriodType.DURATION, is_ltm=False, days=None)


def test_cell_rejects_invalid_unit():
    with pytest.raises(SchemaError, match="unit must be one of"):
        Cell(value=Decimal("1"), unit="EUR", period_label="FY2023", provenance=Provenance(mechanism=Mechanism.DIRECT, tag="Foo"))


def test_data_warning_rejects_unknown_code():
    with pytest.raises(SchemaError, match="not one of the recognized codes"):
        DataWarning(code="NOT_A_REAL_CODE", message="oops")


def test_financial_model_rejects_duplicate_period_labels():
    revenue = LineItem(
        key="revenue", label="Revenue", statement=Statement.INCOME, period_type=PeriodType.DURATION,
        sign=Sign.AS_REPORTED, is_subtotal=False, cells={},
    )
    with pytest.raises(SchemaError, match="duplicate labels"):
        FinancialModel(company=COMPANY, currency="USD", fiscal_year_end="1231", periods=(FY2023, FY2023), line_items=(revenue,), warnings=())


def test_financial_model_ltm_must_be_last_period():
    ltm = FiscalPeriod(label="LTM", fiscal_year=2024, end=date(2024, 6, 30), start=date(2023, 7, 1), kind=PeriodType.DURATION, is_ltm=True, days=365)
    with pytest.raises(SchemaError, match="must be last"):
        FinancialModel(company=COMPANY, currency="USD", fiscal_year_end="1231", periods=(ltm, FY2023), line_items=(), warnings=())


def test_financial_model_allows_empty_periods_for_registrant_change_case():
    """Not one of the six invariants, but a deliberate design choice: a
    company with zero resolvable anchor periods (PLANNING.md Section
    5.3.11, the XOM finding) must still produce a valid, warned model rather
    than fail construction outright."""
    model = FinancialModel(
        company=COMPANY, currency="USD", fiscal_year_end="1231", periods=(), line_items=(),
        warnings=(DataWarning(code="REGISTRANT_CHANGE_SUSPECTED", message="zero periods"),),
    )
    assert model.periods == ()
    assert model.warnings[0].code == "REGISTRANT_CHANGE_SUSPECTED"
