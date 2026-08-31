"""Tests for fsa.normalize.statements: mapping mechanisms, the period-axis
rule, warnings, and the five named regression tests from PLANNING.md Section
9 / the Phase 2 task brief.

Uses the trimmed, committed `norm_companyfacts_<TICKER>.json` fixtures
(real SEC data, see `tests/conftest.py:load_normalize_company` for how they
were built) for everything that needs to demonstrate a real, empirically-
confirmed finding. Two pitfalls that no required test ticker actually
triggers (THIN_HISTORY, NON_USD) use small synthetic fixtures built inline.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fsa.normalize.facts import RawFact
from fsa.normalize.schema import Mechanism, PeriodType
from fsa.normalize.statements import LineItemSpec, MappingError, load_mapping, resolve_span
from fsa.normalize.statements import build_financial_model
from fsa.sec.endpoints import CompanyFactsDoc, CompanyRef, SubmissionsDoc

from tests.conftest import load_normalize_company

DEFAULT_MAPPING = load_mapping()


# ============================== load_mapping validation ==============================


def test_load_mapping_loads_the_real_mapping_file():
    specs = DEFAULT_MAPPING
    by_key = {s.key: s for s in specs}
    assert "revenue" in by_key
    assert by_key["revenue"].anchor is True
    anchors = [s for s in specs if s.anchor]
    assert len(anchors) == 1


def test_load_mapping_requires_exactly_one_anchor(tmp_path: Path):
    bad = tmp_path / "no_anchor.yaml"
    bad.write_text(
        "revenue:\n  label: R\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  tags: [Revenues]\n"
    )
    with pytest.raises(MappingError, match="exactly one anchor"):
        load_mapping(bad)


def test_load_mapping_rejects_two_anchors(tmp_path: Path):
    bad = tmp_path / "two_anchors.yaml"
    bad.write_text(
        "a:\n  label: A\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [TagA]\n"
        "b:\n  label: B\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [TagB]\n"
    )
    with pytest.raises(MappingError, match="exactly one anchor"):
        load_mapping(bad)


def test_load_mapping_rejects_subtotal_with_tags(tmp_path: Path):
    bad = tmp_path / "bad_subtotal.yaml"
    bad.write_text(
        "revenue:\n  label: R\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [Revenues]\n"
        "gp:\n  label: GP\n  statement: income\n  period_type: duration\n  sign: as_reported\n  is_subtotal: true\n  tags: [SomeTag]\n"
    )
    with pytest.raises(MappingError, match="is_subtotal=True must not declare tags"):
        load_mapping(bad)


def test_load_mapping_rejects_fetchable_item_with_no_tags(tmp_path: Path):
    bad = tmp_path / "no_tags.yaml"
    bad.write_text(
        "revenue:\n  label: R\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [Revenues]\n"
        "x:\n  label: X\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n"
    )
    with pytest.raises(MappingError, match="at least one tag"):
        load_mapping(bad)


def test_load_mapping_rejects_unregistered_derived_key(tmp_path: Path):
    bad = tmp_path / "bad_derived.yaml"
    bad.write_text(
        "revenue:\n  label: R\n  statement: income\n  period_type: duration\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [Revenues]\n"
        "mystery:\n  label: M\n  statement: cashflow\n  period_type: duration\n  sign: as_reported\n  derived: true\n"
    )
    with pytest.raises(MappingError, match="no derivation logic registered"):
        load_mapping(bad)


def test_load_mapping_anchor_must_be_duration(tmp_path: Path):
    bad = tmp_path / "instant_anchor.yaml"
    bad.write_text(
        "revenue:\n  label: R\n  statement: income\n  period_type: instant\n  sign: as_reported\n  unit: USD\n  anchor: true\n  tags: [Revenues]\n"
    )
    with pytest.raises(MappingError, match="period_type=duration"):
        load_mapping(bad)


def test_every_required_canonical_line_item_is_present():
    """PLANNING.md Section 13.3 / the Phase 2 task brief's minimum coverage
    list -- fails loudly if a required line item is ever dropped from the
    mapping file."""
    required = {
        "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
        "operating_income", "interest_expense", "pretax_income", "income_tax_expense",
        "net_income", "diluted_shares", "diluted_eps",
        "cash_and_equivalents", "short_term_investments", "accounts_receivable", "inventory",
        "total_current_assets", "total_assets", "accounts_payable", "short_term_debt",
        "total_current_liabilities", "long_term_debt", "total_liabilities", "minority_interest",
        "preferred_equity", "total_equity",
        "d_and_a", "stock_based_comp", "change_in_nwc", "cash_from_operations",
        "capex", "cash_from_investing", "cash_from_financing",
        "shares_outstanding_dei", "total_debt", "net_debt",
    }
    present = {s.key for s in DEFAULT_MAPPING}
    assert required <= present, f"missing: {required - present}"


def test_every_non_subtotal_non_derived_spec_declares_an_explicit_sign():
    """PLANNING.md Section 5.3.6: every mapped line item carries an explicit
    sign convention. Trivially true given the schema requires it to parse at
    all, but this locks in that `capex` specifically is NEGATED (the one
    pitfall explicitly named: PaymentsToAcquirePropertyPlantAndEquipment is
    reported positive but means an outflow)."""
    from fsa.normalize.schema import Sign

    by_key = {s.key: s for s in DEFAULT_MAPPING}
    assert by_key["capex"].sign is Sign.NEGATED
    assert by_key["revenue"].sign is Sign.AS_REPORTED


# ============================== resolve_span mechanism unit tests ==============================


def _spec(**overrides) -> LineItemSpec:
    from fsa.normalize.schema import PeriodType, Sign, Statement

    base = dict(
        key="test_item", label="Test", statement=Statement.INCOME, period_type=PeriodType.DURATION,
        sign=Sign.AS_REPORTED, namespace="us-gaap", unit="USD", tags=("TagA", "TagB"), compose=(),
    )
    base.update(overrides)
    return LineItemSpec(**base)


def _rf(start, end, val, filed="2024-01-01", frame=None):
    return RawFact(value=Decimal(str(val)), start=start, end=end, filed=date.fromisoformat(filed), accession="acc", frame=frame, form="10-K")


PERIOD = (date(2023, 1, 1), date(2023, 12, 31))


def test_resolve_span_direct_when_top_priority_tag_alone_has_data():
    spec = _spec()
    facts_by_tag = {"TagA": [_rf(*PERIOD, "100")], "TagB": []}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.value == Decimal("100")
    assert cell.provenance.mechanism is Mechanism.DIRECT
    assert cell.provenance.tag == "TagA"


def test_resolve_span_alternative_when_only_lower_priority_tag_has_data():
    spec = _spec()
    facts_by_tag = {"TagA": [], "TagB": [_rf(*PERIOD, "100")]}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.provenance.mechanism is Mechanism.ALTERNATIVE
    assert cell.provenance.tag == "TagB"


def test_resolve_span_stitched_when_both_tags_present_and_agree():
    spec = _spec()
    facts_by_tag = {"TagA": [_rf(*PERIOD, "100")], "TagB": [_rf(*PERIOD, "100")]}
    warnings = []
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023", warnings_out=warnings)
    assert cell.provenance.mechanism is Mechanism.STITCHED
    assert cell.provenance.tag == "TagA"  # priority winner
    assert warnings == []  # no disagreement to flag


def test_resolve_span_stitch_disagreement_flagged_but_still_resolves():
    spec = _spec()
    facts_by_tag = {"TagA": [_rf(*PERIOD, "100")], "TagB": [_rf(*PERIOD, "50")]}
    warnings = []
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023", warnings_out=warnings)
    assert cell.provenance.mechanism is Mechanism.STITCHED
    assert cell.value == Decimal("100")  # still resolves to the priority winner
    assert len(warnings) == 1
    assert warnings[0].code == "STITCH_DISAGREEMENT"
    assert warnings[0].detail["winner_tag"] == "TagA"
    assert warnings[0].detail["other_tag"] == "TagB"


def test_resolve_span_composed_when_no_alternative_but_components_present():
    spec = _spec(compose=("CompA", "CompB"))
    facts_by_tag = {"TagA": [], "TagB": [], "CompA": [_rf(*PERIOD, "40")], "CompB": [_rf(*PERIOD, "60")]}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.provenance.mechanism is Mechanism.COMPOSED
    assert cell.provenance.tag is None
    assert cell.provenance.component_tags == ("CompA", "CompB")
    assert cell.value == Decimal("100")


def test_resolve_span_composed_uses_only_the_components_that_have_data():
    spec = _spec(compose=("CompA", "CompB", "CompC"))
    facts_by_tag = {"TagA": [], "TagB": [], "CompA": [_rf(*PERIOD, "40")], "CompB": [], "CompC": [_rf(*PERIOD, "10")]}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.provenance.component_tags == ("CompA", "CompC")
    assert cell.value == Decimal("50")


def test_resolve_span_missing_when_nothing_resolves():
    spec = _spec(compose=("CompA",))
    facts_by_tag = {"TagA": [], "TagB": [], "CompA": []}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.value is None
    assert cell.provenance.mechanism is Mechanism.MISSING


def test_resolve_span_negated_sign_flips_the_stored_value():
    spec = _spec(sign=__import__("fsa.normalize.schema", fromlist=["Sign"]).Sign.NEGATED)
    facts_by_tag = {"TagA": [_rf(*PERIOD, "500")], "TagB": []}
    cell = resolve_span(spec, facts_by_tag, start=PERIOD[0], end=PERIOD[1], period_label="FY2023")
    assert cell.value == Decimal("-500")


def test_resolve_span_never_approximate_matches_exact_span_required():
    spec = _spec()
    facts_by_tag = {"TagA": [_rf(date(2023, 1, 2), date(2023, 12, 31), "100")], "TagB": []}
    cell = resolve_span(spec, facts_by_tag, start=date(2023, 1, 1), end=date(2023, 12, 31), period_label="FY2023")
    assert cell.value is None
    assert cell.provenance.mechanism is Mechanism.MISSING


# ============================== named regression test 1: MSFT D&A via COMPOSED ==============================


def test_msft_d_and_a_resolves_via_composition():
    """MSFT never reports DepreciationDepletionAndAmortization -- confirmed
    against live data across its entire tagged history. Without composition
    this line item would be entirely empty for Microsoft."""
    company, submissions, company_facts = load_normalize_company("MSFT")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    da = next(li for li in model.line_items if li.key == "d_and_a")

    non_ltm_periods = [p.label for p in model.periods if not p.label.startswith("LTM")]
    assert len(non_ltm_periods) >= 5
    for label in non_ltm_periods:
        cell = da.cells[label]
        assert cell.value is not None, f"MSFT d_and_a is a gap for {label} -- composition should have covered it"
        assert cell.provenance.mechanism is Mechanism.COMPOSED, f"expected COMPOSED for {label}, got {cell.provenance.mechanism}"
        assert "Depreciation" in cell.provenance.component_tags
        assert "AmortizationOfIntangibleAssets" in cell.provenance.component_tags


# ============================== named regression test 2: TSLA D&A never stale-filled ==============================


def test_tsla_d_and_a_is_never_stale_filled_from_2013_2017():
    """Tesla stopped using DepreciationDepletionAndAmortization after
    FY2017. A naive 'last N periods this tag happens to have' implementation
    would return FY2013-2017 data for a request of the most recent 10 years
    -- this is PLANNING.md's headline example of the period-axis bug class.
    The fix (per-period resolution against the revenue-anchored axis, with
    composition as a fallback) must never resolve a post-2017 period using
    that abandoned tag, and must never leave 2018+ as a gap either."""
    company, submissions, company_facts = load_normalize_company("TSLA")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    da = next(li for li in model.line_items if li.key == "d_and_a")

    for label, cell in da.cells.items():
        if label.startswith("LTM"):
            continue
        fiscal_year = int(label.replace("FY", ""))
        if fiscal_year >= 2018:
            assert cell.value is not None, f"TSLA d_and_a for {label} must not be a gap"
            if cell.provenance.tag is not None:
                assert cell.provenance.tag != "DepreciationDepletionAndAmortization", (
                    f"{label} resolved via the abandoned tag DepreciationDepletionAndAmortization "
                    "-- this is the stale-fill bug PLANNING.md Section 5.1 warns about"
                )
            assert cell.provenance.mechanism is Mechanism.COMPOSED

    # And confirm the tag *is* correctly used where it's genuinely valid (2016-2017),
    # so this test is checking "not stale" rather than "never used at all".
    if "FY2017" in da.cells:
        assert da.cells["FY2017"].provenance.tag == "DepreciationDepletionAndAmortization"


# ============================== named regression test 3: TSLA revenue stitches two tags ==============================


def test_tsla_revenue_stitches_two_tags_across_the_asc606_boundary():
    company, submissions, company_facts = load_normalize_company("TSLA")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    revenue = next(li for li in model.line_items if li.key == "revenue")

    tags_used = {cell.provenance.tag for label, cell in revenue.cells.items() if not label.startswith("LTM") and cell.provenance.tag}
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in tags_used
    assert "Revenues" in tags_used, (
        "TSLA revenue over a 10-year window must fall back to the pre-ASC-606 'Revenues' tag "
        "for years before RevenueFromContractWithCustomerExcludingAssessedTax existed"
    )

    # No misalignment: every resolved period's value must be a plausible,
    # monotonically-sane Tesla revenue figure (spot-check against known
    # public figures, confirmed live), never a value from the wrong period.
    known = {"FY2023": Decimal("96773000000"), "FY2024": Decimal("97690000000")}
    for label, expected in known.items():
        if label in revenue.cells:
            assert revenue.cells[label].value == expected


# ============================== named regression test 4: KO FY2018 restatement ==============================


def test_ko_fy2018_revenue_resolves_to_the_latest_filed_value():
    """KO's FY2018 revenue was originally filed as $31.86bn (2019-02-21),
    then restated to $34.30bn (2019-09-20, reaffirmed 2020-02-24 and again
    2021-02-25 with SEC's `frame` key set). Latest `filed` must win."""
    company, submissions, company_facts = load_normalize_company("KO")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    revenue = next(li for li in model.line_items if li.key == "revenue")

    cell = revenue.cells["FY2018"]
    assert cell.value == Decimal("34300000000")
    assert cell.provenance.filed == date(2021, 2, 25)
    assert cell.provenance.frame_corroborated is True


def test_ko_fy2017_reveals_a_genuine_stitch_disagreement():
    """A finding beyond PLANNING.md's own claim that KO's two revenue tags
    'agree in both overlap years': confirmed against live data, FY2017
    disagrees by ~2.3% between SalesRevenueGoodsNet ($35.41bn, as originally
    filed, never restated since KO retired that tag) and the ASC-606-
    restated `Revenues` figure ($36.212bn). This is exactly the case
    STITCH_DISAGREEMENT exists to catch -- flagged in the Phase 2 report as
    a correction to the planning document's empirical claim."""
    company, submissions, company_facts = load_normalize_company("KO")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)

    disagreements = [
        w for w in model.warnings
        if w.code == "STITCH_DISAGREEMENT" and w.detail.get("key") == "revenue" and w.detail.get("period_label") == "FY2017"
    ]
    assert len(disagreements) == 1
    detail = disagreements[0].detail
    assert {detail["winner_tag"], detail["other_tag"]} == {"Revenues", "SalesRevenueGoodsNet"}


