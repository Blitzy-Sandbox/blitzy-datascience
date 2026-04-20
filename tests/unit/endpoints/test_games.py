"""
Unit tests for the ``endpoints/games.py`` thin wrapper module (Feature F-011).

=============================================================================
Coverage matrix
=============================================================================

Each wrapper under test is verified across the following seven behavioral
dimensions that collectively exercise the entire observable contract of a
thin endpoint wrapper (per AAP §0.4.1.1 and §0.5.1.4):

1. **Endpoint-name routing** — the wrapper calls
   ``NBAClient.get(endpoint, params)`` with the exact lowercase endpoint
   string documented in ``docs/api/endpoints_catalog.md`` and the AAP
   feature inventory (``scoreboardv2``, ``boxscoretraditionalv2``,
   ``boxscoreadvancedv2``, ``playbyplayv2``).

2. **Required-parameter construction** — when only the documented
   required positional argument (``game_date`` for scoreboard;
   ``game_id`` for the other three wrappers) is supplied, the emitted
   params dict is populated with every pinned key required by the
   upstream NBA Stats API with the exact default values sourced from
   ``config.py`` or the wrapper's signature defaults.

3. **Type coercion** — the wrapper applies ``str(...)`` to identifier
   parameters (``game_id`` on box-score and play-by-play wrappers,
   ``day_offset`` on scoreboard) and to numeric period/range parameters
   (``start_period``, ``end_period``, ``start_range``, ``end_range``,
   ``range_type``) so that the upstream API consistently receives
   strings regardless of whether callers supply ``int`` or ``str``.

4. **Config-defaulted fields propagate** — any parameter whose default
   reads from ``config`` (e.g. ``LeagueID`` in ``fetch_scoreboardv2``)
   propagates its configured value into the emitted params dict.

5. **``**kwargs`` override semantics** — callers can override or extend
   the params dict by passing additional keyword arguments; the last
   value wins because the wrapper performs ``params.update(kwargs)``
   after constructing its default dict.

6. **Return-value passthrough** — the wrapper returns exactly the value
   returned by ``NBAClient.get`` without any transformation, wrapping,
   or additional logic.

7. **Single call per invocation** — each wrapper invocation issues
   exactly one call to ``client.get`` (no retries, no internal loops,
   no cached requests).

Rule 1 / Rule 7 invariants
--------------------------

The ``TestModuleInvariants`` class additionally verifies that
``endpoints/games.py`` does not directly import or expose the
``requests`` library (AAP Rule 1 — Single HTTP Client) and does not
import ``pandas`` (AAP Rule 7 — Pluggable Storage): all four wrappers
are thin dict-builders that delegate to ``NBAClient.get`` and never
touch the transport layer directly.

=============================================================================
Mocking strategy
=============================================================================

All tests use the handwritten :class:`conftest.RecordingClient` spy
exposed by the ``recording_client`` factory fixture declared in
``tests/conftest.py``. The wrappers themselves are pure Python and do
not require network mocking (they never touch ``requests``). The spy
captures every ``(endpoint, params)`` pair and allows tests to assert
on the exact call arguments that would be forwarded to the live HTTP
layer in production.

Test organization
-----------------

One ``TestCase`` class per public wrapper, plus a
``TestModuleInvariants`` class for module-level properties, plus a
``TestParamDictShape`` parametric class that asserts invariants common
to every wrapper (exactly one call per invocation; required-parameter
passthrough).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

import config
from endpoints import games


# ---------------------------------------------------------------------------
# TestFetchScoreboardv2 — scoreboardv2 (3-key param surface)
# ---------------------------------------------------------------------------


class TestFetchScoreboardv2:
    """Contract tests for :func:`endpoints.games.fetch_scoreboardv2`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ):
        """The wrapper must route to exactly endpoint ``scoreboardv2``."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        assert len(client.calls) == 1
        endpoint, _ = client.calls[0]
        assert endpoint == "scoreboardv2"

    def test_game_date_is_passed_through_verbatim(self, recording_client):
        """``game_date`` is NOT str-cast; it is forwarded as-is."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        _, params = client.calls[0]
        assert params["GameDate"] == "10/22/2024"
        assert isinstance(params["GameDate"], str)

    def test_league_id_defaults_to_config_value(self, recording_client):
        """``LeagueID`` default propagates from ``config.DEFAULT_LEAGUE_ID``."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        _, params = client.calls[0]
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_day_offset_defaults_to_zero_string(self, recording_client):
        """``DayOffset`` default is the literal string ``'0'``."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        _, params = client.calls[0]
        assert params["DayOffset"] == "0"
        assert isinstance(params["DayOffset"], str)

    def test_day_offset_int_is_cast_to_string(self, recording_client):
        """Integer ``day_offset`` is coerced to ``str`` in the params dict."""
        client = recording_client()
        games.fetch_scoreboardv2(
            client=client, game_date="10/22/2024", day_offset=-1
        )
        _, params = client.calls[0]
        assert params["DayOffset"] == "-1"
        assert isinstance(params["DayOffset"], str)

    def test_day_offset_string_is_passed_through_verbatim(
        self, recording_client
    ):
        """String ``day_offset`` survives the str-cast unchanged."""
        client = recording_client()
        games.fetch_scoreboardv2(
            client=client, game_date="10/22/2024", day_offset="2"
        )
        _, params = client.calls[0]
        assert params["DayOffset"] == "2"

    def test_params_contains_only_three_keys_by_default(self, recording_client):
        """Default invocation produces exactly the documented 3-key surface."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        _, params = client.calls[0]
        assert set(params.keys()) == {"GameDate", "LeagueID", "DayOffset"}

    def test_params_excludes_season_and_season_type(self, recording_client):
        """Games wrappers do not include ``Season`` or ``SeasonType``."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        _, params = client.calls[0]
        assert "Season" not in params
        assert "SeasonType" not in params

    def test_explicit_league_id_overrides_default(self, recording_client):
        """Explicit ``league_id=`` kwarg is honored over the config default."""
        client = recording_client()
        games.fetch_scoreboardv2(
            client=client, game_date="10/22/2024", league_id="20"
        )
        _, params = client.calls[0]
        assert params["LeagueID"] == "20"

    def test_kwargs_override_defaults(self, recording_client):
        """Upper-case kwargs forwarded via ``**kwargs`` win over defaults."""
        client = recording_client()
        games.fetch_scoreboardv2(
            client=client,
            game_date="10/22/2024",
            GameDate="11/01/2024",
            DayOffset="5",
            LeagueID="20",
        )
        _, params = client.calls[0]
        # The explicit keyword GameDate goes into the params dict directly,
        # but ``params.update(kwargs)`` after construction means the
        # kwargs-supplied value wins.
        assert params["GameDate"] == "11/01/2024"
        assert params["DayOffset"] == "5"
        assert params["LeagueID"] == "20"

    def test_return_value_is_raw_client_response(self, recording_client):
        """The wrapper returns exactly the client's response, identity-preserved."""
        response: Dict[str, Any] = {"resultSets": [{"name": "GameHeader"}]}
        client = recording_client(responses={"scoreboardv2": response})
        result = games.fetch_scoreboardv2(
            client=client, game_date="10/22/2024"
        )
        assert result is response

    def test_single_call_per_invocation(self, recording_client):
        """Exactly one call to ``client.get`` per wrapper invocation."""
        client = recording_client()
        games.fetch_scoreboardv2(client=client, game_date="10/22/2024")
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# TestFetchBoxscoretraditionalv2 — boxscoretraditionalv2 (6-key param surface)
# ---------------------------------------------------------------------------


