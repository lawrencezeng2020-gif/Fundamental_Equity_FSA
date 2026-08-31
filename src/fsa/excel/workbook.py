"""Workbook assembly: creates the target .xlsx, orders sheets, wires named ranges.

Phase 5 responsibility (orchestration), consuming the sheet writers built in
Phases 3-4. See PLANNING.md Section 3 architecture diagram and Section 9
phase table.
"""

from __future__ import annotations


def build_workbook(*args: object, **kwargs: object) -> None:
    """Assemble Info + Financials + DCF sheets into one workbook. Phase 5."""
    raise NotImplementedError("fsa.excel.workbook.build_workbook is implemented in Phase 5")
