# STATUS.md — snapshot as of 2026-08-29

Working snapshot for session continuity. Authoritative spec remains `PLANNING.md`;
operating rules remain `CLAUDE.md`. This file records *where we are*, not *what we decided*.

---

## Phase status

| Phase | State | Notes |
|---|---|---|
| 0. Scaffold + macOS spike | **Complete** (verified) | Manual spike step **not yet run** — needs the owner |
| 1. SEC client | **Complete** (verified) | 403 cap, `--cik` override, registrant-change signal all landed |
| 2. Normalization | **Complete** (verified) | 181 tests; debt-convention limitation documented below |
| 3. Financials sheet | Not started | Unblocked — schema frozen |
| 4. DCF sheet | Not started | Unblocked — schema frozen |
| 5. Orchestration | Not started | |
| 6. Excel front end | **Blocked** on the manual spike | |
| 7. Validation + docs | Not started | |

**Test suite:** `venv/bin/pytest -q` → **181 passed, 16 deselected**. `-m live` → 16 passed.

---

## What actually works today

```
python -m fsa.cli --ticker AAPL      # resolve, fetch, normalize, print text model
python -m fsa.cli --cik 34088        # bypass ticker resolution (registrant changes)
```

- **SEC transport:** token-bucket rate limiter (5 req/s, injected clock), User-Agent enforced before
  any socket opens, 429/5xx retried to 5 attempts, 403 capped at 2 with response body surfaced,
  404 never retried. Gzipped-JSON response cache with TTL; `--refresh` / `--no-cache` honored.
- **Resolution:** ticker→CIK with class-share normalization (`BRK.B` ≡ `BRK-B`), `--cik` override
  mutually exclusive with `--ticker`.
- **Normalization:** revenue-anchored period axis; alternatives / stitching / composition, plus
  `derived` and `cover_page` resolution kinds; latest-filed restatement dedup with `frame`
  corroboration; LTM derivation; gaps as `None` with full provenance per cell.
- **Warnings:** `FINANCIAL_SECTOR`, `THIN_HISTORY`, `STITCH_DISAGREEMENT`, `NON_USD`,
  `REGISTRANT_CHANGE_SUSPECTED`, `DEBT_CONVENTION_UNKNOWN`.
- **Coverage:** ~100% on most line items for AAPL/MSFT/KO/TSLA; JPM sparse by design (financial
  sector); XOM-by-ticker correctly yields zero periods + registrant warning.

---

## Outstanding work

### Accepted limitation: net debt is underivable for some filers

`long_term_debt` is a **gap** for TSLA, JPM, RDDT and CAVA, so `net_debt` — and therefore the DCF's
implied share price — is unavailable for them. This is a real limit of standard XBRL tags, not a bug.

A derivation (`LongTermDebt − short_term_debt`) was implemented and then **reverted**: it produced
$13M for TSLA FY2022 against a true ~$1,029M, a 99% understatement that would have flowed into the
equity bridge. `LongTermDebt`'s scope is not stable even within one filer — TSLA satisfied
`LongTermDebt = Noncurrent + Current` exactly in 2011-12, but by FY2022 the current-side tag had
become `DebtCurrent` (includes current finance leases) while `LongTermDebt` excludes finance leases.
Automatic calibration was considered and rejected on that evidence. Filers in this state now raise
`DEBT_CONVENTION_UNKNOWN`. Verified live: JPM reports no noncurrent, `DebtCurrent` or
`LongTermDebtCurrent` tag at all after 2014.

**Phase 4 consequence:** the DCF must fail visibly when `net_debt` is missing, never treat it as zero.

### Known hazards recorded for later phases

- **Subtotal gap absorption (Phase 3, `PLANNING.md` §6.3).** Excel treats blank as zero, so a gap in
  `long_term_debt` would silently understate `total_debt`, overstate equity value, and yield a wrong
  share price with no visible error. Gaps must render as `#N/A` or a propagating sentinel. Required test.
- **WACC circularity (Phase 4, §6.4B).** The price input is *current market* price. Wiring implied
  price into WACC creates a real circular reference. Blank price must fall through to the manual
  weight override, not `#DIV/0!`.
- **Sensitivity tables (Phase 4, §6.4F).** openpyxl cannot write native Excel What-If Data Tables;
  each cell must be a self-contained formula re-deriving the valuation. No Python-computed constants.
- **Fiscal-year labelling** uses the calendar year of the period end — wrong for Jan/Feb-FYE retailers.
  Outside the current ticker set; recorded, not fixed.
- **Phase 6 is gated** on the unrun manual spike.

### Repository hygiene

- **Nothing is committed.** The entire project (`src/`, `tests/`, `mappings/`, `spike/`, all planning
  docs) is untracked; only the original `.gitignore` and `README.md` are in git. One `rm -rf` from
  total loss. Recommend an initial commit at the next natural checkpoint.
- `.fsa.toml` holds the SEC User-Agent identity and is correctly gitignored; no identity is
  hardcoded anywhere in tracked files.

---

## Waiting on the owner

1. **Run the manual spike** (~5 min) — `spike/README.md`. Import `SpikeShell.bas` in Excel's VB
   editor, run it, read the status cell. Gates Phase 6 only. If it FAILs, first remedy is defaulting
   output to `~/Desktop/FSA_Output` (symlinked through the sandbox; `~/Documents` is not), then
   xlwings.
2. **Decide whether to commit** the current tree.
