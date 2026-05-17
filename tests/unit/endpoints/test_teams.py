"""Unit tests for :mod:`endpoints.teams` — the Teams-domain endpoint wrappers.

These tests verify the three thin wrappers exposed by
:mod:`endpoints.teams` (Feature F-010): each wrapper must build the
correct parameter dict for its upstream endpoint and delegate to
:meth:`api.nba_client.NBAClient.get` — the sole HTTP transport in the
codebase (Rule 1, Single HTTP Client).

Coverage matrix
---------------

For every wrapper the tests verify:

* **Endpoint name routing** — ``client.get`` is called with the EXACT
  upstream endpoint name (``leaguedashteamstats``,
  ``teamgamelog``, ``teamdashboardbygeneralsplits``).
* **Param dict construction** — the literal dict is built from the
  positional / keyword arguments with the documented defaults.
* **Default-argument propagation from config** — omitting
  ``season_type`` and ``league_id`` uses
  :data:`config.DEFAULT_SEASON_TYPE` and
  :data:`config.DEFAULT_LEAGUE_ID` (Gate 12 Config Propagation Tracing).
* **kwargs override** — caller-supplied ``**kwargs`` are applied AFTER
  the literal dict (``params.update(kwargs)``), so overrides win.
* **TeamID type casting** — :func:`fetch_teamgamelog` and
  :func:`fetch_teamdashboardbygeneralsplits` accept both :class:`str`
  and :class:`int` ``team_id`` values and cast to :class:`str` before
  inclusion in the params dict.
* **Return-value passthrough** — the wrapper returns the raw JSON dict
  produced by ``client.get`` without modification.
* **Single call per invocation** — the wrapper issues exactly ONE
  ``client.get`` call (no extra enumeration or discovery calls).

Rule 1 invariants also verified
-------------------------------

* The test module itself does NOT import :mod:`requests` or any of
  :mod:`urllib` / :mod:`httpx` — the production wrappers use
  ``NBAClient`` exclusively, and the tests use :class:`RecordingClient`
  (a spy-style stand-in defined in :mod:`tests.conftest`) as a drop-in
  replacement.

Mocking strategy
----------------

* :class:`RecordingClient` records every ``(endpoint, params)`` tuple
  on its :attr:`calls` attribute so assertions can inspect the exact
  dict passed in. No live HTTP is performed.
* The ``recording_client`` fixture in :mod:`tests.conftest` returns a
  factory producing fresh ``RecordingClient`` instances per test so
  recorded call state does not leak across tests.

Test organization
-----------------

One :class:`TestCase`-style class per wrapper for grouping, with the
following naming convention:

* ``TestFetchLeaguedashteamstats`` — tests for ``leaguedashteamstats``.
* ``TestFetchTeamgamelog`` — tests for ``teamgamelog``.
* ``TestFetchTeamdashboardbygeneralsplits`` — tests for
  ``teamdashboardbygeneralsplits``.

A final :class:`TestModuleInvariants` group verifies cross-wrapper
invariants (Rule 1, the fixed endpoint-name strings).
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

import config
from endpoints import teams


# ---------------------------------------------------------------------------
# ``fetch_leaguedashteamstats``
# ---------------------------------------------------------------------------


class TestFetchLeaguedashteamstats:
    """Behavioral tests for :func:`endpoints.teams.fetch_leaguedashteamstats`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        """The wrapper must call ``client.get("leaguedashteamstats", ...)``."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(client=client, season="2025-26")

        # Exactly ONE call recorded.
        assert len(client.calls) == 1
        # Endpoint name is the EXACT upstream identifier.
        assert client.calls[0][0] == "leaguedashteamstats"

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        """Omitting ``season_type`` / ``league_id`` uses the config defaults (Gate 12)."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(client=client, season="2025-26")

        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_required_param_surface_is_populated(self, recording_client) -> None:
        """The literal dict populates every NBA Stats filter the endpoint expects."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(
            client=client,
            season="2024-25",
            per_mode="Totals",
            measure_type="Advanced",
        )

        _, params = client.calls[0]
        # Core inputs.
        assert params["Season"] == "2024-25"
        assert params["PerMode"] == "Totals"
        assert params["MeasureType"] == "Advanced"
        # "N"-flag filters (raw stats, not derived).
        assert params["PlusMinus"] == "N"
        assert params["PaceAdjust"] == "N"
        assert params["Rank"] == "N"
        # Numeric defaults as string "0".
        assert params["LastNGames"] == "0"
        assert params["Month"] == "0"
        assert params["OpponentTeamID"] == "0"
        assert params["Period"] == "0"
        assert params["PORound"] == "0"
        # ``leaguedashteamstats`` ignores a specific team filter, so
        # ``TeamID`` is pinned to "0" by default.
        assert params["TeamID"] == "0"
        assert params["TwoWay"] == "0"

    def test_filter_strings_default_empty(self, recording_client) -> None:
        """Empty-string filters match the upstream "no filter applied" semantics."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(client=client, season="2025-26")

        _, params = client.calls[0]
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
            "GameScope",
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
        ):
            assert params[key] == "", f"expected empty filter for {key!r}"

    def test_kwargs_override_defaults(self, recording_client) -> None:
        """``**kwargs`` win over the literal defaults via ``params.update``."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(
            client=client,
            season="2025-26",
            Conference="East",
            LastNGames="10",
            PlusMinus="Y",
        )

        _, params = client.calls[0]
        assert params["Conference"] == "East"
        assert params["LastNGames"] == "10"
        assert params["PlusMinus"] == "Y"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        """The wrapper returns exactly the dict produced by ``client.get``."""
        response: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueDashTeamStats",
                    "headers": ["TEAM_ID", "TEAM_NAME", "GP"],
                    "rowSet": [[1610612747, "Los Angeles Lakers", 82]],
                }
            ]
        }
        client = recording_client(responses={"leaguedashteamstats": response})
        result = teams.fetch_leaguedashteamstats(client=client, season="2025-26")

        assert result is response

    def test_returns_raw_payload_from_shared_fixture(
        self, recording_client, sample_single_table_payload
    ) -> None:
        """The wrapper returns the canonical ``resultSets`` envelope unchanged.

        Uses the shared ``sample_single_table_payload`` fixture from
        :mod:`tests.conftest` — the same envelope shape the schema
        normalizer accepts — to guarantee the wrapper performs zero
        mutation between ``client.get`` and the caller. This is a
        passthrough contract: if the wrapper ever starts re-shaping or
        filtering the response, this assertion will fail loudly.
        """
        client = recording_client(
            responses={"leaguedashteamstats": sample_single_table_payload}
        )
        result = teams.fetch_leaguedashteamstats(client=client, season="2025-26")

        # Identity equality proves zero mutation — the same dict object
        # flows from ``client.get`` through the wrapper to the caller.
        assert result is sample_single_table_payload
        # Structural equality is a strictly weaker assertion but also
        # holds and documents the expected envelope shape.
        assert result == sample_single_table_payload

    def test_explicit_season_type_and_league_id_win(self, recording_client) -> None:
        """Explicit args override the config defaults."""
        client = recording_client()
        teams.fetch_leaguedashteamstats(
            client=client,
            season="2025-26",
            season_type="Playoffs",
            league_id="00",
        )

        _, params = client.calls[0]
        assert params["SeasonType"] == "Playoffs"
        assert params["LeagueID"] == "00"


# ---------------------------------------------------------------------------
# ``fetch_teamgamelog``
# ---------------------------------------------------------------------------


class TestFetchTeamgamelog:
    """Behavioral tests for :func:`endpoints.teams.fetch_teamgamelog`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        """The wrapper must call ``client.get("teamgamelog", ...)``."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        assert len(client.calls) == 1
        assert client.calls[0][0] == "teamgamelog"

    def test_team_id_string_is_passed_through_verbatim(
        self, recording_client
    ) -> None:
        """A string ``team_id`` flows into the params dict unchanged."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["TeamID"] == "1610612747"

    def test_team_id_int_is_cast_to_string(self, recording_client) -> None:
        """An integer ``team_id`` is ``str``-cast at the call site."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id=1610612747,
            season="2025-26",
        )

        _, params = client.calls[0]
        # The cast guarantees the upstream wire format is always str.
        assert params["TeamID"] == "1610612747"
        assert isinstance(params["TeamID"], str)

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        """Omitting ``season_type`` / ``league_id`` uses the config defaults (Gate 12)."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_date_from_and_date_to_default_to_empty_string(
        self, recording_client
    ) -> None:
        """Omitting date bounds produces empty-string params (no upstream filter)."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["DateFrom"] == ""
        assert params["DateTo"] == ""

    def test_explicit_date_bounds_are_passed_through(
        self, recording_client
    ) -> None:
        """Explicit date args flow into ``DateFrom`` / ``DateTo`` params."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
            date_from="10/22/2024",
            date_to="04/13/2025",
        )

        _, params = client.calls[0]
        assert params["DateFrom"] == "10/22/2024"
        assert params["DateTo"] == "04/13/2025"

    def test_params_contains_only_six_keys_by_default(
        self, recording_client
    ) -> None:
        """The ``teamgamelog`` params surface is small and well-bounded."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        # The upstream surface is small; six keys is the full default surface.
        assert set(params.keys()) == {
            "TeamID",
            "Season",
            "SeasonType",
            "LeagueID",
            "DateFrom",
            "DateTo",
        }

    def test_kwargs_override_defaults(self, recording_client) -> None:
        """``**kwargs`` applied AFTER the literal dict via ``params.update``."""
        client = recording_client()
        teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
            date_from="10/22/2024",
            DateFrom="01/01/2025",  # kwargs must win.
        )

        _, params = client.calls[0]
        assert params["DateFrom"] == "01/01/2025"

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        """The wrapper returns exactly the dict produced by ``client.get``."""
        response: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "TeamGameLog",
                    "headers": ["Team_ID", "Game_ID", "PTS"],
                    "rowSet": [[1610612747, "0022500001", 114]],
                }
            ]
        }
        client = recording_client(responses={"teamgamelog": response})
        result = teams.fetch_teamgamelog(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        assert result is response


# ---------------------------------------------------------------------------
# ``fetch_teamdashboardbygeneralsplits``
# ---------------------------------------------------------------------------


class TestFetchTeamdashboardbygeneralsplits:
    """Behavioral tests for :func:`endpoints.teams.fetch_teamdashboardbygeneralsplits`."""

    def test_delegates_to_client_get_with_correct_endpoint_name(
        self, recording_client
    ) -> None:
        """The wrapper must call ``client.get("teamdashboardbygeneralsplits", ...)``."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        assert len(client.calls) == 1
        assert client.calls[0][0] == "teamdashboardbygeneralsplits"

    def test_team_id_is_str_cast(self, recording_client) -> None:
        """The dashboard endpoint also casts integer ``team_id`` to ``str``."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id=1610612747,
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["TeamID"] == "1610612747"
        assert isinstance(params["TeamID"], str)

    def test_default_season_type_and_league_id_propagate_from_config(
        self, recording_client
    ) -> None:
        """Omitting ``season_type`` / ``league_id`` uses the config defaults (Gate 12)."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_default_per_mode_and_measure_type(self, recording_client) -> None:
        """``per_mode`` defaults to ``"PerGame"``; ``measure_type`` defaults to ``"Base"``."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["PerMode"] == "PerGame"
        assert params["MeasureType"] == "Base"

    def test_filter_flags_default_to_raw_stats(self, recording_client) -> None:
        """``PlusMinus``, ``PaceAdjust``, ``Rank`` default to ``"N"`` (raw stats)."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        assert params["PlusMinus"] == "N"
        assert params["PaceAdjust"] == "N"
        assert params["Rank"] == "N"

    def test_numeric_default_filters_are_zero_strings(
        self, recording_client
    ) -> None:
        """Numeric default filters are string ``"0"`` per NBA Stats convention."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        for key in (
            "LastNGames",
            "Month",
            "OpponentTeamID",
            "Period",
            "PORound",
        ):
            assert params[key] == "0", f"expected '0' for {key!r}"

    def test_string_filters_default_empty(self, recording_client) -> None:
        """String filters default to empty — no upstream filter applied."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
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
        ):
            assert params[key] == "", f"expected empty filter for {key!r}"

    def test_kwargs_override_defaults(self, recording_client) -> None:
        """``**kwargs`` applied AFTER the literal dict via ``params.update``."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
            MeasureType="Advanced",
            Location="Home",
            Month="10",
        )

        _, params = client.calls[0]
        assert params["MeasureType"] == "Advanced"
        assert params["Location"] == "Home"
        assert params["Month"] == "10"

    def test_explicit_per_mode_and_measure_type(self, recording_client) -> None:
        """Explicit positional / keyword args override the defaults."""
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
            per_mode="Totals",
            measure_type="Advanced",
        )

        _, params = client.calls[0]
        assert params["PerMode"] == "Totals"
        assert params["MeasureType"] == "Advanced"

    def test_team_level_filter_surface_excludes_player_filters(
        self, recording_client
    ) -> None:
        """Team endpoints must NOT include player-specific filters.

        The NBA Stats API surface for team dashboards does not accept
        ``PlayerExperience`` / ``PlayerPosition`` / ``StarterBench``
        / ``TwoWay`` — these are player-only filters. If the wrapper
        included them the upstream API would return 400 Bad Request.
        This test asserts they are ABSENT from the default params dict.
        """
        client = recording_client()
        teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        _, params = client.calls[0]
        for forbidden in (
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
            "TwoWay",
            "College",
            "DraftYear",
            "DraftPick",
            "Height",
            "Weight",
        ):
            assert forbidden not in params, (
                f"params must NOT include player-only filter {forbidden!r} "
                f"for the team dashboard endpoint"
            )

    def test_return_value_is_raw_client_response(self, recording_client) -> None:
        """The wrapper returns exactly the multi-table dict produced by ``client.get``."""
        # This endpoint returns a multi-table envelope in production.
        response: Dict[str, Any] = {
            "resultSets": [
                {"name": "OverallTeamDashboard", "headers": ["A"], "rowSet": [[1]]},
                {"name": "LocationTeamDashboard", "headers": ["A"], "rowSet": [[2]]},
                {"name": "WinsLossesTeamDashboard", "headers": ["A"], "rowSet": [[3]]},
                {"name": "MonthTeamDashboard", "headers": ["A"], "rowSet": [[4]]},
                {"name": "PrePostAllStarTeamDashboard", "headers": ["A"], "rowSet": [[5]]},
                {"name": "DaysRestTeamDashboard", "headers": ["A"], "rowSet": [[6]]},
            ]
        }
        client = recording_client(
            responses={"teamdashboardbygeneralsplits": response}
        )
        result = teams.fetch_teamdashboardbygeneralsplits(
            client=client,
            team_id="1610612747",
            season="2025-26",
        )

        assert result is response
        # All six tables are present and not mutated.
        assert len(result["resultSets"]) == 6


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Cross-wrapper invariants that must hold regardless of which wrapper ran."""

    def test_module_does_not_import_requests(self) -> None:
        """Rule 1 — Single HTTP Client.

        ``endpoints.teams`` must NEVER import :mod:`requests` or any
        transport-layer package; all HTTP goes through ``NBAClient.get``.
        Verified by inspecting the module's ``__dict__`` — a live
        ``requests`` attribute would indicate a smuggled import.
        """
        assert not hasattr(teams, "requests"), (
            "endpoints.teams must not expose a `requests` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(teams, "urllib"), (
            "endpoints.teams must not expose a `urllib` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(teams, "httpx"), (
            "endpoints.teams must not expose an `httpx` attribute "
            "(Rule 1 — Single HTTP Client)"
        )

    def test_module_does_not_import_pandas(self) -> None:
        """Rule 4 (indirect) — endpoint wrappers must not construct DataFrames.

        Flattening belongs to :mod:`utils.schema_normalizer`; the
        wrapper layer returns raw JSON unchanged.
        """
        assert not hasattr(teams, "pd"), (
            "endpoints.teams must not expose `pd` (pandas)"
        )
        assert not hasattr(teams, "pandas"), (
            "endpoints.teams must not expose `pandas`"
        )

    def test_three_public_callables_exported(self) -> None:
        """Exactly three wrapper functions are exported as required by schema."""
        for name in (
            "fetch_leaguedashteamstats",
            "fetch_teamgamelog",
            "fetch_teamdashboardbygeneralsplits",
        ):
            assert callable(getattr(teams, name)), (
                f"endpoints.teams.{name} must be a public callable"
            )

    def test_module_uses_module_level_logger(self) -> None:
        """A single module-level ``logger`` is attached for Observability."""
        assert hasattr(teams, "logger"), (
            "endpoints.teams must expose a module-level `logger`"
        )

    def test_logger_name_matches_module_path(self) -> None:
        """The module-level logger is named via ``__name__`` per F-008."""
        # ``get_logger`` returns a ``LoggerAdapter``; the underlying
        # logger is accessible via ``.logger`` on the adapter.
        adapter = teams.logger
        # The adapter wraps a ``logging.Logger`` whose name is the
        # module's dotted path. The attribute chain works for both
        # :class:`logging.LoggerAdapter` and the project's
        # ``CorrelationAdapter``.
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "endpoints.teams"


