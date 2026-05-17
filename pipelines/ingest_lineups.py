"""Lineups ingestion orchestrator (F-012).

This module implements Feature **F-012 Lineups** of the NBA Data
Ingestion Pipeline per AAP §0.2.3, §0.4.1.1, and §0.5.1.6. It
orchestrates the enumerate -> fetch -> normalize -> write ->
checkpoint cycle for the league-wide lineup aggregates published by
the NBA Stats ``leaguedashlineups`` endpoint, emitting a single flat
CSV artifact at ``output/lineups.csv`` per the README output contract
(key columns ``season, group_id, team_id``).

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
* ``leaguedashlineups`` -> ``output/lineups.csv``. This is the sole
  endpoint invoked by :func:`run`; its key columns (``GROUP_ID``,
  ``TEAM_ID``) align with the README output contract for
  ``lineups.csv``.

Endpoint NOT invoked by default
-------------------------------
The sibling :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff`
endpoint (on/off clutch splits) is deliberately **NOT** imported or
invoked here: its emitted rows are player-keyed (``PLAYER_ID``, clutch
splits) and would corrupt ``lineups.csv``'s lineup-keyed schema if
written to the same artifact. It remains library-accessible via a
direct :mod:`endpoints.lineups` import for future use cases (AAP §0.1.3
pluggable endpoint posture).

Idempotence and resumability
----------------------------
Checkpointed under key ``"leaguedashlineups:<season>"`` in domain
:data:`config.DOMAIN_LINEUPS`. A run whose checkpoint has already been
marked returns immediately after emitting ``pipeline.skip`` and
``pipeline.complete`` log lines, performing no HTTP, no normalization,
and no writer activity.

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
from endpoints.lineups import fetch_leaguedashlineups
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

    The NBA Stats ``leaguedashlineups`` endpoint emits a single
    ``resultSets`` entry under the name ``"Lineups"`` (or an upstream
    variant), so picking the first — and typically only — DataFrame
    produced by :func:`utils.schema_normalizer.normalize_result_sets`
    is sufficient for F-012's single-artifact contract.

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

    The README output contract for ``lineups.csv`` lists ``season``
    among the primary key columns. Not every ``resultSets`` payload
    from ``leaguedashlineups`` includes a ``SEASON`` column natively
    (particularly when only a single season is queried per call), so
    this helper guarantees the column is present on the emitted
    artifact.

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
    """Run the Lineups ingestion pipeline (F-012).

    Fetches the season-scoped league-wide lineup aggregate from the
    NBA Stats ``leaguedashlineups`` endpoint, normalizes it to a flat
    DataFrame (Rule 4), writes it to ``output/lineups.csv`` via
    :meth:`BaseWriter.write` (Rule 7), and marks the checkpoint
    (Rule 5).

    **Endpoint invoked**: ``leaguedashlineups`` ->
    ``output/lineups.csv``. The key columns are
    ``season, group_id, team_id`` per the README output contract.

    **Endpoint NOT invoked by default but available via direct
    library import** from :mod:`endpoints.lineups`:
    ``leaguedashplayerclutch_onoff`` (on/off splits). It is excluded
    from the default pipeline because its key columns are
    player-oriented (``PLAYER_ID``, clutch splits) and do not map
    onto ``lineups.csv``'s lineup-keyed schema.

    **Rule scope**: enforces Rules 4, 5, 7. Rule 6 does NOT apply —
    per AAP §0.7.2.6 only :mod:`pipelines.ingest_games` wraps its
    loop in ``try/except Exception``; all other pipelines propagate
    exceptions.

    **Idempotence**: checkpointed under the key
    ``"leaguedashlineups:<season>"`` in the ``lineups`` domain. A
    resumed run short-circuits the fetch entirely.

    **Observability**: emits ``pipeline.start`` / ``pipeline.skip``
    / ``pipeline.wrote`` / ``pipeline.complete`` log events at INFO
    and increments the ``pipeline_rows_written_total`` counter
    (labeled ``{"domain": "lineups", "file": "lineups"}``) by the
    row count of the emitted DataFrame on every successful write.

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
        Propagated verbatim to :func:`fetch_leaguedashlineups`, to
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
        Any exception raised by :func:`fetch_leaguedashlineups`
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

    domain = config.DOMAIN_LINEUPS
    endpoint_label = "leaguedashlineups"
    key = f"{endpoint_label}:{season}"
    csv_name = config.CSV_LINEUPS

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

    payload = fetch_leaguedashlineups(client, season)
    dfs = normalize_result_sets(payload)
    df = _select_primary_df(dfs)
    df = _ensure_season_column(df, season)
    path = writer.write(df, csv_name, season)
    met.inc(
        "pipeline_rows_written_total",
        {"pipeline": "ingest_lineups", "artifact": f"{csv_name}.csv"},
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
