"""Shared pytest fixtures and lightweight test doubles for the NBA pipeline suite.

This module is pytest's canonical shared-fixture file for the entire
``tests/`` tree. It exists to satisfy the checkpoint scope note in the QA
report (Issue #2) and the contract published by ``tests/__init__.py``:
all behavioural unit tests for ``api/``, ``storage/``, ``pipelines/``,
``endpoints/``, and ``utils/`` obtain their collaborators, payloads,
clock, and test harnesses from here.

The fixtures below fall into four functional groups:

1. **Autouse resets** (implicit) -- ensure that mutable module-level state
   (the ``correlation_id`` :class:`~contextvars.ContextVar`, the
   :data:`utils.metrics.registry` singleton, and the root logger handler
   list) is restored to a pristine state between tests so that the order
   in which tests execute cannot influence their outcome. The logger
   reset is deliberately *caplog-safe*: it removes only handlers owned
   by this project while preserving the handlers pytest installs under
   the ``_pytest.logging`` module so that ``caplog`` and
   ``--log-cli-level`` continue to function.

2. **Test doubles** (``FakeClock``, ``RecordingClient``,
   ``RecordingWriter``, ``RecordingCheckpoint``) -- handwritten spies that
   record every interaction for later assertion. They replace the need
   for ``unittest.mock.MagicMock`` in common cases and make failing-test
   diagnostics easier to read because every attribute is explicitly
   documented.

3. **Data fixtures** (``flat_df``, ``nested_df``, ``list_cell_df``,
   ``empty_df``, ``resultset_players``, ``resultset_empty``,
   ``resultset_multi``) -- deterministic, scalar-only (or deliberately
   pathological) :class:`pandas.DataFrame` and ``resultSets`` envelope
   values that every test can reference without reconstruction.

4. **Environment fixtures** (``tmp_output_dir``, ``csv_reader``,
   ``rate_limiter_factory``, ``cli_runner``) -- parameterise the writer
   and rate-limiter under test and provide a handy UTF-8 CSV reader and
   a :class:`click.testing.CliRunner` factory for CLI-level tests added
   in later checkpoints.

References
----------
* AAP section 0.5.1.8 -- tests mirror the production module tree one-to-one.
* AAP sections 0.7.2.1-0.7.2.7 -- Operational Rules 1-7 verified by the tests
  that consume these fixtures.
* AAP section 0.5.2.1 -- retry predicate contract (``_is_transient``) exercised
  by ``tests/unit/api/test_nba_client.py``.
* QA Report Issue #2 -- this file and its sibling ``test_*.py`` modules
  are the concrete deliverables that unblock Gate 10 for Checkpoint IC-2.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
#
# All fixtures and test doubles defined below rely only on the stdlib,
# ``pandas`` (a production-pinned dependency), and a handful of symbols
# re-exported by our own ``api`` / ``storage`` / ``utils`` packages. We
# deliberately DO NOT import ``requests`` here even though some of the
# recording spies imitate ``requests.Response`` -- that would double as a
# stealth Rule 1 violation inside the test suite. Instead, the exception
# types we need (``HTTPError``, ``Timeout``, ``RequestsConnectionError``)
# are consumed from ``api.nba_client`` where they are already re-exported.
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import pandas as pd
import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# First-party imports
# ---------------------------------------------------------------------------
#
# We intentionally import the production modules so that the fixtures
# exercise the REAL registry / config / logger / correlation objects
# that production code will see at runtime. Tests that need isolation
# from these singletons rely on the autouse reset fixtures below rather
# than on module reloads (which would break cross-test object identity
# on imports made at module-import time elsewhere in the tree).
import config
from utils import logger as _logger_module
from utils import metrics as _metrics_module
from utils.correlation import correlation_id


# ===========================================================================
# Autouse reset fixtures
# ===========================================================================
#
# Each fixture below is decorated with ``autouse=True`` so pytest invokes
# it for every test in the session without the test having to opt in.
# They execute in the declaration order of this file; callers of
# ``setup``/``teardown`` style are therefore guaranteed the following
# guarantee sequence at the START of every test:
#
#   1. correlation_id reset to "" via the raw ContextVar setter
#   2. metrics registry reset to zero counters / histograms
#   3. application logger handlers removed (pytest handlers preserved)
#
# and the MIRROR sequence on teardown (same three operations, since the
# fixture bodies yield only once).


@pytest.fixture(autouse=True)
def _reset_correlation_id() -> Iterator[None]:
    """Reset the ``correlation_id`` ContextVar to its default empty value.

    The public helper :func:`utils.correlation.set_correlation_id` mints a
    fresh UUID when called with an empty string (line 194 of
    ``utils/correlation.py``: ``cid = value or new_correlation_id()``).
    That is exactly wrong for a test reset. We therefore use the raw
    :class:`contextvars.ContextVar` setter with the empty-string sentinel
    that matches the ContextVar's declared default value.

    Yields
    ------
    None
        Execution returns control to the test, then the fixture resets
        the ContextVar once more on teardown as a belt-and-braces guard
        against teardown-time assertions that depend on a pristine CID.
    """
    correlation_id.set("")
    try:
        yield
    finally:
        correlation_id.set("")


@pytest.fixture(autouse=True)
def _reset_metrics_registry() -> Iterator[None]:
    """Zero all counter / histogram values on ``utils.metrics.registry``.

    The :class:`utils.metrics.MetricsRegistry` is a process-wide
    singleton. Without a per-test reset, assertions such as
    ``get_counter_value('nba_requests_total') == 1`` would observe
    cumulative totals across the whole test session.

    ``registry.reset()`` is the public API for this use case; it clears
    all counter values and histogram buckets while preserving the
    registry's *registration* metadata (``# HELP`` / ``# TYPE`` lines).
    """
    _metrics_module.registry.reset()
    try:
        yield
    finally:
        _metrics_module.registry.reset()


@pytest.fixture(autouse=True)
def _reset_logger_handlers() -> Iterator[None]:
    """Remove this project's root-logger handlers; leave pytest's in place.

    Background
    ----------
    :func:`utils.logger.get_logger` guards its expensive handler setup
    with a module-level ``_configured`` flag: the first call attaches a
    :class:`logging.StreamHandler` and a
    :class:`logging.handlers.RotatingFileHandler`, and every subsequent
    call is a short-circuit. That is correct behaviour at runtime, but
    in a test session multiple tests construct :class:`NBAClient` or
    :class:`CSVWriter` instances in quick succession and the guard means
    only the FIRST test sees the real configuration path. Later tests
    would then fail to exercise the format string, the rotating-file
    plumbing, or any assertions that depend on the handler list.

    We therefore flip ``_configured`` back to ``False`` and strip our own
    handlers between tests so every test gets a pristine configuration.

    caplog safety
    -------------
    pytest installs its own handlers on the root logger BEFORE any test
    runs (``_LiveLoggingNullHandler``, ``_FileHandler``, and two
    ``LogCaptureHandler`` instances). Those handlers are the mechanism
    by which the ``caplog`` fixture captures records. Removing them
    would silently break every ``caplog``-based assertion in the suite.

    We discriminate between project-owned handlers and pytest-owned
    handlers by inspecting the handler class's ``__module__``. Every
    handler installed by pytest lives under the ``_pytest`` package;
    every handler installed by :mod:`utils.logger` lives under the
    stdlib ``logging`` / ``logging.handlers`` namespace. Filtering
    ``type(h).__module__.startswith("_pytest")`` therefore preserves the
    caplog machinery while still allowing us to clear the project's
    own handlers.
    """
    def _strip_non_pytest_handlers() -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if type(handler).__module__.startswith("_pytest"):
                # Preserve pytest's caplog / live-log / file handlers.
                continue
            root.removeHandler(handler)
            # ``close()`` releases underlying file descriptors for the
            # rotating file handler. Swallow any error because a
            # well-behaved test teardown must never raise.
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass
        _logger_module._configured = False

    _strip_non_pytest_handlers()
    try:
        yield
    finally:
        _strip_non_pytest_handlers()


# ===========================================================================
# Deterministic clock for rate-limiter tests
# ===========================================================================


class FakeClock:
    """Deterministic monotonic clock with an explicit sleeper callback.

    Parameters
    ----------
    start : float, optional
        Initial value returned by subsequent calls to the instance's
        ``monotonic`` method. Defaults to ``0.0``.

    Attributes
    ----------
    now : float
        The current clock reading. Advanced either manually via
        :meth:`advance` or automatically when :meth:`sleep` is called.
    sleeps : list[float]
        Ordered list of every sleep duration requested via
        :meth:`sleep`. Tests inspect this to verify Rule 2 compliance
        (``sum(sleeps) >= RATE_LIMIT_SECONDS * (N - 1)``).

    Notes
    -----
    A ``FakeClock`` is used in two distinct roles by the rate-limiter
    fixture:

    * ``clock=fake.monotonic`` replaces :func:`time.monotonic` so the
      ``RateLimiter`` sees a controlled, deterministic clock.
    * ``sleeper=fake.sleep`` replaces :func:`time.sleep` so the test
      never actually blocks -- the callback records the requested
      duration and instantly advances ``now`` by that amount.

    This pair turns a real-wall-clock rate-limiter test into a
    millisecond-fast deterministic one while still exercising the exact
    same production code path as runtime would.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now: float = float(start)
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        """Return the current synthetic clock value (non-decreasing)."""
        return self.now

    def sleep(self, duration: float) -> None:
        """Record ``duration`` and advance the clock by the same amount."""
        # Production ``time.sleep`` accepts 0 and negative values without
        # blocking; we replicate that contract so the rate-limiter's
        # "no-op when interval has elapsed" branch is exercised too.
        self.sleeps.append(float(duration))
        if duration > 0:
            self.now += float(duration)

    def advance(self, delta: float) -> None:
        """Advance the clock by ``delta`` seconds without recording a sleep."""
        if delta < 0:
            raise ValueError("FakeClock.advance requires a non-negative delta")
        self.now += float(delta)


