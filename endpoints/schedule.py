"""Schedule domain endpoint wrapper (Feature F-013).

This module wraps the single NBA Stats endpoint that backs the Schedule
data domain (``leaguegamefinder``) plus a derived helper that enumerates
the unique ``GAME_ID`` values for a season. The helper is the SOLE
cross-domain dependency expressed in this codebase: the Games pipeline
(:mod:`pipelines.ingest_games`) calls :func:`enumerate_game_ids` at the
top of its ``run()`` to determine which games to iterate.

Endpoint
--------

``leaguegamefinder`` is a league-wide game finder that returns one row
per (team, game) pair for the configured ``Season``, ``SeasonType``,
``LeagueID``, and ``PlayerOrTeam`` filter. With ``PlayerOrTeam="T"``
(the pipeline convention) the endpoint returns roughly 2 * number-of-games
rows — one per team per game. The
:func:`enumerate_game_ids` helper dedupes to the canonical one-row-per-game
``GAME_ID`` list (roughly 1,230 for a full regular season) while
preserving first-seen ordering for deterministic iteration.

Rule compliance
---------------

This module contains no direct HTTP transport (Rule 1 — all traffic
routes through :class:`api.nba_client.NBAClient`) and no CSV emission
(Rule 7 — only :class:`storage.csv_writer.CSVWriter` calls
``DataFrame.to_csv``). It does NOT import ``requests``, ``pandas``,
``json``, or filesystem I/O modules. The :func:`enumerate_game_ids`
helper walks the raw JSON envelope structure with pure-Python list and
dict primitives — pandas/normalizer composition is deferred to the
Schedule pipeline.

Cross-domain dependency
-----------------------

Per :doc:`docs/api/endpoints_catalog.md` §9 and the Agent Action Plan,
:mod:`pipelines.ingest_games` depends on :func:`enumerate_game_ids` —
not on ``output/schedule.csv``. This keeps standalone
``python run.py games --season <season>`` functional even when no
schedule pipeline has run previously: the Games pipeline re-enumerates
``GAME_ID`` values on demand against the live API.

Observability
-------------

A DEBUG-level log record is emitted before each delegation to
``client.get`` and after :func:`enumerate_game_ids` completes
deduplication (including the raw and unique counts), using ``%s``
placeholders so the stdlib formatter only renders parameter values
when DEBUG logging is actually enabled. Request bodies and response
payloads are never logged from this module.
"""

from typing import Any, Dict, List

from api.nba_client import NBAClient
from utils.logger import get_logger

import config

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Public constants
# ----------------------------------------------------------------------

#: The upstream result-set table name returned by ``leaguegamefinder``.
#: Used by :func:`enumerate_game_ids` to locate the table inside the
#: ``resultSets`` array of the raw JSON envelope.
_RESULT_SET_NAME: str = "LeagueGameFinderResults"