class TestFetchBoxscoretraditionalv2:
    """Contract tests for :func:`endpoints.games.fetch_boxscoretraditionalv2`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ):
        """The wrapper must route to exactly endpoint ``boxscoretraditionalv2``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        assert len(client.calls) == 1
        endpoint, _ = client.calls[0]
        assert endpoint == "boxscoretraditionalv2"

    def test_game_id_string_is_passed_through_verbatim(self, recording_client):
        """String ``game_id`` survives the str-cast unchanged."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["GameID"] == "0022500001"
        assert isinstance(params["GameID"], str)

    def test_game_id_int_is_cast_to_string(self, recording_client):
        """Integer ``game_id`` is coerced to ``str`` — no zero-padding applied."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(client=client, game_id=22500001)
        _, params = client.calls[0]
        assert params["GameID"] == "22500001"
        assert isinstance(params["GameID"], str)

    def test_start_period_defaults_to_zero_string(self, recording_client):
        """``StartPeriod`` default is the literal string ``'0'``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "0"
        assert isinstance(params["StartPeriod"], str)

    def test_end_period_defaults_to_ten_string(self, recording_client):
        """``EndPeriod`` default is the literal string ``'10'``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["EndPeriod"] == "10"
        assert isinstance(params["EndPeriod"], str)

    def test_start_range_defaults_to_zero_string(self, recording_client):
        """``StartRange`` default is the literal string ``'0'``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["StartRange"] == "0"
        assert isinstance(params["StartRange"], str)

    def test_end_range_defaults_to_28800_string(self, recording_client):
        """``EndRange`` default is the literal string ``'28800'`` (full game)."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["EndRange"] == "28800"
        assert isinstance(params["EndRange"], str)

    def test_range_type_defaults_to_zero_string(self, recording_client):
        """``RangeType`` default is the literal string ``'0'``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert params["RangeType"] == "0"
        assert isinstance(params["RangeType"], str)

    def test_numeric_periods_and_ranges_are_cast_to_strings(
        self, recording_client
    ):
        """All period/range integer inputs are coerced to strings."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client,
            game_id="0022500001",
            start_period=1,
            end_period=4,
            start_range=0,
            end_range=14400,
            range_type=1,
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "1"
        assert params["EndPeriod"] == "4"
        assert params["StartRange"] == "0"
        assert params["EndRange"] == "14400"
        assert params["RangeType"] == "1"
        for key in (
            "StartPeriod",
            "EndPeriod",
            "StartRange",
            "EndRange",
            "RangeType",
        ):
            assert isinstance(params[key], str)

    def test_params_contains_only_six_keys_by_default(self, recording_client):
        """Default invocation produces exactly the documented 6-key surface."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert set(params.keys()) == {
            "GameID",
            "StartPeriod",
            "EndPeriod",
            "StartRange",
            "EndRange",
            "RangeType",
        }

    def test_params_excludes_season_and_season_type(self, recording_client):
        """Games wrappers do not include ``Season`` or ``SeasonType``."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        _, params = client.calls[0]
        assert "Season" not in params
        assert "SeasonType" not in params

    def test_kwargs_override_defaults(self, recording_client):
        """Upper-case kwargs supplied via ``**kwargs`` win over defaults."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client,
            game_id="0022500001",
            StartPeriod="2",
            EndPeriod="3",
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "2"
        assert params["EndPeriod"] == "3"

    def test_return_value_is_raw_client_response(self, recording_client):
        """The wrapper returns exactly the client's response, identity-preserved."""
        response: Dict[str, Any] = {
            "resultSets": [{"name": "PlayerStats"}]
        }
        client = recording_client(
            responses={"boxscoretraditionalv2": response}
        )
        result = games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        assert result is response

    def test_single_call_per_invocation(self, recording_client):
        """Exactly one call to ``client.get`` per wrapper invocation."""
        client = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client, game_id="0022500001"
        )
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# TestFetchBoxscoreadvancedv2 — boxscoreadvancedv2 (identical 6-key surface)
# ---------------------------------------------------------------------------