@pytest.fixture
def fake_clock() -> FakeClock:
    """A fresh :class:`FakeClock` starting at ``t=0``."""
    return FakeClock(start=0.0)


@pytest.fixture
def rate_limiter_factory(
    fake_clock: FakeClock,
) -> Callable[..., Any]:
    """Factory producing :class:`utils.rate_limiter.RateLimiter` instances.

    The returned callable defaults to the Rule 2 floor (1.0s) and wires
    :class:`FakeClock` in as both the clock and sleeper. Tests that want
    a different interval pass ``interval=2.5`` (etc.).
    """
    # Local import to avoid a top-level dependency on a production module
    # before it has been validated by earlier collection steps.
    from utils.rate_limiter import RateLimiter

    def _make(interval: Optional[float] = None) -> Any:
        # ``RateLimiter.__init__`` signature (AAP-verified):
        #   (self, min_interval_seconds=None, *, clock=..., sleeper=...)
        return RateLimiter(
            interval if interval is not None else RateLimiter.RULE2_FLOOR,
            clock=fake_clock.monotonic,
            sleeper=fake_clock.sleep,
        )

    return _make


# ===========================================================================
# Recording test doubles (handwritten spies)
# ===========================================================================


class RecordingClient:
    """Spy replacement for :class:`api.nba_client.NBAClient`.

    The production ``NBAClient.get(endpoint, params) -> dict`` contract is
    reproduced exactly: each call records its arguments and returns a
    deterministic value chosen from the configured queue of responses.

    Parameters
    ----------
    responses : dict[str, list[dict]] or dict[str, dict], optional
        Mapping of endpoint name to either a single response dict (which
        will be returned on every call) or a list of responses consumed
        in FIFO order. When a list is configured and exhausted, the next
        call raises :class:`AssertionError` -- a loud failure mode so
        tests cannot silently over-consume fixtures.
    failures : dict[str, list[BaseException]] or dict[str, BaseException]
        Mapping of endpoint name to exceptions that should be raised in
        place of a response. Exceptions are consumed in the same FIFO
        order as ``responses``; tests that mix response dicts and
        exceptions should supply both keys.

    Attributes
    ----------
    calls : list[tuple[str, dict]]
        Append-only list of every ``(endpoint, params)`` pair received
        by :meth:`get`. Tests assert on both length and content.
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
        failures: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._responses: Dict[str, List[Dict[str, Any]]] = {}
        self._failures: Dict[str, List[BaseException]] = {}
        if responses:
            for name, value in responses.items():
                # Normalise scalar -> single-element list so the queue
                # model is uniform across the whole double.
                self._responses[name] = (
                    list(value) if isinstance(value, list) else [value]
                )
        if failures:
            for name, value in failures.items():
                self._failures[name] = (
                    list(value) if isinstance(value, list) else [value]
                )
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Record the call and return / raise per the configured queue."""
        # Copy ``params`` to a new dict so mutations by the caller do not
        # retroactively alter the recorded arguments.
        self.calls.append((endpoint, dict(params)))
        if endpoint in self._failures and self._failures[endpoint]:
            raise self._failures[endpoint].pop(0)
        if endpoint in self._responses and self._responses[endpoint]:
            return self._responses[endpoint].pop(0)
        raise AssertionError(
            f"RecordingClient has no response configured for endpoint={endpoint!r}"
        )


