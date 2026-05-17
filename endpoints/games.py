"""Games domain endpoint wrappers (Feature F-011).

This module wraps the four NBA Stats endpoints that feed the Games data
domain:

* ``scoreboardv2`` — daily scoreboard / schedule header with line scores
  and standings snapshots. The primary season-wide enumeration path is
  :func:`endpoints.schedule.enumerate_game_ids` (``leaguegamefinder``);
  :func:`fetch_scoreboardv2` provides a per-day alternative that is
  useful for incremental runs or date-specific investigations.
* ``boxscoretraditionalv2`` — traditional per-player and per-team box
  scores for a single ``GAME_ID`` (points, rebounds, assists, etc.).
* ``boxscoreadvancedv2`` — advanced / four-factors box scores for a
  single ``GAME_ID`` (offensive/defensive rating, true shooting %,
  usage %, pace, PIE, etc.).
* ``playbyplayv2`` — event-by-event play-by-play stream for a single
  ``GAME_ID``. The v2 endpoint supersedes the deprecated ``playbyplay``
  (v1) endpoint and returns additional per-event metadata.

Parameter surface differences
-----------------------------

The four wrappers intentionally expose three distinct parameter
surfaces, mirroring the upstream NBA Stats contracts:

* :func:`fetch_scoreboardv2` accepts a ``GameDate`` (ISO-8601
  ``YYYY-MM-DD``), a ``LeagueID``, and a ``DayOffset`` (defaults to
  ``"0"`` for the exact date).
* :func:`fetch_boxscoretraditionalv2` and
  :func:`fetch_boxscoreadvancedv2` share the same six required
  parameters: ``GameID``, ``StartPeriod``, ``EndPeriod``, ``StartRange``,
  ``EndRange``, ``RangeType``. The ``Range`` triplet encodes a time
  window in tenths of a second — ``StartRange=0``, ``EndRange=28800``,
  ``RangeType=0`` expresses "entire game" (48 minutes × 60 seconds ×
  10 tenths = 28,800).
* :func:`fetch_playbyplayv2` has a NARROWER parameter surface: it
  accepts only ``GameID``, ``StartPeriod``, and ``EndPeriod`` — it
  does NOT accept the ``Range`` triplet. Supplying the triplet here is
  an upstream validation error; callers should restrict the event
  window via period bounds instead.

Rule compliance
---------------

All four wrappers route through :class:`api.nba_client.NBAClient`
(Rule 1 — Single HTTP Client). No wrapper imports :mod:`requests`,
:mod:`pandas`, :mod:`json`, or any filesystem module; no wrapper
writes CSV output (Rule 7 — Pluggable Storage). Parameter values are
forwarded verbatim to the transport layer — rate limiting (Rule 2),
required headers (Rule 3), and retry-with-backoff are all enforced
inside :meth:`api.nba_client.NBAClient.get` and are transparent to
these wrappers.

Rule 6 (Fail-Safe Game Iteration) is NOT implemented at this layer.
These wrappers are called once per ``GAME_ID`` inside
:mod:`pipelines.ingest_games`, which owns the per-game ``try/except``
loop. Exceptions raised here MUST propagate naturally so the pipeline
can log a WARNING, increment ``games_failed_total``, and continue to
the next ``GAME_ID``.

Observability
-------------

Each wrapper emits a DEBUG log record with ``%s`` placeholders before
the :meth:`api.nba_client.NBAClient.get` call. Deferred ``%s``
formatting (not f-strings) ensures no string interpolation cost is
paid when the log level is above DEBUG — critical for the
``boxscoretraditionalv2``/``boxscoreadvancedv2``/``playbyplayv2``
wrappers which are invoked ~3,690 times during a full-season run
(~1,230 games × 3 per-game endpoints).

The DEBUG log records contain only parameter values that are safe at
DEBUG (``game_date``, ``game_id``, ``day_offset``) — never full
request bodies or response payloads.

Gate compliance
---------------

* Gate 2 — Clean lint / zero-warning compile: verified by
  ``python -m py_compile endpoints/games.py`` and
  ``python -m flake8 endpoints/games.py``.
* Gate 9 — Every registered pipeline is reachable from ``run.py``:
  these wrappers are invoked by :mod:`pipelines.ingest_games`.
* Gate 12 — Config propagation tracing: ``config.DEFAULT_LEAGUE_ID``
  is referenced by its literal dotted name in the default-argument
  binding of :func:`fetch_scoreboardv2`.
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
    """Fetch the scoreboard / standings envelope for a single calendar date.

    The ``scoreboardv2`` response is multi-table: it includes
    ``GameHeader``, ``LineScore``, ``SeriesStandings``, ``LastMeeting``,
    ``EastConfStandingsByDay``, ``WestConfStandingsByDay``,
    ``Available``, ``TeamLeaders``, ``TicketLinks``, and
    ``WinProbability``. Downstream normalization flattens the relevant
    tables into rows contributing to ``games.csv``.

    ``scoreboardv2`` is useful for date-partitioned game enumeration
    (e.g., "fetch today's games"). The primary season-wide enumeration
    path is :func:`endpoints.schedule.enumerate_game_ids` which uses
    ``leaguegamefinder`` and returns all ``GAME_ID`` values for a
    season in a single call; callers needing per-day granularity or
    incremental runs should prefer :func:`fetch_scoreboardv2`.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance —
            the sole HTTP transport (Rule 1).
        game_date: ISO-8601 date string (``YYYY-MM-DD``) — e.g.
            ``"2025-10-21"``. NBA Stats is strict about this format
            and will return an empty envelope for other shapes.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"`` — NBA). Other
            upstream-accepted values include ``"10"`` (WNBA) and
            ``"20"`` (G League), though these are outside the scope
            of the current pipeline.
        day_offset: Day offset string applied to ``game_date``.
            Defaults to ``"0"`` (exact date). The upstream accepts
            negative and positive integer strings (e.g. ``"-1"`` for
            the day before, ``"1"`` for the day after). The value is
            :func:`str`-cast so callers passing integers are
            accommodated.
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.
            Typical uses include ``SeasonType`` overrides (e.g.,
            ``SeasonType="Playoffs"``).

    Returns:
        Raw JSON envelope with the multi-table ``resultSets`` array
        (``GameHeader``, ``LineScore``, ``SeriesStandings``, etc.).
        Callers pass this dict directly to
        :func:`utils.schema_normalizer.normalize_result_sets` for
        flattening.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API
            after retry exhaustion.
        requests.exceptions.RequestException: Connection-level
            failure after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameDate": game_date,
        "LeagueID": league_id,
        "DayOffset": str(day_offset),
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.games.scoreboardv2 game_date=%s day_offset=%s",
        game_date,
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

    * ``RangeType=0`` with ``StartRange=0`` and ``EndRange=28800`` is
      the canonical "whole game" request (48 minutes × 60 seconds ×
      10 tenths-of-second = 28,800). This is the default.
    * Other values are reserved for partial-window requests (e.g.,
      "first five minutes of Q4"); the pipeline does not use them
      today and they are considered an advanced use case.

    Parameter shape note
    --------------------

    The parameter surface is IDENTICAL to
    :func:`fetch_boxscoreadvancedv2` — the NBA Stats API uses the same
    six-parameter contract for both boxscore variants. This
    consistency allows pipelines to iterate over a list of boxscore
    endpoint variants uniformly.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier, e.g.
            ``"0022500001"`` (the ``002`` prefix is Regular Season,
            ``25`` is the 2025-26 season year, ``00001`` is the
            sequence). :func:`str`-cast is applied so callers passing
            integer or numeric-string ``GAME_ID`` values from
            :func:`endpoints.schedule.enumerate_game_ids` are
            accommodated, though the canonical form is a string.
            Stripping leading zeros (via int conversion) corrupts the
            ID, so callers must preserve the string form upstream.
        start_period: Starting period as a string. Defaults to ``"0"``
            (all periods from game start — NBA Stats API convention
            where ``0`` means "no lower bound").
        end_period: Ending period as a string. Defaults to ``"10"``
            (covers regulation quarters 1-4 plus up to 6 overtime
            periods, 5-10 — NBA Stats API convention).
        start_range: Start of the time window in tenths of a second.
            Defaults to ``"0"``.
        end_range: End of the time window in tenths of a second.
            Defaults to ``"28800"`` (end of regulation: 48 min ×
            60 s × 10 tenths).
        range_type: Range-type selector. Defaults to ``"0"`` (whole
            game).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with ``PlayerStats`` and ``TeamStats``
        result-set tables populated with traditional box-score
        columns.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API
            after retry exhaustion.
        requests.exceptions.RequestException: Connection-level
            failure after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": str(game_id),
        "StartPeriod": str(start_period),
        "EndPeriod": str(end_period),
        "StartRange": str(start_range),
        "EndRange": str(end_range),
        "RangeType": str(range_type),
    }
    params.update(kwargs)
    logger.debug("endpoints.games.boxscoretraditionalv2 game_id=%s", game_id)
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

    Parameter shape note
    --------------------

    The parameter surface is IDENTICAL to
    :func:`fetch_boxscoretraditionalv2` — including the ``Range``
    triplet which encodes the requested time window. This consistency
    is intentional and allows pipelines to iterate over the two
    boxscore variants uniformly.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier, e.g.
            ``"0022500001"``. :func:`str`-cast is applied so callers
            passing integer or numeric-string IDs are accommodated.
        start_period: Starting period as a string. Defaults to ``"0"``
            (all periods from game start).
        end_period: Ending period as a string. Defaults to ``"10"``
            (covers regulation quarters plus up to 6 overtime
            periods).
        start_range: Start of the time window in tenths of a second.
            Defaults to ``"0"``.
        end_range: End of the time window in tenths of a second.
            Defaults to ``"28800"`` (end of regulation).
        range_type: Range-type selector. Defaults to ``"0"`` (whole
            game).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with ``PlayerStats`` and ``TeamStats``
        result-set tables populated with advanced metrics (offensive
        and defensive ratings, four-factors, pace, PIE, etc.).

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API
            after retry exhaustion.
        requests.exceptions.RequestException: Connection-level
            failure after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": str(game_id),
        "StartPeriod": str(start_period),
        "EndPeriod": str(end_period),
        "StartRange": str(start_range),
        "EndRange": str(end_range),
        "RangeType": str(range_type),
    }
    params.update(kwargs)
    logger.debug("endpoints.games.boxscoreadvancedv2 game_id=%s", game_id)
    return client.get("boxscoreadvancedv2", params)


