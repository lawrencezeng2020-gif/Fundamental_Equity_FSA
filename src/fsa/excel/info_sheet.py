"""`Info` sheet writer: provenance, warnings, disclaimer.

Phase 5 responsibility (PLANNING.md Section 6.2). Content: ticker, company
name, CIK, SIC/industry, fiscal year end, reporting currency, generation
timestamp, tool version; table of source filings (form, period, filing date,
accession number, EDGAR URL); unmapped/missing line items and which mapping
mechanism resolved each; warnings (financial-sector filer, thin public
history under 3 years, stitched-tag overlap disagreements); disclaimer.
"""

from __future__ import annotations


def write_info_sheet(*args: object, **kwargs: object) -> None:
    """Write the Info sheet into a workbook. Phase 5."""
    raise NotImplementedError("fsa.excel.info_sheet.write_info_sheet is implemented in Phase 5")
