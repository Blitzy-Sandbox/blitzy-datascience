"""Unit tests for :mod:`endpoints.schedule` (Feature F-013 — Schedule domain).

This module covers the two public callables exported by
``endpoints/schedule.py``:

* :func:`endpoints.schedule.fetch_leaguegamefinder` — the sole upstream
  endpoint wrapper for the Schedule domain. It delegates to
  ``client.get("leaguegamefinder", params)`` and pins
  ``PlayerOrTeam="T"`` so the upstream envelope returns one row per
  (team, game) pair — meaning each ``GAME_ID`` appears twice (home and
  away). Any downstream consumer that enumerates unique games MUST
  therefore deduplicate.

* :func:`endpoints.schedule.enumerate_game_ids` — the cross-domain
  helper consumed by ``pipelines.ingest_games`` to enumerate the
  ``GAME_ID`` list for a season (see AAP §0.4.5 Schedule → Games
  cross-dependency). It returns the deduplicated, first-seen-ordered
  list of ``GAME_ID`` strings extracted from the ``leaguegamefinder``
  envelope, and never raises on envelope-shape issues (empty payload,
  missing ``resultSets``, missing ``GAME_ID`` column) — those paths
  yield ``[]`` plus a ``WARNING`` log record.

Rule compliance
---------------

This test module honors the architectural invariants of the pipeline:

* Rule 1 — No ``import requests`` anywhere. Every HTTP interaction is
  emulated through the ``recording_client`` factory fixture from
  :mod:`tests.conftest`, which returns a ``RecordingClient`` instance
  that records calls to ``.get(endpoint, params)`` without performing
  any network I/O.

* Rule 7 — No ``DataFrame.to_csv`` call. This file performs no CSV
  emission and imports no pandas symbols.

Log-based assertions use pytest's built-in ``caplog`` fixture. The
``caplog.at_level(level, logger="endpoints.schedule")`` context manager
both elevates the named logger's level and the ``LogCaptureHandler``'s
level for the duration of the ``with`` block, guaranteeing that records
emitted by :mod:`endpoints.schedule` are captured regardless of the
logger's default level. Assertions about log-message content use
lenient substring matching so that the production log format string is
not coupled to the test.
"""
from __future__ import annotations

import logging

import pytest  # noqa: F401  (imported for convention parity with sibling test modules)

from endpoints import schedule


# ---------------------------------------------------------------------------
# fetch_leaguegamefinder — wrapper tests
# ---------------------------------------------------------------------------


def test_fetch_leaguegamefinder_calls_correct_endpoint(recording_client):
    """The wrapper MUST delegate to ``client.get("leaguegamefinder", ...)``.

    Verifies two things in one shot:

    1. Exactly one upstream call is issued (single HTTP call invariant).
    2. The endpoint name is the canonical ``"leaguegamefinder"`` — the
       same string the NBA Stats API exposes at
       ``/stats/leaguegamefinder``. Typos or deviations (e.g.,
       ``"LeagueGameFinder"`` or ``"league_game_finder"``) would break
       upstream routing without any compile-time error.
    """
    client = recording_client()
    schedule.fetch_leaguegamefinder(client, "2025-26", "Regular Season", "00")
    assert len(client.calls) == 1, "wrapper must issue exactly one upstream call"
    assert client.calls[0][0] == "leaguegamefinder"


def test_fetch_leaguegamefinder_includes_playerorteam_T(recording_client):
    """``PlayerOrTeam="T"`` MUST be pinned in the outbound params dict.

    This is the single most important structural contract in the
    Schedule wrapper. ``PlayerOrTeam="T"`` instructs the NBA Stats API
    to emit team-level rows — one row per team per game — so each
    ``GAME_ID`` appears exactly twice (home and away). The downstream
    :func:`enumerate_game_ids` helper and the Schedule pipeline both
    assume this shape and deduplicate accordingly. A silent change to
    ``"P"`` (player) would produce roughly 26,000 rows per game and
    catastrophically corrupt every downstream artifact (wrong rowcount
    in ``schedule.csv``, exploded ``GAME_ID`` enumeration, wrong
    checkpoint keys).
    """
    client = recording_client()
    schedule.fetch_leaguegamefinder(client, "2025-26", "Regular Season", "00")
    params = client.calls[-1][1]
    assert params["PlayerOrTeam"] == "T", (
        "PlayerOrTeam must be pinned to 'T' (team-level rows — one row per team per "
        "game). Changing this would silently corrupt the Schedule pipeline output."
    )


