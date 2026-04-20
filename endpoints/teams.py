"""Teams domain endpoint wrappers (Feature F-010).

This module wraps the three NBA Stats endpoints that feed the Teams data
domain and is a direct literal implementation of AAP §0.5.1.4 Group 4
(Endpoint Wrappers) for the Teams domain. The three wrappers are
intentionally thin: each builds a parameter dict, emits a single DEBUG
log record, and delegates the actual HTTP exchange to
:meth:`api.nba_client.NBAClient.get` — the SOLE HTTP transport path in
the production codebase (Rule 1, Single HTTP Client).

Endpoints wrapped
-----------------

* ``leaguedashteamstats`` — league-wide per-team aggregates for a
  season; returns a SINGLE ``LeagueDashTeamStats`` result-set table
  with one row per team. The Teams pipeline invokes this first so the
  ``TEAM_ID`` column can seed the per-team iteration that follows.
* ``teamgamelog`` — the per-game log for a specific ``TeamID`` and
  ``Season``; returns a SINGLE ``TeamGameLog`` result-set table with
  one row per game. Requires ``TeamID`` as a mandatory caller argument.
* ``teamdashboardbygeneralsplits`` — a multi-facet team dashboard
  sliced across six split dimensions (Overall, Location, WinsLosses,
  Month, PrePostAllStar, DaysRest). This is the ONLY Teams-domain
  endpoint whose response envelope is multi-table; downstream
  normalization in :mod:`utils.schema_normalizer` flattens each table
  independently and :mod:`pipelines.ingest_teams` decides which subset
  to persist.

Operational rules enforced by this module
-----------------------------------------

* **Rule 1 — Single HTTP Client.** No module outside
  :mod:`api.nba_client` may invoke :mod:`requests`; the wrappers here
  therefore delegate to ``client.get(endpoint, params)`` and import
  neither :mod:`requests` nor :mod:`urllib`/:mod:`httpx`. This is
  verified by the grep-based invariant test at
  ``tests/invariants/test_rule1_sole_http_client.py``.
* **Rule 4 — Flat CSV Output (indirect).** This module performs no
  DataFrame construction and no schema flattening; the raw JSON
  envelope is returned unchanged. Flattening is the responsibility of
  :mod:`utils.schema_normalizer`, which is invoked downstream by the
  pipeline.
* **Rule 7 — Pluggable Storage.** This module performs no filesystem
  I/O and does not invoke ``DataFrame.to_csv``, ``open(...)``, or any
  ``write(...)`` / ``write_text(...)`` / ``write_bytes(...)`` method.

Observability
-------------

Each wrapper emits exactly one DEBUG-level log record before the
``client.get`` delegation. Log records use ``%s``-style placeholders so
the standard-library ``logging.Formatter`` only resolves the arguments
when DEBUG logging is actually enabled — this complies with the
F-008 stdlib-logging mandate and the Observability rule's deferred-
formatting requirement. Only safe parameter values (``season``,
``season_type``, ``measure_type``, ``per_mode``, ``team_id``) are
logged. Full request bodies and response payloads are never emitted by
this module; the central transport layer emits those at DEBUG.

Style constraints (AAP §0.5.1.4 / agent-prompt Phase 7)
--------------------------------------------------------

* Literal dict construction + ``params.update(kwargs)`` pattern is
  applied uniformly in every wrapper for grep-discoverability of the
  parameter surface.
* ``team_id`` is ``str``-cast at the call site inside the params dict
  rather than at the function boundary so callers passing integer IDs
  (``1610612747``) or strings (``"1610612747"``) both work without
  surprising the upstream API.
* NO ``_base_team_params()`` helper exists — the two multi-filter dicts
  are duplicated literally (per AAP style constraint) so each endpoint's
  parameter surface is visible in one place rather than split across
  a base dict and an override.

Gate 12 — config read-sites
---------------------------

Every reference to :data:`config.DEFAULT_SEASON_TYPE` and
:data:`config.DEFAULT_LEAGUE_ID` appears as a LITERAL ``config.<NAME>``
dotted reference (in default-argument position) rather than a cached
local variable, so the Config Propagation Tracing gate (Gate 12) can
discover these read-sites via ``grep -rn "config\\.<NAME>" endpoints/``.
"""

from typing import Any, Dict

from api.nba_client import NBAClient
from utils.logger import get_logger

