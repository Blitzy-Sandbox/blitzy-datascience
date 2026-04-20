"""
Unit tests for ``endpoints.players`` — the thin wrapper layer that packages
the NBA Stats API's five Players endpoints (F-009) as single-purpose Python
callables.

Feature Coverage
================
Per AAP §0.5.1.4 and §0.7.2 (Rules 1, 3, 7), the module under test exposes
exactly five public wrappers, each forwarding a single ``NBAClient.get`` call
with a domain-specific parameter dictionary:

- ``fetch_leaguedashplayerstats`` — ``leaguedashplayerstats`` endpoint, 36 keys
- ``fetch_leaguedashplayerclutch`` — ``leaguedashplayerclutch`` endpoint,
  38 keys (OMITS ``TwoWay``; adds ``ClutchTime``, ``AheadBehind``, ``PointDiff``)
- ``fetch_playercareerstats`` — ``playercareerstats`` endpoint, strict 3 keys
  (``PlayerID`` str-cast, ``PerMode``, ``LeagueID``) — **no ``Season``**
- ``fetch_playergamelog`` — ``playergamelog`` endpoint, strict 6 keys
  (``PlayerID`` str-cast, ``Season``, ``SeasonType``, ``LeagueID``,
  ``DateFrom``, ``DateTo``)
- ``fetch_leaguedashptstats`` — ``leaguedashptstats`` endpoint, 29 keys
  (includes ``PtMeasureType``, ``PlayerOrTeam``; omits ``MeasureType``,
  ``PlusMinus``, ``PaceAdjust``, ``Rank``, ``Period``, ``PORound``,
  ``GameSegment``, ``ShotClockRange``, ``TwoWay``)

Coverage Matrix
---------------
For every wrapper each test class asserts:

1. **Endpoint name routing** — ``client.get`` is invoked with the exact NBA
   Stats endpoint string (no aliasing, no transformation).
2. **Param dict construction** — the param dict populates the NBA Stats API's
   expected ``PascalCase`` keys with the wrapper's resolved values.
3. **Default-argument propagation from config** — unspecified optional
   arguments fall through to :mod:`config` constants (``DEFAULT_SEASON_TYPE``,
   ``DEFAULT_LEAGUE_ID``), preserving the upstream authority defined in
   AAP §0.5.1.1.
4. **``**kwargs`` override** — any caller-supplied kwargs override the
   wrapper's literal defaults, allowing callers to specialize the param
   surface without touching the config layer.
5. **PlayerID type coercion** — the two wrappers that accept a ``player_id``
   argument (``fetch_playercareerstats``, ``fetch_playergamelog``) cast the
   value to :class:`str` so that integer inputs serialize correctly.
6. **Return-value passthrough** — whatever ``NBAClient.get`` returns is
   returned verbatim (object identity preserved), consistent with the
   wrapper's thin-wrapper obligation under Rule 1.
7. **Single call per invocation** — exactly one ``NBAClient.get`` call per
   wrapper invocation (no retries, no internal pagination).

Rule 1 Invariants
-----------------
These tests additionally verify the negative-space invariants enumerated in
AAP §0.7.2.1 (Rule 1 — Single HTTP Client):

- ``endpoints.players`` never imports ``requests``, ``urllib``, or ``httpx``.
- ``endpoints.players`` never imports ``pandas`` (flat-CSV assertion enforced
  downstream by :mod:`utils.schema_normalizer`).

Mocking Strategy
----------------
Tests use the :class:`tests.conftest.RecordingClient` spy (a handwritten
collaborator, *not* :class:`unittest.mock.MagicMock`) so that every
``(endpoint, params)`` tuple is captured and asserted on. The spy is
consistent with the conftest directive at §6.1 of the product spec: "tests
rely on explicit fixtures with deterministic state transitions, not
generic mocking libraries."

Test Organization
-----------------
One ``TestCase``-style class per wrapper plus two module-level classes:

- :class:`TestModuleInvariants` — cross-module assertions (Rule 1, logger
  name, public callable surface).
- :class:`TestParamDictShape` — parametric assertions that hold across every
  wrapper (single call, season propagation). Note that
  ``fetch_playercareerstats`` is excluded from the season parametric because
  its endpoint does **not** accept a ``Season`` parameter.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

import config
from endpoints import players


# ---------------------------------------------------------------------------
# fetch_leaguedashplayerstats — the flagship Players endpoint (36 keys)
# ---------------------------------------------------------------------------


class TestFetchLeaguedashplayerstats:
    """Covers :func:`endpoints.players.fetch_leaguedashplayerstats`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        """The wrapper MUST invoke ``client.get`` exactly once with the NBA
        Stats endpoint string ``"leaguedashplayerstats"`` (verbatim, no alias).
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(client=client, season="2025-26")

        assert len(client.calls) == 1
        assert client.calls[0][0] == "leaguedashplayerstats"

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        """When the caller omits ``season_type`` and ``league_id``, the
        wrapper MUST resolve them from :mod:`config`, preserving the authority
        of the project-wide defaults documented in AAP §0.5.1.1.
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(client=client, season="2025-26")

        params: Dict[str, Any] = client.calls[0][1]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_required_param_surface_is_populated(self, recording_client) -> None:
        """The 36-key param surface includes five caller-settable knobs
        (``Season``, ``PerMode``, ``MeasureType``) plus a fixed triplet of
        ``N``-flag filters (``PlusMinus``, ``PaceAdjust``, ``Rank``) and a
        suite of zero-string numeric filters.
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(
            client=client,
            season="2024-25",
            per_mode="Totals",
            measure_type="Advanced",
        )

        params = client.calls[0][1]
        assert params["Season"] == "2024-25"
        assert params["PerMode"] == "Totals"
        assert params["MeasureType"] == "Advanced"
        assert params["PlusMinus"] == "N"
        assert params["PaceAdjust"] == "N"
        assert params["Rank"] == "N"
        assert params["LastNGames"] == "0"
        assert params["Month"] == "0"
        assert params["OpponentTeamID"] == "0"
        assert params["Period"] == "0"
        assert params["PORound"] == "0"
        assert params["TeamID"] == "0"
        assert params["TwoWay"] == "0"

    def test_filter_strings_default_empty(self, recording_client) -> None:
        """Every string-valued filter defaults to an empty string so that the
        NBA Stats API interprets the field as "unset" rather than as a typed
        value that would constrain the response.
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(client=client, season="2025-26")

        params = client.calls[0][1]
        for key in (
            "DateFrom",
            "DateTo",
            "GameSegment",
            "Location",
            "Outcome",
            "SeasonSegment",
            "ShotClockRange",
            "VsConference",
            "VsDivision",
            "Conference",
            "Division",
            "College",
            "Country",
            "DraftPick",
            "DraftYear",
            "GameScope",
            "Height",
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
            "Weight",
        ):
            assert params[key] == "", f"{key!r} defaulted to {params[key]!r}"

    def test_kwargs_override_defaults(self, recording_client) -> None:
        """Caller-supplied ``**kwargs`` MUST win over the wrapper's literal
        defaults, enabling targeted scenarios without mutating :mod:`config`.
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(
            client=client,
            season="2025-26",
            Conference="East",
            LastNGames="10",
            PlusMinus="Y",
        )

        params = client.calls[0][1]
        assert params["Conference"] == "East"
        assert params["LastNGames"] == "10"
        assert params["PlusMinus"] == "Y"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        """Thin-wrapper contract: the dict returned by ``NBAClient.get`` MUST
        be returned by object identity, not copied, re-shaped, or filtered.
        """
        response: Dict[str, Any] = {
            "resource": "leaguedashplayerstats",
            "resultSets": [
                {
                    "name": "LeagueDashPlayerStats",
                    "headers": ["PLAYER_ID", "PLAYER_NAME", "PTS"],
                    "rowSet": [[2544, "LeBron James", 30.1]],
                }
            ],
        }
        client = recording_client(responses={"leaguedashplayerstats": response})

        result = players.fetch_leaguedashplayerstats(client=client, season="2025-26")

        assert result is response

    def test_explicit_season_type_and_league_id_win(self, recording_client) -> None:
        """When the caller explicitly supplies ``season_type`` or
        ``league_id``, the explicit values MUST populate the param dict
        (overriding the :mod:`config` defaults).
        """
        client = recording_client()

        players.fetch_leaguedashplayerstats(
            client=client,
            season="2025-26",
            season_type="Playoffs",
            league_id="00",
        )

        params = client.calls[0][1]
        assert params["SeasonType"] == "Playoffs"
        assert params["LeagueID"] == "00"


# ---------------------------------------------------------------------------
# fetch_leaguedashplayerclutch — clutch-time variant (38 keys, OMITS TwoWay)
# ---------------------------------------------------------------------------


class TestFetchLeaguedashplayerclutch:
    """Covers :func:`endpoints.players.fetch_leaguedashplayerclutch`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        """The wrapper MUST invoke ``client.get`` exactly once with the NBA
        Stats endpoint string ``"leaguedashplayerclutch"``.
        """
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        assert len(client.calls) == 1
        assert client.calls[0][0] == "leaguedashplayerclutch"

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_clutch_specific_defaults(self, recording_client) -> None:
        """The three clutch-specific defaults (``ClutchTime``,
        ``AheadBehind``, ``PointDiff``) MUST appear in the param dict with
        their documented default values.
        """
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["ClutchTime"] == "Last 5 Minutes"
        assert params["AheadBehind"] == "Ahead or Behind"
        assert params["PointDiff"] == "5"

    def test_point_diff_int_is_cast_to_string(self, recording_client) -> None:
        """When the caller supplies ``point_diff`` as an :class:`int`, the
        wrapper MUST coerce it to :class:`str` so the NBA Stats API receives a
        JSON-serializable string value.
        """
        client = recording_client()

        players.fetch_leaguedashplayerclutch(
            client=client, season="2025-26", point_diff=10
        )

        params = client.calls[0][1]
        assert params["PointDiff"] == "10"
        assert isinstance(params["PointDiff"], str)

    def test_twoway_key_is_omitted(self, recording_client) -> None:
        """Unlike ``fetch_leaguedashplayerstats``, the clutch variant MUST
        NOT include a ``TwoWay`` key in its param dict.

        This distinguishes the Players-layer clutch variant from the Lineups
        on/off variant (which DOES include ``TwoWay="0"``); see AAP §0.4.5.
        """
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        params = client.calls[0][1]
        assert "TwoWay" not in params

    def test_thirty_eight_key_param_surface(self, recording_client) -> None:
        """The clutch param dict defaults to exactly 38 keys — the
        ``fetch_leaguedashplayerstats`` surface minus ``TwoWay`` plus
        ``ClutchTime``, ``AheadBehind``, and ``PointDiff``.
        """
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        params = client.calls[0][1]
        assert len(params) == 38

    def test_kwargs_override_defaults(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashplayerclutch(
            client=client,
            season="2025-26",
            ClutchTime="Last 3 Minutes",
            AheadBehind="Ahead",
        )

        params = client.calls[0][1]
        assert params["ClutchTime"] == "Last 3 Minutes"
        assert params["AheadBehind"] == "Ahead"

    def test_filter_strings_default_empty(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashplayerclutch(client=client, season="2025-26")

        params = client.calls[0][1]
        for key in (
            "DateFrom",
            "DateTo",
            "GameSegment",
            "Location",
            "Outcome",
            "SeasonSegment",
            "ShotClockRange",
            "VsConference",
            "VsDivision",
            "Conference",
            "Division",
            "College",
            "Country",
            "DraftPick",
            "DraftYear",
            "GameScope",
            "Height",
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
            "Weight",
        ):
            assert params[key] == "", f"{key!r} defaulted to {params[key]!r}"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        response: Dict[str, Any] = {
            "resource": "leaguedashplayerclutch",
            "resultSets": [
                {
                    "name": "LeagueDashPlayerClutch",
                    "headers": ["PLAYER_ID", "PLAYER_NAME", "CLUTCH_PTS"],
                    "rowSet": [[201939, "Stephen Curry", 8.3]],
                }
            ],
        }
        client = recording_client(responses={"leaguedashplayerclutch": response})

        result = players.fetch_leaguedashplayerclutch(
            client=client, season="2025-26"
        )

        assert result is response


# ---------------------------------------------------------------------------
# fetch_playercareerstats — strict 3-key, NO Season parameter
# ---------------------------------------------------------------------------


class TestFetchPlayercareerstats:
    """Covers :func:`endpoints.players.fetch_playercareerstats`.

    **Unique contract:** this is the only Players wrapper whose param dict
    does NOT include a ``Season`` key. The NBA Stats API's
    ``playercareerstats`` endpoint returns a player's entire career
    aggregation, so scoping by season would defeat its purpose.
    """

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        assert len(client.calls) == 1
        assert client.calls[0][0] == "playercareerstats"

    def test_player_id_string_is_passed_through_verbatim(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        params = client.calls[0][1]
        assert params["PlayerID"] == "2544"
        assert isinstance(params["PlayerID"], str)

    def test_player_id_int_is_cast_to_string(self, recording_client) -> None:
        """Integer ``player_id`` MUST be coerced to :class:`str`."""
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id=2544)

        params = client.calls[0][1]
        assert params["PlayerID"] == "2544"
        assert isinstance(params["PlayerID"], str)

    def test_params_contains_only_three_keys(self, recording_client) -> None:
        """Strict param-dict shape: exactly ``{PlayerID, PerMode, LeagueID}``.
        No ``Season``, no ``SeasonType``, no filter scaffolding.
        """
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        params = client.calls[0][1]
        assert set(params.keys()) == {"PlayerID", "PerMode", "LeagueID"}

    def test_no_season_key_ever_appears(self, recording_client) -> None:
        """Season must never leak into the param dict — not even as an
        empty string — because ``playercareerstats`` is explicitly a
        career-aggregate endpoint (AAP §0.5.1.4).
        """
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        params = client.calls[0][1]
        assert "Season" not in params
        assert "SeasonType" not in params

    def test_default_per_mode_is_pergame(self, recording_client) -> None:
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        params = client.calls[0][1]
        assert params["PerMode"] == "PerGame"

    def test_default_league_id_propagates_from_config(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playercareerstats(client=client, player_id="2544")

        params = client.calls[0][1]
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_explicit_per_mode_and_league_id_win(self, recording_client) -> None:
        client = recording_client()

        players.fetch_playercareerstats(
            client=client,
            player_id="2544",
            per_mode="Totals",
            league_id="10",
        )

        params = client.calls[0][1]
        assert params["PerMode"] == "Totals"
        assert params["LeagueID"] == "10"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        response: Dict[str, Any] = {
            "resource": "playercareerstats",
            "resultSets": [
                {
                    "name": "SeasonTotalsRegularSeason",
                    "headers": ["PLAYER_ID", "SEASON_ID", "PTS"],
                    "rowSet": [[2544, "2003-04", 1654]],
                }
            ],
        }
        client = recording_client(responses={"playercareerstats": response})

        result = players.fetch_playercareerstats(client=client, player_id="2544")

        assert result is response


# ---------------------------------------------------------------------------
# fetch_playergamelog — strict 6-key (PlayerID, Season, SeasonType, LeagueID,
#                                     DateFrom, DateTo)
# ---------------------------------------------------------------------------


class TestFetchPlayergamelog:
    """Covers :func:`endpoints.players.fetch_playergamelog`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        assert len(client.calls) == 1
        assert client.calls[0][0] == "playergamelog"

    def test_player_id_string_is_passed_through_verbatim(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        params = client.calls[0][1]
        assert params["PlayerID"] == "2544"
        assert isinstance(params["PlayerID"], str)

    def test_player_id_int_is_cast_to_string(self, recording_client) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id=2544, season="2025-26"
        )

        params = client.calls[0][1]
        assert params["PlayerID"] == "2544"
        assert isinstance(params["PlayerID"], str)

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        params = client.calls[0][1]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_date_from_and_date_to_default_to_empty_string(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        params = client.calls[0][1]
        assert params["DateFrom"] == ""
        assert params["DateTo"] == ""

    def test_explicit_date_bounds_are_passed_through(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_playergamelog(
            client=client,
            player_id="2544",
            season="2025-26",
            date_from="10/22/2024",
            date_to="04/13/2025",
        )

        params = client.calls[0][1]
        assert params["DateFrom"] == "10/22/2024"
        assert params["DateTo"] == "04/13/2025"

    def test_params_contains_only_six_keys_by_default(
        self, recording_client
    ) -> None:
        """Strict param-dict shape: exactly six keys. No hidden filters."""
        client = recording_client()

        players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        params = client.calls[0][1]
        assert set(params.keys()) == {
            "PlayerID",
            "Season",
            "SeasonType",
            "LeagueID",
            "DateFrom",
            "DateTo",
        }

    def test_kwargs_override_defaults(self, recording_client) -> None:
        """Keyword-argument overrides (passed through ``**kwargs``) MUST
        take precedence over literal defaults of equivalent name.
        """
        client = recording_client()

        players.fetch_playergamelog(
            client=client,
            player_id="2544",
            season="2025-26",
            date_from="10/22/2024",
            DateFrom="01/01/2025",
        )

        params = client.calls[0][1]
        assert params["DateFrom"] == "01/01/2025"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        response: Dict[str, Any] = {
            "resource": "playergamelog",
            "resultSets": [
                {
                    "name": "PlayerGameLog",
                    "headers": ["GAME_ID", "PTS"],
                    "rowSet": [["0022500001", 30]],
                }
            ],
        }
        client = recording_client(responses={"playergamelog": response})

        result = players.fetch_playergamelog(
            client=client, player_id="2544", season="2025-26"
        )

        assert result is response


# ---------------------------------------------------------------------------
# fetch_leaguedashptstats — player-tracking stats (29 keys, PtMeasureType +
#                                                   PlayerOrTeam specific)
# ---------------------------------------------------------------------------


class TestFetchLeaguedashptstats:
    """Covers :func:`endpoints.players.fetch_leaguedashptstats`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        assert len(client.calls) == 1
        assert client.calls[0][0] == "leaguedashptstats"

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_default_pt_measure_type_is_speed_distance(
        self, recording_client
    ) -> None:
        """The NBA Stats player-tracking surface requires a
        ``PtMeasureType`` discriminator; the wrapper defaults to
        ``"SpeedDistance"`` to provide a harmless, broad baseline.
        """
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["PtMeasureType"] == "SpeedDistance"

    def test_default_player_or_team_is_player(self, recording_client) -> None:
        """Because this wrapper lives in the Players domain module (F-009),
        the default ``PlayerOrTeam`` discriminator is ``"Player"``. The
        Teams-domain consumer can opt into ``"Team"`` via the explicit
        keyword argument.
        """
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["PlayerOrTeam"] == "Player"

    def test_default_per_mode_is_pergame(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        assert params["PerMode"] == "PerGame"

    def test_omits_measure_surface_keys(self, recording_client) -> None:
        """The player-tracking endpoint does NOT accept the standard
        advanced-box-score knobs. The wrapper MUST omit them from the param
        dict so the upstream API does not reject the request.
        """
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        for forbidden in (
            "MeasureType",
            "PlusMinus",
            "PaceAdjust",
            "Rank",
            "Period",
            "PORound",
            "GameSegment",
            "ShotClockRange",
            "TwoWay",
        ):
            assert (
                forbidden not in params
            ), f"Forbidden key {forbidden!r} leaked into param dict"

    def test_numeric_default_filters_are_zero_strings(
        self, recording_client
    ) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        for key in ("LastNGames", "Month", "OpponentTeamID", "TeamID"):
            assert params[key] == "0", f"{key!r} defaulted to {params[key]!r}"

    def test_string_filters_default_empty(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        for key in (
            "DateFrom",
            "DateTo",
            "GameScope",
            "Location",
            "Outcome",
            "SeasonSegment",
            "VsConference",
            "VsDivision",
            "College",
            "Conference",
            "Country",
            "DraftPick",
            "DraftYear",
            "Division",
            "Height",
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
            "Weight",
        ):
            assert params[key] == "", f"{key!r} defaulted to {params[key]!r}"

    def test_explicit_pt_measure_type_wins(self, recording_client) -> None:
        """The caller may specialize the player-tracking surface via
        ``pt_measure_type``; the explicit value MUST replace the default.
        """
        client = recording_client()

        players.fetch_leaguedashptstats(
            client=client, season="2025-26", pt_measure_type="Drives"
        )

        params = client.calls[0][1]
        assert params["PtMeasureType"] == "Drives"

    def test_explicit_player_or_team_wins(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(
            client=client, season="2025-26", player_or_team="Team"
        )

        params = client.calls[0][1]
        assert params["PlayerOrTeam"] == "Team"

    def test_kwargs_override_defaults(self, recording_client) -> None:
        client = recording_client()

        players.fetch_leaguedashptstats(
            client=client,
            season="2025-26",
            Location="Home",
            Month="10",
        )

        params = client.calls[0][1]
        assert params["Location"] == "Home"
        assert params["Month"] == "10"

    def test_twenty_nine_key_param_surface(self, recording_client) -> None:
        """The player-tracking param dict resolves to exactly 29 keys."""
        client = recording_client()

        players.fetch_leaguedashptstats(client=client, season="2025-26")

        params = client.calls[0][1]
        assert len(params) == 29

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        response: Dict[str, Any] = {
            "resource": "leaguedashptstats",
            "resultSets": [
                {
                    "name": "LeagueDashPtStats",
                    "headers": ["PLAYER_ID", "DIST_MILES"],
                    "rowSet": [[2544, 2.45]],
                }
            ],
        }
        client = recording_client(responses={"leaguedashptstats": response})

        result = players.fetch_leaguedashptstats(client=client, season="2025-26")

        assert result is response


# ---------------------------------------------------------------------------
# Cross-module invariants (Rule 1, logger, public surface)
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Negative-space and structural invariants on :mod:`endpoints.players`."""

    def test_module_does_not_import_requests(self) -> None:
        """Rule 1 — Single HTTP Client. The endpoint wrapper module MUST
        NOT import the ``requests`` (or equivalent) HTTP library at module
        level; it must delegate all transport to :class:`NBAClient`.
        """
        assert not hasattr(players, "requests")
        assert not hasattr(players, "urllib")
        assert not hasattr(players, "httpx")

    def test_module_does_not_import_pandas(self) -> None:
        """Rule 1 side-effect: the wrapper layer is data-shape-agnostic.
        The transformation to DataFrames happens downstream in
        :mod:`utils.schema_normalizer` (Rule 4).
        """
        assert not hasattr(players, "pd")
        assert not hasattr(players, "pandas")

    def test_five_public_callables_exported(self) -> None:
        """F-009 requires exactly five public wrappers (AAP §0.5.1.4)."""
        for name in (
            "fetch_leaguedashplayerstats",
            "fetch_leaguedashplayerclutch",
            "fetch_playercareerstats",
            "fetch_playergamelog",
            "fetch_leaguedashptstats",
        ):
            attr = getattr(players, name, None)
            assert callable(attr), f"{name!r} is not a callable on endpoints.players"

    def test_module_uses_module_level_logger(self) -> None:
        """Observability rule: every module emits structured logs through a
        module-level logger (not ``print``, not per-function loggers).
        """
        assert hasattr(players, "logger")

    def test_logger_name_matches_module_path(self) -> None:
        """The module's logger name MUST match its dotted import path, so
        hierarchical logger filtering works at the package level.
        """
        adapter = players.logger
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "endpoints.players"


# ---------------------------------------------------------------------------
# Parametric cross-wrapper assertions
# ---------------------------------------------------------------------------


class TestParamDictShape:
    """Assertions that hold across every wrapper in :mod:`endpoints.players`.

    Note that :func:`fetch_playercareerstats` is intentionally excluded from
    the season-verbatim parametric because that endpoint does NOT accept a
    ``Season`` parameter (see :class:`TestFetchPlayercareerstats`).
    """

    @pytest.mark.parametrize(
        ("func", "extra_kwargs", "expected_endpoint"),
        [
            (
                players.fetch_leaguedashplayerstats,
                {},
                "leaguedashplayerstats",
            ),
            (
                players.fetch_leaguedashplayerclutch,
                {},
                "leaguedashplayerclutch",
            ),
            (
                players.fetch_playercareerstats,
                {"player_id": "2544"},
                "playercareerstats",
            ),
            (
                players.fetch_playergamelog,
                {"player_id": "2544"},
                "playergamelog",
            ),
            (
                players.fetch_leaguedashptstats,
                {},
                "leaguedashptstats",
            ),
        ],
    )
    def test_every_wrapper_issues_exactly_one_call(
        self,
        recording_client,
        func,
        extra_kwargs,
        expected_endpoint,
    ) -> None:
        client = recording_client()

        # ``fetch_playercareerstats`` does not accept ``season``; for all
        # other wrappers, pass a fixed season to satisfy the required
        # positional/keyword contract.
        if func is players.fetch_playercareerstats:
            func(client=client, **extra_kwargs)
        else:
            func(client=client, season="2025-26", **extra_kwargs)

        assert len(client.calls) == 1
        assert client.calls[0][0] == expected_endpoint

    @pytest.mark.parametrize(
        ("func", "extra_kwargs"),
        [
            (players.fetch_leaguedashplayerstats, {}),
            (players.fetch_leaguedashplayerclutch, {}),
            # fetch_playercareerstats is EXCLUDED — no Season parameter.
            (players.fetch_playergamelog, {"player_id": "2544"}),
            (players.fetch_leaguedashptstats, {}),
        ],
    )
    def test_every_wrapper_populates_season_verbatim(
        self,
        recording_client,
        func,
        extra_kwargs,
    ) -> None:
        """Every Season-accepting wrapper MUST propagate the caller's
        ``season`` argument verbatim into the ``Season`` key of the param
        dict. No transformation, no default substitution.
        """
        client = recording_client()

        func(client=client, season="2024-25", **extra_kwargs)

        params = client.calls[0][1]
        assert params["Season"] == "2024-25"
