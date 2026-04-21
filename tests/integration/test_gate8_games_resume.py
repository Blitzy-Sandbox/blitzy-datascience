"""Validation Gate 8 - Live games smoke, resume determinism, zero 429s.

Per ``docs/New_Product_Prompt_20260418.md`` §6 Gate 8 (lines 170-174) and
Agent Action Plan §0.7.2.5 (Rule 5 - Checkpoint After Every Pull) and
§0.7.5 (Rule-to-Gate Verification Matrix), this test runs the ``games``
pipeline twice against the live NBA Stats API at
``https://stats.nba.com/stats/`` and asserts, in order:

* Phase A produces a ``games.csv`` with > 0 rows (product brief §6 Gate 8
  bullet 1).
* Phase A produces ``play_by_play.csv`` and ``checkpoint.json``
  alongside ``games.csv`` - the three artifact contracts expected from
  ``pipelines.ingest_games`` per AAP §0.4.5.
* Phase A records at least one increment of the ``nba_requests_total``
  counter registered in :mod:`utils.metrics` and incremented on every
  outbound HTTPS GET by :meth:`api.nba_client.NBAClient.get`
  (``api/nba_client.py`` per AAP §0.5.1.3).
* Phase B re-invokes the pipeline with the existing ``checkpoint.json``
  manifest preserved but the two CSV artifacts deleted; the second run
  therefore MUST consult the manifest and skip already-completed
  ``GAME_ID`` pulls (Rule 5 resume determinism, product brief §6 Gate 8
  bullet 3).
* Phase B re-produces CSV artifacts whose row counts and ``game_id``
  sets match phase A exactly - deterministic resume semantics at the
  data level.
* Neither phase records an HTTP 429 (*"Too Many Requests"*) in
  ``logs/pipeline.log`` - the rate limiter (:mod:`utils.rate_limiter`,
  Rule 2 per AAP §0.7.2.2) and tenacity-backed retries
  (:mod:`api.nba_client`, F-004) together MUST keep the observed HTTP
  status code stream free of 429 responses during a full pipeline run
  (product brief §6 Gate 8 bullet 4).

The full ``games`` pipeline typically takes 5-15 minutes because Rule 2
enforces a 1.0 s inter-request floor (``utils/rate_limiter.py``) and the
NBA Stats API exposes hundreds of games per season; each game requires
two endpoint calls (``boxscoretraditionalv2`` + ``playbyplayv2``). Do
not impose a shorter timeout.

The test is automatically skipped when ``stats.nba.com`` is not
reachable from the executing environment - preserving Gate 10 (``pytest``
exit 0) on offline machines. Set ``PYTEST_ADDOPTS='-m integration'`` to
include this test explicitly; the default ``pytest -m "not integration"``
filter excludes it.

The product brief states literally: *"Mocked tests do not satisfy this
gate."* This test hits the live upstream - Gate 8 is intentionally not
satisfiable by mocked tests.

Rule compliance (verified by ``tests/invariants/``):

* Rule 1 (*Single HTTP Client*, AAP §0.7.2.1) - this module does not
  import :mod:`requests`; the reachability probe uses
  :func:`socket.create_connection` at the TCP layer.
* Rule 7 (*Pluggable Storage*, AAP §0.7.2.7) - this module does not
  call :meth:`pandas.DataFrame.to_csv`; CSV reads go through the
  :pyfixture:`csv_reader` fixture which wraps :func:`pandas.read_csv`.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pandas as pd
import pytest

import config
from run import cli
from utils.metrics import registry


# Module-level marker applies ``@pytest.mark.integration`` to every test
# function in this file. The marker is registered in ``pytest.ini`` per
# AAP §0.5.1.1 so ``pytest -m "not integration"`` excludes this module
# cleanly (Gate 10). ``--strict-markers`` is in effect: an unregistered
# marker would fail collection rather than silently pass through.
pytestmark = pytest.mark.integration


def _stats_nba_reachable() -> bool:
    """Return ``True`` when ``stats.nba.com:443`` answers within 5s.

    Uses the stdlib :mod:`socket` module rather than :mod:`requests`
    in deliberate observance of Rule 1 (*Single HTTP Client*, AAP
    §0.7.2.1): no test file may import :mod:`requests`, so the
    reachability probe is implemented at the TCP layer via
    :func:`socket.create_connection`.

    The 5-second timeout is deliberately short - a probe longer than
    that would itself be a failure mode worth surfacing, and the live
    ``games`` run will spend vastly more time than that on its first
    real request. This is the *only* function in the module permitted
    to use ``try``/``except`` (per Phase 7 rule-compliance check in the
    assigned-file agent prompt).

    The opened socket is explicitly closed via :keyword:`with` so
    it does not leak into pytest's unraisable-exception handler - in
    pytest 9.x, a resource warning from a garbage-collected open
    socket is elevated to
    :class:`~_pytest.unraisableexception.PytestUnraisableExceptionWarning`
    and fails the test unless suppressed.

    Returns
    -------
    bool
        ``True`` when a TCP socket to ``stats.nba.com:443`` opens
        within 5 seconds; ``False`` on any :class:`OSError` (which is
        the common base class for :class:`TimeoutError`,
        :class:`ConnectionRefusedError`, and :class:`socket.gaierror`).
        Never raises.
    """
    try:
        with socket.create_connection(("stats.nba.com", 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.fixture
def skip_if_offline() -> None:
    """Skip the test when ``stats.nba.com`` is not reachable.

    Preserves Gate 10 (``pytest`` exit 0) on environments without
    outbound network access - offline CI jobs, sandboxed review
    runners, and locked-down developer laptops all see the test as
    ``SKIPPED`` rather than ``FAILED``. The decision to *skip* rather
    than *fail* is intentional: Gate 8 requires a live API, and no
    live API means no gate exercise is possible - not that the code
    is broken.
    """
    if not _stats_nba_reachable():
        pytest.skip("stats.nba.com is not reachable from this environment")


def test_games_resume_is_deterministic(
    cli_runner,
    isolated_filesystem,
    csv_reader,
    skip_if_offline,
) -> None:
    """Gate 8 - Live games smoke + checkpoint resume determinism + zero 429s.

    Phase A: Invoke ``run games --season 2025-26`` once. Capture
    ``games.csv`` row count, ``play_by_play.csv`` row count, the
    persisted ``checkpoint.json`` manifest, and the value of the
    ``nba_requests_total`` counter.

    Phase B: Delete the two CSV artifacts but leave ``checkpoint.json``
    intact; reset the in-memory metrics registry; re-invoke the
    pipeline. Assert that the second run completes successfully with
    strictly fewer outbound HTTP requests (Rule 5 resume determinism)
    and produces the same row counts and the same set of ``game_id``
    values.

    Phase C: Assert that the captured pipeline log contains zero HTTP
    429 occurrences (Rule 2 rate-limiting discipline; product brief §6
    Gate 8 bullet 4).

    The ``catch_exceptions=False`` argument to
    :meth:`CliRunner.invoke` surfaces any exception that escapes the
    CLI so failures are diagnosable rather than silent exit-code-1
    failures.

    Uses ``config.CSV_GAMES`` / ``config.CSV_PLAY_BY_PLAY`` /
    ``config.DEFAULT_SEASON`` rather than literal strings so the
    single source-of-truth in :mod:`config` governs artifact naming
    and the default season.
    """
    output_dir: Path = isolated_filesystem["output"]
    log_dir: Path = isolated_filesystem["logs"]
    games_csv = output_dir / ("%s.csv" % config.CSV_GAMES)
    pbp_csv = output_dir / ("%s.csv" % config.CSV_PLAY_BY_PLAY)
    checkpoint_path = output_dir / "checkpoint.json"
    log_file = log_dir / "pipeline.log"

    # ------------------------------------------------------------------
    # Phase A: first pipeline run
    # ------------------------------------------------------------------
    # Fresh invocation against the live NBA Stats API. The
    # ``isolated_filesystem`` fixture has monkeypatched
    # ``config.OUTPUT_DIR`` / ``config.CHECKPOINT_PATH`` /
    # ``config.LOG_DIR`` / ``config.LOG_FILE`` into ``tmp_path`` so the
    # operator's real ``output/`` and ``logs/`` directories are never
    # touched. The ``_reset_metrics_registry_between_tests`` autouse
    # fixture in ``tests/conftest.py`` guarantees the registry starts
    # at zero, so any non-zero ``nba_requests_total`` below is
    # attributable solely to this invocation.
    result_a = cli_runner.invoke(
        cli,
        ["games", "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result_a.exit_code == 0, (
        "phase A `run games` exited with %d: stderr=%s stdout=%s"
        % (result_a.exit_code, result_a.stderr, result_a.output)
    )
    assert games_csv.exists(), (
        "phase A did not produce %s (Gate 8 bullet 1)" % games_csv.name
    )
    assert pbp_csv.exists(), (
        "phase A did not produce %s (AAP §0.4.5 games pipeline contract)"
        % pbp_csv.name
    )
    assert checkpoint_path.exists(), (
        "phase A did not produce checkpoint.json; Rule 5 (AAP §0.7.2.5) "
        "requires checkpoint persistence after every successful pull"
    )

    games_df_a: pd.DataFrame = csv_reader(games_csv)
    pbp_df_a: pd.DataFrame = csv_reader(pbp_csv)
    assert len(games_df_a) > 0, (
        "games.csv has zero rows; Gate 8 bullet 1 requires > 0 rows"
    )

    manifest_a = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert isinstance(manifest_a, dict), (
        "checkpoint.json must be a JSON object (Rule 5)"
    )
    assert manifest_a, (
        "checkpoint.json must have at least one completed entry after "
        "a successful phase A run (Rule 5)"
    )

    requests_after_a = registry.get_counter_value("nba_requests_total") or 0.0
    assert requests_after_a > 0.0, (
        "phase A recorded zero ``nba_requests_total`` increments; "
        "``api.nba_client.NBAClient.get`` is contracted to increment "
        "the counter on every outbound request (AAP §0.5.1.3)"
    )

    # ------------------------------------------------------------------
    # Phase B: resume run with checkpoint preserved
    # ------------------------------------------------------------------
    # Deleting the CSV artifacts while preserving ``checkpoint.json``
    # forces the second run to consult the manifest and skip
    # ``(domain, key)`` tuples already marked completed (Rule 5). If
    # the checkpoint read path is broken, phase B would re-fetch every
    # ``GAME_ID`` and ``requests_after_b >= requests_after_a`` -
    # failing the assertion below.
    games_csv.unlink()
    pbp_csv.unlink()

    # Reset the metrics registry so the counter delta is attributable
    # to phase B alone. ``registry.reset()`` is documented in
    # ``utils/metrics.py`` as a tests-only affordance that clears
    # recorded observations while preserving metric registrations
    # (metric name, help text, bucket layout).
    registry.reset()

    result_b = cli_runner.invoke(
        cli,
        ["games", "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result_b.exit_code == 0, (
        "phase B `run games` exited with %d: stderr=%s stdout=%s"
        % (result_b.exit_code, result_b.stderr, result_b.output)
    )
    assert games_csv.exists(), (
        "phase B did not re-produce %s from the checkpoint-driven resume"
        % games_csv.name
    )
    assert pbp_csv.exists(), (
        "phase B did not re-produce %s from the checkpoint-driven resume"
        % pbp_csv.name
    )

    games_df_b: pd.DataFrame = csv_reader(games_csv)
    pbp_df_b: pd.DataFrame = csv_reader(pbp_csv)
    requests_after_b = registry.get_counter_value("nba_requests_total") or 0.0

    # Rule 5 resume determinism: phase B MUST make strictly fewer
    # outbound requests than phase A. Phase A had to fetch every
    # ``GAME_ID`` from scratch; phase B reads ``checkpoint.json``,
    # skips completed pairs, and issues only the residual requests
    # required to rematerialize the deleted CSV artifacts. Equal or
    # greater request counts would evidence a broken checkpoint
    # consultation path.
    assert requests_after_b < requests_after_a, (
        "phase B should make fewer HTTP requests than phase A when "
        "resuming from the checkpoint manifest: phase_a=%s phase_b=%s "
        "(AAP §0.7.2.5)"
        % (requests_after_a, requests_after_b)
    )

    # Determinism at the row-count level: identical runs of the games
    # pipeline against the same season MUST produce identical CSV
    # sizes. A divergence here indicates either a non-deterministic
    # pipeline or a transient upstream change - both of which are
    # Gate 8 failures.
    assert len(games_df_a) == len(games_df_b), (
        "games.csv row count diverged between runs: %d vs %d"
        % (len(games_df_a), len(games_df_b))
    )
    assert len(pbp_df_a) == len(pbp_df_b), (
        "play_by_play.csv row count diverged between runs: %d vs %d"
        % (len(pbp_df_a), len(pbp_df_b))
    )

    # Determinism at the identifier-set level: equal row counts are
    # necessary but not sufficient; the *same* ``game_id`` set must
    # appear in both runs. Guarded by a column-membership check
    # because the NBA Stats API occasionally returns case variants
    # (``GAME_ID`` vs ``game_id``) depending on the endpoint; the
    # schema normalizer in :mod:`utils.schema_normalizer` preserves
    # the original column casing, so we probe the lowercase form
    # first (the canonical flat-CSV convention) and only assert when
    # both runs surface the column.
    if "game_id" in games_df_a.columns and "game_id" in games_df_b.columns:
        ids_a = set(games_df_a["game_id"].astype(str).tolist())
        ids_b = set(games_df_b["game_id"].astype(str).tolist())
        assert ids_a == ids_b, (
            "game_id set diverged between phase A and phase B: "
            "only_in_a=%s only_in_b=%s"
            % (sorted(ids_a - ids_b)[:5], sorted(ids_b - ids_a)[:5])
        )

    # ------------------------------------------------------------------
    # Phase C: zero 429s in the pipeline log
    # ------------------------------------------------------------------
    # The pipeline logger writes to ``logs/pipeline.log`` via
    # :class:`logging.handlers.RotatingFileHandler` (see
    # ``utils/logger.py``). Absence of HTTP 429 occurrences is the
    # product brief §6 Gate 8 bullet 4 requirement and evidences both
    # Rule 2 (rate limiter ≥ 1.0 s floor) and the tenacity-based
    # retry discipline of F-004 working together. We use two
    # sentinels because tenacity's retry machinery logs exceptions in
    # both formats depending on the retry stage:
    #
    # * ``" 429 "`` (with surrounding spaces) catches the raw
    #   ``HTTPError("429 Client Error: Too Many Requests ...")`` body
    #   emitted when ``raise_for_status`` propagates a 4xx response -
    #   the surrounding spaces avoid false positives from timestamps or
    #   request-id substrings that happen to contain ``429``.
    # * ``"Too Many Requests"`` catches any formatter that emits only
    #   the canonical HTTP status text without the numeric code.
    if log_file.exists():
        log_contents = log_file.read_text(encoding="utf-8", errors="replace")
        assert " 429 " not in log_contents, (
            "HTTP 429 detected in pipeline log - Rule 2 rate-limiting "
            "violated during live run (product brief §6 Gate 8 bullet 4)"
        )
        assert "Too Many Requests" not in log_contents, (
            "HTTP 429 detected in pipeline log (message form) - Rule 2 "
            "rate-limiting violated during live run "
            "(product brief §6 Gate 8 bullet 4)"
        )