import config


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
#
# ``get_logger`` returns a :class:`utils.correlation.CorrelationAdapter`
# that auto-injects the current correlation-ID context variable into
# every record's extra dict. All three wrappers below share this single
# adapter so the logger name is stable (``endpoints.teams``) and
# correlation propagation is uniform.
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_leaguedashteamstats(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch league-wide team stats (per-game, totals, or advanced) for a season.

    The upstream ``leaguedashteamstats`` endpoint returns a SINGLE
    result-set table named ``LeagueDashTeamStats`` with one row per team
    keyed by ``(TEAM_ID, SEASON_ID)``. The Teams pipeline uses this
    endpoint both as a statistical source for ``teams.csv`` and as the
    enumerator that produces the ``TEAM_ID`` list used to seed
    :func:`fetch_teamgamelog` and
    :func:`fetch_teamdashboardbygeneralsplits`.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance. This
            is the sole HTTP transport in the pipeline (Rule 1); the
            wrapper delegates to ``client.get(endpoint, params)``.
        season: Season string in NBA format, e.g. ``"2025-26"``. See
            :data:`config.DEFAULT_SEASON` for the configured default
            season value.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
            Other accepted values include ``"Playoffs"``,
            ``"Pre Season"``, and ``"All Star"``.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``, the NBA itself).
            ``"10"`` (WNBA) and ``"20"`` (G-League) are also accepted by
            the upstream but are out of scope for this pipeline.
        per_mode: Normalization mode. Defaults to ``"PerGame"``. Other
            accepted values include ``"Totals"``,
            ``"Per100Possessions"``, ``"Per36"``, ``"MinutesPer"``, and
            ``"PerMinute"``.
        measure_type: Statistical family. Defaults to ``"Base"``. Other
            accepted values include ``"Advanced"``, ``"Four Factors"``,
            ``"Misc"``, ``"Scoring"``, ``"Opponent"``, ``"Usage"``, and
            ``"Defense"``.
        **kwargs: Additional NBA Stats filters that override the defaults
            below. Applied via ``params.update(kwargs)`` after the base
            dict is built. This is the documented extension point for
            passing less-common filters (``Conference``, ``Division``,
            ``GameScope``, etc.) without bloating the function
            signature.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The
        response carries a ``resultSets`` array whose
        ``LeagueDashTeamStats`` table is keyed by
        ``(TEAM_ID, SEASON_ID)`` with one row per team.

    Raises:
        requests.exceptions.HTTPError: On a non-transient 4xx response
            from the upstream API after tenacity retry exhaustion.
        requests.exceptions.RequestException: On a persistent transport
            failure (timeout, connection refused, DNS) after tenacity
            retry exhaustion.
    """
    # Literal dict construction per AAP §0.5.1.4 / Phase 7 style
    # constraint ("Use literal dict construction + params.update(kwargs)
    # pattern consistently"). The filter flags PlusMinus/PaceAdjust/Rank
    # are string "N"/"Y" per the NBA Stats API convention — "N" means
    # raw stats (no derived columns). Zero / empty-string defaults
    # match the canonical "no filter applied" upstream semantics.
    params: Dict[str, Any] = {
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "PerMode": per_mode,
        "MeasureType": measure_type,
        "PlusMinus": "N",
        "PaceAdjust": "N",
        "Rank": "N",
        "LastNGames": "0",
        "Month": "0",
        "OpponentTeamID": "0",
        "Period": "0",
        "PORound": "0",
        "TeamID": "0",
        "DateFrom": "",
        "DateTo": "",
        "GameSegment": "",
        "Location": "",
        "Outcome": "",
        "SeasonSegment": "",
        "ShotClockRange": "",
        "VsConference": "",
        "VsDivision": "",
        "Conference": "",
        "Division": "",
        "GameScope": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "TwoWay": "0",
    }
    # ``params.update(kwargs)`` is applied AFTER the literal dict so that
    # caller-supplied overrides win over the defaults — documented
    # contract for the **kwargs passthrough.
    params.update(kwargs)
    # %s-style deferred formatting satisfies the F-008 stdlib-logging
    # mandate and the Observability rule's deferred-formatting
    # requirement: the logging framework only substitutes the argument
    # values if the DEBUG level is actually enabled.
    logger.debug(
        "endpoints.teams.leaguedashteamstats season=%s measure_type=%s per_mode=%s",
        season,
        measure_type,
        per_mode,
    )
    return client.get("leaguedashteamstats", params)


def fetch_teamgamelog(
    client: NBAClient,
    team_id: str,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    date_from: str = "",
    date_to: str = "",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the per-game log for a specific team / season.

    The upstream ``teamgamelog`` endpoint returns a SINGLE result-set
    table named ``TeamGameLog`` with one row per game keyed by
    ``(TEAM_ID, GAME_ID)``. ``TeamID`` is MANDATORY for this endpoint
    — the upstream API rejects the call if ``TeamID`` is missing or set
    to ``"0"``. Callers must therefore enumerate ``TEAM_ID`` values
    (typically from the output of :func:`fetch_leaguedashteamstats`)
    before iterating per team.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        team_id: NBA team identifier. Accepted as either :class:`str`
            (the canonical form, e.g. ``"1610612747"`` for the Los
            Angeles Lakers) or :class:`int` — the value is cast via
            ``str(team_id)`` before inclusion in the params dict, per
            AAP style constraint. Must not be empty.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
            Other accepted values include ``"Playoffs"``,
            ``"Pre Season"``, and ``"All Star"``.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        date_from: Optional inclusive lower date bound in ``MM/DD/YYYY``
            format (e.g. ``"10/22/2024"``). Empty string (the default)
            means no lower bound is applied upstream.
        date_to: Optional inclusive upper date bound in ``MM/DD/YYYY``
            format. Empty string (the default) means no upper bound is
            applied upstream.
        **kwargs: Additional NBA Stats filters that override the
            defaults. Applied via ``params.update(kwargs)`` after the
            base dict is built. Rarely required for this endpoint — the
            upstream surface is narrow.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The
        response carries a ``resultSets`` array whose ``TeamGameLog``
        table is keyed by ``(TEAM_ID, GAME_ID)`` with one row per game
        played by the specified team in the specified season.

    Raises:
        requests.exceptions.HTTPError: On a non-transient 4xx response
            from the upstream API after tenacity retry exhaustion.
        requests.exceptions.RequestException: On a persistent transport
            failure after tenacity retry exhaustion.
    """
    # ``str(team_id)`` is applied at the call site (AAP style
    # constraint) so both integer and string callers produce an
    # identical wire-format value.
    params: Dict[str, Any] = {
        "TeamID": str(team_id),
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "DateFrom": date_from,
        "DateTo": date_to,
    }
    params.update(kwargs)
    # Per AAP §0.5.1.4 Phase 3 the prescribed log line logs team_id and
    # season only — the other parameters are either defaults or empty
    # strings in the common case and would bloat the log without
    # adding diagnostic value.
    logger.debug(
        "endpoints.teams.teamgamelog team_id=%s season=%s",
        team_id,
        season,
    )
    return client.get("teamgamelog", params)


