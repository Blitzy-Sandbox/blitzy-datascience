"""Unit tests for the Lineups endpoint wrappers.

Covers Feature F-012 (Lineups) per the Agent Action Plan
§0.5.1.4 (Group 4 — Endpoint Wrappers) and §0.5.1.8 (Group 8 — Tests):

    - :func:`endpoints.lineups.fetch_leaguedashlineups`
    - :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff`

Plus an explicit, mandatory disambiguation test class covering the
subtle-but-critical collision documented in the Checkpoint IC-3 QA
report: ``endpoints.players.fetch_leaguedashplayerclutch`` and
``endpoints.lineups.fetch_leaguedashplayerclutch_onoff`` hit the SAME
upstream endpoint name (``"leaguedashplayerclutch"``) but differ at
the ``params`` level.  The single param-level differentiator is the
``"TwoWay"`` key.  This fact is the headline behavioural contract of
the Lineups package and is therefore tested here directly, by keyset
difference, with BOTH modules imported.

Test coverage matrix (dimension × wrapper)
------------------------------------------
      | Endpoint    | Defaults | Key-set | int-cast | kwargs    | Config   | Single-call | Return passthrough | Logger |
      |             |          |         |          | override  | propag.  | invariant   | (object identity)  | format |
------|-------------|----------|---------|----------|-----------|----------|-------------|--------------------|--------|
Lg-L  |      ✓      |    ✓     |   ✓     |   ✓      |    ✓      |    ✓     |      ✓      |         ✓          |   ✓    |
Lg-C  |      ✓      |    ✓     |   ✓     |   ✓      |    ✓      |    ✓     |      ✓      |         ✓          |   ✓    |

(Lg-L = :func:`fetch_leaguedashlineups`;
 Lg-C = :func:`fetch_leaguedashplayerclutch_onoff`)

Rule compliance
---------------
- Rule 1 — Single HTTP Client: this test module never imports
  :mod:`requests`.  Every simulated network edge is a
  ``RecordingClient`` instance provided by the ``recording_client``
  fixture in ``tests/conftest.py``.  Additionally, the module-level
  invariant tests assert that ``endpoints.lineups`` never exposes a
  ``requests``, ``urllib``, or ``httpx`` attribute.
- Rule 3 — Required Headers: headers are a transport-layer concern
  satisfied by :class:`api.nba_client.NBAClient` and verified by its
  own test suite.  The wrappers themselves do not set headers.
- Rule 4 — Flat CSV output (indirect): the wrapper layer must never
  construct :class:`pandas.DataFrame` objects; flattening belongs to
  :mod:`utils.schema_normalizer`.  Module-level invariants assert
  that ``endpoints.lineups`` never exposes ``pd`` or ``pandas``.
- Rule 7 — Pluggable Storage: the wrapper layer must never call
  :meth:`pandas.DataFrame.to_csv`; writes belong to
  :class:`storage.csv_writer.CSVWriter`.  The invariants above
  enforce the first half of this rule transitively.

Mocking strategy (per ``tests/conftest.py`` directive)
------------------------------------------------------
All tests use the handwritten :class:`RecordingClient` spy.
``MagicMock`` and ``unittest.mock`` are intentionally avoided so that
parameter-dict assertions are exercised against a real ``dict`` that
flows through the same contract surface that :class:`NBAClient` would
accept at runtime.  The ``recording_client`` fixture is a factory and
is invoked via ``recording_client()`` (no positional arg) to obtain
a fresh, empty-response client per test.

Test organization
-----------------
1. :class:`TestFetchLeaguedashlineups` — per-wrapper contract.
2. :class:`TestFetchLeaguedashplayerclutch_onoff` — per-wrapper contract.
3. :class:`TestDisambiguation` — cross-module invariant: same
   upstream endpoint, different params, delta = ``{"TwoWay"}``.
4. :class:`TestModuleInvariants` — Rule 1 / Rule 4 / Observability
   assertions at the module level.
5. :class:`TestParamDictShape` — parametric coverage for both
   wrappers.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

import config
from endpoints import lineups
from endpoints import players


# ---------------------------------------------------------------------------
# Shared constants derived from the authoritative source modules
# ---------------------------------------------------------------------------

EXPECTED_LEAGUEDASHLINEUPS_KEYS: frozenset = frozenset(
    {
        # Identity & scope
        "Season",
        "SeasonType",
        "LeagueID",
        "PerMode",
        "MeasureType",
        "GroupQuantity",
        # Baked-in filter constants
        "PlusMinus",
        "PaceAdjust",
        "Rank",
        "LastNGames",
        "Month",
        "OpponentTeamID",
        "Period",
        "PORound",
        "TeamID",
        # Empty-string defaults
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
        # Headline disambiguation key for Lineups domain
        "TwoWay",
    }
)

EXPECTED_LEAGUEDASHPLAYERCLUTCH_ONOFF_KEYS: frozenset = frozenset(
    {
        # Identity & scope
        "Season",
        "SeasonType",
        "LeagueID",
        "PerMode",
        "MeasureType",
        # Clutch triplet
        "ClutchTime",
        "AheadBehind",
        "PointDiff",
        # Baked-in filter constants
        "PlusMinus",
        "PaceAdjust",
        "Rank",
        "LastNGames",
        "Month",
        "OpponentTeamID",
        "Period",
        "PORound",
        "TeamID",
        # Empty-string defaults
        "DateFrom",
        "DateTo",
        "GameSegment",
        "Location",
        "Outcome",
        "SeasonSegment",
        "ShotClockRange",
        "VsConference",
        "VsDivision",
        "College",
        "Conference",
        "Country",
        "DraftPick",
        "DraftYear",
        "Division",
        "GameScope",
        "Height",
        "PlayerExperience",
        "PlayerPosition",
        "StarterBench",
        # Headline disambiguation key for Lineups domain
        "TwoWay",
        "Weight",
    }
)

EXPECTED_PLAYERS_LEAGUEDASHPLAYERCLUTCH_KEYS: frozenset = frozenset(
    {
        # Identity & scope
        "Season",
        "SeasonType",
        "LeagueID",
        "PerMode",
        "MeasureType",
        # Clutch triplet
        "ClutchTime",
        "AheadBehind",
        "PointDiff",
        # Baked-in filter constants
        "PlusMinus",
        "PaceAdjust",
        "Rank",
        "LastNGames",
        "Month",
        "OpponentTeamID",
        "Period",
        "PORound",
        "TeamID",
        # Empty-string defaults
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
        # NOTE: TwoWay is INTENTIONALLY absent from the Players variant.
    }
)


# ---------------------------------------------------------------------------
# fetch_leaguedashlineups (F-012, endpoint "leaguedashlineups")
# ---------------------------------------------------------------------------


class TestFetchLeaguedashlineups:
    """Contract tests for :func:`endpoints.lineups.fetch_leaguedashlineups`.

    Verifies that the wrapper routes to the upstream endpoint string
    ``"leaguedashlineups"`` with a 31-key param surface that includes
    the lineup-specific ``GroupQuantity`` field (default ``"5"``)
    plus the Lineups-domain ``TwoWay`` disambiguator.
    """

    def test_calls_correct_endpoint(self, recording_client) -> None:
        """The wrapper must call ``client.get("leaguedashlineups", ...)``.

        Rule 1 — the wrapper delegates to ``NBAClient.get``; the
        endpoint string is the first positional argument.
        """
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        assert len(client.calls) == 1
        endpoint, _params = client.calls[0]
        assert endpoint == "leaguedashlineups"

    def test_params_contains_season_verbatim(self, recording_client) -> None:
        """The ``season`` argument must flow into ``params["Season"]`` unchanged."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2024-25")
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"

    def test_defaults_applied_when_not_overridden(self, recording_client) -> None:
        """Non-overridden defaults populate ``SeasonType`` / ``PerMode`` / ``MeasureType`` / ``GroupQuantity``."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["SeasonType"] == "Regular Season"
        assert params["PerMode"] == "PerGame"
        assert params["MeasureType"] == "Base"
        assert params["GroupQuantity"] == "5"

    def test_league_id_default_flows_from_config(self, recording_client) -> None:
        """Gate 12 — LeagueID default is read from :mod:`config`, not hardcoded.

        We assert the value equals ``config.DEFAULT_LEAGUE_ID`` rather
        than literal ``"00"`` so that changing the config constant
        would reach the wrapper without touching the test.
        """
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_baked_in_constants(self, recording_client) -> None:
        """``PlusMinus`` / ``PaceAdjust`` / ``Rank`` / ``TwoWay`` baked to contract values."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["PlusMinus"] == "N"
        assert params["PaceAdjust"] == "N"
        assert params["Rank"] == "N"
        # Lineups-domain disambiguator: TwoWay present with default "0".
        assert params["TwoWay"] == "0"

    def test_zero_string_numeric_defaults(self, recording_client) -> None:
        """Numeric-but-string-typed filters default to the "0" scalar string."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        for key in (
            "LastNGames",
            "Month",
            "OpponentTeamID",
            "Period",
            "PORound",
            "TeamID",
        ):
            assert params[key] == "0", f"{key} must default to '0'"

    def test_empty_string_defaults(self, recording_client) -> None:
        """Discretionary filter slots default to empty string when not passed."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
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
            assert params[key] == "", (
                f"{key} must default to empty string, got {params[key]!r}"
            )

    def test_exact_31_key_surface(self, recording_client) -> None:
        """The params dict has the exact 31-key surface, no omissions, no extras."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        assert isinstance(params, dict)
        assert set(params.keys()) == set(EXPECTED_LEAGUEDASHLINEUPS_KEYS), (
            f"Unexpected key-set delta: "
            f"extras={set(params.keys()) - EXPECTED_LEAGUEDASHLINEUPS_KEYS} "
            f"missing={EXPECTED_LEAGUEDASHLINEUPS_KEYS - set(params.keys())}"
        )
        assert len(params) == 31

    def test_excludes_player_demographic_keys(self, recording_client) -> None:
        """Lineups does NOT expose per-player demographic filters.

        These keys exist in the Players domain's clutch variant but
        are structurally absent from the Lineups leaguedashlineups
        param surface (the unit of analysis is a 5-man lineup, not a
        player).
        """
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        _, params = client.calls[0]
        for excluded in ("College", "Country", "DraftPick", "DraftYear", "Height", "Weight"):
            assert excluded not in params, (
                f"{excluded} must NOT appear in fetch_leaguedashlineups params"
            )

    def test_group_quantity_int_cast_to_string(self, recording_client) -> None:
        """Int ``group_quantity`` must be coerced to a string before upstream call.

        The wrapper signature types ``group_quantity`` as ``str`` but
        the body calls ``str(group_quantity)`` so callers that pass an
        ``int`` get a well-formed upstream request instead of a 400.
        """
        client = recording_client()
        lineups.fetch_leaguedashlineups(
            client=client, season="2025-26", group_quantity=3
        )
        _, params = client.calls[0]
        assert params["GroupQuantity"] == "3"
        assert isinstance(params["GroupQuantity"], str)

    def test_kwargs_override_default(self, recording_client) -> None:
        """Arbitrary upstream filter overrides via ``**kwargs`` are propagated.

        Implementation contract: ``params.update(kwargs)`` happens
        AFTER the base dict is built, so kwargs take precedence over
        hard-coded defaults.
        """
        client = recording_client()
        lineups.fetch_leaguedashlineups(
            client=client, season="2025-26", TwoWay="1", Period="3"
        )
        _, params = client.calls[0]
        # kwargs override wins over default "0" for TwoWay
        assert params["TwoWay"] == "1"
        # kwargs override wins over default "0" for Period
        assert params["Period"] == "3"

    def test_kwargs_add_novel_key(self, recording_client) -> None:
        """``**kwargs`` can add a novel key not in the base param dict."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(
            client=client, season="2025-26", NovelFutureFilter="xyz"
        )
        _, params = client.calls[0]
        assert params["NovelFutureFilter"] == "xyz"

    def test_overriding_per_mode(self, recording_client) -> None:
        """Explicit ``per_mode`` param overrides the ``"PerGame"`` default."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(
            client=client, season="2025-26", per_mode="Totals"
        )
        _, params = client.calls[0]
        assert params["PerMode"] == "Totals"

    def test_overriding_measure_type(self, recording_client) -> None:
        """Explicit ``measure_type`` overrides the ``"Base"`` default."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(
            client=client, season="2025-26", measure_type="Advanced"
        )
        _, params = client.calls[0]
        assert params["MeasureType"] == "Advanced"

    def test_single_http_call_invariant(self, recording_client) -> None:
        """The wrapper makes exactly one call — no enumeration happens here."""
        client = recording_client()
        lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        assert len(client.calls) == 1

    def test_return_value_is_upstream_dict_identity(self, recording_client) -> None:
        """The wrapper returns the exact object produced by ``client.get``.

        No mutation, no wrapping, no copy — the wrapper is a pure
        pass-through.
        """
        sentinel: Dict[str, Any] = {"resultSets": [{"name": "LineupsEnvelope"}]}
        client = recording_client(responses={"leaguedashlineups": sentinel})
        result = lineups.fetch_leaguedashlineups(client=client, season="2025-26")
        assert result is sentinel


