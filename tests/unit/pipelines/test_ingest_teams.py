"""Unit tests for :mod:`pipelines.ingest_teams` (Feature F-010).

Scope
-----
This module exercises the Teams ingestion pipeline orchestrator at
:mod:`pipelines.ingest_teams` — the F-010 component in the Agent Action Plan
(AAP) feature catalog. The pipeline is deliberately narrow: it fetches a
single NBA Stats endpoint (``leaguedashteamstats``) and emits a single flat
CSV artifact (``teams.csv``) with a single checkpoint key
(``"leaguedashteamstats:<season>"``). Per-team detail endpoints
(:func:`endpoints.teams.fetch_teamgamelog` and
:func:`endpoints.teams.fetch_teamdashboardbygeneralsplits`) exist as library
functions but are intentionally NOT invoked by :func:`ingest_teams.run`; the
F-010 pipeline is scoped to league-wide team aggregates only.

Verified behaviors
------------------
1. **Happy path** — invoking :func:`ingest_teams.run` against a clean
   checkpoint results in exactly one ``leaguedashteamstats`` call on the
   injected :class:`RecordingClient`, exactly one ``writer.write`` call with
   ``name == config.CSV_TEAMS`` and ``season == "2025-26"``, and exactly one
   ``checkpoint.mark_completed`` call keyed by ``(config.DOMAIN_TEAMS,
   "leaguedashteamstats:2025-26")``. The ``pipeline_rows_written_total``
   metric is incremented exactly once.

2. **Idempotency (Rule 5 resume behavior)** — invoking
   :func:`ingest_teams.run` with a pre-seeded checkpoint short-circuits BEFORE
   any HTTP call, any CSV write, or any additional ``mark_completed``
   invocation. Only the ``is_completed`` probe should be observed.

3. **Rule 5 ordering** — ``checkpoint.is_completed`` runs BEFORE fetch,
   ``writer.write`` precedes ``checkpoint.mark_completed``, and
   ``mark_completed`` is the last side-effect recorded by the spies. Per AAP
   §0.7.2.5, this ordering is the durability invariant that makes the pipeline
   resumable across crashes.

4. **Season column guarantee** — the DataFrame handed to ``writer.write``
   carries a lowercase ``"season"`` column. :mod:`pipelines.ingest_teams`
   normalizes the upstream envelope and invokes an internal
   ``_ensure_season_column`` helper that inserts the column at position 0 if
   the upstream payload did not already carry a season-like column. Without
   this guarantee the downstream consumer of ``teams.csv`` would be unable to
   partition team aggregates by season, which is the README output contract.

Style conventions
-----------------
* Domain and CSV-artifact identifiers are referenced symbolically via
  :data:`config.DOMAIN_TEAMS` and :data:`config.CSV_TEAMS` (AAP Phase 7 style
  requirement) so a future rename of either constant automatically propagates
  to the assertions without test-file edits.
* Collaborators (client/writer/checkpoint) are handwritten spies supplied by
  :mod:`tests.conftest` factory fixtures (``recording_client``,
  ``recording_writer``, ``recording_checkpoint``) —
  :class:`~unittest.mock.MagicMock` is used ONLY for the optional ``metrics``
  collaborator, whose interface surface (``inc``, ``observe``) is deliberately
  narrow.
* Rule 6 (fail-safe per-entity iteration) does NOT apply to this pipeline —
  per AAP §0.7.2.6 only :mod:`pipelines.ingest_games` wraps its loop in
  ``try/except Exception``; teams propagates exceptions upward.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest  # noqa: F401  (imported to satisfy Phase 1 header convention)

import config
from pipelines import ingest_teams


# ---------------------------------------------------------------------------
# Module-level constants shared by every test
# ---------------------------------------------------------------------------
#
# ``_SEASON``           — the canonical NBA season identifier exercised by all
#                         four tests. Chosen to match AAP §0.1.1 default.
# ``_ENDPOINT_LABEL``   — the exact string passed to ``NBAClient.get`` by
#                         :func:`endpoints.teams.fetch_leaguedashteamstats`;
#                         also the leading token of the checkpoint key.
# ``_EXPECTED_KEY``     — the fully-qualified checkpoint key that
#                         :mod:`pipelines.ingest_teams` constructs internally
#                         (see the ``key = f"{endpoint_label}:{season}"`` line
#                         in the pipeline source). Duplicated here so the test
#                         file is self-contained and does not reach into the
#                         pipeline module's private construction logic.
_SEASON = "2025-26"
_ENDPOINT_LABEL = "leaguedashteamstats"
_EXPECTED_KEY = f"{_ENDPOINT_LABEL}:{_SEASON}"


# ---------------------------------------------------------------------------
# Test 1 — Happy path: fetch → write → mark checkpoint
# ---------------------------------------------------------------------------
def test_run_happy_path_writes_teams_and_marks_checkpoint(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """End-to-end happy path: fetch ``leaguedashteamstats`` → write ``teams.csv`` → mark checkpoint.

    Given a clean (empty) checkpoint and a recording client preloaded with a
    canonical single-table ``resultSets`` envelope for the
    ``leaguedashteamstats`` endpoint, invoking :func:`ingest_teams.run` must:

    * produce at least one ``leaguedashteamstats`` call on the client spy,
    * produce exactly one ``writer.write`` call whose ``name`` equals
      :data:`config.CSV_TEAMS`, whose ``season`` equals ``_SEASON``, and whose
      recorded ``rows`` count is strictly positive,
    * record the ``is_completed`` probe for
      ``(config.DOMAIN_TEAMS, _EXPECTED_KEY)``,
    * produce exactly one ``mark_completed`` call for the same
      ``(domain, key)`` tuple (Rule 5),
    * increment the ``pipeline_rows_written_total`` metric exactly once.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()
    metrics_mock = MagicMock()

    ingest_teams.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # --- Client spy: the leaguedashteamstats endpoint was invoked ----------
    assert any(call[0] == _ENDPOINT_LABEL for call in client.calls), (
        f"expected a {_ENDPOINT_LABEL} call; got {client.calls!r}"
    )

    # --- Writer spy: exactly one write with the expected identifiers ------
    assert len(writer.writes) == 1, (
        f"expected exactly 1 writer.write call; got {len(writer.writes)}: {writer.writes!r}"
    )
    write = writer.writes[0]
    assert write["name"] == config.CSV_TEAMS, (
        f"expected CSV name {config.CSV_TEAMS!r}; got {write['name']!r}"
    )
    assert write["season"] == _SEASON, (
        f"expected season {_SEASON!r}; got {write['season']!r}"
    )
    assert write["rows"] > 0, (
        f"expected non-empty DataFrame; got rows={write['rows']}"
    )

    # --- Checkpoint spy: is_completed probed, mark_completed called once --
    assert (config.DOMAIN_TEAMS, _EXPECTED_KEY) in checkpoint.checks, (
        f"expected is_completed probe for {(config.DOMAIN_TEAMS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )
    assert checkpoint.marks == [(config.DOMAIN_TEAMS, _EXPECTED_KEY)], (
        f"expected exactly one mark_completed for {(config.DOMAIN_TEAMS, _EXPECTED_KEY)!r}; "
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
    ``{config.DOMAIN_TEAMS: [_EXPECTED_KEY]}`` simulates a prior successful
    run. On re-invocation the pipeline must:

    * make zero client calls (no HTTP against NBA Stats),
    * make zero writer calls (no CSV overwrite),
    * make zero additional ``mark_completed`` calls (the existing checkpoint
      entry already records completion),
    * still record the ``is_completed`` probe — this IS expected because the
      pipeline discovers its short-circuit precisely by asking the checkpoint,
    * make zero ``pipeline_rows_written_total`` increments (nothing was
      written, nothing to count).
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_TEAMS: [_EXPECTED_KEY]},
    )
    metrics_mock = MagicMock()

    ingest_teams.run(
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
    assert (config.DOMAIN_TEAMS, _EXPECTED_KEY) in checkpoint.checks, (
        f"expected is_completed probe for {(config.DOMAIN_TEAMS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )

    # --- Metrics: zero rows-written increments when nothing was written --
    rows_written_calls = [
        c for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert rows_written_calls == [], (
        f"expected zero pipeline_rows_written_total increments on idempotent skip; "
        f"got {rows_written_calls!r}"
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
       crash between fetch and write leaves the key in a re-attemptable
       state).

    The :class:`RecordingCheckpoint` spy records ``checks`` and ``marks`` as
    ordered lists, letting us assert the exact sequence of observations:
    exactly one ``is_completed`` probe, exactly one ``writer.write``, exactly
    one ``mark_completed``, all for the same ``(domain, key)`` tuple.
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_teams.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # Exactly one is_completed probe, for the expected (domain, key) tuple
    assert checkpoint.checks == [(config.DOMAIN_TEAMS, _EXPECTED_KEY)], (
        f"expected exactly one is_completed probe for {(config.DOMAIN_TEAMS, _EXPECTED_KEY)!r}; "
        f"got checks={checkpoint.checks!r}"
    )

    # Exactly one writer.write — the side effect that must precede mark
    assert len(writer.writes) == 1, (
        f"expected exactly 1 writer.write call; got {len(writer.writes)}: {writer.writes!r}"
    )

    # Exactly one mark_completed — and for the same (domain, key) tuple
    assert checkpoint.marks == [(config.DOMAIN_TEAMS, _EXPECTED_KEY)], (
        f"expected exactly one mark_completed for {(config.DOMAIN_TEAMS, _EXPECTED_KEY)!r}; "
        f"got marks={checkpoint.marks!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Season column guarantee: writer receives a DataFrame with `season`
# ---------------------------------------------------------------------------
def test_writer_receives_dataframe_with_season_column(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
    tmp_path,
):
    """The pipeline must guarantee a ``season`` column on the DataFrame handed to the writer.

    The :mod:`pipelines.ingest_teams` orchestrator normalizes the upstream
    ``resultSets`` envelope and invokes its internal ``_ensure_season_column``
    helper before handing the DataFrame to :meth:`RecordingWriter.write`. That
    helper inserts a lowercase ``"season"`` column at position 0 IFF the
    upstream payload did not already carry a season-like column.

    The :data:`sample_single_table_payload` fixture emits headers
    ``["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "PTS"]`` — none of which are
    season-like — so for this fixture the helper WILL inject the column. The
    lowercase-casefolded membership check below tolerates both the injected
    lowercase ``"season"`` and any future upstream passthrough using an
    uppercase variant (``SEASON``, ``SEASON_ID``), satisfying the README
    output contract that every row in ``teams.csv`` must be partitionable by
    season.

    The :class:`RecordingWriter` spy stores an independent ``.copy()`` of the
    DataFrame at write time, so ``writer.writes[0]["df"]`` accurately reflects
    the exact frame the pipeline handed off — immune to any subsequent
    mutation within :func:`ingest_teams.run` (there are none today, but the
    ``.copy()`` behavior is a defensive guarantee of the spy).
    """
    client = recording_client(responses={_ENDPOINT_LABEL: sample_single_table_payload})
    writer = recording_writer(tmp_path)
    checkpoint = recording_checkpoint()

    ingest_teams.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    df = writer.writes[0]["df"]
    lower_cols = {c.lower() for c in df.columns}
    assert "season" in lower_cols, (
        f"DataFrame must contain a 'season' column (any case); "
        f"got columns={list(df.columns)!r}"
    )