# ============================== named regression test 5: XOM registrant change ==============================


def test_xom_by_ticker_raises_registrant_change_suspected_not_thin_history():
    """CIK 2115436 (what SEC's ticker map currently resolves XOM to) has
    zero annual revenue periods; the real ~17-year history sits under the
    legacy CIK 34088. Must raise REGISTRANT_CHANGE_SUSPECTED, and must NOT
    also raise THIN_HISTORY (checked first, per PLANNING.md Section 5.3.11,
    so a supermajor is never misclassified as a young filer)."""
    company, submissions, company_facts = load_normalize_company("XOM")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)

    assert model.periods == ()
    codes = [w.code for w in model.warnings]
    assert "REGISTRANT_CHANGE_SUSPECTED" in codes
    assert "THIN_HISTORY" not in codes


def test_xom_legacy_cik_has_full_multi_year_history():
    """Complementary check: the --cik escape hatch (Phase 1) reaches the CIK
    that actually has Exxon's history, and Phase 2 builds a normal model
    from it with no registrant-change warning."""
    company, submissions, company_facts = load_normalize_company("XOM_LEGACY", cik="0000034088")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)

    non_ltm = [p for p in model.periods if not p.label.startswith("LTM")]
    assert len(non_ltm) >= 8
    codes = [w.code for w in model.warnings]
    assert "REGISTRANT_CHANGE_SUSPECTED" not in codes
    assert "THIN_HISTORY" not in codes