# ---------------------------------------------------------------------------
# fetch_leaguedashplayerclutch_onoff (F-012, endpoint "leaguedashplayerclutch")
# ---------------------------------------------------------------------------


class TestFetchLeaguedashplayerclutchOnoff:
    """Contract tests for :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff`.

    Critical: this wrapper hits the SAME upstream endpoint name
    (``"leaguedashplayerclutch"``) as
    :func:`endpoints.players.fetch_leaguedashplayerclutch`.  The
    wrappers diverge at the ``params`` level — the Lineups variant
    includes an additional ``TwoWay`` key (default ``"0"``), making
    it a 39-key surface vs. the Players variant's 38-key surface.
    This disambiguation is tested explicitly in
    :class:`TestDisambiguation` below.
    """

    def test_calls_same_upstream_endpoint_as_players_variant(self, recording_client) -> None:
        """Routes to ``"leaguedashplayerclutch"`` — SAME upstream name as Players domain."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        assert len(client.calls) == 1
        endpoint, _params = client.calls[0]
        assert endpoint == "leaguedashplayerclutch"

    def test_params_contains_season_verbatim(self, recording_client) -> None:
        """``season`` flows into ``params["Season"]`` unchanged."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2024-25"
        )
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"

    def test_defaults_applied_when_not_overridden(self, recording_client) -> None:
        """Defaults flow through: ``SeasonType`` / ``LeagueID`` / ``PerMode`` / ``MeasureType``."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID
        assert params["PerMode"] == "PerGame"
        assert params["MeasureType"] == "Base"

    def test_clutch_triplet_defaults(self, recording_client) -> None:
        """The three-dimensional clutch filter has the canonical NBA default triplet."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        assert params["ClutchTime"] == "Last 5 Minutes"
        assert params["AheadBehind"] == "Ahead or Behind"
        assert params["PointDiff"] == "5"

    def test_twoway_default_present(self, recording_client) -> None:
        """The Lineups variant INCLUDES ``TwoWay`` (default ``"0"``).

        This is the single param-level differentiator between the
        Lineups ``_onoff`` variant and the Players variant of the
        same upstream endpoint.
        """
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        assert "TwoWay" in params, (
            "Lineups variant must include TwoWay key "
            "(disambiguation from Players variant)"
        )
        assert params["TwoWay"] == "0"

    def test_baked_in_constants(self, recording_client) -> None:
        """``PlusMinus`` / ``PaceAdjust`` / ``Rank`` baked to contract values."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        assert params["PlusMinus"] == "N"
        assert params["PaceAdjust"] == "N"
        assert params["Rank"] == "N"

    def test_zero_string_numeric_defaults(self, recording_client) -> None:
        """Numeric-but-string-typed filters default to ``"0"``."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        for key in (
            "LastNGames",
            "Month",
            "OpponentTeamID",
            "Period",
            "PORound",
            "TeamID",
        ):
            assert params[key] == "0", f"{key} must default to '0'"

    def test_empty_string_defaults(self, recording_client) -> None:
        """Empty-string defaults cover all demographic and discretionary filters."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
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
            "College",
            "Conference",
            "Country",
            "DraftPick",
            "DraftYear",
            "Division",
            "GameScope",
            "Height",
            "PlayerExperience",
            "PlayerPosition",
            "StarterBench",
            "Weight",
        ):
            assert params[key] == "", (
                f"{key} must default to empty string, got {params[key]!r}"
            )

    def test_exact_39_key_surface(self, recording_client) -> None:
        """The params dict has the exact 39-key surface; no omissions, no extras."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, params = client.calls[0]
        assert isinstance(params, dict)
        assert set(params.keys()) == set(EXPECTED_LEAGUEDASHPLAYERCLUTCH_ONOFF_KEYS), (
            f"Unexpected key-set delta: "
            f"extras="
            f"{set(params.keys()) - EXPECTED_LEAGUEDASHPLAYERCLUTCH_ONOFF_KEYS} "
            f"missing="
            f"{EXPECTED_LEAGUEDASHPLAYERCLUTCH_ONOFF_KEYS - set(params.keys())}"
        )
        assert len(params) == 39

    def test_point_diff_int_cast_to_string(self, recording_client) -> None:
        """Int ``point_diff`` is coerced to a string before upstream call."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26", point_diff=3
        )
        _, params = client.calls[0]
        assert params["PointDiff"] == "3"
        assert isinstance(params["PointDiff"], str)

    def test_kwargs_override_default(self, recording_client) -> None:
        """``**kwargs`` overrides win over base-dict defaults (``params.update(kwargs)``)."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26", TwoWay="1", PlusMinus="Y"
        )
        _, params = client.calls[0]
        assert params["TwoWay"] == "1"
        assert params["PlusMinus"] == "Y"

    def test_kwargs_add_novel_key(self, recording_client) -> None:
        """``**kwargs`` can add a novel key not present in the base dict."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26", NovelFutureFilter="xyz"
        )
        _, params = client.calls[0]
        assert params["NovelFutureFilter"] == "xyz"

    def test_explicit_clutch_triplet_overrides(self, recording_client) -> None:
        """Explicit ``clutch_time`` / ``ahead_behind`` / ``point_diff`` override defaults."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client,
            season="2025-26",
            clutch_time="Last 3 Minutes",
            ahead_behind="Behind or Tied",
            point_diff="3",
        )
        _, params = client.calls[0]
        assert params["ClutchTime"] == "Last 3 Minutes"
        assert params["AheadBehind"] == "Behind or Tied"
        assert params["PointDiff"] == "3"

    def test_single_http_call_invariant(self, recording_client) -> None:
        """Exactly one ``client.get`` call per wrapper invocation."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        assert len(client.calls) == 1

    def test_return_value_is_upstream_dict_identity(self, recording_client) -> None:
        """Wrapper returns ``client.get``'s output by identity (no wrapping)."""
        sentinel: Dict[str, Any] = {"resultSets": [{"name": "ClutchOnOffEnvelope"}]}
        client = recording_client(
            responses={"leaguedashplayerclutch": sentinel}
        )
        result = lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        assert result is sentinel