def test_fetch_leaguegamefinder_passes_season_and_league_params(recording_client):
    """Season, SeasonType, LeagueID MUST appear verbatim in the params dict.

    These three parameters form the minimum query surface the NBA Stats
    API requires. Verbatim pass-through is important because the API
    rejects (HTTP 400) any reformatting — ``"2025-26"`` is the accepted
    season string, ``"2025-2026"`` and ``"25-26"`` are not; ``"Regular
    Season"`` (with a space) is accepted, ``"regular-season"`` is not.
    """
    client = recording_client()
    schedule.fetch_leaguegamefinder(client, "2023-24", "Playoffs", "00")
    params = client.calls[-1][1]
    assert params["Season"] == "2023-24"
    assert params["SeasonType"] == "Playoffs"
    assert params["LeagueID"] == "00"


def test_fetch_leaguegamefinder_kwargs_passthrough(recording_client):
    """``**kwargs`` MUST override / supplement the baseline params dict.

    The wrapper applies ``params.update(kwargs)`` after constructing the
    baseline dict, which means caller-supplied kwargs (``DateFrom``,
    ``DateTo``, ``TeamID``, etc.) take precedence over the empty-string
    defaults. This is how the Schedule pipeline narrows the window when
    a caller requests an incremental or date-bounded backfill.
    """
    client = recording_client()
    schedule.fetch_leaguegamefinder(
        client, "2025-26", "Regular Season", "00", DateFrom="10/01/2025"
    )
    params = client.calls[-1][1]
    assert params["DateFrom"] == "10/01/2025"


def test_fetch_leaguegamefinder_returns_raw_payload(
    recording_client, sample_schedule_payload
):
    """The wrapper returns the upstream payload UNMODIFIED (pass-through).

    No normalization, flattening, or mutation happens at the endpoint
    layer — that is the pipeline's job (via
    :mod:`utils.schema_normalizer`). The wrapper must be a thin,
    deterministic delegation so that :func:`enumerate_game_ids` and
    other downstream consumers can reason about the raw envelope
    structure.
    """
    client = recording_client(responses={"leaguegamefinder": sample_schedule_payload})
    result = schedule.fetch_leaguegamefinder(client, "2025-26", "Regular Season", "00")
    assert result == sample_schedule_payload


# ---------------------------------------------------------------------------
# enumerate_game_ids — helper tests
# ---------------------------------------------------------------------------


def test_enumerate_game_ids_returns_deduplicated_first_seen_order(
    recording_client, sample_schedule_payload
):
    """Output MUST be deduplicated AND preserve first-seen order.

    The ``sample_schedule_payload`` fixture contains 5 rows with 3
    unique ``GAME_ID`` values:

    * ``"0022500001"`` appears at rows 0 and 1 (home + away).
    * ``"0022500002"`` appears at rows 2 and 3 (home + away).
    * ``"0022500003"`` appears at row 4 (solo).

    The expected enumeration is therefore exactly
    ``["0022500001", "0022500002", "0022500003"]`` — in the order the
    IDs first appear in ``rowSet``. First-seen ordering is a Gate 8
    resume-determinism prerequisite: the Games pipeline checkpoints per
    ``GAME_ID`` and relies on stable iteration order so an interrupted
    run reproducibly resumes where it left off.
    """
    client = recording_client(responses={"leaguegamefinder": sample_schedule_payload})
    result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result == ["0022500001", "0022500002", "0022500003"]


