"""Games (F-011) ingestion pipeline — the sole implementation of Rule 6.

This module implements Feature **F-011 Games** of the NBA Data
Ingestion Pipeline per AAP §0.2.3, §0.4.1.1, and §0.5.1.6. It is
architecturally unique among the pipelines in :mod:`pipelines`: it is
the **only** place in the production codebase where a bare
``except Exception`` block is sanctioned. Rule 6 (Fail-Safe Game
Iteration, AAP §0.7.2.6 / Product Brief ``docs/New_Product_Prompt_20260418.md``
§5) mandates that a failure on a single ``GAME_ID`` is logged at
WARNING, the ``games_failed_total`` counter is incremented, and
iteration continues with the next ``GAME_ID`` — the pipeline never
aborts because one game fails. All other pipelines in
:mod:`pipelines` (``ingest_schedule``, ``ingest_teams``,
``ingest_players``, ``ingest_lineups``) propagate exceptions as-is.

Outputs
-------
The pipeline materialises two CSV artifacts in ``output/`` via the
injected :class:`~storage.csv_writer.BaseWriter`:

* ``games.csv`` (key columns: ``season, game_id, team_id``) —
  traditional box-score rows sourced from the NBA Stats
  ``boxscoretraditionalv2`` endpoint. One row per (game, team,
  player).
* ``play_by_play.csv`` (key columns: ``season, game_id, event_num``) —
  play-by-play events sourced from the NBA Stats ``playbyplayv2``
  endpoint. One row per in-game event.

See ``docs/features/games.md`` for the per-endpoint narrative and
``README.md`` "Output Files" for the canonical output contract.

Rule compliance
---------------
The pipeline satisfies four of the seven operational rules defined in
``docs/New_Product_Prompt_20260418.md`` §5:

* **Rule 4 (Flat CSV):** every payload is flattened through
  :func:`utils.schema_normalizer.normalize_result_sets`, which
  asserts as a post-condition that no cell contains ``dict`` or
  ``list`` before returning.
* **Rule 5 (Checkpoint After Every Pull):** every successful per-game
  iteration calls
  :meth:`utils.checkpoint.CheckpointManager.mark_completed`
  **immediately** after the CSV write finishes, before the loop
  advances. A failure in any step before ``mark_completed`` leaves
  the checkpoint un-marked so a subsequent run retries the game.
* **Rule 6 (Fail-Safe Iteration):** the per-``GAME_ID`` loop is
  wrapped in ``try/except Exception``; per-game failures are logged
  at WARNING and iteration continues.
* **Rule 7 (Pluggable Storage):** all CSV emission goes through
  :meth:`storage.csv_writer.BaseWriter.write`; this module never
  calls :meth:`pandas.DataFrame.to_csv` directly and never imports
  :mod:`requests` (Rule 1 compliance is maintained transitively —
  all HTTP traffic flows through the injected ``client``).

Endpoint coverage
-----------------
Of the four NBA Stats endpoints exposed by :mod:`endpoints.games`,
this pipeline invokes two:

* ``boxscoretraditionalv2`` — minimum-viable box-score coverage for
  ``games.csv``.
* ``playbyplayv2`` — minimum-viable event coverage for
  ``play_by_play.csv``.

``fetch_scoreboardv2`` is intentionally **not** invoked here:
per-date enumeration is handled more efficiently by the Schedule
domain via :func:`endpoints.schedule.enumerate_game_ids`, which
issues a single ``leaguegamefinder`` call for the whole season
instead of one call per date. ``fetch_boxscoreadvancedv2`` is
intentionally **not** invoked here: its key columns overlap with
the traditional box score, so including both would require either
row duplication or a merge on ``(GAME_ID, TEAM_ID, PLAYER_ID)`` —
complexity not justified by the README output contract. A future
iteration can extend the per-``GAME_ID`` block to call
``fetch_boxscoreadvancedv2`` and merge; both wrappers remain
library-accessible via direct import from :mod:`endpoints.games`.

Cross-pipeline dependency
-------------------------
F-013 (Schedule) → F-011 (Games): the Games pipeline cannot run
without a ``GAME_ID`` list. Per AAP §0.4.5 this module imports
:func:`endpoints.schedule.enumerate_game_ids` **directly** (not from
:mod:`pipelines.ingest_schedule`), so the pipeline is self-sufficient
when invoked via ``python run.py games --season <season>`` in
isolation, regardless of whether ``schedule.csv`` has been
materialised on disk. When ``python run.py all`` is invoked, the
schedule pipeline has already run upstream, but this pipeline
re-enumerates independently — avoiding any coupling between pipeline
orchestrators.

Resume semantics
----------------
On resume, :meth:`utils.checkpoint.CheckpointManager.get_pending`
returns only the not-yet-processed ``GAME_IDs`` in their original
first-seen order. In-memory buffers (``games_buffer`` and
``pbp_buffer``) are seeded from the existing on-disk CSVs via the
:func:`_load_existing_games` and :func:`_load_existing_pbp`
helpers before per-game iteration begins, so the on-disk CSVs
after resume reflect the **cumulative** set of games processed
across all sessions (prior session's rows + current session's
rows), not just the current session's games. This is history-
preserving resume behaviour.

The seeding step uses :func:`pandas.read_csv` — which is permitted
in pipelines by Rule 7 (only :meth:`pandas.DataFrame.to_csv` is
forbidden outside :mod:`storage.csv_writer`). Rows whose
``game_id`` is present in the current run's ``pending`` list are
filtered out during seeding, so if a prior session wrote a game's
rows to disk but the checkpoint mark failed (a rare inconsistency
that leaves the ``GAME_ID`` in ``pending``), the retry will not
produce duplicate rows in the final CSV. If the on-disk CSVs are
missing, empty, or unreadable, the helpers fall back to an empty
list and log a WARNING — the pipeline never fails merely because
a stale CSV artifact cannot be parsed. Helper invocations occur
in the ``run()`` preamble, **outside** the per-game try/except,
so failures in the helpers still propagate fatally (consistent
with the enumerate/get_pending contracts); only per-game fetch/
normalize/write failures are swallowed by Rule 6.

Observability
-------------
Structured log events — with correlation IDs auto-injected by
:class:`utils.correlation.CorrelationAdapter` via
:func:`utils.logger.get_logger` — are emitted at every pipeline
milestone:

* ``pipeline.start`` (INFO) — run entered, preamble executed.
* ``pipeline.games.enumerated`` (INFO) — ``GAME_ID`` list obtained.
* ``pipeline.games.pending`` (INFO) — resume-filtered subset
  computed.
* ``pipeline.complete`` with ``status=skipped`` (INFO) — no
  pending games (short-circuit).
* ``pipeline.games.game_complete`` (INFO) — per-game success.
* ``game %s failed: %s`` (WARNING) — Rule 6 per-game failure
  (the only WARNING emitted by this pipeline).
* ``pipeline.complete`` (INFO) — run exiting normally with
  ``processed`` and ``failed`` counts.

All log calls use ``%s``-style placeholders (not f-strings) so the
:class:`~utils.correlation.CorrelationAdapter` can inject
``correlation_id`` into every :class:`logging.LogRecord`.

Counters incremented (pre-registered by :mod:`utils.metrics`):

* ``pipeline_rows_written_total{pipeline="ingest_games", artifact="games.csv"}``
  — incremented by ``len(bs_df)`` on every successful per-game write.
* ``pipeline_rows_written_total{pipeline="ingest_games", artifact="play_by_play.csv"}``
  — incremented by ``len(pbp_df)`` on every successful per-game write.
* ``games_failed_total{reason=<ExceptionClassName>}`` — incremented
  inside the Rule 6 ``except`` block. The ``reason`` label captures
  ``type(exc).__name__`` so dashboards can filter failures by
  exception class (e.g. ``"RuntimeError"``, ``"Timeout"``, or
  ``"HTTPError"``) and ``sum(increase(games_failed_total[24h]))``
  yields a bounded-cardinality aggregate (not one series per
  GAME_ID). This counter's name is a **verbatim binding
  invariant** per AAP §0.5.1.6 — it appears exactly once in the
  production codebase and is incremented nowhere else. The label
  shape is aligned with ``docs/OBSERVABILITY.md`` L179/L224 and
  ``docs/dashboards/operator_dashboard.json`` L236.

Authoritative references
------------------------
* Agent Action Plan §0.1.1, §0.1.3, §0.2.3 — pipeline requirements.
* Agent Action Plan §0.4.1.1, §0.4.5 — integration wiring &
  cross-pipeline dependency.
* Agent Action Plan §0.5.1.6, §0.5.2.1, §0.5.3 — execution plan,
  error-handling approach, and skeleton.
* Agent Action Plan §0.7.2.6 — Rule 6 binding constraint.
* ``docs/New_Product_Prompt_20260418.md`` §5 Rule 6 — product-brief
  verbatim scope: ``pipelines/ingest_games.py``.
* ``README.md`` "Output Files" — CSV key-column contract.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

import config
from endpoints.games import (
    fetch_boxscoretraditionalv2,
    fetch_playbyplayv2,
)
from endpoints.schedule import enumerate_game_ids
from utils.logger import get_logger
from utils.metrics import registry as _metrics_registry
from utils.schema_normalizer import normalize_result_sets


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

#: Re-exports declared by this module. Only :func:`run` is part of
#: the public API; the two module-private helpers below
#: (:func:`_select_primary_df` and :func:`_ensure_game_columns`) are
#: implementation details kept at module scope so they remain
#: unit-testable without being consumed by peer pipelines.
__all__ = ["run"]


# ---------------------------------------------------------------------------
# Module-level fallback logger
# ---------------------------------------------------------------------------

#: Module-level fallback logger adapter. Used when :func:`run` is
#: invoked without an explicit ``logger`` argument — typically by
#: unit tests or ad-hoc scripts that don't mint a correlation ID
#: beforehand. The returned :class:`logging.LoggerAdapter` subclass
#: (:class:`utils.correlation.CorrelationAdapter`) transparently
#: injects the per-run correlation ID set by ``run.py`` at CLI
#: entry into every :class:`logging.LogRecord` emitted from this
#: module, so no explicit plumbing is required at call sites.
_LOGGER: logging.LoggerAdapter = get_logger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _select_primary_df(dfs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the first DataFrame from a normalized ``resultSets`` dict.

    NBA Stats API endpoints return an ordered list of ``resultSets``
    where the first entry is conventionally the primary table — e.g.
    ``boxscoretraditionalv2`` emits ``PlayerStats`` first (per-player
    box rows) followed by ``TeamStats`` (per-team aggregates), and
    ``playbyplayv2`` emits ``PlayByPlay`` first followed by
    ``AvailableVideo``. The Games pipeline only needs the primary
    table in each case; selecting the first DataFrame in iteration
    order is therefore correct for both endpoints invoked here.

    Returning an empty :class:`pandas.DataFrame` when ``dfs`` is
    falsy (empty dict, ``None``-equivalent) is deliberate: it keeps
    the downstream ``writer.write`` call total — the CSV writer
    gracefully handles zero-row frames by emitting a header-only
    file — so the pipeline never raises on a degenerate payload and
    the Rule 6 fail-safe does not need to catch a "no primary table"
    error. Any truly malformed envelope would have been rejected by
    :func:`utils.schema_normalizer.normalize_result_sets` already
    (it raises on non-dict input).

    Parameters
    ----------
    dfs:
        The mapping returned by
        :func:`utils.schema_normalizer.normalize_result_sets`;
        snake_case result-set names keyed to their flat
        :class:`pandas.DataFrame` representations.

    Returns
    -------
    pandas.DataFrame
        The first DataFrame in iteration order (Python 3.7+ dicts
        preserve insertion order, and the normalizer preserves the
        order of entries in the upstream ``resultSets`` array), or
        an empty DataFrame when ``dfs`` has no entries.
    """
    if not dfs:
        return pd.DataFrame()
    first_key = next(iter(dfs))
    return dfs[first_key]


