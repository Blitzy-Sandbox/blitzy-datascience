"""Unit tests for ``pipelines.ingest_players`` — the F-009 Players
ingestion orchestrator.

Feature Coverage
----------------
Covers Feature **F-009 Players** per AAP §0.2.3, §0.4.1.1, §0.5.1.6
and the Product Brief §5 operational rules that apply to this
pipeline (Rules 4, 5, 7; Rule 6 intentionally does NOT apply — see
AAP §0.7.2.6).

The pipeline under test iterates a module-level
:data:`pipelines.ingest_players._ENDPOINT_PLAN` tuple of
``(fetch_callable, csv_name, endpoint_label)`` triples and, for each
endpoint, performs the canonical
fetch -> normalize -> season-guard -> write -> count -> checkpoint
cycle. The two default endpoints emit the two CSV artifacts the
README declares for the Players domain:

* ``output/players.csv`` from :func:`endpoints.players.fetch_leaguedashplayerstats`
* ``output/player_tracking.csv`` from :func:`endpoints.players.fetch_leaguedashptstats`

Coverage Matrix
---------------
1. **Endpoint plan shape** — ``_ENDPOINT_PLAN`` is an ordered
   two-entry tuple whose elements are ``(fetch_fn, csv_name,
   endpoint_label)`` triples bound to the exact symbols from
   :mod:`endpoints.players` and :mod:`config` (AAP §0.4.1.1).
2. **Nominal run** — a run against a pending checkpoint invokes
   both endpoints in plan order and produces exactly two writer
   calls and two checkpoint ``mark_completed`` calls
   (AAP §0.4.1.1, §0.5.1.6).
3. **Writer contract** — each ``writer.write`` call receives the
   exact ``csv_name`` from :mod:`config` (``"players"`` then
   ``"player_tracking"``), the caller-supplied ``season`` string,
   and a :class:`pandas.DataFrame` derived from the payload for
   that endpoint (Rule 7 / AAP §0.7.2.7).
4. **Season column guarantee** — the DataFrame passed to
   ``writer.write`` always contains a lowercase ``season`` column.
   When the upstream rowset omits it, it is inserted at position 0
   with the caller-supplied season value (README output contract).
5. **Season column idempotency** — when upstream payloads already
   carry a ``season``-like column (any case), the helper does not
   re-insert and preserves the upstream value.
6. **Rule 5 ordering** — ``checkpoint.mark_completed`` is invoked
   AFTER the corresponding ``writer.write`` succeeds, and never
   when ``writer.write`` raises (AAP §0.7.2.5).
7. **Partial resume** — when one endpoint's checkpoint key is
   already present, the pipeline emits a ``pipeline.skip`` log event
   for that endpoint, calls the writer ZERO times for it, and
   continues iterating to the next endpoint (AAP §0.7.2.5).
8. **Full resume** — when both checkpoint keys are present, the
   pipeline makes zero writer calls, zero ``mark_completed`` calls,
   and increments the metric counter zero times.
9. **Checkpoint key schema** — keys follow the literal
   ``"<endpoint_label>:<season>"`` template; the session-scoped
   :pyfixture:`checkpoint_keys` fixture pins this contract.
10. **Exception propagation (Rule 6 NOT applied)** — exceptions
    raised by any of the fetch callables, by the normalizer, or by
    the writer propagate unwrapped to the caller. Rule 6's
    ``try/except Exception`` fail-safe is scoped to
    :mod:`pipelines.ingest_games` only per AAP §0.7.2.6.
11. **Rule-5 durability on failure** — when the second endpoint
    raises, the first endpoint's checkpoint is NOT rolled back. A
    subsequent run with the same checkpoint state skips the first
    endpoint and retries only the failed one.
12. **Metrics emission** — each successful write increments
    ``pipeline_rows_written_total`` with labels
    ``{"pipeline": "ingest_players", "artifact": "<csv_name>.csv"}``
    and ``n`` equal to the row count of the DataFrame passed to the
    writer (Observability rule / AAP §0.7.3.1). The ``pipeline`` and
    ``artifact`` label names are the documented contract in
    ``docs/OBSERVABILITY.md`` and are consumed by the operator
    dashboard chart ``pipeline_rows_written_total`` and by the
    Prometheus exposition ordering.
13. **Logger defaulting** — when ``run()`` is invoked without an
    explicit ``logger`` argument the module-level fallback
    :data:`_LOGGER` is used; when ``logger`` is supplied, every
    log call routes through the supplied adapter.
14. **Structured log events** — ``pipeline.start``,
    ``pipeline.skip``, ``pipeline.wrote``, ``pipeline.complete``
    are emitted at INFO in the documented order with the
    documented positional arguments.
15. **Empty-payload handling** — an empty rowset (zero rows) still
    produces a writer call and a checkpoint mark, satisfying the
    "operators always see an artifact" contract documented by
    :func:`_select_primary_df`.
16. **Module invariants** — ``__all__`` exposes only ``run``; the
    module does not import the ``requests`` transport library
    (Rule 1 — AAP §0.7.2.1), does not contain a literal
    ``to_csv`` call in its source (Rule 7), does not contain a
    ``try/except`` block (Rule 6 non-application), and the
    module-level logger is a :class:`logging.LoggerAdapter` with a
    name matching the module's import path.

Rule Invariants Asserted
------------------------
* **Rule 1 — Single HTTP Client** (AAP §0.7.2.1). Negative-space:
  :mod:`pipelines.ingest_players` does not import ``requests``,
  ``urllib``, or ``httpx`` at module level.
* **Rule 4 — Flat CSV Output** (AAP §0.7.2.4). Enforced by
  :func:`utils.schema_normalizer.normalize_result_sets`; these
  tests rely on that contract and verify the pipeline does not
  bypass it.
* **Rule 5 — Checkpoint After Every Pull** (AAP §0.7.2.5).
  Multiple tests pin the invariant that ``mark_completed`` runs
  immediately AFTER a successful ``writer.write`` and NEVER when
  ``writer.write`` raises.
* **Rule 6 — Fail-Safe Game Iteration** (AAP §0.7.2.6). This
  rule is scoped to :mod:`pipelines.ingest_games` ONLY. These
  tests explicitly verify that fetch / normalize / write
  exceptions propagate from ``ingest_players.run`` without being
  swallowed.
* **Rule 7 — Pluggable Storage** (AAP §0.7.2.7). Negative-space:
  :mod:`pipelines.ingest_players` does not contain a literal
  ``to_csv`` token in its source.

Mocking Strategy
----------------
Per ``tests/conftest.py`` §6.1 — "tests rely on explicit fixtures
with deterministic state transitions, not generic mocking
libraries" — these tests use the handwritten spy fixtures provided
by :mod:`tests.conftest` (``recording_client``, ``recording_writer``,
``recording_checkpoint``) rather than :mod:`unittest.mock`. A tiny
module-local :class:`_MetricsSpy` and :class:`_LoggerSpy` provide
call-order assertion surfaces where the project-wide fixtures
would be too coarse. Payload fixtures (``sample_single_table_payload``,
``sample_empty_payload``) are reused from the shared conftest;
tracking-endpoint payloads are built inline by
:func:`_make_tracking_payload` to keep the per-endpoint DataFrames
distinguishable in writer-call assertions.

Test Organization
-----------------
* :class:`TestEndpointPlanShape` — module-level ``_ENDPOINT_PLAN``
  structure and ordering.
* :class:`TestNominalRun` — end-to-end happy path covering both
  endpoints.
* :class:`TestWriterContract` — per-call ``writer.write`` argument
  shape and DataFrame identity.
* :class:`TestSeasonColumnInjection` — lowercase ``season``
  column guarantee and idempotency.
* :class:`TestResumeBehavior` — skip-one and skip-all resume
  paths.
* :class:`TestCheckpointKeySchema` — exact key format for every
  endpoint.
* :class:`TestExceptionPropagation` — Rule 6 non-application.
* :class:`TestMetricsIntegration` — counter name, labels, and
  ``n`` value per write.
* :class:`TestLoggerBehavior` — default vs. explicit logger and
  structured event sequence.
* :class:`TestEmptyPayload` — zero-row path.
* :class:`TestModuleInvariants` — ``__all__``, negative-space
  import checks, ``_LOGGER`` typing, and source-level Rule 1 / 7 /
  6 checks.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pandas as pd
import pytest

import config
from pipelines import ingest_players
from pipelines.ingest_players import run


# ---------------------------------------------------------------------------
# Module-level test helpers and inline spies
# ---------------------------------------------------------------------------


def _make_primary_payload(
    rows: int = 3,
    *,
    with_season_column: bool = False,
) -> Dict[str, Any]:
    """Build a realistic ``leaguedashplayerstats`` response envelope.

    The shape matches :pyfixture:`sample_single_table_payload` from
    :mod:`tests.conftest` but is produced inline so tests can vary
    the row count and optionally include an upstream ``SEASON`` or
    ``season`` column to exercise :func:`_ensure_season_column`'s
    idempotency path.
    """
    header_base = ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "PTS"]
    row_base = [
        [203999, "Nikola Jokic", 1610612743, 29.6],
        [1629029, "Luka Doncic", 1610612742, 32.4],
        [1628369, "Jayson Tatum", 1610612738, 26.9],
        [201939, "Stephen Curry", 1610612744, 26.4],
        [201142, "Kevin Durant", 1610612756, 27.1],
    ]
    if with_season_column:
        headers = ["SEASON"] + header_base
        rowset = [["2025-26"] + r for r in row_base[:rows]]
    else:
        headers = header_base
        rowset = [list(r) for r in row_base[:rows]]
    return {
        "resource": "leaguedashplayerstats",
        "parameters": {"Season": "2025-26", "SeasonType": "Regular Season"},
        "resultSets": [
            {
                "name": "LeagueDashPlayerStats",
                "headers": headers,
                "rowSet": rowset,
            }
        ],
    }


def _make_tracking_payload(rows: int = 2) -> Dict[str, Any]:
    """Build a realistic ``leaguedashptstats`` response envelope.

    Distinct from :func:`_make_primary_payload` in resultSet name and
    column set so writer-call assertions can verify the correct
    DataFrame was routed to ``output/player_tracking.csv``.
    """
    header_base = ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "DIST_FEET", "AVG_SPEED"]
    row_base = [
        [203999, "Nikola Jokic", 1610612743, 12345.0, 3.91],
        [1629029, "Luka Doncic", 1610612742, 11882.0, 4.02],
        [1628369, "Jayson Tatum", 1610612738, 13104.0, 4.14],
    ]
    return {
        "resource": "leaguedashptstats",
        "parameters": {
            "Season": "2025-26",
            "SeasonType": "Regular Season",
            "PtMeasureType": "SpeedDistance",
        },
        "resultSets": [
            {
                "name": "LeagueDashPtStats",
                "headers": header_base,
                "rowSet": [list(r) for r in row_base[:rows]],
            }
        ],
    }


class _LoggerSpy(logging.LoggerAdapter):
    """Handwritten :class:`logging.LoggerAdapter` subclass that captures
    every structured-log call made by the pipeline.

    Stores each call as a ``(level, msg, args)`` triple on the
    :attr:`records` list in call order so tests can assert both the
    content AND the sequence of ``pipeline.start`` / ``pipeline.skip``
    / ``pipeline.wrote`` / ``pipeline.complete`` events. Preferred over
    :mod:`unittest.mock` / :mod:`caplog` because:

    * Matches the project's ``tests/conftest.py`` §6.1 handwritten-spy
      directive.
    * Bypasses the autouse logger-handler reset fixture (the spy
      never touches the real :mod:`logging` root) so the captured
      records are immune to per-test teardown ordering.
    * Concrete :class:`logging.LoggerAdapter` subclass satisfies
      :func:`ingest_players.run`'s ``logger`` parameter type hint.
    """

    def __init__(self) -> None:
        # A ``logging.LoggerAdapter`` requires an underlying logger
        # and an ``extra`` dict. We give it a stand-alone logger that
        # never propagates to the root so nothing leaks into captured
        # output, and ignore ``extra`` because we override ``info`` /
        # ``warning`` / ``error`` directly.
        underlying = logging.getLogger("tests.pipelines.ingest_players.spy")
        underlying.propagate = False
        super().__init__(underlying, {})
        self.records: List[Tuple[str, str, Tuple[Any, ...]]] = []

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.records.append(("INFO", str(msg), tuple(args)))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.records.append(("WARNING", str(msg), tuple(args)))

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.records.append(("ERROR", str(msg), tuple(args)))

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.records.append(("DEBUG", str(msg), tuple(args)))

    # Provide a convenience method for the common "first INFO event"
    # assertion pattern.
    def info_messages(self) -> List[str]:
        return [msg for level, msg, _ in self.records if level == "INFO"]


class _MetricsSpy:
    """Handwritten metrics-sink spy.

    Duck-types the :meth:`utils.metrics.MetricsRegistry.inc` signature
    used by :func:`ingest_players.run` and records every invocation as
    a ``(name, labels_dict, n)`` triple on :attr:`calls`. Tests can
    then assert call count, argument shape, and ordering without
    depending on the shared-mutable :data:`utils.metrics.registry`
    singleton. The autouse metrics-registry reset fixture still fires
    (so the real registry is cleaned up), but these tests do not
    touch it.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, str], float]] = []

    def inc(
        self,
        name: str,
        labels: Dict[str, str] = None,  # type: ignore[assignment]
        n: float = 1.0,
    ) -> None:
        self.calls.append(
            (str(name), dict(labels or {}), float(n))
        )