def test_enumerate_game_ids_casts_to_str(recording_client):
    """Numeric ``GAME_ID`` values MUST be coerced to ``str``.

    The NBA Stats API occasionally returns integer-typed ``GAME_ID``
    values for IDs that are numerically parseable (e.g., ``22500001``
    instead of ``"0022500001"``). Downstream box-score endpoints expect
    string-typed IDs, and the checkpoint manifest serializes them as
    JSON strings, so the enumerator MUST coerce via ``str()`` before
    emitting. This test constructs a payload with mixed int/str
    ``GAME_ID`` columns and asserts that every returned element is an
    instance of ``str``.
    """
    payload = {
        "resultSets": [
            {
                "name": "LeagueGameFinderResults",
                "headers": ["GAME_ID", "GAME_DATE"],
                "rowSet": [
                    [22500001, "2025-10-21"],
                    ["0022500002", "2025-10-21"],
                    [22500003, "2025-10-22"],
                ],
            }
        ]
    }
    client = recording_client(responses={"leaguegamefinder": payload})
    result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result, "expected a non-empty list from a three-row payload"
    assert all(isinstance(x, str) for x in result), (
        "every GAME_ID returned by enumerate_game_ids must be a str — "
        f"got types {[type(x).__name__ for x in result]}"
    )


def test_enumerate_game_ids_empty_rowset_returns_empty_list(recording_client, caplog):
    """Empty ``rowSet`` (with ``GAME_ID`` header present) yields an empty list.

    The production code walks ``payload["resultSets"]``, finds the first
    table with a ``"GAME_ID"`` header, and iterates its ``rowSet``. When
    the ``rowSet`` is an empty list, the iteration produces zero
    entries and the function returns ``[]`` without raising. A log
    record is emitted at INFO level (``game_count=0``); this test does
    not pin the level because the important contract is the empty-list
    return value, and the log format string is permitted to evolve.
    """
    empty_payload = {
        "resultSets": [
            {"name": "LeagueGameFinderResults", "headers": ["GAME_ID"], "rowSet": []}
        ]
    }
    client = recording_client(responses={"leaguegamefinder": empty_payload})
    with caplog.at_level(logging.INFO, logger="endpoints.schedule"):
        result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result == []


def test_enumerate_game_ids_missing_resultsets_returns_empty(recording_client, caplog):
    """Missing or empty ``resultSets`` yields an empty list + WARNING log.

    ``payload["resultSets"] = []`` is the "nothing to iterate" envelope
    shape the upstream sometimes returns when a season has not yet
    started or when an invalid filter combination was supplied. The
    function must NEVER raise in this case — returning ``[]`` plus a
    WARNING lets the caller (the Games pipeline) surface a
    "no games to iterate" signal to the operator without the exception
    noise. A bare ``{}`` (no ``resultSets`` key at all) is treated the
    same way because ``payload.get("resultSets") or []`` normalizes both
    shapes to ``[]``.
    """
    payload = {"resultSets": []}
    client = recording_client(responses={"leaguegamefinder": payload})
    with caplog.at_level(logging.WARNING, logger="endpoints.schedule"):
        result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result == []
    assert any(r.levelname == "WARNING" for r in caplog.records), (
        "expected at least one WARNING log record for empty resultSets"
    )


def test_enumerate_game_ids_no_game_id_header_returns_empty(recording_client, caplog):
    """Envelope lacking a ``GAME_ID`` header yields ``[]`` + WARNING log.

    Upstream schema changes or variant result-set shapes may omit the
    ``GAME_ID`` column. The header-based table discovery in
    :func:`enumerate_game_ids` walks every ``resultSets`` entry looking
    for one whose ``headers`` list contains ``"GAME_ID"``. When no such
    table exists, the function emits a WARNING and returns ``[]`` —
    again never raising ``KeyError`` or ``IndexError`` on the missing
    column. This defensive behavior keeps the Games pipeline resilient
    against upstream surprises.
    """
    payload = {
        "resultSets": [
            {"name": "Other", "headers": ["TEAM_ID"], "rowSet": [[1610612738]]}
        ]
    }
    client = recording_client(responses={"leaguegamefinder": payload})
    with caplog.at_level(logging.WARNING, logger="endpoints.schedule"):
        result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result == []
    assert any(r.levelname == "WARNING" for r in caplog.records), (
        "expected at least one WARNING log record when GAME_ID header is absent"
    )


