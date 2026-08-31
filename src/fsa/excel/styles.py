"""Blue-input / black-formula / green-link cell style convention.

Phase 3 responsibility (PLANNING.md Section 6.1):

| Style                                          | Meaning                          |
|-------------------------------------------------|-----------------------------------|
| Blue font, light-yellow fill, thin border       | User input / assumption          |
| Black font                                      | Formula computed on the same sheet |
| Green font                                      | Link to another sheet            |
| Grey italic                                     | Metadata / provenance            |

Applied consistently across `financials_sheet.py` and `dcf_sheet.py`. Input
cells additionally need data validation (numeric ranges) and should be
collected under a defined-name group for discoverability.
"""

from __future__ import annotations