# ============================== Phase 2 review fix 1: long_term_debt no longer double-counts ==============================


def test_long_term_debt_does_not_double_count_the_current_portion_for_aapl():
    """Coordinator-identified HIGH-severity bug, fixed: `LongTermDebt`
    (AAPL, FY2025-end: $90.7bn) is the TOTAL including the current portion,
    while `LongTermDebtNoncurrent` ($78.3bn) excludes it -- not synonyms.
    The old mapping listed both as alternatives for `long_term_debt`, which
    could let the total-flavoured tag win and double-count the current
    portion once summed with `short_term_debt` into `total_debt`, an error
    that would have flowed into net debt and the DCF's equity bridge.

    Fixed by restricting `long_term_debt` to noncurrent-only tags. This
    proves the fix two ways: (1) long_term_debt never resolves via the
    excluded `LongTermDebt` tag, and (2) short_term_debt + long_term_debt
    reconstructs the filer's own total-debt tag almost exactly (small
    residual from unamortized discount/issuance costs), rather than
    exceeding it by roughly the current portion's amount the way the bug
    would have."""
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    long_term_debt = next(li for li in model.line_items if li.key == "long_term_debt")
    short_term_debt = next(li for li in model.line_items if li.key == "short_term_debt")

    for label, cell in long_term_debt.cells.items():
        if cell.provenance.tag is not None:
            assert cell.provenance.tag != "LongTermDebt", (
                f"{label}: long_term_debt resolved via the excluded total-including-current tag "
                "'LongTermDebt' -- this is exactly the double-counting bug that was fixed"
            )

    # Reference: AAPL's own `LongTermDebt` tag (total including current),
    # read directly from the fixture rather than hardcoded, so this stays
    # correct if the fixture is ever refreshed.
    raw = json.loads((Path(__file__).parent / "fixtures" / "norm_companyfacts_AAPL.json").read_text())
    raw_long_term_debt_total = {
        entry["end"]: Decimal(str(entry["val"]))
        for entry in raw["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"]
    }
    # FY end dates for the periods this model resolved -- map FYxxxx label -> end date via the axis.
    end_by_label = {p.label: p.end.isoformat() for p in model.periods}

    checked_any = False
    for label in ("FY2023", "FY2024", "FY2025"):
        if label not in long_term_debt.cells or label not in short_term_debt.cells:
            continue
        ltd_cell = long_term_debt.cells[label]
        std_cell = short_term_debt.cells[label]
        end_date = end_by_label.get(label)
        reference = raw_long_term_debt_total.get(end_date)
        if ltd_cell.value is None or std_cell.value is None or reference is None:
            continue
        checked_any = True
        combined = ltd_cell.value + std_cell.value
        relative_gap = abs(combined - reference) / reference
        assert relative_gap < Decimal("0.01"), (
            f"{label}: short_term_debt + long_term_debt = {combined}, but AAPL's own total-debt tag "
            f"(LongTermDebt) reports {reference} for the same date -- more than 1% apart suggests "
            "double-counting or under-counting, not the expected small unamortized-discount residual"
        )
        # And confirm it is NOT double-counting: adding the current portion
        # a second time would overshoot the reference by roughly std_cell's
        # own magnitude -- assert we are nowhere near that.
        overcounted = combined - reference
        assert overcounted < std_cell.value, (
            f"{label}: combined total overshoots the reference by {overcounted}, comparable to or "
            f"exceeding the current portion ({std_cell.value}) -- looks like double-counting"
        )
    assert checked_any, "expected at least one of FY2023-FY2025 to be checkable against the raw fixture"