class RecordingWriter:
    """Spy replacement for :class:`storage.csv_writer.CSVWriter`.

    Each call to :meth:`write` records the DataFrame (by reference), the
    logical ``name``, and the ``season`` so tests can assert on the
    sequence of writes a pipeline produced without actually touching
    the filesystem.

    Parameters
    ----------
    output_dir : pathlib.Path, optional
        Advertised output directory. The value is stored verbatim and
        returned as a synthesised ``<name>.csv`` path so callers get a
        :class:`~pathlib.Path` shape compatible with the real
        :meth:`CSVWriter.write`. Defaults to ``Path("/tmp/recording")``.

    Attributes
    ----------
    writes : list[dict]
        Append-only call log. Each entry has keys ``df`` (the DataFrame
        reference), ``name`` (artifact name), ``season`` (season string),
        and ``path`` (the synthesised return value).
    """

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self.output_dir: Path = (
            output_dir if output_dir is not None else Path("/tmp/recording")
        )
        self.writes: List[Dict[str, Any]] = []

    def write(self, df: pd.DataFrame, name: str, season: str) -> Path:
        """Record the write and return a synthesised artifact path."""
        target = self.output_dir / f"{name}.csv"
        self.writes.append(
            {
                "df": df,
                "name": name,
                "season": season,
                "path": target,
            }
        )
        return target