# Canonical season string used across every test; pins the default to
# the current season to mirror the AAP ``--season 2025-26`` examples.
_SEASON: str = "2025-26"


# Repeatedly-used checkpoint domain string. Always drawn from
# :mod:`config`, never hardcoded, so config changes cascade through
# tests without manual updates (Gate 12).
def _domain() -> str:
    """Return the Players checkpoint domain under test."""
    return config.DOMAIN_PLAYERS


# Schema-specified checkpoint keys (AAP §0.5.1.8). Composed from the
# canonical :data:`_SEASON` constant and the upstream endpoint label
# strings that :data:`pipelines.ingest_players._ENDPOINT_PLAN` emits,
# so a change to either the season pin or the endpoint-name vocabulary
# cascades through tests without manual updates.
_KEY_PRIMARY: str = f"leaguedashplayerstats:{_SEASON}"
_KEY_TRACKING: str = f"leaguedashptstats:{_SEASON}"


def _make_tracking_fakes(recording_writer, recording_checkpoint):
    """Wrap factory-produced fakes with shared event-log instrumentation.

    Used by the Rule 5 interleaving assertion
    (:func:`test_rule5_ordering_write_precedes_mark_for_each_endpoint`)
    to capture the cross-fake operation sequence in a single list
    rather than two separately-timestamped records. Monkey-patches
    the instance methods of freshly-produced
    :class:`~tests.conftest.RecordingWriter` and
    :class:`~tests.conftest.RecordingCheckpoint` instances rather than
    subclassing so this helper is resilient to the conftest's exact
    class exposure surface (module-scope attribute, factory closure,
    etc.) — the classes do not need to be importable by name.

    Parameters
    ----------
    recording_writer:
        The :func:`tests.conftest.recording_writer` fixture, a
        zero-argument factory returning a fresh
        :class:`~tests.conftest.RecordingWriter`.
    recording_checkpoint:
        The :func:`tests.conftest.recording_checkpoint` fixture, a
        zero-argument factory returning a fresh
        :class:`~tests.conftest.RecordingCheckpoint`.

    Returns
    -------
    Tuple[Any, Any, List[Tuple[str, str]]]
        ``(writer, checkpoint, event_log)``. ``event_log`` is a
        shared list populated in strict invocation order with
        ``("write", csv_name)`` entries (appended after each
        successful ``writer.write`` call) and ``("mark", key)``
        entries (appended after each ``checkpoint.mark_completed``
        call). Exact list equality against the expected ``write →
        mark → write → mark`` sequence catches any batch-write
        regression that would violate Rule 5's
        resumability guarantee (AAP §0.7.2.5).
    """
    writer = recording_writer()
    checkpoint = recording_checkpoint()
    event_log: List[Tuple[str, str]] = []

    orig_write = writer.write
    orig_mark = checkpoint.mark_completed

    def write_and_log(df, name, season):
        result = orig_write(df, name, season)
        event_log.append(("write", str(name)))
        return result

    def mark_and_log(domain, key):
        orig_mark(domain, key)
        event_log.append(("mark", str(key)))

    writer.write = write_and_log
    checkpoint.mark_completed = mark_and_log
    return writer, checkpoint, event_log


# ---------------------------------------------------------------------------
# TestEndpointPlanShape
# ---------------------------------------------------------------------------