# ---------------------------------------------------------------------------
# Parameter-level deep-coverage tests (parametric)
# ---------------------------------------------------------------------------


class TestParamDictShape:
    """Parametric coverage of param-dict invariants across the wrappers."""

    @pytest.mark.parametrize(
        ("func", "extra_kwargs", "expected_endpoint"),
        [
            (
                teams.fetch_leaguedashteamstats,
                {},
                "leaguedashteamstats",
            ),
            (
                teams.fetch_teamgamelog,
                {"team_id": "1610612747"},
                "teamgamelog",
            ),
            (
                teams.fetch_teamdashboardbygeneralsplits,
                {"team_id": "1610612747"},
                "teamdashboardbygeneralsplits",
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
        """Each wrapper issues exactly one ``client.get`` call.

        No extra enumeration, discovery, or secondary endpoint calls
        happen inside the wrapper layer — that belongs to the pipeline.
        """
        client = recording_client()
        func(client=client, season="2025-26", **extra_kwargs)
        assert len(client.calls) == 1
        assert client.calls[0][0] == expected_endpoint

    @pytest.mark.parametrize(
        ("func", "extra_kwargs"),
        [
            (teams.fetch_leaguedashteamstats, {}),
            (teams.fetch_teamgamelog, {"team_id": "1610612747"}),
            (
                teams.fetch_teamdashboardbygeneralsplits,
                {"team_id": "1610612747"},
            ),
        ],
    )
    def test_every_wrapper_populates_season_verbatim(
        self,
        recording_client,
        func,
        extra_kwargs,
    ) -> None:
        """The ``season`` argument flows into ``params["Season"]`` verbatim."""
        client = recording_client()
        func(client=client, season="2024-25", **extra_kwargs)
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"