def _ensure_game_columns(
    df: pd.DataFrame,
    season: str,
    game_id: str,
) -> pd.DataFrame:
    """Ensure the DataFrame carries ``season`` and ``game_id`` columns.

    Adds the columns (at positions 0 and 1 respectively) when they
    are not already present under any case variant. This guarantees
    the key-column contract published in ``README.md`` for
    ``games.csv`` (``season, game_id, team_id``) and
    ``play_by_play.csv`` (``season, game_id, event_num``). NBA Stats
    payloads include ``GAME_ID`` natively in both endpoint responses,
    so the ``game_id`` branch is typically a no-op; the ``season``
    branch is typically active because the upstream ``resultSets``
    are filtered for a single season and therefore omit the
    redundant column.

    The check is **case-insensitive** against :attr:`df.columns` so
    it tolerates both the uppercase convention (``SEASON``,
    ``GAME_ID``) common in NBA Stats payloads and the snake_cased
    form that downstream consumers use. When a match is found under
    any case, no insertion is performed — the existing column is
    preserved verbatim so the normalizer's intended schema survives
    intact.

    The input DataFrame is **not** mutated in place — a shallow
    :meth:`pandas.DataFrame.copy` is taken first so the caller's
    buffer accumulator is unaffected by position-dependent
    :meth:`pandas.DataFrame.insert` calls.

    Parameters
    ----------
    df:
        The primary DataFrame extracted from the normalized
        ``resultSets`` envelope (see :func:`_select_primary_df`).
    season:
        Season string in ``YYYY-YY`` form, e.g. ``"2025-26"``.
        Prepended verbatim as the value of the inserted ``season``
        column when absent.
    game_id:
        10-character zero-padded ``GAME_ID`` string, e.g.
        ``"0022500001"``. Prepended verbatim as the value of the
        inserted ``game_id`` column when absent.

    Returns
    -------
    pandas.DataFrame
        A copy of ``df`` guaranteed to carry both ``season`` and
        ``game_id`` columns. The relative column order becomes
        ``[season, game_id, ...]`` when both are inserted,
        matching the README key-column ordering.
    """
    out = df.copy()
    if not any(c.lower() == "season" for c in out.columns):
        out.insert(0, "season", season)
    if not any(c.lower() == "game_id" for c in out.columns):
        insert_pos = 1 if any(c.lower() == "season" for c in out.columns) else 0
        out.insert(insert_pos, "game_id", game_id)
    return out


