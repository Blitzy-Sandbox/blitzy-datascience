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

Package re-exports
------------------

This module eagerly imports the 15 endpoint wrappers and the
``enumerate_game_ids`` helper from their respective submodules so that
consumers can use the shorthand ``from endpoints import
fetch_leaguedashplayerstats`` in addition to the fully-qualified
``from endpoints.players import fetch_leaguedashplayerstats``. The
``__all__`` tuple pins the public API surface of the package to exactly
16 names (5 + 3 + 4 + 2 + 1 wrapper + 1 helper) and is used by
``from endpoints import *`` and by documentation tooling to emit a
canonical symbol inventory. Adding or removing a wrapper requires a
synchronized update in three places: the wrapper's submodule, the
re-export block below, and the ``__all__`` tuple.
"""

# ---------------------------------------------------------------------------
# Submodule re-exports — 16 public names (15 wrappers + enumerate_game_ids).
#
# These imports are deliberately eager: ``import endpoints`` materializes
# every wrapper at package-import time so downstream callers see a fully
# populated namespace without having to reach into submodules. The import
# order mirrors the feature-mapping narrative above (Players → Teams →
# Games → Lineups → Schedule) so a reader scanning the block sees the
# same domain progression as the docstring.
# ---------------------------------------------------------------------------

# F-009 Players — 5 wrappers
from endpoints.players import (
    fetch_leaguedashplayerstats,
    fetch_leaguedashplayerclutch,
    fetch_playercareerstats,
    fetch_playergamelog,
    fetch_leaguedashptstats,
)

# F-010 Teams — 3 wrappers
from endpoints.teams import (
    fetch_leaguedashteamstats,
    fetch_teamgamelog,
    fetch_teamdashboardbygeneralsplits,
)

# F-011 Games — 4 wrappers
from endpoints.games import (
    fetch_scoreboardv2,
    fetch_boxscoretraditionalv2,
    fetch_boxscoreadvancedv2,
    fetch_playbyplayv2,
)

# F-012 Lineups — 2 wrappers (fetch_leaguedashplayerclutch_onoff shares
# the upstream ``leaguedashplayerclutch`` endpoint with the Players
# domain but is a distinct wrapper with a different parameter surface —
# see the "Endpoint-count bookkeeping" section of the module docstring).
from endpoints.lineups import (
    fetch_leaguedashlineups,
    fetch_leaguedashplayerclutch_onoff,
)

# F-013 Schedule — 1 wrapper + 1 helper (the helper is the integration
# seam consumed by the Games pipeline for GAME_ID enumeration).
from endpoints.schedule import (
    fetch_leaguegamefinder,
    enumerate_game_ids,
)

# ---------------------------------------------------------------------------
# __all__ manifest — canonical 16-name public API surface of the package.
#
# Ordered to match the docstring feature mapping; the length MUST remain
# equal to 16 and every name MUST resolve to a callable defined in one of
# the five submodules above. A contract test in
# ``tests/unit/endpoints/`` pins both invariants.
# ---------------------------------------------------------------------------
__all__ = [
    # Players (F-009)
    "fetch_leaguedashplayerstats",
    "fetch_leaguedashplayerclutch",
    "fetch_playercareerstats",
    "fetch_playergamelog",
    "fetch_leaguedashptstats",
    # Teams (F-010)
    "fetch_leaguedashteamstats",
    "fetch_teamgamelog",
    "fetch_teamdashboardbygeneralsplits",
    # Games (F-011)
    "fetch_scoreboardv2",
    "fetch_boxscoretraditionalv2",
    "fetch_boxscoreadvancedv2",
    "fetch_playbyplayv2",
    # Lineups (F-012)
    "fetch_leaguedashlineups",
    "fetch_leaguedashplayerclutch_onoff",
    # Schedule (F-013)
    "fetch_leaguegamefinder",
    "enumerate_game_ids",
]