class RecordingCheckpoint:
    """Spy replacement for :class:`utils.checkpoint.CheckpointManager`.

    The production signatures are preserved exactly:

    * ``is_completed(domain: str, key: str) -> bool``
    * ``mark_completed(domain: str, key: str) -> None``
    * ``get_pending(domain: str, all_keys: Iterable[str]) -> list[str]``

    Completion state is held in an in-memory ``dict[str, set[str]]``
    keyed by domain name.

    Attributes
    ----------
    completions : list[tuple[str, str]]
        Append-only chronological log of every ``mark_completed`` call.
        Tests assert on ordering to verify Rule 5 (checkpoint immediately
        after every successful pull).
    """

    def __init__(self, initial: Optional[Dict[str, Iterable[str]]] = None) -> None:
        self._state: Dict[str, set] = {}
        if initial:
            for domain, keys in initial.items():
                self._state[domain] = set(keys)
        self.completions: List[Tuple[str, str]] = []

    def is_completed(self, domain: str, key: str) -> bool:
        """Return whether ``(domain, key)`` has been marked completed."""
        return key in self._state.get(domain, set())

    def mark_completed(self, domain: str, key: str) -> None:
        """Mark ``(domain, key)`` completed and record the call order."""
        self._state.setdefault(domain, set()).add(key)
        self.completions.append((domain, key))

    def get_pending(self, domain: str, all_keys: Iterable[str]) -> List[str]:
        """Return the subset of ``all_keys`` not yet completed in ``domain``."""
        done = self._state.get(domain, set())
        return [k for k in all_keys if k not in done]


@pytest.fixture
def recording_client() -> RecordingClient:
    """A :class:`RecordingClient` pre-loaded with no responses or failures."""
    return RecordingClient()


