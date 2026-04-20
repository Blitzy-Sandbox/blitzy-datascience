"""Games domain endpoint wrappers (Feature F-011).

This module wraps the four NBA Stats endpoints that feed the Games data
domain:

* ``scoreboardv2`` — daily scoreboard / schedule header with line scores
  and standings snapshots.
* ``boxscoretraditionalv2`` — traditional per-player and per-team box
  scores for a single ``GAME_ID``.
* ``boxscoreadvancedv2`` — advanced / four-factors box scores for a
  single ``GAME_ID``.
* ``playbyplayv2`` — event-by-event play-by-play stream for a single
  ``GAME_ID``.

Parameter surface differences
-----------------------------

The four wrappers intentionally expose three distinct parameter
surfaces, mirroring the upstream NBA Stats contracts documented in
:doc:`docs/api/endpoints_catalog.md` §6:

* ``scoreboardv2`` accepts a ``GameDate`` (ISO-8601 ``YYYY-MM-DD``), a
  ``LeagueID``, and a ``DayOffset`` (defaults to ``"0"`` for the exact
  date).
* The two box-score endpoints share the same six required parameters:
  ``GameID``, ``StartPeriod``, ``EndPeriod``, ``StartRange``,
  ``EndRange``, ``RangeType``. The ``Range`` triplet encodes a time
  window in tenths of a second — ``StartRange=0``, ``EndRange=28800``,
  ``RangeType=0`` expresses "entire game" (48 minutes * 60 seconds *
  10 tenths = 28,800).
* ``playbyplayv2`` has a NARROWER parameter surface: it accepts only
  ``GameID``, ``StartPeriod``, and ``EndPeriod`` — it does NOT accept
  the ``Range`` triplet. Supplying the triplet here is an upstream
  validation error.

Rule compliance
---------------

All four wrappers route through :class:`api.nba_client.NBAClient`
(Rule 1). No wrapper imports ``requests`` or ``pandas``; no wrapper
writes CSV output (Rule 7). Parameter values are forwarded verbatim to
the transport layer — validation at the trust boundary is enforced by
:meth:`api.nba_client.NBAClient.get`.

Observability
-------------

Each wrapper emits a DEBUG log record with ``%s`` placeholders before
the :meth:`api.nba_client.NBAClient.get` call. The log records contain
only parameter values that are safe at DEBUG (``game_date``, ``game_id``,
``start_period``, ``end_period``) — never full request/response bodies.
"""

from typing import Any, Dict

from api.nba_client import NBAClient
from utils.logger import get_logger

import config

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def fetch_scoreboardv2(
    client: NBAClient,
    game_date: str,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    day_offset: str = "0",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the scoreboard / standings envelope for a single date.

    The ``scoreboardv2`` response is multi-table: it includes
    ``GameHeader``, ``LineScore``, ``SeriesStandings``, ``LastMeeting``,
    ``EastConfStandingsByDay``, ``WestConfStandingsByDay``, ``Available``,
    ``TeamLeaders``, ``TicketLinks``, and ``WinProbability``. Downstream
    normalization produces rows contributing to ``games.csv``.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_date: ISO-8601 date string (``YYYY-MM-DD``) — e.g.
            ``"2025-10-21"``. NBA Stats is strict about this format and
            will return an empty envelope for other shapes.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        day_offset: Day offset string applied to ``game_date``. Defaults
            to ``"0"`` (exact date). The upstream accepts negative and
            positive integer strings (e.g. ``"-1"``, ``"1"``); callers
            passing integers should :func:`str`-cast first.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the multi-table ``resultSets`` array.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameDate": game_date,
        "LeagueID": league_id,
        "DayOffset": day_offset,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.games.scoreboardv2 game_date=%s league_id=%s day_offset=%s",
        game_date,
        league_id,
        day_offset,
    )
    return client.get("scoreboardv2", params)