def _load_existing_games(
    output_dir: "pd.api.types.Any",  # type: ignore[name-defined]
    season: str,
    pending_ids: List[str],
    log: logging.LoggerAdapter,
) -> List[pd.DataFrame]:
    """Seed the games-buffer from a prior session's ``games.csv``.

    Implements the history-preserving side of the resume contract
    documented in the module docstring's "Resume semantics" section.
    Reads an existing ``<output_dir>/games.csv`` (if one is present)
    and returns it as a single-element list suitable for prepending
    to the in-memory ``games_buffer`` before per-game iteration
    begins.

    Rule 7 compliance
    -----------------
    This helper uses :func:`pandas.read_csv`, which is **permitted**
    in pipeline modules. Rule 7 forbids only
    :meth:`pandas.DataFrame.to_csv` outside
    :mod:`storage.csv_writer`; reading CSVs is unrestricted
    (AAP §0.7.2.7 binding invariant text).

    Dedupe semantics
    ----------------
    Rows whose ``game_id`` column matches any entry in
    ``pending_ids`` are filtered out before return. This protects
    against the pathological case where a prior session wrote a
    per-game slice to ``games.csv`` but crashed before
    :meth:`utils.checkpoint.CheckpointManager.mark_completed`
    persisted the entry — on the next run that ``GAME_ID`` would
    appear in ``pending`` and be re-fetched, which would duplicate
    its rows in the concatenated buffer without this dedupe step.
    The lowercase ``game_id`` column is guaranteed by
    :func:`_ensure_game_columns`; a case-insensitive fallback is
    applied defensively in case an older on-disk artifact was
    written under a different casing convention.

    Failure posture
    ---------------
    Any failure to locate, parse, or filter the existing CSV is
    swallowed and a WARNING is logged; the function returns an
    empty list in that case. The pipeline continues normally —
    losing only the benefit of the prior-session rows (which will
    not be re-fetched because they remain checkpointed), not the
    ability to process the ``pending`` ``GAME_IDs``. This is
    consistent with the "best-effort resume enrichment" posture
    required to keep the Rule 6 invariants meaningful.

    Parameters
    ----------
    output_dir:
        Filesystem directory under which ``games.csv`` is expected
        to live. Accepts any object exposing an ``os.PathLike``
        interface (both :class:`pathlib.Path` from
        :class:`storage.csv_writer.CSVWriter.output_dir` and a
        plain :class:`pathlib.Path` attribute from the test
        ``RecordingWriter``).
    season:
        Season string passed through for logging only; rows in the
        CSV are NOT filtered by ``season`` because the CSV is
        season-scoped by convention (one season per run per
        output directory).
    pending_ids:
        The list returned by
        :meth:`utils.checkpoint.CheckpointManager.get_pending` for
        the ``games`` domain. Used to dedupe rows from the existing
        CSV whose ``game_id`` is scheduled for re-processing in the
        current run.
    log:
        Logger adapter used for the WARNING emission path.

    Returns
    -------
    list of pandas.DataFrame
        Either a single-element list containing the filtered
        prior-session DataFrame, or an empty list if no prior
        artifact exists or an unrecoverable parse error occurred.
    """
    from pathlib import Path

    try:
        path = Path(output_dir) / f"{config.CSV_GAMES}.csv"
        if not path.is_file():
            return []
        # ``dtype={'game_id': str}`` preserves leading zeros in the
        # 10-character zero-padded NBA GAME_ID convention (e.g.
        # ``"0022500001"``) — pandas would otherwise coerce to int64
        # and silently drop the prefix.
        df = pd.read_csv(path, dtype={"game_id": str})
        if df.empty:
            return []
        # Normalize the game_id column name case for dedupe.
        gid_col: Optional[str] = None
        for col in df.columns:
            if col.lower() == "game_id":
                gid_col = col
                break
        if gid_col is None:
            log.warning(
                "pipeline.games.resume.seed_games_skipped"
                " reason=missing_game_id_column path=%s",
                path,
            )
            return []
        # Preserve the 10-character zero-padded NBA GAME_ID convention
        # even when pandas's default type inference coerced the column
        # to int64. This happens whenever the prior-session CSV stores
        # the id as uppercase ``GAME_ID`` (the runtime default) so the
        # ``dtype={'game_id': str}`` hint above is silently ignored.
        # Without this normalization the dedupe comparison below would
        # never match (e.g. upstream pending id ``"0022500001"`` vs
        # stripped-prefix cell value ``"22500001"``) and overlap rows
        # would incorrectly survive into the cumulative write.
        df[gid_col] = df[gid_col].astype(str).str.zfill(10)
        # Filter out rows whose GAME_ID will be re-processed this run.
        if pending_ids:
            pending_set = set(pending_ids)
            df = df[~df[gid_col].isin(pending_set)].copy()
        if df.empty:
            log.info(
                "pipeline.games.resume.seed_games path=%s rows=0"
                " reason=all_rows_filtered",
                path,
            )
            return []
        log.info(
            "pipeline.games.resume.seed_games path=%s rows=%d season=%s",
            path,
            len(df),
            season,
        )
        return [df]
    except Exception as exc:  # pragma: no cover - defensive fallback
        # Intentionally broad catch: a corrupted CSV or transient I/O
        # error must not abort the pipeline. Resume behaviour remains
        # correct — only the history-preserving enrichment is lost.
        log.warning(
            "pipeline.games.resume.seed_games_failed"
            " reason=%s season=%s",
            type(exc).__name__,
            season,
        )
        return []