@pytest.fixture
def recording_writer(tmp_path: Path) -> RecordingWriter:
    """A :class:`RecordingWriter` rooted at an ephemeral ``tmp_path``."""
    return RecordingWriter(output_dir=tmp_path)


@pytest.fixture
def recording_checkpoint() -> RecordingCheckpoint:
    """A :class:`RecordingCheckpoint` with no pre-marked completions."""
    return RecordingCheckpoint()


# ===========================================================================
# DataFrame fixtures
# ===========================================================================


@pytest.fixture
def flat_df() -> pd.DataFrame:
    """A scalar-only :class:`pandas.DataFrame` typical of a normalised result.

    This DataFrame satisfies Rule 4 (no nested cells) and contains a
    mix of integer, float, string, and ``None`` values so tests exercise
    both the happy path of :meth:`CSVWriter.write` and the subtler
    null-handling behaviour.
    """
    return pd.DataFrame(
        {
            "PLAYER_ID": [203999, 1629029, 1628369],
            "PLAYER_NAME": ["Nikola Jokić", "Luka Dončić", "Jayson Tatum"],
            "PTS": [29.2, 33.9, 26.9],
            "AST": [9.8, 9.2, 4.9],
            "REB": [12.4, 9.2, 8.8],
            "TEAM_ABBREVIATION": ["DEN", "DAL", "BOS"],
            "NOTES": [None, None, None],
        }
    )


@pytest.fixture
def nested_df() -> pd.DataFrame:
    """A pathological DataFrame whose cells contain ``dict`` values.

    Used to verify Rule 4 defence-in-depth in :class:`CSVWriter`
    (``_assert_flat`` must raise :class:`ValueError` before ever
    calling :meth:`DataFrame.to_csv`).
    """
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2],
            # pandas holds arbitrary Python objects in object-dtype cells
            # without complaint -- the check is entirely the writer's
            # responsibility.
            "STATS": [{"PTS": 30, "AST": 8}, {"PTS": 20, "AST": 5}],
        }
    )


@pytest.fixture
def list_cell_df() -> pd.DataFrame:
    """A pathological DataFrame whose cells contain ``list`` values."""
    return pd.DataFrame(
        {
            "TEAM_ID": [1, 2, 3],
            "ROSTER": [[101, 102], [201, 202], [301, 302]],
        }
    )


@pytest.fixture
def empty_df() -> pd.DataFrame:
    """A zero-row DataFrame with declared columns.

    Exercises the ``df.empty`` short-circuit in ``_assert_flat`` as well
    as the header-only CSV emission path.
    """
    return pd.DataFrame(columns=["PLAYER_ID", "PLAYER_NAME", "PTS"])


@pytest.fixture
def large_df() -> pd.DataFrame:
    """A 1000-row x 50-column scalar DataFrame for stress-level tests."""
    import numpy as np  # local import: numpy is a pandas transitive dep

    n_rows, n_cols = 1000, 50
    rng = np.random.default_rng(seed=42)
    return pd.DataFrame(
        rng.integers(low=0, high=1_000_000, size=(n_rows, n_cols)),
        columns=[f"col_{i:02d}" for i in range(n_cols)],
    )


# ===========================================================================
# NBA Stats API resultSets envelope fixtures
# ===========================================================================


@pytest.fixture
def resultset_players() -> Dict[str, Any]:
    """A canonical ``resultSets`` envelope modelled on ``leaguedashplayerstats``.

    Single result set with three headers and three rows. Every cell is
    scalar so the envelope is directly normaliseable without Rule 4
    violation.
    """
    return {
        "resource": "leaguedashplayerstats",
        "parameters": {"Season": "2025-26", "SeasonType": "Regular Season"},
        "resultSets": [
            {
                "name": "LeagueDashPlayerStats",
                "headers": ["PLAYER_ID", "PLAYER_NAME", "PTS"],
                "rowSet": [
                    [203999, "Nikola Jokić", 29.2],
                    [1629029, "Luka Dončić", 33.9],
                    [1628369, "Jayson Tatum", 26.9],
                ],
            }
        ],
    }