def fetch_boxscoretraditionalv2(
    client: NBAClient,
    game_id: str,
    start_period: str = "0",
    end_period: str = "10",
    start_range: str = "0",
    end_range: str = "28800",
    range_type: str = "0",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the traditional box score for a single ``GAME_ID``.

    The response envelope carries two result-set tables: ``PlayerStats``
    (one row per player, keyed by ``(GAME_ID, PLAYER_ID)``) and
    ``TeamStats`` (one row per team, keyed by ``(GAME_ID, TEAM_ID)``).
    Both tables contribute rows to ``games.csv``.

    The ``Range`` triplet (``StartRange`` / ``EndRange`` / ``RangeType``)
    encodes a time window in tenths of a second:

    * ``RangeType=0`` with ``StartRange=0`` and ``EndRange=28800`` is the
      canonical "whole game" request (48 min * 60 s * 10 tenths).
    * Other values are reserved for partial-window requests; the pipeline
      does not use them today.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier, e.g.
            ``"0022500001"``. Callers passing numeric ``GAME_ID`` objects
            should :func:`str`-cast first.
        start_period: Starting period as a string. Defaults to ``"0"``
            (all periods from game start).
        end_period: Ending period as a string. Defaults to ``"10"``
            (covers overtime periods up to 5OT).
        start_range: Start of the time window in tenths of a second.
            Defaults to ``"0"``.
        end_range: End of the time window in tenths of a second. Defaults
            to ``"28800"`` (end of regulation).
        range_type: Range-type selector. Defaults to ``"0"`` (whole game).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with ``PlayerStats`` and ``TeamStats`` tables.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": game_id,
        "StartPeriod": start_period,
        "EndPeriod": end_period,
        "StartRange": start_range,
        "EndRange": end_range,
        "RangeType": range_type,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.games.boxscoretraditionalv2 game_id=%s start_period=%s end_period=%s",
        game_id,
        start_period,
        end_period,
    )
    return client.get("boxscoretraditionalv2", params)


def fetch_boxscoreadvancedv2(
    client: NBAClient,
    game_id: str,
    start_period: str = "0",
    end_period: str = "10",
    start_range: str = "0",
    end_range: str = "28800",
    range_type: str = "0",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the advanced box score for a single ``GAME_ID``.

    The response envelope carries the same two result-set table names
    as :func:`fetch_boxscoretraditionalv2` (``PlayerStats``,
    ``TeamStats``), but each row exposes advanced metrics such as
    ``OFF_RATING``, ``DEF_RATING``, ``NET_RATING``, ``AST_PCT``,
    ``REB_PCT``, ``EFG_PCT``, ``TS_PCT``, ``USG_PCT``, ``PACE``, and
    ``PIE``. Downstream normalization contributes these rows to
    ``games.csv`` alongside the traditional box-score columns.

    The parameter surface is IDENTICAL to
    :func:`fetch_boxscoretraditionalv2` — including the ``Range`` triplet
    which encodes the requested time window.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier.
        start_period: Starting period as a string. Defaults to ``"0"``.
        end_period: Ending period as a string. Defaults to ``"10"``.
        start_range: Start of the time window in tenths of a second.
            Defaults to ``"0"``.
        end_range: End of the time window in tenths of a second. Defaults
            to ``"28800"``.
        range_type: Range-type selector. Defaults to ``"0"``.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with ``PlayerStats`` and ``TeamStats`` tables
        populated with advanced metrics.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": game_id,
        "StartPeriod": start_period,
        "EndPeriod": end_period,
        "StartRange": start_range,
        "EndRange": end_range,
        "RangeType": range_type,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.games.boxscoreadvancedv2 game_id=%s start_period=%s end_period=%s",
        game_id,
        start_period,
        end_period,
    )
    return client.get("boxscoreadvancedv2", params)


def fetch_playbyplayv2(
    client: NBAClient,
    game_id: str,
    start_period: str = "0",
    end_period: str = "10",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the play-by-play event stream for a single ``GAME_ID``.

    The response envelope carries two result-set tables: ``PlayByPlay``
    (one row per event, keyed by ``(GAME_ID, EVENTNUM)``) and
    ``AvailableVideo`` (auxiliary). Downstream normalization contributes
    the ``PlayByPlay`` table to ``play_by_play.csv``; ``AvailableVideo``
    is informational and may be dropped by the pipeline.

    .. important::

       Unlike :func:`fetch_boxscoretraditionalv2` and
       :func:`fetch_boxscoreadvancedv2`, this wrapper does NOT accept
       the ``Range`` triplet (``StartRange`` / ``EndRange`` /
       ``RangeType``). Supplying those parameters is an upstream
       validation error — pass ``start_period`` and ``end_period`` to
       constrain the event window instead.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier, e.g.
            ``"0022500001"``.
        start_period: Starting period as a string. Defaults to ``"0"``
            (include all periods from game start).
        end_period: Ending period as a string. Defaults to ``"10"`` to
            include overtime periods up to 5OT.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``PlayByPlay`` and ``AvailableVideo``
        tables.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": game_id,
        "StartPeriod": start_period,
        "EndPeriod": end_period,
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.games.playbyplayv2 game_id=%s start_period=%s end_period=%s",
        game_id,
        start_period,
        end_period,
    )
    return client.get("playbyplayv2", params)
