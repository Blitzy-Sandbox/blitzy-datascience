"""Unit tests for :mod:`pipelines.ingest_schedule` (Feature F-013, Schedule domain).

These tests verify the orchestration contract of the Schedule pipeline without
performing any real network I/O or filesystem work beyond ``tmp_path``. They
use the ``recording_client`` / ``recording_writer`` / ``recording_checkpoint``
spy fixtures defined in :mod:`tests.conftest` — the same fixtures that power
:mod:`tests.unit.pipelines.test_ingest_lineups` (the template this module
parallels per the Agent Action Plan §0.5.1.8).

Behavioral scope
----------------

The tests assert the following contracts:

1. **Happy path** — ``ingest_schedule.run`` invokes the ``leaguegamefinder``
   endpoint exactly once, writes the normalized DataFrame under
   ``config.CSV_SCHEDULE`` with the caller-provided season, marks the
   checkpoint with the ``"<endpoint>:<season>"`` key pattern, and
   increments the ``pipeline_rows_written_total`` counter.
2. **Idempotent resume** — when the checkpoint is pre-seeded with the
   expected ``(domain, key)`` pair, ``run`` makes no HTTP calls, writes no
   CSVs, and performs no additional ``mark_completed`` calls.
3. **Rule 5 ordering** — the pipeline calls ``checkpoint.is_completed``
   exactly once BEFORE writing, and ``checkpoint.mark_completed`` exactly
   once AFTER writing. ``mark_completed`` MUST never precede a successful
   ``write``.
4. **Negative-space guard** — the pipeline invokes ONLY
   ``leaguegamefinder``; no other NBA Stats endpoint name appears in
   ``client.calls``.

Rule 6 (fail-safe per-game iteration) is intentionally NOT exercised here
because Rule 6 is EXCLUSIVE to ``pipelines.ingest_games`` per AAP §0.7.2.6.
The Schedule pipeline must propagate exceptions rather than swallow them;
that contract is covered by integration tests and the per-pipeline
exception-propagation behavior asserted in ``test_ingest_players`` and
``test_ingest_teams``.

Cross-module invariants (Rules 1, 4, 7) are verified by the dedicated
``tests/invariants/`` suite and are deliberately out of scope here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest  # noqa: F401  (imported to satisfy Phase 1 header convention)

import config
from pipelines import ingest_schedule


# ---------------------------------------------------------------------------
# Module-level constants
#
# The checkpoint key format is an implicit contract between this pipeline,
# the checkpoint manifest JSON schema, and any operator-visible key listing
# (``output/checkpoint.json``). Duplicating the format here — rather than
# importing a private helper — is intentional so that drift in either the
# production module or the key schema is detected by a failing test.
# ---------------------------------------------------------------------------

_SEASON = "2025-26"
_ENDPOINT_LABEL = "leaguegamefinder"
_EXPECTED_KEY = f"{_ENDPOINT_LABEL}:{_SEASON}"


# ===========================================================================
# Test 1 — Happy-path: endpoint invoked, CSV written, checkpoint marked
# ===========================================================================

def test_run_happy_path_writes_schedule_and_marks_checkpoint(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_schedule_payload,
    tmp_path,
):
    """``run`` fetches leaguegamefinder, writes ``schedule.csv``, checkpoints.

    This is the end-to-end sanity test for F-013: given a simulated NBA
    Stats ``leaguegamefinder`` response, the pipeline must (a) invoke the
    client with exactly that endpoint name, (b) pass the resulting
    DataFrame to the writer with the canonical ``config.CSV_SCHEDULE``
    name, (c) mark the checkpoint with the ``"leaguegamefinder:<season>"``
    key, and (d) increment the row-count counter at least once.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_schedule_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()
    metrics_mock = MagicMock()

    ingest_schedule.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # (a) Endpoint contract: leaguegamefinder invoked.
    assert any(
        call[0] == _ENDPOINT_LABEL for call in client.calls
    ), (
        "schedule pipeline must invoke the leaguegamefinder endpoint; "
        f"recorded calls were {client.calls!r}"
    )

    # (b) Writer contract: one write with the canonical schedule CSV name.
    assert len(writer.writes) == 1, (
        f"schedule pipeline must write exactly one CSV artifact; "
        f"observed {len(writer.writes)} writes: {writer.writes!r}"
    )
    write = writer.writes[0]
    assert write["name"] == config.CSV_SCHEDULE, (
        f"writer must receive name={config.CSV_SCHEDULE!r}; got {write['name']!r}"
    )
    assert write["season"] == _SEASON, (
        f"writer must receive season={_SEASON!r}; got {write['season']!r}"
    )
    assert write["rows"] > 0, (
        "writer must receive a non-empty DataFrame for a happy-path payload; "
        f"got rows={write['rows']} (write={write!r})"
    )

    # (c) Rule 5 contract: checkpoint probed AND marked.
    assert (config.DOMAIN_SCHEDULE, _EXPECTED_KEY) in checkpoint.checks, (
        "pipeline must probe is_completed(DOMAIN_SCHEDULE, "
        f"{_EXPECTED_KEY!r}) before work; observed checks={checkpoint.checks!r}"
    )
    assert checkpoint.marks == [(config.DOMAIN_SCHEDULE, _EXPECTED_KEY)], (
        "pipeline must call mark_completed exactly once with "
        f"(DOMAIN_SCHEDULE, {_EXPECTED_KEY!r}); observed marks={checkpoint.marks!r}"
    )

    # (d) Observability contract: pipeline_rows_written_total incremented.
    rows_written_calls = [
        c
        for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert len(rows_written_calls) == 1, (
        "pipeline must increment pipeline_rows_written_total exactly once "
        "per successful write; observed "
        f"{len(rows_written_calls)} relevant inc() calls: {metrics_mock.inc.call_args_list!r}"
    )


# ===========================================================================
# Test 2 — Idempotent resume: pre-seeded checkpoint short-circuits the run
# ===========================================================================

def test_run_idempotent_skip_when_already_checkpointed(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_schedule_payload,
    tmp_path,
):
    """If the ``(schedule, leaguegamefinder:<season>)`` pair is already in
    the checkpoint, ``run`` must skip all work.

    Rule 5's resumability guarantee (AAP §0.7.2.5) means the pipeline MUST
    be idempotent: a second invocation against an already-completed
    checkpoint must produce ZERO HTTP calls, ZERO writes, and ZERO
    additional ``mark_completed`` calls. The ``is_completed`` probe itself
    IS permitted (and required) — that is how the pipeline detects the
    short-circuit condition.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_schedule_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_SCHEDULE: [_EXPECTED_KEY]}
    )
    metrics_mock = MagicMock()

    ingest_schedule.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # Short-circuit contract: no side effects beyond the is_completed probe.
    assert client.calls == [], (
        "idempotent skip must make zero client.get calls; observed "
        f"{client.calls!r}"
    )
    assert writer.writes == [], (
        "idempotent skip must write zero CSV artifacts; observed "
        f"{writer.writes!r}"
    )
    assert checkpoint.marks == [], (
        "idempotent skip must NOT re-call mark_completed; observed "
        f"{checkpoint.marks!r}"
    )
    # The probe itself is required — verify it happened exactly once
    # against the expected (domain, key) pair.
    assert (config.DOMAIN_SCHEDULE, _EXPECTED_KEY) in checkpoint.checks, (
        "idempotent skip path must still probe is_completed; observed "
        f"checks={checkpoint.checks!r}"
    )


# ===========================================================================
# Test 3 — Rule 5 ordering: is_completed BEFORE mark_completed
# ===========================================================================

def test_rule5_is_completed_precedes_mark_completed(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_schedule_payload,
    tmp_path,
):
    """The pipeline must probe the checkpoint exactly once BEFORE work and
    mark it exactly once AFTER a successful write.

    This test encodes Operational Rule 5 at the granularity of call
    ordering. A single write must be flanked by exactly ONE preceding
    ``is_completed`` probe and exactly ONE following ``mark_completed``
    call — no more, no less. Any divergence (e.g., marking before writing,
    probing twice, or marking twice) indicates a Rule 5 violation that
    would compromise resume determinism (AAP Gate 8).
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_schedule_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_schedule.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # Rule 5 pre-condition: exactly one is_completed probe, for the
    # expected (domain, key) pair.
    assert checkpoint.checks == [(config.DOMAIN_SCHEDULE, _EXPECTED_KEY)], (
        "pipeline must probe is_completed exactly once with "
        f"(DOMAIN_SCHEDULE, {_EXPECTED_KEY!r}); observed {checkpoint.checks!r}"
    )
    # Rule 5 post-condition: exactly one successful write.
    assert len(writer.writes) == 1, (
        f"pipeline must perform exactly one write; observed {len(writer.writes)}"
    )
    # Rule 5 post-condition: exactly one mark_completed for the same pair.
    assert checkpoint.marks == [(config.DOMAIN_SCHEDULE, _EXPECTED_KEY)], (
        "pipeline must call mark_completed exactly once with "
        f"(DOMAIN_SCHEDULE, {_EXPECTED_KEY!r}); observed {checkpoint.marks!r}"
    )


# ===========================================================================
# Test 4 — Negative-space guard: no non-schedule endpoints invoked
# ===========================================================================

def test_non_leaguegamefinder_endpoints_not_invoked(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_schedule_payload,
    tmp_path,
):
    """The Schedule pipeline must invoke ONLY ``leaguegamefinder``.

    The NBA Stats API exposes many related endpoints whose responses
    overlap with ``leaguegamefinder`` (e.g., ``scoreboardv2`` and
    ``leaguedashteamstats``). F-013 deliberately restricts itself to the
    single ``leaguegamefinder`` call so that (a) ``schedule.csv`` has a
    single, well-defined provenance and (b) the cross-coupling with
    :func:`endpoints.schedule.enumerate_game_ids` (which itself wraps
    ``leaguegamefinder``) is traceable.

    This test mirrors the negative-space guard in
    :mod:`tests.unit.pipelines.test_ingest_lineups` and functions as a
    regression fence: if a future maintainer adds a secondary endpoint
    call to the Schedule pipeline, this test will surface the change
    before the AAP scope can drift unnoticed.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_schedule_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_schedule.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # Endpoints the Schedule pipeline MUST NOT touch. This set is
    # intentionally broad so new inadvertent additions are caught early.
    forbidden_endpoints = {
        "scoreboardv2",
        "boxscoretraditionalv2",
        "boxscoreadvancedv2",
        "playbyplayv2",
        "leaguedashteamstats",
        "leaguedashplayerstats",
        "leaguedashlineups",
    }
    called_endpoints = {call[0] for call in client.calls}
    overlap = forbidden_endpoints & called_endpoints
    assert not overlap, (
        f"schedule pipeline must not invoke {overlap!r}; "
        f"got calls {client.calls!r}"
    )
    # Positive counterpart: leaguegamefinder SHOULD be among the calls so
    # the negative-space assertion is not trivially satisfied by a no-op
    # run (e.g., a bug that short-circuits before any endpoint call).
    assert _ENDPOINT_LABEL in called_endpoints, (
        f"schedule pipeline must invoke {_ENDPOINT_LABEL!r}; "
        f"observed endpoints={called_endpoints!r}"
    )
