"""Players ingestion orchestrator (F-009).

This module implements Feature **F-009 Players** of the NBA Data
Ingestion Pipeline per AAP §0.2.3, §0.4.1.1, and §0.5.1.6. It
orchestrates the fetch -> normalize -> write -> checkpoint cycle for
the two league-wide per-player endpoints that together satisfy the
README output contract for the Players domain, emitting two flat CSV
artifacts:

* ``output/players.csv`` from ``leaguedashplayerstats`` — key columns
  ``season, player_id, team_id``.
* ``output/player_tracking.csv`` from ``leaguedashptstats`` — key
  columns ``season, player_id, team_id``.

Unlike the single-endpoint peer pipelines
(:mod:`pipelines.ingest_teams`, :mod:`pipelines.ingest_lineups`,
:mod:`pipelines.ingest_schedule`), this pipeline iterates a
module-level :data:`_ENDPOINT_PLAN` tuple of
``(fetch_callable, csv_name, endpoint_label)`` triples so that each
endpoint can be checkpointed, written, and metered independently. The
tuple encoding keeps iteration order deterministic
(``players.csv`` first, ``player_tracking.csv`` second) to match the
AAP-specified narrative and to make the on-disk ``checkpoint.json``
human-readable.

Rules enforced
--------------
* **Rule 4 — Flat CSV Output** (Product Brief §5 / AAP §0.7.2.4).
  The :func:`utils.schema_normalizer.normalize_result_sets` helper
  asserts flatness before returning; this pipeline relies on that
  contract and does no further flattening of its own.
* **Rule 5 — Checkpoint After Every Pull** (Product Brief §5 /
  AAP §0.7.2.5). :meth:`CheckpointManager.mark_completed` is invoked
  **immediately** after every successful :meth:`BaseWriter.write`
  call, per endpoint, so a crash mid-iteration leaves exactly the
  endpoints that wrote successfully marked as complete, and the
  next run resumes at the first unmarked endpoint.
* **Rule 7 — Pluggable Storage** (Product Brief §5 /
  AAP §0.7.2.7). This module NEVER emits CSV artifacts directly;
  all CSV serialization is delegated to the injected
  ``writer.write(df, name, season)`` boundary defined by
  :class:`storage.csv_writer.BaseWriter`.

Rule **6** (Fail-Safe Game Iteration) does **NOT** apply here: per
AAP §0.7.2.6 the ``try/except Exception`` fail-safe loop is scoped to
:mod:`pipelines.ingest_games` only. All other pipelines, including
this one, propagate exceptions to the caller so transient upstream
failures can be observed and retried via checkpoint resume. If an
endpoint mid-loop raises, endpoints that already wrote successfully
remain checkpointed, and the next run will skip them and retry the
failed endpoint.

Endpoints invoked by default
----------------------------
* ``leaguedashplayerstats`` -> ``output/players.csv``. League-wide
  per-player aggregate (per-game or totals). Natively includes
  ``PLAYER_ID`` and ``TEAM_ID``; the ``season`` column is guaranteed
  by :func:`_ensure_season_column` when the upstream rowset omits it.
* ``leaguedashptstats`` -> ``output/player_tracking.csv``. League-wide
  per-player SportVU-derived tracking metrics
  (``SpeedDistance`` by default; ``pt_measure_type`` is selectable via
  direct-library use). Natively includes ``PLAYER_ID`` and
  ``TEAM_ID``; the ``season`` column is guaranteed by
  :func:`_ensure_season_column`.

Endpoints NOT invoked by default
--------------------------------
The sibling wrappers in :mod:`endpoints.players`:

* :func:`~endpoints.players.fetch_leaguedashplayerclutch` —
  season-scoped clutch variant. Not invoked by default because its
  key columns overlap with the base aggregate and routing its rows
  into ``players.csv`` would collide with the ``leaguedashplayerstats``
  write (writer performs atomic overwrite, so two writes to the same
  ``name`` would discard the first).
* :func:`~endpoints.players.fetch_playercareerstats` and
  :func:`~endpoints.players.fetch_playergamelog` — per-player
  endpoints requiring iteration across the full league roster
  (450+ players × the Rule 2 rate-limit floor of 1.0 s per request).
  AAP §0.6.2 explicitly defers per-entity iteration as out-of-scope
  for the initial deliverable; these wrappers remain
  library-accessible via a direct :mod:`endpoints.players` import
  for operators who need a per-player data pull.

This "coverage = available, not invoked" interpretation is the
AAP §0.1.1 Design Decision "Minimum-Viable Endpoint Coverage": every
wrapper is implemented in :mod:`endpoints.players` (so the 15+
endpoint-coverage contract is satisfied at the wrapper layer), while
the default batch pipeline invokes only the league-wide endpoints
that map cleanly onto the declared CSV artifacts without collision.

Idempotence and resumability
----------------------------
Each endpoint is checkpointed under its own key of the form
``"<endpoint_label>:<season>"`` in domain :data:`config.DOMAIN_PLAYERS`.
A run whose checkpoint already has an entry for a given key skips
that endpoint (logging ``pipeline.skip``) and continues iterating,
so per-endpoint resume is deterministic. Force a re-fetch of a
single endpoint by editing ``output/checkpoint.json`` to remove that
key, or delete the manifest entirely to re-run everything.

Observability
-------------
Structured log events — all with auto-injected correlation IDs from
:class:`utils.correlation.CorrelationAdapter` — are emitted at the
following pipeline milestones:

* ``pipeline.start`` (INFO) — run entered
* ``pipeline.skip`` (INFO) — per-endpoint, checkpoint already present
* ``pipeline.wrote`` (INFO) — per-endpoint, writer call succeeded
* ``pipeline.complete`` (INFO) — run exiting normally, with aggregate
  counts of ``wrote`` vs ``skipped`` endpoints

The ``pipeline_rows_written_total`` counter (pre-registered by
:mod:`utils.metrics`) is incremented by ``len(df)`` per successful
write, labeled with ``domain`` and ``file`` for per-artifact
dashboards. Because this pipeline iterates :data:`_ENDPOINT_PLAN` and
writes two CSVs, the counter is incremented twice per run with
differing ``file`` label values, producing two distinct Prometheus
samples.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

import config
from endpoints.players import (
    fetch_leaguedashplayerstats,
    fetch_leaguedashptstats,
)
from utils.logger import get_logger
from utils.metrics import registry as _metrics_registry
from utils.schema_normalizer import normalize_result_sets


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

#: Re-exports declared by this module. Only :func:`run` is part of the
#: public API; the module-level plan tuple and the two helpers below
#: are implementation details kept at module scope only so they can be
#: unit-tested without being consumed by peer pipelines.
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
# Endpoint plan
# ---------------------------------------------------------------------------

#: Ordered iteration plan for this pipeline. Each entry is a triple of
#: ``(fetch_callable, csv_name, endpoint_label)``:
#:
#: * ``fetch_callable`` — one of the two season-scoped league-wide
#:   wrappers imported from :mod:`endpoints.players`. Each accepts
#:   ``(client, season, **kwargs)`` and returns the raw NBA Stats
#:   ``resultSets`` JSON envelope as a ``Dict[str, Any]``.
#: * ``csv_name`` — the logical artifact name (drawn from
#:   :mod:`config`) passed to :meth:`BaseWriter.write`. The writer
#:   resolves this to ``output/<csv_name>.csv``.
#: * ``endpoint_label`` — the lowercase NBA Stats endpoint identifier,
#:   used verbatim in the checkpoint key and in the ``pipeline.wrote``
#:   log event. Kept as a literal string (not derived from
#:   ``fetch_callable.__name__``) so on-disk checkpoint keys are
#:   resilient to future Python-side function renames.
#:
#: Iteration order is intentional: ``players.csv`` (base aggregate)
#: is produced before ``player_tracking.csv`` (tracking metrics) so
#: the larger, higher-value artifact lands on disk first in the
#: common case. This ordering is also asserted by the unit tests
#: (``test_ingest_players.py``) to pin the contract.
_ENDPOINT_PLAN: Tuple[Tuple[Callable[..., Dict[str, Any]], str, str], ...] = (
    (fetch_leaguedashplayerstats, config.CSV_PLAYERS, "leaguedashplayerstats"),
    (fetch_leaguedashptstats, config.CSV_PLAYER_TRACKING, "leaguedashptstats"),
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _select_primary_df(dfs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return the first DataFrame from a normalized ``resultSets`` dict.

    Both :func:`endpoints.players.fetch_leaguedashplayerstats` and
    :func:`endpoints.players.fetch_leaguedashptstats` emit a single
    ``resultSets`` entry under their canonical upstream name
    (``"LeagueDashPlayerStats"`` and ``"LeagueDashPtStats"``
    respectively), so picking the first — and typically only —
    DataFrame produced by
    :func:`utils.schema_normalizer.normalize_result_sets` is
    sufficient for F-009's two-artifact contract.

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

    The README output contract for both ``players.csv`` and
    ``player_tracking.csv`` lists ``season`` among the primary key
    columns alongside ``player_id`` and ``team_id``. The
    ``leaguedashplayerstats`` and ``leaguedashptstats`` payloads
    occasionally emit a ``SEASON_ID`` column (when filtered across
    seasons) but, for a single-season query, may omit the season from
    the rowset entirely. This helper guarantees the column is present
    on the emitted artifact so every row is unambiguously attributable
    to the season the pipeline was invoked with.

    The check is **case-insensitive** against ``df.columns`` to
    tolerate both the uppercase convention (``SEASON`` / ``SEASON_ID``)
    common in NBA Stats payloads and the snake_cased form that
    downstream consumers expect. When a match is found, the
    DataFrame is returned unchanged — no column renaming is
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
    """Run the Players ingestion pipeline (F-009).

    Iterates the set of season-scoped Players endpoints configured in
    :data:`_ENDPOINT_PLAN` and, for each one that is not yet
    checkpointed, fetches the payload, normalizes it to a flat
    DataFrame (Rule 4), writes it to the configured CSV artifact via
    :meth:`BaseWriter.write` (Rule 7), and marks the checkpoint
    (Rule 5).

    **Endpoints invoked by default**:

    * ``leaguedashplayerstats`` -> ``output/players.csv`` — league-wide
      per-player aggregate (per-game or totals). Native key columns
      ``PLAYER_ID`` and ``TEAM_ID``; ``season`` guaranteed by
      :func:`_ensure_season_column`.
    * ``leaguedashptstats`` -> ``output/player_tracking.csv`` —
      league-wide per-player SportVU tracking metrics (speed,
      distance, passing — default ``pt_measure_type=SpeedDistance``).
      Native key columns ``PLAYER_ID`` and ``TEAM_ID``; ``season``
      guaranteed by :func:`_ensure_season_column`.

    **Endpoints NOT invoked by default but available via direct
    library import** from :mod:`endpoints.players`:

    * :func:`~endpoints.players.fetch_leaguedashplayerclutch` —
      season-scoped clutch variant that overlaps key columns with
      the base aggregate and would collide on the ``players.csv``
      writer target.
    * :func:`~endpoints.players.fetch_playercareerstats` and
      :func:`~endpoints.players.fetch_playergamelog` — per-player
      endpoints that would require iterating the full player roster
      and are therefore deferred per AAP §0.6.2 (per-entity
      iteration listed as a future-phase consideration).

    **Rule scope**: enforces Rules 4, 5, 7. Rule 6 does NOT apply —
    per AAP §0.7.2.6 only :mod:`pipelines.ingest_games` wraps its
    loop in ``try/except Exception``; all other pipelines propagate
    exceptions. If one endpoint fails mid-iteration, any endpoints
    that already wrote successfully remain checkpointed and a
    subsequent run resumes at the failed endpoint.

    **Idempotence**: each endpoint is checkpointed under its own key
    of the form ``"<endpoint_label>:<season>"`` in the
    :data:`config.DOMAIN_PLAYERS` domain (``"players"``). Previously
    completed endpoints are skipped individually; the iteration
    continues with the next pending endpoint. Force a re-fetch of a
    single endpoint by editing ``output/checkpoint.json`` to remove
    that key, or delete the manifest entirely.

    **Observability**: emits ``pipeline.start`` / ``pipeline.skip``
    / ``pipeline.wrote`` / ``pipeline.complete`` log events at INFO.
    The ``pipeline_rows_written_total`` counter (labeled with
    ``domain`` and ``file``) is incremented by ``len(df)`` per
    successful write — twice per nominal run, once per endpoint in
    :data:`_ENDPOINT_PLAN`.

    Parameters
    ----------
    client:
        :class:`~api.nba_client.NBAClient` instance (or any object
        satisfying the same ``get(endpoint, params) -> dict``
        contract — the dependency-injection seam exploited by the
        unit-test :class:`RecordingClient` spy).
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
        Propagated verbatim to every fetch callable in
        :data:`_ENDPOINT_PLAN`, to each checkpoint key, and to the
        ``season`` column fallback.
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
        Any exception raised by a fetch callable in
        :data:`_ENDPOINT_PLAN` (including network errors surfaced by
        the :mod:`tenacity`-wrapped :class:`NBAClient`),
        :func:`normalize_result_sets` (:class:`ValueError` on
        malformed upstream payloads per Rule 4 post-condition), or
        :meth:`BaseWriter.write` (I/O errors) will propagate. Rule 6
        fail-safe wrapping is **not** applied here — it is scoped to
        :mod:`pipelines.ingest_games` only.
    """
    log = logger if logger is not None else _LOGGER
    met = metrics if metrics is not None else _metrics_registry

    domain = config.DOMAIN_PLAYERS

    log.info("pipeline.start domain=%s season=%s", domain, season)

    wrote = 0
    skipped = 0

    for fetch_fn, csv_name, endpoint_label in _ENDPOINT_PLAN:
        key = f"{endpoint_label}:{season}"

        if checkpoint.is_completed(domain, key):
            log.info(
                "pipeline.skip domain=%s key=%s reason=checkpointed",
                domain, key,
            )
            skipped += 1
            continue

        payload = fetch_fn(client, season)
        dfs = normalize_result_sets(payload)
        df = _select_primary_df(dfs)
        df = _ensure_season_column(df, season)
        path = writer.write(df, csv_name, season)
        met.inc(
            "pipeline_rows_written_total",
            {"domain": domain, "file": csv_name},
            n=len(df),
        )
        log.info(
            "pipeline.wrote domain=%s endpoint=%s file=%s rows=%d path=%s",
            domain, endpoint_label, csv_name, len(df), path,
        )
        # Rule 5: mark_completed MUST run immediately after a
        # successful write so interrupted runs resume
        # deterministically on the next key. Any exception raised
        # above leaves the checkpoint unmarked for this endpoint —
        # endpoints completed earlier in the loop remain marked, and
        # the next run skips them and retries only the failed one.
        checkpoint.mark_completed(domain, key)
        wrote += 1

    log.info(
        "pipeline.complete domain=%s season=%s wrote=%d skipped=%d",
        domain, season, wrote, skipped,
    )
