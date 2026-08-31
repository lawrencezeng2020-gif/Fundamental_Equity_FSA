"""Settings, paths, User-Agent, and defaults.

This module is NOT a stub -- it is a fully working Phase 0 deliverable. It loads
local, gitignored configuration (never hardcoded secrets/identity) and resolves
the runtime settings every later phase depends on: cache dir, output dir,
historical/projection window defaults, rate limit, cache TTL, and the SEC
User-Agent string required by PLANNING.md Section 4.

Config file resolution order (first found wins):
    1. ``.fsa.toml`` in the repository root (gitignored; see ``.fsa.toml.example``)
    2. ``~/.fsa/config.toml``

If neither exists, or the required ``sec_user_agent`` key is missing/invalid,
loading raises :class:`ConfigError` with an actionable message -- never a bare
traceback and never a hardcoded fallback identity, per CLAUDE.md rule 5 / 8.
"""

from __future__ import annotations

import re
import sys
import dataclasses
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project requires >=3.13
    import tomli as tomllib  # type: ignore[no-redef]

# Repository root: three levels up from this file (src/fsa/config.py -> src/fsa -> src -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]

PROJECT_CONFIG_PATH = _REPO_ROOT / ".fsa.toml"
USER_CONFIG_PATH = Path.home() / ".fsa" / "config.toml"

# SEC wants "Name email@domain" -- a permissive check, not full RFC 5322 validation.
_USER_AGENT_RE = re.compile(r"^\S.*\s\S+@\S+\.\S+$")

DEFAULT_CACHE_DIR = _REPO_ROOT / ".cache"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "FSA_Output"
DEFAULT_HISTORICAL_YEARS = 10
DEFAULT_PROJECTION_YEARS = 5
DEFAULT_RATE_LIMIT_RPS = 5
DEFAULT_CACHE_TTL_HOURS = 24


class ConfigError(Exception):
    """Raised when configuration is missing or invalid.

    Callers (notably ``cli.py``) should catch this and print ``str(exc)``
    directly rather than letting a traceback surface -- the message is
    written to already be actionable.
    """


@dataclass(frozen=True)
class Settings:
    """Fully resolved runtime configuration.

    All paths are absolute. Loading configuration (:func:`load_settings`) is
    side-effect free -- it does not touch the filesystem beyond reading the
    config file itself. ``output_dir`` is created on demand by
    :func:`ensure_output_dir`, called once any CLI overrides (e.g. ``--out``)
    have been applied via :meth:`with_overrides`, so the directory actually
    created matches what the run will use.
    """

    sec_user_agent: str
    cache_dir: Path
    output_dir: Path
    historical_years: int
    projection_years: int
    rate_limit_rps: float
    cache_ttl_hours: float
    source_path: Path

    def as_display_dict(self) -> dict[str, str]:
        """Render settings for human display (e.g. CLI --ticker run summary)."""
        return {
            "sec_user_agent": self.sec_user_agent,
            "cache_dir": str(self.cache_dir),
            "output_dir": str(self.output_dir),
            "historical_years": str(self.historical_years),
            "projection_years": str(self.projection_years),
            "rate_limit_rps": str(self.rate_limit_rps),
            "cache_ttl_hours": str(self.cache_ttl_hours),
            "config_source": str(self.source_path),
        }

    def with_overrides(
        self,
        *,
        output_dir: Path | None = None,
        historical_years: int | None = None,
        projection_years: int | None = None,
    ) -> "Settings":
        """Return a copy with any provided overrides applied.

        A parameter left as ``None`` means "no override" -- keep the current
        value. This mirrors how the CLI's optional flags behave (they default
        to ``None`` when the user doesn't pass them). Used by ``cli.py`` to
        fold ``--out`` / ``--historical-years`` / ``--projection-years`` into
        a single authoritative ``Settings`` object before anything downstream
        (including :func:`ensure_output_dir`) reads it, so there is exactly
        one source of truth for these values, not a config value and a
        separately-tracked CLI override.
        """
        changes: dict[str, object] = {}
        if output_dir is not None:
            changes["output_dir"] = output_dir
        if historical_years is not None:
            changes["historical_years"] = historical_years
        if projection_years is not None:
            changes["projection_years"] = projection_years
        return dataclasses.replace(self, **changes) if changes else self


def _find_config_file() -> Path:
    if PROJECT_CONFIG_PATH.is_file():
        return PROJECT_CONFIG_PATH
    if USER_CONFIG_PATH.is_file():
        return USER_CONFIG_PATH
    raise ConfigError(
        "No FSA configuration file found.\n\n"
        f"Create one of:\n"
        f"  - {PROJECT_CONFIG_PATH}\n"
        f"  - {USER_CONFIG_PATH}\n\n"
        f"The quickest way: copy the committed example and edit it:\n"
        f"  cp {PROJECT_CONFIG_PATH}.example {PROJECT_CONFIG_PATH}\n\n"
        "At minimum it must set:\n"
        '  sec_user_agent = "Your Name your.email@example.com"\n\n'
        "SEC EDGAR returns HTTP 403 on every request without a User-Agent in this "
        "format. This value is never committed to version control."
    )