class TestEndpointPlanShape:
    """Covers the shape and contents of ``_ENDPOINT_PLAN``.

    The plan tuple is the source of truth for which endpoints this
    pipeline iterates; pinning its shape prevents accidental
    regressions (e.g. an endpoint being dropped or re-ordered during
    a refactor).
    """

    def test_plan_is_immutable_tuple(self) -> None:
        """The plan MUST be a ``tuple`` (not a ``list``) so it is
        immutable at module level.
        """
        assert isinstance(ingest_players._ENDPOINT_PLAN, tuple)

    def test_plan_has_exactly_two_entries(self) -> None:
        """F-009 maps exactly to two default endpoints — the base
        aggregate and the tracking feed.
        """
        assert len(ingest_players._ENDPOINT_PLAN) == 2

    def test_each_entry_is_a_three_tuple(self) -> None:
        """Each plan entry MUST be a ``(fetch_callable, csv_name,
        endpoint_label)`` triple.
        """
        for entry in ingest_players._ENDPOINT_PLAN:
            assert isinstance(entry, tuple)
            assert len(entry) == 3

    def test_primary_entry_binds_to_players_wrapper_and_csv(self) -> None:
        """Entry [0] MUST route :func:`fetch_leaguedashplayerstats`
        into ``config.CSV_PLAYERS`` under the literal endpoint label
        ``"leaguedashplayerstats"``.
        """
        from endpoints.players import fetch_leaguedashplayerstats

        fetch_fn, csv_name, endpoint_label = ingest_players._ENDPOINT_PLAN[0]
        assert fetch_fn is fetch_leaguedashplayerstats
        assert csv_name == config.CSV_PLAYERS
        assert endpoint_label == "leaguedashplayerstats"

    def test_tracking_entry_binds_to_ptstats_wrapper_and_csv(self) -> None:
        """Entry [1] MUST route :func:`fetch_leaguedashptstats` into
        ``config.CSV_PLAYER_TRACKING`` under the literal endpoint
        label ``"leaguedashptstats"``.
        """
        from endpoints.players import fetch_leaguedashptstats

        fetch_fn, csv_name, endpoint_label = ingest_players._ENDPOINT_PLAN[1]
        assert fetch_fn is fetch_leaguedashptstats
        assert csv_name == config.CSV_PLAYER_TRACKING
        assert endpoint_label == "leaguedashptstats"

    def test_all_csv_names_are_strings_from_config(self) -> None:
        """Every plan entry's ``csv_name`` MUST originate from
        :mod:`config` (verified by value equality; the literal strings
        ``"players"`` and ``"player_tracking"`` MUST NOT appear as
        hardcoded CSV names).
        """
        known_csv_names = {config.CSV_PLAYERS, config.CSV_PLAYER_TRACKING}
        for _, csv_name, _ in ingest_players._ENDPOINT_PLAN:
            assert isinstance(csv_name, str)
            assert csv_name in known_csv_names

    def test_all_endpoint_labels_are_lowercase_strings(self) -> None:
        """Endpoint labels MUST be lowercase (matches the NBA Stats
        URL path convention and the checkpoint-key convention).
        """
        for _, _, endpoint_label in ingest_players._ENDPOINT_PLAN:
            assert isinstance(endpoint_label, str)
            assert endpoint_label == endpoint_label.lower()
            assert endpoint_label != ""

    def test_all_fetch_callables_are_callable(self) -> None:
        """Every plan entry's ``fetch_callable`` MUST be callable."""
        for fetch_fn, _, _ in ingest_players._ENDPOINT_PLAN:
            assert callable(fetch_fn)


# ---------------------------------------------------------------------------
# TestNominalRun
# ---------------------------------------------------------------------------


