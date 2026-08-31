# CLAUDE.md — Fundamental Equity FSA

Session-starter context. Read this first, every session.

---

## What this project is

An automated pipeline for fundamental equity analysis. The user enters a ticker into an Excel
control panel and presses a macro button. The tool pulls that company's financial statements from
SEC EDGAR (10-K / 10-Q) and generates a **new Excel workbook** containing:

1. **`Financials`** — normalized, formatted income statement, balance sheet and cash flow statement.
2. **`DCF`** — a dynamic, formula-linked discounted cash flow model with clearly highlighted input
   cells (revenue growth, margins, WACC components, terminal growth rate, etc.) that produce an
   implied equity value and **implied share price**, recalculating natively in Excel.
3. **`Info`** — provenance: source filings, accession numbers, data gaps, warnings.

Full specification: **[PLANNING.md](PLANNING.md)**. It is the source of truth for architecture,
data handling, the Excel layout, and the phased task breakdown. Read it before implementing anything.

---

## Operating rules (binding)

### 1. Opus orchestrates; Sonnet implements
- **Opus does not write implementation code.** Opus plans, specifies interfaces, writes task briefs
  with acceptance criteria, delegates to Sonnet subagents, reviews their work, and iterates until the
  result meets the bar.
- **All direct code implementation is done by Sonnet subagents.**
- Opus may write and edit planning/spec/documentation files (`PLANNING.md`, `CLAUDE.md`, task briefs,
  interface contracts).
- Review means *verification*, not acceptance of a subagent's summary. Check the actual files and
  run the actual commands. Subagents sometimes report success they did not achieve.

### 2. No implementation without an approved plan
`PLANNING.md` must be approved by the owner before implementation starts. Material changes of
direction go back into `PLANNING.md` for approval rather than being absorbed silently.

### 3. The model must be live, not a snapshot
Python writes **Excel formulas**, never DCF results computed in Python and pasted in as constants.
If a cell depends on an assumption, it contains a formula referencing that assumption cell. This is
the whole point of the deliverable. It is enforced by a structural test.

The only numbers Python writes as literal values are (a) historical figures sourced from SEC filings
and (b) default values seeded into input cells.

### 4. The CLI is the product; Excel is a front end
Everything must work via `python -m fsa.cli --ticker XYZ` with no Excel involved. This keeps the
work testable and lets subagents verify their own output.

### 5. SEC rate limits are not optional
Max 10 req/s by SEC policy; **this tool caps at 5 req/s** via a token bucket. Every request carries a
`User-Agent` of the form `Name email@domain` (SEC returns 403 without it), sourced from local
gitignored config — never hardcoded, never committed. Exponential backoff with jitter, honoring
`Retry-After`.

### 6. Data sourcing: fetch on demand, cache, never warehouse
The `companyfacts` endpoint returns a company's entire tagged history in one request, so a full run
costs ~3 requests. There is no local filing database. There **is** a revalidating response cache
(ETag / conditional GET, 24h TTL) for development speed, offline work, and reproducibility.
Rationale in PLANNING.md §2.

### 7. Never silently zero-fill, never silently misalign
If a line item cannot be sourced, it is flagged in the workbook and listed on the `Info` sheet.
A zero that should be a gap is a wrong valuation that looks right.

Equally: establish the fiscal-year axis **once** from revenue, then fill every other line item
per-period against that axis. Never take "the last N periods this tag happens to have" — that returns
stale data for filers who switched tags mid-history (confirmed: TSLA D&A returns FY2013–2017).

The `mappings/us_gaap.yaml` file supports three mechanisms, all required: priority-ordered
**alternatives**, per-period **stitching** across tags, and **composition** (summing component tags
when no aggregate exists — without it, Microsoft yields no D&A at all). See PLANNING.md §5.

### 8. Personal project
No corporate proxy configuration. No enterprise auth. Keep dependencies minimal.

---

## Conventions

- **Python** 3.13, `venv/` in repo root, package under `src/fsa/`.
- **Excel cell styling** (standard sell-side): blue font on light-yellow fill = user input; black =
  same-sheet formula; green = cross-sheet link; grey italic = metadata.
- **Values in cells are raw USD**, displayed as millions via number format — never hand-scaled.
- **VBA source** lives as `.bas` text under `excel/vba/` and is version-controlled. The `.xlsm` is a
  rebuildable binary artifact.
- **us-gaap tag mappings** live in `mappings/us_gaap.yaml` as priority-ordered lists per canonical
  line item, so coverage can be improved without code changes.

## Known traps

- **WACC circularity.** The share price input is the *current market* price. Never wire the model's
  implied price into WACC — implied price → market cap → equity weight → WACC → implied price is a
  true circular reference Excel will refuse to calculate.
- **`companyfacts` returns only SEC-standard taxonomies** (`us-gaap`, `dei`, `srt`, `ecd`, …). Company
  extension tags never appear, so there is no custom taxonomy to parse — but a line tagged only with an
  extension shows up as a *gap*, not an error.
- **Restated facts** repeat a period with different `filed` dates. Latest `filed` wins; the `frame` key
  corroborates. Keep the accession number.

## Platform notes

- macOS. Excel for Mac runs VBA in an **App Sandbox**; `Shell()` and file access outside the sandbox
  container may need `GrantAccessToMultipleFiles`. This is de-risked by a Phase 0 spike, with
  `xlwings` as the fallback.
- No LibreOffice installed. Formula recalculation for validation is driven through the installed
  Excel via AppleScript.