# ============================== reverted: long_term_debt must never be derived from LongTermDebt - short_term_debt ==============================


def test_long_term_debt_is_never_derived_from_long_term_debt_minus_short_term_debt_for_tsla_fy2022():
    """Regression guard for a bug that was implemented, caught, and
    reverted: a `LongTermDebt - short_term_debt` backfill for `long_term_debt`
    (once wired via a `derived_fallback_tag` mapping entry) computed TSLA
    FY2022 `long_term_debt` as $13M (LongTermDebt 1,029M - DebtCurrent
    1,016M), versus TSLA's actual FY2022 noncurrent debt of ~$1,029M -- a
    ~99% understatement that would have flowed straight into net_debt,
    equity value, and the implied share price.

    The premise behind that backfill -- that `LongTermDebt` is always the
    noncurrent-plus-current total, so subtracting `short_term_debt` recovers
    the noncurrent figure -- held for TSLA's own 2011-2012 filings
    (`LongTermDebt` = `LongTermDebtNoncurrent` + `LongTermDebtCurrent`
    exactly) but silently stopped holding by FY2022: `short_term_debt`
    resolves to `DebtCurrent`, a broader current-debt concept that includes
    current finance leases, while `LongTermDebt` itself excludes finance
    leases entirely. Subtracting two figures with mismatched scope is not a
    safe arithmetic fallback, however exact the arithmetic is.

    TSLA tags no noncurrent long-term-debt concept for FY2022 (confirmed:
    `LongTermDebtNoncurrent` was abandoned after 2013), so with the backfill
    correctly reverted, `long_term_debt` must be an honest gap here -- never
    the specific wrong value ($13,000,000) the bug produced, and never any
    other value silently derived from the mismatched-scope subtraction."""
    company, submissions, company_facts = load_normalize_company("TSLA")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    long_term_debt = next(li for li in model.line_items if li.key == "long_term_debt")

    assert "FY2022" in long_term_debt.cells, "expected FY2022 on the TSLA axis for this fixture"
    cell = long_term_debt.cells["FY2022"]
    assert cell.value != Decimal("13000000"), (
        "FY2022 long_term_debt reproduced the reverted bug's exact wrong value ($13M) -- the "
        "LongTermDebt-minus-short_term_debt derivation must not be back"
    )
    assert cell.value is None, (
        f"FY2022 long_term_debt = {cell.value!r}, expected a gap: TSLA tags no noncurrent "
        "long-term-debt concept for this period, and there is no safe way to split "
        "LongTermDebt (excludes finance leases) using short_term_debt (DebtCurrent, includes "
        "current finance leases) -- the two have mismatched scope"
    )
    assert cell.provenance.mechanism is Mechanism.MISSING


