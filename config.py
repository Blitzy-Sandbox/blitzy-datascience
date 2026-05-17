"""Central configuration module for the NBA Data Ingestion Pipeline.

This module implements Feature F-002 (Central Configuration) — the single
source of truth for every tunable in the pipeline. Every module in the
codebase (except ``utils/schema_normalizer.py``, which is a pure
transformation) imports from here. This is the write-site for every
constant traced by Validation Gate 12 (Config Propagation Tracing).

Design contract
---------------
* Pure declarations. No intra-project imports. Only standard-library imports
  are permitted (``pathlib``, ``typing``, ``os``).
* No side effects at module load time. ``ensure_directories()`` MUST be
  invoked explicitly by a consumer (logger/writer) so that test fixtures
  using ``tmp_path`` can monkeypatch the paths first.
* No logging inside this module — that would create a circular import with
  ``utils/logger.py``.
* Every constant is annotated with ``typing.Final[...]`` to signal
  immutability to linters and type-checkers.
* All constants may be overridden at runtime via the ``NBA_*`` environment
  variable family; env-vars are read once at module load.

Exported constants
------------------
Upstream API
    ``API_BASE_URL``                    Upstream NBA Stats API base URL.
    ``REQUEST_TIMEOUT_SECONDS``         Per-request HTTP timeout (seconds).

Required request headers (Rule 3)
    ``REQUIRED_HEADERS``                Dict of HTTP headers attached to every
                                        outbound request. Must include
                                        ``Referer`` and a browser-like
                                        ``User-Agent``.

Rate limiting (Rule 2)
    ``RATE_LIMIT_SECONDS``              Minimum inter-request delay. MUST
                                        remain >= 1.0 seconds.

Retry parameters (Feature F-004)
    ``RETRY_ATTEMPTS``                  Tenacity stop-after-attempt count.
    ``RETRY_MULTIPLIER``                Exponential back-off multiplier.
    ``RETRY_MAX_WAIT``                  Exponential back-off ceiling (seconds).
    ``RETRY_MIN_WAIT``                  Exponential back-off floor (seconds).

Filesystem paths
    ``OUTPUT_DIR``                      Directory for flat CSV artifacts.
    ``CHECKPOINT_PATH``                 JSON manifest tracking completed pulls.
    ``LOG_DIR``                         Directory for rotating log files.
    ``LOG_FILE``                        Primary log sink (rotating handler).

Log configuration
    ``LOG_LEVEL``                       Root logger level.
    ``LOG_FORMAT``                      ``logging.Formatter`` format string;
                                        embeds ``%(correlation_id)s`` which is
                                        injected by ``utils/correlation.py``.
    ``LOG_DATE_FORMAT``                 ``logging.Formatter`` date format.
    ``LOG_FILE_MAX_BYTES``              Rotation size threshold (bytes).
    ``LOG_FILE_BACKUP_COUNT``           Number of rotation files retained.

Season defaults
    ``DEFAULT_SEASON``                  Default season string (``"2025-26"``).
    ``DEFAULT_SEASON_TYPE``             Default season type (``"Regular Season"``).
    ``DEFAULT_LEAGUE_ID``               NBA League ID (``"00"``).
    ``SEASONS``                         List of seasons for potential backfills.

CSV output artifact names
    ``CSV_PLAYERS``, ``CSV_PLAYER_TRACKING``, ``CSV_TEAMS``, ``CSV_GAMES``,
    ``CSV_PLAY_BY_PLAY``, ``CSV_LINEUPS``, ``CSV_SCHEDULE``
    Canonical stem names passed to ``BaseWriter.write(..., name=...)``.

Checkpoint domain keys
    ``DOMAIN_PLAYERS``, ``DOMAIN_TEAMS``, ``DOMAIN_GAMES``,
    ``DOMAIN_LINEUPS``, ``DOMAIN_SCHEDULE``
    Canonical string literals passed to
    ``CheckpointManager.mark_completed(domain=..., key=...)``.

Critical invariants
-------------------
* **Rule 2 floor** — ``RATE_LIMIT_SECONDS`` MUST remain ``>= 1.0``. The
  default is ``1.0``. Operators may override via ``NBA_RATE_LIMIT_SECONDS``
  but MUST NOT reduce below the floor. Defense-in-depth validation lives in
  ``utils/rate_limiter.py``.
* **Rule 3 required headers** — ``REQUIRED_HEADERS`` MUST include, at a
  minimum, ``Referer`` and a browser-like ``User-Agent``. The additional
  headers (``Accept``, ``Accept-Language``, ``Origin``, ``Connection``,
  ``x-nba-stats-origin``, ``x-nba-stats-token``) are field-proven stabilisers
  for the live NBA Stats API; removing them risks intermittent HTTP 403 and
  429 responses.
* **Gate 12 (Config Propagation Tracing)** — every constant declared here
  MUST have at least one verified read-site reachable from ``run.py``. The
  authoritative read-site mapping is reproduced in the table below. Any
  addition to this module MUST also add a read-site in a consumer module.
* **No relative-path surprises** — ``OUTPUT_DIR`` and ``LOG_DIR`` default to
  *relative* paths. Consumers (``CSVWriter``, ``CheckpointManager``,
  ``utils/logger``) are expected to resolve them (e.g. via ``Path.resolve()``)
  so operators invoking the CLI from alternate working directories still get
  predictable behaviour.

Gate 12 read-site trace
-----------------------
===========================  ===================================================
Constant                     Representative read-site
===========================  ===================================================
``API_BASE_URL``             ``api/nba_client.py::NBAClient.get`` (builds URL)
``REQUIRED_HEADERS``         ``api/nba_client.py::NBAClient.__init__``
                             (assigns to ``session.headers``)
``REQUEST_TIMEOUT_SECONDS``  ``api/nba_client.py::NBAClient._request``
                             (``requests.get(..., timeout=...)``)
``RATE_LIMIT_SECONDS``       ``utils/rate_limiter.py::RateLimiter.wait``
``RETRY_ATTEMPTS``           ``api/nba_client.py`` tenacity decorator
``RETRY_MULTIPLIER``         ``api/nba_client.py`` tenacity decorator
``RETRY_MAX_WAIT``           ``api/nba_client.py`` tenacity decorator
``RETRY_MIN_WAIT``           ``api/nba_client.py`` tenacity decorator
``OUTPUT_DIR``               ``storage/csv_writer.py::CSVWriter.__init__``
``CHECKPOINT_PATH``          ``utils/checkpoint.py::CheckpointManager.__init__``
``LOG_DIR``                  ``utils/logger.py::get_logger``
``LOG_FILE``                 ``utils/logger.py::get_logger``
``LOG_LEVEL``                ``utils/logger.py::get_logger``
``LOG_FORMAT``               ``utils/logger.py::get_logger``
``LOG_DATE_FORMAT``          ``utils/logger.py::get_logger``
``LOG_FILE_MAX_BYTES``       ``utils/logger.py::get_logger``
``LOG_FILE_BACKUP_COUNT``    ``utils/logger.py::get_logger``
``DEFAULT_SEASON``           ``run.py`` (Click ``--season`` default on every
                             subcommand)
``DEFAULT_SEASON_TYPE``      ``endpoints/*.py`` (default param on every wrapper)
``DEFAULT_LEAGUE_ID``        ``endpoints/*.py`` (default param on every wrapper)
``SEASONS``                  ``docs/ONBOARDING.md`` references; future backfill
                             iteration in ``pipelines/*.py``
``CSV_*``                    ``pipelines/ingest_*.py`` (``name=`` argument to
                             ``writer.write``)
``DOMAIN_*``                 ``pipelines/ingest_*.py`` (``domain=`` argument to
                             ``checkpoint.mark_completed``)
===========================  ===================================================
"""
import os
from pathlib import Path
from typing import Dict, Final, List

