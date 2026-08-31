"""`DCF` sheet writer -- the formula engine, and the core deliverable.

Phase 4 responsibility (PLANNING.md Section 6.4). Every cell PLANNING.md
describes here (assumptions block, WACC build, projection grid, terminal
value, bridge to implied share price, sensitivity tables) must be written as
a live Excel FORMULA referencing input/assumption cells -- never a Python-
computed constant. This is enforced by a structural test (PLANNING.md
Section 8, item 2).

Notable traps documented in PLANNING.md to respect here:
    - WACC circularity: the price input must be CURRENT MARKET price, never
      the model's own implied price (Section 6.4 B warning; CLAUDE.md "Known
      traps"). A blank price cell must fall through to the manual capital-
      structure weight override without producing #DIV/0!.
    - `WACC <= g` must be guarded with a readable error string, not #DIV/0!.
    - openpyxl cannot write native Excel What-If Data Tables -- sensitivity
      cells must each be a self-contained re-derivation formula (Section 6.4
      "Implementation note").
"""

from __future__ import annotations


def write_dcf_sheet(*args: object, **kwargs: object) -> None:
    """Write the DCF sheet into a workbook. Phase 4."""
    raise NotImplementedError("fsa.excel.dcf_sheet.write_dcf_sheet is implemented in Phase 4")