def test_debt_convention_unknown_warned_for_tsla_recent_years():
    """TSLA has a `LongTermDebt` fact for FY2016-2025/LTM but no noncurrent
    tag to resolve `long_term_debt` directly -- exactly the situation
    `DEBT_CONVENTION_UNKNOWN` exists to surface, so the user is told
    net_debt is unavailable and to consult the filing, rather than the gap
    passing silently (or, as the reverted bug did, being filled with a
    wrong number)."""
    company, submissions, company_facts = load_normalize_company("TSLA")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    matching = [w for w in model.warnings if w.code == "DEBT_CONVENTION_UNKNOWN"]
    assert matching, "expected a DEBT_CONVENTION_UNKNOWN warning for TSLA"
    warning = matching[0]
    assert "FY2022" in warning.detail["periods"]
    assert warning.detail["total_debt_tag"] == "LongTermDebt"
    assert "net_debt" in warning.message
    assert "consult the filing" in warning.message


# ============================== Phase 2 review fix 2: change_in_nwc replaces the CFO residual ==============================


def test_change_in_nwc_is_balance_sheet_delta_not_a_cfo_residual():
    """Coordinator-identified HIGH-severity bug, fixed: the previous design
    derived this line as CFO - NI - D&A - SBC, which under the indirect
    method (CFO = NI + D&A + SBC + other non-cash + change in working
    capital) silently absorbed deferred taxes, impairments, and disposal
    gains/losses -- a wrong number wearing the right name. Replaced with
    nwc[t] - nwc[t-1], nwc = accounts_receivable + inventory -
    accounts_payable, verified here against AAPL's own resolved balance
    sheet figures rather than trusting the formula blindly."""
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    ar = next(li for li in model.line_items if li.key == "accounts_receivable")
    inv = next(li for li in model.line_items if li.key == "inventory")
    ap = next(li for li in model.line_items if li.key == "accounts_payable")
    change_in_nwc = next(li for li in model.line_items if li.key == "change_in_nwc")

    def nwc(label: str) -> Decimal:
        return ar.cells[label].value + inv.cells[label].value - ap.cells[label].value

    for label in ("FY2017", "FY2022", "FY2025"):
        prior_label = f"FY{int(label[2:]) - 1}"
        expected = nwc(label) - nwc(prior_label)
        assert change_in_nwc.cells[label].value == expected
        assert change_in_nwc.cells[label].provenance.mechanism is Mechanism.DERIVED