class TestNominalRun:
    """Covers the end-to-end nominal path: both endpoints pending,
    both produce data, both write, both checkpoint.
    """

    def test_writes_both_csvs_in_plan_order(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Nominal run MUST call ``writer.write`` exactly twice, in
        ``_ENDPOINT_PLAN`` order: first ``config.CSV_PLAYERS``, then
        ``config.CSV_PLAYER_TRACKING``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        assert len(writer.writes) == 2
        assert writer.writes[0]["name"] == config.CSV_PLAYERS
        assert writer.writes[1]["name"] == config.CSV_PLAYER_TRACKING

    def test_propagates_season_to_writer(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Each ``writer.write`` call MUST receive the exact season
        string passed to ``run()``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        for record in writer.writes:
            assert record["season"] == _SEASON

    def test_marks_checkpoints_in_plan_order(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """Rule 5 — Nominal run MUST mark both endpoint checkpoints
        in plan order AFTER their respective writer calls succeed.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        expected_primary = checkpoint_keys["players_primary"].format(
            season=_SEASON
        )
        expected_tracking = checkpoint_keys["players_tracking"].format(
            season=_SEASON
        )
        assert checkpoint.marks == [
            (_domain(), expected_primary),
            (_domain(), expected_tracking),
        ]

    def test_delegates_fetches_to_real_endpoint_wrappers(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The pipeline MUST call ``client.get`` for BOTH endpoint
        labels in the canonical lowercase form so the real
        :mod:`endpoints.players` wrappers route correctly through
        :class:`NBAClient`.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        called_endpoints = [endpoint for endpoint, _ in client.calls]
        assert "leaguedashplayerstats" in called_endpoints
        assert "leaguedashptstats" in called_endpoints
        # Ordering: primary before tracking.
        primary_idx = called_endpoints.index("leaguedashplayerstats")
        tracking_idx = called_endpoints.index("leaguedashptstats")
        assert primary_idx < tracking_idx

    def test_returns_none(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """``run()`` MUST return ``None`` on success (contract is
        side-effectful; the signature pins the return type to
        ``None``).
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        result = run(client, writer, checkpoint, _SEASON)

        assert result is None


# ---------------------------------------------------------------------------
# TestWriterContract
# ---------------------------------------------------------------------------


class TestWriterContract:
    """Covers the per-call ``writer.write(df, name, season)`` contract
    and the DataFrame content routed to each artifact.
    """

    def test_first_write_contains_primary_rows(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The first ``writer.write`` MUST receive a DataFrame whose
        body originates from the ``leaguedashplayerstats`` payload
        (identified by the ``PTS`` column on the primary fixture).
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        df_primary = writer.writes[0]["df"]
        assert "PTS" in df_primary.columns
        # 3 primary rows, season column + 4 payload columns = 5 cols.
        assert len(df_primary) == 3
        assert df_primary.shape[1] == 5

    def test_second_write_contains_tracking_rows(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The second ``writer.write`` MUST receive a DataFrame whose
        body originates from the ``leaguedashptstats`` payload
        (identified by the ``DIST_FEET`` and ``AVG_SPEED`` columns
        unique to the tracking fixture).
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        df_tracking = writer.writes[1]["df"]
        assert "DIST_FEET" in df_tracking.columns
        assert "AVG_SPEED" in df_tracking.columns
        assert len(df_tracking) == 2

    def test_writer_receives_dataframe_instance(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Every ``writer.write`` call MUST receive a
        :class:`pandas.DataFrame` — NEVER a raw dict, list, or other
        structure.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        for record in writer.writes:
            assert isinstance(record["df"], pd.DataFrame)

    def test_writer_row_count_matches_dataframe_length(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """RecordingWriter's ``rows`` field (captured from ``len(df)``)
        MUST equal the DataFrame's row count.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=4),
            "leaguedashptstats": _make_tracking_payload(rows=3),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        assert writer.writes[0]["rows"] == 4
        assert writer.writes[1]["rows"] == 3


# ---------------------------------------------------------------------------
# TestSeasonColumnInjection
# ---------------------------------------------------------------------------


class TestSeasonColumnInjection:
    """Covers :func:`_ensure_season_column`'s interaction with the
    pipeline — the lowercase ``season`` column guarantee.
    """

    def test_injects_season_when_absent(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When the upstream payload omits any season-like column,
        ``writer.write`` MUST receive a DataFrame with a lowercase
        ``season`` column inserted at position 0 whose every value
        equals the caller-supplied season.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(
                rows=3, with_season_column=False
            ),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        df_primary = writer.writes[0]["df"]
        assert df_primary.columns[0] == "season"
        assert df_primary["season"].tolist() == [_SEASON, _SEASON, _SEASON]

    def test_preserves_upstream_uppercase_season_column(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When the upstream payload already contains a ``SEASON``
        column (uppercase), the helper MUST NOT insert a second
        lowercase column. The upstream values MUST be preserved.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(
                rows=3, with_season_column=True
            ),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        df_primary = writer.writes[0]["df"]
        # Exactly ONE season-like column.
        season_cols = [c for c in df_primary.columns if c.lower() == "season"]
        assert len(season_cols) == 1
        # The upstream SEASON column is preserved — no "season"
        # (lowercase) was inserted.
        assert "SEASON" in df_primary.columns
        assert "season" not in df_primary.columns
        assert df_primary["SEASON"].tolist() == ["2025-26"] * 3

    def test_season_column_applies_to_both_artifacts(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Both ``players.csv`` and ``player_tracking.csv`` MUST have
        a season-like column after the pipeline writes them.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        for record in writer.writes:
            df = record["df"]
            season_cols = [c for c in df.columns if c.lower() == "season"]
            assert len(season_cols) >= 1

    def test_uses_run_season_value_for_injection(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The injected ``season`` column MUST use the value passed
        to ``run()``, not any value embedded in the payload
        ``parameters`` block.
        """
        custom_season = "2023-24"
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=2),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, custom_season)

        for record in writer.writes:
            df = record["df"]
            assert df["season"].tolist() == [custom_season] * len(df)


# ---------------------------------------------------------------------------
# TestResumeBehavior
# ---------------------------------------------------------------------------


class TestResumeBehavior:
    """Covers the per-endpoint checkpoint resume paths."""

    def test_skips_primary_when_checkpointed(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When only the primary endpoint is pre-checkpointed, the
        tracking endpoint MUST still run.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )

        run(client, writer, checkpoint, _SEASON)

        # Only the tracking endpoint was written.
        assert len(writer.writes) == 1
        assert writer.writes[0]["name"] == config.CSV_PLAYER_TRACKING
        # Only one new checkpoint mark.
        assert checkpoint.marks == [
            (_domain(), checkpoint_keys["players_tracking"].format(season=_SEASON))
        ]

    def test_skips_tracking_when_checkpointed(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When only the tracking endpoint is pre-checkpointed, the
        primary endpoint MUST still run.
        """
        tracking_key = checkpoint_keys["players_tracking"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [tracking_key]}
        )

        run(client, writer, checkpoint, _SEASON)

        assert len(writer.writes) == 1
        assert writer.writes[0]["name"] == config.CSV_PLAYERS
        assert checkpoint.marks == [
            (_domain(), checkpoint_keys["players_primary"].format(season=_SEASON))
        ]

    def test_full_resume_noop(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When both endpoint checkpoints are present, the pipeline
        MUST emit zero writer calls and zero checkpoint marks.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        tracking_key = checkpoint_keys["players_tracking"].format(season=_SEASON)
        client = recording_client()
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key, tracking_key]}
        )

        run(client, writer, checkpoint, _SEASON)

        assert writer.writes == []
        assert checkpoint.marks == []

    def test_is_completed_invoked_for_every_endpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """``checkpoint.is_completed`` MUST be called once per
        endpoint regardless of resume state, with the exact
        ``(domain, key)`` tuple.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        expected_primary = checkpoint_keys["players_primary"].format(season=_SEASON)
        expected_tracking = checkpoint_keys["players_tracking"].format(season=_SEASON)
        assert checkpoint.checks == [
            (_domain(), expected_primary),
            (_domain(), expected_tracking),
        ]

    def test_client_not_called_for_skipped_endpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When an endpoint is skipped via checkpoint, the pipeline
        MUST NOT invoke its fetch callable (no wasted HTTP traffic).
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )

        run(client, writer, checkpoint, _SEASON)

        endpoints_called = [endpoint for endpoint, _ in client.calls]
        assert "leaguedashplayerstats" not in endpoints_called
        assert "leaguedashptstats" in endpoints_called


# ---------------------------------------------------------------------------
# TestCheckpointKeySchema
# ---------------------------------------------------------------------------


class TestCheckpointKeySchema:
    """Covers the literal ``"<endpoint_label>:<season>"`` checkpoint
    key schema.
    """

    @pytest.mark.parametrize("season", [
        "2025-26", "2024-25", "2019-20", "2010-11",
    ])
    def test_primary_key_format(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
        season: str,
    ) -> None:
        """Primary endpoint key MUST be
        ``"leaguedashplayerstats:<season>"``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, season)

        expected = checkpoint_keys["players_primary"].format(season=season)
        assert expected == f"leaguedashplayerstats:{season}"
        assert (_domain(), expected) in checkpoint.marks

    @pytest.mark.parametrize("season", [
        "2025-26", "2024-25", "2019-20", "2010-11",
    ])
    def test_tracking_key_format(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
        season: str,
    ) -> None:
        """Tracking endpoint key MUST be
        ``"leaguedashptstats:<season>"``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, season)

        expected = checkpoint_keys["players_tracking"].format(season=season)
        assert expected == f"leaguedashptstats:{season}"
        assert (_domain(), expected) in checkpoint.marks

    def test_domain_is_config_domain_players(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Every checkpoint call MUST use ``config.DOMAIN_PLAYERS``
        as the ``domain`` argument — literally ``"players"``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        for domain_arg, _ in checkpoint.marks:
            assert domain_arg == config.DOMAIN_PLAYERS
        for domain_arg, _ in checkpoint.checks:
            assert domain_arg == config.DOMAIN_PLAYERS


# ---------------------------------------------------------------------------
# TestExceptionPropagation (Rule 6 does NOT apply)
# ---------------------------------------------------------------------------


class TestExceptionPropagation:
    """Covers Rule 6 NON-application: exceptions MUST propagate.

    Per AAP §0.7.2.6 the ``try/except Exception`` fail-safe wrapper
    is scoped to :mod:`pipelines.ingest_games` only; every other
    pipeline, including this one, propagates fetch / normalize /
    write exceptions to the caller unwrapped.
    """

    def test_first_endpoint_fetch_exception_propagates(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When the primary fetch raises, ``run()`` MUST re-raise the
        SAME exception and NEVER call the writer or checkpoint.
        """
        err = RuntimeError("synthetic primary-endpoint HTTP failure")
        client = recording_client(raise_for={"leaguedashplayerstats": err})
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        with pytest.raises(RuntimeError) as excinfo:
            run(client, writer, checkpoint, _SEASON)

        assert excinfo.value is err
        assert writer.writes == []
        assert checkpoint.marks == []

    def test_second_endpoint_fetch_exception_propagates(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When the tracking fetch raises AFTER the primary succeeds,
        ``run()`` MUST propagate the exception BUT the primary
        checkpoint MUST remain marked (Rule 5 durability).
        """
        err = RuntimeError("synthetic tracking-endpoint HTTP failure")
        client = recording_client(
            responses={"leaguedashplayerstats": _make_primary_payload(rows=2)},
            raise_for={"leaguedashptstats": err},
        )
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        with pytest.raises(RuntimeError) as excinfo:
            run(client, writer, checkpoint, _SEASON)

        assert excinfo.value is err
        # The primary write DID happen.
        assert len(writer.writes) == 1
        assert writer.writes[0]["name"] == config.CSV_PLAYERS
        # The primary checkpoint is marked; tracking is not.
        assert checkpoint.marks == [
            (_domain(), checkpoint_keys["players_primary"].format(season=_SEASON))
        ]

    def test_writer_exception_propagates_and_blocks_checkpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Rule 5 — When ``writer.write`` raises, ``run()`` MUST
        propagate the exception AND MUST NOT mark the checkpoint for
        the failed endpoint.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
        })
        writer = recording_writer(raise_on=config.CSV_PLAYERS)
        checkpoint = recording_checkpoint()

        with pytest.raises(RuntimeError) as excinfo:
            run(client, writer, checkpoint, _SEASON)

        # The writer raised a RuntimeError with the synthetic message.
        assert "synthetic write failure" in str(excinfo.value)
        # Checkpoint for primary was NOT marked because write failed.
        assert checkpoint.marks == []

    def test_second_writer_exception_preserves_first_checkpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """Rule 5 durability — When the second writer call fails,
        the first endpoint's checkpoint MUST remain marked.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer(raise_on=config.CSV_PLAYER_TRACKING)
        checkpoint = recording_checkpoint()

        with pytest.raises(RuntimeError):
            run(client, writer, checkpoint, _SEASON)

        assert checkpoint.marks == [
            (_domain(), checkpoint_keys["players_primary"].format(season=_SEASON))
        ]

    def test_resume_after_failure_retries_only_failed_endpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """Simulates: run-1 succeeds on primary but fails on tracking;
        run-2 (with the run-1 checkpoint state) MUST skip primary and
        retry only tracking.
        """
        # Run 1 — tracking endpoint fails.
        err = RuntimeError("run1 tracking failure")
        client1 = recording_client(
            responses={
                "leaguedashplayerstats": _make_primary_payload(),
            },
            raise_for={"leaguedashptstats": err},
        )
        writer1 = recording_writer()
        checkpoint1 = recording_checkpoint()
        with pytest.raises(RuntimeError):
            run(client1, writer1, checkpoint1, _SEASON)

        # Run 2 — inherits run-1's completed-set.
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        tracking_payload = _make_tracking_payload()
        client2 = recording_client(
            responses={"leaguedashptstats": tracking_payload},
        )
        writer2 = recording_writer()
        checkpoint2 = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )
        run(client2, writer2, checkpoint2, _SEASON)

        # Run 2 only wrote tracking.
        assert len(writer2.writes) == 1
        assert writer2.writes[0]["name"] == config.CSV_PLAYER_TRACKING
        # Run 2 did NOT call the primary fetch.
        run2_endpoints = [endpoint for endpoint, _ in client2.calls]
        assert "leaguedashplayerstats" not in run2_endpoints
        assert "leaguedashptstats" in run2_endpoints

    def test_no_try_except_in_run_source(self) -> None:
        """Rule 6 source-level check — the body of ``run()`` MUST
        NOT contain ``try:`` / ``except`` blocks. A negative-space
        AST-free check on the function source text.
        """
        source = inspect.getsource(run)
        # Strip the docstring first; the docstring legitimately
        # mentions try/except as a discussion of what this module
        # does NOT do. The docstring is always the first triple-
        # quoted literal that follows the ``def`` signature.
        stripped = _strip_docstring(source)
        # Count actual ``try:`` occurrences.
        lines = [line for line in stripped.splitlines()
                 if line.strip().startswith("try:") or
                 line.strip().startswith("except ") or
                 line.strip().startswith("except:")]
        assert lines == [], (
            "run() body contains try/except; Rule 6 forbids it here "
            f"(matches: {lines})"
        )


# ---------------------------------------------------------------------------
# TestMetricsIntegration
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    """Covers the ``pipeline_rows_written_total`` counter emission."""

    def test_exactly_two_inc_calls_on_nominal_run(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Nominal run MUST increment the counter exactly twice,
        once per endpoint.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        assert len(metrics.calls) == 2

    def test_counter_name_is_pipeline_rows_written_total(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Both ``inc`` calls MUST use the exact counter name
        ``"pipeline_rows_written_total"``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        for name, _, _ in metrics.calls:
            assert name == "pipeline_rows_written_total"

    def test_labels_identify_pipeline_and_artifact(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Every ``inc`` call MUST carry labels
        ``{"pipeline": "ingest_players", "artifact": "<csv_name>.csv"}``.

        The ``pipeline`` and ``artifact`` label names are the documented
        operator contract (``docs/OBSERVABILITY.md`` §
        ``pipeline_rows_written_total`` and
        ``docs/dashboards/operator_dashboard.json`` L477). The
        ``artifact`` value MUST include the ``.csv`` suffix so the
        dashboard legend renders operator-meaningful filenames rather
        than stems.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        artifacts_seen = set()
        for _, labels, _ in metrics.calls:
            assert labels["pipeline"] == "ingest_players"
            assert "artifact" in labels
            artifacts_seen.add(labels["artifact"])
        assert artifacts_seen == {
            f"{config.CSV_PLAYERS}.csv",
            f"{config.CSV_PLAYER_TRACKING}.csv",
        }

    def test_n_equals_row_count(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The ``n`` argument to ``inc`` MUST equal ``len(df)`` for
        each write — proving the per-endpoint contribution to the
        counter scales with ingested rows.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=5),
            "leaguedashptstats": _make_tracking_payload(rows=3),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        by_artifact: Dict[str, float] = {
            labels["artifact"]: n
            for _, labels, n in metrics.calls
        }
        assert by_artifact[f"{config.CSV_PLAYERS}.csv"] == 5.0
        assert by_artifact[f"{config.CSV_PLAYER_TRACKING}.csv"] == 3.0

    def test_skipped_endpoints_do_not_increment_metric(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When an endpoint is skipped via checkpoint resume, its
        ``inc`` call MUST NOT fire.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        assert len(metrics.calls) == 1
        _, labels, _ = metrics.calls[0]
        assert labels["artifact"] == f"{config.CSV_PLAYER_TRACKING}.csv"

    def test_default_metrics_registry_wired_in(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When ``metrics`` is omitted, :func:`run` MUST increment the
        real :data:`utils.metrics.registry` counter (verified via
        :meth:`get_counter_value`).
        """
        from utils.metrics import registry

        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=2),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        primary_value = registry.get_counter_value(
            "pipeline_rows_written_total",
            labels={
                "pipeline": "ingest_players",
                "artifact": f"{config.CSV_PLAYERS}.csv",
            },
        )
        tracking_value = registry.get_counter_value(
            "pipeline_rows_written_total",
            labels={
                "pipeline": "ingest_players",
                "artifact": f"{config.CSV_PLAYER_TRACKING}.csv",
            },
        )
        assert primary_value == 2.0
        assert tracking_value == 2.0

    def test_no_metric_increment_on_writer_failure(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When a writer raises, the metric counter for that endpoint
        MUST NOT be incremented — the increment is AFTER write
        succeeds.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
        })
        writer = recording_writer(raise_on=config.CSV_PLAYERS)
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        with pytest.raises(RuntimeError):
            run(client, writer, checkpoint, _SEASON, metrics=metrics)

        assert metrics.calls == []


# ---------------------------------------------------------------------------
# TestLoggerBehavior
# ---------------------------------------------------------------------------


class TestLoggerBehavior:
    """Covers the logger-injection seam and the structured-log event
    sequence.
    """

    def test_emits_pipeline_start_event(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The first INFO event on an explicit spy logger MUST be
        ``pipeline.start`` with the domain and season positional
        args.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        assert len(spy.records) >= 1
        level, msg, args = spy.records[0]
        assert level == "INFO"
        assert msg.startswith("pipeline.start")
        assert args == (config.DOMAIN_PLAYERS, _SEASON)

    def test_emits_pipeline_complete_event_last(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """The last event on an explicit spy logger MUST be
        ``pipeline.complete`` with domain, season, wrote, skipped
        positional args.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        level, msg, args = spy.records[-1]
        assert level == "INFO"
        assert msg.startswith("pipeline.complete")
        # (domain, season, wrote=2, skipped=0)
        assert args == (config.DOMAIN_PLAYERS, _SEASON, 2, 0)

    def test_emits_pipeline_wrote_event_per_endpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """One ``pipeline.wrote`` INFO event MUST be emitted per
        successful write (twice on a nominal run).
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(rows=3),
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        wrote_events = [
            (level, msg, args)
            for level, msg, args in spy.records
            if msg.startswith("pipeline.wrote")
        ]
        assert len(wrote_events) == 2
        # Verify the csv_name is threaded through as one of the args.
        csv_names_logged = set()
        for _, _, args in wrote_events:
            csv_names_logged.update(
                arg for arg in args
                if isinstance(arg, str)
                and arg in {config.CSV_PLAYERS, config.CSV_PLAYER_TRACKING}
            )
        assert csv_names_logged == {config.CSV_PLAYERS, config.CSV_PLAYER_TRACKING}

    def test_emits_pipeline_skip_event_for_resumed_endpoint(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When an endpoint is pre-checkpointed, a ``pipeline.skip``
        INFO event MUST be emitted for it, while ``pipeline.wrote``
        MUST NOT fire for that endpoint.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        skip_events = [r for r in spy.records if r[1].startswith("pipeline.skip")]
        wrote_events = [r for r in spy.records if r[1].startswith("pipeline.wrote")]
        assert len(skip_events) == 1
        assert len(wrote_events) == 1
        # The skip event names the primary key.
        _, _, skip_args = skip_events[0]
        assert primary_key in skip_args

    def test_complete_event_reports_skip_and_wrote_counts(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """``pipeline.complete`` MUST carry correct ``wrote`` and
        ``skipped`` counts under resume.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        client = recording_client(responses={
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key]}
        )
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        last_level, last_msg, last_args = spy.records[-1]
        assert last_level == "INFO"
        assert last_msg.startswith("pipeline.complete")
        # (domain, season, wrote=1, skipped=1)
        assert last_args == (config.DOMAIN_PLAYERS, _SEASON, 1, 1)

    def test_full_resume_records_skip_count_two(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        checkpoint_keys,
    ) -> None:
        """When both endpoints are already checkpointed, the
        ``pipeline.complete`` summary MUST report ``wrote=0`` and
        ``skipped=2``.
        """
        primary_key = checkpoint_keys["players_primary"].format(season=_SEASON)
        tracking_key = checkpoint_keys["players_tracking"].format(season=_SEASON)
        client = recording_client()
        writer = recording_writer()
        checkpoint = recording_checkpoint(
            completed={_domain(): [primary_key, tracking_key]}
        )
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        _, last_msg, last_args = spy.records[-1]
        assert last_msg.startswith("pipeline.complete")
        assert last_args == (config.DOMAIN_PLAYERS, _SEASON, 0, 2)

    def test_default_logger_is_module_level_fallback(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """When ``logger`` is not provided, ``run()`` MUST fall back
        to :data:`pipelines.ingest_players._LOGGER`. Verified by
        confirming the module-level ``_LOGGER`` is a proper
        :class:`logging.LoggerAdapter` and the call succeeds without
        error.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        # Call without ``logger`` — no exception means the fallback
        # was successfully resolved.
        run(client, writer, checkpoint, _SEASON)

        assert isinstance(ingest_players._LOGGER, logging.LoggerAdapter)

    def test_explicit_logger_is_used_not_module_fallback(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
    ) -> None:
        """Providing an explicit ``logger`` argument MUST route every
        log call through the supplied adapter and NOT through the
        module-level ``_LOGGER``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": _make_primary_payload(),
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        spy = _LoggerSpy()

        run(client, writer, checkpoint, _SEASON, logger=spy)

        # At least 4 events on the spy: start + 2 * wrote + complete.
        assert len(spy.records) >= 4


# ---------------------------------------------------------------------------
# TestEmptyPayload
# ---------------------------------------------------------------------------


class TestEmptyPayload:
    """Covers the zero-row path — an upstream endpoint returns an
    empty rowset.
    """

    def test_empty_payload_still_writes_header(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        sample_empty_payload,
    ) -> None:
        """When ``leaguedashplayerstats`` returns an empty rowset,
        the pipeline MUST still write an (empty) DataFrame and still
        checkpoint the endpoint to avoid busy-looping on a
        permanently empty upstream.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": sample_empty_payload,
            "leaguedashptstats": _make_tracking_payload(),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()

        run(client, writer, checkpoint, _SEASON)

        # Both endpoints wrote and checkpointed.
        assert len(writer.writes) == 2
        assert writer.writes[0]["name"] == config.CSV_PLAYERS
        assert writer.writes[0]["rows"] == 0
        assert len(checkpoint.marks) == 2

    def test_empty_payload_metric_n_is_zero(
        self,
        recording_client,
        recording_writer,
        recording_checkpoint,
        sample_empty_payload,
    ) -> None:
        """An empty rowset MUST produce an ``inc`` call with
        ``n=0``.
        """
        client = recording_client(responses={
            "leaguedashplayerstats": sample_empty_payload,
            "leaguedashptstats": _make_tracking_payload(rows=2),
        })
        writer = recording_writer()
        checkpoint = recording_checkpoint()
        metrics = _MetricsSpy()

        run(client, writer, checkpoint, _SEASON, metrics=metrics)

        by_artifact = {
            labels["artifact"]: n for _, labels, n in metrics.calls
        }
        assert by_artifact[f"{config.CSV_PLAYERS}.csv"] == 0.0
        assert by_artifact[f"{config.CSV_PLAYER_TRACKING}.csv"] == 2.0


# ---------------------------------------------------------------------------
# TestModuleInvariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """Negative-space structural invariants on
    :mod:`pipelines.ingest_players`.
    """

    def test_all_exports_only_run(self) -> None:
        """``__all__`` MUST expose exactly one symbol: ``run``.

        Helpers (``_select_primary_df``, ``_ensure_season_column``)
        and the module-level constants (``_ENDPOINT_PLAN``,
        ``_LOGGER``) remain module-private.
        """
        assert ingest_players.__all__ == ["run"]

    def test_module_does_not_import_requests(self) -> None:
        """Rule 1 — Single HTTP Client (AAP §0.7.2.1). The pipeline
        module MUST NOT import ``requests``, ``urllib``, or
        ``httpx`` at module level; all transport goes through
        :class:`NBAClient`.
        """
        assert not hasattr(ingest_players, "requests")
        assert not hasattr(ingest_players, "urllib")
        assert not hasattr(ingest_players, "httpx")

    def test_module_uses_module_level_logger(self) -> None:
        """The module MUST declare a private ``_LOGGER`` attribute."""
        assert hasattr(ingest_players, "_LOGGER")

    def test_module_logger_is_logger_adapter(self) -> None:
        """``_LOGGER`` MUST be a :class:`logging.LoggerAdapter`
        instance so the logger-injection seam in ``run()`` type-checks
        and the correlation-ID adapter's ``process()`` hook runs.
        """
        assert isinstance(ingest_players._LOGGER, logging.LoggerAdapter)

    def test_module_logger_name_matches_module_path(self) -> None:
        """The underlying logger of ``_LOGGER`` MUST be named
        ``"pipelines.ingest_players"`` so log records are filterable
        by module namespace in aggregation tools.
        """
        adapter = ingest_players._LOGGER
        underlying = getattr(adapter, "logger", adapter)
        assert underlying.name == "pipelines.ingest_players"

    def test_run_is_callable(self) -> None:
        """:func:`pipelines.ingest_players.run` MUST be callable."""
        assert callable(run)
        assert callable(ingest_players.run)
        assert ingest_players.run is run

    def test_run_signature(self) -> None:
        """``run`` MUST expose the documented positional + keyword
        signature ``(client, writer, checkpoint, season, logger=None,
        metrics=None)``.
        """
        sig = inspect.signature(run)
        params = list(sig.parameters.values())
        names = [p.name for p in params]
        assert names == [
            "client", "writer", "checkpoint",
            "season", "logger", "metrics",
        ]
        # ``season`` is positional-or-keyword, no default.
        season_param = sig.parameters["season"]
        assert season_param.default is inspect.Parameter.empty
        # ``logger`` and ``metrics`` have defaults of None.
        assert sig.parameters["logger"].default is None
        assert sig.parameters["metrics"].default is None

    def test_run_returns_none_type_annotation(self) -> None:
        """``run``'s return annotation MUST be ``None`` (side-
        effectful).

        Because the implementation uses ``from __future__ import
        annotations`` (PEP 563 postponed evaluation), the return
        annotation is stored as the string ``"None"`` rather than
        the :class:`NoneType` object. We therefore accept either
        the literal ``None`` / :class:`NoneType` values (if
        annotations were eagerly evaluated) or the string form
        ``"None"`` (under PEP 563).
        """
        sig = inspect.signature(run)
        assert sig.return_annotation in (None, type(None), "None")

    def test_source_contains_no_to_csv_literal(self) -> None:
        """Rule 7 — Pluggable Storage (AAP §0.7.2.7). The module
        source MUST NOT contain a literal ``to_csv`` token outside
        comments — Rule 7 requires that only
        :class:`storage.csv_writer.CSVWriter` calls
        :meth:`DataFrame.to_csv`.

        The docstring on this module does not reference ``to_csv`` so
        a simple substring check is sufficient.
        """
        source = inspect.getsource(ingest_players)
        assert "to_csv" not in source, (
            "pipelines.ingest_players contains a literal 'to_csv' — "
            "Rule 7 requires all CSV emission go through BaseWriter."
        )

    def test_source_contains_no_requests_get(self) -> None:
        """Rule 1 source-level — No direct ``requests.get`` / POST
        / Session call in this module.
        """
        source = inspect.getsource(ingest_players)
        forbidden = [
            "requests.get",
            "requests.post",
            "requests.Session",
        ]
        for token in forbidden:
            assert token not in source, (
                f"pipelines.ingest_players contains forbidden HTTP "
                f"token {token!r} — Rule 1 mandates all transport "
                f"routes through NBAClient."
            )

    def test_source_body_contains_no_try_except(self) -> None:
        """Rule 6 non-application — The ``run()`` function body MUST
        NOT contain a ``try:``/``except`` block. Rule 6 scopes the
        fail-safe wrapper to :mod:`pipelines.ingest_games` only.
        """
        source = inspect.getsource(run)
        stripped = _strip_docstring(source)
        for line in stripped.splitlines():
            s = line.strip()
            assert not s.startswith("try:"), (
                "run() body contains 'try:' — Rule 6 does not "
                "apply to the Players pipeline."
            )
            assert not (s.startswith("except ") or s == "except:"), (
                "run() body contains 'except' — Rule 6 does not "
                "apply to the Players pipeline."
            )

    def test_helpers_are_module_private(self) -> None:
        """The internal helpers MUST be exposed at module scope
        (so the unit tests can reach them) but MUST NOT be part of
        ``__all__``.
        """
        assert hasattr(ingest_players, "_select_primary_df")
        assert hasattr(ingest_players, "_ensure_season_column")
        assert hasattr(ingest_players, "_ENDPOINT_PLAN")
        for name in (
            "_select_primary_df",
            "_ensure_season_column",
            "_ENDPOINT_PLAN",
            "_LOGGER",
        ):
            assert name not in ingest_players.__all__


# ---------------------------------------------------------------------------
# TestHelpers — direct unit tests on the private helper functions
# ---------------------------------------------------------------------------


class TestSelectPrimaryDf:
    """Directly exercises :func:`_select_primary_df`.

    The helper is private but unit-testing it in isolation gives
    explicit coverage of the empty-dict fallback path separately
    from the pipeline orchestration above.
    """

    def test_returns_first_dataframe(self) -> None:
        """For a non-empty dict, returns the DataFrame associated
        with the first key in iteration order.
        """
        df_a = pd.DataFrame({"x": [1, 2]})
        df_b = pd.DataFrame({"y": [3, 4]})
        dfs = {"first": df_a, "second": df_b}

        result = ingest_players._select_primary_df(dfs)

        assert result is df_a

    def test_returns_empty_dataframe_on_empty_dict(self) -> None:
        """For an empty dict, returns a fresh empty
        :class:`pandas.DataFrame`.
        """
        result = ingest_players._select_primary_df({})

        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_single_entry_dict(self) -> None:
        """For a one-key dict, returns that single DataFrame."""
        df = pd.DataFrame({"a": [1]})
        result = ingest_players._select_primary_df({"only": df})

        assert result is df


class TestEnsureSeasonColumn:
    """Directly exercises :func:`_ensure_season_column`."""

    def test_inserts_lowercase_season_when_absent(self) -> None:
        """The helper MUST insert ``season`` at column position 0
        when no season-like column is present.
        """
        df = pd.DataFrame({"PLAYER_ID": [1, 2], "PTS": [10.0, 20.0]})

        result = ingest_players._ensure_season_column(df, "2025-26")

        assert result.columns[0] == "season"
        assert result["season"].tolist() == ["2025-26", "2025-26"]

    def test_does_not_mutate_input(self) -> None:
        """The helper MUST NOT mutate the caller's DataFrame when
        inserting the season column.
        """
        df = pd.DataFrame({"PLAYER_ID": [1]})
        original_columns = list(df.columns)

        ingest_players._ensure_season_column(df, "2025-26")

        assert list(df.columns) == original_columns
        assert "season" not in df.columns

    def test_preserves_lowercase_season(self) -> None:
        """When the DataFrame already contains a lowercase ``season``
        column, the helper MUST NOT re-insert it.
        """
        df = pd.DataFrame({"season": ["1999-00"], "pts": [10.0]})

        result = ingest_players._ensure_season_column(df, "2025-26")

        assert list(result.columns) == ["season", "pts"]
        assert result["season"].tolist() == ["1999-00"]

    def test_preserves_uppercase_season_column(self) -> None:
        """Case-insensitive match — ``SEASON`` in the DataFrame
        blocks a lowercase-``season`` insertion.
        """
        df = pd.DataFrame({"SEASON": ["1999-00"], "pts": [10.0]})

        result = ingest_players._ensure_season_column(df, "2025-26")

        # No new 'season' column inserted; upstream SEASON preserved.
        assert "season" not in result.columns
        assert "SEASON" in result.columns
        assert result["SEASON"].tolist() == ["1999-00"]

    def test_matches_season_id_case_insensitively_does_not_inject(self) -> None:
        """A column named ``SEASON_ID`` has lower-case form
        ``season_id`` — not literally ``season`` — so this edge case
        exercises whether the helper does EXACT lowercase match
        versus prefix match. The implementation uses exact match
        (``c.lower() == "season"``), so ``SEASON_ID`` does NOT
        satisfy the guard and a new ``season`` column IS prepended.
        """
        df = pd.DataFrame({"SEASON_ID": ["22025"], "pts": [10.0]})

        result = ingest_players._ensure_season_column(df, "2025-26")

        assert result.columns[0] == "season"
        assert "SEASON_ID" in result.columns
        assert result["season"].tolist() == ["2025-26"]

    def test_empty_dataframe_still_gets_column(self) -> None:
        """Even an empty DataFrame MUST get a ``season`` column
        (matching the header-only artifact policy for zero-row
        upstream data).
        """
        df = pd.DataFrame(columns=["PLAYER_ID", "PTS"])

        result = ingest_players._ensure_season_column(df, "2025-26")

        assert result.columns[0] == "season"
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Schema-specified module-level tests (AAP §0.5.1.8 Group 8)
#
# These five test functions cover the explicit Phase 10 validation
# matrix from the file's agent prompt:
#
#   pytest -k "rule5"       -> finds EXACTLY one test
#       (test_rule5_ordering_write_precedes_mark_for_each_endpoint)
#   pytest -k "idempotency" -> finds EXACTLY two tests
#       (test_run_partial_idempotency_skips_primary_runs_tracking,
#        test_run_full_idempotency_no_writes_when_both_checkpointed)
#
# They supplement — rather than replace — the classful test matrix
# above, anchoring the Rule 5 interleaving invariant (AAP §0.7.2.5)
# and the resume / no-op idempotency contracts (AAP §0.7.2.5) as
# first-class, discoverable selector surfaces.
# ---------------------------------------------------------------------------


def test_run_happy_path_writes_both_csvs_and_marks_both_checkpoints(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
):
    """Both endpoints fetched, both CSVs written, both keys checkpointed.

    Exercises the 2-endpoint iteration of
    :data:`pipelines.ingest_players._ENDPOINT_PLAN` with no prior
    checkpoint state: the pipeline must fetch both
    ``leaguedashplayerstats`` and ``leaguedashptstats``, emit both
    ``players.csv`` and ``player_tracking.csv`` through
    :meth:`~tests.conftest.RecordingWriter.write` in order, and mark
    both keys under ``config.DOMAIN_PLAYERS`` in the checkpoint. The
    metrics boundary receives exactly two
    ``pipeline_rows_written_total`` increments (one per endpoint pull).
    """
    client = recording_client(
        responses={
            "leaguedashplayerstats": sample_single_table_payload,
            "leaguedashptstats": sample_single_table_payload,
        }
    )
    writer = recording_writer()
    checkpoint = recording_checkpoint()
    metrics_mock = MagicMock()

    ingest_players.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
        metrics=metrics_mock,
    )

    # ---- Both endpoints were called ----
    called_endpoints = [call[0] for call in client.calls]
    assert "leaguedashplayerstats" in called_endpoints, (
        f"expected leaguedashplayerstats call; got {called_endpoints!r}"
    )
    assert "leaguedashptstats" in called_endpoints, (
        f"expected leaguedashptstats call; got {called_endpoints!r}"
    )

    # ---- Writer received exactly two calls in the canonical order ----
    assert len(writer.writes) == 2, (
        f"expected 2 writes; got {len(writer.writes)}"
    )
    names_in_order = [w["name"] for w in writer.writes]
    assert names_in_order == [config.CSV_PLAYERS, config.CSV_PLAYER_TRACKING], (
        f"expected order [{config.CSV_PLAYERS}, {config.CSV_PLAYER_TRACKING}]; "
        f"got {names_in_order!r}"
    )
    for record in writer.writes:
        assert record["season"] == _SEASON
        assert record["rows"] > 0

    # ---- Checkpoint marked BOTH keys under DOMAIN_PLAYERS, in order ----
    assert checkpoint.marks == [
        (config.DOMAIN_PLAYERS, _KEY_PRIMARY),
        (config.DOMAIN_PLAYERS, _KEY_TRACKING),
    ], f"expected marks in order; got {checkpoint.marks!r}"

    # ---- Both is_completed probes occurred ----
    check_keys = [check[1] for check in checkpoint.checks]
    assert _KEY_PRIMARY in check_keys
    assert _KEY_TRACKING in check_keys

    # ---- Metrics: exactly two pipeline_rows_written_total increments ----
    rows_written_calls = [
        c for c in metrics_mock.inc.call_args_list
        if c.args and c.args[0] == "pipeline_rows_written_total"
    ]
    assert len(rows_written_calls) == 2, (
        f"expected 2 pipeline_rows_written_total increments; "
        f"got {metrics_mock.inc.call_args_list!r}"
    )


def test_rule5_ordering_write_precedes_mark_for_each_endpoint(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
):
    """Rule 5 (AAP §0.7.2.5): interleaved write → mark sequence per endpoint.

    For each endpoint the operation order is
    ``write → mark_completed``, and the SECOND endpoint's fetch must
    not happen until the FIRST endpoint's ``mark_completed`` has
    been recorded. :func:`_make_tracking_fakes` wraps the conftest-
    produced fakes with a shared event log; this test asserts the
    exact interleaved sequence::

        write(players) → mark(players_key) →
        write(player_tracking) → mark(player_tracking_key)

    Any deviation (for example, ``write → write → mark → mark``)
    indicates a batch-write regression that would violate the
    resumability guarantee: if the second write fails, the first
    must already be marked complete so a resume run skips it.
    """
    client = recording_client(
        responses={
            "leaguedashplayerstats": sample_single_table_payload,
            "leaguedashptstats": sample_single_table_payload,
        }
    )
    writer, checkpoint, event_log = _make_tracking_fakes(
        recording_writer, recording_checkpoint,
    )

    ingest_players.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    expected = [
        ("write", config.CSV_PLAYERS),
        ("mark", _KEY_PRIMARY),
        ("write", config.CSV_PLAYER_TRACKING),
        ("mark", _KEY_TRACKING),
    ]
    assert event_log == expected, (
        f"Rule 5 ordering violated.\n"
        f"Expected: {expected!r}\nActual:   {event_log!r}"
    )


def test_run_partial_idempotency_skips_primary_runs_tracking(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
):
    """Partial idempotency: PRIMARY pre-checkpointed, only TRACKING runs.

    Mirrors the most common crash-and-resume scenario: the
    ``leaguedashplayerstats`` endpoint succeeded and was marked
    complete, then the process died before
    ``leaguedashptstats`` could write. On restart, the pipeline
    must skip the primary endpoint (no redundant HTTP call, no
    duplicate CSV write, no duplicate mark) and attempt only the
    tracking endpoint. This is the primary motivation for
    per-endpoint checkpointing; the single-endpoint idempotency
    tests elsewhere in this module do not prove that the loop
    correctly handles a partially-completed plan.
    """
    client = recording_client(
        responses={
            "leaguedashplayerstats": sample_single_table_payload,
            "leaguedashptstats": sample_single_table_payload,
        }
    )
    writer = recording_writer()
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_PLAYERS: [_KEY_PRIMARY]},
    )

    ingest_players.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    # ---- Only the tracking endpoint was called ----
    called_endpoints = [call[0] for call in client.calls]
    assert "leaguedashplayerstats" not in called_endpoints, (
        f"primary endpoint was unexpectedly called; got {called_endpoints!r}"
    )
    assert "leaguedashptstats" in called_endpoints, (
        f"expected leaguedashptstats call; got {called_endpoints!r}"
    )

    # ---- Exactly one write: player_tracking ----
    assert len(writer.writes) == 1, (
        f"expected exactly 1 write; got {len(writer.writes)}"
    )
    assert writer.writes[0]["name"] == config.CSV_PLAYER_TRACKING
    assert writer.writes[0]["season"] == _SEASON

    # ---- Exactly one new mark: tracking ----
    assert checkpoint.marks == [
        (config.DOMAIN_PLAYERS, _KEY_TRACKING),
    ], f"expected only tracking mark; got {checkpoint.marks!r}"


def test_run_full_idempotency_no_writes_when_both_checkpointed(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
):
    """Full idempotency: BOTH endpoints pre-checkpointed → pure no-op.

    When both keys are already marked complete, ``ingest_players.run``
    performs no HTTP requests, no writes, and adds no new marks.
    This is the successful-resume-of-completed-run scenario: a
    subsequent invocation with the same ``--season`` and the
    ``checkpoint.json`` left behind by a prior successful run must
    be a pure no-op on the upstream and filesystem boundaries.
    Only the idempotency probes (``is_completed``) fire — the
    ``for`` loop still iterates the plan but skips every entry.
    """
    client = recording_client(
        responses={
            "leaguedashplayerstats": sample_single_table_payload,
            "leaguedashptstats": sample_single_table_payload,
        }
    )
    writer = recording_writer()
    checkpoint = recording_checkpoint(
        completed={config.DOMAIN_PLAYERS: [_KEY_PRIMARY, _KEY_TRACKING]},
    )

    ingest_players.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    assert client.calls == [], (
        f"no endpoint calls expected; got {client.calls!r}"
    )
    assert writer.writes == [], (
        f"no writes expected; got {writer.writes!r}"
    )
    assert checkpoint.marks == [], (
        f"no new marks expected; got {checkpoint.marks!r}"
    )

    # Both is_completed probes still happen before the skip decision
    check_keys = [check[1] for check in checkpoint.checks]
    assert _KEY_PRIMARY in check_keys
    assert _KEY_TRACKING in check_keys


def test_library_only_endpoints_not_invoked(
    recording_client,
    recording_writer,
    recording_checkpoint,
    sample_single_table_payload,
):
    """``leaguedashplayerclutch`` / ``playercareerstats`` /
    ``playergamelog`` are library-only wrappers (exposed by
    :mod:`endpoints.players` but NOT orchestrated by
    :data:`pipelines.ingest_players._ENDPOINT_PLAN`);
    :func:`pipelines.ingest_players.run` MUST NOT invoke them.

    Verifies the pipeline's documented scope boundary: only the two
    league-wide aggregate endpoints
    (``leaguedashplayerstats`` and ``leaguedashptstats``) are part of
    the orchestrated flow. The per-player and split-aware endpoints
    remain available to ad-hoc library consumers of
    :mod:`endpoints.players` but are never called implicitly during
    an ``ingest_players.run`` invocation — preventing accidental
    scope creep if the plan tuple is mis-edited.
    """
    client = recording_client(
        responses={
            "leaguedashplayerstats": sample_single_table_payload,
            "leaguedashptstats": sample_single_table_payload,
        }
    )
    writer = recording_writer()
    checkpoint = recording_checkpoint()

    ingest_players.run(
        client=client,
        writer=writer,
        checkpoint=checkpoint,
        season=_SEASON,
    )

    forbidden = {"leaguedashplayerclutch", "playercareerstats", "playergamelog"}
    called = {call[0] for call in client.calls}
    overlap = forbidden & called
    assert not overlap, (
        f"library-only endpoints invoked: {overlap!r}; "
        f"pipeline orchestrator should only invoke endpoints from "
        f"ingest_players._ENDPOINT_PLAN"
    )


# ---------------------------------------------------------------------------
# Module-level utility: strip Python triple-quoted docstring
# ---------------------------------------------------------------------------


def _strip_docstring(source: str) -> str:
    """Remove the leading triple-quoted docstring from a function's
    source.

    Used by the Rule 6 / Rule 7 source-level invariant tests so the
    docstring's legitimate discussion of ``try/except`` / ``to_csv``
    (as things the module does NOT do) does not create false
    positives in the negative-space substring checks.

    The implementation is intentionally simple: it finds the first
    triple-double-quote opening after a ``def`` or ``class``
    signature and returns the source with the whole docstring
    block removed. If no docstring is found, the source is returned
    unchanged.
    """
    # Triple quotes used in the codebase.
    triple = '"""'
    first = source.find(triple)
    if first == -1:
        return source
    # Find the closing triple quote. If the docstring is on one
    # line it's ``"""..."""`` — the ``second`` search must start
    # AFTER the opening quotes.
    second = source.find(triple, first + len(triple))
    if second == -1:
        return source
    return source[:first] + source[second + len(triple):]
