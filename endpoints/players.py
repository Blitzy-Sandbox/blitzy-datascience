"""Players domain endpoint wrappers (Feature F-009).

This module wraps five NBA Stats endpoints that feed the Players data
domain plus the player-tracking artifact (``player_tracking.csv``):

* ``leaguedashplayerstats`` — league-wide per-player aggregates; one row
  per player in a single call.
* ``leaguedashplayerclutch`` — per-player clutch-time aggregates;
  shares the endpoint name with :mod:`endpoints.lineups`, but the
  player-domain wrapper returns the per-player aggregate envelope while
  the lineups-domain wrapper requests on/off-court splits.
* ``playercareerstats`` — a single player's complete career history
  across regular season, post-season, all-star, college, and preseason
  tables. UNIQUE parameter surface: this endpoint does NOT accept a
  ``Season`` parameter; ``PlayerID`` is the only required input.
* ``playergamelog`` — a single player's per-game log for a specific
  ``Season``.
* ``leaguedashptstats`` — league-wide player-tracking aggregates (speed,
  distance, passing, rebounding, etc.). This wrapper lives in the
  Players module even though its output contributes to
  ``player_tracking.csv`` rather than ``players.csv``; the placement is
  dictated by :doc:`docs/api/endpoints_catalog.md` §7 and the Agent
  Action Plan (Feature F-009 explicitly owns the tracking artifact).

Endpoint reuse note
-------------------

``leaguedashplayerclutch`` is called from TWO wrappers:

* :func:`fetch_leaguedashplayerclutch` in this module — per-player
  basic aggregates; contributes to ``players.csv``.
* :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff` — on/off
  court splits used by :mod:`pipelines.ingest_lineups`; contributes to
  ``lineups.csv``.

Both call :meth:`api.nba_client.NBAClient.get` with the same endpoint
string ``"leaguedashplayerclutch"``; the distinct pipelines consume
different result-set projections. The 15-endpoint count in the Agent
Action Plan counts this endpoint once under Players and once under
Lineups.

Rule compliance
---------------

All five wrappers route through :class:`api.nba_client.NBAClient`
(Rule 1). No wrapper imports ``requests`` or ``pandas``; no wrapper
writes CSV output (Rule 7).

Observability
-------------

Each wrapper emits a DEBUG log record with ``%s`` placeholders before
the :meth:`api.nba_client.NBAClient.get` call. Only safe parameter
values are logged — never full request/response bodies.
"""

from typing import Any, Dict, Optional  # noqa: F401  (Optional reserved for future type-annotation flexibility)

from api.nba_client import NBAClient
from utils.logger import get_logger

import config

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def fetch_leaguedashplayerstats(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch league-wide per-player statistics for a season.

    The response envelope returns a single result-set table
    (``LeagueDashPlayerStats``) with one row per player-team-season
    triple. The pipeline uses this as the primary source for
    ``players.csv``. Per-row key: ``(PLAYER_ID, SEASON_ID, TEAM_ID)``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``.
        measure_type: Statistical family. Defaults to ``"Base"``. Other
            accepted values include ``"Advanced"``, ``"Misc"``,
            ``"Scoring"``, ``"Usage"``, ``"Opponent"``, ``"Defense"``.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``LeagueDashPlayerStats`` result-set.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
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
        "College": "",
        "Country": "",
        "DraftPick": "",
        "DraftYear": "",
        "GameScope": "",
        "Height": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "TwoWay": "0",
        "Weight": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.players.leaguedashplayerstats season=%s season_type=%s per_mode=%s measure_type=%s",
        season,
        season_type,
        per_mode,
        measure_type,
    )
    return client.get("leaguedashplayerstats", params)


