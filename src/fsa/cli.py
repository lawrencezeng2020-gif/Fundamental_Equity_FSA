"""Entry point, orchestration, exit codes.

Phase 0 scope was argument parsing/validation, config loading, logging setup.
Phase 1 added the SEC transport/caching layer (`fsa.sec`). Phase 2 adds
`fsa.normalize`: `--ticker`/`--cik` now builds the full canonical
`FinancialModel` (IS/BS/CF + LTM, per PLANNING.md Section 13.2) and prints a
readable text rendering of it -- periods as columns, line items as rows,
gaps visibly marked -- so the normalization layer is inspectable without
Excel. Writing the actual workbook is Phase 3/4 (`fsa.excel`), so the run
still ends with a "not yet implemented" note for that part and exits 0.

Console entry point: `fsa` (see pyproject.toml `[project.scripts]`), and
`python -m fsa.cli` for the same behavior without installation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path

from fsa import __version__
from fsa.config import ConfigError, ensure_output_dir, load_settings
from fsa.normalize.schema import Cell, FinancialModel, LineItem, SchemaError, Statement
from fsa.normalize.statements import MappingError, build_financial_model
from fsa.sec.client import SecClient
from fsa.sec.endpoints import (
    CompanyRef,
    FilingRef,
    SubmissionsDoc,
    fetch_company_facts,
    fetch_submissions,
    resolve_cik,
)
from fsa.sec.errors import SecError, TickerNotFound

logger = logging.getLogger("fsa")

# Exit codes. Defined now so every phase agrees on their meaning.
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_DATA_GAP_ERROR = 4

_TICKER_RE_MAX_LEN = 10  # generous upper bound; real validation happens against SEC's ticker map in Phase 1


def _ticker_type(raw: str) -> str:
    """argparse type= validator: uppercase and sanity-check a ticker string.

    This is a syntactic check only (letters, digits, '.', '-', reasonable
    length) -- confirming the ticker actually resolves to a CIK is Phase 1's
    job (fsa.sec.endpoints), since that requires a network call.
    """
    candidate = raw.strip().upper()
    if not candidate:
        raise argparse.ArgumentTypeError("ticker must not be empty")
    if len(candidate) > _TICKER_RE_MAX_LEN:
        raise argparse.ArgumentTypeError(
            f"ticker {raw!r} is too long to be a valid US ticker symbol"
        )
    if not all(c.isalnum() or c in ".-" for c in candidate):
        raise argparse.ArgumentTypeError(
            f"ticker {raw!r} contains invalid characters "
            "(expected letters, digits, '.', or '-')"
        )
    return candidate


def _cik_type(raw: str) -> str:
    """argparse type= validator for ``--cik``: digits only, zero-padded to 10.

    ``--cik`` bypasses ticker->CIK resolution entirely (see
    ``fsa.sec.endpoints`` module docstring and PLANNING.md Section 5.3.11):
    the ticker map can point at a freshly created registrant CIK holding
    almost no history (observed for XOM), while the CIK holding the real
    multi-decade history has no ticker associated with it at all and so
    cannot be reached by ticker lookup, only by CIK. Accepts either the
    unpadded (e.g. ``34088``) or zero-padded (``0000034088``) form.
    """
    candidate = raw.strip()
    if not candidate.isdigit():
        raise argparse.ArgumentTypeError(f"--cik must be numeric, got {raw!r}")
    if len(candidate) > 10:
        raise argparse.ArgumentTypeError(f"--cik {raw!r} is too long (max 10 digits)")
    return candidate.zfill(10)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fsa",
        description=(
            "Fundamental Equity FSA: pull SEC EDGAR filings for a ticker and "
            "generate a normalized Financials + live DCF Excel workbook."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    identity_group = parser.add_mutually_exclusive_group(required=True)
    identity_group.add_argument(
        "--ticker",
        type=_ticker_type,
        help="Ticker symbol to analyze (e.g. AAPL). Case-insensitive; normalized to uppercase.",
    )
    identity_group.add_argument(
        "--cik",
        type=_cik_type,
        help=(
            "SEC CIK to analyze directly, bypassing ticker->CIK resolution "
            "entirely (10 digits, or unpadded, e.g. 34088). Mutually "
            "exclusive with --ticker. Use this when ticker resolution would "
            "pick the wrong registrant -- e.g. a company that changed SEC "
            "registrant CIK, where the ticker map's current entry may hold "
            "almost no filing history (see PLANNING.md Section 5.3.11)."
        ),
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        metavar="DIR",
        help="Output directory for the generated workbook (default: configured output_dir).",
    )
    parser.add_argument(
        "--price",
        type=_positive_float,
        default=None,
        metavar="PRICE",
        help=(
            "Current market share price to seed the DCF's market-value equity "
            "weight (see PLANNING.md Decision D1). Optional; leave unset to use "
            "the manual capital-structure weight override instead."
        ),
    )
    parser.add_argument(
        "--projection-years",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Forward projection horizon for the DCF (default: configured projection_years).",
    )
    parser.add_argument(
        "--historical-years",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Historical fiscal years to pull for the Financials sheet (default: configured historical_years).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force revalidation of cached SEC responses for this run (Phase 1).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the response cache entirely for this run (Phase 1).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


def configure_logging(verbose: bool) -> None:
    """Central logging configuration.

    Log lines are formatted to support the request-auditing use case in
    PLANNING.md Section 4 (endpoint, status, bytes, cache hit/miss will be
    logged by fsa.sec.client in Phase 1) -- timestamp, level, logger name and
    message are all present so those fields are greppable/parseable later.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


