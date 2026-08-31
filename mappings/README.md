# mappings/

This directory will hold `us_gaap.yaml`: the canonical-line-item -> us-gaap-tag
mapping that Phase 2 (`fsa.normalize`) uses to build the Income Statement,
Balance Sheet, and Cash Flow Statement from raw XBRL facts.

Not yet populated -- this is Phase 0 (scaffold only). Phase 2 will write
`us_gaap.yaml` supporting the three mechanisms specified in PLANNING.md
Section 5.2, all three of which are mandatory:

1. **Alternatives** -- a priority-ordered list of tags meaning the same
   thing; the highest-priority tag with a value for a given period wins.
2. **Per-period stitching** -- different periods on the same axis satisfied
   by *different* tags across an accounting-standard transition (e.g. TSLA
   revenue needs both `RevenueFromContractWithCustomerExcludingAssessedTax`
   post-ASC 606 and `Revenues` before it).
3. **Composition** -- a canonical line defined as the sum of component tags
   when no aggregate tag exists (e.g. neither MSFT nor TSLA report
   `DepreciationDepletionAndAmortization` directly; it must be composed from
   `Depreciation` + `AmortizationOfIntangibleAssets` +
   `FinanceLeaseRightOfUseAssetAmortization`).

See PLANNING.md Section 5 for the full rationale and the empirical findings
behind each mechanism.