class TestFetchBoxscoreadvancedv2:
    """Contract tests for :func:`endpoints.games.fetch_boxscoreadvancedv2`.

    The ``boxscoreadvancedv2`` wrapper has an identical parameter surface
    to ``boxscoretraditionalv2`` — the only observable difference at the
    wrapper boundary is the endpoint name. Both wrappers accept the same
    set of six parameters with identical defaults, identical str-cast
    behavior, and identical kwargs-override semantics. These tests
    duplicate the structural assertions to provide regression coverage
    for each wrapper independently.
    """

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ):
        """The wrapper must route to exactly endpoint ``boxscoreadvancedv2``."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        assert len(client.calls) == 1
        endpoint, _ = client.calls[0]
        assert endpoint == "boxscoreadvancedv2"

    def test_game_id_string_is_passed_through_verbatim(self, recording_client):
        """String ``game_id`` survives the str-cast unchanged."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert params["GameID"] == "0022500001"
        assert isinstance(params["GameID"], str)

    def test_game_id_int_is_cast_to_string(self, recording_client):
        """Integer ``game_id`` is coerced to ``str``."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id=22500002)
        _, params = client.calls[0]
        assert params["GameID"] == "22500002"
        assert isinstance(params["GameID"], str)

    def test_default_periods_and_ranges_match_documented_values(
        self, recording_client
    ):
        """All five period/range defaults match the documented literal strings."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert params["StartPeriod"] == "0"
        assert params["EndPeriod"] == "10"
        assert params["StartRange"] == "0"
        assert params["EndRange"] == "28800"
        assert params["RangeType"] == "0"

    def test_numeric_periods_and_ranges_are_cast_to_strings(
        self, recording_client
    ):
        """All period/range integer inputs are coerced to strings."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(
            client=client,
            game_id="0022500001",
            start_period=2,
            end_period=3,
            start_range=14400,
            end_range=28800,
            range_type=2,
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "2"
        assert params["EndPeriod"] == "3"
        assert params["StartRange"] == "14400"
        assert params["EndRange"] == "28800"
        assert params["RangeType"] == "2"
        for key in (
            "StartPeriod",
            "EndPeriod",
            "StartRange",
            "EndRange",
            "RangeType",
        ):
            assert isinstance(params[key], str)

    def test_params_contains_only_six_keys_by_default(self, recording_client):
        """Default invocation produces exactly the documented 6-key surface
        (identical to ``boxscoretraditionalv2``)."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert set(params.keys()) == {
            "GameID",
            "StartPeriod",
            "EndPeriod",
            "StartRange",
            "EndRange",
            "RangeType",
        }

    def test_params_excludes_season_and_season_type(self, recording_client):
        """Games wrappers do not include ``Season`` or ``SeasonType``."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert "Season" not in params
        assert "SeasonType" not in params

    def test_keyset_identical_to_boxscoretraditionalv2(self, recording_client):
        """Cross-wrapper invariant: ``boxscoreadvancedv2`` and
        ``boxscoretraditionalv2`` emit identical parameter keysets."""
        client_trad = recording_client()
        client_adv = recording_client()
        games.fetch_boxscoretraditionalv2(
            client=client_trad, game_id="0022500001"
        )
        games.fetch_boxscoreadvancedv2(
            client=client_adv, game_id="0022500001"
        )
        _, trad_params = client_trad.calls[0]
        _, adv_params = client_adv.calls[0]
        assert set(trad_params.keys()) == set(adv_params.keys())

    def test_kwargs_override_defaults(self, recording_client):
        """Upper-case kwargs supplied via ``**kwargs`` win over defaults."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(
            client=client,
            game_id="0022500001",
            RangeType="2",
            EndRange="14400",
        )
        _, params = client.calls[0]
        assert params["RangeType"] == "2"
        assert params["EndRange"] == "14400"

    def test_return_value_is_raw_client_response(self, recording_client):
        """The wrapper returns exactly the client's response, identity-preserved."""
        response: Dict[str, Any] = {
            "resultSets": [{"name": "PlayerStats"}]
        }
        client = recording_client(responses={"boxscoreadvancedv2": response})
        result = games.fetch_boxscoreadvancedv2(
            client=client, game_id="0022500001"
        )
        assert result is response

    def test_single_call_per_invocation(self, recording_client):
        """Exactly one call to ``client.get`` per wrapper invocation."""
        client = recording_client()
        games.fetch_boxscoreadvancedv2(client=client, game_id="0022500001")
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# TestFetchPlaybyplayv2 — playbyplayv2 (NARROWER 3-key param surface)
# ---------------------------------------------------------------------------


