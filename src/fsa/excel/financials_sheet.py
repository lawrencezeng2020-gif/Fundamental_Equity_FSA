"""`Financials` sheet writer.

Phase 3 responsibility (PLANNING.md Section 6.3):
    - Columns: line item label | up to `historical_years` fiscal years
      (oldest -> newest) | LTM.
    - Blocks: Income Statement, Balance Sheet, Cash Flow Statement, plus a
      Supplemental block (diluted shares, cover-page share count, total debt
      build, net debt).
    - Raw USD values written; display scaling to millions via number format
      (`#,##0,,;(#,##0,,)`) -- never a hand-scaled constant.
    - Subtotals (gross profit, EBIT, EBITDA, net debt, ...) are formulas
      summing their components, not fetched values.
    - Adjacent collapsible column group showing the us-gaap tag backing each
      row (provenance).
    - Defined names for every row the DCF sheet consumes: `fin_Revenue`,
      `fin_EBIT`, `fin_Cash`, `fin_TotalDebt`, `fin_DilutedShares`, etc.
"""

from __future__ import annotations


def write_financials_sheet(*args: object, **kwargs: object) -> None:
    """Write the Financials sheet into a workbook. Phase 3."""
    raise NotImplementedError(
        "fsa.excel.financials_sheet.write_financials_sheet is implemented in Phase 3"
    )
