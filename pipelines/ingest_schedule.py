"""Schedule ingestion orchestrator (F-013).

This module implements Feature **F-013 Schedule** of the NBA Data
Ingestion Pipeline per AAP §0.2.3, §0.4.1.1, and §0.5.1.6. It
orchestrates the fetch -> normalize -> write -> checkpoint cycle for
the league-wide season schedule published by the NBA Stats
``leaguegamefinder`` endpoint, emitting a single flat CSV artifact at
``output/schedule.csv`` per the README output contract (key columns
``season, game_id, home_team_id, away_team_id``).

Rules enforced
--------------
* **Rule 4 — Flat CSV Output** (Product Brief §5 / AAP §0.7.2.4).
  The :func:`utils.schema_normalizer.normalize_result_sets` helper
  asserts flatness before returning; this pipeline relies on that
  contract and does no further flattening of its own.
* **Rule 5 — Checkpoint After Every Pull** (Product Brief §5 /
  AAP §0.7.2.5). :meth:`CheckpointManager.mark_completed` is invoked
  **immediately** after every successful :meth:`BaseWriter.write`
  call. If the write raises, the checkpoint is NOT marked, so a
  subsequent run will retry.
* **Rule 7 — Pluggable Storage** (Product Brief §5 /
  AAP §0.7.2.7). This module NEVER emits CSV artifacts directly;
  all CSV serialization is delegated to the injected
  ``writer.write(df, name, season)`` boundary defined by
  :class:`storage.csv_writer.BaseWriter`.

Rule **6** (Fail-Safe Game Iteration) does **NOT** apply here: per
AAP §0.7.2.6 the ``try/except Exception`` fail-safe loop is scoped to
:mod:`pipelines.ingest_games` only. All other pipelines, including
this one, propagate exceptions to the caller so transient upstream
failures can be observed and retried via checkpoint resume.

Endpoint invoked
----------------
* ``leaguegamefinder`` -> ``output/schedule.csv``. This is the sole
  endpoint invoked by :func:`run`; its upstream envelope returns one
  row per ``(TEAM_ID, GAME_ID)`` tuple — two rows per game (home +
  away) — which is preserved verbatim in the CSV because that is the
  natural upstream shape. Downstream consumers that need the
  one-row-per-game pivot ``(season, game_id, home_team_id,
  away_team_id)`` can group on ``GAME_ID`` and project the
  ``MATCHUP`` column, which carries the home/away indicator in the
  leaguegamefinder payload.

Cross-pipeline dependency
-------------------------
Per AAP §0.4.5 and §0.5.1.6, :mod:`pipelines.ingest_games` (F-011)
cannot function without the deduplicated ``GAME_ID`` list that the
Schedule domain produces. That list is exposed via
:func:`endpoints.schedule.enumerate_game_ids`, which the Games
pipeline imports **directly** from the endpoints layer — not from
this pipeline module. This keeps pipeline-to-pipeline coupling at
zero: pipelines share data only via (a) the CSV filesystem, (b) the
checkpoint manager, or (c) direct endpoint-helper imports. When
``python run.py all`` is invoked, this pipeline runs first so that
``output/schedule.csv`` exists on disk before any downstream pipeline
consults it; when ``python run.py games`` is invoked in isolation,
the Games pipeline re-enumerates game IDs on demand via the same
endpoint helper.

Idempotence and resumability
----------------------------
Checkpointed under key ``"leaguegamefinder:<season>"`` in domain
:data:`config.DOMAIN_SCHEDULE`. A run whose checkpoint has already
been marked returns immediately after emitting ``pipeline.skip`` and
``pipeline.complete`` log lines, performing no HTTP, no normalization,
and no writer activity. Operators can force a re-fetch by deleting
the relevant entry from ``output/checkpoint.json`` (or the entire
file).

Observability
-------------
Structured log events — all with auto-injected correlation IDs from
:class:`utils.correlation.CorrelationAdapter` — are emitted at the
following pipeline milestones:

* ``pipeline.start`` (INFO) — run entered
* ``pipeline.skip`` (INFO) — checkpoint already present
* ``pipeline.wrote`` (INFO) — writer call succeeded
* ``pipeline.complete`` (INFO) — run exiting normally

The ``pipeline_rows_written_total`` counter (pre-registered by
:mod:`utils.metrics`) is incremented by ``len(df)`` per successful
write, labeled with ``domain`` and ``file`` for per-artifact
dashboards.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd

import config
from endpoints.schedule import fetch_leaguegamefinder
from utils.logger import get_logger
from utils.metrics import registry as _metrics_registry
from utils.schema_normalizer import normalize_result_sets


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

#: Re-exports declared by this module. Only :func:`run` is part of the
#: public API; the two helpers below are implementation details kept
#: at module scope only so they can be unit-tested without being
#: consumed by peer pipelines.
__all__ = ["run"]


# ---------------------------------------------------------------------------
# Module-level fallback logger
# ---------------------------------------------------------------------------

#: Module-level fallback logger adapter. Used when :func:`run` is
#: invoked without an explicit ``logger`` argument — typically by
#: unit tests or by ad-hoc scripts that don't mint a correlation ID
#: beforehand. The returned :class:`logging.LoggerAdapter` subclass
#: (``CorrelationAdapter``) transparently injects the per-run
#: correlation ID set by ``run.py`` at CLI entry into every
#: :class:`logging.LogRecord` emitted from this module, so no
#: explicit plumbing is required at call sites.
_LOGGER: logging.LoggerAdapter = get_logger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _select_primary_df(dfs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the first DataFrame from a normalized ``resultSets`` dict.

    The NBA Stats ``leaguegamefinder`` endpoint emits its primary
    rowset under the name ``"LeagueGameFinderResults"`` (snake-cased
    to ``"league_game_finder_results"`` by
    :func:`utils.schema_normalizer.normalize_result_sets`). The
    envelope may occasionally carry auxiliary tables (e.g. an empty
    ``"AvailableSeasons"`` list) that precede or follow the primary
    table across upstream versions; picking the first — and
    conventionally primary — DataFrame is sufficient for F-013's
    single-artifact contract.

    Returning an empty :class:`pandas.DataFrame` when ``dfs`` is
    falsy (empty dict, ``None``-equivalent) is deliberate: it keeps
    the downstream ``writer.write`` call total — the CSV writer
    gracefully handles zero-row frames by emitting a header-only
    file — so operators consistently see an artifact on disk even in
    degenerate upstream cases, and the checkpoint can still be
    marked to avoid busy-looping on a permanently empty upstream.

    Parameters
    ----------
    dfs:
        Mapping of snake_cased ``resultSets`` entry name to flat
        :class:`pandas.DataFrame` — the exact return shape of
        :func:`utils.schema_normalizer.normalize_result_sets`.

    Returns
    -------
    pandas.DataFrame
        The first DataFrame in iteration order, or an empty
        DataFrame if ``dfs`` has no entries.
    """
    if not dfs:
        return pd.DataFrame()
    first_key = next(iter(dfs))
    return dfs[first_key]