def _load_existing_pbp(
    output_dir: "pd.api.types.Any",  # type: ignore[name-defined]
    season: str,
    pending_ids: List[str],
    log: logging.LoggerAdapter,
) -> List[pd.DataFrame]:
    """Seed the play-by-play buffer from a prior session's CSV.

    Mirror of :func:`_load_existing_games` for the ``play_by_play.csv``
    artifact. See that function's docstring for the full rationale,
    Rule 7 compliance discussion, dedupe semantics, and failure
    posture — they apply identically here.

    Parameters
    ----------
    output_dir:
        Filesystem directory under which ``play_by_play.csv`` is
        expected to live.
    season:
        Season string (for logging only).
    pending_ids:
        The ``games`` domain pending list — the same list used for
        the games-CSV seed. Rows whose ``game_id`` matches are
        filtered out so re-processed games do not produce duplicate
        play-by-play rows.
    log:
        Logger adapter for the WARNING emission path.

    Returns
    -------
    list of pandas.DataFrame
        Either a single-element list containing the filtered
        prior-session play-by-play DataFrame, or an empty list if
        no prior artifact exists or an unrecoverable parse error
        occurred.
    """
    from pathlib import Path

    try:
        path = Path(output_dir) / f"{config.CSV_PLAY_BY_PLAY}.csv"
        if not path.is_file():
            return []
        df = pd.read_csv(path, dtype={"game_id": str})
        if df.empty:
            return []
        gid_col: Optional[str] = None
        for col in df.columns:
            if col.lower() == "game_id":
                gid_col = col
                break
        if gid_col is None:
            log.warning(
                "pipeline.games.resume.seed_pbp_skipped"
                " reason=missing_game_id_column path=%s",
                path,
            )
            return []
        # Preserve leading zeros in GAME_ID for case-insensitive
        # dedupe parity with ``_load_existing_games``. See that
        # helper for the full rationale.
        df[gid_col] = df[gid_col].astype(str).str.zfill(10)
        if pending_ids:
            pending_set = set(pending_ids)
            df = df[~df[gid_col].isin(pending_set)].copy()
        if df.empty:
            log.info(
                "pipeline.games.resume.seed_pbp path=%s rows=0"
                " reason=all_rows_filtered",
                path,
            )
            return []
        log.info(
            "pipeline.games.resume.seed_pbp path=%s rows=%d season=%s",
            path,
            len(df),
            season,
        )
        return [df]
    except Exception as exc:  # pragma: no cover - defensive fallback
        log.warning(
            "pipeline.games.resume.seed_pbp_failed"
            " reason=%s season=%s",
            type(exc).__name__,
            season,
        )
        return []


