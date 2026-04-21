"""Pipeline orchestrators for the NBA Data Ingestion Pipeline (F-009 - F-013).

This package contains one module per data domain, each of which owns the
enumerate -> fetch -> normalize -> write -> checkpoint lifecycle for its
feature. Per the Agent Action Plan (AAP) §0.5.1.6, the package exposes
exactly five pipeline modules:

* :mod:`pipelines.ingest_games`    - Feature F-011 (box scores + play-by-play,
  the sole module covered by Rule 6 fail-safe iteration per AAP §0.7.2.6).
* :mod:`pipelines.ingest_lineups`  - Feature F-012 (lineup aggregates).
* :mod:`pipelines.ingest_players`  - Feature F-009 (league-wide player
  statistics and player-tracking metrics).
* :mod:`pipelines.ingest_schedule` - Feature F-013 (season schedule; exposes
  ``enumerate_game_ids`` consumption point for F-011).
* :mod:`pipelines.ingest_teams`    - Feature F-010 (team-level statistics).

Import policy
-------------

This package marker intentionally performs **no eager imports** of the
submodules. Downstream callers (principally :mod:`run`, the Click CLI
entry point) import pipelines explicitly::

    from pipelines import ingest_games, ingest_schedule

Keeping the import graph explicit avoids transitively pulling in
``pandas``, ``requests``, and the full observability stack when a caller
only needs a single pipeline; it also keeps Rule 1 (single HTTP client)
and Rule 7 (writer-only CSV emission) auditable via grep-based invariant
tests because no module-level imports are triggered by ``import pipelines``.

The ``__all__`` manifest below is the authoritative enumeration of the
five modules that constitute the pipeline layer. It is consulted by the
checkpoint acceptance test (``from pipelines import __all__``) and is
maintained in strict alphabetical order so additions are unambiguous.
"""

__all__ = [
    "ingest_games",
    "ingest_lineups",
    "ingest_players",
    "ingest_schedule",
    "ingest_teams",
]