def _ensure_season_column(df: pd.DataFrame, season: str) -> pd.DataFrame:
    """Ensure the DataFrame has a lowercase ``season`` column.

    The README output contract for ``schedule.csv`` lists ``season``
    as the leading key column alongside ``game_id``,
    ``home_team_id``, and ``away_team_id``. The ``leaguegamefinder``
    payload typically emits a ``SEASON_ID`` column (a numeric season
    identifier such as ``"22025"`` for the 2025-26 regular season)
    but not a column literally named ``season``. This helper
    guarantees the lowercase ``season`` column is present on the
    emitted artifact so every row is unambiguously attributable to
    the season the pipeline was invoked with in the
    ``YYYY-YY`` form the operator supplied.

    The check is **case-insensitive** against ``df.columns`` to
    tolerate both the uppercase convention (``SEASON`` / ``season``)
    that may surface across upstream versions and the snake_cased
    form that downstream consumers expect. When a match is found,
    the DataFrame is returned unchanged — no column renaming is
    attempted here to preserve the normalizer's intended schema.

    When no season-like column exists, a new lowercase ``season``
    column is inserted at position 0 so it appears first in the CSV,
    matching the README's leading key-column ordering. A shallow
    :meth:`pandas.DataFrame.copy` is taken first so the caller's
    DataFrame is never mutated in place (defensive contract; the
    normalizer already returns fresh frames but this keeps the
    helper side-effect-free regardless of upstream changes).

    Parameters
    ----------
    df:
        Source DataFrame produced by
        :func:`utils.schema_normalizer.normalize_result_sets`.
    season:
        Season string in ``YYYY-YY`` form, e.g. ``"2025-26"``. Used
        verbatim as the value for every row of the inserted column.

    Returns
    -------
    pandas.DataFrame
        Either the original ``df`` (when a season-like column was
        already present) or a copy with a prepended ``season``
        column.
    """
    if any(c.lower() == "season" for c in df.columns):
        return df
    out = df.copy()
    out.insert(0, "season", season)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    client,
    writer,
    checkpoint,
    season: str,
    logger: Optional[logging.LoggerAdapter] = None,
    metrics: Optional[Any] = None,
) -> None:
    """Run the Schedule ingestion pipeline (F-013).

    Fetches the league-wide game schedule for the given season via
    the NBA Stats ``leaguegamefinder`` endpoint, normalizes the
    ``resultSets`` envelope into a flat pandas DataFrame (Rule 4),
    writes it to ``output/schedule.csv`` via
    :meth:`BaseWriter.write` (Rule 7), and marks the checkpoint
    (Rule 5).

    **Endpoint invoked**: ``leaguegamefinder`` ->
    ``output/schedule.csv``. The key columns are ``season, game_id,
    home_team_id, away_team_id`` per the README output contract.
    The upstream rowset contains one row per ``(TEAM_ID, GAME_ID)``
    tuple — two rows per game (home + away) — which is preserved
    verbatim in the CSV because that is the natural upstream shape;
    downstream consumers pivot by ``GAME_ID`` and the ``MATCHUP``
    column as needed. The ``season`` column is guaranteed by
    :func:`_ensure_season_column` in case the upstream rowset omits
    a literal ``season`` column.

    **Cross-pipeline dependency**: :mod:`pipelines.ingest_games`
    imports :func:`endpoints.schedule.enumerate_game_ids` directly
    (not from this module) to obtain the ``GAME_ID`` list. When
    ``python run.py all`` is invoked, this pipeline runs first by
    design (AAP §0.4.5). When ``python run.py games`` is invoked in
    isolation, the Games pipeline re-enumerates game IDs on demand
    via the same endpoint helper — i.e. an isolated ``games``
    invocation does not require this pipeline to have run.

    **Rule scope**: enforces Rules 4, 5, 7. Rule 6 does NOT apply —
    per AAP §0.7.2.6 only :mod:`pipelines.ingest_games` wraps its
    loop in ``try/except Exception``; all other pipelines propagate
    exceptions.

    **Idempotence**: checkpointed under the key
    ``"leaguegamefinder:<season>"`` in the ``schedule`` domain. A
    resumed run short-circuits the fetch entirely.

    **Observability**: emits ``pipeline.start`` / ``pipeline.skip``
    / ``pipeline.wrote`` / ``pipeline.complete`` log events at INFO
    and increments the ``pipeline_rows_written_total`` counter
    (labeled ``{"domain": "schedule", "file": "schedule"}``) by the
    row count of the emitted DataFrame on every successful write.

    Parameters
    ----------
    client:
        :class:`~api.nba_client.NBAClient` instance (or any object
        satisfying the same ``get(endpoint, params) -> dict``
        contract — the dependency-injection seam exploited by
        unit-test :class:`RecordingClient` spies).
    writer:
        :class:`~storage.csv_writer.BaseWriter` instance (or any
        object satisfying the ``write(df, name, season) -> Path``
        contract — see AAP §0.4.1.1 and the unit-test
        :class:`RecordingWriter` spy).
    checkpoint:
        :class:`~utils.checkpoint.CheckpointManager` instance (or
        any object satisfying the ``is_completed(domain, key) -> bool``
        and ``mark_completed(domain, key) -> None`` contract — see
        the unit-test :class:`RecordingCheckpoint` spy).
    season:
        Season string in ``YYYY-YY`` form, e.g. ``"2025-26"``.
        Propagated verbatim to :func:`fetch_leaguegamefinder`, to
        the checkpoint key, and to the ``season`` column fallback.
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

    Raises
    ------
    Exception
        Any exception raised by :func:`fetch_leaguegamefinder`
        (including network errors surfaced by the
        :mod:`tenacity`-wrapped :class:`NBAClient`),
        :func:`normalize_result_sets` (:class:`ValueError` on
        malformed upstream payloads per Rule 4 post-condition), or
        :meth:`BaseWriter.write` (I/O errors) will propagate. Rule 6
        fail-safe wrapping is **not** applied here — it is scoped to
        :mod:`pipelines.ingest_games` only.
    """
    log = logger if logger is not None else _LOGGER
    met = metrics if metrics is not None else _metrics_registry

    domain = config.DOMAIN_SCHEDULE
    endpoint_label = "leaguegamefinder"
    key = f"{endpoint_label}:{season}"
    csv_name = config.CSV_SCHEDULE

    log.info("pipeline.start domain=%s season=%s", domain, season)

    if checkpoint.is_completed(domain, key):
        log.info(
            "pipeline.skip domain=%s key=%s reason=checkpointed",
            domain, key,
        )
        log.info(
            "pipeline.complete domain=%s season=%s wrote=0 skipped=1",
            domain, season,
        )
        return

    payload = fetch_leaguegamefinder(client, season)
    dfs = normalize_result_sets(payload)
    df = _select_primary_df(dfs)
    df = _ensure_season_column(df, season)
    path = writer.write(df, csv_name, season)
    met.inc(
        "pipeline_rows_written_total",
        {"pipeline": "ingest_schedule", "artifact": f"{csv_name}.csv"},
        n=len(df),
    )
    log.info(
        "pipeline.wrote domain=%s endpoint=%s file=%s rows=%d path=%s",
        domain, endpoint_label, csv_name, len(df), path,
    )
    # Rule 5: mark_completed MUST run immediately after a successful
    # write so interrupted runs resume deterministically on the next
    # key. Any exception raised above leaves the checkpoint unmarked
    # and the next run retries.
    checkpoint.mark_completed(domain, key)
    log.info(
        "pipeline.complete domain=%s season=%s wrote=1 skipped=0",
        domain, season,
    )