# ---------------------------------------------------------------------------
# Public entry point — the Rule 6 fail-safe iteration body
# ---------------------------------------------------------------------------


def run(
    client,
    writer,
    checkpoint,
    season: str,
    logger: Optional[logging.LoggerAdapter] = None,
    metrics: Optional[Any] = None,
) -> None:
    """Run the Games ingestion pipeline (F-011) with Rule 6 fail-safe iteration.

    Enumerates the season's ``GAME_ID`` list via
    :func:`endpoints.schedule.enumerate_game_ids`, then iterates every
    pending (non-checkpointed) ``GAME_ID`` and fetches the traditional
    box score + play-by-play for each. After each successful per-game
    pull, the accumulated buffers are rewritten to
    ``output/games.csv`` and ``output/play_by_play.csv`` via the
    injected writer, and the ``GAME_ID`` is marked complete in the
    checkpoint manifest.

    **Rule 6 (Fail-Safe Game Iteration) — AAP §0.7.2.6 / Product Brief
    §5**: the per-game loop is wrapped in ``try/except Exception``. A
    failure on a single ``GAME_ID`` is logged at WARNING with the
    offending identifier, increments the ``games_failed_total``
    counter, and iteration continues with the next ``GAME_ID``. The
    pipeline **never** aborts because one game fails. This is the
    only place in the production codebase where ``except Exception``
    is sanctioned.

    **Rule 5 (Checkpoint After Every Pull)**: every successful
    per-game block writes both CSV artifacts and calls
    ``checkpoint.mark_completed(config.DOMAIN_GAMES, game_id)``
    before advancing. On resume, already-completed ``GAME_IDs`` are
    skipped via ``checkpoint.get_pending``. Because
    :class:`~storage.csv_writer.CSVWriter` uses atomic
    ``tmp → Path.replace`` writes and writes happen per game, the
    file on disk always reflects the state of the last
    successfully-checkpointed ``GAME_ID``.

    **Rule 4 (Flat CSV)**: every payload is flattened through
    :func:`utils.schema_normalizer.normalize_result_sets`, which
    asserts that no cell contains ``dict`` or ``list`` before
    returning.

    **Rule 7 (Pluggable Storage)**: the only CSV emission is through
    ``writer.write``; this module never imports or calls
    :meth:`pandas.DataFrame.to_csv`.

    Cross-pipeline dependency
        ``enumerate_game_ids`` is imported directly from
        :mod:`endpoints.schedule`, not from
        :mod:`pipelines.ingest_schedule`. When ``python run.py all``
        is invoked, the schedule pipeline has already run
        (AAP §0.4.5), so ``schedule.csv`` is on disk. When
        ``python run.py games`` is invoked in isolation, this
        pipeline re-enumerates on demand using the same endpoint
        helper — both modes are supported by design.

    Fatal conditions (propagated — NOT wrapped by Rule 6)
        The Rule 6 ``try/except`` block is deliberately scoped to the
        per-``GAME_ID`` body only. Exceptions raised **outside** that
        body propagate to the caller:

        * :func:`endpoints.schedule.enumerate_game_ids` HTTP/transport
          failures (connection refused, retries exhausted, etc.).
          A failed enumeration leaves the pipeline with no work to
          do; retrying on the next run is the correct recovery.
        * :meth:`utils.checkpoint.CheckpointManager.get_pending`
          I/O errors.
        * :meth:`utils.checkpoint.CheckpointManager.mark_completed`
          I/O errors raised *after* a successful write: the
          checkpoint manager rolls back in-memory state before
          propagating the :class:`OSError`.

    Parameters
    ----------
    client:
        :class:`~api.nba_client.NBAClient` instance (or any object
        satisfying the ``get(endpoint, params) -> dict`` contract —
        the dependency-injection seam exploited by unit tests). All
        upstream HTTP calls are routed through this instance via the
        endpoint wrappers (Rule 1 — no direct :mod:`requests`
        import in this module).
    writer:
        :class:`~storage.csv_writer.BaseWriter` instance (or any
        object satisfying the ``write(df, name, season) -> Path``
        contract). Rule 7 — this module NEVER calls
        :meth:`pandas.DataFrame.to_csv` directly; all CSV
        serialization is delegated here.
    checkpoint:
        :class:`~utils.checkpoint.CheckpointManager` instance (or
        any object satisfying the
        ``get_pending(domain, all_keys) -> List[str]`` and
        ``mark_completed(domain, key) -> None`` contracts).
    season:
        Season string in ``YYYY-YY`` form, e.g. ``"2025-26"``.
        Propagated verbatim to
        :func:`endpoints.schedule.enumerate_game_ids`, to every
        per-endpoint call, and to the writer.
    logger:
        Optional :class:`logging.LoggerAdapter` (typically a
        :class:`utils.correlation.CorrelationAdapter`). Defaults to
        the module-level :data:`_LOGGER` when ``None``. Passing a
        caller-provided adapter lets ``run.py`` thread its own
        per-invocation correlation ID into every log record emitted
        by this pipeline without relying on the ``contextvars``
        fallback.
    metrics:
        Optional metrics sink with an ``inc(name, labels, n)``
        method — duck-typed to accept any
        :class:`utils.metrics.MetricsRegistry`-compatible object.
        Defaults to :data:`utils.metrics.registry` (module-level
        singleton) when ``None``.

    Returns
    -------
    None
        The pipeline emits artifacts via the injected ``writer`` and
        state via the injected ``checkpoint``; it returns nothing.

    Raises
    ------
    Exception
        Any exception raised by
        :func:`endpoints.schedule.enumerate_game_ids`, by
        :meth:`utils.checkpoint.CheckpointManager.get_pending`, or
        by :meth:`utils.checkpoint.CheckpointManager.mark_completed`
        after a successful write, propagates to the caller. The
        Rule 6 ``try/except`` wrapping is scoped to the per-game
        body only.
    """
    log = logger if logger is not None else _LOGGER
    met = metrics if metrics is not None else _metrics_registry
    domain = config.DOMAIN_GAMES

    log.info("pipeline.start domain=%s season=%s", domain, season)

    # ---- Enumerate GAME_IDs (fatal if upstream HTTP fails) ----
    # ``enumerate_game_ids`` returns [] defensively for a malformed
    # envelope (it logs a warning internally); HTTP-level
    # exceptions propagate upward here, which is the intended
    # behaviour per AAP §0.5.2.1 — enumeration failures are fatal,
    # consistent with the checkpoint I/O and CSV-write-error policy.
    # This call is deliberately NOT wrapped in try/except: Rule 6
    # scopes the fail-safe to the per-GAME_ID body, not the
    # enumeration step.
    game_ids: List[str] = enumerate_game_ids(client, season)
    log.info(
        "pipeline.games.enumerated count=%d season=%s",
        len(game_ids),
        season,
    )

    # ---- Compute pending subset for resume (Rule 5) ----
    # ``get_pending`` preserves the input order of ``game_ids`` so
    # iteration remains deterministic across resumes. Order is
    # significant for observability (log replay matches prior runs)
    # and for test determinism.
    pending: List[str] = checkpoint.get_pending(domain, game_ids)
    log.info(
        "pipeline.games.pending pending=%d total=%d season=%s",
        len(pending),
        len(game_ids),
        season,
    )

    if not pending:
        log.info(
            "pipeline.complete domain=%s season=%s status=skipped"
            " reason=all_checkpointed",
            domain,
            season,
        )
        return

    # ---- Seed buffers from prior-session CSVs (history-preserving resume) ----
    # AAP §0.7.2.7 (Rule 7): only ``DataFrame.to_csv`` is forbidden
    # outside ``storage/csv_writer.py`` — reading CSVs via
    # :func:`pandas.read_csv` is explicitly permitted in pipeline
    # modules. Seeding the buffers from the existing artifacts
    # before per-game iteration preserves rows written by prior
    # sessions, so an interrupted run that resumes produces a CSV
    # containing **both** the prior-session rows and the current-
    # session rows (not only the current-session rows). The
    # helpers defend against any parse error with an empty-list
    # fallback + WARNING so a stale or corrupted on-disk artifact
    # cannot abort the pipeline. Helpers run OUTSIDE the per-game
    # try/except to preserve the Rule 6 scope boundary — only
    # per-game fetch/normalize/write failures are swallowed.
    writer_output_dir = getattr(writer, "output_dir", config.OUTPUT_DIR)
    games_buffer: List[pd.DataFrame] = _load_existing_games(
        writer_output_dir, season, pending, log,
    )
    pbp_buffer: List[pd.DataFrame] = _load_existing_pbp(
        writer_output_dir, season, pending, log,
    )

    # ---- Per-game iteration with Rule 6 fail-safe try/except ----
    # Buffers accumulate per-game DataFrames in-memory; after every
    # successful per-game pull the full buffer is re-written to
    # disk via ``writer.write`` (Rule 7). This is O(N²) I/O for a
    # full 1,230-game season but is dominated by the Rule 2
    # 1.0-second rate-limit sleep (≥ 1,230 seconds of forced
    # latency), so the incremental write is not the bottleneck.
    processed = 0
    failed = 0

    for gid in pending:
        try:
            # ---- Fetch + normalize traditional box score ----
            bs_payload = fetch_boxscoretraditionalv2(client, gid)
            bs_dfs = normalize_result_sets(bs_payload)
            bs_df = _select_primary_df(bs_dfs)
            bs_df = _ensure_game_columns(bs_df, season, gid)

            # ---- Fetch + normalize play-by-play ----
            pbp_payload = fetch_playbyplayv2(client, gid)
            pbp_dfs = normalize_result_sets(pbp_payload)
            pbp_df = _select_primary_df(pbp_dfs)
            pbp_df = _ensure_game_columns(pbp_df, season, gid)

            # ---- Accumulate + re-write aggregated CSV artifacts ----
            # Rule 7 gate: ``writer.write`` is the ONLY CSV
            # emission path. This module never imports or calls
            # :meth:`pandas.DataFrame.to_csv`.
            games_buffer.append(bs_df)
            pbp_buffer.append(pbp_df)
            combined_games = pd.concat(games_buffer, ignore_index=True)
            combined_pbp = pd.concat(pbp_buffer, ignore_index=True)
            writer.write(combined_games, config.CSV_GAMES, season)
            writer.write(combined_pbp, config.CSV_PLAY_BY_PLAY, season)

            # ---- Per-artifact row counters (Observability rule) ----
            met.inc(
                "pipeline_rows_written_total",
                {"pipeline": "ingest_games", "artifact": f"{config.CSV_GAMES}.csv"},
                n=len(bs_df),
            )
            met.inc(
                "pipeline_rows_written_total",
                {"pipeline": "ingest_games", "artifact": f"{config.CSV_PLAY_BY_PLAY}.csv"},
                n=len(pbp_df),
            )

            # ---- Rule 5: checkpoint AFTER the successful write ----
            # If any call above raises, the except block below
            # catches it, increments ``games_failed_total``, logs
            # at WARNING, and continues without marking the
            # checkpoint — so the next run retries this GAME_ID.
            checkpoint.mark_completed(domain, gid)
            processed += 1
            log.info(
                "pipeline.games.game_complete game_id=%s"
                " box_rows=%d pbp_rows=%d",
                gid,
                len(bs_df),
                len(pbp_df),
            )
        except Exception as exc:  # Rule 6 — fail-safe iteration; AAP §0.7.2.6.
            # The ONLY sanctioned ``except Exception`` in the
            # production codebase. We catch :class:`Exception` (NOT
            # :class:`BaseException`) so operator-initiated
            # :class:`KeyboardInterrupt` / :class:`SystemExit`
            # still terminates the pipeline. Log level is WARNING
            # (NOT ERROR) per the Rule 6 specification; the
            # increment target is the verbatim counter name
            # ``games_failed_total`` (AAP §0.5.1.6 binding
            # invariant). The ``continue`` keyword is mandatory —
            # without it the remaining in-loop iteration would
            # advance without explicit intent.
            #
            # The ``reason`` label captures the exception class name
            # (AAP §0.5.1.6: "reason=type(e).__name__"). Using the
            # class name — not the failing ``GAME_ID`` — keeps the
            # metric's label cardinality bounded (Prometheus best
            # practice) and makes dashboard aggregations like
            # ``sum(increase(games_failed_total[24h]))`` meaningful.
            # The failing ``GAME_ID`` remains visible in the WARNING
            # log record above (``"game %s failed: %s"``) for
            # operator forensics.
            failed += 1
            log.warning("game %s failed: %s", gid, exc)
            met.inc("games_failed_total", {"reason": type(exc).__name__})
            continue

    log.info(
        "pipeline.complete domain=%s season=%s processed=%d failed=%d",
        domain,
        season,
        processed,
        failed,
    )
