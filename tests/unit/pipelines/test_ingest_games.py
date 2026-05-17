"""Unit tests for :mod:`pipelines.ingest_games` (Feature F-011).

Scope
-----
The Games pipeline is the most complex of the five domain pipelines in
this project — and the *only* one permitted to wrap its per-entity loop
in ``try/except Exception`` (AAP §0.7.2.6, Rule 6 "Fail-Safe Game
Iteration"). Because Rule 6 is behaviourally unique to this module,
this test file also houses the mandatory **Rule 6 canary** suite that
verifies the fail-safe semantics under three adversarial scenarios:
single-game failure, blanket failure, and negative-space verification
that failures raised *outside* the per-game body (notably by
``enumerate_game_ids``) do *not* get swallowed.

Verified behaviours
-------------------
1. **Happy path** — with 3 game IDs returned by
   :func:`endpoints.schedule.enumerate_game_ids` (monkey-patched at the
   module level — see *Monkey-patch target* below) and a clean
   checkpoint, invoking :func:`pipelines.ingest_games.run` results in
   exactly 2 ``client.get`` calls per game (one each to
   ``boxscoretraditionalv2`` and ``playbyplayv2``), exactly 2
   ``writer.write`` calls per game (``config.CSV_GAMES`` then
   ``config.CSV_PLAY_BY_PLAY``, with *cumulative* concatenated
   buffers), exactly 1 ``checkpoint.mark_completed`` per successful
   game keyed by ``(config.DOMAIN_GAMES, <GAME_ID>)``, and one
   ``pipeline_rows_written_total`` metric increment per write with
   labels
   ``{"pipeline": "ingest_games", "artifact": "games.csv"|"play_by_play.csv"}``
   and the ``n=`` keyword carrying the single-game row count (*not*
   the cumulative row count — each game contributes its own row
   delta). The ``pipeline`` and ``artifact`` label names — rather
   than ``domain``/``file`` — are the documented operator contract
   (``docs/OBSERVABILITY.md`` §``pipeline_rows_written_total`` and
   ``docs/dashboards/operator_dashboard.json`` L477).

2. **Idempotency (Rule 5 resume behaviour)** — a checkpoint
   pre-seeded with every game ID short-circuits ``get_pending`` to an
   empty list and the pipeline returns immediately without invoking
   the client, the writer, or ``mark_completed``. The ``get_pending``
   probe *is* recorded (that's how the skip is decided), but no other
   side effects occur.

3. **Rule 5 ordering per game** — for every successful game, the
   pipeline emits *two* writes (games, then play-by-play) *before* the
   corresponding ``mark_completed``. The test verifies this by
   inspecting the interleaved order of spy records across 3 games.

4. **Negative-space guard** — only the two permitted endpoints are
   invoked (``boxscoretraditionalv2``, ``playbyplayv2``); the pipeline
   must *not* call any of the other 14 endpoints registered in
   :mod:`endpoints` (e.g. ``leaguegamefinder``, ``leaguedashteamstats``,
   ``leaguedashlineups``).

5. **Rule 6 CANARY — single game failure** — when exactly one of three
   game IDs raises at its first fetch (``boxscoretraditionalv2``), the
   pipeline must: (a) not re-raise, (b) increment
   ``games_failed_total{reason=<ExceptionClassName>}`` exactly once
   (the ``reason`` label uses ``type(exc).__name__`` per AAP §0.5.1.6
   to keep cardinality bounded — the failing GID is *not* a label;
   it is surfaced via the WARNING log line instead), (c) emit a
   WARNING log with the format string ``"game %s failed: %s"`` and
   the failing ``<GAME_ID>`` as the first positional argument, (d)
   *not* call ``checkpoint.mark_completed`` for the failing GID,
   (e) still successfully process and mark the other two games, and
   (f) never accumulate the failed game's rows into either CSV
   buffer.

6. **Rule 6 CANARY — blanket failure** — when *all* three game IDs
   raise at ``boxscoretraditionalv2``, the pipeline still completes
   without raising; ``writer.write`` is never called; no game is
   checkpointed; ``games_failed_total`` is incremented three times
   (once per GID; all three carry the same ``reason`` label because
   the injected exception class is shared — this verifies the label
   collapses to bounded cardinality regardless of how many distinct
   games fail, and the distinct GIDs are instead surfaced through the
   WARNING log positional arguments); the final log line reports
   ``processed=0 failed=3``.

7. **Rule 6 scope — ``enumerate_game_ids`` failure propagates** —
   Rule 6's ``try/except`` is scoped to the *per-game* body only.
   Exceptions raised by ``enumerate_game_ids`` (upstream of the loop)
   propagate to the caller verbatim; no warning log, no metric bump,
   and no pipeline-level swallowing.

Monkey-patch target
-------------------
Because :mod:`pipelines.ingest_games` performs

    from endpoints.schedule import enumerate_game_ids

at *module scope* (see source lines 165–169), the monkey-patch target
for every test that wants to control the enumeration output is

    monkeypatch.setattr(
        "pipelines.ingest_games.enumerate_game_ids",
        lambda client, season: [...],
    )

Patching :mod:`endpoints.schedule.enumerate_game_ids` directly would
*not* work because the name is resolved at import time and bound into
the ``pipelines.ingest_games`` namespace.

Style conventions
-----------------
- Symbolic references via :mod:`config` (``config.DOMAIN_GAMES``,
  ``config.CSV_GAMES``, ``config.CSV_PLAY_BY_PLAY``) — no hardcoded
  string literals in assertions (AAP Phase 7 style convention).
- Handwritten ``recording_*`` spies via the conftest.py factory
  fixtures; :class:`unittest.mock.MagicMock` is reserved for the
  metrics sink and (when exact call captures are needed) the logger.
- Rule 6 is **behaviourally exclusive** to this pipeline — AAP
  §0.7.2.6 — so the canary tests below are the authoritative guard
  against regressions that would extend Rule 6 to other pipelines
  or weaken its scope here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest  # noqa: F401  (imported to satisfy Phase 1 header convention)

import config
from pipelines import ingest_games


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Season string used by every test. Must match the ``season`` kwarg
#: propagated into :func:`pipelines.ingest_games.run` and is verified
#: by assertions against ``writer.writes[*]["season"]`` and the
#: checkpoint key schema.
_SEASON = "2025-26"

#: Endpoint name for the traditional box score. Appears as ``calls[i][0]``
#: in :class:`RecordingClient.calls` — see conftest.py.
_BOXSCORE_ENDPOINT = "boxscoretraditionalv2"

#: Endpoint name for the play-by-play endpoint.
_PLAYBYPLAY_ENDPOINT = "playbyplayv2"

#: The three NBA Stats-style GAME_IDs used by every happy-path and
#: Rule 6 test. Structured so that a "failure on G2" scenario
#: deterministically leaves G1 and G3 succeeding — enabling strong
#: ordering assertions around the writer buffer and checkpoint marks.
_GAME_IDS = ("0022500001", "0022500002", "0022500003")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_enumerate(
    monkeypatch: pytest.MonkeyPatch,
    game_ids: Iterable[str] = _GAME_IDS,
) -> None:
    """Patch :func:`pipelines.ingest_games.enumerate_game_ids`.

    Replaces the module-level import with a trivial stub that returns
    ``list(game_ids)`` regardless of the arguments passed in. This is
    the canonical seam exploited by every test in this file — it
    isolates the Games pipeline from the Schedule endpoint so that
    Games-specific behaviours can be exercised in isolation (AAP
    §0.4.5 cross-domain dependency: Schedule → Games).

    The target path is ``pipelines.ingest_games.enumerate_game_ids``
    (not ``endpoints.schedule.enumerate_game_ids``) because the name
    is bound into the pipeline module's namespace at import time.

    Parameters
    ----------
    monkeypatch:
        The pytest ``monkeypatch`` fixture, scoped to the invoking
        test. Undoes the patch automatically on test teardown.
    game_ids:
        Iterable of GAME_IDs to return. Defaults to ``_GAME_IDS`` (3
        synthetic IDs). Pass a tuple/list/generator to customise.
    """
    ids: List[str] = list(game_ids)
    monkeypatch.setattr(
        "pipelines.ingest_games.enumerate_game_ids",
        lambda client, season: list(ids),
    )


class _SelectiveFailureClient:
    """Client spy that raises only for specific ``GameID`` values.

    Used by ``TestRule6FailSafe.test_single_game_failure_continues_iteration``
    to simulate a per-game upstream failure that Rule 6 must isolate.
    Unlike :class:`RecordingClient.raise_for` (which raises on *every*
    call to a given endpoint), this spy inspects ``params["GameID"]``
    so that only the designated game fails while the remaining games
    receive the configured happy-path response.

    Records call tuples on :attr:`calls` using the same
    ``(endpoint, params_dict)`` shape as :class:`RecordingClient`, so
    assertions over ``client.calls`` remain semantically compatible.

    Attributes
    ----------
    calls:
        Running list of ``(endpoint, params)`` tuples observed by
        :meth:`get` — order-preserving.
    responses:
        The endpoint → payload map supplied at construction time.
    failing_game_ids:
        Set of GAME_ID strings for which
        :meth:`get` raises :class:`RuntimeError` when invoked against
        :attr:`_failing_endpoint`.
    failing_endpoint:
        The endpoint name at which failure is simulated. Defaults to
        ``_BOXSCORE_ENDPOINT`` (the first upstream call per game in
        the pipeline — making the failure deterministic *before* any
        write or checkpoint work begins).
    exception_factory:
        Callable that receives the auto-generated failure message and
        returns a :class:`BaseException` instance to raise. Defaults
        to :class:`RuntimeError` (preserving pre-existing behaviour).
        Exception *classes* are themselves callables of the required
        shape, so passing ``exception_factory=KeyError`` produces
        ``KeyError("simulated ...")``. This hook is what enables
        :meth:`TestRule6FailSafe.test_rule6_tolerates_arbitrary_exception_types`
        to prove Rule 6's ``except Exception`` handler is
        exception-type-agnostic by parametrizing across ``RuntimeError``,
        ``KeyError``, ``IndexError``, and ``ValueError``.
    """

    def __init__(
        self,
        responses: Dict[str, Dict[str, Any]],
        failing_game_ids: Iterable[str],
        failing_endpoint: str = _BOXSCORE_ENDPOINT,
        exception_factory: Optional[Callable[[str], BaseException]] = None,
    ) -> None:
        self.responses: Dict[str, Dict[str, Any]] = dict(responses)
        self.failing_game_ids = set(failing_game_ids)
        self.failing_endpoint = str(failing_endpoint)
        # Default to ``RuntimeError`` to preserve the historical
        # failure type used by :meth:`TestRule6FailSafe.test_single_game_failure_continues_iteration`
        # and :meth:`TestRule6FailSafe.test_all_games_fail_does_not_abort_pipeline`.
        # Exception classes are themselves callables of shape
        # ``(str) -> BaseException``, so the parametrized test can pass
        # any concrete subclass (KeyError, IndexError, ValueError, …)
        # directly.
        self.exception_factory: Callable[[str], BaseException] = (
            exception_factory if exception_factory is not None else RuntimeError
        )
        self.calls: List[tuple] = []

    def get(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Record the call then return a payload or raise per failure config."""
        self.calls.append((str(endpoint), dict(params or {})))
        game_id = (params or {}).get("GameID")
        if endpoint == self.failing_endpoint and game_id in self.failing_game_ids:
            # Build the exception via ``exception_factory`` so that
            # subclass-agnostic Rule 6 tests can inject arbitrary
            # :class:`Exception` subclasses without having to subclass
            # this helper. The message format is identical regardless
            # of the injected type, keeping the WARNING log assertions
            # stable across parametrizations.
            raise self.exception_factory(
                f"simulated {endpoint} failure for GAME_ID={game_id}"
            )
        if endpoint in self.responses:
            return self.responses[endpoint]
        return {
            "resultSets": [
                {
                    "name": str(endpoint),
                    "headers": ["A"],
                    "rowSet": [[1]],
                }
            ]
        }