def _most_recent_filing(filings: list[FilingRef], form: str) -> FilingRef | None:
    """The most recently filed entry of ``form`` (e.g. "10-K"), if any.

    ``submissions.filings.recent`` is already newest-first in practice, but
    that ordering isn't part of SEC's documented contract, so this sorts
    explicitly by ``filing_date`` rather than trusting array order.
    """
    matches = [f for f in filings if f.form == form]
    if not matches:
        return None
    return max(matches, key=lambda f: f.filing_date)


def _print_sec_summary(doc: SubmissionsDoc, namespaces: list[str], tag_counts: dict[str, int]) -> None:
    print("\nSEC EDGAR summary:")
    print(f"  Company: {doc.entity_name}")
    print(f"  CIK: {doc.cik}")
    print(f"  SIC: {doc.sic} ({doc.sic_description})")
    print(f"  Fiscal year end (MMDD): {doc.fiscal_year_end}")
    # Purely descriptive -- not a judgment. An empty list here is the signal
    # Phase 2 may use to raise REGISTRANT_CHANGE_SUSPECTED (PLANNING.md
    # Section 5.3.11); this CLI summary doesn't interpret it, only shows it.
    print(f"  Tickers on file (submissions): {', '.join(doc.tickers) if doc.tickers else '(none)'}")
    if doc.former_names:
        names = ", ".join(fn.get("name", "?") for fn in doc.former_names)
        print(f"  Former name(s): {names}")
    print(f"  companyfacts namespaces: {', '.join(namespaces) if namespaces else '(none)'}")
    for ns in namespaces:
        print(f"    {ns}: {tag_counts[ns]} tags")

    latest_10k = _most_recent_filing(doc.recent_filings, "10-K")
    latest_10q = _most_recent_filing(doc.recent_filings, "10-Q")
    print("  Most recent 10-K:", end=" ")
    if latest_10k is not None:
        print(f"{latest_10k.filing_date} (accession {latest_10k.accession}, period end {latest_10k.period_end})")
    else:
        print("none found in recent filings")
    print("  Most recent 10-Q:", end=" ")
    if latest_10q is not None:
        print(f"{latest_10q.filing_date} (accession {latest_10q.accession}, period end {latest_10q.period_end})")
    else:
        print("none found in recent filings")


_STATEMENT_ORDER = [Statement.INCOME, Statement.BALANCE, Statement.CASHFLOW, Statement.SUPPLEMENTAL]
_STATEMENT_TITLES = {
    Statement.INCOME: "Income Statement",
    Statement.BALANCE: "Balance Sheet",
    Statement.CASHFLOW: "Cash Flow Statement",
    Statement.SUPPLEMENTAL: "Supplemental",
}
_GAP_MARKER = "n/a"


def _format_cell_value(cell: Cell | None) -> str:
    if cell is None or cell.value is None:
        return _GAP_MARKER
    value: Decimal = cell.value
    if cell.unit == "USD":
        return f"{(value / Decimal(1_000_000)):,.0f}"
    if cell.unit == "shares":
        return f"{(value / Decimal(1_000_000)):,.1f}M"
    # USD/shares (EPS) -- small numbers, show as-is.
    return f"{value:,.2f}"