def fetch_teamdashboardbygeneralsplits(
    client: NBAClient,
    team_id: str,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the general-splits dashboard for a specific team / season.

    This is the ONLY Teams-domain endpoint whose response envelope is
    MULTI-TABLE. The upstream ``teamdashboardbygeneralsplits`` endpoint
    returns six result-set tables in a single call:

    * ``OverallTeamDashboard`` — single-row overall aggregate for the
      specified team and season.
    * ``LocationTeamDashboard`` — split by Home / Road (two rows).
    * ``WinsLossesTeamDashboard`` — split by Win / Loss (two rows).
    * ``MonthTeamDashboard`` — per-calendar-month rows (six to eight
      rows depending on season schedule).
    * ``PrePostAllStarTeamDashboard`` — split by Pre / Post All-Star
      Break (two rows).
    * ``DaysRestTeamDashboard`` — rows by days-rest bucket (0, 1, 2, 3+
      days).

    Each table carries the composite key
    ``(TEAM_ID, SEASON_ID, GROUP_SET, GROUP_VALUE)``. Downstream
    normalization in :mod:`utils.schema_normalizer` flattens each table
    independently into a separate DataFrame; :mod:`pipelines.ingest_teams`
    decides which tables to persist into ``teams.csv``.

    ``TeamID`` is MANDATORY for this endpoint — the upstream API
    rejects the call without it. Callers must therefore enumerate
    ``TEAM_ID`` values (typically from the output of
    :func:`fetch_leaguedashteamstats`) before iterating per team.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        team_id: NBA team identifier (10-digit franchise ID). Accepted
            as either :class:`str` or :class:`int` — the value is cast
            via ``str(team_id)`` before inclusion in the params dict.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``. Other
            accepted values include ``"Totals"`` and
            ``"Per100Possessions"``.
        measure_type: Statistical family. Defaults to ``"Base"``. Other
            accepted values include ``"Advanced"``, ``"Four Factors"``,
            ``"Misc"``, ``"Scoring"``, ``"Opponent"``, ``"Usage"``, and
            ``"Defense"``.
        **kwargs: Additional NBA Stats filters that override the
            defaults. Applied via ``params.update(kwargs)`` after the
            base dict is built.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The
        response carries a ``resultSets`` array with the SIX dashboard
        result-set tables listed above.

    Raises:
        requests.exceptions.HTTPError: On a non-transient 4xx response
            from the upstream API after tenacity retry exhaustion.
        requests.exceptions.RequestException: On a persistent transport
            failure after tenacity retry exhaustion.
    """
    # The team-level filter surface is slightly smaller than the
    # player-level equivalent (no College / DraftYear / PlayerExperience
    # / Height / Weight filters) — the literal dict reflects exactly
    # what the NBA Stats API accepts for this endpoint.
    params: Dict[str, Any] = {
        "TeamID": str(team_id),
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "PerMode": per_mode,
        "MeasureType": measure_type,
        "PlusMinus": "N",
        "PaceAdjust": "N",
        "Rank": "N",
        "LastNGames": "0",
        "Month": "0",
        "OpponentTeamID": "0",
        "Period": "0",
        "PORound": "0",
        "DateFrom": "",
        "DateTo": "",
        "GameSegment": "",
        "Location": "",
        "Outcome": "",
        "SeasonSegment": "",
        "ShotClockRange": "",
        "VsConference": "",
        "VsDivision": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.teams.teamdashboardbygeneralsplits team_id=%s season=%s measure_type=%s",
        team_id,
        season,
        measure_type,
    )
    return client.get("teamdashboardbygeneralsplits", params)
