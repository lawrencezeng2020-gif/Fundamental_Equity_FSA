"""Tests for fsa.cli argument parsing and Phase 0/1 behavior."""

from __future__ import annotations

import pytest

from fsa.cli import (
    EXIT_NETWORK_ERROR,
    EXIT_OK,
    EXIT_USAGE_OR_CONFIG_ERROR,
    build_parser,
    main,
)
from fsa.sec.errors import SecUnavailable, TickerNotFound


def test_one_of_ticker_or_cik_is_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_ticker_and_cik_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--ticker", "AAPL", "--cik", "320193"])


def test_cik_alone_is_accepted_and_zero_padded():
    parser = build_parser()
    args = parser.parse_args(["--cik", "320193"])
    assert args.cik == "0000320193"
    assert args.ticker is None


def test_cik_accepts_already_padded_form():
    parser = build_parser()
    args = parser.parse_args(["--cik", "0000320193"])
    assert args.cik == "0000320193"


def test_cik_rejects_non_numeric():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--cik", "AAPL"])


def test_cik_rejects_too_many_digits():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--cik", "12345678901"])


def test_ticker_is_uppercased():
    parser = build_parser()
    args = parser.parse_args(["--ticker", "aapl"])
    assert args.ticker == "AAPL"


def test_ticker_with_invalid_characters_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--ticker", "AA PL"])


def test_projection_years_must_be_positive_int():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--ticker", "AAPL", "--projection-years", "0"])


def test_price_must_be_positive_number():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--ticker", "AAPL", "--price", "-5"])
    args = parser.parse_args(["--ticker", "AAPL", "--price", "123.45"])
    assert args.price == 123.45


def test_main_with_valid_ticker_and_config_exits_ok(tmp_config_file, monkeypatch, capsys, mock_sec_calls):
    config_path = tmp_config_file()
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", config_path.parent / "nonexistent")

    exit_code = main(["--ticker", "aapl"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert "AAPL" in captured.out
    assert "not yet implemented" in captured.out.lower()
    # Phase 1 summary content, sourced from the mocked SEC responses.
    assert "Apple Inc." in captured.out
    assert "0000320193" in captured.out
    assert "dei" in captured.out and "us-gaap" in captured.out
    assert "10-K" in captured.out
    assert "10-Q" in captured.out


def test_main_without_config_exits_with_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", tmp_path / "missing.toml")
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", tmp_path / "also_missing.toml")

    exit_code = main(["--ticker", "AAPL"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_OR_CONFIG_ERROR
    assert "Configuration error" in captured.err


def test_version_flag_exits_zero(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0


def test_out_override_does_not_create_config_default_output_dir(
    tmp_config_file, tmp_path, monkeypatch, capsys, mock_sec_calls
):
    """Regression test: `--out` must fully replace the config-default output
    directory for this run, not merely shadow it for display while the
    config-default directory still gets created underneath as a side effect
    of loading settings. Only the overridden directory should be created."""
    config_default_dir = tmp_path / "config_default_output"
    override_dir = tmp_path / "override_output"

    config_path = tmp_config_file(output_dir=str(config_default_dir))
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", config_path.parent / "nonexistent")

    exit_code = main(["--ticker", "aapl", "--out", str(override_dir)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK

    # The resolved settings actually used (and printed) must reflect the override.
    assert f"output_dir: {override_dir}" in captured.out

    # Only the override directory should exist on disk; the config-default
    # directory must never have been created as a side effect.
    assert override_dir.is_dir()
    assert not config_default_dir.exists()


def test_main_with_bogus_ticker_exits_cleanly_not_with_traceback(
    tmp_config_file, monkeypatch, capsys
):
    """A ticker that doesn't resolve to a CIK must produce a typed error and
    a clean exit -- never an uncaught traceback."""
    config_path = tmp_config_file()
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", config_path.parent / "nonexistent")

    def _raise_not_found(client, ticker):
        raise TickerNotFound(ticker)

    monkeypatch.setattr("fsa.cli.resolve_cik", _raise_not_found)

    exit_code = main(["--ticker", "ZZZZZZNOPE"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_OR_CONFIG_ERROR
    assert "ZZZZZZNOPE" in captured.err
    assert "not found" in captured.err.lower()


def test_main_with_sec_network_failure_exits_with_network_error_code(
    tmp_config_file, monkeypatch, capsys
):
    """A network-layer failure (SEC unreachable, retries exhausted, etc.)
    must map to EXIT_NETWORK_ERROR, not a traceback."""
    config_path = tmp_config_file()
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", config_path.parent / "nonexistent")

    def _raise_unavailable(client, ticker):
        raise SecUnavailable("SEC returned HTTP 503 for ... after 5 attempts")

    monkeypatch.setattr("fsa.cli.resolve_cik", _raise_unavailable)

    exit_code = main(["--ticker", "AAPL"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_NETWORK_ERROR
    assert "SEC EDGAR request failed" in captured.err


def test_main_with_cik_bypasses_ticker_resolution_entirely(tmp_config_file, monkeypatch, capsys):
    """--cik must go straight to fetch_submissions/fetch_company_facts and
    never call resolve_cik -- that's the whole point of the escape hatch
    (PLANNING.md Section 5.3.11 / the XOM finding: ticker resolution can land
    on a registrant CIK with almost no history)."""
    config_path = tmp_config_file()
    monkeypatch.setattr("fsa.config.PROJECT_CONFIG_PATH", config_path)
    monkeypatch.setattr("fsa.config.USER_CONFIG_PATH", config_path.parent / "nonexistent")

    def _must_not_be_called(client, ticker):
        raise AssertionError("resolve_cik must not be called when --cik is given")

    from datetime import date

    from fsa.sec.endpoints import CompanyFactsDoc, SubmissionsDoc

    submissions = SubmissionsDoc(
        raw={},
        cik="0000034088",
        entity_name="EXXON MOBIL CORP",
        sic="2911",
        sic_description="Petroleum Refining",
        fiscal_year_end="1231",
        tickers=[],
        former_names=[{"name": "EXXON CORP", "from": "1994-05-11", "to": "1999-11-30"}],
        recent_filings=[],
    )
    company_facts = CompanyFactsDoc(
        raw={"facts": {"dei": {}, "us-gaap": {"Revenues": {}}}},
        cik="0000034088",
        entity_name="EXXON MOBIL CORP",
        namespaces=["dei", "us-gaap"],
    )

    captured_ciks = []

    def _fake_fetch_submissions(client, cik):
        captured_ciks.append(cik)
        return submissions

    monkeypatch.setattr("fsa.cli.resolve_cik", _must_not_be_called)
    monkeypatch.setattr("fsa.cli.fetch_submissions", _fake_fetch_submissions)
    monkeypatch.setattr("fsa.cli.fetch_company_facts", lambda client, cik: company_facts)

    exit_code = main(["--cik", "34088"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert captured_ciks == ["0000034088"]  # zero-padded before use
    assert "EXXON MOBIL CORP" in captured.out
    assert "Tickers on file (submissions): (none)" in captured.out
    assert "Former name(s): EXXON CORP" in captured.out
