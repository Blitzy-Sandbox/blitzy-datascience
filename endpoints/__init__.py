"""Domain-specific NBA Stats endpoint wrappers (Features F-009 through F-013).

This package contains one module per NBA Stats data domain. Each module
hosts thin, side-effect-free Python functions that wrap a single upstream
endpoint by constructing the correct parameter dict and delegating to
:meth:`api.nba_client.NBAClient.get` — the SOLE HTTP transport path in
the pipeline (Rule 1).

Modules and feature mapping
---------------------------

* :mod:`endpoints.players` — Feature F-009 (5 wrappers):
  ``fetch_leaguedashplayerstats``, ``fetch_leaguedashplayerclutch``,
  ``fetch_playercareerstats``, ``fetch_playergamelog``,
  ``fetch_leaguedashptstats``.
* :mod:`endpoints.teams` — Feature F-010 (3 wrappers):
  ``fetch_leaguedashteamstats``, ``fetch_teamgamelog``,
  ``fetch_teamdashboardbygeneralsplits``.
* :mod:`endpoints.games` — Feature F-011 (4 wrappers):
  ``fetch_scoreboardv2``, ``fetch_boxscoretraditionalv2``,
  ``fetch_boxscoreadvancedv2``, ``fetch_playbyplayv2``.
* :mod:`endpoints.lineups` — Feature F-012 (2 wrappers):
  ``fetch_leaguedashlineups``, ``fetch_leaguedashplayerclutch_onoff``.
* :mod:`endpoints.schedule` — Feature F-013 (1 wrapper + 1 helper):
  ``fetch_leaguegamefinder`` and the ``enumerate_game_ids`` helper that
  derives the ``GAME_ID`` list consumed by the Games pipeline.

Rule compliance
---------------

No module in this package imports ``requests``, ``pandas``, ``json``, or
filesystem I/O modules. The modules are pure parameter-builders that
delegate to :class:`api.nba_client.NBAClient` (Rule 1). They never write
CSV output (Rule 7). Rate limiting (Rule 2), required headers (Rule 3),
and retry-with-backoff (Feature F-004) are all enforced inside
:class:`api.nba_client.NBAClient` and are transparent to these wrappers.

Cross-domain dependency
-----------------------

Per :doc:`docs/api/endpoints_catalog.md` §9, the Games pipeline
(:mod:`pipelines.ingest_games`) depends on the Schedule pipeline
(:mod:`pipelines.ingest_schedule`) for ``GAME_ID`` enumeration. The
coupling is expressed through the :func:`endpoints.schedule.enumerate_game_ids`
helper — not by reading ``output/schedule.csv`` — so standalone
``python run.py games --season <season>`` continues to work without a
prior ``schedule`` invocation.

Endpoint-count bookkeeping
--------------------------

The upstream ``leaguedashplayerclutch`` endpoint is wrapped by TWO
intentionally-separate functions: :func:`endpoints.players.fetch_leaguedashplayerclutch`
(Players domain) and :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff`
(Lineups domain). The Agent Action Plan counts the endpoint once in
Players (5 endpoints) and once in Lineups (2 endpoints) to reach 15
logical endpoint invocations across 6 domains.
"""