#: The column whose values are extracted and deduplicated to produce the
#: canonical ``GAME_ID`` list for the Games pipeline.
_GAME_ID_COLUMN: str = "GAME_ID"


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def fetch_leaguegamefinder(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    player_or_team: str = "T",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Fetch the league-wide game finder result set for a season.

    The pipeline invokes ``leaguegamefinder`` with ``PlayerOrTeam="T"``
    (team-level rows — one row per team per game) by convention.
    ``PlayerOrTeam="P"`` is available for future per-player enumeration
    but is not used by the Schedule or Games pipelines today.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance. This
            is the sole HTTP transport in the pipeline (Rule 1); the
            wrapper delegates to ``client.get(endpoint, params)``.
        season: Season string in NBA format, e.g. ``"2025-26"``. See
            :data:`config.DEFAULT_SEASON` for the configured default
            season.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
            Other accepted values include ``"Playoffs"``, ``"Pre Season"``,
            and ``"All Star"``.
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``, the NBA itself).
            ``"10"`` (WNBA) and ``"20"`` (G-League) are also accepted by
            the upstream but are out of scope for this pipeline.
        player_or_team: ``"T"`` (default) for team-level rows or ``"P"``
            for player-level rows. The Schedule and Games pipelines rely
            on the ``"T"`` shape; changing this will change the row
            multiplicity and invalidate :func:`enumerate_game_ids` in
            its current form.
        **kwargs: Additional NBA Stats filters that override the defaults
            below. Applied via ``params.update(kwargs)`` after the base
            dict is built. Recognized filters include ``DateFrom``,
            ``DateTo``, ``TeamID``, ``Outcome``, ``Location``,
            ``VsConference``, ``VsDivision``, ``Conference``,
            ``Division``, ``SeasonSegment``, and ``GameID``.

    Returns:
        Raw JSON envelope returned by the upstream endpoint. The response
        carries a ``resultSets`` array whose ``LeagueGameFinderResults``
        table is keyed by ``(GAME_ID, TEAM_ID)`` — one row per team per
        game. Downstream normalization produces ``schedule.csv``.

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
    """
    params: Dict[str, Any] = {
        "Season": season,
        "SeasonType": season_type,
        "LeagueID": league_id,
        "PlayerOrTeam": player_or_team,
        "PlayerID": "",
        "TeamID": "",
        "Outcome": "",
        "Location": "",
        "VsConference": "",
        "VsDivision": "",
        "Conference": "",
        "Division": "",
        "SeasonSegment": "",
        "GameID": "",
        "DateFrom": "",
        "DateTo": "",
    }
    params.update(kwargs)
    logger.debug(
        "endpoints.schedule.leaguegamefinder season=%s season_type=%s player_or_team=%s",
        season,
        season_type,
        player_or_team,
    )
    return client.get("leaguegamefinder", params)


def enumerate_game_ids(
    client: NBAClient,
    season: str,
    season_type: str = config.DEFAULT_SEASON_TYPE,
    league_id: str = config.DEFAULT_LEAGUE_ID,
    **kwargs: Any,
) -> List[str]:
    """Return the deduplicated, first-seen-ordered list of ``GAME_ID`` values.

    This is the canonical ``GAME_ID`` enumerator used by
    :mod:`pipelines.ingest_games`. It issues a single ``leaguegamefinder``
    call with ``PlayerOrTeam="T"`` (which returns two rows per game,
    one for each team), extracts the ``GAME_ID`` column from the
    ``LeagueGameFinderResults`` result-set, and deduplicates while
    preserving the first-seen order so downstream iteration is
    deterministic (important for Gate 8 resume determinism).

    The deduplication uses :func:`dict.fromkeys` which has preserved
    insertion order since CPython 3.7 — this is portable across the
    supported 3.11 and 3.12 runtimes.

    Args:
        client: Shared :class:`api.nba_client.NBAClient` instance.
        season: Season string in NBA format, e.g. ``"2025-26"``.
        season_type: Season type filter. Defaults to
            :data:`config.DEFAULT_SEASON_TYPE` (``"Regular Season"``).
        league_id: NBA League ID. Defaults to
            :data:`config.DEFAULT_LEAGUE_ID` (``"00"``).
        **kwargs: Additional NBA Stats filters passed through to
            :func:`fetch_leaguegamefinder` and applied as
            ``params.update(kwargs)`` on the upstream dict. Note that
            ``PlayerOrTeam`` is intentionally NOT exposed here because
            the dedup logic assumes team-level rows.

    Returns:
        List of ``GAME_ID`` strings (typically 10-character zero-padded
        identifiers such as ``"0022500001"``) with duplicates removed,
        preserving the order in which each ``GAME_ID`` first appears in
        the raw ``rowSet``. An empty list is returned if the upstream
        returns an empty ``rowSet`` (e.g., for an invalid season string
        or before any games have been played in the configured season).

    Raises:
        requests.exceptions.HTTPError: Non-transient 4xx from the API.
        requests.exceptions.RequestException: Connection-level failure
            after tenacity retry exhaustion.
        KeyError: If the ``leaguegamefinder`` response envelope does not
            contain the ``LeagueGameFinderResults`` table or its
            ``headers`` array does not include ``GAME_ID``. This signals
            an upstream schema change that warrants investigation rather
            than silent recovery.
    """
    payload = fetch_leaguegamefinder(
        client,
        season,
        season_type=season_type,
        league_id=league_id,
        player_or_team="T",
        **kwargs,
    )

    # Walk the raw envelope structure to find the LeagueGameFinderResults
    # table. We intentionally avoid pulling in utils.schema_normalizer
    # here — normalizer composition belongs to the Schedule pipeline,
    # and this helper is invoked from the Games pipeline directly during
    # run() enumeration (see docs/api/endpoints_catalog.md §9).
    result_sets = payload.get("resultSets", [])
    if not isinstance(result_sets, list):
        raise KeyError(
            "leaguegamefinder response missing 'resultSets' list; "
            "upstream schema change suspected"
        )

    target_table: Dict[str, Any] = {}
    for entry in result_sets:
        if isinstance(entry, dict) and entry.get("name") == _RESULT_SET_NAME:
            target_table = entry
            break
    if not target_table:
        raise KeyError(
            "leaguegamefinder response does not contain the "
            f"{_RESULT_SET_NAME!r} table; upstream schema change suspected"
        )

    headers = target_table.get("headers") or []
    row_set = target_table.get("rowSet") or []
    if _GAME_ID_COLUMN not in headers:
        raise KeyError(
            f"{_RESULT_SET_NAME!r} table does not expose the "
            f"{_GAME_ID_COLUMN!r} column; upstream schema change suspected"
        )
    game_id_index = headers.index(_GAME_ID_COLUMN)

    # Order-preserving deduplication. Every GAME_ID is coerced via str()
    # because the upstream occasionally returns integers for IDs that
    # look numeric — the Games pipeline expects string IDs for URL
    # construction on downstream box-score calls.
    raw_ids = [str(row[game_id_index]) for row in row_set if row]
    unique_ids = list(dict.fromkeys(raw_ids))

    logger.debug(
        "endpoints.schedule.enumerate_game_ids season=%s raw_rows=%s unique_game_ids=%s",
        season,
        len(raw_ids),
        len(unique_ids),
    )
    return unique_ids