# ---------------------------------------------------------------------------
# Test 1 — Happy path (3 games, all successful)
# ---------------------------------------------------------------------------


def test_run_happy_path_writes_games_and_marks_each_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_multi_table_payload: Dict[str, Any],
    sample_playbyplay_payload: Dict[str, Any],
    tmp_path,
) -> None:
    """Happy path: 3 games produce 6 writes, 3 marks, 6 row-metric increments.

    With 3 GAME_IDs from the stubbed enumerator and both endpoints
    returning canonical sample payloads, the pipeline must:

    - Call the client exactly 2× per game (one boxscore, one PBP) ⇒ 6 total
    - Call the writer exactly 2× per game (games, play_by_play) ⇒ 6 total
    - Record cumulative row counts: games rows grow 2 → 4 → 6, PBP rows
      grow 3 → 6 → 9 (the sample payloads deliver 2 box-score rows and
      3 play-by-play rows per game after flattening)
    - Mark-complete exactly 3× (one per successful GAME_ID)
    - Increment ``pipeline_rows_written_total`` exactly 6 times with
      the per-game single-frame row count (``n=2`` for games rows,
      ``n=3`` for play-by-play rows) — never the cumulative value.
    """
    # --- Arrange -------------------------------------------------------
    _patch_enumerate(monkeypatch, _GAME_IDS)
    client = recording_client(
        responses={
            _BOXSCORE_ENDPOINT: sample_multi_table_payload,
            _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
        },
    )
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()
    metrics_mock = MagicMock()

    # --- Act -----------------------------------------------------------
    ingest_games.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- Assert: client calls -----------------------------------------
    # 2 endpoints per game × 3 games = 6 calls, alternating per game.
    assert len(client.calls) == 6, (
        f"expected 6 client.get calls (2 endpoints × 3 games); "
        f"got {len(client.calls)}: {client.calls!r}"
    )
    for i, gid in enumerate(_GAME_IDS):
        # Per-game ordering: boxscore then playbyplay.
        assert client.calls[2 * i][0] == _BOXSCORE_ENDPOINT, (
            f"game index {i}: expected call {2 * i} to be boxscore, "
            f"got {client.calls[2 * i]!r}"
        )
        assert client.calls[2 * i][1].get("GameID") == gid, (
            f"game index {i}: boxscore call must carry GameID={gid}, "
            f"got params={client.calls[2 * i][1]!r}"
        )
        assert client.calls[2 * i + 1][0] == _PLAYBYPLAY_ENDPOINT, (
            f"game index {i}: expected call {2 * i + 1} to be PBP, "
            f"got {client.calls[2 * i + 1]!r}"
        )
        assert client.calls[2 * i + 1][1].get("GameID") == gid, (
            f"game index {i}: PBP call must carry GameID={gid}, "
            f"got params={client.calls[2 * i + 1][1]!r}"
        )

    # --- Assert: writer calls (cumulative buffer pattern) -------------
    # The games pipeline re-writes BOTH artifacts after every successful
    # game, with a monotonically-growing cumulative buffer. So for 3
    # games we expect 6 writes: [games(2), pbp(3), games(4), pbp(6),
    # games(6), pbp(9)].
    assert len(writer.writes) == 6, (
        f"expected 6 writer.write calls (2 per game × 3 games); "
        f"got {len(writer.writes)}: {[w['name'] for w in writer.writes]!r}"
    )
    # Alternating artifact names: games, pbp, games, pbp, games, pbp.
    for i, write in enumerate(writer.writes):
        expected_name = (
            config.CSV_GAMES if i % 2 == 0 else config.CSV_PLAY_BY_PLAY
        )
        assert write["name"] == expected_name, (
            f"writer.writes[{i}] name mismatch: expected {expected_name!r}, "
            f"got {write['name']!r}"
        )
        assert write["season"] == _SEASON, (
            f"writer.writes[{i}] season mismatch: expected {_SEASON!r}, "
            f"got {write['season']!r}"
        )
    # Row-count monotonicity (cumulative): each artifact's row count
    # must be non-decreasing across its writes.
    games_writes = [w for w in writer.writes if w["name"] == config.CSV_GAMES]
    pbp_writes = [w for w in writer.writes if w["name"] == config.CSV_PLAY_BY_PLAY]
    assert len(games_writes) == 3, (
        f"expected 3 games-CSV writes; got {len(games_writes)}"
    )
    assert len(pbp_writes) == 3, (
        f"expected 3 play_by_play-CSV writes; got {len(pbp_writes)}"
    )
    prev_games_rows = 0
    for i, w in enumerate(games_writes):
        assert w["rows"] >= prev_games_rows, (
            f"games-CSV row count regressed at write {i}: "
            f"{w['rows']} < previous {prev_games_rows}"
        )
        prev_games_rows = w["rows"]
    prev_pbp_rows = 0
    for i, w in enumerate(pbp_writes):
        assert w["rows"] >= prev_pbp_rows, (
            f"PBP-CSV row count regressed at write {i}: "
            f"{w['rows']} < previous {prev_pbp_rows}"
        )
        prev_pbp_rows = w["rows"]

    # --- Assert: checkpoint marks -------------------------------------
    # Exactly one mark per game, in iteration order, with the correct
    # domain/key schema.
    expected_marks = [(config.DOMAIN_GAMES, gid) for gid in _GAME_IDS]
    assert checkpoint.marks == expected_marks, (
        f"expected marks={expected_marks!r}; got {checkpoint.marks!r}"
    )

    # --- Assert: get_pending probe ------------------------------------
    # Pipeline must consult the checkpoint's pending list ONCE at top
    # of run(). The RecordingCheckpoint records the ``(domain, tuple)``
    # shape of the call.
    assert len(checkpoint.pendings) == 1, (
        f"expected exactly 1 get_pending probe; "
        f"got {len(checkpoint.pendings)}: {checkpoint.pendings!r}"
    )
    assert checkpoint.pendings[0] == (config.DOMAIN_GAMES, tuple(_GAME_IDS)), (
        f"expected get_pending probe=({config.DOMAIN_GAMES!r}, "
        f"{tuple(_GAME_IDS)!r}); got {checkpoint.pendings[0]!r}"
    )

    # --- Assert: row-written metric increments ------------------------
    # Expect 6 inc calls to "pipeline_rows_written_total": 3 with
    # artifact="games.csv" and 3 with artifact="play_by_play.csv", each
    # carrying the single-game row count under the ``n=`` kwarg (NOT the
    # cumulative count). The ``pipeline`` and ``artifact`` label names —
    # rather than the older ``domain``/``file`` — are the documented
    # operator contract (``docs/OBSERVABILITY.md`` and
    # ``docs/dashboards/operator_dashboard.json`` L477). Artifact values
    # carry the ``.csv`` suffix to match the CSV filenames operators
    # actually see on disk.
    row_inc_calls = [
        c
        for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert len(row_inc_calls) == 6, (
        f"expected 6 pipeline_rows_written_total inc calls; "
        f"got {len(row_inc_calls)}: {row_inc_calls!r}"
    )
    games_inc = [
        c
        for c in row_inc_calls
        if c.args[1] == {"pipeline": "ingest_games", "artifact": f"{config.CSV_GAMES}.csv"}
    ]
    pbp_inc = [
        c
        for c in row_inc_calls
        if c.args[1]
        == {"pipeline": "ingest_games", "artifact": f"{config.CSV_PLAY_BY_PLAY}.csv"}
    ]
    assert len(games_inc) == 3, (
        f"expected 3 inc calls with artifact={config.CSV_GAMES!r}.csv; "
        f"got {len(games_inc)}"
    )
    assert len(pbp_inc) == 3, (
        f"expected 3 inc calls with artifact={config.CSV_PLAY_BY_PLAY!r}.csv; "
        f"got {len(pbp_inc)}"
    )
    # Each row-inc call must carry an ``n`` kwarg (per-game delta,
    # never the cumulative total).
    for c in row_inc_calls:
        assert "n" in c.kwargs, (
            f"pipeline_rows_written_total inc must use ``n=`` kwarg; "
            f"got args={c.args!r}, kwargs={c.kwargs!r}"
        )
        assert c.kwargs["n"] > 0, (
            f"``n=`` must be positive; got {c.kwargs['n']!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Idempotent skip
# ---------------------------------------------------------------------------


def test_run_idempotent_skip_when_all_games_checkpointed(
    monkeypatch: pytest.MonkeyPatch,
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_multi_table_payload: Dict[str, Any],
    sample_playbyplay_payload: Dict[str, Any],
    tmp_path,
) -> None:
    """Pre-seeded checkpoint ⇒ zero fetches, zero writes, zero marks.

    If every GAME_ID is already present in the checkpoint (via a prior
    successful run), :func:`CheckpointManager.get_pending` returns an
    empty list and the pipeline's early-return branch kicks in (see
    ``ingest_games.py`` lines 483-490: ``if not pending: return``).

    This test verifies:
      - ``client.get`` is never called;
      - ``writer.write`` is never called;
      - ``checkpoint.mark_completed`` is never called (no new marks);
      - ``checkpoint.get_pending`` **is** called once — that's how the
        skip is decided.
    """
    # --- Arrange -------------------------------------------------------
    _patch_enumerate(monkeypatch, _GAME_IDS)
    client = recording_client(
        responses={
            _BOXSCORE_ENDPOINT: sample_multi_table_payload,
            _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
        },
    )
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_GAMES: list(_GAME_IDS)},
    )
    metrics_mock = MagicMock()

    # --- Act -----------------------------------------------------------
    ingest_games.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- Assert --------------------------------------------------------
    assert client.calls == [], (
        f"client must not be called when all games are checkpointed; "
        f"got {client.calls!r}"
    )
    assert writer.writes == [], (
        f"writer must not be called when all games are checkpointed; "
        f"got {writer.writes!r}"
    )
    assert checkpoint.marks == [], (
        f"no mark_completed calls expected on an idempotent resume; "
        f"got {checkpoint.marks!r}"
    )
    # But get_pending IS observed — that's how the skip was decided.
    assert len(checkpoint.pendings) == 1, (
        f"expected exactly one get_pending probe; "
        f"got {len(checkpoint.pendings)}: {checkpoint.pendings!r}"
    )
    assert checkpoint.pendings[0] == (config.DOMAIN_GAMES, tuple(_GAME_IDS)), (
        f"expected get_pending probe=({config.DOMAIN_GAMES!r}, "
        f"{tuple(_GAME_IDS)!r}); got {checkpoint.pendings[0]!r}"
    )
    # No row-written metric increments should fire on a skip.
    row_inc_calls = [
        c
        for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert row_inc_calls == [], (
        f"no row-written metric increments expected on idempotent resume; "
        f"got {row_inc_calls!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Resume preserves existing CSV rows (QA FC-1 Issue 2)
# ---------------------------------------------------------------------------


def test_run_resume_preserves_existing_csv_rows(
    monkeypatch: pytest.MonkeyPatch,
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_multi_table_payload: Dict[str, Any],
    sample_playbyplay_payload: Dict[str, Any],
    tmp_path,
) -> None:
    """Cumulative-CSV resume contract (QA Final Checkpoint FC-1, Issue 2).

    Regression guard for the resume-determinism defect identified by the QA
    agent at Final Checkpoint FC-1: prior to the Issue 2 fix, the Games
    pipeline's on-disk ``games.csv`` and ``play_by_play.csv`` were
    **session-scoped** — after an interrupt-and-resume cycle the disk
    artifacts contained only the *current* session's rows, not the cumulative
    union of all sessions. The fix in ``pipelines/ingest_games.py`` adds two
    helpers (``_load_existing_games`` / ``_load_existing_pbp``) that seed the
    in-memory append buffers from any pre-existing CSVs before the per-game
    iteration begins, while de-duplicating any rows whose ``GAME_ID`` is
    scheduled for refresh in the current run. See the module docstring's
    "Resume semantics" section for the full contract.

    This test exercises that contract end-to-end under fully mocked
    collaborators:

    1. **History preservation.** Rows in ``games.csv`` / ``play_by_play.csv``
       whose ``GAME_ID`` is NOT in the current run's pending set must survive
       verbatim into every cumulative write performed by the current run.

    2. **Refresh-aware dedupe.** Rows in the seed CSV whose ``GAME_ID`` IS in
       the current run's pending set must be discarded by the seed loader —
       the current run's fresh fetch is the authoritative data for those
       games. This prevents the doubling-up that would otherwise occur when
       the pipeline re-fetches an already-completed but uncheckpointed game.

    3. **Monotonic cumulative growth.** Each subsequent per-game write must
       contain ≥ the row count of the previous per-game write of the same
       artifact name. Concretely: with 2 preserved seed rows and a mock
       payload of 2 PlayerStats rows per game across 3 games, the three
       successive ``games.csv`` writes must have row counts ``[4, 6, 8]``.
       Equivalently for ``play_by_play.csv``: with 2 preserved seed rows
       and 3 PlayByPlay rows per game, counts are ``[5, 8, 11]``.

    4. **Rule 5 checkpoint determinism.** The resume contract must not alter
       the per-game checkpoint ordering: one ``mark_completed`` call per
       successfully processed (not skipped) GAME_ID, in iteration order.

    The test intentionally does NOT patch the checkpoint manager to report
    prior-session completion for any of the 3 pending game IDs: the goal is
    to cover the realistic crash-recovery edge case where the on-disk CSV
    reflects *more* games than the on-disk checkpoint does (e.g., the
    process died between CSV write and checkpoint mark). In that regime,
    pending-ID-aware seed dedupe is what guarantees the fresh fetch wins.

    Seed rows for the test are shaped to mirror the runtime column schema
    produced by :func:`pipelines.ingest_games._ensure_game_columns` when
    feeding the mock payloads through :func:`utils.schema_normalizer`:
    uppercase ``GAME_ID`` (preserved verbatim from the NBA Stats API payload
    headers) plus lowercase ``season`` (injected by ``_ensure_game_columns``
    because the payload headers do not carry a SEASON column). Distinctive
    marker values (``PTS = 99`` in the games seed, ``EVENTNUM = 99`` in the
    PBP seed) make it trivial to prove dedupe with a single set comparison.

    Note on the mock client's payload shape: the ``recording_client`` fixture
    returns the *same* hardcoded payload for every endpoint invocation
    regardless of the ``GameID`` argument — see ``tests/conftest.py``. Every
    PlayerStats row carried by the mock payload bears ``GAME_ID="0022500001"``
    (the overlap GID), so the 6 freshly-fetched rows across 3 iterations all
    share that value. The overlap ``GAME_ID`` therefore appears exactly 6
    times in the final cumulative write — once per fresh-fetch row — with
    zero contribution from the seed (the seed's overlap row was filtered).
    This mirrors the happy-path test's approach of validating row-count
    monotonicity and per-artifact accounting without asserting per-gid
    GAME_ID values inside the DataFrame payload.
    """

    # --- Arrange ---------------------------------------------------------
    _patch_enumerate(monkeypatch, _GAME_IDS)
    client = recording_client(
        responses={
            _BOXSCORE_ENDPOINT: sample_multi_table_payload,
            _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
        }
    )
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()  # no prior completion for any GID
    metrics_mock = MagicMock()

    #: GAME_IDs present in the seed CSV that are NOT in the current run's
    #: pending set — these rows must be preserved verbatim.
    _HISTORICAL_GID_1 = "0022400098"
    _HISTORICAL_GID_2 = "0022400099"

    #: GAME_ID present in BOTH the seed CSV and the current run's pending
    #: set — the seed row must be filtered and the fresh fetch wins.
    _OVERLAP_GID = _GAME_IDS[0]  # "0022500001"

    #: Distinctive marker value that identifies a seed row; if this value
    #: surfaces in the fresh-fetch overlap rows of the final cumulative
    #: write, it proves the seed dedupe failed.
    _SEED_OVERLAP_PTS = 99
    _SEED_OVERLAP_EVENTNUM = 99

    # Seed the prior-session artifacts on disk. ``recording_writer``
    # auto-creates ``tmp_path / "output"`` so the parent dir exists.
    #
    # Schema rationale: uppercase ``GAME_ID`` (matches payload headers
    # preserved by the schema normalizer) + lowercase ``season`` (matches
    # the column ``_ensure_game_columns`` injects). Seeding with this exact
    # schema avoids spurious column additions or renames on the first
    # cumulative concat performed by ``_load_existing_games`` inside the
    # pipeline.
    seed_games_df = pd.DataFrame(
        [
            {
                "season": "2024-25",
                "GAME_ID": _HISTORICAL_GID_1,
                "PLAYER_ID": 201,
                "TEAM_ID": 1610612737,
                "PTS": 22,
            },
            {
                "season": "2024-25",
                "GAME_ID": _HISTORICAL_GID_2,
                "PLAYER_ID": 202,
                "TEAM_ID": 1610612738,
                "PTS": 18,
            },
            {
                # Overlap row — must be filtered by pending-ID dedupe.
                "season": "2025-26",
                "GAME_ID": _OVERLAP_GID,
                "PLAYER_ID": 999,
                "TEAM_ID": 1610612739,
                "PTS": _SEED_OVERLAP_PTS,
            },
        ]
    )
    seed_pbp_df = pd.DataFrame(
        [
            {
                "season": "2024-25",
                "GAME_ID": _HISTORICAL_GID_1,
                "EVENTNUM": 1,
                "EVENTMSGTYPE": 1,
                "PERIOD": 1,
            },
            {
                "season": "2024-25",
                "GAME_ID": _HISTORICAL_GID_2,
                "EVENTNUM": 1,
                "EVENTMSGTYPE": 1,
                "PERIOD": 1,
            },
            {
                # Overlap row — must be filtered by pending-ID dedupe.
                "season": "2025-26",
                "GAME_ID": _OVERLAP_GID,
                "EVENTNUM": _SEED_OVERLAP_EVENTNUM,
                "EVENTMSGTYPE": 99,
                "PERIOD": 9,
            },
        ]
    )

    games_csv_path = writer.output_dir / f"{config.CSV_GAMES}.csv"
    pbp_csv_path = writer.output_dir / f"{config.CSV_PLAY_BY_PLAY}.csv"
    # ``DataFrame.to_csv`` is invoked from test code here — Rule 7 scopes
    # the "sole caller of to_csv" invariant to production code under
    # ``pipelines/``, ``endpoints/``, ``utils/``, ``run.py``, ``config.py``;
    # test fixtures are explicitly outside that scope. The pipeline's
    # ``_load_existing_*`` helpers will exercise the ``pd.read_csv`` side
    # of the contract.
    seed_games_df.to_csv(games_csv_path, index=False)
    seed_pbp_df.to_csv(pbp_csv_path, index=False)

    # --- Act -------------------------------------------------------------
    ingest_games.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- Assert ----------------------------------------------------------

    # (1) Fetch pattern unchanged: even with seed CSVs present, every
    #     pending game ID is fetched exactly once for boxscore and once for
    #     PBP, in iteration order. Seeding does NOT suppress API calls —
    #     only the checkpoint suppresses them (Rule 5).
    assert len(client.calls) == 6, (
        f"expected 6 client calls (2 endpoints × 3 games); "
        f"got {len(client.calls)}: {client.calls!r}"
    )
    for i, gid in enumerate(_GAME_IDS):
        assert client.calls[2 * i][0] == _BOXSCORE_ENDPOINT, (
            f"game {i} expected boxscore endpoint; got {client.calls[2 * i][0]!r}"
        )
        assert client.calls[2 * i][1].get("GameID") == gid, (
            f"game {i} boxscore call expected GameID={gid!r}; "
            f"got {client.calls[2 * i][1]!r}"
        )
        assert client.calls[2 * i + 1][0] == _PLAYBYPLAY_ENDPOINT, (
            f"game {i} expected PBP endpoint; "
            f"got {client.calls[2 * i + 1][0]!r}"
        )
        assert client.calls[2 * i + 1][1].get("GameID") == gid, (
            f"game {i} PBP call expected GameID={gid!r}; "
            f"got {client.calls[2 * i + 1][1]!r}"
        )

    # (2) Writer was invoked 6 times (3 games × 2 artifacts), alternating
    #     ``games`` then ``play_by_play`` — same cadence as the happy path.
    #     Seeding adds no extra writes; the seed contributes rows to the
    #     in-memory buffer, not additional writer calls.
    assert len(writer.writes) == 6, (
        f"expected 6 writer.write invocations; got {len(writer.writes)}: "
        f"names={[w['name'] for w in writer.writes]!r}"
    )
    for i, write in enumerate(writer.writes):
        expected_name = (
            config.CSV_GAMES if i % 2 == 0 else config.CSV_PLAY_BY_PLAY
        )
        assert write["name"] == expected_name, (
            f"write[{i}] expected name={expected_name!r}; got {write['name']!r}"
        )
        assert write["season"] == _SEASON, (
            f"write[{i}] expected season={_SEASON!r}; got {write['season']!r}"
        )

    games_writes = [w for w in writer.writes if w["name"] == config.CSV_GAMES]
    pbp_writes = [
        w for w in writer.writes if w["name"] == config.CSV_PLAY_BY_PLAY
    ]
    assert len(games_writes) == 3, (
        f"expected 3 games writes (1 per pending game); got {len(games_writes)}"
    )
    assert len(pbp_writes) == 3, (
        f"expected 3 pbp writes (1 per pending game); got {len(pbp_writes)}"
    )

    # (3) Monotonic cumulative growth.
    #
    #     Seed contribution after dedupe:
    #       games:  3 seed rows - 1 overlap row filtered = 2 preserved rows
    #       pbp:    3 seed rows - 1 overlap row filtered = 2 preserved rows
    #
    #     Fresh fetches per iteration (from the mock payloads):
    #       games:  2 rows per boxscore fetch  (PlayerStats: 2 rows)
    #       pbp:    3 rows per PBP fetch       (PlayByPlay: 3 rows)
    #
    #     Expected per-iteration cumulative totals:
    #       games_writes[i].rows = 2 (seed) + 2 * (i + 1)  for i in 0..2
    #         → [4, 6, 8]
    #       pbp_writes[i].rows   = 2 (seed) + 3 * (i + 1)  for i in 0..2
    #         → [5, 8, 11]
    games_row_counts = [w["rows"] for w in games_writes]
    pbp_row_counts = [w["rows"] for w in pbp_writes]
    assert games_row_counts == [4, 6, 8], (
        f"expected games cumulative row counts [4, 6, 8] "
        f"(2 preserved seed rows + 2×N fresh rows from {_BOXSCORE_ENDPOINT} "
        f"across 3 iterations); got {games_row_counts!r}"
    )
    assert pbp_row_counts == [5, 8, 11], (
        f"expected pbp cumulative row counts [5, 8, 11] "
        f"(2 preserved seed rows + 3×N fresh rows from {_PLAYBYPLAY_ENDPOINT} "
        f"across 3 iterations); got {pbp_row_counts!r}"
    )

    # (4) History preservation: every cumulative write — including the
    #     very first one, which is written mid-iteration — contains both
    #     historical ``GAME_ID`` values verbatim from the seed.
    def _resolve_col(df: pd.DataFrame, canonical: str) -> str:
        """Case-insensitive column lookup; mirrors ``_load_existing_*`` logic."""
        for col in df.columns:
            if col.lower() == canonical.lower():
                return col
        raise AssertionError(
            f"expected a column matching {canonical!r} (case-insensitive); "
            f"got columns={list(df.columns)!r}"
        )

    for idx, write in enumerate(games_writes):
        df = write["df"]
        gid_col = _resolve_col(df, "GAME_ID")
        gids_in_write = set(df[gid_col].astype(str).tolist())
        assert _HISTORICAL_GID_1 in gids_in_write, (
            f"games_writes[{idx}] missing historical GID {_HISTORICAL_GID_1!r}; "
            f"seed-history preservation broken. GIDs in write: "
            f"{sorted(gids_in_write)!r}"
        )
        assert _HISTORICAL_GID_2 in gids_in_write, (
            f"games_writes[{idx}] missing historical GID {_HISTORICAL_GID_2!r}; "
            f"seed-history preservation broken. GIDs in write: "
            f"{sorted(gids_in_write)!r}"
        )

    for idx, write in enumerate(pbp_writes):
        df = write["df"]
        gid_col = _resolve_col(df, "GAME_ID")
        gids_in_write = set(df[gid_col].astype(str).tolist())
        assert _HISTORICAL_GID_1 in gids_in_write, (
            f"pbp_writes[{idx}] missing historical GID {_HISTORICAL_GID_1!r}; "
            f"seed-history preservation broken. GIDs in write: "
            f"{sorted(gids_in_write)!r}"
        )
        assert _HISTORICAL_GID_2 in gids_in_write, (
            f"pbp_writes[{idx}] missing historical GID {_HISTORICAL_GID_2!r}; "
            f"seed-history preservation broken. GIDs in write: "
            f"{sorted(gids_in_write)!r}"
        )

    # (5) Refresh-aware dedupe: the final cumulative write is the
    #     strongest evidence of the contract because it represents the
    #     on-disk CSV shape after the run completes. In the final games
    #     write, the overlap GAME_ID must appear exactly 6 times — all
    #     from fresh fetches (2 rows × 3 iterations). Zero contribution
    #     from the seed overlap row, which was filtered out of the
    #     preserved set by ``_load_existing_games``'s pending-ID filter.
    final_games_df = games_writes[-1]["df"]
    games_gid_col = _resolve_col(final_games_df, "GAME_ID")
    overlap_rows_games = final_games_df[
        final_games_df[games_gid_col].astype(str) == _OVERLAP_GID
    ]
    assert len(overlap_rows_games) == 6, (
        f"expected exactly 6 rows with GAME_ID={_OVERLAP_GID!r} in the final "
        f"games write (2 PlayerStats rows per payload × 3 iterations = 6, "
        f"with the seed overlap row filtered by pending-ID dedupe); "
        f"got {len(overlap_rows_games)}. A value of 7 would indicate the "
        f"seed overlap row was not filtered (dedupe regression). A value "
        f"other than 6 or 7 would indicate a deeper buffer-management "
        f"regression."
    )

    games_pts_col = _resolve_col(final_games_df, "PTS")
    # pandas may read CSV PTS values as numpy int64; coerce to plain Python
    # ints so the set comparison is stable across pandas versions.
    overlap_pts = {int(v) for v in overlap_rows_games[games_pts_col].tolist()}
    # The mock ``PlayerStats`` payload has PTS values {28, 35}. If the seed
    # overlap row (PTS=99) survived, 99 would appear in this set.
    assert overlap_pts == {28, 35}, (
        f"expected overlap-GID rows in the final games write to carry only "
        f"PTS values from the fresh PlayerStats payload ({{28, 35}}); "
        f"got {sorted(overlap_pts)!r}. Presence of {_SEED_OVERLAP_PTS} "
        f"indicates the seed overlap row (GAME_ID={_OVERLAP_GID!r}, "
        f"PTS={_SEED_OVERLAP_PTS}) was not filtered by the resume dedupe "
        f"logic — a direct regression of QA FC-1 Issue 2."
    )
    assert _SEED_OVERLAP_PTS not in overlap_pts, (
        f"seed-overlap marker PTS={_SEED_OVERLAP_PTS} must not survive into "
        f"the final write; its presence proves the ``_load_existing_games`` "
        f"pending-ID dedupe filter is broken."
    )

    # (6) Same dedupe contract for play-by-play: overlap GAME_ID must
    #     appear exactly 9 times (3 PBP rows per payload × 3 iterations)
    #     and the seed overlap row's distinctive EVENTNUM=99 must not
    #     survive.
    final_pbp_df = pbp_writes[-1]["df"]
    pbp_gid_col = _resolve_col(final_pbp_df, "GAME_ID")
    overlap_rows_pbp = final_pbp_df[
        final_pbp_df[pbp_gid_col].astype(str) == _OVERLAP_GID
    ]
    assert len(overlap_rows_pbp) == 9, (
        f"expected exactly 9 rows with GAME_ID={_OVERLAP_GID!r} in the final "
        f"pbp write (3 PlayByPlay rows per payload × 3 iterations = 9, with "
        f"the seed overlap row filtered by pending-ID dedupe); "
        f"got {len(overlap_rows_pbp)}. 10 would indicate dedupe regression."
    )

    pbp_eventnum_col = _resolve_col(final_pbp_df, "EVENTNUM")
    overlap_eventnums = {
        int(v) for v in overlap_rows_pbp[pbp_eventnum_col].tolist()
    }
    # The mock ``PlayByPlay`` payload has EVENTNUM values {1, 2, 3}.
    assert overlap_eventnums == {1, 2, 3}, (
        f"expected overlap-GID rows in the final pbp write to carry only "
        f"EVENTNUM values from the fresh PlayByPlay payload ({{1, 2, 3}}); "
        f"got {sorted(overlap_eventnums)!r}. Presence of "
        f"{_SEED_OVERLAP_EVENTNUM} indicates the seed overlap row was not "
        f"filtered by the resume dedupe logic."
    )

    # (7) Rule 5 checkpoint determinism: exactly one mark_completed call
    #     per pending GAME_ID, in iteration order. Resume semantics must
    #     not alter the per-game ordering.
    expected_marks = [(config.DOMAIN_GAMES, gid) for gid in _GAME_IDS]
    assert checkpoint.marks == expected_marks, (
        f"expected one checkpoint mark per pending game in iteration order; "
        f"expected={expected_marks!r}, got={checkpoint.marks!r}"
    )

    # (8) Pending probe: pipeline asked the checkpoint for the pending set
    #     exactly once, for the full set of enumerated GAME_IDs.
    assert len(checkpoint.pendings) == 1, (
        f"expected exactly one get_pending probe; "
        f"got {len(checkpoint.pendings)}: {checkpoint.pendings!r}"
    )
    assert checkpoint.pendings[0] == (
        config.DOMAIN_GAMES,
        tuple(_GAME_IDS),
    ), (
        f"pending probe was called with unexpected arguments; "
        f"got {checkpoint.pendings[0]!r}"
    )

    # (9) Metrics: pipeline emitted exactly 3 ``pipeline_rows_written_total``
    #     increments per artifact (one per per-game write). Each increment
    #     carries a positive ``n`` kwarg reflecting the *fresh* rows
    #     appended by that iteration — NOT the cumulative buffer size —
    #     because metrics semantics describe deltas, not snapshots.
    row_inc_calls = [
        c
        for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    games_inc_calls = [
        c
        for c in row_inc_calls
        if len(c.args) >= 2
        and c.args[1]
        == {"pipeline": "ingest_games", "artifact": f"{config.CSV_GAMES}.csv"}
    ]
    pbp_inc_calls = [
        c
        for c in row_inc_calls
        if len(c.args) >= 2
        and c.args[1]
        == {
            "pipeline": "ingest_games",
            "artifact": f"{config.CSV_PLAY_BY_PLAY}.csv",
        }
    ]
    assert len(games_inc_calls) == 3, (
        f"expected 3 games-artifact row-written increments (one per game); "
        f"got {len(games_inc_calls)}"
    )
    assert len(pbp_inc_calls) == 3, (
        f"expected 3 pbp-artifact row-written increments (one per game); "
        f"got {len(pbp_inc_calls)}"
    )
    for c in games_inc_calls + pbp_inc_calls:
        assert "n" in c.kwargs, (
            f"pipeline_rows_written_total increment missing ``n`` kwarg: "
            f"{c!r}"
        )
        assert c.kwargs["n"] > 0, (
            f"pipeline_rows_written_total increment had non-positive n: {c!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Rule 5 ordering (mark_completed follows writes per game)
# ---------------------------------------------------------------------------


def test_rule5_mark_completed_follows_writes_per_game(
    monkeypatch: pytest.MonkeyPatch,
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_multi_table_payload: Dict[str, Any],
    sample_playbyplay_payload: Dict[str, Any],
    tmp_path,
) -> None:
    """Rule 5: ``mark_completed`` must follow ``writer.write`` per game.

    Rule 5 (AAP §0.7.2.5) requires that the checkpoint be marked
    *immediately after* the successful CSV write — never before.
    Because the Games pipeline writes *both* CSVs (games and PBP)
    before marking, this test verifies the exact interleaving:

    Expected per-game sequence (over 3 games):
      write(games, g1) → write(pbp, g1) → mark(g1)
      → write(games, g2) → write(pbp, g2) → mark(g2)
      → write(games, g3) → write(pbp, g3) → mark(g3)

    The :class:`RecordingWriter` and :class:`RecordingCheckpoint`
    spies do not share a unified event log, so we reconstruct the
    ordering by counting: after N marks, there must be exactly 2N
    writes.
    """
    # --- Arrange -------------------------------------------------------
    _patch_enumerate(monkeypatch, _GAME_IDS)
    client = recording_client(
        responses={
            _BOXSCORE_ENDPOINT: sample_multi_table_payload,
            _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
        },
    )
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    # --- Act -----------------------------------------------------------
    ingest_games.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # --- Assert: exact tally -----------------------------------------
    # For N games successfully processed, 2N writes and N marks.
    assert len(writer.writes) == 2 * len(_GAME_IDS), (
        f"expected {2 * len(_GAME_IDS)} writes; got {len(writer.writes)}"
    )
    assert len(checkpoint.marks) == len(_GAME_IDS), (
        f"expected {len(_GAME_IDS)} marks; got {len(checkpoint.marks)}: "
        f"{checkpoint.marks!r}"
    )
    # Verify marks arrive in GAME_ID order (reflects the per-game
    # control flow — not an incidental quirk of the spy).
    assert checkpoint.marks == [
        (config.DOMAIN_GAMES, gid) for gid in _GAME_IDS
    ], f"mark order diverged; got {checkpoint.marks!r}"

    # --- Assert: get_pending precedes any write ----------------------
    # Rule 5 also requires the pending probe to precede the per-game
    # loop. With a single get_pending call recorded, and 2N writes
    # recorded afterwards, the invariant holds.
    assert len(checkpoint.pendings) == 1, (
        f"expected exactly one get_pending probe; "
        f"got {len(checkpoint.pendings)}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Negative-space guard (forbidden endpoints)
# ---------------------------------------------------------------------------


def test_no_forbidden_endpoints_are_invoked(
    monkeypatch: pytest.MonkeyPatch,
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_multi_table_payload: Dict[str, Any],
    sample_playbyplay_payload: Dict[str, Any],
    tmp_path,
) -> None:
    """Games pipeline must only invoke ``boxscoretraditionalv2`` and ``playbyplayv2``.

    This negative-space assertion protects against future regressions
    where the Games pipeline might be "helpfully" extended to invoke
    endpoints owned by other domains (e.g. ``leaguegamefinder`` from
    Schedule, ``leaguedashteamstats`` from Teams, or
    ``leaguedashlineups`` from Lineups). The set of forbidden
    endpoints below covers the other 14 endpoints registered in
    :mod:`endpoints` — see ``endpoints/__init__.py::__all__``.
    """
    # --- Arrange -------------------------------------------------------
    _patch_enumerate(monkeypatch, _GAME_IDS)
    client = recording_client(
        responses={
            _BOXSCORE_ENDPOINT: sample_multi_table_payload,
            _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
        },
    )
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    # --- Act -----------------------------------------------------------
    ingest_games.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # --- Assert --------------------------------------------------------
    forbidden_endpoints = {
        # Schedule domain
        "leaguegamefinder",
        # Teams domain
        "leaguedashteamstats",
        "teamgamelog",
        "teamdashboardbygeneralsplits",
        # Players domain
        "leaguedashplayerstats",
        "leaguedashplayerclutch",
        "playercareerstats",
        "playergamelog",
        "leaguedashptstats",
        # Lineups domain
        "leaguedashlineups",
        "leaguedashplayerclutch_onoff",
        # Other Games endpoints NOT used by this pipeline's primary path
        "scoreboardv2",
        "boxscoreadvancedv2",
    }
    called_endpoints = {call[0] for call in client.calls}
    overlap = forbidden_endpoints & called_endpoints
    assert not overlap, (
        f"games pipeline must not invoke {overlap!r}; "
        f"got client.calls endpoints={called_endpoints!r}"
    )
    # Affirmative: only the two permitted endpoints appear.
    assert called_endpoints == {_BOXSCORE_ENDPOINT, _PLAYBYPLAY_ENDPOINT}, (
        f"expected only {{{_BOXSCORE_ENDPOINT!r}, {_PLAYBYPLAY_ENDPOINT!r}}}; "
        f"got {called_endpoints!r}"
    )


# ---------------------------------------------------------------------------
# Rule 6 CANARY Suite — AAP §0.7.2.6
# ---------------------------------------------------------------------------


class TestRule6FailSafe:
    """Rule 6 Canary — the most important test class in this file.

    Rule 6 (AAP §0.7.2.6) states that the Games pipeline — and *only*
    the Games pipeline — wraps its per-``GAME_ID`` body in
    ``try/except Exception`` so that a failure fetching, normalising,
    writing, or checkpointing *one* game does not abort the entire
    season. The handler's responsibilities, as implemented at
    ``ingest_games.py`` lines 565-570, are exactly four actions:

      1. Increment the local ``failed`` counter (used only for the
         final log line).
      2. Emit a WARNING log with the format string
         ``"game %s failed: %s"`` carrying the failing GAME_ID and
         the exception as positional arguments — this is where the
         failing GAME_ID is surfaced to operators (it is deliberately
         NOT a metric label).
      3. Increment ``games_failed_total`` with the positional label
         dict ``{"reason": type(exc).__name__}`` (no ``n=`` kwarg —
         the default increment of 1 applies). Per AAP §0.5.1.6 the
         ``reason`` label takes the exception *class name* (e.g.
         ``"RuntimeError"``, ``"HTTPError"``) — NEVER the failing
         GAME_ID — so that cardinality stays bounded by the finite
         set of exception types the pipeline can raise, regardless
         of how many distinct games fail over the season. This
         matches the documented operator contract at
         ``docs/OBSERVABILITY.md`` §``games_failed_total`` and
         ``docs/dashboards/operator_dashboard.json`` L236.
      4. ``continue`` — skip to the next GAME_ID without marking the
         checkpoint, without re-raising, and without writing the
         current game's partial buffer contents.

    These tests exercise three distinct scenarios to prevent
    regression:
      - *Single game failure* — boundary conditions around the
        successful games on either side of the failing one.
      - *Blanket failure* — every game fails; pipeline still
        completes gracefully with ``processed=0 failed=N``. Because
        all three injected exceptions share a class, the
        ``games_failed_total`` metric collapses to a single label
        value (``reason="RuntimeError"``) incremented 3 times — this
        is precisely the bounded-cardinality property the
        ``reason``-labelled contract guarantees. The three distinct
        GAME_IDs are surfaced via the three WARNING log records'
        positional arguments.
      - *Rule 6 scope* — failures *outside* the per-game body
        (specifically, in ``enumerate_game_ids``) propagate verbatim.
    """

    # -----------------------------------------------------------------
    # Rule 6 — single game failure (G2 fails, G1 and G3 succeed)
    # -----------------------------------------------------------------

    def test_single_game_failure_continues_iteration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_writer,
        recording_checkpoint,
        sample_multi_table_payload: Dict[str, Any],
        sample_playbyplay_payload: Dict[str, Any],
        tmp_path,
    ) -> None:
        """G2 boxscore raises ⇒ G1 and G3 still process; only G2 marked failed.

        Uses :class:`_SelectiveFailureClient` to simulate an upstream
        failure on exactly one GAME_ID. The pipeline must:
          - not re-raise
          - invoke the boxscore endpoint for every GAME_ID (3 calls)
          - invoke the PBP endpoint for only G1 and G3 (2 calls) —
            G2 bails before reaching PBP
          - write exactly 4 CSVs (2 per successful game)
          - mark only G1 and G3
          - increment ``games_failed_total`` exactly once with the
            bounded-cardinality label
            ``{"reason": type(exc).__name__}`` — here
            ``{"reason": "RuntimeError"}`` because
            :class:`_SelectiveFailureClient` raises ``RuntimeError``.
            The failing GAME_ID deliberately does NOT appear in the
            metric label; it is surfaced in the WARNING log line
            instead.
          - emit exactly one WARNING log matching ``"game %s failed: %s"``
            with G2's ID as the first positional argument (this is
            where the failing GAME_ID is surfaced to operators).
        """
        # --- Arrange ---------------------------------------------------
        _patch_enumerate(monkeypatch, _GAME_IDS)
        failing_gid = _GAME_IDS[1]
        client = _SelectiveFailureClient(
            responses={
                _BOXSCORE_ENDPOINT: sample_multi_table_payload,
                _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
            },
            failing_game_ids=[failing_gid],
        )
        writer = recording_writer(tmp_path)
        checkpoint = recording_checkpoint()
        metrics_mock = MagicMock()
        logger_mock = MagicMock()

        # --- Act — MUST NOT RAISE -------------------------------------
        try:
            ingest_games.run(
                client=client,
                writer=writer,
                checkpoint=checkpoint,
                season=_SEASON,
                logger=logger_mock,
                metrics=metrics_mock,
            )
        except Exception as exc:
            pytest.fail(
                f"Rule 6 violated: per-game failure propagated to caller: "
                f"{exc!r}"
            )

        # --- Assert: client was called for each game's boxscore ------
        boxscore_calls = [c for c in client.calls if c[0] == _BOXSCORE_ENDPOINT]
        assert len(boxscore_calls) == 3, (
            f"expected boxscore fetch for every GAME_ID (3 calls); "
            f"got {len(boxscore_calls)}: {boxscore_calls!r}"
        )
        # PBP is only invoked for the two successful games.
        pbp_calls = [c for c in client.calls if c[0] == _PLAYBYPLAY_ENDPOINT]
        pbp_gids = {c[1].get("GameID") for c in pbp_calls}
        assert pbp_gids == {_GAME_IDS[0], _GAME_IDS[2]}, (
            f"PBP must be fetched only for successful games; "
            f"got PBP GAME_IDs={pbp_gids!r}"
        )

        # --- Assert: writer invoked only for successful games --------
        # 2 writes per successful game × 2 successful games = 4 writes.
        assert len(writer.writes) == 4, (
            f"expected 4 writes (2 successful games × 2 artifacts); "
            f"got {len(writer.writes)}: "
            f"{[w['name'] for w in writer.writes]!r}"
        )
        # All writes carry season propagation.
        for w in writer.writes:
            assert w["season"] == _SEASON
            assert w["name"] in {config.CSV_GAMES, config.CSV_PLAY_BY_PLAY}

        # --- Assert: checkpoint marks exclude the failing game -------
        # G2 was skipped mid-try-block and must NOT appear in marks.
        assert checkpoint.marks == [
            (config.DOMAIN_GAMES, _GAME_IDS[0]),
            (config.DOMAIN_GAMES, _GAME_IDS[2]),
        ], (
            f"expected marks for G1 and G3 only (G2 failed); "
            f"got {checkpoint.marks!r}"
        )
        # Failing game must not leak into the marks list via any path.
        assert (config.DOMAIN_GAMES, failing_gid) not in checkpoint.marks, (
            f"failing GAME_ID {failing_gid!r} must not be checkpointed; "
            f"got marks={checkpoint.marks!r}"
        )

        # --- Assert: games_failed_total incremented for G2 only ------
        failed_inc_calls = [
            c
            for c in metrics_mock.inc.call_args_list
            if c.args and c.args[0] == "games_failed_total"
        ]
        assert len(failed_inc_calls) == 1, (
            f"expected exactly 1 games_failed_total inc call; "
            f"got {len(failed_inc_calls)}: {failed_inc_calls!r}"
        )
        failed_call = failed_inc_calls[0]
        # Labels: must be positional, {"reason": type(exc).__name__},
        # with NO ``n=`` kwarg (the default of 1 is implicit — see
        # ingest_games.py line 569: met.inc("games_failed_total",
        # {"reason": type(exc).__name__}). Per AAP §0.5.1.6 the
        # ``reason`` label carries the exception class name — here
        # "RuntimeError" because :class:`_SelectiveFailureClient`
        # raises ``RuntimeError``. The failing GAME_ID is deliberately
        # NOT a label; it is surfaced in the WARNING log line below
        # (cardinality discipline — keeps the label set bounded by
        # exception types rather than ballooning with the number of
        # distinct failing games over a season).
        assert failed_call.args[1] == {"reason": "RuntimeError"}, (
            f"games_failed_total labels mismatch; "
            f"expected {{'reason': 'RuntimeError'}}; "
            f"got {failed_call.args[1]!r}"
        )
        assert "n" not in failed_call.kwargs, (
            f"games_failed_total must be incremented positionally without "
            f"``n=`` kwarg; got kwargs={failed_call.kwargs!r}"
        )

        # --- Assert: exactly one WARNING log with Rule 6 format ------
        warning_calls = logger_mock.warning.call_args_list
        assert len(warning_calls) == 1, (
            f"expected exactly 1 log.warning call for the failing game; "
            f"got {len(warning_calls)}: {warning_calls!r}"
        )
        fmt, *args = warning_calls[0].args
        assert fmt == "game %s failed: %s", (
            f"Rule 6 WARNING format string mismatch; "
            f"expected 'game %%s failed: %%s'; got {fmt!r}"
        )
        assert args[0] == failing_gid, (
            f"WARNING first positional arg must be failing GAME_ID; "
            f"expected {failing_gid!r}; got {args[0]!r}"
        )

    # -----------------------------------------------------------------
    # Rule 6 — exception-type-agnosticism (parametrized)
    # -----------------------------------------------------------------

    @pytest.mark.parametrize(
        "exception_cls",
        [RuntimeError, KeyError, IndexError, ValueError],
        ids=["RuntimeError", "KeyError", "IndexError", "ValueError"],
    )
    def test_rule6_tolerates_arbitrary_exception_types(
        self,
        exception_cls: type,
        monkeypatch: pytest.MonkeyPatch,
        recording_writer,
        recording_checkpoint,
        sample_multi_table_payload: Dict[str, Any],
        sample_playbyplay_payload: Dict[str, Any],
        tmp_path,
    ) -> None:
        """Rule 6's ``except Exception`` catches every :class:`Exception` subclass.

        Proves exception-type-agnosticism by parametrizing the injected
        failure across four common standard-library exception classes:
        :class:`RuntimeError`, :class:`KeyError`, :class:`IndexError`,
        and :class:`ValueError`. For each type, the pipeline must:

          1. Not re-raise — Rule 6 catches the exception at the per-game
             boundary regardless of concrete class.
          2. Process and checkpoint G1 and G3 (the two successful games)
             normally — demonstrating the ``continue`` branch does not
             corrupt iteration state.
          3. Leave the failing G2 UNMARKED — ``mark_completed`` is inside
             the ``try`` block, so a failure must prevent the checkpoint
             write.
          4. Increment ``games_failed_total`` exactly once with the
             positional label ``{"reason": exception_cls.__name__}`` —
             confirming the bounded-cardinality contract at AAP §0.5.1.6
             holds uniformly across all four injected types (the label
             value is the exception *class name*, not the failing
             GAME_ID).
          5. Emit exactly one WARNING log with the Rule 6 format string
             ``"game %s failed: %s"`` carrying the failing GAME_ID.

        This test is the dedicated regression shield against a refactor
        that narrows the handler from ``except Exception`` to a specific
        subclass (e.g., ``except RuntimeError``) —
        :meth:`test_single_game_failure_continues_iteration` covers only
        the ``RuntimeError`` case, so a narrowing refactor could pass
        that test while silently breaking ``KeyError``/``IndexError``/
        ``ValueError`` handling. This parametrized test catches that.
        """
        # --- Arrange ---------------------------------------------------
        _patch_enumerate(monkeypatch, _GAME_IDS)
        failing_gid = _GAME_IDS[1]
        # ``_SelectiveFailureClient`` accepts ``exception_factory`` so
        # each parametrization injects a different exception class at
        # the transport seam without bypassing the ``client.get``
        # boundary — mirroring how a real upstream failure (HTTP error,
        # JSON decode error, key-lookup on a malformed envelope) would
        # surface to the pipeline.
        client = _SelectiveFailureClient(
            responses={
                _BOXSCORE_ENDPOINT: sample_multi_table_payload,
                _PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload,
            },
            failing_game_ids=[failing_gid],
            exception_factory=exception_cls,
        )
        writer = recording_writer(tmp_path)
        checkpoint = recording_checkpoint()
        metrics_mock = MagicMock()
        logger_mock = MagicMock()

        # --- Act — MUST NOT RAISE regardless of exception type ---------
        try:
            ingest_games.run(
                client=client,
                writer=writer,
                checkpoint=checkpoint,
                season=_SEASON,
                logger=logger_mock,
                metrics=metrics_mock,
            )
        except Exception as exc:
            pytest.fail(
                f"Rule 6 violated: {exception_cls.__name__} "
                f"propagated to caller instead of being caught by "
                f"the per-game try/except: {exc!r}"
            )

        # --- Assert: successful games still processed ------------------
        marked_game_ids = {
            key
            for (domain, key) in checkpoint.marks
            if domain == config.DOMAIN_GAMES
        }
        assert marked_game_ids == {_GAME_IDS[0], _GAME_IDS[2]}, (
            f"successful games not checkpointed despite "
            f"{exception_cls.__name__} failure on middle game; "
            f"marks={checkpoint.marks!r}"
        )
        # --- Assert: failing game NOT marked --------------------------
        assert failing_gid not in marked_game_ids, (
            f"failing GAME_ID {failing_gid!r} was checkpointed even "
            f"though {exception_cls.__name__} was raised in the try "
            f"block; marks={checkpoint.marks!r}"
        )

        # --- Assert: writer invoked only for successful games ---------
        # 2 artifacts × 2 successful games = 4 writes (same shape as
        # the ``test_single_game_failure_continues_iteration`` case).
        assert len(writer.writes) == 4, (
            f"expected 4 writes from successful games under "
            f"{exception_cls.__name__} failure; got {len(writer.writes)}: "
            f"{[w['name'] for w in writer.writes]!r}"
        )
        for w in writer.writes:
            assert w["season"] == _SEASON
            assert w["name"] in {config.CSV_GAMES, config.CSV_PLAY_BY_PLAY}

        # --- Assert: games_failed_total carries class-name label ------
        failed_inc_calls = [
            c
            for c in metrics_mock.inc.call_args_list
            if c.args and c.args[0] == "games_failed_total"
        ]
        assert len(failed_inc_calls) == 1, (
            f"expected exactly 1 games_failed_total inc for "
            f"{exception_cls.__name__}; got {len(failed_inc_calls)}: "
            f"{failed_inc_calls!r}"
        )
        # Bounded-cardinality contract: label ``reason`` is the
        # exception CLASS name — NEVER the failing GAME_ID. Exact
        # equality (not subset) ensures no spurious labels accrete over
        # time (AAP §0.5.1.6).
        assert failed_inc_calls[0].args[1] == {
            "reason": exception_cls.__name__
        }, (
            f"games_failed_total label must be "
            f"{{'reason': {exception_cls.__name__!r}}} "
            f"(bounded cardinality — AAP §0.5.1.6); "
            f"got {failed_inc_calls[0].args[1]!r}"
        )
        # No ``n=`` kwarg — the default increment of 1 is implicit.
        assert "n" not in failed_inc_calls[0].kwargs, (
            f"games_failed_total must be incremented positionally "
            f"without ``n=`` kwarg; got kwargs="
            f"{failed_inc_calls[0].kwargs!r}"
        )

        # --- Assert: exactly one WARNING log with correct format ------
        warning_calls = logger_mock.warning.call_args_list
        assert len(warning_calls) == 1, (
            f"expected exactly 1 WARNING log for "
            f"{exception_cls.__name__}; got {len(warning_calls)}: "
            f"{warning_calls!r}"
        )
        fmt, *args = warning_calls[0].args
        assert fmt == "game %s failed: %s", (
            f"Rule 6 WARNING format string mismatch under "
            f"{exception_cls.__name__}; expected 'game %%s failed: %%s'; "
            f"got {fmt!r}"
        )
        assert args[0] == failing_gid, (
            f"WARNING first positional arg must be failing GAME_ID; "
            f"expected {failing_gid!r}; got {args[0]!r}"
        )

    # -----------------------------------------------------------------
    # Rule 6 — blanket failure (all games fail)
    # -----------------------------------------------------------------

    def test_all_games_fail_does_not_abort_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_client,
        recording_writer,
        recording_checkpoint,
        sample_playbyplay_payload: Dict[str, Any],
        tmp_path,
    ) -> None:
        """Blanket failure: every game raises ⇒ pipeline completes gracefully.

        Uses the conftest :class:`RecordingClient.raise_for` mechanism
        (raises on *every* call to a given endpoint regardless of
        params) to simulate a transient upstream outage where
        ``boxscoretraditionalv2`` is entirely unreachable.

        The pipeline must:
          - not re-raise (Rule 6 fail-safe active for *every* game)
          - never call the writer (boxscore fetch fails before any
            write is attempted)
          - never mark any game completed
          - increment ``games_failed_total`` exactly 3 times (once
            per GAME_ID). All three increments share the *same*
            label dict ``{"reason": "RuntimeError"}`` because
            ``raise_for`` injects a single ``RuntimeError`` class
            for every game — this verifies the bounded-cardinality
            property of the ``reason`` label: the set of label
            *values* is the finite set of exception *types*, NOT
            the unbounded set of failing GAME_IDs. The three
            distinct failing GAME_IDs are instead surfaced through
            the three WARNING log records' positional arguments
            (see the log-record assertion below).
          - emit exactly 3 WARNING log records whose positional
            GAME_ID arguments form the full set ``set(_GAME_IDS)``
            (this is where distinct-GID evidence now lives after
            the ``game_id``→``reason`` label rename).
          - still emit the final ``pipeline.complete`` log (reached
            because Rule 6 ``continue`` short-circuits each game).
        """
        # --- Arrange ---------------------------------------------------
        _patch_enumerate(monkeypatch, _GAME_IDS)
        client = recording_client(
            responses={_PLAYBYPLAY_ENDPOINT: sample_playbyplay_payload},
            raise_for={
                _BOXSCORE_ENDPOINT: RuntimeError(
                    "simulated boxscoretraditionalv2 outage"
                ),
            },
        )
        writer = recording_writer(tmp_path)
        checkpoint = recording_checkpoint()
        metrics_mock = MagicMock()
        logger_mock = MagicMock()

        # --- Act — MUST NOT RAISE -------------------------------------
        try:
            ingest_games.run(
                client=client,
                writer=writer,
                checkpoint=checkpoint,
                season=_SEASON,
                logger=logger_mock,
                metrics=metrics_mock,
            )
        except Exception as exc:
            pytest.fail(
                f"Rule 6 violated: blanket failure propagated to caller: "
                f"{exc!r}"
            )

        # --- Assert: only boxscore was attempted (3×) ----------------
        # PBP should never be reached because boxscore raises first.
        boxscore_calls = [c for c in client.calls if c[0] == _BOXSCORE_ENDPOINT]
        pbp_calls = [c for c in client.calls if c[0] == _PLAYBYPLAY_ENDPOINT]
        assert len(boxscore_calls) == 3, (
            f"expected boxscore attempted for every game (3×); "
            f"got {len(boxscore_calls)}"
        )
        assert pbp_calls == [], (
            f"PBP must never be reached when boxscore fails first; "
            f"got {pbp_calls!r}"
        )

        # --- Assert: writer never invoked ----------------------------
        assert writer.writes == [], (
            f"writer must not be called when all games fail; "
            f"got {writer.writes!r}"
        )

        # --- Assert: no checkpoint marks -----------------------------
        assert checkpoint.marks == [], (
            f"no game was successful ⇒ no marks expected; "
            f"got {checkpoint.marks!r}"
        )

        # --- Assert: games_failed_total incremented 3× ---------------
        # Bounded-cardinality property: all three increments carry the
        # *identical* label dict ``{"reason": "RuntimeError"}`` because
        # a single ``RuntimeError`` class was injected by ``raise_for``
        # for all three games. The metric's label set therefore
        # collapses to a single value regardless of how many distinct
        # games fail — this is the defining property of the
        # ``reason``-labelled contract (AAP §0.5.1.6) and protects
        # the metric from cardinality explosion. Distinct failing
        # GAME_IDs are verified separately below via the WARNING log
        # positional arguments.
        failed_inc_calls = [
            c
            for c in metrics_mock.inc.call_args_list
            if c.args and c.args[0] == "games_failed_total"
        ]
        assert len(failed_inc_calls) == 3, (
            f"expected 3 games_failed_total inc calls; "
            f"got {len(failed_inc_calls)}: {failed_inc_calls!r}"
        )
        # All three increments must carry the identical label dict.
        # Since every game raises the same ``RuntimeError`` class, the
        # set of distinct label dicts collapses to exactly one element.
        failed_label_dicts = [c.args[1] for c in failed_inc_calls]
        expected_label = {"reason": "RuntimeError"}
        assert all(labels == expected_label for labels in failed_label_dicts), (
            f"every games_failed_total increment must carry the identical "
            f"label dict {expected_label!r} (bounded-cardinality property "
            f"of the ``reason`` label); got {failed_label_dicts!r}"
        )
        # Sanity check: the set of distinct labels collapses to 1.
        distinct_labels = {tuple(sorted(d.items())) for d in failed_label_dicts}
        assert len(distinct_labels) == 1, (
            f"all three increments should collapse to 1 distinct label tuple "
            f"(single shared exception class); got {len(distinct_labels)}: "
            f"{distinct_labels!r}"
        )
        # No ``n=`` kwarg — default increment of 1 per call.
        for c in failed_inc_calls:
            assert "n" not in c.kwargs, (
                f"games_failed_total must be incremented positionally "
                f"without ``n=`` kwarg; got kwargs={c.kwargs!r}"
            )

        # --- Assert: 3 WARNING logs with Rule 6 format ---------------
        # After the ``game_id``→``reason`` label rename, the WARNING
        # log records are the canonical place distinct failing
        # GAME_IDs are surfaced to operators. Extract the three
        # failing GAME_IDs from the positional arguments here — this
        # is the replacement evidence for the old ``failed_gids``
        # assertion that read the set from the metric labels.
        warning_calls = logger_mock.warning.call_args_list
        assert len(warning_calls) == 3, (
            f"expected 3 WARNING logs (one per failing game); "
            f"got {len(warning_calls)}"
        )
        warning_gids = set()
        for call in warning_calls:
            fmt = call.args[0]
            assert fmt == "game %s failed: %s", (
                f"Rule 6 WARNING format mismatch; got {fmt!r}"
            )
            # First positional arg after format is the GAME_ID.
            gid = call.args[1]
            assert gid in _GAME_IDS, (
                f"WARNING GAME_ID arg must be one of {_GAME_IDS!r}; "
                f"got {gid!r}"
            )
            warning_gids.add(gid)
        # Distinct-GID evidence: all three failing GAME_IDs appear
        # across the WARNING log records (this is the property the
        # old metric-label-based ``failed_gids`` assertion checked,
        # now relocated to the log stream where per-GID context
        # belongs).
        assert warning_gids == set(_GAME_IDS), (
            f"WARNING log GAME_IDs must cover all failing games; "
            f"expected {set(_GAME_IDS)!r}; got {warning_gids!r}"
        )

    # -----------------------------------------------------------------
    # Rule 6 scope — enumerate_game_ids failure propagates
    # -----------------------------------------------------------------

    def test_enumerate_game_ids_failure_propagates_past_rule6(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_client,
        recording_writer,
        recording_checkpoint,
        tmp_path,
    ) -> None:
        """Rule 6 scope: ``enumerate_game_ids`` failures are NOT swallowed.

        AAP §0.7.2.6 scopes the Rule 6 ``try/except`` to the per-game
        body *only*. Per the ingest_games.py docstring (lines 375-389,
        "Fatal conditions (propagated — NOT wrapped by Rule 6)"), a
        failure in :func:`endpoints.schedule.enumerate_game_ids` must
        propagate to the caller unchanged — the pipeline has no work
        to do when the schedule cannot be enumerated, so retrying on
        the next run is the correct recovery.

        This test simulates that scenario by monkey-patching
        ``enumerate_game_ids`` to raise :class:`RuntimeError` and
        asserts that:
          - the exception reaches the caller verbatim (with identical
            message for traceability)
          - no client calls, writes, or marks occur
          - no WARNING log is emitted (Rule 6's ``log.warning`` is
            per-game, not pipeline-level)
          - ``games_failed_total`` is NOT incremented (it, too, is
            per-game).
        """

        # --- Arrange ---------------------------------------------------
        def _boom(_client, _season):
            raise RuntimeError("simulated enumerate_game_ids transport failure")

        monkeypatch.setattr(
            "pipelines.ingest_games.enumerate_game_ids", _boom
        )
        client = recording_client()
        writer = recording_writer(tmp_path)
        checkpoint = recording_checkpoint()
        metrics_mock = MagicMock()
        logger_mock = MagicMock()

        # --- Act — MUST RAISE (Rule 6 does NOT wrap this scope) ------
        with pytest.raises(RuntimeError) as excinfo:
            ingest_games.run(
                client=client,
                writer=writer,
                checkpoint=checkpoint,
                season=_SEASON,
                logger=logger_mock,
                metrics=metrics_mock,
            )
        assert "simulated enumerate_game_ids transport failure" in str(
            excinfo.value
        ), (
            f"expected the original RuntimeError message to propagate "
            f"unchanged; got {excinfo.value!r}"
        )

        # --- Assert: no downstream side effects observed -------------
        assert client.calls == [], (
            f"no HTTP work should occur when enumeration fails; "
            f"got {client.calls!r}"
        )
        assert writer.writes == [], (
            f"no writes should occur when enumeration fails; "
            f"got {writer.writes!r}"
        )
        assert checkpoint.marks == [], (
            f"no marks should occur when enumeration fails; "
            f"got {checkpoint.marks!r}"
        )
        # No per-game WARNING — enumeration failure is pipeline-level,
        # not per-game.
        assert logger_mock.warning.call_args_list == [], (
            f"no per-game WARNING expected; "
            f"got {logger_mock.warning.call_args_list!r}"
        )
        # No games_failed_total bump — Rule 6 is scoped to per-game.
        failed_inc_calls = [
            c
            for c in metrics_mock.inc.call_args_list
            if c.args and c.args[0] == "games_failed_total"
        ]
        assert failed_inc_calls == [], (
            f"games_failed_total must NOT be incremented when the failure "
            f"is outside the per-game body; got {failed_inc_calls!r}"
        )