# ---------------------------------------------------------------------------
# Internal helpers — env-var override surface
# ---------------------------------------------------------------------------
#
# Every constant below is declared with ``Final[...]`` and permits an
# environment-variable override. Overrides are read at module load time.
# Changing an environment variable after ``config`` has been imported has no
# effect — this is documented behaviour, not a bug.


def _env(key: str, default: str) -> str:
    """Return the environment override for ``key`` or the literal ``default``.

    Empty strings are treated as explicit overrides and returned as-is. Only
    an *absent* variable falls back to ``default``.
    """
    return os.environ.get(key, default)


def _env_path(key: str, default: str) -> Path:
    """Return a :class:`pathlib.Path` override for ``key`` or ``default``."""
    return Path(_env(key, default))


def _env_float(key: str, default: float) -> float:
    """Return a ``float`` override for ``key`` or ``default``.

    Raises ``ValueError`` if the environment variable is set but cannot be
    parsed as a float — a deliberate fail-fast behaviour so misconfiguration
    surfaces immediately at import time.
    """
    raw = os.environ.get(key)
    return float(raw) if raw is not None else default


def _env_int(key: str, default: int) -> int:
    """Return an ``int`` override for ``key`` or ``default``.

    Raises ``ValueError`` if the environment variable is set but cannot be
    parsed as an integer — a deliberate fail-fast behaviour so
    misconfiguration surfaces immediately at import time.
    """
    raw = os.environ.get(key)
    return int(raw) if raw is not None else default