def test_change_in_nwc_first_axis_period_is_a_gap_not_zero():
    """No prior period exists to difference against for the oldest column
    -- must be a gap (PLANNING.md Section 5.3.8), never a fabricated 0."""
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    change_in_nwc = next(li for li in model.line_items if li.key == "change_in_nwc")
    oldest_label = [p.label for p in model.periods if not p.label.startswith("LTM")][0]
    cell = change_in_nwc.cells[oldest_label]
    assert cell.value is None
    assert cell.provenance.mechanism is Mechanism.MISSING


def test_change_in_nwc_is_a_gap_wherever_any_component_is_a_gap():
    """JPM (financial sector, no classified current/noncurrent balance
    sheet) has no accounts_receivable/inventory/accounts_payable -- change_in_nwc
    must inherit that as a gap, not silently treat a missing component as 0."""
    company, submissions, company_facts = load_normalize_company("JPM")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    change_in_nwc = next(li for li in model.line_items if li.key == "change_in_nwc")
    non_ltm_cells = [c for label, c in change_in_nwc.cells.items() if not label.startswith("LTM")]
    assert non_ltm_cells
    for cell in non_ltm_cells:
        assert cell.value is None
        assert cell.provenance.mechanism is Mechanism.MISSING


# ============================== Phase 2 review fix 3: total_liabilities backfill ==============================


def test_ko_total_liabilities_backfills_from_assets_minus_equity():
    """KO never tags `Liabilities` at all (confirmed: 0% direct-tag
    coverage across its whole 10-year window). Must backfill exactly as
    total_assets - total_equity, Mechanism.DERIVED, rather than being a
    100% gap."""
    company, submissions, company_facts = load_normalize_company("KO")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    total_liabilities = next(li for li in model.line_items if li.key == "total_liabilities")
    total_assets = next(li for li in model.line_items if li.key == "total_assets")
    total_equity = next(li for li in model.line_items if li.key == "total_equity")

    non_ltm_labels = [p.label for p in model.periods if not p.label.startswith("LTM")]
    assert non_ltm_labels
    for label in non_ltm_labels:
        cell = total_liabilities.cells[label]
        assert cell.value is not None, f"{label}: expected the assets-minus-equity backfill, got a gap"
        assert cell.provenance.mechanism is Mechanism.DERIVED
        assert cell.value == total_assets.cells[label].value - total_equity.cells[label].value


def test_total_liabilities_direct_tag_still_wins_when_present():
    """The backfill must never override a real, directly-tagged fact --
    AAPL does tag `Liabilities` directly, so it must not be DERIVED."""
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    total_liabilities = next(li for li in model.line_items if li.key == "total_liabilities")
    for label, cell in total_liabilities.cells.items():
        if cell.value is not None:
            assert cell.provenance.mechanism is not Mechanism.DERIVED


# ============================== Phase 2 review fix 4: broadened registrant-change trigger ==============================


def test_registrant_change_suspected_also_triggers_on_few_periods_with_former_name_change():
    """Broadened trigger: zero periods (XOM) is unambiguous, but a
    registrant/CIK reorg leaving 1-2 periods on the new CIK should also be
    caught rather than falling through to THIN_HISTORY -- gated on a
    genuine secondary signal (a former-name change on record), which a
    never-renamed young IPO filer (RDDT, CAVA) does not have."""
    company = CompanyRef(ticker="REORGCO", cik="0000000097", name="Reorg Co")
    submissions = SubmissionsDoc(
        raw={}, cik="0000000097", entity_name="Reorg Co", sic="2911", sic_description="Petroleum Refining",
        fiscal_year_end="1231", tickers=["REORGCO"],
        former_names=[{"name": "OLD REORG CO", "from": "1990-01-01", "to": "2024-01-01"}],
        recent_filings=[],
    )
    facts_raw = {
        "cik": 97, "entityName": "Reorg Co",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"start": "2024-01-01", "end": "2024-12-31", "val": 1000000, "filed": "2025-02-01"},
                    ]}
                }
            }
        },
    }
    company_facts = CompanyFactsDoc(raw=facts_raw, cik="0000000097", entity_name="Reorg Co", namespaces=["us-gaap"])
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    codes = [w.code for w in model.warnings]
    assert "REGISTRANT_CHANGE_SUSPECTED" in codes
    assert "THIN_HISTORY" not in codes


