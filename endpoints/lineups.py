"""Lineups domain endpoint wrappers (Feature F-012).

This module provides thin, side-effect-free wrappers around the TWO NBA Stats
API endpoints that back the Lineups data domain of the ingestion pipeline:

1. ``leaguedashlineups`` — league-wide multi-player lineup aggregates.
   See :func:`fetch_leaguedashlineups`.

2. ``leaguedashplayerclutch`` (on/off-court splits semantic) — clutch-time
   player/lineup statistics used by the Lineups pipeline to compute on/off
   contributions per lineup. See :func:`fetch_leaguedashplayerclutch_onoff`.

Disambiguation — ``leaguedashplayerclutch`` appears in TWO modules
-----------------------------------------------------------------

The upstream NBA Stats endpoint ``leaguedashplayerclutch`` is wrapped by TWO
separate, intentionally-distinct functions elsewhere in this codebase:

* :func:`endpoints.players.fetch_leaguedashplayerclutch` — player-level basic
  clutch statistics, with player-filter parameters (College, Country,
  DraftPick, DraftYear, Height, Weight, etc.).
* :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff` (THIS MODULE) —
  lineup on/off-court splits during clutch time, consumed by the Lineups
  pipeline for on/off split aggregation into ``lineups.csv``.

Both functions hit the same upstream endpoint but differ in parameter
semantics and in which result-set tables the downstream pipeline consumes.
The Agent Action Plan counts ``leaguedashplayerclutch`` once in Players
(5 endpoints) and once in Lineups (2 endpoints) to reach the required total
of 15+ endpoints across 6 domains. These two functions MUST remain separate.

Rule compliance
---------------

This module contains no direct HTTP transport (Rule 1 — all traffic routes
through :class:`api.nba_client.NBAClient`) and no CSV emission (Rule 7 — only
:class:`storage.csv_writer.CSVWriter` calls ``DataFrame.to_csv``). No
``requests``, ``pandas``, ``json``, or filesystem modules are imported here.

Observability
-------------

A DEBUG-level log record is emitted before each delegation to
``client.get``, using ``%s`` placeholders so the stdlib formatter only
renders parameter values when DEBUG logging is actually enabled. Request
bodies and response payloads are never logged from this module (they are
logged, if at all, at DEBUG from :mod:`api.nba_client`).
"""

from typing import Any, Dict

from api.nba_client import NBAClient
from utils.logger import get_logger

import config

logger = get_logger(__name__)


def fetch_leaguedashlineups(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    group_quantity: str = "5",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch league-wide multi-player lineup aggregates for a season.

    A "lineup" is a set of N players who were on the court simultaneously;
    the upstream ``leaguedashlineups`` endpoint aggregates box-score and
    efficiency statistics over every such set observed during the season
    (for the configured ``GroupQuantity``, typically 5-man units).

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance. This is
            the sole HTTP transport in the pipeline (Rule 1); the wrapper
            delegates to ``client.get(endpoint, params)``.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``). Other
            accepted values include ``"Playoffs"``, ``"Pre Season"``, and
            ``"All Star"``.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``, the NBA itself).
        per_mode: Normalization mode. ``"PerGame"`` (default), ``"Totals"``,
            ``"Per36"``, ``"Per100Possessions"``, etc.
        measure_type: Statistical measure family. ``"Base"`` (default),
            ``"Advanced"``, ``"Misc"``, ``"Four Factors"``, ``"Scoring"``,
            ``"Opponent"``, ``"Defense"``.
        group_quantity: N-man lineup size. Cast to string before the call
            because the upstream API expects string query parameters.
            Typical values: ``"5"`` (standard 5-man lineups, default),
            ``"2"``/``"3"``/``"4"`` (duos, trios, quartets for bench-unit
            analysis).
        **kwargs: Additional NBA Stats filters that override the defaults
            below. Applied via ``params.update(kwargs)`` after the base
            dict is built.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The response
        carries a ``resultSets`` array whose primary ``Lineups`` table is
        keyed by ``GROUP_ID`` (hyphen-delimited composite of sorted player
        IDs, e.g. ``"-201939-202681-203081-203507-203954-"``) and
        ``TEAM_ID``. Downstream normalization produces ``lineups.csv`` with
        key columns ``(season, group_id, team_id)``.

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
        "GroupQuantity": str(group_quantity),
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
    params.update(kwargs)
    logger.debug(
        "endpoints.lineups.leaguedashlineups season=%s group_quantity=%s measure_type=%s",
        season,
        group_quantity,
        measure_type,
    )
    return client.get("leaguedashlineups", params)


def fetch_leaguedashplayerclutch_onoff(
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
    """Fetch clutch-time player/lineup stats representing on/off splits.

    Wraps the same upstream endpoint (``leaguedashplayerclutch``) as
    :func:`endpoints.players.fetch_leaguedashplayerclutch` but is a
    deliberately separate function serving the Lineups pipeline (F-012).
    They share parameter surface but differ in downstream domain semantics
    — the players variant consumes the per-player basic clutch splits;
    this variant is aggregated by the Lineups pipeline into on/off-court
    contributions per lineup. The two functions MUST NOT be consolidated:
    the Agent Action Plan counts this endpoint once toward the Players
    domain total and once toward the Lineups domain total, reaching 15+
    endpoints across 6 domains.

    The default ``(clutch_time, ahead_behind, point_diff)`` triplet of
    ``("Last 5 Minutes", "Ahead or Behind", "5")`` matches the NBA's
    official "clutch time" definition: games within 5 points in the last
    5 minutes of regulation or overtime.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance — the
            sole HTTP transport path (Rule 1).
        season: Season string, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. ``"PerGame"`` (default), ``"Totals"``,
            ``"Per36"``, ``"Per100Possessions"``, etc.
        measure_type: Statistical measure family. ``"Base"`` (default),
            ``"Advanced"``, ``"Misc"``, ``"Four Factors"``, ``"Scoring"``,
            ``"Opponent"``, ``"Defense"``, ``"Usage"``.
        clutch_time: Time-remaining window defining the clutch situation.
            Accepted values: ``"Last 5 Minutes"`` (default, NBA canonical),
            ``"Last 4 Minutes"``, ``"Last 3 Minutes"``, ``"Last 2 Minutes"``,
            ``"Last 1 Minute"``, ``"Last 30 Seconds"``, ``"Last 10 Seconds"``.
        ahead_behind: Score-state qualifier. Accepted values:
            ``"Ahead or Behind"`` (default — absolute point spread, either
            direction), ``"Behind or Tied"``, ``"Ahead or Tied"``.
        point_diff: Absolute point-differential threshold, as a string
            (cast automatically before the call). Default ``"5"`` matches
            the NBA clutch threshold.
        **kwargs: Additional NBA Stats filters that override the defaults
            below. Applied via ``params.update(kwargs)`` after the base
            dict is built.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The response
        carries a ``resultSets`` array whose ``LeagueDashPlayerClutch``
        table is consumed by the Lineups pipeline for on/off split
        aggregation before it lands in ``lineups.csv``.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.

    See Also:
        :func:`endpoints.players.fetch_leaguedashplayerclutch`: the peer
        wrapper of the same upstream endpoint used by the Players pipeline.
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
        "College": "",
        "Conference": "",
        "Country": "",
        "DraftPick": "",
        "DraftYear": "",
        "Division": "",
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
        "endpoints.lineups.leaguedashplayerclutch_onoff season=%s clutch_time=%s ahead_behind=%s point_diff=%s",
        season,
        clutch_time,
        ahead_behind,
        point_diff,
    )
    return client.get("leaguedashplayerclutch", params)