def _validate_user_agent(value: object, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"'sec_user_agent' is missing or empty in {source}.\n\n"
            'Set it to: sec_user_agent = "Your Name your.email@example.com"\n'
            "SEC EDGAR requires a User-Agent of the form 'Name email@domain' and "
            "returns HTTP 403 without one."
        )
    value = value.strip()
    if not _USER_AGENT_RE.match(value):
        raise ConfigError(
            f"'sec_user_agent' in {source} does not look like 'Name email@domain': "
            f"{value!r}\n\n"
            'Example: sec_user_agent = "Jane Doe jane.doe@example.com"'
        )
    return value


def _coerce_positive_int(value: object, key: str, default: int, source: Path) -> int:
    if value is None:
        return default
    try:
        ivalue = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' in {source} must be an integer, got {value!r}.") from exc
    if ivalue <= 0:
        raise ConfigError(f"'{key}' in {source} must be a positive integer, got {ivalue}.")
    return ivalue


def _coerce_positive_number(value: object, key: str, default: float, source: Path) -> float:
    if value is None:
        return default
    try:
        fvalue = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' in {source} must be a number, got {value!r}.") from exc
    if fvalue <= 0:
        raise ConfigError(f"'{key}' in {source} must be positive, got {fvalue}.")
    return fvalue


def _expand_path(raw: object, default: Path, source: Path, key: str) -> Path:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"'{key}' in {source} must be a non-empty string path, got {raw!r}.")
    return Path(raw).expanduser().resolve()


def load_settings(config_path: Path | None = None) -> Settings:
    """Load and validate settings.

    Args:
        config_path: Explicit config file to load, bypassing the normal
            project-then-home search. Primarily for tests.

    Raises:
        ConfigError: on a missing file, missing/invalid ``sec_user_agent``,
            or a malformed value for any other key. The message is written
            to be printed directly to the user.
    """
    path = config_path if config_path is not None else _find_config_file()

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid TOML: {exc}") from exc

    sec_user_agent = _validate_user_agent(data.get("sec_user_agent"), path)

    cache_dir = _expand_path(data.get("cache_dir"), DEFAULT_CACHE_DIR, path, "cache_dir")
    output_dir = _expand_path(data.get("output_dir"), DEFAULT_OUTPUT_DIR, path, "output_dir")

    historical_years = _coerce_positive_int(
        data.get("historical_years"), "historical_years", DEFAULT_HISTORICAL_YEARS, path
    )
    projection_years = _coerce_positive_int(
        data.get("projection_years"), "projection_years", DEFAULT_PROJECTION_YEARS, path
    )
    rate_limit_rps = _coerce_positive_number(
        data.get("rate_limit_rps"), "rate_limit_rps", DEFAULT_RATE_LIMIT_RPS, path
    )
    if rate_limit_rps > 10:
        raise ConfigError(
            f"'rate_limit_rps' in {path} is {rate_limit_rps}, which exceeds SEC's own "
            "10 req/s policy limit. PLANNING.md Section 4 caps this tool at 5 req/s "
            "as a safety margin -- lower the value."
        )
    cache_ttl_hours = _coerce_positive_number(
        data.get("cache_ttl_hours"), "cache_ttl_hours", DEFAULT_CACHE_TTL_HOURS, path
    )

    # NOTE: no filesystem writes here. Loading configuration must be side-effect
    # free -- directory creation happens later, via ensure_output_dir(), once any
    # CLI overrides (e.g. --out) have been folded in via Settings.with_overrides().
    # See ensure_output_dir() below.

    return Settings(
        sec_user_agent=sec_user_agent,
        cache_dir=cache_dir,
        output_dir=output_dir,
        historical_years=historical_years,
        projection_years=projection_years,
        rate_limit_rps=rate_limit_rps,
        cache_ttl_hours=cache_ttl_hours,
        source_path=path,
    )


def ensure_output_dir(settings: Settings) -> None:
    """Create ``settings.output_dir`` if it does not already exist.

    Deliberately NOT part of :func:`load_settings` -- loading configuration
    must be side-effect free, and the directory that should actually get
    created is whichever one survives CLI override resolution (see
    :meth:`Settings.with_overrides`), not necessarily the config-file
    default. Call this only once the final, override-applied ``Settings`` is
    known. Phase 5 will call it immediately before writing the workbook; the
    Phase 0 CLI calls it right after override resolution so this error path
    stays exercised even though no workbook is written yet.

    Raises:
        ConfigError: if the directory cannot be created -- permission
            denied, a read-only filesystem, an unmounted volume, etc. This is
            not exotic: it is especially plausible for a path under
            ``~/Documents``, since (per the Phase 0 sandbox spike, see
            spike/README.md) Excel for Mac's own sandboxed view of
            "Documents" is a separate, private directory from the real one --
            the same class of filesystem surprise. The message names the
            offending path, the responsible setting, and where it came from,
            so this never surfaces as a bare traceback.
    """
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"Could not create output directory: {settings.output_dir}\n\n"
            f"Reason: {exc}\n\n"
            "This comes from the 'output_dir' setting "
            f"(config file: {settings.source_path}, or a --out override on the "
            "command line). Choose a location you have write access to -- e.g. "
            "pass --out /some/writable/dir, or edit 'output_dir' in your config "
            "file."
        ) from exc