def fetch_playbyplayv2(
    client: NBAClient,
    game_id: str,
    start_period: str = "0",
    end_period: str = "10",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the play-by-play event stream for a single ``GAME_ID``.

    The response envelope carries two result-set tables:

    * ``PlayByPlay`` — one row per event (shot, foul, turnover,
      timeout, substitution, etc.), keyed by ``(GAME_ID, EVENTNUM)``.
      A single game's response typically contains 400-500 events.
      Downstream normalization contributes this table to
      ``play_by_play.csv``.
    * ``AvailableVideo`` — auxiliary table describing video
      availability for the game. This table is informational and may
      be dropped by the pipeline.

    .. important::

       Unlike :func:`fetch_boxscoretraditionalv2` and
       :func:`fetch_boxscoreadvancedv2`, this wrapper has a NARROWER
       parameter surface — it accepts only ``GameID``,
       ``StartPeriod``, and ``EndPeriod``. It does NOT accept the
       ``Range`` triplet (``StartRange`` / ``EndRange`` /
       ``RangeType``); supplying those parameters is an upstream
       validation error. Callers should constrain the event window
       via the period bounds instead.

    Version note
    ------------

    This wrapper targets the v2 endpoint (``playbyplayv2``) only; the
    older ``playbyplay`` (v1) endpoint is deprecated and returns
    fewer per-event metadata fields. The v2 endpoint returns
    event-level coordinates (when available) and player marks that
    the older v1 endpoint omits.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        game_id: 10-character zero-padded game identifier, e.g.
            ``"0022500001"``. :func:`str`-cast is applied so callers
            passing integer or numeric-string IDs are accommodated.
        start_period: Starting period as a string. Defaults to ``"0"``
            (include all events from game start).
        end_period: Ending period as a string. Defaults to ``"10"``
            (include overtime events through up to 6 overtime
            periods).
        **kwargs: Additional upstream filters applied via
            ``params.update(kwargs)`` after the base dict is built.

    Returns:
        Raw JSON envelope with the ``PlayByPlay`` and
        ``AvailableVideo`` result-set tables.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API
            after retry exhaustion.
        requests.exceptions.RequestException: Connection-level
            failure after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "GameID": str(game_id),
        "StartPeriod": str(start_period),
        "EndPeriod": str(end_period),
    }
    params.update(kwargs)
    logger.debug("endpoints.games.playbyplayv2 game_id=%s", game_id)
    return client.get("playbyplayv2", params)
