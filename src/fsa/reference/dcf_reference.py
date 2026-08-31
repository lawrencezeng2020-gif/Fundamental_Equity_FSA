"""Independent Python DCF implementation, TEST ONLY, for cross-checking.

PLANNING.md Section 8, validation layer 3: this module implements the same
DCF math as `fsa.excel.dcf_sheet` independently in Python. The generated
workbook is force-recalculated (via AppleScript driving the installed Excel
for Mac -- see PLANNING.md Section 8 and the spike/ probe results) and its
computed implied share price is compared against this reference within
tolerance. This is the test that catches an off-by-one discount period or a
sign error in ΔNWC -- the class of bug that silently produces a plausible-
looking wrong price.

This module must NEVER be imported by `fsa.excel` or `fsa.cli` -- it exists
solely for the test suite added in Phase 7/validation, to avoid the
reference and the implementation-under-test sharing a bug.
"""

from __future__ import annotations


def compute_implied_share_price(*args: object, **kwargs: object) -> float:
    """Independently compute implied share price for cross-checking. Phase 7."""
    raise NotImplementedError(
        "fsa.reference.dcf_reference.compute_implied_share_price is implemented in Phase 7"
    )