@pytest.fixture
def resultset_empty() -> Dict[str, Any]:
    """A ``resultSets`` envelope with a valid header list but zero rows.

    Useful for verifying that pipelines handle "no data returned"
    gracefully without crashing on an empty ``rowSet``.
    """
    return {
        "resource": "leaguedashplayerstats",
        "parameters": {"Season": "2099-00"},
        "resultSets": [
            {
                "name": "LeagueDashPlayerStats",
                "headers": ["PLAYER_ID", "PLAYER_NAME", "PTS"],
                "rowSet": [],
            }
        ],
    }


@pytest.fixture
def resultset_multi() -> Dict[str, Any]:
    """A ``resultSets`` envelope containing TWO distinct result tables.

    Models the NBA Stats endpoints (e.g., ``boxscoretraditionalv2``) that
    return multiple parallel result sets in a single response. The
    normaliser is expected to flatten both into independent DataFrames.
    """
    return {
        "resource": "boxscoretraditionalv2",
        "parameters": {"GameID": "0022300001"},
        "resultSets": [
            {
                "name": "PlayerStats",
                "headers": ["PLAYER_ID", "PTS", "AST"],
                "rowSet": [
                    [203999, 30, 8],
                    [1629029, 28, 10],
                ],
            },
            {
                "name": "TeamStats",
                "headers": ["TEAM_ID", "PTS"],
                "rowSet": [
                    [1610612743, 112],
                    [1610612742, 108],
                ],
            },
        ],
    }


# ===========================================================================
# Filesystem fixtures
# ===========================================================================


@pytest.fixture
def tmp_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable tmp directory with ``config.OUTPUT_DIR`` redirected to it.

    Every test that writes CSVs should depend on this fixture rather
    than on raw ``tmp_path`` so the default :class:`CSVWriter` (which
    reads :data:`config.OUTPUT_DIR` on construction) naturally targets
    the temporary directory.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Built-in pytest fixture supplying a unique per-test directory.
    monkeypatch : pytest.MonkeyPatch
        Built-in pytest fixture used to restore :data:`config.OUTPUT_DIR`
        when the test completes.

    Returns
    -------
    pathlib.Path
        The same :class:`~pathlib.Path` that ``config.OUTPUT_DIR`` now
        resolves to for the duration of the test.
    """
    # Redirect config.OUTPUT_DIR for the duration of the test. Pytest's
    # monkeypatch will automatically restore the original attribute on
    # teardown.
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        config, "CHECKPOINT_PATH", output_dir / "checkpoint.json"
    )
    return output_dir


@pytest.fixture
def csv_reader() -> Callable[[Path], pd.DataFrame]:
    """Factory returning a UTF-8-aware CSV reader.

    Centralising the reader keeps every ``assert_frame_equal`` call on
    the same reading semantics (``dtype=str``-free, no separators
    overridden, utf-8 encoding explicit). Tests that need different
    semantics construct their own :func:`pandas.read_csv` call.
    """

    def _read(path: Path) -> pd.DataFrame:
        return pd.read_csv(path, encoding="utf-8")

    return _read


# ===========================================================================
# CLI harness
# ===========================================================================


@pytest.fixture
def cli_runner() -> CliRunner:
    """A fresh :class:`click.testing.CliRunner`.

    The click 8.3.2 constructor accepts only ``charset``, ``env``,
    ``echo_stdin``, and ``catch_exceptions`` -- there is no
    ``mix_stderr`` kwarg in this version. The bare-call form below is
    the portable shape.

    Returns
    -------
    click.testing.CliRunner
        The runner instance. Callers typically use its
        :meth:`~click.testing.CliRunner.isolated_filesystem` context
        manager inside each test so CLI-integrated output does not leak
        into the shared working directory.
    """
    return CliRunner()


# ===========================================================================
# Miscellaneous helpers
# ===========================================================================


@contextmanager
def _restore_attr(obj: Any, name: str) -> Iterator[None]:
    """Context manager that saves and restores ``obj.name`` around a block.

    Used internally by some tests to locally patch a module-level
    attribute without relying on ``monkeypatch`` (e.g., when a test
    already consumes ``monkeypatch`` for a different purpose and wants
    an additional local scope).
    """
    original = getattr(obj, name)
    try:
        yield
    finally:
        setattr(obj, name, original)
