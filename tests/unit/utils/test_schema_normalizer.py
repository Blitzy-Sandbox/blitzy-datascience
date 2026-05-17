"""Unit tests for ``utils.schema_normalizer``.

Verifies the ``resultSets`` envelope flattener that satisfies **Rule 4 —
Flat CSV Output (no nested JSON)** and Feature F-005. The normalizer is
a pure function consumed by every pipeline (``ingest_players``,
``ingest_teams``, ``ingest_games``, ``ingest_lineups``,
``ingest_schedule``) to transform NBA Stats API payloads into flat
:class:`pandas.DataFrame` objects written to disk by
``storage/csv_writer.py`` (the sole caller of ``DataFrame.to_csv`` per
Rule 7).

Test-file contract highlights
-----------------------------
* Every successful :func:`utils.schema_normalizer.normalize_result_sets`
  return is a ``Dict[str, pandas.DataFrame]`` keyed by the snake_case
  form of the originating ``resultSets[*].name``. The first occurrence
  of a duplicate name keeps the bare snake_case form; subsequent
  occurrences receive ``_2``, ``_3``, ... suffixes (numeric uniquifier
  starting at two, never at one).
* Empty ``rowSet`` arrays MUST produce a zero-row DataFrame with the
  originating headers preserved as columns. The Rule 4 flatness check
  is skipped for empty frames (there are no cells to inspect).
* Any cell holding a ``dict`` or ``list`` value MUST raise
  :class:`ValueError` containing the substring ``"Rule 4"`` together
  with the snake_case name of the offending set so operator stack
  traces self-document the violation.
* Row length must match header length or a :class:`ValueError` naming
  the offending set (snake_case) and referencing the failing row index
  is raised by the :func:`utils.schema_normalizer._build_dataframe`
  helper.
* Missing both ``resultSets`` and ``resultSet`` keys MUST raise
  :class:`ValueError` mentioning both key names so operators can
  trivially identify the upstream-contract violation.
* Non-dict payloads MUST raise :class:`TypeError` (distinguishing
  programmer errors from data errors, which are :class:`ValueError`).
* The private :func:`utils.schema_normalizer._snake_case` helper is
  exercised directly via the aliased module reference ``sn_module`` to
  lock in the two-regex CamelCase / SCREAMING_SNAKE_CASE contract
  (``_CAMEL_BOUNDARY_1`` + ``_CAMEL_BOUNDARY_2`` followed by
  ``.lower().strip("_")``).
* The normalizer does NOT deduplicate rows. Deduplication of
  ``GAME_ID`` lists is the sole responsibility of
  ``endpoints/schedule.enumerate_game_ids``. The schedule-payload test
  in Phase 2.11 locks in that contract boundary explicitly.
* All tests are network-free, filesystem-neutral, and rely only on
  shared :mod:`tests.conftest` fixtures and inline-constructed payload
  dicts. No ``requests``, no ``.to_csv(``, no third-party mocking.

Authoritative references
------------------------
* AAP §0.1.3 — Rule 4 binding constraint (no dict/list cells in CSV).
* AAP §0.5.1.2 Group 2 — ``utils/schema_normalizer.py`` contract and
  the two-regex snake_case helper.
* AAP §0.7.2.4 — Rule 4 verification mechanism and scope.
* AAP §0.7.5 — Rule-to-Gate traceability (Rule 4 verified here + in
  ``tests/invariants/test_rule4_no_nested_cells.py``).
* Peer test file ``tests/unit/utils/__init__.py`` — enumerates the
  invariants this module covers (Rule 4 flat-cells post-condition,
  resultSets/resultSet flattening, snake_case naming, duplicate-name
  uniquification).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import pytest

from utils import schema_normalizer as sn_module
from utils.schema_normalizer import normalize_result_sets


# ---------------------------------------------------------------------------
# Phase 2.1 — Happy path: single table
# ---------------------------------------------------------------------------
#
# The canonical one-table ``leaguedashplayerstats`` envelope drives four
# structural assertions:
#   1. The returned dict has exactly one key, and that key is the
#      snake_case form of the original ``"LeagueDashPlayerStats"`` name.
#   2. Column order is preserved verbatim from the payload ``headers``
#      array (no reordering, no renaming).
#   3. Row count and per-cell values are faithfully propagated from the
#      ``rowSet`` list-of-lists (no rows dropped, no values mutated).
#   4. Every returned value is a real :class:`pandas.DataFrame` — not a
#      :class:`numpy.ndarray`, :class:`dict`, or any other container.
#
# These four tests together establish that the "happy path" output
# contract is byte-for-byte honored by the normalizer so downstream
# ``CSVWriter.write`` can assume it receives a faithful DataFrame.
# ---------------------------------------------------------------------------


def test_single_table_returns_snake_case_name(sample_single_table_payload):
    """``normalize_result_sets`` returns a dict keyed by snake_case name."""
    out = normalize_result_sets(sample_single_table_payload)
    assert list(out.keys()) == ["league_dash_player_stats"]


def test_single_table_preserves_column_order(sample_single_table_payload):
    """Column order in the returned DataFrame matches the payload headers."""
    out = normalize_result_sets(sample_single_table_payload)
    df = out["league_dash_player_stats"]
    assert list(df.columns) == ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "PTS"]


def test_single_table_preserves_row_count_and_values(sample_single_table_payload):
    """Row count and per-cell values are faithfully preserved from the payload rowSet.

    The ``sample_single_table_payload`` fixture carries three real 2025-26
    players (Jokić, Dončić, Tatum); assertions are pinned to those exact
    values to catch any silent truncation or reordering by the flattener.
    """
    out = normalize_result_sets(sample_single_table_payload)
    df = out["league_dash_player_stats"]
    assert len(df) == 3
    assert tuple(df.iloc[0]) == (203999, "Nikola Jokić", 1610612743, 29.6)
    assert tuple(df.iloc[1]) == (1629029, "Luka Dončić", 1610612742, 32.4)
    assert tuple(df.iloc[2]) == (1628369, "Jayson Tatum", 1610612738, 26.9)


def test_single_table_returns_pandas_dataframe(sample_single_table_payload):
    """All values in the returned dict are :class:`pandas.DataFrame` instances."""
    out = normalize_result_sets(sample_single_table_payload)
    assert len(out) >= 1
    for name, df in out.items():
        assert isinstance(df, pd.DataFrame), f"{name} is not a DataFrame (got {type(df).__name__})"


# ---------------------------------------------------------------------------
# Phase 2.2 — Multi-table payload
# ---------------------------------------------------------------------------
#
# A ``boxscoretraditionalv2`` response carries two tables
# (``PlayerStats`` and ``TeamStats``); each must become an independent
# entry in the returned dict with its own header-derived columns and
# row count. This is the core pattern used by F-011 ``ingest_games``
# which persists both box-score tables into separate CSV artifacts.
# ---------------------------------------------------------------------------


def test_multi_table_returns_two_keys(sample_multi_table_payload):
    """Multi-table payload produces one dict entry per table with snake_case keys."""
    out = normalize_result_sets(sample_multi_table_payload)
    assert set(out.keys()) == {"player_stats", "team_stats"}


def test_multi_table_each_has_correct_shape(sample_multi_table_payload):
    """Each table in a multi-table payload is independently flattened with its own headers.

    Both tables share two rows by construction but have different header
    arrays; the test pins both the row count and the column list so any
    accidental cross-table bleed-over is caught immediately.
    """
    out = normalize_result_sets(sample_multi_table_payload)
    player_df = out["player_stats"]
    team_df = out["team_stats"]
    assert len(player_df) == 2
    assert list(player_df.columns) == ["GAME_ID", "PLAYER_ID", "TEAM_ID", "PTS"]
    assert len(team_df) == 2
    assert list(team_df.columns) == ["GAME_ID", "TEAM_ID", "PTS"]


# ---------------------------------------------------------------------------
# Phase 2.3 — Empty rowSet
# ---------------------------------------------------------------------------
#
# The NBA Stats API returns an empty ``rowSet`` for endpoints that have
# no data for the requested season (e.g., a fresh pre-season pull
# before any games are played). The normalizer must NOT raise in that
# case; it must emit a zero-row DataFrame with the declared columns
# preserved so ``CSVWriter.write`` can write a header-only CSV rather
# than failing the pipeline. Rule 4 flatness is irrelevant for a frame
# with no cells, so the check MUST be skipped (early return inside
# :func:`utils.schema_normalizer._assert_rule4_flat`).
# ---------------------------------------------------------------------------


def test_empty_rowset_returns_empty_dataframe_with_headers(sample_empty_payload):
    """Empty rowSet yields a zero-row DataFrame with columns still preserved."""
    out = normalize_result_sets(sample_empty_payload)
    df = out["league_dash_player_stats"]
    assert len(df) == 0
    assert df.empty
    assert list(df.columns) == ["PLAYER_ID", "PTS"]


def test_empty_rowset_does_not_trigger_rule4_check(sample_empty_payload):
    """Rule 4 flatness check is skipped when the DataFrame is empty (no cells to inspect).

    Calling ``normalize_result_sets`` on an empty-rowset payload must not
    raise. This guarantees the flatness assertion uses the documented
    ``if df.empty: return`` early-return path rather than invoking
    :meth:`pandas.DataFrame.map` on a zero-row frame (which would be a
    no-op but would still pay the inspection cost).
    """
    # Any raise would fail the test immediately; call is sufficient.
    out = normalize_result_sets(sample_empty_payload)
    assert "league_dash_player_stats" in out


# ---------------------------------------------------------------------------
# Phase 2.4 — Singular ``resultSet`` key
# ---------------------------------------------------------------------------
#
# Some NBA Stats endpoints use ``resultSet`` (singular) instead of the
# plural ``resultSets``. The normalizer must handle both the
# dict-valued and list-valued forms of the singular key equivalently so
# the call-site contract in every pipeline does not need to branch on
# envelope shape.
# ---------------------------------------------------------------------------


def test_result_set_singular_key_handled_equivalently(sample_result_set_singular_payload):
    """Singular ``resultSet`` key as a dict is handled identically to plural ``resultSets``.

    The ``sample_result_set_singular_payload`` fixture carries a
    ``playercareerstats`` envelope with a dict-valued singular
    ``resultSet``. The normalizer must treat the single table as if it
    were a one-element ``resultSets`` list.
    """
    out = normalize_result_sets(sample_result_set_singular_payload)
    assert list(out.keys()) == ["season_totals_regular_season"]
    df = out["season_totals_regular_season"]
    assert len(df) == 2
    assert list(df["PTS"]) == [1700, 1800]


def test_result_set_singular_as_list_also_handled():
    """Singular ``resultSet`` key holding a LIST of tables is also valid."""
    payload: Dict[str, Any] = {
        "resultSet": [
            {"name": "OneTable", "headers": ["A"], "rowSet": [[1]]},
        ],
    }
    out = normalize_result_sets(payload)
    assert list(out.keys()) == ["one_table"]
    assert len(out["one_table"]) == 1
    assert out["one_table"].iloc[0, 0] == 1


# ---------------------------------------------------------------------------
# Phase 2.5 — Duplicate table names
# ---------------------------------------------------------------------------
#
# When an upstream envelope contains multiple tables with the same
# ``name``, the normalizer must uniquify keys so no entry silently
# overwrites another. The production contract (see
# ``utils/schema_normalizer.py``) tracks seen names in a dict whose
# counter STARTS AT ONE; the first duplicate therefore gets ``_2``,
# the second ``_3``, etc. The bare snake_case form is reserved for the
# FIRST occurrence — never for any duplicate.
# ---------------------------------------------------------------------------


def test_duplicate_table_names_are_uniquified_with_numeric_suffix():
    """Duplicate table names receive ``_2``, ``_3`` suffixes; first occurrence keeps bare name.

    The first occurrence of ``"X"`` yields the bare snake_case key ``"x"``.
    The second and third occurrences yield ``"x_2"`` and ``"x_3"``
    respectively, preserving insertion order in the returned dict.
    This test pins the exact suffix schema so any drift (e.g., starting
    at ``_1``, using dashes, renumbering the first occurrence) is caught.
    """
    payload: Dict[str, Any] = {
        "resultSets": [
            {"name": "X", "headers": ["A"], "rowSet": [[1]]},
            {"name": "X", "headers": ["A"], "rowSet": [[2]]},
            {"name": "X", "headers": ["A"], "rowSet": [[3]]},
        ],
    }
    out = normalize_result_sets(payload)
    assert list(out.keys()) == ["x", "x_2", "x_3"]
    assert out["x"].iloc[0, 0] == 1
    assert out["x_2"].iloc[0, 0] == 2
    assert out["x_3"].iloc[0, 0] == 3


# ---------------------------------------------------------------------------
# Phase 2.6 — Row / header length mismatch
# ---------------------------------------------------------------------------
#
# The NBA Stats ``rowSet`` array-of-arrays contract demands every row
# carry exactly as many values as the ``headers`` array declares. Any
# deviation indicates upstream corruption and must raise
# :class:`ValueError` with a message that names the offending set
# (snake_case) and the row index so operators can immediately locate
# the corrupt record.
# ---------------------------------------------------------------------------


def test_row_header_length_mismatch_raises_valueerror_naming_set(sample_row_mismatch_payload):
    """Row length mismatch raises :class:`ValueError` identifying the offending set by snake_case name.

    The ``sample_row_mismatch_payload`` fixture is named ``"BadShape"``
    (CamelCase), which the normalizer converts to ``"bad_shape"`` before
    embedding in the error message. The snake_case form (not the raw
    CamelCase) must appear in the message.
    """
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(sample_row_mismatch_payload)
    assert "bad_shape" in str(exc_info.value)


def test_row_header_length_mismatch_message_references_row_index(sample_row_mismatch_payload):
    """Row mismatch error message references the failing row's index for debugging.

    The exact wording may vary across minor refactors, so the assertion
    is lenient: either the word ``"row"`` (case-insensitive) or the
    word ``"index"`` must appear. The production message currently uses
    the form ``"... row 0 has 2 values but 3 headers are declared"``.
    """
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(sample_row_mismatch_payload)
    message = str(exc_info.value).lower()
    assert "row" in message or "index" in message


# ---------------------------------------------------------------------------
# Phase 2.7 — Rule 4 violation detection
# ---------------------------------------------------------------------------
#
# Rule 4 forbids ``dict`` and ``list`` cells in the flattened
# DataFrame because writing them to CSV would smuggle JSON fragments
# into CSV cells — which is precisely the upstream envelope shape that
# the normalizer exists to eliminate. Both dict-valued and list-valued
# cells must be caught by the same code path, and the error message
# must include both the literal ``"Rule 4"`` substring (for operator
# recognition) and the snake_case name of the offending set (for
# pinpoint debugging). The invariant counterpart in
# ``tests/invariants/test_rule4_no_nested_cells.py`` provides a
# grep-style cross-check on production code.
# ---------------------------------------------------------------------------


def test_nested_dict_cell_raises_valueerror_with_rule4_substring(sample_nested_violation_payload):
    """Dict-valued cells raise :class:`ValueError` containing the ``"Rule 4"`` substring."""
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(sample_nested_violation_payload)
    assert "Rule 4" in str(exc_info.value)


def test_nested_list_cell_raises_valueerror_with_rule4_substring():
    """List-valued cells trigger the same Rule 4 violation path as dict-valued cells.

    The Rule 4 flatness check uses :meth:`pandas.DataFrame.map` (or
    :meth:`~pandas.DataFrame.applymap` on pandas 2.0.x) with the
    predicate ``isinstance(x, (dict, list))``. Both branches of the
    union must be covered by tests; this case exercises the list branch.
    """
    payload: Dict[str, Any] = {
        "resultSets": [
            {"name": "BadList", "headers": ["ID", "TAGS"], "rowSet": [[1, ["a", "b"]]]},
        ],
    }
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(payload)
    assert "Rule 4" in str(exc_info.value)


def test_rule4_error_message_names_offending_set():
    """Rule 4 error message contains the snake_case name of the offending set for debugging."""
    payload: Dict[str, Any] = {
        "resultSets": [
            {"name": "SpecificSetName", "headers": ["X"], "rowSet": [[{"bad": 1}]]},
        ],
    }
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(payload)
    message = str(exc_info.value)
    assert "Rule 4" in message
    assert "specific_set_name" in message


# ---------------------------------------------------------------------------
# Phase 2.8 — Missing ``resultSets`` / ``resultSet`` keys
# ---------------------------------------------------------------------------
#
# A payload that carries NEITHER key is a contract violation on the
# upstream side (NBA Stats never omits both). The normalizer must
# raise :class:`ValueError` with both key names in the message so the
# operator can immediately correlate the failure with the envelope
# shape they were expecting. An empty dict ``{}`` and an explicit
# ``{"resultSets": []}`` are both considered "no tables" and take the
# same error path.
# ---------------------------------------------------------------------------


def test_missing_resultsets_and_resultset_raises_valueerror_mentioning_both_keys(
    sample_missing_resultsets_payload,
):
    """Payload lacking both ``resultSets`` and ``resultSet`` keys raises a descriptive ValueError.

    The ``sample_missing_resultsets_payload`` fixture is a
    ``{"resource": "broken_upstream", "parameters": {}}`` dict — a
    realistic simulation of an upstream response that dropped the
    envelope entirely. Both key names must appear in the error message.
    """
    with pytest.raises(ValueError) as exc_info:
        normalize_result_sets(sample_missing_resultsets_payload)
    message = str(exc_info.value)
    assert "resultSets" in message
    assert "resultSet" in message


def test_empty_dict_raises_valueerror():
    """An empty dict ``{}`` has neither key and raises :class:`ValueError`."""
    with pytest.raises(ValueError):
        normalize_result_sets({})


def test_resultsets_empty_list_raises_valueerror():
    """An empty ``resultSets`` list is treated as 'no tables' and raises :class:`ValueError`.

    The semantic ``{"resultSets": []}`` is indistinguishable from
    'envelope present but empty' — both mean there is nothing to
    flatten. Raising ValueError ensures the caller cannot silently
    receive an empty dict and skip the expected CSV emission.
    """
    with pytest.raises(ValueError):
        normalize_result_sets({"resultSets": []})


# ---------------------------------------------------------------------------
# Phase 2.9 — Non-dict payload type
# ---------------------------------------------------------------------------
#
# A non-dict payload is a PROGRAMMER error (wrong object type passed
# into the function) — as distinct from a DATA error (malformed payload
# content). The normalizer must surface this distinction by raising
# :class:`TypeError` rather than :class:`ValueError` so callers can
# programmatically differentiate the two failure classes.
# ---------------------------------------------------------------------------


def test_non_dict_payload_raises_typeerror():
    """Non-dict payloads (None, str, int, list, tuple, object) raise :class:`TypeError`.

    This distinguishes programmer errors (wrong payload type) from data
    errors (malformed payload content, which is :class:`ValueError`).
    The enumerated bad values cover the common accidental-pass cases:
    ``None`` (forgotten return statement), strings and ints (wrong
    variable passed), lists and tuples (JSON array instead of object),
    and a bare ``object()`` (catch-all).
    """
    bad_payloads: List[Any] = [None, "not a dict", 42, [], (), object()]
    for bad in bad_payloads:
        with pytest.raises(TypeError):
            normalize_result_sets(bad)


# ---------------------------------------------------------------------------
# Phase 2.10 — ``_snake_case`` helper examples
# ---------------------------------------------------------------------------
#
# The :func:`utils.schema_normalizer._snake_case` helper is the
# single source of truth for converting NBA Stats' mixed CamelCase and
# SCREAMING_SNAKE_CASE table names into the snake_case keys used by
# every pipeline. Because the helper is underscore-prefixed (i.e.,
# not part of the formal public API), it is accessed through the
# aliased module reference ``sn_module`` rather than a direct
# ``from ... import _snake_case`` statement.
#
# The five tests below lock in the two-regex contract:
#   * ``_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")``
#   * ``_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")``
# followed by ``.lower().strip("_")``.
# ---------------------------------------------------------------------------


def test_snake_case_camel_case_examples():
    """CamelCase identifiers are converted to snake_case using the two-regex contract."""
    cases = [
        ("LeagueDashPlayerStats", "league_dash_player_stats"),
        ("PlayByPlay", "play_by_play"),
        ("PlayerStats", "player_stats"),
        ("TeamStats", "team_stats"),
        ("LeagueGameFinderResults", "league_game_finder_results"),
        ("SeasonTotalsRegularSeason", "season_totals_regular_season"),
    ]
    for camel, expected in cases:
        assert sn_module._snake_case(camel) == expected, (
            f"_snake_case({camel!r}) returned unexpected value"
        )


def test_snake_case_screaming_snake_case_lowercases():
    """SCREAMING_SNAKE_CASE inputs are lowercased without mangling the embedded underscores.

    ``"GAME_ID"`` is the canonical column name used in every game-level
    CSV artifact; the snake_case form ``"game_id"`` is used as the
    composite-key column throughout ``games.csv`` and
    ``play_by_play.csv``.
    """
    assert sn_module._snake_case("GAME_ID") == "game_id"


def test_snake_case_already_snake_case_unchanged():
    """Already-snake_case inputs are idempotent under the conversion.

    Idempotency matters because some NBA Stats endpoints emit table
    names that are already snake_case. Applying the helper a second
    time (e.g., from a re-normalized cache) must not corrupt the key.
    """
    assert sn_module._snake_case("play_by_play") == "play_by_play"


def test_snake_case_strips_leading_trailing_underscores():
    """Leading and trailing underscores are stripped after lowering.

    This defends against upstream quirks where a table name accidentally
    includes a leading/trailing underscore (typically from string
    concatenation bugs on the upstream side).
    """
    assert sn_module._snake_case("_Leading") == "leading"
    assert sn_module._snake_case("Trailing_") == "trailing"


def test_snake_case_consecutive_capitals_handled():
    """Consecutive capitals collapse cleanly (e.g., ``HTTPResponse`` -> ``http_response``).

    The two-regex pattern correctly handles a run of capitals followed
    by a lowercase tail: ``"HTTPResponse"`` becomes ``"http_response"``
    rather than ``"h_t_t_p_response"`` (single-pass naïve approach) or
    ``"http_r_esponse"`` (misplaced boundary).
    """
    assert sn_module._snake_case("HTTPResponse") == "http_response"


# ---------------------------------------------------------------------------
# Phase 2.11 — Schedule payload (``leaguegamefinder``) integration
# ---------------------------------------------------------------------------
#
# The ``leaguegamefinder`` endpoint returns two rows per game (one for
# the home team, one for the away team), so duplicate ``GAME_ID``
# values are expected and CORRECT. The normalizer MUST preserve those
# duplicates — deduplication is the sole responsibility of
# ``endpoints/schedule.enumerate_game_ids``, which downstream
# pipelines use to drive per-game ingestion in F-011. This test locks
# in that contract boundary explicitly so any accidental introduction
# of "helpful" deduplication inside the normalizer is caught.
# ---------------------------------------------------------------------------


def test_schedule_payload_flattens_to_flat_dataframe(sample_schedule_payload):
    """Schedule payload flattens with duplicate ``GAME_ID`` values PRESERVED (not deduplicated).

    Deduplication is the responsibility of
    ``endpoints/schedule.enumerate_game_ids``, NOT the normalizer. The
    fixture contains five rows covering three distinct games (two rows
    per game for two games, one row for a third partial game); the
    flattened DataFrame must carry all five rows in the original
    upstream order.
    """
    out = normalize_result_sets(sample_schedule_payload)
    assert list(out.keys()) == ["league_game_finder_results"]
    df = out["league_game_finder_results"]
    assert len(df) == 5
    assert list(df.columns) == ["SEASON_ID", "TEAM_ID", "GAME_ID", "GAME_DATE"]
    game_ids = list(df["GAME_ID"])
    assert game_ids == [
        "0022500001",
        "0022500001",
        "0022500002",
        "0022500002",
        "0022500003",
    ]


# ---------------------------------------------------------------------------
# Phase 2.12 — Cross-cutting: output passes Rule 4 spot-check
# ---------------------------------------------------------------------------
#
# This spot-check is the positive counterpart to the negative Rule 4
# tests in Phase 2.7: every successful normalization MUST produce a
# DataFrame that passes the same flatness assertion that the
# production :func:`utils.schema_normalizer._assert_rule4_flat` helper
# applies internally. The ``getattr(df, "map", None) or df.applymap``
# pattern matches the production fallback exactly so the test works
# on both pandas 2.0.x (only ``applymap`` available) and pandas 2.1+
# (both ``map`` and ``applymap`` available, with ``map`` preferred).
# ---------------------------------------------------------------------------


def test_happy_path_output_satisfies_rule4_flatness(sample_single_table_payload):
    """Successful normalization ALWAYS produces Rule-4-compliant flat DataFrames.

    Uses the same pandas-version-aware ``df.map`` / ``df.applymap``
    fallback as the production ``_assert_rule4_flat`` helper so the
    assertion is valid across pandas 2.0.x and 2.1+.
    """
    out = normalize_result_sets(sample_single_table_payload)
    df = out["league_dash_player_stats"]
    checker = getattr(df, "map", None) or df.applymap
    assert not checker(lambda x: isinstance(x, (dict, list))).any().any()