def test_young_filer_with_no_former_names_is_thin_history_not_registrant_change():
    """The converse: a genuinely young filer with a short, clean history and
    no former-name change must still be THIN_HISTORY, not misclassified as
    a registrant change by the broadened trigger."""
    company = CompanyRef(ticker="YOUNGCO2", cik="0000000096", name="Young Co 2")
    submissions = SubmissionsDoc(
        raw={}, cik="0000000096", entity_name="Young Co 2", sic="7372", sic_description="Software",
        fiscal_year_end="1231", tickers=["YOUNGCO2"], former_names=[], recent_filings=[],
    )
    facts_raw = {
        "cik": 96, "entityName": "Young Co 2",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"start": "2024-01-01", "end": "2024-12-31", "val": 1000000, "filed": "2025-02-01"},
                    ]}
                }
            }
        },
    }
    company_facts = CompanyFactsDoc(raw=facts_raw, cik="0000000096", entity_name="Young Co 2", namespaces=["us-gaap"])
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    codes = [w.code for w in model.warnings]
    assert "THIN_HISTORY" in codes
    assert "REGISTRANT_CHANGE_SUSPECTED" not in codes
    thin_message = next(w for w in model.warnings if w.code == "THIN_HISTORY").message
    assert "registrant" in thin_message.lower() or "cik" in thin_message.lower()
    assert "--cik" in thin_message


def test_rddt_and_cava_have_no_genuine_former_name_change_confirming_the_broadened_trigger_does_not_misfire():
    """Sanity check against real data: neither required young-filer ticker
    has a GENUINE former-name change on record, so the broadened trigger's
    second branch (periods<=2 AND a real rename) structurally cannot
    misfire for them even though both currently have well under 10 years of
    history.

    Deliberately not `submissions.former_names == []`: confirmed live that
    RDDT actually has ONE `formerNames` entry, but its `name` ("Reddit,
    Inc.") is identical to its own current `entity_name` -- SEC's own
    record of the current name's validity period, not a real predecessor
    name. `_has_genuine_former_name_change` filters exactly this out."""
    from fsa.normalize.statements import _has_genuine_former_name_change

    for ticker in ("RDDT", "CAVA"):
        _, submissions, _ = load_normalize_company(ticker)
        assert _has_genuine_former_name_change(submissions) is False


def test_has_genuine_former_name_change_distinguishes_real_renames_from_the_current_name_artifact():
    """Direct unit test of the filter against all 8 required tickers'
    real submissions data: AAPL, TSLA and JPM have genuine historical
    renames; MSFT, KO, CAVA and XOM have no formerNames entries at all;
    RDDT has exactly one entry that merely re-records its current name."""
    from fsa.normalize.statements import _has_genuine_former_name_change

    expected = {
        "AAPL": True, "MSFT": False, "KO": False, "TSLA": True,
        "JPM": True, "RDDT": False, "CAVA": False, "XOM": False,
    }
    for ticker, expected_genuine in expected.items():
        _, submissions, _ = load_normalize_company(ticker)
        assert _has_genuine_former_name_change(submissions) is expected_genuine, (
            f"{ticker}: expected _has_genuine_former_name_change={expected_genuine}, "
            f"former_names={submissions.former_names}, entity_name={submissions.entity_name!r}"
        )


# ============================== remaining §5.3 pitfalls ==============================


def test_pitfall9_financial_sector_warning_for_jpm():
    company, submissions, company_facts = load_normalize_company("JPM")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    financial_sector = [w for w in model.warnings if w.code == "FINANCIAL_SECTOR"]
    assert len(financial_sector) == 1
    assert financial_sector[0].detail["sic"] == "6021"


def test_pitfall9_no_financial_sector_warning_for_a_non_bank():
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    assert not [w for w in model.warnings if w.code == "FINANCIAL_SECTOR"]


def test_jpm_working_capital_items_are_gaps_not_zero_filled():
    """Banks don't classify their balance sheet into current/non-current --
    total_current_assets etc. are legitimately absent for JPM. Must be a
    gap (None), never a fabricated 0."""
    company, submissions, company_facts = load_normalize_company("JPM")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    tca = next(li for li in model.line_items if li.key == "total_current_assets")
    non_ltm_cells = [c for label, c in tca.cells.items() if not label.startswith("LTM")]
    assert non_ltm_cells, "expected at least one period to check"
    for cell in non_ltm_cells:
        assert cell.value is None
        assert cell.provenance.mechanism is Mechanism.MISSING