def render_financial_model_text(model: FinancialModel) -> str:
    """A readable, column-aligned text rendering of the model: periods as
    columns, line items as rows, gaps marked explicitly (never blank, never
    a silent 0) -- PLANNING.md Section 4's "CLI is the product" rule made
    concrete for Phase 2's output specifically. USD line items are shown in
    millions (raw USD is still what Phase 2 stores and what Excel will
    write per PLANNING.md Section 6.3 -- this is a display convenience
    only). Subtotal line items (`is_subtotal=True`) carry no Phase 2 values
    by design (PLANNING.md Section 13.2 invariant 4) and are shown as
    "(formula)" -- Phase 3 computes them as Excel formulas.
    """
    period_labels = [p.label for p in model.periods]
    lines: list[str] = []

    lines.append(f"{model.company.name} ({model.company.ticker}, CIK {model.company.cik})")
    lines.append(f"Currency: {model.currency}   Fiscal year end (MMDD): {model.fiscal_year_end or 'unknown'}")
    lines.append("Values in USD line items are millions; shares in millions; EPS as reported.")

    if not period_labels:
        lines.append("")
        lines.append("No usable periods were resolved for this company -- see warnings below.")
    else:
        label_width = max([len("Line item"), *(len(li.label) for li in model.line_items)]) + 2
        col_width = max(10, max((len(pl) for pl in period_labels), default=10) + 2)

        by_statement: dict[Statement, list[LineItem]] = {s: [] for s in _STATEMENT_ORDER}
        for li in model.line_items:
            by_statement[li.statement].append(li)

        for statement in _STATEMENT_ORDER:
            items = by_statement[statement]
            if not items:
                continue
            lines.append("")
            lines.append(f"== {_STATEMENT_TITLES[statement]} ==")
            header = "Line item".ljust(label_width) + "".join(pl.rjust(col_width) for pl in period_labels)
            lines.append(header)
            lines.append("-" * len(header))
            for li in items:
                if li.is_subtotal:
                    row = li.label.ljust(label_width) + "".join("(formula)".rjust(col_width) for _ in period_labels)
                else:
                    row = li.label.ljust(label_width) + "".join(
                        _format_cell_value(li.cells.get(pl)).rjust(col_width) for pl in period_labels
                    )
                lines.append(row)

    if model.warnings:
        lines.append("")
        lines.append("== Warnings ==")
        for w in model.warnings:
            lines.append(f"[{w.code}] {w.message}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    logger.debug("Parsed CLI arguments: %s", vars(args))

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error:\n\n{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG_ERROR

    # Fold CLI overrides into a single authoritative Settings object -- this
    # is the only place `--out` / `--historical-years` / `--projection-years`
    # get applied, so there is exactly one source of truth downstream (see
    # Settings.with_overrides docstring). Loading config above did not touch
    # the filesystem; the override below must be resolved *before* anything
    # creates a directory, or `--out` would leave behind an unused
    # config-default directory.
    override_output_dir = (
        Path(args.out).expanduser().resolve() if args.out is not None else None
    )
    settings = settings.with_overrides(
        output_dir=override_output_dir,
        historical_years=args.historical_years,
        projection_years=args.projection_years,
    )

    try:
        ensure_output_dir(settings)
    except ConfigError as exc:
        print(f"Configuration error:\n\n{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG_ERROR

    run_config = {
        "ticker": args.ticker,
        "cik": args.cik,
        "price": args.price,
        "refresh": args.refresh,
        "no_cache": args.no_cache,
        "verbose": args.verbose,
        **settings.as_display_dict(),
    }

    print("Resolved run configuration:")
    for key, value in run_config.items():
        print(f"  {key}: {value}")

    logger.info("Run configuration resolved for ticker=%s cik=%s", args.ticker, args.cik)

    company: CompanyRef | None = None
    with SecClient(
        settings,
        use_cache=not args.no_cache,
        force_refresh=args.refresh,
    ) as client:
        try:
            if args.cik is not None:
                # --cik takes precedence by construction: it's mutually
                # exclusive with --ticker (see build_parser), so exactly one
                # of the two is ever set. This bypasses resolve_cik entirely
                # -- see PLANNING.md Section 5.3.11 / the XOM finding for why
                # ticker resolution alone cannot be trusted to reach the CIK
                # holding a company's actual filing history.
                cik = args.cik
                logger.info("Using explicit --cik=%s, bypassing ticker resolution", cik)
            else:
                company = resolve_cik(client, args.ticker)
                cik = company.cik
                logger.info("Resolved ticker=%s to CIK=%s (%s)", args.ticker, cik, company.name)
            submissions = fetch_submissions(client, cik)
            company_facts = fetch_company_facts(client, cik)
            if company is None:
                # --cik path: no CompanyRef from resolve_cik, so build one
                # from what fetch_submissions gave us (its own entity name,
                # which is authoritative regardless of ticker resolution).
                company = CompanyRef(ticker=args.ticker or cik, cik=cik, name=submissions.entity_name)
        except TickerNotFound as exc:
            # A bad ticker is treated as bad *input*, not a network failure or
            # a data gap in an otherwise-resolved company -- same family as
            # argparse's own exit(2) for a malformed argument. See the Phase 1
            # report for why this reading of the (currently unspecified for
            # this case) exit-code table was chosen.
            print(f"\n{exc}", file=sys.stderr)
            return EXIT_USAGE_OR_CONFIG_ERROR
        except SecError as exc:
            print(f"\nSEC EDGAR request failed:\n\n{exc}", file=sys.stderr)
            return EXIT_NETWORK_ERROR

    tag_counts = {ns: len(company_facts.tags(ns)) for ns in company_facts.namespaces}
    _print_sec_summary(submissions, company_facts.namespaces, tag_counts)

    try:
        model = build_financial_model(
            company, submissions, company_facts, historical_years=settings.historical_years
        )
    except (MappingError, SchemaError) as exc:
        # A malformed mapping file or a schema invariant violated while
        # building the model is a data-layer failure, not a network or
        # usage error -- distinct exit code per PLANNING.md Section 13.1.
        print(f"\nFailed to build the normalized financial model:\n\n{exc}", file=sys.stderr)
        return EXIT_DATA_GAP_ERROR

    print("\n" + render_financial_model_text(model))

    print(
        "\nPhase 3-4 not yet implemented (fsa.excel: Financials/DCF Excel "
        "sheet writers). The normalized model above was built successfully; "
        "no workbook is written yet."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