# ---------------------------------------------------------------------------
# Cross-module disambiguation (MANDATORY — QA report Phase 3d explicit)
# ---------------------------------------------------------------------------


class TestDisambiguation:
    """Cross-module invariant: Players vs. Lineups variants of ``leaguedashplayerclutch``.

    Both :func:`endpoints.players.fetch_leaguedashplayerclutch` and
    :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff` call
    the SAME upstream endpoint string ``"leaguedashplayerclutch"``
    but diverge at the ``params`` level by exactly one key,
    ``"TwoWay"``.

    This test class is MANDATORY per the QA testing checkpoint (IC-3
    Phase 3d).  The two modules MUST both be importable side-by-side
    and the key-set delta MUST equal ``{"TwoWay"}``.
    """

    def test_both_call_same_upstream_endpoint(self, recording_client) -> None:
        """Both wrappers route to the same upstream endpoint string.

        This captures the "SAME endpoint, DIFFERENT params" invariant
        in a single assertion.
        """
        client_lineups = recording_client()
        client_players = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client_lineups, season="2025-26"
        )
        players.fetch_leaguedashplayerclutch(
            client=client_players, season="2025-26"
        )
        assert client_lineups.calls[0][0] == "leaguedashplayerclutch"
        assert client_players.calls[0][0] == "leaguedashplayerclutch"
        assert client_lineups.calls[0][0] == client_players.calls[0][0]

    def test_wrappers_are_distinct_function_objects(self) -> None:
        """The two wrappers are distinct Python objects, not aliases.

        Defensive check: even though they both call the same upstream
        endpoint, the Python function identities are independent so
        that future behavioural divergence stays isolated per domain.
        """
        assert (
            players.fetch_leaguedashplayerclutch
            is not lineups.fetch_leaguedashplayerclutch_onoff
        )
        # Sanity: they are indeed located in different modules.
        assert (
            players.fetch_leaguedashplayerclutch.__module__
            == "endpoints.players"
        )
        assert (
            lineups.fetch_leaguedashplayerclutch_onoff.__module__
            == "endpoints.lineups"
        )

    def test_players_variant_has_38_keys_and_omits_twoway(self, recording_client) -> None:
        """The Players variant has a 38-key surface and OMITS ``"TwoWay"``."""
        client = recording_client()
        players.fetch_leaguedashplayerclutch(
            client=client, season="2025-26"
        )
        _, players_params = client.calls[0]
        assert len(players_params) == 38
        assert "TwoWay" not in players_params, (
            "Players variant must NOT include TwoWay "
            "(defining disambiguation fact)"
        )

    def test_lineups_variant_has_39_keys_and_includes_twoway(self, recording_client) -> None:
        """The Lineups variant has a 39-key surface and INCLUDES ``"TwoWay"``."""
        client = recording_client()
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client, season="2025-26"
        )
        _, lineups_params = client.calls[0]
        assert len(lineups_params) == 39
        assert "TwoWay" in lineups_params, (
            "Lineups variant MUST include TwoWay "
            "(defining disambiguation fact)"
        )
        assert lineups_params["TwoWay"] == "0"

    def test_keyset_delta_equals_exactly_twoway(self, recording_client) -> None:
        """The full keyset delta between the two variants equals exactly ``{"TwoWay"}``.

        This is the headline disambiguation invariant.  If any future
        refactor adds or removes keys from either variant without
        explicitly accounting for this test, the assertion will fail
        and the Decision Log must be updated.
        """
        client_players = recording_client()
        client_lineups = recording_client()
        players.fetch_leaguedashplayerclutch(
            client=client_players, season="2025-26"
        )
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client_lineups, season="2025-26"
        )
        _, players_params = client_players.calls[0]
        _, lineups_params = client_lineups.calls[0]
        players_keys = set(players_params.keys())
        lineups_keys = set(lineups_params.keys())
        # Lineups adds exactly TwoWay.
        assert lineups_keys - players_keys == {"TwoWay"}, (
            f"Expected Lineups to add exactly TwoWay, got "
            f"{lineups_keys - players_keys}"
        )
        # Players omits exactly nothing the Lineups variant has apart from TwoWay.
        assert players_keys - lineups_keys == set(), (
            f"Expected no Players-only keys, got "
            f"{players_keys - lineups_keys}"
        )
        # Symmetric delta is precisely {TwoWay}.
        assert (
            players_keys.symmetric_difference(lineups_keys) == {"TwoWay"}
        )

    def test_shared_clutch_triplet_values_identical(self, recording_client) -> None:
        """Beyond the delta key, both variants share identical default values for the clutch triplet.

        This verifies that the disambiguation is truly one-key-wide
        (``"TwoWay"``) and that the other clutch-defining params
        (``ClutchTime`` / ``AheadBehind`` / ``PointDiff``) match
        between variants.
        """
        client_players = recording_client()
        client_lineups = recording_client()
        players.fetch_leaguedashplayerclutch(
            client=client_players, season="2025-26"
        )
        lineups.fetch_leaguedashplayerclutch_onoff(
            client=client_lineups, season="2025-26"
        )
        _, players_params = client_players.calls[0]
        _, lineups_params = client_lineups.calls[0]
        for shared_key in ("ClutchTime", "AheadBehind", "PointDiff"):
            assert players_params[shared_key] == lineups_params[shared_key], (
                f"Shared clutch key {shared_key} diverged between variants: "
                f"players={players_params[shared_key]!r} vs "
                f"lineups={lineups_params[shared_key]!r}"
            )


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Cross-wrapper invariants that must hold regardless of which wrapper ran."""

    def test_module_does_not_import_requests(self) -> None:
        """Rule 1 — Single HTTP Client.

        ``endpoints.lineups`` must NEVER import :mod:`requests` or any
        transport-layer package; all HTTP goes through
        :meth:`api.nba_client.NBAClient.get`.  Verified by inspecting
        the module's ``__dict__`` — a live ``requests`` attribute
        would indicate a smuggled import.
        """
        assert not hasattr(lineups, "requests"), (
            "endpoints.lineups must not expose a `requests` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(lineups, "urllib"), (
            "endpoints.lineups must not expose a `urllib` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(lineups, "httpx"), (
            "endpoints.lineups must not expose an `httpx` attribute "
            "(Rule 1 — Single HTTP Client)"
        )

    def test_module_does_not_import_pandas(self) -> None:
        """Rule 4 (indirect) — endpoint wrappers must not construct DataFrames.

        Flattening belongs to :mod:`utils.schema_normalizer`; the
        wrapper layer returns raw JSON unchanged.
        """
        assert not hasattr(lineups, "pd"), (
            "endpoints.lineups must not expose `pd` (pandas)"
        )
        assert not hasattr(lineups, "pandas"), (
            "endpoints.lineups must not expose `pandas`"
        )

    def test_two_public_callables_exported(self) -> None:
        """Exactly two wrapper functions are exported as required by AAP §0.5.1.4."""
        for name in (
            "fetch_leaguedashlineups",
            "fetch_leaguedashplayerclutch_onoff",
        ):
            assert callable(getattr(lineups, name)), (
                f"endpoints.lineups.{name} must be a public callable"
            )

    def test_module_uses_module_level_logger(self) -> None:
        """A single module-level ``logger`` is attached for Observability."""
        assert hasattr(lineups, "logger"), (
            "endpoints.lineups must expose a module-level `logger`"
        )

    def test_logger_name_matches_module_path(self) -> None:
        """The module-level logger is named via ``__name__`` per F-008."""
        # ``get_logger`` returns a ``LoggerAdapter``; the underlying
        # logger is accessible via ``.logger`` on the adapter.
        adapter = lineups.logger
        # The attribute chain works for both :class:`logging.LoggerAdapter`
        # and the project's :class:`utils.correlation.CorrelationAdapter`.
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "endpoints.lineups"


# ---------------------------------------------------------------------------
# Parameter-level deep-coverage tests (parametric)
# ---------------------------------------------------------------------------


class TestParamDictShape:
    """Parametric coverage of param-dict invariants across both Lineups wrappers."""

    @pytest.mark.parametrize(
        ("func", "expected_endpoint"),
        [
            (
                lineups.fetch_leaguedashlineups,
                "leaguedashlineups",
            ),
            (
                lineups.fetch_leaguedashplayerclutch_onoff,
                # SAME upstream endpoint name as the Players variant:
                "leaguedashplayerclutch",
            ),
        ],
    )
    def test_every_wrapper_issues_exactly_one_call(
        self,
        recording_client,
        func,
        expected_endpoint,
    ) -> None:
        """Each wrapper issues exactly one ``client.get`` call.

        No extra enumeration, discovery, or secondary endpoint calls
        happen inside the wrapper layer — that belongs to the
        pipeline.
        """
        client = recording_client()
        func(client=client, season="2025-26")
        assert len(client.calls) == 1
        assert client.calls[0][0] == expected_endpoint

    @pytest.mark.parametrize(
        "func",
        [
            lineups.fetch_leaguedashlineups,
            lineups.fetch_leaguedashplayerclutch_onoff,
        ],
    )
    def test_every_wrapper_populates_season_verbatim(
        self,
        recording_client,
        func,
    ) -> None:
        """The ``season`` argument flows into ``params["Season"]`` verbatim."""
        client = recording_client()
        func(client=client, season="2024-25")
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"

    @pytest.mark.parametrize(
        "func",
        [
            lineups.fetch_leaguedashlineups,
            lineups.fetch_leaguedashplayerclutch_onoff,
        ],
    )
    def test_every_wrapper_passes_params_as_dict(
        self,
        recording_client,
        func,
    ) -> None:
        """The second positional arg to ``client.get`` is a ``dict``.

        :meth:`api.nba_client.NBAClient.get` validates that ``params``
        is a ``dict`` (not ``None``, not a list).  Asserting this at
        the wrapper layer fences off accidental regressions where a
        future refactor might forget to build the dict.
        """
        client = recording_client()
        func(client=client, season="2025-26")
        _, params = client.calls[0]
        assert isinstance(params, dict)

    @pytest.mark.parametrize(
        "func",
        [
            lineups.fetch_leaguedashlineups,
            lineups.fetch_leaguedashplayerclutch_onoff,
        ],
    )
    def test_every_param_value_is_a_string(
        self,
        recording_client,
        func,
    ) -> None:
        """All built-in param values are strings (no ints, no floats, no None).

        The NBA Stats API treats every query parameter as a string.
        Passing non-string values would require the transport layer
        to serialize them, introducing a Rule 4 edge-case risk
        (nested JSON values from stringified dicts).  Asserting
        string-only values at the wrapper layer rules out that entire
        class of bug.
        """
        client = recording_client()
        func(client=client, season="2025-26")
        _, params = client.calls[0]
        for key, value in params.items():
            assert isinstance(value, str), (
                f"Param {key!r} must be a string, got "
                f"{type(value).__name__}({value!r})"
            )