def test_pitfall8_rddt_shares_outstanding_dei_is_a_real_gap():
    """Confirmed against live data: Reddit's dual-class share structure
    means the cover-page shares-outstanding fact is only tagged per-class
    under a dimensioned context, which companyfacts does not expose --
    dei:EntityCommonStockSharesOutstanding is absent entirely for RDDT. A
    real, expected gap, not a mapping failure."""
    company, submissions, company_facts = load_normalize_company("RDDT")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    shares = next(li for li in model.line_items if li.key == "shares_outstanding_dei")
    assert all(cell.value is None and cell.provenance.mechanism is Mechanism.MISSING for cell in shares.cells.values())


def test_pitfall10_thin_history_warning_below_three_years():
    """None of the required test tickers actually have under 3 years of
    history (RDDT has 4, CAVA has 5) -- a small synthetic fixture exercises
    the threshold directly."""
    company = CompanyRef(ticker="YOUNGCO", cik="0000000099", name="Young Co")
    submissions = SubmissionsDoc(
        raw={}, cik="0000000099", entity_name="Young Co", sic="7372", sic_description="Software",
        fiscal_year_end="1231", tickers=["YOUNGCO"], former_names=[], recent_filings=[],
    )
    facts_raw = {
        "cik": 99, "entityName": "Young Co",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"start": "2023-01-01", "end": "2023-12-31", "val": 1000000, "filed": "2024-02-01"},
                        {"start": "2024-01-01", "end": "2024-12-31", "val": 1500000, "filed": "2025-02-01"},
                    ]}
                }
            }
        },
    }
    company_facts = CompanyFactsDoc(raw=facts_raw, cik="0000000099", entity_name="Young Co", namespaces=["us-gaap"])
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    codes = [w.code for w in model.warnings]
    assert "THIN_HISTORY" in codes
    assert "REGISTRANT_CHANGE_SUSPECTED" not in codes
    thin_warning = next(w for w in model.warnings if w.code == "THIN_HISTORY")
    assert thin_warning.detail["years_available"] == 2


def test_pitfall7_non_usd_warning_when_only_a_non_usd_unit_is_present():
    """No required test ticker is a non-USD filer (all are US domestic) --
    exercised with a synthetic fixture where the anchor tag exists only
    under a non-USD unit key."""
    company = CompanyRef(ticker="FOREIGNCO", cik="0000000098", name="Foreign Co")
    submissions = SubmissionsDoc(
        raw={}, cik="0000000098", entity_name="Foreign Co", sic="2080", sic_description="Beverages",
        fiscal_year_end="1231", tickers=["FOREIGNCO"], former_names=[], recent_filings=[],
    )
    facts_raw = {
        "cik": 98, "entityName": "Foreign Co",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"EUR": [{"start": "2023-01-01", "end": "2023-12-31", "val": 1000000, "filed": "2024-02-01"}]}
                }
            }
        },
    }
    company_facts = CompanyFactsDoc(raw=facts_raw, cik="0000000098", entity_name="Foreign Co", namespaces=["us-gaap"])
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    codes = [w.code for w in model.warnings]
    assert "NON_USD" in codes
    assert "REGISTRANT_CHANGE_SUSPECTED" not in codes
    assert model.periods == ()


# ============================== invariant 1 in an end-to-end build: the axis is built once ==============================


def test_axis_built_once_every_fetched_line_item_keys_exactly_against_it():
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    axis_labels = {p.label for p in model.periods}
    for line_item in model.line_items:
        if line_item.is_subtotal:
            assert line_item.cells == {}
            continue
        assert set(line_item.cells.keys()) == axis_labels, (
            f"{line_item.key} cell keys {set(line_item.cells.keys())} do not match the shared "
            f"axis {axis_labels} -- every line item must be filled against the SAME axis (Section 5.1)"
        )


def test_subtotal_line_items_are_never_populated_by_phase2():
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    for key in ("gross_profit", "total_debt", "net_debt"):
        line_item = next(li for li in model.line_items if li.key == key)
        assert line_item.is_subtotal is True
        assert line_item.cells == {}


def test_no_cell_anywhere_is_a_fabricated_zero_standing_in_for_a_gap():
    """Every MISSING cell has value=None (enforced by the schema itself, so
    this end-to-end sweep is really confirming build_financial_model never
    tries to construct the forbidden state)."""
    company, submissions, company_facts = load_normalize_company("JPM")
    model = build_financial_model(company, submissions, company_facts, historical_years=10)
    for line_item in model.line_items:
        for cell in line_item.cells.values():
            if cell.provenance.mechanism is Mechanism.MISSING:
                assert cell.value is None


def test_historical_years_window_is_respected():
    company, submissions, company_facts = load_normalize_company("AAPL")
    model = build_financial_model(company, submissions, company_facts, historical_years=3)
    non_ltm = [p for p in model.periods if not p.label.startswith("LTM")]
    assert len(non_ltm) == 3
    assert [p.fiscal_year for p in non_ltm] == sorted(p.fiscal_year for p in non_ltm)  # oldest -> newest
