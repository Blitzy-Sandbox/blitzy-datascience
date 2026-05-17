"""Unit tests for :mod:`pipelines.ingest_lineups` (Feature F-012).

Scope
-----
This module exercises the Lineups ingestion pipeline orchestrator at
:mod:`pipelines.ingest_lineups` — the F-012 component in the Agent Action Plan
(AAP) feature catalog. The pipeline is deliberately narrow: it fetches a
single NBA Stats endpoint (``leaguedashlineups``) and emits a single flat CSV
artifact (``lineups.csv``) with a single checkpoint key
(``"leaguedashlineups:<season>"``).

Verified behaviors
------------------
1. **Happy path** — invoking :func:`ingest_lineups.run` against a clean
   checkpoint results in exactly one ``leaguedashlineups`` call on the
   injected :class:`RecordingClient`, exactly one ``writer.write`` call with
   ``name == config.CSV_LINEUPS`` and ``season == "2025-26"``, and exactly one
   ``checkpoint.mark_completed`` call keyed by ``(config.DOMAIN_LINEUPS,
   "leaguedashlineups:2025-26")``. The ``pipeline_rows_written_total`` metric
   is incremented exactly once.

2. **Idempotency (Rule 5 resume behavior)** — invoking :func:`ingest_lineups.run`
   with a pre-seeded checkpoint short-circuits BEFORE any HTTP call, any CSV
   write, or any additional ``mark_completed`` invocation. Only the
   ``is_completed`` probe should be observed.

3. **Rule 5 ordering** — ``checkpoint.is_completed`` runs BEFORE fetch,
   ``writer.write`` precedes ``checkpoint.mark_completed``, and
   ``mark_completed`` is the last side-effect recorded by the spies. Per AAP
   §0.7.2.5, this ordering is the durability invariant that makes the pipeline
   resumable across crashes.

4. **Negative-space guard** — the pipeline must NOT invoke the
   ``leaguedashplayerclutch`` endpoint. Although
   :func:`endpoints.lineups.fetch_leaguedashplayerclutch_onoff` exists in the
   endpoints module for library-level access, it is intentionally excluded from
   the lineups pipeline's fetch graph (key-column mismatch: per-player clutch
   splits cannot be flattened into the same lineups leaderboard CSV). This
   test protects against future regressions where someone might "helpfully"
   extend the pipeline to include clutch splits.

Style conventions
-----------------
* Domain and CSV-artifact identifiers are referenced symbolically via
  :data:`config.DOMAIN_LINEUPS` and :data:`config.CSV_LINEUPS` (AAP Phase 7
  style requirement) so a future rename of either constant automatically
  propagates to the assertions without test-file edits.
* Collaborators (client/writer/checkpoint) are handwritten spies supplied by
  :mod:`tests.conftest` factory fixtures (``recording_client``,
  ``recording_writer``, ``recording_checkpoint``) — :class:`~unittest.mock.MagicMock`
  is used ONLY for the optional ``metrics`` collaborator, whose interface
  surface (``inc``, ``observe``) is deliberately narrow.
* Rule 6 (fail-safe per-entity iteration) does NOT apply to this pipeline —
  per AAP §0.7.2.6 only :mod:`pipelines.ingest_games` wraps its loop in
  ``try/except Exception``; lineups propagates exceptions upward.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest  # noqa: F401  (imported to satisfy Phase 1 header convention)

import config
from pipelines import ingest_lineups


# ---------------------------------------------------------------------------
# Module-level constants shared by every test
# ---------------------------------------------------------------------------
#
# ``_SEASON``           — the canonical NBA season identifier exercised by all
#                         four tests. Chosen to match AAP §0.1.1 default.
# ``_ENDPOINT_LABEL``   — the exact string passed to ``NBAClient.get`` by
#                         :func:`endpoints.lineups.fetch_leaguedashlineups`;
#                         also the leading token of the checkpoint key.
# ``_EXPECTED_KEY``     — the fully-qualified checkpoint key that
#                         :mod:`pipelines.ingest_lineups` constructs internally
#                         (see the ``key = f"{endpoint_label}:{season}"`` line
#                         in the pipeline source). Duplicated here so the test
#                         file is self-contained and does not reach into the
#                         pipeline module's private construction logic.
_SEASON = "2025-26"
_ENDPOINT_LABEL = "leaguedashlineups"
_EXPECTED_KEY = f"{_ENDPOINT_LABEL}:{_SEASON}"


# ---------------------------------------------------------------------------
# Test 1 — Happy path: fetch → write → mark checkpoint
# ---------------------------------------------------------------------------
def test_run_happy_path_writes_lineups_and_marks_checkpoint(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """End-to-end happy path: fetch ``leaguedashlineups`` → write ``lineups.csv`` → mark checkpoint.

    Given a clean (empty) checkpoint and a recording client preloaded with a
    canonical single-table ``resultSets`` envelope for the ``leaguedashlineups``
    endpoint, invoking :func:`ingest_lineups.run` must:

    * produce at least one ``leaguedashlineups`` call on the client spy,
    * produce exactly one ``writer.write`` call whose ``name`` equals
      :data:`config.CSV_LINEUPS`, whose ``season`` equals ``_SEASON``, and
      whose recorded ``rows`` count is strictly positive,
    * record the ``is_completed`` probe for ``(config.DOMAIN_LINEUPS, _EXPECTED_KEY)``,
    * produce exactly one ``mark_completed`` call for the same
      ``(domain, key)`` tuple (Rule 5),
    * increment the ``pipeline_rows_written_total`` metric exactly once.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()
    metrics_mock = MagicMock()

    ingest_lineups.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- Client spy: the leaguedashlineups endpoint was invoked ------------
    assert any(call[0] == _ENDPOINT_LABEL for call in client.calls), (
        f"expected a {_ENDPOINT_LABEL} call; got {client.calls!r}"
    )

    # --- Writer spy: exactly one write with the expected identifiers ------
    assert len(writer.writes) == 1, (
        f"expected exactly 1 writer.write call; got {len(writer.writes)}: {writer.writes!r}"
    )
    write = writer.writes[0]
    assert write["name"] == config.CSV_LINEUPS, (
        f"expected CSV name {config.CSV_LINEUPS!r}; got {write['name']!r}"
    )
    assert write["season"] == _SEASON, (
        f"expected season {_SEASON!r}; got {write['season']!r}"
    )
    assert write["rows"] > 0, (
        f"expected non-empty DataFrame; got rows={write['rows']}"
    )

    # --- Checkpoint spy: is_completed probed, mark_completed called once --
    assert (config.DOMAIN_LINEUPS, _EXPECTED_KEY) in checkpoint.checks, (
        f"expected is_completed probe for {(config.DOMAIN_LINEUPS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )
    assert checkpoint.marks == [(config.DOMAIN_LINEUPS, _EXPECTED_KEY)], (
        f"expected exactly one mark_completed for {(config.DOMAIN_LINEUPS, _EXPECTED_KEY)!r}; "
        f"got marks={checkpoint.marks!r}"
    )

    # --- Metrics spy: pipeline_rows_written_total incremented exactly once --
    rows_written_calls = [
        c for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert len(rows_written_calls) == 1, (
        f"expected exactly 1 pipeline_rows_written_total increment; "
        f"got {len(rows_written_calls)}: {metrics_mock.inc.call_args_list!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Idempotency: pre-seeded checkpoint short-circuits the pipeline
# ---------------------------------------------------------------------------
def test_run_idempotent_skip_when_already_checkpointed(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """If checkpoint reports completion, the pipeline must not fetch or write.

    Pre-seeding the :class:`RecordingCheckpoint` with
    ``{config.DOMAIN_LINEUPS: [_EXPECTED_KEY]}`` simulates a prior successful
    run. On re-invocation the pipeline must:

    * make zero client calls (no HTTP against NBA Stats),
    * make zero writer calls (no CSV overwrite),
    * make zero additional ``mark_completed`` calls (the existing
      checkpoint entry already records completion),
    * still record the ``is_completed`` probe — this IS expected because the
      pipeline discovers its short-circuit precisely by asking the checkpoint.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_LINEUPS: [_EXPECTED_KEY]},
    )
    metrics_mock = MagicMock()

    ingest_lineups.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- No side effects on fetch, write, or mark paths ------------------
    assert client.calls == [], f"client must not be called; got {client.calls!r}"
    assert writer.writes == [], f"writer must not be called; got {writer.writes!r}"
    assert checkpoint.marks == [], (
        f"no additional mark_completed calls expected; got {checkpoint.marks!r}"
    )

    # --- But the is_completed probe IS expected (that's how we skipped) --
    assert (config.DOMAIN_LINEUPS, _EXPECTED_KEY) in checkpoint.checks, (
        f"expected is_completed probe for {(config.DOMAIN_LINEUPS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Rule 5 ordering: is_completed precedes mark_completed
# ---------------------------------------------------------------------------
def test_rule5_is_completed_precedes_mark_completed(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """Rule 5: ``is_completed`` precedes fetch; ``mark_completed`` follows ``writer.write``.

    The ordering invariant established by AAP §0.7.2.5 requires that:

    1. The checkpoint is probed BEFORE the endpoint is fetched (so a resumed
       run never re-incurs the HTTP cost of a completed pull).
    2. The checkpoint is marked AFTER the CSV is successfully written (so a
       crash between fetch and write leaves the key in a re-attemptable state).

    The :class:`RecordingCheckpoint` spy records ``checks`` and ``marks`` as
    ordered lists, letting us assert the exact sequence of observations.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_lineups.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # Exactly one is_completed probe, for the expected (domain, key) tuple
    assert checkpoint.checks == [(config.DOMAIN_LINEUPS, _EXPECTED_KEY)], (
        f"expected exactly one is_completed probe for {(config.DOMAIN_LINEUPS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )

    # Exactly one writer.write — the side effect that must precede mark
    assert len(writer.writes) == 1, (
        f"expected exactly 1 writer.write call; got {len(writer.writes)}: {writer.writes!r}"
    )

    # Exactly one mark_completed — and for the same (domain, key) tuple
    assert checkpoint.marks == [(config.DOMAIN_LINEUPS, _EXPECTED_KEY)], (
        f"expected exactly one mark_completed for {(config.DOMAIN_LINEUPS, _EXPECTED_KEY)!r}; "
        f"got marks={checkpoint.marks!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Negative-space guard: leaguedashplayerclutch is NOT invoked
# ---------------------------------------------------------------------------
def test_clutch_onoff_endpoint_is_not_invoked(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """The lineups pipeline must NOT invoke ``leaguedashplayerclutch``.

    The :mod:`endpoints.lineups` module deliberately exposes TWO wrappers —
    :func:`fetch_leaguedashlineups` AND :func:`fetch_leaguedashplayerclutch_onoff`
    — but only the former is part of the F-012 ingestion graph. The latter
    exists for library-level access (e.g., future experimentation or an
    ad-hoc analysis script) and intentionally emits rows with a per-player
    key-column signature that cannot be flattened into the lineups
    leaderboard CSV.

    This test is a negative-space assertion that protects against future
    regressions where someone might "helpfully" extend the lineups pipeline
    to include clutch splits. If anyone adds a ``fetch_leaguedashplayerclutch_onoff``
    call to :func:`ingest_lineups.run`, this test will fail loudly.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_lineups.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    forbidden_endpoints = {"leaguedashplayerclutch"}
    called_endpoints = {call[0] for call in client.calls}
    overlap = forbidden_endpoints & called_endpoints
    assert not overlap, (
        f"lineups pipeline must not invoke {overlap!r}; got calls {client.calls!r}"
    )