def test_enumerate_game_ids_handles_table_not_first(recording_client):
    """Helper MUST search every ``resultSets`` entry, not just the first.

    Some NBA Stats API responses include metadata tables that precede
    the primary data table. The header-based discovery logic in
    :func:`enumerate_game_ids` MUST iterate the full ``resultSets`` list
    looking for the first entry whose ``headers`` contains ``GAME_ID``,
    rather than naively indexing ``resultSets[0]``. This test builds a
    two-table envelope where the first entry has an unrelated
    ``headers`` shape and the second carries the actual data, and
    asserts that the data table is found and its ``GAME_ID`` column is
    extracted correctly.
    """
    payload = {
        "resultSets": [
            {"name": "SomeMeta", "headers": ["META"], "rowSet": [["x"]]},
            {
                "name": "LeagueGameFinderResults",
                "headers": ["SEASON_ID", "TEAM_ID", "GAME_ID", "GAME_DATE"],
                "rowSet": [
                    ["22025", 1610612738, "0022500001", "2025-10-21"],
                    ["22025", 1610612744, "0022500001", "2025-10-21"],
                ],
            },
        ]
    }
    client = recording_client(responses={"leaguegamefinder": payload})
    result = schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert result == ["0022500001"]


def test_enumerate_game_ids_logs_info_with_count(
    recording_client, sample_schedule_payload, caplog
):
    """On success, an INFO record containing the game count MUST be emitted.

    The ``sample_schedule_payload`` fixture contains exactly 3 unique
    ``GAME_ID`` values, so the emitted INFO log should mention ``"3"``
    somewhere in its message (the production format string is
    ``"endpoints.schedule.enumerate_game_ids season=%s game_count=%d"``).
    We assert only the presence of the digit ``"3"`` in at least one
    INFO message so this test does not couple to the exact format
    string — a future log-message revision is permitted to rearrange
    the wording without breaking this test.
    """
    client = recording_client(responses={"leaguegamefinder": sample_schedule_payload})
    with caplog.at_level(logging.INFO, logger="endpoints.schedule"):
        schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert info_records, "expected at least one INFO log record on successful enumeration"
    assert any("3" in r.getMessage() for r in info_records), (
        "at least one INFO log message must contain the game_count digit '3' "
        "(sample_schedule_payload has exactly 3 unique GAME_IDs)"
    )


def test_enumerate_game_ids_invokes_fetch_leaguegamefinder(recording_client):
    """``enumerate_game_ids`` MUST delegate to ``fetch_leaguegamefinder`` internally.

    The helper is not permitted to reach past the wrapper and invoke
    ``client.get`` directly — that would duplicate the params-building
    logic and risk drift between the two code paths. Instead, the
    helper calls ``fetch_leaguegamefinder`` (which builds the params
    dict and delegates to ``client.get``), which means the single
    upstream call recorded on ``client.calls`` is for the
    ``"leaguegamefinder"`` endpoint.
    """
    client = recording_client()
    schedule.enumerate_game_ids(client, "2025-26", "Regular Season", "00")
    assert len(client.calls) == 1, (
        "enumerate_game_ids must issue exactly one upstream call "
        "(via fetch_leaguegamefinder)"
    )
    assert client.calls[0][0] == "leaguegamefinder"


def test_enumerate_game_ids_kwargs_passthrough(recording_client):
    """Kwargs given to ``enumerate_game_ids`` MUST flow through to the upstream call.

    ``enumerate_game_ids`` forwards ``**kwargs`` to
    ``fetch_leaguegamefinder``, which in turn applies
    ``params.update(kwargs)`` on the baseline params dict. This test
    passes ``DateFrom="10/01/2025"`` through ``enumerate_game_ids`` and
    asserts that the value reaches the recorded params dict on
    ``client.calls[-1]``.
    """
    client = recording_client()
    schedule.enumerate_game_ids(
        client, "2025-26", "Regular Season", "00", DateFrom="10/01/2025"
    )
    assert client.calls[-1][1].get("DateFrom") == "10/01/2025"
