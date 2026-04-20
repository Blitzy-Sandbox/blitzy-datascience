"""Teams domain endpoint wrappers (Feature F-010).

This module wraps the three NBA Stats endpoints that feed the Teams data
domain:

* ``leaguedashteamstats`` — league-wide per-team aggregates for a season;
  returns one row per team in a single call.
* ``teamgamelog`` — per-team game log (one row per game) for a specific
  ``TeamID`` and ``Season``.
* ``teamdashboardbygeneralsplits`` — multi-facet dashboard for a single
  ``TeamID`` / ``Season`` whose response decomposes into six result-set
  tables (Overall, Location, WinsLosses, Month, PrePostAllStar,
  DaysRest). This is the only endpoint in the Teams domain whose
  response is multi-table.

Rule compliance
---------------

All three wrappers route through :class:`api.nba_client.NBAClient`
(Rule 1). No wrapper imports ``requests`` or ``pandas``; no wrapper
writes CSV output (Rule 7).

Observability
-------------

Each wrapper emits a DEBUG log record with ``%s`` placeholders before
the :meth:`api.nba_client.NBAClient.get` call. Only safe parameter
values (``team_id``, ``season``, ``season_type``) are logged — never
full request/response bodies.
"""

from typing import Any, Dict

from api.nba_client import NBAClient
from utils.logger import get_logger

import config

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def fetch_leaguedashteamstats(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    per_mode: str = "PerGame",
    measure_type: str = "Base",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch league-wide per-team statistics for a season.

    The response envelope returns a single result-set table
    (``LeagueDashTeamStats``) with one row per team. The pipeline uses
    this as the primary source for ``teams.csv``. The per-row key is
    ``(TEAM_ID, SEASON_ID)``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``. Other
            accepted values include ``"Totals"``, ``"Per100Possessions"``,
            ``"Per36"``, ``"MinutesPer"``, and ``"PerMinute"``.
        measure_type: Statistical family. Defaults to ``"Base"``. Other
            accepted values include ``"Advanced"``, ``"Four Factors"``,
            ``"Misc"``, ``"Scoring"``, ``"Opponent"``, ``"Usage"``, and
            ``"Defense"``.
        **kwargs: Additional NBA Stats filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``LeagueDashTeamStats`` result-set.

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
        "GameScope": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "TwoWay": "0",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.teams.leaguedashteamstats season=%s season_type=%s per_mode=%s measure_type=%s",
        season,
        season_type,
        per_mode,
        measure_type,
    )
    return client.get("leaguedashteamstats", params)


def fetch_teamgamelog(
    client: NBAClient,
    team_id: str,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the per-game log for a single team / season.

    The response envelope returns a single result-set table
    (``TeamGameLog``) with one row per game — keyed by
    ``(TEAM_ID, GAME_ID)``. Downstream normalization contributes rows
    to ``teams.csv``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        team_id: NBA team identifier as a string (10-digit franchise
            ID, e.g. ``"1610612747"`` for the Los Angeles Lakers).
            Callers passing integer IDs should :func:`str`-cast first.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.
            Recognized filters include ``DateFrom`` and ``DateTo``.

    Returns:
        Raw JSON envelope with the ``TeamGameLog`` result-set.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "TeamID": str(team_id),
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "DateFrom": "",
        "DateTo": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.teams.teamgamelog team_id=%s season=%s season_type=%s",
        team_id,
        season,
        season_type,
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
    """Fetch the multi-facet general-splits dashboard for a team / season.

    This is the ONLY Teams-domain endpoint whose response envelope is
    multi-table. The response carries six result-set tables:

    * ``OverallTeamDashboard`` — single-row overall aggregate.
    * ``LocationTeamDashboard`` — Home / Road split.
    * ``WinsLossesTeamDashboard`` — W / L split.
    * ``MonthTeamDashboard`` — per-calendar-month rows.
    * ``PrePostAllStarTeamDashboard`` — Pre / Post All-Star split.
    * ``DaysRestTeamDashboard`` — rows by days-rest bucket.

    Each table carries the composite key
    ``(TEAM_ID, SEASON_ID, GROUP_SET, GROUP_VALUE)``. Downstream
    normalization contributes rows to ``teams.csv``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        team_id: NBA team identifier as a string (10-digit franchise
            ID). Callers passing integer IDs should :func:`str`-cast
            first.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        per_mode: Normalization mode. Defaults to ``"PerGame"``.
        measure_type: Statistical family. Defaults to ``"Base"``.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the six dashboard result-sets.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
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
        "endpoints.teams.teamdashboardbygeneralsplits team_id=%s season=%s per_mode=%s measure_type=%s",
        team_id,
        season,
        per_mode,
        measure_type,
    )
    return client.get("teamdashboardbygeneralsplits", params)
