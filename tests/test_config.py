"""Tests for fsa.config -- the one Phase 0 module with real business logic."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fsa.config import ConfigError, ensure_output_dir, load_settings


def test_load_settings_minimal_valid_config(tmp_config_file, tmp_path):
    # Point output_dir at a fresh tmp_path location rather than relying on the
    # real DEFAULT_OUTPUT_DIR (~/Documents/FSA_Output) -- that path may or may
    # not already exist on the machine running the tests, which would make
    # the "not created as a side effect" assertion below flaky/host-dependent.
    output_dir = tmp_path / "not_yet_created_output"
    path = tmp_config_file(output_dir=str(output_dir))
    settings = load_settings(config_path=path)

    assert settings.sec_user_agent == "Test Runner test.runner@example.com"
    assert settings.historical_years == 10
    assert settings.projection_years == 5
    assert settings.rate_limit_rps == 5
    assert settings.cache_ttl_hours == 24
    assert settings.output_dir.is_absolute()
    # load_settings() is side-effect free: it must NOT create the directory.
    assert not settings.output_dir.exists()
    assert settings.cache_dir.is_absolute()
    assert settings.source_path == path


def test_no_config_file_found_via_search_raises_actionable_error(tmp_path, monkeypatch):
    """This exercises the real code path used by `fsa.cli` (load_settings() with
    no explicit path): both candidate locations are searched and, if neither
    exists, the error names both paths and the required format."""
    import fsa.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_CONFIG_PATH", tmp_path / "project.toml")
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", tmp_path / "user.toml")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "project.toml" in message
    assert "user.toml" in message
    assert "sec_user_agent" in message


def test_explicit_missing_config_path_raises_actionable_error(tmp_path):
    missing_path = tmp_path / "does_not_exist.toml"

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=missing_path)

    message = str(excinfo.value)
    assert "does_not_exist.toml" in message


def test_missing_user_agent_key_raises_actionable_error(tmp_path):
    path = tmp_path / ".fsa.toml"
    path.write_text('historical_years = 5\n')

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    message = str(excinfo.value)
    assert "sec_user_agent" in message
    assert str(path) in message


@pytest.mark.parametrize(
    "bad_value",
    [
        "",
        "no-email-here",
        "just.an.email@example.com",  # missing a leading "name" token separated by whitespace
    ],
)
def test_malformed_user_agent_raises_actionable_error(tmp_path, bad_value):
    path = tmp_path / ".fsa.toml"
    path.write_text(f'sec_user_agent = "{bad_value}"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "sec_user_agent" in str(excinfo.value)


def test_invalid_toml_raises_actionable_error(tmp_path):
    path = tmp_path / ".fsa.toml"
    path.write_text("this is not valid toml {{{")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "not valid TOML" in str(excinfo.value)


def test_rate_limit_above_sec_policy_limit_rejected(tmp_config_file):
    path = tmp_config_file(rate_limit_rps=20)

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=path)

    assert "rate_limit_rps" in str(excinfo.value)


def test_custom_paths_are_resolved_absolute(tmp_config_file, tmp_path):
    cache_dir = tmp_path / "somewhere" / "cache"
    output_dir = tmp_path / "somewhere" / "output"
    path = tmp_config_file(cache_dir=str(cache_dir), output_dir=str(output_dir))

    settings = load_settings(config_path=path)

    assert settings.cache_dir == cache_dir.resolve()
    assert settings.output_dir == output_dir.resolve()
    # load_settings() must not have created it -- that's ensure_output_dir()'s job.
    assert not settings.output_dir.exists()

    ensure_output_dir(settings)
    assert settings.output_dir.is_dir()


def test_negative_historical_years_rejected(tmp_config_file):
    path = tmp_config_file(historical_years=-1)

    with pytest.raises(ConfigError):
        load_settings(config_path=path)


def test_example_file_contains_only_a_placeholder_identity():
    """The committed .fsa.toml.example must exist and must not carry any real
    name or email -- only the obvious placeholder from the template. It is
    expected to be *syntactically* well-formed (so it demonstrates the
    required shape), but its content must not be a real identity."""
    example_path = Path(__file__).resolve().parents[1] / ".fsa.toml.example"
    assert example_path.is_file()

    content = example_path.read_text()
    assert "Your Name your.email@example.com" in content
    # Guard against ever committing a real-looking address in the example.
    assert "example.com" in content


def test_ensure_output_dir_creates_missing_directory(tmp_config_file, tmp_path):
    output_dir = tmp_path / "fresh" / "output"
    path = tmp_config_file(output_dir=str(output_dir))
    settings = load_settings(config_path=path)

    assert not settings.output_dir.exists()
    ensure_output_dir(settings)
    assert settings.output_dir.is_dir()

    # Idempotent: calling again on an already-existing dir must not raise.
    ensure_output_dir(settings)


def test_ensure_output_dir_unwritable_parent_raises_actionable_configerror(
    tmp_config_file, tmp_path
):
    """Regression test: ensure_output_dir() must turn a filesystem OSError
    (permission denied, read-only fs, unmounted volume, ...) into a
    ConfigError naming the path -- never let it escape as a raw traceback.

    Reproduced via a chmod 500 (read+execute, no write) parent directory, so
    mkdir() on a child path underneath it fails with PermissionError. Mode is
    restored in a `finally` block so pytest's tmp_path cleanup can still
    remove it afterward.
    """
    readonly_parent = tmp_path / "ro"
    readonly_parent.mkdir()
    blocked_output_dir = readonly_parent / "blocked"

    original_mode = readonly_parent.stat().st_mode
    os.chmod(readonly_parent, stat.S_IRUSR | stat.S_IXUSR)  # r-x------, no write

    try:
        path = tmp_config_file(output_dir=str(blocked_output_dir))
        settings = load_settings(config_path=path)

        with pytest.raises(ConfigError) as excinfo:
            ensure_output_dir(settings)

        message = str(excinfo.value)
        assert str(blocked_output_dir) in message
        assert "output_dir" in message
    finally:
        os.chmod(readonly_parent, original_mode)


def test_with_overrides_output_dir_only_affects_output_dir(tmp_config_file, tmp_path):
    path = tmp_config_file()
    settings = load_settings(config_path=path)

    override_dir = tmp_path / "overridden"
    updated = settings.with_overrides(output_dir=override_dir)

    assert updated.output_dir == override_dir
    assert updated is not settings
    # Everything else carries over unchanged.
    assert updated.sec_user_agent == settings.sec_user_agent
    assert updated.historical_years == settings.historical_years
    assert updated.projection_years == settings.projection_years
    assert updated.source_path == settings.source_path


def test_with_overrides_no_arguments_returns_equivalent_settings(tmp_config_file):
    path = tmp_config_file()
    settings = load_settings(config_path=path)

    updated = settings.with_overrides()

    assert updated == settings