# ---------------------------------------------------------------------------
# 3.1 Upstream API
# ---------------------------------------------------------------------------
API_BASE_URL: Final[str] = _env("NBA_API_BASE_URL", "https://stats.nba.com/stats/")
REQUEST_TIMEOUT_SECONDS: Final[int] = _env_int("NBA_REQUEST_TIMEOUT_SECONDS", 30)


# ---------------------------------------------------------------------------
# 3.2 Required request headers (Rule 3)
# ---------------------------------------------------------------------------
# Rule 3 requires ``Referer`` and a browser-like ``User-Agent`` on every
# outbound request. The additional headers below are field-proven stabilisers
# for the live NBA Stats API; they reduce 403/429 rates. If Gate 1 fails with
# HTTP 403, temporarily reduce to just ``Referer`` + ``User-Agent`` to isolate
# the cause, but NEVER drop those two minimum headers.
REQUIRED_HEADERS: Final[Dict[str, str]] = {
    "Referer": "https://stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://stats.nba.com",
    "Connection": "keep-alive",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


# ---------------------------------------------------------------------------
# 3.3 Rate limiting (Rule 2)
# ---------------------------------------------------------------------------
# Rule 2 floor — DO NOT reduce below 1.0.
RATE_LIMIT_SECONDS: Final[float] = _env_float("NBA_RATE_LIMIT_SECONDS", 1.0)


# ---------------------------------------------------------------------------
# 3.4 Retry parameters (Feature F-004)
# ---------------------------------------------------------------------------
RETRY_ATTEMPTS: Final[int] = _env_int("NBA_RETRY_ATTEMPTS", 5)
RETRY_MULTIPLIER: Final[int] = _env_int("NBA_RETRY_MULTIPLIER", 2)
RETRY_MAX_WAIT: Final[int] = _env_int("NBA_RETRY_MAX_WAIT", 60)
RETRY_MIN_WAIT: Final[int] = _env_int("NBA_RETRY_MIN_WAIT", 1)


# ---------------------------------------------------------------------------
# 3.5 Filesystem paths
# ---------------------------------------------------------------------------
# Defaults are deliberately *relative*. Consumers (``CSVWriter``,
# ``CheckpointManager``, ``utils/logger``) resolve them before writing so
# invocations from alternate working directories behave predictably.
OUTPUT_DIR: Final[Path] = _env_path("NBA_OUTPUT_DIR", "output")
CHECKPOINT_PATH: Final[Path] = _env_path("NBA_CHECKPOINT_PATH", "output/checkpoint.json")
LOG_DIR: Final[Path] = _env_path("NBA_LOG_DIR", "logs")
LOG_FILE: Final[Path] = _env_path("NBA_LOG_FILE", "logs/pipeline.log")


# ---------------------------------------------------------------------------
# 3.6 Log configuration
# ---------------------------------------------------------------------------
# ``LOG_FORMAT`` embeds ``%(correlation_id)s``. The ``LoggerAdapter`` in
# ``utils/correlation.py`` is responsible for injecting this key into every
# ``LogRecord``; without that adapter, ``logging.Formatter`` raises
# ``KeyError``. The dependency between ``LOG_FORMAT`` and
# ``utils/correlation.py`` MUST be preserved.
LOG_LEVEL: Final[str] = _env("NBA_LOG_LEVEL", "INFO")
LOG_FORMAT: Final[str] = (
    "%(asctime)s %(levelname)s corr=%(correlation_id)s %(name)s %(message)s"
)
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"
LOG_FILE_MAX_BYTES: Final[int] = _env_int("NBA_LOG_FILE_MAX_BYTES", 10_485_760)  # 10 MB
LOG_FILE_BACKUP_COUNT: Final[int] = _env_int("NBA_LOG_FILE_BACKUP_COUNT", 5)


# ---------------------------------------------------------------------------
# 3.7 Season defaults
# ---------------------------------------------------------------------------
DEFAULT_SEASON: Final[str] = _env("NBA_DEFAULT_SEASON", "2025-26")
DEFAULT_SEASON_TYPE: Final[str] = _env("NBA_DEFAULT_SEASON_TYPE", "Regular Season")
DEFAULT_LEAGUE_ID: Final[str] = _env("NBA_DEFAULT_LEAGUE_ID", "00")
# ``SEASONS`` is exposed for future multi-season backfill iteration (deferred
# to a future phase per Technical Specification §1.3). The default single-
# season path runs with ``DEFAULT_SEASON``.
SEASONS: Final[List[str]] = [
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
]


# ---------------------------------------------------------------------------
# 3.8 CSV output artifact names
# ---------------------------------------------------------------------------
# Canonical stem names. Passed to ``BaseWriter.write(df, name=..., season=...)``
# by every pipeline. The writer appends ``.csv`` and places the file under
# ``OUTPUT_DIR``.
CSV_PLAYERS: Final[str] = "players"
CSV_PLAYER_TRACKING: Final[str] = "player_tracking"
CSV_TEAMS: Final[str] = "teams"
CSV_GAMES: Final[str] = "games"
CSV_PLAY_BY_PLAY: Final[str] = "play_by_play"
CSV_LINEUPS: Final[str] = "lineups"
CSV_SCHEDULE: Final[str] = "schedule"


# ---------------------------------------------------------------------------
# 3.9 Checkpoint domain keys
# ---------------------------------------------------------------------------
# Canonical string literals. Passed to
# ``CheckpointManager.mark_completed(domain=..., key=...)``. Using these named
# constants (rather than raw strings) keeps Rule 5 checkpoint keys consistent
# across every pipeline and makes refactors safe.
DOMAIN_PLAYERS: Final[str] = "players"
DOMAIN_TEAMS: Final[str] = "teams"
DOMAIN_GAMES: Final[str] = "games"
DOMAIN_LINEUPS: Final[str] = "lineups"
DOMAIN_SCHEDULE: Final[str] = "schedule"


# ---------------------------------------------------------------------------
# Directory creation helper
# ---------------------------------------------------------------------------
def ensure_directories() -> None:
    """Create :data:`OUTPUT_DIR` and :data:`LOG_DIR` if they do not exist.

    Idempotent. Invoked by ``utils/logger.get_logger`` before configuring the
    rotating file handler, by ``storage/csv_writer.CSVWriter.__init__``
    before writing any CSV, and by ``utils/health.check_readiness`` as part
    of the readiness probe.

    This function is deliberately NOT called at module-load time so that
    test fixtures using ``tmp_path`` can monkeypatch ``OUTPUT_DIR`` and
    ``LOG_DIR`` before directories are materialised on disk.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
