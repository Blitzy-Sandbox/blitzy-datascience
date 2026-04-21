"""Validation Gate 1 — End-to-end live smoke for ``python run.py all``.

Per ``docs/New_Product_Prompt_20260418.md`` §6 Gate 1 (lines 163-169)
and Agent Action Plan §§0.1.2, 0.5.1.8, 0.7.5, this test exercises
the full ``all`` subcommand against the live NBA Stats API at
``https://stats.nba.com/stats/`` and asserts that each of the seven
canonical CSV artifacts is produced with > 0 rows.

``all`` dispatches pipelines in the binding order specified by AAP
§0.4.5: ``schedule → games → teams → players → lineups``. The test
therefore exercises every feature F-009 through F-013 plus the
schedule enumeration pre-dependency (F-013 → F-011) and Rule 6
fail-safe iteration inside the games pipeline.

The product brief requires literally: *"Mocked tests do not satisfy
this gate."* This test hits the live upstream and is automatically
skipped when ``stats.nba.com`` is unreachable, so Gate 10 (``pytest``
exit 0) holds across environments. Expect the test to take 10-30
minutes because Rule 2 enforces a 1.0s inter-request floor and the
``all`` run hits 15+ endpoints; do not impose a shorter timeout.

Rule compliance (verified by ``tests/invariants/``):

* Rule 1 (*Single HTTP Client*) — this module does not import
  :mod:`requests`; the reachability probe uses :func:`socket.create_connection`.
* Rule 7 (*Pluggable Storage*) — this module does not call
  :meth:`pandas.DataFrame.to_csv`; all CSV reads go through the
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


pytestmark = pytest.mark.integration


# Canonical ordered tuple of the seven CSV artifact basenames the
# ``all`` subcommand is contractually required to produce (AAP §0.1.1
# "seven flat CSV artifacts under ``output/``"). Declared at module
# scope so the test body iterates one-shot over the exact set
# enumerated by the product brief rather than re-deriving the list.
_EXPECTED_CSV_NAMES: tuple[str, ...] = (
    config.CSV_PLAYERS,
    config.CSV_TEAMS,
    config.CSV_GAMES,
    config.CSV_PLAY_BY_PLAY,
    config.CSV_LINEUPS,
    config.CSV_SCHEDULE,
    config.CSV_PLAYER_TRACKING,
)


def _stats_nba_reachable() -> bool:
    """Return ``True`` when ``stats.nba.com:443`` answers within 5s.

    Uses the stdlib :mod:`socket` module rather than :mod:`requests`
    in deliberate observance of Rule 1 (*Single HTTP Client*, AAP
    §0.7.2.1): no test file may import :mod:`requests`, so the
    reachability probe is implemented at the TCP layer via
    :func:`socket.create_connection`.

    The 5-second timeout is deliberately short — a probe longer than
    that would itself be a failure mode worth surfacing, and the live
    ``all`` run will spend vastly more time than that on its first
    real request. This is the *only* function in the module permitted
    to use ``try``/``except`` (per Phase 9 rule-compliance check).

    The opened socket is explicitly closed via :keyword:`with` so
    it does not leak into pytest's unraisable-exception handler — in
    pytest 9.x, a resource warning from a garbage-collected open
    socket is elevated to
    :class:`~_pytest.unraisableexception.PytestUnraisableExceptionWarning`
    and fails the test under ``pytest.ini``'s ``filterwarnings=error``
    policy unless the socket is deterministically closed. This matches
    the pattern already used by the Gate 8 sibling helper at
    ``tests/integration/test_gate8_games_resume.py``.

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
    outbound network access — offline CI jobs, sandboxed review
    runners, and locked-down developer laptops all see the test as
    ``SKIPPED`` rather than ``FAILED``. The decision to *skip* rather
    than *fail* is intentional: Gate 1 requires a live API, and no
    live API means no gate exercise is possible — not that the code
    is broken.
    """
    if not _stats_nba_reachable():
        pytest.skip("stats.nba.com is not reachable from this environment")


def test_run_all_live_smoke_and_flatness(
    cli_runner,
    isolated_filesystem,
    csv_reader,
    skip_if_offline,
) -> None:
    """Gate 1 — ``python run.py all --season 2025-26`` end-to-end live smoke.

    Asserts, in order:

    * :class:`click.testing.CliRunner` exit code is 0 (Gate 1 contract).
    * Each of the seven canonical CSVs exists under
      ``config.OUTPUT_DIR`` (redirected into ``tmp_path`` by the
      :pyfixture:`isolated_filesystem` fixture so the operator's real
      ``output/`` directory is never touched) — Gate 1 artifact
      contract from ``docs/New_Product_Prompt_20260418.md`` §6.
    * Each CSV has > 0 rows — Gate 1 "non-empty" requirement. Uses
      :func:`len` on the :class:`pandas.DataFrame` returned by the
      :pyfixture:`csv_reader` fixture rather than
      :meth:`Path.stat.st_size` because a header-only CSV has
      non-zero bytes but zero data rows.
    * No CSV contains a cell with :class:`dict` or :class:`list`
      value — defense-in-depth for Rule 4 (*Flat CSV Output*, AAP
      §0.7.2.4) at the CSV read-back boundary. The
      :mod:`utils.schema_normalizer` pre-flatten assertion and the
      :class:`storage.csv_writer.CSVWriter` pre-write assertion are
      the primary enforcement points; this check is a third line of
      defense that catches any future regression where a normalizer
      bug lets a structured cell slip through both gates.
    * ``checkpoint.json`` exists, is valid JSON, and is a non-empty
      :class:`dict` — evidences Rule 5 (*Checkpoint After Every
      Pull*, AAP §0.7.2.5) at the end-to-end boundary. An empty
      manifest after a successful ``all`` run is an automatic Rule 5
      violation even if the CSVs themselves look correct.

    The ``catch_exceptions=False`` argument to
    :meth:`CliRunner.invoke` surfaces any exception that escapes the
    CLI so failures are diagnosable rather than silent exit-code-1
    failures.

    The test runs the 15+ endpoint ``all`` pipeline *exactly once*;
    splitting the non-emptiness and flatness assertions into separate
    test functions would double the 10-30 minute live runtime for no
    additional diagnostic value. See the schema's Phase 8
    "Recommended Final Structure (Single Test)" rationale.
    """
    output_dir: Path = isolated_filesystem["output"]

    result = cli_runner.invoke(
        cli,
        ["all", "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        "`run all` exited with %d: stderr=%s stdout=%s"
        % (result.exit_code, result.stderr, result.output)
    )

    for name in _EXPECTED_CSV_NAMES:
        path = output_dir / f"{name}.csv"
        assert path.exists(), "%s.csv was not produced by `run all`" % name
        df: pd.DataFrame = csv_reader(path)
        assert len(df) > 0, (
            "%s.csv has zero rows (Gate 1 requires non-empty)" % name
        )

        # ``DataFrame.map`` was introduced in pandas 2.1 (deprecating
        # ``applymap``); ``applymap`` remains available in pandas 2.0.
        # AAP §0.3.1 pins ``pandas>=2.0,<3`` so select whichever the
        # installed version exposes. ``getattr(df, "map", None)``
        # returns the bound method on 2.1+ (truthy, so ``or`` short-
        # circuits) and ``None`` on 2.0 (falsy, so the ``or`` falls
        # through to ``df.applymap``).
        checker = getattr(df, "map", None) or df.applymap
        has_nested = bool(
            checker(lambda cell: isinstance(cell, (dict, list))).any().any()
        )
        assert not has_nested, (
            "%s.csv contains a nested (dict/list) cell; Rule 4 violation "
            "at CSV read-back boundary" % name
        )

    checkpoint_path = output_dir / "checkpoint.json"
    assert checkpoint_path.exists(), (
        "checkpoint.json not produced; Rule 5 (AAP §0.7.2.5) requires "
        "checkpoint persistence after every successful pull"
    )
    manifest = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict), "checkpoint.json must be a JSON object"
    assert manifest, "checkpoint.json must have at least one completed entry"
