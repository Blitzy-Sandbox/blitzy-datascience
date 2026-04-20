"""Unit tests for ``endpoints.schedule`` (Feature F-013).

Scope
-----

This module exhaustively tests the Schedule endpoint layer:

* :func:`endpoints.schedule.fetch_leaguegamefinder` — the single NBA Stats
  endpoint wrapper backing the Schedule data domain (AAP §0.5.1.4). The
  wrapper pins ``PlayerOrTeam="T"`` (team-level rows, one per team per
  game), accepts ``**kwargs`` overrides, and returns the raw JSON envelope
  from the upstream ``leaguegamefinder`` endpoint unmodified.
* :func:`endpoints.schedule.enumerate_game_ids` — the derived helper that
  walks the raw envelope, extracts and deduplicates ``GAME_ID`` values, and
  returns a first-seen-ordered ``List[str]`` for the Games pipeline
  (AAP §0.4.5 cross-domain dependency F-013 → F-011).

Coverage matrix
---------------

``fetch_leaguegamefinder``:

* Endpoint string ``"leaguegamefinder"``
* 16-key strict param surface (``Season``, ``SeasonType``, ``LeagueID``,
  ``PlayerOrTeam`` pinned to ``"T"``, plus 12 optional empty-string
  filters)
* ``LeagueID`` default flows from :data:`config.DEFAULT_LEAGUE_ID` (Gate 12)
* ``SeasonType`` default flows from :data:`config.DEFAULT_SEASON_TYPE`
* ``**kwargs`` override propagation
* Single HTTP call invariant
* Return-value identity passthrough

``enumerate_game_ids`` — 8 mandatory edge cases (AAP §0.4.5):

1. Happy path with duplicates (insertion-ordered dedup)
2. Empty ``resultSets`` list → ``[]`` + WARNING log
3. Missing ``resultSets`` key → ``[]`` + WARNING log
4. Non-dict entries skipped (defensive envelope walking)
5. No ``GAME_ID`` column anywhere → ``[]`` + WARNING log
6. Header-based table discovery (scan for first table with ``GAME_ID``
   in its headers, not by table name)
7. Row length validation (skip short rows, skip ``None`` values)
8. ``str()`` coercion for numeric ``GAME_ID`` values + insertion-ordered
   deduplication across types

Rule compliance
---------------

* **Rule 1 — Single HTTP Client**: verified by asserting the module
  does not expose ``requests``, ``urllib``, or ``httpx`` attributes.
* **Rule 4 (indirect)**: verified by asserting the module does not
  expose ``pandas``/``pd``; flattening belongs to
  :mod:`utils.schema_normalizer`.
* **Rule 7 — Pluggable Storage**: verified indirectly (no
  ``to_csv`` call site in endpoint modules); the wrapper returns
  raw JSON unmodified.

Testing strategy
----------------

Tests use the ``recording_client`` fixture factory from
:mod:`tests.conftest` which manufactures a :class:`RecordingClient`
handwritten spy (per conftest.py directive — no ``MagicMock``). Each
:meth:`RecordingClient.get` call records the ``(endpoint, params)``
tuple on ``client.calls`` so tests can inspect the exact parameter
dict the wrapper constructed.

The ``sample_schedule_payload`` fixture from ``conftest.py`` supplies
an NBA-Stats-shaped envelope containing 5 rows across 3 distinct
``GAME_ID`` values (two rows per game — one home, one away) so
``enumerate_game_ids`` can be verified against realistic deduplication
data.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import config
from endpoints import schedule


# ---------------------------------------------------------------------------
# Shared constants — expected ``leaguegamefinder`` param surface
# ---------------------------------------------------------------------------

#: Exact keyset produced by :func:`fetch_leaguegamefinder` (16 keys).
#:
#: Pinned per AAP §0.7.2.2 endpoint catalog entry for F-013. The
#: keyset includes 4 anchor keys (``Season``, ``SeasonType``,
#: ``LeagueID``, ``PlayerOrTeam``) plus 12 empty-string optional
#: filter keys. ``PlayerOrTeam`` is pinned to ``"T"`` by the wrapper
#: so the upstream emits team-level rows (the assumption behind
#: ``enumerate_game_ids`` deduplication).
EXPECTED_LEAGUEGAMEFINDER_KEYS = frozenset(
    (
        "Season",
        "SeasonType",
        "LeagueID",
        "PlayerOrTeam",
        "PlayerID",
        "TeamID",
        "Outcome",
        "Location",
        "DateFrom",
        "DateTo",
        "VsConference",
        "VsDivision",
        "Conference",
        "Division",
        "SeasonSegment",
        "GameID",
    )
)

#: The 12 empty-string default filter keys in the param surface.
EXPECTED_EMPTY_STRING_KEYS = frozenset(
    (
        "PlayerID",
        "TeamID",
        "Outcome",
        "Location",
        "DateFrom",
        "DateTo",
        "VsConference",
        "VsDivision",
        "Conference",
        "Division",
        "SeasonSegment",
        "GameID",
    )
)


# ---------------------------------------------------------------------------
# fetch_leaguegamefinder — wrapper contract tests
# ---------------------------------------------------------------------------


class TestFetchLeaguegamefinder:
    """Contract tests for :func:`endpoints.schedule.fetch_leaguegamefinder`."""

    def test_calls_correct_endpoint(self, recording_client) -> None:
        """Dispatches to upstream endpoint ``"leaguegamefinder"``."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        assert client.calls[0][0] == "leaguegamefinder"

    def test_params_contains_season_verbatim(self, recording_client) -> None:
        """The ``season`` argument flows unchanged into ``params['Season']``."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2024-25")
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"

    def test_defaults_applied_when_not_overridden(
        self, recording_client
    ) -> None:
        """Config-backed defaults propagate when the caller passes no overrides."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID
        assert params["PlayerOrTeam"] == "T"

    def test_league_id_default_flows_from_config(
        self, recording_client
    ) -> None:
        """Gate 12 — ``LeagueID`` default traces to ``config.DEFAULT_LEAGUE_ID``.

        This is the config-propagation-tracing assertion: verifying
        that the constant is read at call time (not hardcoded) confirms
        the AAP §0.7.5 Gate 12 invariant.
        """
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["LeagueID"] == config.DEFAULT_LEAGUE_ID

    def test_season_type_default_flows_from_config(
        self, recording_client
    ) -> None:
        """Gate 12 — ``SeasonType`` default traces to
        ``config.DEFAULT_SEASON_TYPE``."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["SeasonType"] == config.DEFAULT_SEASON_TYPE

    def test_player_or_team_pinned_to_T(self, recording_client) -> None:
        """``PlayerOrTeam`` is pinned to ``"T"`` regardless of defaults.

        Per AAP §0.5.1.4 and the module docstring, team-level rows are
        the Schedule pipeline convention — one row per team per game —
        which ``enumerate_game_ids`` relies on for its deduplication
        arithmetic (two rows per GAME_ID from distinct teams).
        """
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        assert params["PlayerOrTeam"] == "T"

    def test_empty_string_defaults(self, recording_client) -> None:
        """12 optional filter keys default to empty strings.

        The NBA Stats API returns HTTP 400 on entirely-missing optional
        fields but accepts empty strings as "no filter". The wrapper
        therefore populates all optional filters to ``""`` when not
        overridden.
        """
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        for key in EXPECTED_EMPTY_STRING_KEYS:
            assert params[key] == "", (
                f"expected params[{key!r}] == '' (default), "
                f"got {params[key]!r}"
            )

    def test_exact_16_key_surface(self, recording_client) -> None:
        """The param dict has exactly 16 keys — no more, no fewer."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        _, params = client.calls[0]
        assert set(params.keys()) == EXPECTED_LEAGUEGAMEFINDER_KEYS, (
            f"expected 16-key surface, got {sorted(params.keys())}"
        )
        assert len(params) == 16

    def test_kwargs_override_default(self, recording_client) -> None:
        """Overriding defaults via ``**kwargs`` propagates into params."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(
            client=client,
            season="2025-26",
            DateFrom="2025-10-01",
            DateTo="2025-10-31",
            TeamID="1610612747",
        )
        _, params = client.calls[0]
        assert params["DateFrom"] == "2025-10-01"
        assert params["DateTo"] == "2025-10-31"
        assert params["TeamID"] == "1610612747"

    def test_kwargs_add_novel_key(self, recording_client) -> None:
        """Novel ``**kwargs`` keys (not in defaults) appear in the param dict."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(
            client=client,
            season="2025-26",
            FutureFilterParam="xyz",
        )
        _, params = client.calls[0]
        assert params["FutureFilterParam"] == "xyz"

    def test_overriding_season_type(self, recording_client) -> None:
        """Explicit ``season_type`` overrides the default."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(
            client=client,
            season="2024-25",
            season_type="Playoffs",
        )
        _, params = client.calls[0]
        assert params["SeasonType"] == "Playoffs"

    def test_overriding_league_id(self, recording_client) -> None:
        """Explicit ``league_id`` overrides the default."""
        client = recording_client()
        schedule.fetch_leaguegamefinder(
            client=client,
            season="2024-25",
            league_id="10",  # WNBA — not in scope but accepted by upstream
        )
        _, params = client.calls[0]
        assert params["LeagueID"] == "10"

    def test_single_http_call_invariant(self, recording_client) -> None:
        """Exactly one ``client.get`` call per wrapper invocation.

        No enumeration, discovery, or secondary endpoint calls happen
        inside the wrapper — that belongs to the pipeline (F-011) or to
        :func:`enumerate_game_ids` (helper).
        """
        client = recording_client()
        schedule.fetch_leaguegamefinder(client=client, season="2025-26")
        assert len(client.calls) == 1

    def test_return_value_is_upstream_dict_identity(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """Return value is the identical dict object returned by ``client.get``.

        The wrapper must NOT copy, wrap, or transform the upstream
        payload; the result is returned by identity so downstream
        normalization sees the exact envelope structure emitted by
        the NBA Stats API.
        """
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        result = schedule.fetch_leaguegamefinder(
            client=client, season="2025-26"
        )
        assert result is sample_schedule_payload


# ---------------------------------------------------------------------------
# enumerate_game_ids — 8 mandatory edge cases (AAP §0.4.5)
# ---------------------------------------------------------------------------


class TestEnumerateGameIds:
    """Edge-case coverage for :func:`endpoints.schedule.enumerate_game_ids`.

    Tests are grouped by the 8 documented edge cases from AAP §0.4.5
    and the ``endpoints/schedule.py`` source commentary (lines 249-304).
    """

    # ---- Edge Case 1: Happy path with duplicates + dedup ----

    def test_happy_path_dedup_and_order_preservation(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """Deduplicates correctly while preserving first-seen ordering.

        The ``sample_schedule_payload`` fixture contains 5 rows across
        3 distinct GAME_IDs: ``0022500001`` (2 rows), ``0022500002``
        (2 rows), ``0022500003`` (1 row). The function must return
        exactly ``['0022500001', '0022500002', '0022500003']`` in that
        insertion order (CPython 3.7+ dict insertion-order guarantee).
        """
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500001", "0022500002", "0022500003"]
        assert len(result) == 3

    def test_returns_list_type(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """The return type is ``list`` — not a ``set``, ``tuple``, or generator."""
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert isinstance(result, list)

    def test_all_game_ids_are_strings(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """Every emitted GAME_ID is a ``str`` (not int, not None)."""
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        for gid in result:
            assert isinstance(gid, str), (
                f"expected str, got {type(gid).__name__}: {gid!r}"
            )

    # ---- Edge Case 2: Empty ``resultSets`` list ----

    def test_empty_result_sets_returns_empty_list(
        self, recording_client
    ) -> None:
        """``{"resultSets": []}`` returns ``[]`` without raising."""
        client = recording_client(
            responses={"leaguegamefinder": {"resultSets": []}}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []

    def test_empty_result_sets_emits_warning(
        self, recording_client, caplog
    ) -> None:
        """Empty ``resultSets`` triggers a WARNING log record."""
        client = recording_client(
            responses={"leaguegamefinder": {"resultSets": []}}
        )
        with caplog.at_level("WARNING", logger="endpoints.schedule"):
            schedule.enumerate_game_ids(client=client, season="2025-26")
        assert any(
            "empty payload" in record.message.lower()
            for record in caplog.records
        ), (
            f"expected WARNING mentioning 'empty payload', "
            f"got: {[r.message for r in caplog.records]}"
        )

    # ---- Edge Case 3: Missing ``resultSets`` key ----

    def test_missing_result_sets_key_returns_empty_list(
        self, recording_client
    ) -> None:
        """``{}`` (no ``resultSets`` key at all) returns ``[]`` defensively."""
        client = recording_client(
            responses={"leaguegamefinder": {}}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []

    def test_missing_result_sets_key_emits_warning(
        self, recording_client, caplog
    ) -> None:
        """Missing ``resultSets`` key triggers a WARNING log record.

        The source code uses ``payload.get("resultSets") or []`` which
        treats a missing key identically to an empty list — both should
        emit the same "empty payload" WARNING.
        """
        client = recording_client(
            responses={"leaguegamefinder": {}}
        )
        with caplog.at_level("WARNING", logger="endpoints.schedule"):
            schedule.enumerate_game_ids(client=client, season="2025-26")
        assert any(
            "empty payload" in record.message.lower()
            for record in caplog.records
        )

    def test_none_result_sets_returns_empty_list(
        self, recording_client
    ) -> None:
        """Explicit ``"resultSets": None`` returns ``[]`` defensively.

        The ``.get("resultSets") or []`` idiom treats ``None`` as falsy
        and substitutes the empty list.
        """
        client = recording_client(
            responses={"leaguegamefinder": {"resultSets": None}}
        )
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []

    # ---- Edge Case 4: Non-dict entries skipped ----

    def test_non_dict_entries_are_skipped(self, recording_client) -> None:
        """Non-dict entries in ``resultSets`` are silently skipped.

        Defensive walking: ``if not isinstance(entry, dict): continue``
        (source line 270). The function scans past the non-dict entries
        and finds the real dict containing ``GAME_ID``.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                "not a dict",
                None,
                123,
                [],
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [
                        ["0022500010", 1610612747],
                        ["0022500011", 1610612744],
                    ],
                },
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500010", "0022500011"]

    # ---- Edge Case 5: No ``GAME_ID`` column anywhere ----

    def test_no_game_id_column_returns_empty_list(
        self, recording_client
    ) -> None:
        """When no table has a ``GAME_ID`` column, returns ``[]``.

        Source line 277-282: if ``target_table is None`` after scanning
        all tables, the function emits a WARNING and returns ``[]``.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "SomeOtherTable",
                    "headers": ["SEASON_ID", "TEAM_ID"],
                    "rowSet": [
                        ["22025", 1610612747],
                        ["22025", 1610612744],
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []

    def test_no_game_id_column_emits_warning(
        self, recording_client, caplog
    ) -> None:
        """Missing ``GAME_ID`` column triggers a specific WARNING log record."""
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "SomeOtherTable",
                    "headers": ["SEASON_ID", "TEAM_ID"],
                    "rowSet": [["22025", 1610612747]],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        with caplog.at_level("WARNING", logger="endpoints.schedule"):
            schedule.enumerate_game_ids(client=client, season="2025-26")
        assert any(
            "no GAME_ID column" in record.message
            for record in caplog.records
        ), (
            f"expected WARNING mentioning 'no GAME_ID column', "
            f"got: {[r.message for r in caplog.records]}"
        )

    # ---- Edge Case 6: Header-based table discovery ----

    def test_header_based_table_discovery_skips_first_table(
        self, recording_client
    ) -> None:
        """Finds the first table whose headers list contains ``GAME_ID``.

        Source line 269-275: table discovery is header-based (not
        name-based) so the helper is resilient to upstream table
        renames and auxiliary tables preceding the GAME_ID table.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                # First table: no GAME_ID column — should be skipped.
                {
                    "name": "Available Seasons",
                    "headers": ["SEASON_ID", "LEAGUE"],
                    "rowSet": [["22025", "00"]],
                },
                # Second table: has GAME_ID — should be used.
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["SEASON_ID", "TEAM_ID", "GAME_ID"],
                    "rowSet": [
                        ["22025", 1610612747, "0022500005"],
                    ],
                },
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500005"]

    def test_header_based_discovery_uses_first_matching_table(
        self, recording_client
    ) -> None:
        """When multiple tables have ``GAME_ID``, the FIRST one wins.

        The loop breaks on first match (source line 275), so a later
        table with a different ``GAME_ID`` set is NOT consumed.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "TableA",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [["0022500010", 1610612747]],
                },
                {
                    "name": "TableB",
                    "headers": ["GAME_ID", "OTHER"],
                    "rowSet": [["9999999999", "ignored"]],
                },
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500010"]
        # Second table's GAME_ID does NOT appear in the result.
        assert "9999999999" not in result

    # ---- Edge Case 7: Row length validation + None skipping ----

    def test_short_rows_are_skipped(self, recording_client) -> None:
        """Rows shorter than ``game_id_index`` are skipped defensively.

        Source line 296: ``if not row or game_id_index >= len(row):
        continue``.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    # GAME_ID is at index 2 — rows shorter than 3 skip.
                    "headers": ["SEASON_ID", "TEAM_ID", "GAME_ID"],
                    "rowSet": [
                        ["22025", 1610612747, "0022500001"],
                        # Short rows — should skip.
                        ["22025"],
                        ["22025", 1610612744],
                        # Empty row — should skip.
                        [],
                        ["22025", 1610612738, "0022500002"],
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500001", "0022500002"]

    def test_none_game_id_values_are_skipped(self, recording_client) -> None:
        """Rows where ``GAME_ID`` is ``None`` are skipped.

        Source line 299-300: ``if game_id is None: continue``. Prevents
        ``None`` from being coerced to the string ``"None"`` in the
        output list.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [
                        ["0022500001", 1610612747],
                        [None, 1610612744],
                        ["0022500002", 1610612738],
                        [None, 1610612739],
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["0022500001", "0022500002"]
        assert "None" not in result
        assert None not in result

    # ---- Edge Case 8: str() coercion + insertion-ordered dedup ----

    def test_numeric_game_ids_are_coerced_to_strings(
        self, recording_client
    ) -> None:
        """Numeric ``GAME_ID`` values are coerced via ``str()``.

        Source line 301: ``key = str(game_id)``. Some upstream envelopes
        return numeric types for IDs that look numeric; downstream
        box-score calls expect the string form.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [
                        [22500001, 1610612747],  # int
                        [22500001, 1610612744],  # int dup
                        [22500002, 1610612738],  # int distinct
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["22500001", "22500002"]
        for gid in result:
            assert isinstance(gid, str)

    def test_insertion_ordered_dedup_preserves_first_occurrence(
        self, recording_client
    ) -> None:
        """Dedup preserves FIRST occurrence (not numeric sort).

        This is the Gate 8 resume-determinism invariant. The dict-based
        seen-set (source line 294) yields insertion-ordered iteration
        via the CPython 3.7+ dict-order language guarantee.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [
                        ["0022500005", 1610612747],  # First to appear
                        ["0022500001", 1610612744],  # Then 001
                        ["0022500005", 1610612738],  # Dup of 005
                        ["0022500003", 1610612739],  # Then 003
                        ["0022500001", 1610612740],  # Dup of 001
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        # First-occurrence order — NOT numeric sort.
        assert result == ["0022500005", "0022500001", "0022500003"]

    def test_dedup_across_mixed_types_coerces_then_dedupes(
        self, recording_client
    ) -> None:
        """Rows with ``int`` and ``str`` forms of the same GAME_ID dedup correctly.

        After ``str()`` coercion, ``22500001`` (int) and ``"22500001"``
        (str) produce the same key and are deduplicated.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [
                        [22500001, 1610612747],      # int form
                        ["22500001", 1610612744],    # str form of same
                        ["22500002", 1610612738],    # distinct
                    ],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == ["22500001", "22500002"]

    # ---- Additional enumerate_game_ids behavioral tests ----

    def test_emits_info_log_with_game_count(
        self, recording_client, sample_schedule_payload, caplog
    ) -> None:
        """On successful enumeration, emits an INFO log with game count.

        Source line 306-310: the function emits an INFO record
        summarizing how many unique ``GAME_ID`` values it found.
        """
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        with caplog.at_level("INFO", logger="endpoints.schedule"):
            result = schedule.enumerate_game_ids(
                client=client, season="2025-26"
            )
        # The INFO log line references the game count.
        found_count_log = any(
            "game_count" in record.message
            and str(len(result)) in record.message
            for record in caplog.records
        )
        assert found_count_log, (
            f"expected INFO log with game_count={len(result)}, "
            f"got: {[r.message for r in caplog.records]}"
        )

    def test_forwards_kwargs_to_leaguegamefinder(
        self, recording_client
    ) -> None:
        """``**kwargs`` are forwarded to the underlying ``leaguegamefinder`` call.

        AAP §0.5.1.4 explicitly specifies that ``enumerate_game_ids``
        accepts ``**kwargs`` and passes them through to the upstream
        filter surface.
        """
        client = recording_client()
        schedule.enumerate_game_ids(
            client=client,
            season="2025-26",
            TeamID="1610612747",
            DateFrom="2025-11-01",
        )
        _, params = client.calls[0]
        assert params["TeamID"] == "1610612747"
        assert params["DateFrom"] == "2025-11-01"

    def test_forwards_season_type_kwarg(self, recording_client) -> None:
        """Explicit ``season_type`` is forwarded to ``leaguegamefinder``."""
        client = recording_client()
        schedule.enumerate_game_ids(
            client=client, season="2024-25", season_type="Playoffs"
        )
        _, params = client.calls[0]
        assert params["SeasonType"] == "Playoffs"

    def test_forwards_league_id_kwarg(self, recording_client) -> None:
        """Explicit ``league_id`` is forwarded to ``leaguegamefinder``."""
        client = recording_client()
        schedule.enumerate_game_ids(
            client=client, season="2024-25", league_id="20"
        )
        _, params = client.calls[0]
        assert params["LeagueID"] == "20"

    def test_enumerate_single_http_call(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """``enumerate_game_ids`` issues exactly ONE HTTP call.

        The helper must not perform secondary enumeration — all the
        information it needs is in the single ``leaguegamefinder``
        envelope.
        """
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        schedule.enumerate_game_ids(client=client, season="2025-26")
        assert len(client.calls) == 1

    def test_enumerate_calls_correct_endpoint(
        self, recording_client, sample_schedule_payload
    ) -> None:
        """``enumerate_game_ids`` delegates to the ``leaguegamefinder`` endpoint."""
        client = recording_client(
            responses={"leaguegamefinder": sample_schedule_payload}
        )
        schedule.enumerate_game_ids(client=client, season="2025-26")
        assert client.calls[0][0] == "leaguegamefinder"

    def test_enumerate_empty_rowset_returns_empty_list(
        self, recording_client
    ) -> None:
        """A table with ``GAME_ID`` header but empty ``rowSet`` returns ``[]``."""
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    "rowSet": [],
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []

    def test_enumerate_no_rowset_key_returns_empty_list(
        self, recording_client
    ) -> None:
        """Missing ``rowSet`` key on the target table returns ``[]``.

        Source line 285: ``rows = list(target_table.get("rowSet") or [])``.
        """
        payload: Dict[str, Any] = {
            "resultSets": [
                {
                    "name": "LeagueGameFinderResults",
                    "headers": ["GAME_ID", "TEAM_ID"],
                    # No rowSet key at all.
                }
            ]
        }
        client = recording_client(responses={"leaguegamefinder": payload})
        result = schedule.enumerate_game_ids(
            client=client, season="2025-26"
        )
        assert result == []


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Cross-wrapper invariants that must hold for the schedule module."""

    def test_module_does_not_import_requests(self) -> None:
        """Rule 1 — Single HTTP Client.

        ``endpoints.schedule`` must NEVER import :mod:`requests` or any
        transport-layer package; all HTTP goes through ``NBAClient.get``.
        """
        assert not hasattr(schedule, "requests"), (
            "endpoints.schedule must not expose a `requests` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(schedule, "urllib"), (
            "endpoints.schedule must not expose a `urllib` attribute "
            "(Rule 1 — Single HTTP Client)"
        )
        assert not hasattr(schedule, "httpx"), (
            "endpoints.schedule must not expose an `httpx` attribute "
            "(Rule 1 — Single HTTP Client)"
        )

    def test_module_does_not_import_pandas(self) -> None:
        """Rule 4 (indirect) — endpoint wrappers must not construct DataFrames.

        Flattening belongs to :mod:`utils.schema_normalizer`; the
        wrapper layer returns raw JSON unchanged.
        """
        assert not hasattr(schedule, "pd"), (
            "endpoints.schedule must not expose `pd` (pandas)"
        )
        assert not hasattr(schedule, "pandas"), (
            "endpoints.schedule must not expose `pandas`"
        )

    def test_two_public_callables_exported(self) -> None:
        """Exactly one wrapper and one helper are exported per AAP §0.5.1.4.

        ``fetch_leaguegamefinder`` is the domain wrapper; ``enumerate_game_ids``
        is the cross-domain helper used by ``pipelines.ingest_games`` (F-011).
        """
        for name in (
            "fetch_leaguegamefinder",
            "enumerate_game_ids",
        ):
            assert callable(getattr(schedule, name)), (
                f"endpoints.schedule.{name} must be a public callable"
            )

    def test_module_uses_module_level_logger(self) -> None:
        """A single module-level ``logger`` is attached for Observability."""
        assert hasattr(schedule, "logger"), (
            "endpoints.schedule must expose a module-level `logger`"
        )

    def test_logger_name_matches_module_path(self) -> None:
        """The module-level logger is named via ``__name__`` per F-008."""
        adapter = schedule.logger
        # The adapter wraps a ``logging.Logger`` whose name is the
        # module's dotted path.
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "endpoints.schedule"


# ---------------------------------------------------------------------------
# Parameter-level deep-coverage tests (parametric)
# ---------------------------------------------------------------------------


class TestParamDictShape:
    """Parametric coverage of param-dict invariants across callables."""

    @pytest.mark.parametrize(
        ("func", "expected_endpoint"),
        [
            (schedule.fetch_leaguegamefinder, "leaguegamefinder"),
            (schedule.enumerate_game_ids, "leaguegamefinder"),
        ],
        ids=["fetch_leaguegamefinder", "enumerate_game_ids"],
    )
    def test_every_callable_issues_at_least_one_leaguegamefinder_call(
        self,
        recording_client,
        func,
        expected_endpoint,
    ) -> None:
        """Both the wrapper and the helper delegate through ``leaguegamefinder``.

        ``fetch_leaguegamefinder`` issues exactly one ``client.get``
        call; ``enumerate_game_ids`` composes on top of
        ``fetch_leaguegamefinder`` and thus also produces exactly one
        ``leaguegamefinder`` call. Either way, the first recorded call
        must be to the ``leaguegamefinder`` endpoint.
        """
        client = recording_client()
        func(client=client, season="2025-26")
        assert len(client.calls) == 1
        assert client.calls[0][0] == expected_endpoint

    @pytest.mark.parametrize(
        "func",
        [
            schedule.fetch_leaguegamefinder,
            schedule.enumerate_game_ids,
        ],
        ids=["fetch_leaguegamefinder", "enumerate_game_ids"],
    )
    def test_every_callable_populates_season_verbatim(
        self,
        recording_client,
        func,
    ) -> None:
        """The ``season`` argument flows into ``params["Season"]`` verbatim.

        The wrapper passes it directly; the helper passes it through
        ``fetch_leaguegamefinder``.
        """
        client = recording_client()
        func(client=client, season="2024-25")
        _, params = client.calls[0]
        assert params["Season"] == "2024-25"

    @pytest.mark.parametrize(
        "func",
        [
            schedule.fetch_leaguegamefinder,
            schedule.enumerate_game_ids,
        ],
        ids=["fetch_leaguegamefinder", "enumerate_game_ids"],
    )
    def test_every_callable_passes_params_as_dict(
        self,
        recording_client,
        func,
    ) -> None:
        """Both callables pass a ``dict`` (not None, not other types) to ``get``.

        The :class:`api.nba_client.NBAClient` ``get`` contract requires
        a ``dict`` for params; passing None is a ``TypeError`` upstream.
        """
        client = recording_client()
        func(client=client, season="2025-26")
        _, params = client.calls[0]
        assert isinstance(params, dict)

    @pytest.mark.parametrize(
        "func",
        [
            schedule.fetch_leaguegamefinder,
            schedule.enumerate_game_ids,
        ],
        ids=["fetch_leaguegamefinder", "enumerate_game_ids"],
    )
    def test_every_param_value_is_a_string(
        self,
        recording_client,
        func,
    ) -> None:
        """All param values are strings.

        The NBA Stats API expects string-valued query parameters
        (``urllib.parse.urlencode`` serializes them that way regardless,
        but pinning the wrapper to only emit strings keeps the spy
        assertion deterministic and documents the upstream contract).
        """
        client = recording_client()
        func(client=client, season="2025-26")
        _, params = client.calls[0]
        non_string_keys: List[str] = [
            key for key, value in params.items() if not isinstance(value, str)
        ]
        assert not non_string_keys, (
            f"expected all param values to be str; "
            f"non-string keys: {non_string_keys}"
        )