def fetch_leaguedashplayerclutch(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    clutch_time: str = "Last 5 Minutes",
    ahead_behind: str = "Ahead or Behind",
    point_diff: str = "5",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch per-player clutch-time aggregates (players-domain variant).

    This wrapper produces the per-player clutch envelope consumed by
    :mod:`pipelines.ingest_players` (contributes to ``players.csv``).
    It is DIFFERENT from
    :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff` which
    hits the same upstream endpoint name but consumes on/off-court
    split projections for the Lineups domain.

    The canonical NBA definition of "clutch" is the configurable
    three-dimensional filter ``(ClutchTime, AheadBehind, PointDiff)``.
    Defaults encode the most common NBA clutch definition:
    ``"Last 5 Minutes"`` of game, ``"Ahead or Behind"`` (either team can
    win or lose), ``PointDiff="5"`` (within 5 points).

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``.
        measure_type: Statistical family. Defaults to ``"Base"``.
        clutch_time: Clutch-time window selector. Defaults to
            ``"Last 5 Minutes"``. Other accepted values: ``"Last 4
            Minutes"``, ``"Last 3 Minutes"``, ``"Last 2 Minutes"``,
            ``"Last 1 Minute"``, ``"Last 30 Seconds"``, ``"Last 10
            Seconds"``.
        ahead_behind: Score-state filter. Defaults to ``"Ahead or
            Behind"``. Other accepted values: ``"Behind or Tied"``,
            ``"Ahead or Tied"``.
        point_diff: Maximum point differential as a string. Defaults to
            ``"5"``. Callers passing numeric input should :func:`str`-cast
            first.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``LeagueDashPlayerClutch`` result-set.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "PerMode": per_mode,
        "MeasureType": measure_type,
        "ClutchTime": clutch_time,
        "AheadBehind": ahead_behind,
        "PointDiff": str(point_diff),
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
        "College": "",
        "Country": "",
        "DraftPick": "",
        "DraftYear": "",
        "GameScope": "",
        "Height": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "Weight": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.players.leaguedashplayerclutch season=%s clutch_time=%s ahead_behind=%s",
        season,
        clutch_time,
        ahead_behind,
    )
    return client.get("leaguedashplayerclutch", params)


def fetch_playercareerstats(
    client: NBAClient,
    player_id: str,
    per_mode: str = "PerGame",
    league_id: str = config.DEFAULT_LEAGUE_ID,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the complete career-history envelope for a single player.

    .. important::

       This endpoint has a UNIQUE parameter surface among the 15
       wrappers in the pipeline: it does NOT accept a ``Season`` filter.
       The upstream returns the player's complete career across every
       season and every competition level in a single response.
       Callers that need per-season slices must filter the resulting
       DataFrames in the pipeline layer.

    The response envelope is MULTI-TABLE. Accepted result-set tables
    include:

    * ``SeasonTotalsRegularSeason``
    * ``CareerTotalsRegularSeason``
    * ``SeasonTotalsPostSeason``
    * ``CareerTotalsPostSeason``
    * ``SeasonTotalsAllStarSeason``
    * ``CareerTotalsAllStarSeason``
    * ``SeasonTotalsCollegeSeason``
    * ``CareerTotalsCollegeSeason``
    * ``SeasonTotalsPreseason``
    * ``CareerTotalsPreseason``
    * ``SeasonRankingsRegularSeason``
    * ``SeasonRankingsPostSeason``

    Downstream normalization contributes season-level rows to
    ``players.csv``; career aggregates and rankings tables are
    informational and may be dropped by the pipeline.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        player_id: NBA player identifier as a string. Callers passing
            integer IDs should :func:`str`-cast first.
        per_mode: Normalization mode. Defaults to ``"PerGame"``. Other
            accepted values include ``"Totals"`` and ``"Per36"``.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``). The upstream
            uses this filter to restrict the career scope to the NBA
            rather than including WNBA or G-League records for
            multi-league players.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with up to 12 career result-set tables.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "PlayerID": str(player_id),
        "PerMode": per_mode,
        "LeagueID": league_id,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.players.playercareerstats player_id=%s per_mode=%s league_id=%s",
        player_id,
        per_mode,
        league_id,
    )
    return client.get("playercareerstats", params)


def fetch_playergamelog(
    client: NBAClient,
    player_id: str,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    date_from: str = "",
    date_to: str = "",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the per-game log for a single player / season.

    The response envelope returns a single result-set table
    (``PlayerGameLog``) with one row per game — keyed by
    ``(PLAYER_ID, GAME_ID)``. Downstream normalization contributes rows
    to ``players.csv``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        player_id: NBA player identifier as a string. Callers passing
            integer IDs should :func:`str`-cast first.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        date_from: Inclusive lower-bound date filter in NBA Stats format
            (``"MM/DD/YYYY"``). Defaults to ``""`` which applies no
            lower bound — the NBA Stats API convention for "unfiltered"
            is the literal empty string, not the absence of the key.
        date_to: Inclusive upper-bound date filter in NBA Stats format
            (``"MM/DD/YYYY"``). Defaults to ``""`` (no upper bound).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``PlayerGameLog`` result-set.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "PlayerID": str(player_id),
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "DateFrom": date_from,
        "DateTo": date_to,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.players.playergamelog player_id=%s season=%s season_type=%s",
        player_id,
        season,
        season_type,
    )
    return client.get("playergamelog", params)


def fetch_leaguedashptstats(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    pt_measure_type: str = "SpeedDistance",
    player_or_team: str = "Player",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch league-wide player-tracking statistics for a season.

    This wrapper backs the ``player_tracking.csv`` artifact. It lives in
    :mod:`endpoints.players` (not in a dedicated tracking module) per
    the Agent Action Plan Feature F-009, which explicitly owns both
    ``players.csv`` and ``player_tracking.csv``.

    Unlike the other four Players wrappers, ``leaguedashptstats``
    requires TWO extra distinguishing parameters:

    * ``PtMeasureType`` — selects the tracking family returned by the
      response. The 12 accepted values are: ``"SpeedDistance"``,
      ``"Rebounding"``, ``"Possessions"``, ``"CatchShoot"``,
      ``"PullUpShot"``, ``"Defense"``, ``"Drives"``, ``"Passing"``,
      ``"ElbowTouch"``, ``"PostTouch"``, ``"PaintTouch"``,
      ``"Efficiency"``. Each value produces a different column set in
      the response. Defaults to ``"SpeedDistance"`` — the most
      commonly-useful tracking metric set; callers that need richer
      ``player_tracking.csv`` coverage iterate over every value.
    * ``PlayerOrTeam`` — ``"Player"`` (default, per-player rows) or
      ``"Team"`` (per-team rows). The pipeline conventionally requests
      ``"Player"`` to populate ``player_tracking.csv``; a future
      tracking-by-team artifact could reuse this wrapper with
      ``"Team"``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``. Other
            accepted value: ``"Totals"``.
        pt_measure_type: Tracking family selector. Defaults to
            ``"SpeedDistance"``. See the 12 accepted values listed above.
        player_or_team: ``"Player"`` (default) or ``"Team"``.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``LeagueDashPtStats`` result-set
        whose columns vary by ``pt_measure_type``.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "PerMode": per_mode,
        "PtMeasureType": pt_measure_type,
        "PlayerOrTeam": player_or_team,
        "LastNGames": "0",
        "Month": "0",
        "OpponentTeamID": "0",
        "TeamID": "0",
        "DateFrom": "",
        "DateTo": "",
        "GameScope": "",
        "Location": "",
        "Outcome": "",
        "SeasonSegment": "",
        "VsConference": "",
        "VsDivision": "",
        "College": "",
        "Conference": "",
        "Country": "",
        "DraftPick": "",
        "DraftYear": "",
        "Division": "",
        "Height": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "Weight": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.players.leaguedashptstats season=%s pt_measure_type=%s player_or_team=%s per_mode=%s",
        season,
        pt_measure_type,
        player_or_team,
        per_mode,
    )
    return client.get("leaguedashptstats", params)