class TestFetchPlaybyplayv2:
    """Contract tests for :func:`endpoints.games.fetch_playbyplayv2`.

    Unlike the box-score wrappers, ``fetch_playbyplayv2`` has a NARROWER
    3-key param surface: ``GameID``, ``StartPeriod``, ``EndPeriod``.
    It does NOT accept or emit the range triplet
    (``StartRange``/``EndRange``/``RangeType``). These tests explicitly
    verify the absence of the range keys to prevent accidental drift
    toward the box-score surface in future refactors.
    """

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ):
        """The wrapper must route to exactly endpoint ``playbyplayv2``."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        assert len(client.calls) == 1
        endpoint, _ = client.calls[0]
        assert endpoint == "playbyplayv2"

    def test_game_id_string_is_passed_through_verbatim(self, recording_client):
        """String ``game_id`` survives the str-cast unchanged."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert params["GameID"] == "0022500001"
        assert isinstance(params["GameID"], str)

    def test_game_id_int_is_cast_to_string(self, recording_client):
        """Integer ``game_id`` is coerced to ``str``."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id=22500003)
        _, params = client.calls[0]
        assert params["GameID"] == "22500003"
        assert isinstance(params["GameID"], str)

    def test_start_period_defaults_to_zero_string(self, recording_client):
        """``StartPeriod`` default is the literal string ``'0'``."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert params["StartPeriod"] == "0"
        assert isinstance(params["StartPeriod"], str)

    def test_end_period_defaults_to_ten_string(self, recording_client):
        """``EndPeriod`` default is the literal string ``'10'``."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert params["EndPeriod"] == "10"
        assert isinstance(params["EndPeriod"], str)

    def test_numeric_periods_are_cast_to_strings(self, recording_client):
        """Integer period inputs are coerced to strings."""
        client = recording_client()
        games.fetch_playbyplayv2(
            client=client,
            game_id="0022500001",
            start_period=1,
            end_period=4,
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "1"
        assert params["EndPeriod"] == "4"
        assert isinstance(params["StartPeriod"], str)
        assert isinstance(params["EndPeriod"], str)

    def test_params_contains_only_three_keys_by_default(self, recording_client):
        """Default invocation produces exactly the documented 3-key surface."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert set(params.keys()) == {"GameID", "StartPeriod", "EndPeriod"}

    def test_params_explicitly_excludes_range_triplet(self, recording_client):
        """CRITICAL: ``fetch_playbyplayv2`` must NOT emit the range triplet.

        This is the key differentiator from the box-score wrappers and
        mirrors the narrower upstream NBA Stats API contract for
        ``playbyplayv2``.
        """
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert "StartRange" not in params
        assert "EndRange" not in params
        assert "RangeType" not in params

    def test_params_excludes_season_and_season_type(self, recording_client):
        """Games wrappers do not include ``Season`` or ``SeasonType``."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        _, params = client.calls[0]
        assert "Season" not in params
        assert "SeasonType" not in params

    def test_kwargs_override_defaults(self, recording_client):
        """Upper-case kwargs supplied via ``**kwargs`` win over defaults."""
        client = recording_client()
        games.fetch_playbyplayv2(
            client=client,
            game_id="0022500001",
            StartPeriod="2",
            EndPeriod="4",
        )
        _, params = client.calls[0]
        assert params["StartPeriod"] == "2"
        assert params["EndPeriod"] == "4"

    def test_kwargs_can_add_range_triplet_for_future_compat(
        self, recording_client
    ):
        """If a caller explicitly passes range keys via ``**kwargs`` they
        are forwarded verbatim — the wrapper does not filter kwargs.

        This documents the wrapper's permissive kwargs-forwarding
        behavior: additions are allowed, but defaults do not include them.
        """
        client = recording_client()
        games.fetch_playbyplayv2(
            client=client,
            game_id="0022500001",
            StartRange="0",
            EndRange="28800",
            RangeType="0",
        )
        _, params = client.calls[0]
        # The wrapper's ``params.update(kwargs)`` dutifully injects the
        # extra keys even though they are not part of the default surface.
        assert params["StartRange"] == "0"
        assert params["EndRange"] == "28800"
        assert params["RangeType"] == "0"

    def test_return_value_is_raw_client_response(self, recording_client):
        """The wrapper returns exactly the client's response, identity-preserved."""
        response: Dict[str, Any] = {
            "resultSets": [{"name": "PlayByPlay"}]
        }
        client = recording_client(responses={"playbyplayv2": response})
        result = games.fetch_playbyplayv2(
            client=client, game_id="0022500001"
        )
        assert result is response

    def test_single_call_per_invocation(self, recording_client):
        """Exactly one call to ``client.get`` per wrapper invocation."""
        client = recording_client()
        games.fetch_playbyplayv2(client=client, game_id="0022500001")
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# TestModuleInvariants — Rule 1, Rule 7, logger and public-callable invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Module-level invariants for ``endpoints/games.py``.

    These tests verify the ``endpoints/games.py`` module complies with
    the AAP architectural rules that prohibit direct HTTP or pandas
    usage at the endpoint layer and that the module exposes exactly the
    four documented public wrappers with a properly-named logger
    adapter.
    """

    def test_module_does_not_import_requests(self):
        """Rule 1 — Single HTTP Client: no direct ``requests`` import."""
        assert not hasattr(games, "requests")
        assert not hasattr(games, "urllib")
        assert not hasattr(games, "httpx")

    def test_module_does_not_import_pandas(self):
        """Rule 7 — Pluggable Storage: no ``pandas`` at this layer."""
        assert not hasattr(games, "pd")
        assert not hasattr(games, "pandas")

    def test_four_public_callables_exported(self):
        """All four documented wrappers are present and callable."""
        for name in (
            "fetch_scoreboardv2",
            "fetch_boxscoretraditionalv2",
            "fetch_boxscoreadvancedv2",
            "fetch_playbyplayv2",
        ):
            assert hasattr(games, name), (
                f"endpoints.games is missing public wrapper {name!r}"
            )
            assert callable(getattr(games, name)), (
                f"endpoints.games.{name} is not callable"
            )

    def test_module_uses_module_level_logger(self):
        """The module exposes a ``logger`` attribute (F-008)."""
        assert hasattr(games, "logger"), (
            "endpoints.games must expose a module-level logger"
        )

    def test_logger_name_matches_module_path(self):
        """The underlying logger name matches ``endpoints.games`` — a
        prerequisite for correlation-ID injection via
        :class:`utils.correlation.CorrelationAdapter` wrapping a logger
        obtained from ``utils.logger.get_logger(__name__)``.
        """
        adapter = games.logger
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "endpoints.games"


# ---------------------------------------------------------------------------
# TestParamDictShape — parametric invariants common to all four wrappers
# ---------------------------------------------------------------------------


class TestParamDictShape:
    """Parametric invariants that hold for every Games wrapper.

    These tests execute once per wrapper (via ``@pytest.mark.parametrize``)
    and verify invariants that are common to all four wrappers regardless
    of their individual parameter surfaces.
    """

    @pytest.mark.parametrize(
        "func, call_kwargs, expected_endpoint",
        [
            (
                games.fetch_scoreboardv2,
                {"game_date": "10/22/2024"},
                "scoreboardv2",
            ),
            (
                games.fetch_boxscoretraditionalv2,
                {"game_id": "0022500001"},
                "boxscoretraditionalv2",
            ),
            (
                games.fetch_boxscoreadvancedv2,
                {"game_id": "0022500001"},
                "boxscoreadvancedv2",
            ),
            (
                games.fetch_playbyplayv2,
                {"game_id": "0022500001"},
                "playbyplayv2",
            ),
        ],
        ids=[
            "scoreboardv2",
            "boxscoretraditionalv2",
            "boxscoreadvancedv2",
            "playbyplayv2",
        ],
    )
    def test_every_wrapper_issues_exactly_one_call(
        self, recording_client, func, call_kwargs, expected_endpoint
    ):
        """Each wrapper emits exactly one call with its canonical endpoint name."""
        client = recording_client()
        func(client=client, **call_kwargs)
        assert len(client.calls) == 1
        endpoint, _ = client.calls[0]
        assert endpoint == expected_endpoint

    @pytest.mark.parametrize(
        "func, call_kwargs, required_key, required_value",
        [
            (
                games.fetch_scoreboardv2,
                {"game_date": "10/22/2024"},
                "GameDate",
                "10/22/2024",
            ),
            (
                games.fetch_boxscoretraditionalv2,
                {"game_id": "0022500001"},
                "GameID",
                "0022500001",
            ),
            (
                games.fetch_boxscoreadvancedv2,
                {"game_id": "0022500001"},
                "GameID",
                "0022500001",
            ),
            (
                games.fetch_playbyplayv2,
                {"game_id": "0022500001"},
                "GameID",
                "0022500001",
            ),
        ],
        ids=[
            "scoreboardv2",
            "boxscoretraditionalv2",
            "boxscoreadvancedv2",
            "playbyplayv2",
        ],
    )
    def test_every_wrapper_populates_required_identifier_verbatim(
        self,
        recording_client,
        func,
        call_kwargs,
        required_key,
        required_value,
    ):
        """Each wrapper's required identifier is present verbatim in params."""
        client = recording_client()
        func(client=client, **call_kwargs)
        _, params = client.calls[0]
        assert params[required_key] == required_value

    @pytest.mark.parametrize(
        "func, call_kwargs",
        [
            (games.fetch_scoreboardv2, {"game_date": "10/22/2024"}),
            (
                games.fetch_boxscoretraditionalv2,
                {"game_id": "0022500001"},
            ),
            (
                games.fetch_boxscoreadvancedv2,
                {"game_id": "0022500001"},
            ),
            (games.fetch_playbyplayv2, {"game_id": "0022500001"}),
        ],
        ids=[
            "scoreboardv2",
            "boxscoretraditionalv2",
            "boxscoreadvancedv2",
            "playbyplayv2",
        ],
    )
    def test_no_games_wrapper_emits_season_or_season_type(
        self, recording_client, func, call_kwargs
    ):
        """Games wrappers never emit ``Season`` or ``SeasonType`` keys."""
        client = recording_client()
        func(client=client, **call_kwargs)
        _, params = client.calls[0]
        assert "Season" not in params
        assert "SeasonType" not in params

    @pytest.mark.parametrize(
        "func, call_kwargs",
        [
            (games.fetch_scoreboardv2, {"game_date": "10/22/2024"}),
            (
                games.fetch_boxscoretraditionalv2,
                {"game_id": "0022500001"},
            ),
            (
                games.fetch_boxscoreadvancedv2,
                {"game_id": "0022500001"},
            ),
            (games.fetch_playbyplayv2, {"game_id": "0022500001"}),
        ],
        ids=[
            "scoreboardv2",
            "boxscoretraditionalv2",
            "boxscoreadvancedv2",
            "playbyplayv2",
        ],
    )
    def test_every_wrapper_param_dict_values_are_json_serializable(
        self, recording_client, func, call_kwargs
    ):
        """All emitted param values are JSON-serializable primitives.

        This satisfies the AAP §0.4.1.2 contract that wrappers never emit
        datetime objects, custom classes, or other non-primitive values.
        """
        import json

        client = recording_client()
        func(client=client, **call_kwargs)
        _, params = client.calls[0]
        # If any value were non-primitive (dict, list of non-primitives,
        # custom object), json.dumps would raise TypeError.
        serialized = json.dumps(params)
        assert isinstance(serialized, str)
        assert len(serialized) > 0
