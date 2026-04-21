"""Unit tests for :mod:`run` — the Click CLI entry point (Gate 13).

Verifies the complete CLI contract documented in AAP §0.5.1.7 and
§0.7.5:

* All **nine** subcommands register and invoke cleanly:
  five domain subcommands (``players``, ``teams``, ``games``,
  ``lineups``, ``schedule``), the ``all`` aggregate subcommand, and
  three diagnostic subcommands (``health``, ``ready``, ``metrics``).
* Every data subcommand accepts ``--season STRING`` with a default
  sourced from :data:`config.DEFAULT_SEASON` (Gate 12 read-site).
* Each data subcommand dispatches to its corresponding
  ``pipelines.ingest_<domain>.run`` callable using the **kwargs-only**
  interface documented at ``run.py`` L236-L464, passing exactly the
  four runtime collaborators ``client``, ``writer``, ``checkpoint``,
  and ``season``.
* Metric emission satisfies the documented label contract in
  ``docs/OBSERVABILITY.md`` L215-L216 and ``operator_dashboard.json``
  L548-L549: ``pipeline_runs_total`` uses labels
  ``{"pipeline": "<canonical>", "outcome": "success"|"error"}`` where
  the canonical pipeline names are ``ingest_<domain>`` for the five
  domain subcommands and the literal string ``"all"`` for the ``all``
  subcommand.
* The ``all`` subcommand dispatches the five pipelines in the
  AAP §0.4.5 binding order: **schedule → games → teams → players →
  lineups**. Any pipeline failure aborts the remaining pipelines and
  re-raises after emitting the ``outcome="error"`` counter increment.
* Domain subcommand failures emit the ``outcome="error"`` counter
  increment and re-raise — the CLI never silently swallows an
  exception (Rule 6 applies only inside ``ingest_games`` per-game
  iteration, not at the CLI boundary).
* The three diagnostic subcommands do NOT mint a correlation ID
  (``correlation_id.get()`` remains empty after invocation), emit
  deterministic output shapes documented in ``docs/OBSERVABILITY.md``,
  and translate the readiness aggregate into an exit code
  (``ready=0``, ``not_ready=1``).

Gate and rule mapping
---------------------
* **Gate 13** (CLI subcommand dispatch) — Phases 2, 4, 5, 6, 7
* **Gate 9** (registration-invocation pairing) — Phase 1
* **Gate 12** (config propagation) — Phase 3 (``--season`` default
  sourced from :data:`config.DEFAULT_SEASON`)
* **Rule 1** (single HTTP client) — no ``requests.get``/``requests.post``
  surface appears in the CLI layer; verified indirectly by mocking
  the pipelines and confirming they are the only call sites
* **Rule 6** exclusivity — the CLI layer re-raises every exception
  except within ``ingest_games``; Phase 5 confirms the domain
  subcommands propagate failures

Design notes
------------
* **No mocking libraries** are used. The :class:`_RecordingRun` spy
  class is a hand-written callable replacement for
  ``ingest_<domain>.run`` that records every invocation's kwargs and
  optionally raises a pre-configured side-effect. This matches the
  convention in ``tests/conftest.py`` which provides handwritten
  ``RecordingClient``/``RecordingWriter``/``RecordingCheckpoint``
  spies (see AAP §0.7.3: "tests must be deterministic without
  third-party mock frameworks").
* The :pyfixture:`isolated_filesystem` fixture redirects
  :data:`config.OUTPUT_DIR`, :data:`config.CHECKPOINT_PATH`,
  :data:`config.LOG_DIR`, and :data:`config.LOG_FILE` beneath
  ``tmp_path`` so no real ``output/`` or ``logs/`` artifacts are
  produced by any test in this module.
* **``catch_exceptions=True``** is the CliRunner default and is
  preserved. Failure-path tests inspect ``result.exception`` and
  ``result.exit_code`` to confirm the CLI re-raised rather than
  silently exited 0.
* **Autouse resets** — the ``_reset_correlation_id_between_tests``,
  ``_reset_metrics_registry_between_tests``, and
  ``_reset_logger_handlers_between_tests`` fixtures in
  ``tests/conftest.py`` guarantee each test starts with an empty
  correlation context, a freshly-zeroed metrics registry (pre-declared
  counters retain their ``# HELP``/``# TYPE`` rows), and detached
  logger handlers. Tests therefore do NOT depend on execution order.
* **ContextVar persistence across CliRunner** — empirically verified:
  :class:`click.testing.CliRunner.invoke` does not create a new
  :class:`contextvars.Context`, so a ``ContextVar.set()`` call inside
  a subcommand persists after ``invoke`` returns. This is what allows
  correlation-ID-presence assertions in Phase 6.
"""

from __future__ import annotations

import json
import logging  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
import re
import sys  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
from pathlib import Path  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
from typing import Any, Callable, Dict, List, Optional

import click  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
import pytest
from click.testing import CliRunner  # noqa: F401  # Canonical import template; retained for sibling-test consistency.

import config
import run
from pipelines import (
    ingest_games,
    ingest_lineups,
    ingest_players,
    ingest_schedule,
    ingest_teams,
)
from utils import correlation, health, metrics


# ---------------------------------------------------------------------------
# Helper: recording spy for ``pipelines.ingest_<domain>.run``
# ---------------------------------------------------------------------------


class _RecordingRun:
    """Callable spy that replaces ``pipelines.ingest_<domain>.run``.

    Records every invocation's keyword arguments for subsequent
    assertions. Supports an optional ``side_effect`` so failure-path
    tests can simulate a pipeline error without a real NBA Stats API
    call.

    Attributes:
        name: Canonical pipeline name (``"ingest_players"`` etc.);
            purely informational — used only in assertion failure
            messages.
        calls: List of captured kwargs dicts, one per invocation.
            Assertions typically inspect ``calls[0]`` directly.
        side_effect: Optional exception instance to raise on every
            invocation. ``None`` means the spy returns ``None``.
    """

    def __init__(
        self,
        name: str,
        side_effect: Optional[BaseException] = None,
    ) -> None:
        self.name = name
        self.calls: List[Dict[str, Any]] = []
        self.side_effect = side_effect

    def __call__(self, **kwargs: Any) -> None:
        """Record the invocation and optionally raise ``side_effect``.

        Accepts only keyword arguments — this intentionally mirrors
        the signature enforcement at ``run.py`` L240-L244 where every
        dispatch site uses keyword arguments exclusively. If a future
        refactor introduces a positional-argument call site, Python
        will raise :class:`TypeError` here, making the regression
        visible at test time.
        """
        self.calls.append(kwargs)
        if self.side_effect is not None:
            raise self.side_effect


def _install_pipeline_spies(
    monkeypatch: pytest.MonkeyPatch,
    failures: Optional[Dict[str, BaseException]] = None,
) -> Dict[str, _RecordingRun]:
    """Replace every ``pipelines.ingest_<domain>.run`` with a spy.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        failures: Optional mapping from canonical pipeline name to an
            exception instance. Each pipeline named in ``failures``
            receives a spy configured with that ``side_effect``;
            pipelines omitted from ``failures`` receive a no-op spy.

    Returns:
        Dict mapping canonical pipeline name (``"ingest_<domain>"``)
        to the installed :class:`_RecordingRun` spy. Tests use this
        dict to inspect ``.calls`` after invoking the CLI.

    Why patch the module attribute, not the symbol in ``run.py``?
        ``run.py`` does ``from pipelines import ingest_players`` so
        ``ingest_players`` inside ``run.py`` is a binding to the
        module object ``pipelines.ingest_players``. Monkeypatching
        ``ingest_players.run`` on the module object replaces the
        attribute that ``run.py`` resolves at invocation time — no
        need to reach into ``run.py``'s namespace.
    """
    failures = failures or {}
    spies: Dict[str, _RecordingRun] = {}
    mapping = {
        "ingest_schedule": ingest_schedule,
        "ingest_games": ingest_games,
        "ingest_teams": ingest_teams,
        "ingest_players": ingest_players,
        "ingest_lineups": ingest_lineups,
    }
    for canonical_name, module in mapping.items():
        spy = _RecordingRun(
            name=canonical_name,
            side_effect=failures.get(canonical_name),
        )
        monkeypatch.setattr(module, "run", spy, raising=True)
        spies[canonical_name] = spy
    return spies


# ---------------------------------------------------------------------------
# Phase 1 — CLI group metadata (Gate 9: registration-invocation pairing)
# ---------------------------------------------------------------------------


def test_cli_help_exits_zero(cli_runner: CliRunner) -> None:
    """``cli --help`` exits 0 and renders without a stack trace.

    Verifies the Click group is well-formed. A configuration error
    (duplicate command name, bad decorator argument) would surface as
    a non-zero exit code from Click's help renderer.
    """
    result = cli_runner.invoke(run.cli, ["--help"])
    assert result.exit_code == 0, (
        f"cli --help failed with exit_code={result.exit_code}: {result.output!r}"
    )
    assert result.exception is None


def test_cli_help_lists_all_nine_subcommands(cli_runner: CliRunner) -> None:
    """The top-level ``--help`` lists every one of the nine subcommands.

    Gate 9 (registration-invocation pairing) requires that every
    pipeline callable is reachable from the CLI. The nine expected
    names are: five domain subcommands + ``all`` aggregate + three
    diagnostic subcommands.
    """
    result = cli_runner.invoke(run.cli, ["--help"])
    assert result.exit_code == 0
    expected_subcommands = {
        "players",
        "teams",
        "games",
        "lineups",
        "schedule",
        "all",
        "health",
        "ready",
        "metrics",
    }
    for subcommand in expected_subcommands:
        assert subcommand in result.output, (
            f"Subcommand {subcommand!r} missing from cli --help output: "
            f"{result.output!r}"
        )


@pytest.mark.parametrize(
    "subcommand",
    ["players", "teams", "games", "lineups", "schedule", "all"],
)
def test_data_subcommand_help_exits_zero(
    cli_runner: CliRunner,
    subcommand: str,
) -> None:
    """Each data subcommand's ``--help`` exits 0 and mentions ``--season``.

    The six data subcommands (five domain + ``all``) must accept the
    ``--season`` option. Gate 12 (config propagation) requires that
    the default value for ``--season`` be sourced from
    :data:`config.DEFAULT_SEASON`; the presence of the default value
    in the rendered help text is a visible confirmation of the bind.
    """
    result = cli_runner.invoke(run.cli, [subcommand, "--help"])
    assert result.exit_code == 0, (
        f"{subcommand} --help failed: exit_code={result.exit_code}, "
        f"output={result.output!r}"
    )
    assert "--season" in result.output
    assert config.DEFAULT_SEASON in result.output, (
        f"{subcommand} --help must surface config.DEFAULT_SEASON="
        f"{config.DEFAULT_SEASON!r} as the default value; got: {result.output!r}"
    )


@pytest.mark.parametrize(
    "subcommand",
    ["health", "ready", "metrics"],
)
def test_diagnostic_subcommand_help_exits_zero(
    cli_runner: CliRunner,
    subcommand: str,
) -> None:
    """Each diagnostic subcommand's ``--help`` exits 0.

    The three diagnostic subcommands are stateless — they do not
    accept a ``--season`` option (AAP §0.5.1.7 "Diagnostic subcommands
    are stateless").
    """
    result = cli_runner.invoke(run.cli, [subcommand, "--help"])
    assert result.exit_code == 0, (
        f"{subcommand} --help failed: exit_code={result.exit_code}, "
        f"output={result.output!r}"
    )
    # Negative-space assertion: diagnostic subcommands must NOT expose
    # ``--season`` (AAP §0.5.1.7). A future regression that adds the
    # option for parity with the data subcommands would be caught here.
    assert "--season" not in result.output


# ---------------------------------------------------------------------------
# Phase 2 — Domain subcommand dispatch (Gate 13 core)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_dispatches_to_pipeline(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001  # Fixture ensures no real FS I/O.
    subcommand: str,
    canonical_name: str,
) -> None:
    """Invoking ``<subcommand>`` calls the corresponding pipeline's ``run``.

    Core Gate 13 assertion: each domain subcommand dispatches to
    exactly the correct pipeline module's ``run()`` function.
    Spies for **all five** pipelines are installed so the test can
    assert that the target pipeline was called **and** that no other
    pipeline was accidentally dispatched — a negative-space guard
    against copy-paste bugs in ``run.py`` subcommand bodies.
    """
    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(
        run.cli,
        [subcommand, "--season", config.DEFAULT_SEASON],
    )
    assert result.exit_code == 0, (
        f"{subcommand} subcommand failed: exit_code={result.exit_code}, "
        f"exception={result.exception!r}"
    )
    # Positive assertion: target pipeline was invoked exactly once.
    assert len(spies[canonical_name].calls) == 1, (
        f"Expected exactly 1 invocation of {canonical_name}, "
        f"got {len(spies[canonical_name].calls)}"
    )
    # Negative-space: no other pipeline was invoked.
    for other_name, other_spy in spies.items():
        if other_name == canonical_name:
            continue
        assert len(other_spy.calls) == 0, (
            f"Subcommand {subcommand!r} unexpectedly invoked "
            f"{other_name}: {other_spy.calls!r}"
        )


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_passes_kwargs_only(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
    subcommand: str,
    canonical_name: str,
) -> None:
    """Dispatch uses the kwargs-only interface documented at run.py L240-L244.

    ``_RecordingRun.__call__`` accepts only keyword arguments. If a
    refactor introduces a positional dispatch call site, Python
    raises :class:`TypeError` and the test fails immediately — making
    the regression visible. This test additionally verifies the
    expected keys (``client``, ``writer``, ``checkpoint``, ``season``)
    are present with no extras — particularly ensuring ``logger`` and
    ``metrics`` are NOT passed (pipelines default these to ``None``
    per AAP §0.4.1.2).
    """
    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(
        run.cli,
        [subcommand, "--season", "2024-25"],
    )
    assert result.exit_code == 0, result.output
    call_kwargs = spies[canonical_name].calls[0]
    # Exact key set: four collaborators, nothing more.
    assert set(call_kwargs.keys()) == {"client", "writer", "checkpoint", "season"}, (
        f"Subcommand {subcommand!r} dispatched with unexpected kwargs: "
        f"{sorted(call_kwargs.keys())!r}"
    )
    # Confirm season was propagated verbatim.
    assert call_kwargs["season"] == "2024-25"


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_default_season_is_config_default(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
    subcommand: str,
    canonical_name: str,
) -> None:
    """Omitting ``--season`` defaults to :data:`config.DEFAULT_SEASON` (Gate 12).

    Verifies the read-site bind between ``run.py`` L229 (and siblings)
    and :data:`config.DEFAULT_SEASON`. The default is resolved by Click
    at option-parsing time, so the spy's captured ``season`` kwarg
    reflects whatever ``config.DEFAULT_SEASON`` was at invocation.
    """
    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, [subcommand])  # No --season argument.
    assert result.exit_code == 0, result.output
    assert spies[canonical_name].calls[0]["season"] == config.DEFAULT_SEASON


# ---------------------------------------------------------------------------
# Phase 3 — Collaborator types (explicit DI per D-011 / AAP §0.4.1.2)
# ---------------------------------------------------------------------------


def test_domain_subcommand_passes_concrete_collaborator_types(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """Dispatched ``client``/``writer``/``checkpoint`` are the expected types.

    ``_build_collaborators`` at run.py L88-L171 composes the four
    runtime objects. This test confirms each is the expected concrete
    class — a regression that substituted a mock or wrong type would
    fail here.
    """
    from api.nba_client import NBAClient
    from storage.csv_writer import CSVWriter
    from utils.checkpoint import CheckpointManager

    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, ["players"])
    assert result.exit_code == 0, result.output
    call_kwargs = spies["ingest_players"].calls[0]
    assert isinstance(call_kwargs["client"], NBAClient)
    assert isinstance(call_kwargs["writer"], CSVWriter)
    assert isinstance(call_kwargs["checkpoint"], CheckpointManager)


# ---------------------------------------------------------------------------
# Phase 4 — Metric label contract (success path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_success_emits_pipeline_runs_total(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
    subcommand: str,
    canonical_name: str,
) -> None:
    """Successful run increments ``pipeline_runs_total`` with documented labels.

    Verifies the label contract fix for CRITICAL #3: labels
    ``{"pipeline": "ingest_<domain>", "outcome": "success"}``. This
    is the exact shape the Grafana chart at
    ``docs/dashboards/operator_dashboard.json`` L548-L549 expects.
    """
    _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, [subcommand])
    assert result.exit_code == 0, result.output
    # Success counter with correct labels is incremented.
    success_value = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": canonical_name, "outcome": "success"},
    )
    assert success_value == 1.0, (
        f"Expected 1.0 success increment for pipeline={canonical_name}, "
        f"got {success_value}"
    )
    # Error counter is NOT incremented.
    error_value = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": canonical_name, "outcome": "error"},
    )
    assert error_value == 0.0


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_does_not_emit_legacy_labels(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
    subcommand: str,
    canonical_name: str,
) -> None:
    """Success path does NOT emit the legacy ``{domain, status}`` labels.

    Negative-space guard against CRITICAL #3 regression. The review
    finding flagged ``{"domain": "<d>", "status": "success"|"failure"}``
    as the broken shape. This test confirms those legacy labels are
    never emitted, even transitively.
    """
    _install_pipeline_spies(monkeypatch)
    cli_runner.invoke(run.cli, [subcommand])
    # The domain name stripped of the ``ingest_`` prefix matches the
    # CLI subcommand name (``players`` / ``teams`` / ...).
    short_name = canonical_name.removeprefix("ingest_")
    legacy_success = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"domain": short_name, "status": "success"},
    )
    legacy_failure = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"domain": short_name, "status": "failure"},
    )
    assert legacy_success == 0.0, (
        f"Legacy label shape must NOT be emitted; got {legacy_success} for "
        f"{{domain: {short_name}, status: success}}"
    )
    assert legacy_failure == 0.0


# ---------------------------------------------------------------------------
# Phase 5 — Metric label contract (failure path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand, canonical_name",
    [
        ("players", "ingest_players"),
        ("teams", "ingest_teams"),
        ("games", "ingest_games"),
        ("lineups", "ingest_lineups"),
        ("schedule", "ingest_schedule"),
    ],
)
def test_domain_subcommand_failure_emits_error_outcome(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
    subcommand: str,
    canonical_name: str,
) -> None:
    """Pipeline exception → ``pipeline_runs_total{outcome="error"}`` + re-raise.

    Tests three contract properties in a single invocation:
      1. The domain subcommand increments ``outcome="error"`` (not
         ``"failure"``) — CRITICAL #3 fix.
      2. The exception is re-raised (no silent swallow) — CliRunner
         captures it in ``result.exception`` with ``exit_code != 0``.
      3. The success counter for the same pipeline is NOT incremented.

    The failure is injected via a :class:`RuntimeError` — a benign
    exception subclass that is neither :class:`SystemExit` nor
    :class:`KeyboardInterrupt`, so Click's default ``standalone_mode``
    captures it and CliRunner populates ``result.exception``.
    """
    injected_error = RuntimeError(f"synthetic {canonical_name} failure")
    _install_pipeline_spies(monkeypatch, failures={canonical_name: injected_error})
    result = cli_runner.invoke(run.cli, [subcommand])
    # Exception was re-raised (CliRunner captured it).
    assert result.exit_code != 0, (
        f"Expected non-zero exit_code for failing {subcommand}; got 0"
    )
    assert result.exception is injected_error, (
        f"Expected re-raised exception to be the injected RuntimeError; "
        f"got {result.exception!r}"
    )
    # Error counter incremented with canonical labels.
    error_value = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": canonical_name, "outcome": "error"},
    )
    assert error_value == 1.0
    # Success counter NOT incremented.
    success_value = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": canonical_name, "outcome": "success"},
    )
    assert success_value == 0.0


# ---------------------------------------------------------------------------
# Phase 6 — ``all`` subcommand: binding order and aggregate metric
# ---------------------------------------------------------------------------


def test_all_subcommand_dispatches_every_pipeline_once(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``all`` invokes each of the five pipelines exactly once."""
    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, ["all", "--season", config.DEFAULT_SEASON])
    assert result.exit_code == 0, result.output
    for canonical_name, spy in spies.items():
        assert len(spy.calls) == 1, (
            f"all subcommand must invoke {canonical_name} exactly once; "
            f"got {len(spy.calls)}"
        )


def test_all_subcommand_dispatches_in_binding_order(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """The five pipelines are invoked in the AAP §0.4.5 binding order.

    Binding order: schedule → games → teams → players → lineups.
    This is the canonical contract documented at ``run.py`` L425-L431
    and AAP §0.4.1.1. Any permutation would be a regression of ADR
    D-008.

    A shared ``order_log`` list captures the sequence of invocations
    across the five spies; the test then asserts the list matches the
    canonical tuple ordering.
    """
    order_log: List[str] = []

    def make_recorder(name: str) -> Callable[..., None]:
        def _record(**kwargs: Any) -> None:
            order_log.append(name)
        return _record

    monkeypatch.setattr(ingest_schedule, "run", make_recorder("schedule"))
    monkeypatch.setattr(ingest_games, "run", make_recorder("games"))
    monkeypatch.setattr(ingest_teams, "run", make_recorder("teams"))
    monkeypatch.setattr(ingest_players, "run", make_recorder("players"))
    monkeypatch.setattr(ingest_lineups, "run", make_recorder("lineups"))

    result = cli_runner.invoke(run.cli, ["all"])
    assert result.exit_code == 0, result.output
    assert order_log == ["schedule", "games", "teams", "players", "lineups"], (
        f"all subcommand binding order must be schedule → games → teams "
        f"→ players → lineups (AAP §0.4.5); got {order_log!r}"
    )


def test_all_subcommand_propagates_season(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``all --season X`` passes ``season=X`` to every pipeline."""
    spies = _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, ["all", "--season", "2023-24"])
    assert result.exit_code == 0, result.output
    for canonical_name, spy in spies.items():
        assert spy.calls[0]["season"] == "2023-24", (
            f"Pipeline {canonical_name} received wrong season: "
            f"{spy.calls[0]['season']!r}"
        )


def test_all_subcommand_success_emits_aggregate_marker(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``all`` success increments ``pipeline_runs_total{pipeline="all",outcome="success"}``.

    Per AAP decision D-022 (and the ``run.py`` L458 comment), the
    ``all`` subcommand emits a special aggregate marker with
    ``pipeline="all"`` — not one of the canonical ``ingest_<domain>``
    names. This allows dashboards to distinguish per-pipeline from
    aggregate-run counters.
    """
    _install_pipeline_spies(monkeypatch)
    result = cli_runner.invoke(run.cli, ["all"])
    assert result.exit_code == 0, result.output
    # Aggregate marker counter.
    aggregate_success = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": "all", "outcome": "success"},
    )
    assert aggregate_success == 1.0
    # Aggregate error counter NOT incremented.
    aggregate_error = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": "all", "outcome": "error"},
    )
    assert aggregate_error == 0.0


def test_all_subcommand_does_not_emit_per_pipeline_runs_counter(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``all`` does NOT also emit per-pipeline ``pipeline_runs_total`` rows.

    The per-pipeline ``pipeline_runs_total`` counter is an artifact
    of the *domain subcommand* boundary only. When ``all`` dispatches
    pipelines internally, it does NOT emit the per-pipeline counters
    — only the aggregate ``pipeline="all"`` marker. This prevents
    double-counting when operators run both per-domain subcommands
    and the aggregate ``all`` on the same host.
    """
    _install_pipeline_spies(monkeypatch)
    cli_runner.invoke(run.cli, ["all"])
    for canonical_name in (
        "ingest_schedule",
        "ingest_games",
        "ingest_teams",
        "ingest_players",
        "ingest_lineups",
    ):
        per_pipeline_success = metrics.registry.get_counter_value(
            "pipeline_runs_total",
            {"pipeline": canonical_name, "outcome": "success"},
        )
        assert per_pipeline_success == 0.0, (
            f"all must not emit per-pipeline runs counter for "
            f"{canonical_name}; got {per_pipeline_success}"
        )


# ---------------------------------------------------------------------------
# Phase 7 — ``all`` subcommand: failure path
# ---------------------------------------------------------------------------


def test_all_subcommand_aborts_on_first_pipeline_failure(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """A pipeline failure inside ``all`` aborts the remaining pipelines.

    Binding order is schedule → games → teams → players → lineups.
    Injecting a failure on ``ingest_teams`` must prevent ``players``
    and ``lineups`` from being invoked. The exception is re-raised
    and captured by CliRunner.
    """
    injected_error = RuntimeError("synthetic teams failure")
    spies = _install_pipeline_spies(
        monkeypatch,
        failures={"ingest_teams": injected_error},
    )
    result = cli_runner.invoke(run.cli, ["all"])
    # Failure re-raised.
    assert result.exit_code != 0
    assert result.exception is injected_error
    # Pipelines up-to-and-including the failing one were invoked.
    assert len(spies["ingest_schedule"].calls) == 1
    assert len(spies["ingest_games"].calls) == 1
    assert len(spies["ingest_teams"].calls) == 1
    # Pipelines after the failing one were NOT invoked.
    assert len(spies["ingest_players"].calls) == 0
    assert len(spies["ingest_lineups"].calls) == 0


def test_all_subcommand_failure_emits_aggregate_error(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``all`` failure increments ``pipeline_runs_total{pipeline="all",outcome="error"}``.

    Verifies the aggregate error path for the Grafana alert rule
    ``PipelineErrorOutcome`` at ``operator_dashboard.md`` L77:
    ``increase(pipeline_runs_total{outcome="error"}[24h]) > 0``. If
    this counter is not emitted, the alert never fires (which was
    the original CRITICAL #3 bug).
    """
    _install_pipeline_spies(
        monkeypatch,
        failures={"ingest_schedule": RuntimeError("early failure")},
    )
    result = cli_runner.invoke(run.cli, ["all"])
    assert result.exit_code != 0
    aggregate_error = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": "all", "outcome": "error"},
    )
    assert aggregate_error == 1.0
    # Aggregate success NOT incremented on failure path.
    aggregate_success = metrics.registry.get_counter_value(
        "pipeline_runs_total",
        {"pipeline": "all", "outcome": "success"},
    )
    assert aggregate_success == 0.0


# ---------------------------------------------------------------------------
# Phase 8 — Correlation-ID behavior (Observability rule)
# ---------------------------------------------------------------------------


def test_domain_subcommand_mints_correlation_id(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """Domain subcommand mints and binds a UUID4-hex correlation ID.

    ``_build_collaborators`` at run.py L153-L154 calls
    :func:`correlation.new_correlation_id` followed by
    ``correlation.correlation_id.set(cid)``. After invocation, the
    ContextVar holds a 32-char lowercase-hex string — the AAP §0.7.3.1
    Observability-rule contract.

    The ``_reset_correlation_id_between_tests`` autouse fixture clears
    the ContextVar before the test, so the starting value is ``""``.
    """
    _install_pipeline_spies(monkeypatch)
    assert correlation.correlation_id.get() == ""  # Precondition.
    result = cli_runner.invoke(run.cli, ["players"])
    assert result.exit_code == 0, result.output
    cid_after = correlation.correlation_id.get()
    # UUID4.hex = 32 lowercase-hex characters.
    assert re.fullmatch(r"[0-9a-f]{32}", cid_after), (
        f"Expected 32-char lowercase-hex correlation ID; got {cid_after!r}"
    )


def test_domain_subcommand_correlation_id_is_unique_per_invocation(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """Two successive subcommand invocations produce distinct correlation IDs.

    Each CLI invocation must mint a fresh UUID4; reusing an ID across
    invocations would conflate logs from separate runs and violate
    the observability-rule guarantee that each run is independently
    traceable.
    """
    _install_pipeline_spies(monkeypatch)
    cli_runner.invoke(run.cli, ["players"])
    cid_first = correlation.correlation_id.get()
    # The autouse fixture does NOT reset inside a single test, so we
    # manually reset between invocations to simulate a fresh process.
    correlation.correlation_id.set("")
    _install_pipeline_spies(monkeypatch)  # Re-install spies (they're reset).
    cli_runner.invoke(run.cli, ["teams"])
    cid_second = correlation.correlation_id.get()
    assert cid_first != cid_second, (
        f"Two invocations produced identical correlation IDs: "
        f"{cid_first!r} == {cid_second!r}"
    )
    assert re.fullmatch(r"[0-9a-f]{32}", cid_second)


def test_diagnostic_subcommands_do_not_mint_correlation_id(
    cli_runner: CliRunner,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``health``/``ready``/``metrics`` leave correlation_id empty.

    Per the ``run.py`` L467-L479 comment block, diagnostic subcommands
    are stateless — they do NOT mint a correlation ID because they
    issue no HTTP traffic and generate no pipeline logs. An operator
    probing readiness on a fresh install must not see a spurious
    correlation-ID entry in the process-wide log context.
    """
    for subcommand in ("health", "ready", "metrics"):
        correlation.correlation_id.set("")  # Reset per iteration.
        cli_runner.invoke(run.cli, [subcommand])
        cid_after = correlation.correlation_id.get()
        assert cid_after == "", (
            f"Diagnostic subcommand {subcommand!r} must not mint a "
            f"correlation ID; got {cid_after!r}"
        )


# ---------------------------------------------------------------------------
# Phase 9 — Diagnostic subcommand: ``health``
# ---------------------------------------------------------------------------


def test_health_subcommand_exits_zero(cli_runner: CliRunner) -> None:
    """``health`` always exits 0 — liveness is unconditional.

    Per AAP §0.5.1.7, ``check_health`` never raises and always reports
    ``status="ok"``, so the subcommand's exit code is always 0. An
    operator running ``run.py health`` on any invocation can use
    ``$?`` to confirm the Python interpreter can execute code in this
    process.
    """
    result = cli_runner.invoke(run.cli, ["health"])
    assert result.exit_code == 0, (
        f"health must exit 0; got {result.exit_code}, "
        f"output={result.output!r}"
    )
    assert result.exception is None


def test_health_subcommand_emits_valid_json(cli_runner: CliRunner) -> None:
    """``health`` stdout is parseable JSON with the documented four keys."""
    result = cli_runner.invoke(run.cli, ["health"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    for key in ("status", "timestamp", "python_version", "component"):
        assert key in payload, f"health JSON missing key {key!r}"
    assert payload["component"] == "nba-data-ingestion-pipeline"


def test_health_subcommand_invokes_check_health(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``health`` dispatches to :func:`utils.health.check_health`.

    A negative-space sanity check that the callback wires to the
    correct function. A future refactor that accidentally called
    ``check_readiness`` would be caught here because the returned
    dict would lack ``python_version`` and contain ``checks``.
    """
    calls: List[None] = []
    original = health.check_health

    def _spy() -> Dict[str, Any]:
        calls.append(None)
        return original()

    monkeypatch.setattr(health, "check_health", _spy, raising=True)
    result = cli_runner.invoke(run.cli, ["health"])
    assert result.exit_code == 0
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Phase 10 — Diagnostic subcommand: ``ready``
# ---------------------------------------------------------------------------


def test_ready_subcommand_exits_zero_when_all_probes_pass(
    cli_runner: CliRunner,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``ready`` exits 0 when all four probes return ``status="ok"``.

    The ``isolated_filesystem`` fixture creates writable
    ``output/``/``logs/`` directories under ``tmp_path``, so all four
    probes (output_dir_writable, required_headers_present,
    rate_limit_configured, checkpoint_parseable) should pass on a
    clean filesystem.
    """
    result = cli_runner.invoke(run.cli, ["ready"])
    assert result.exit_code == 0, (
        f"ready must exit 0 on clean filesystem; got {result.exit_code}, "
        f"output={result.output!r}"
    )


def test_ready_subcommand_emits_aggregated_json(
    cli_runner: CliRunner,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``ready`` JSON has the four-probe structure documented in OBSERVABILITY.md."""
    result = cli_runner.invoke(run.cli, ["ready"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert "timestamp" in payload
    assert "checks" in payload
    assert isinstance(payload["checks"], dict)
    for probe_name in (
        "output_dir_writable",
        "required_headers_present",
        "rate_limit_configured",
        "checkpoint_parseable",
    ):
        assert probe_name in payload["checks"], (
            f"ready JSON missing probe {probe_name!r}"
        )
        assert payload["checks"][probe_name]["status"] == "ok", (
            f"probe {probe_name!r} unexpectedly not ok on clean filesystem: "
            f"{payload['checks'][probe_name]!r}"
        )


def test_ready_subcommand_exits_one_when_probe_fails(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    isolated_filesystem: Path,  # noqa: ARG001
) -> None:
    """``ready`` exits 1 when any probe returns ``status="fail"``.

    Forces failure by monkeypatching ``config.REQUIRED_HEADERS`` to
    an empty dict, which the ``_probe_required_headers`` probe flags
    as a configuration error. The aggregate status is then
    ``"not_ready"`` and the subcommand must exit 1 so orchestrators
    (systemd, docker healthcheck) can detect the failure.
    """
    monkeypatch.setattr(config, "REQUIRED_HEADERS", {}, raising=True)
    result = cli_runner.invoke(run.cli, ["ready"])
    assert result.exit_code == 1, (
        f"ready must exit 1 on probe failure; got {result.exit_code}, "
        f"output={result.output!r}"
    )
    # The JSON is still emitted so operators can diagnose which probe
    # failed. The SystemExit(1) happens AFTER click.echo().
    payload = json.loads(result.stdout)
    assert payload["status"] == "not_ready"
    assert payload["checks"]["required_headers_present"]["status"] == "fail"


# ---------------------------------------------------------------------------
# Phase 11 — Diagnostic subcommand: ``metrics``
# ---------------------------------------------------------------------------


def test_metrics_subcommand_exits_zero(cli_runner: CliRunner) -> None:
    """``metrics`` exits 0 — it is a pure read-only dump of the registry."""
    result = cli_runner.invoke(run.cli, ["metrics"])
    assert result.exit_code == 0, result.output


def test_metrics_subcommand_renders_prometheus_text(cli_runner: CliRunner) -> None:
    """``metrics`` output matches :meth:`MetricsRegistry.render_prometheus`.

    The render already ends with ``"\\n"``; ``click.echo(..., nl=False)``
    at run.py L540 suppresses the additional newline Click would
    otherwise prepend, so the final output is exactly the
    Prometheus-text-format string.
    """
    expected = metrics.registry.render_prometheus()
    result = cli_runner.invoke(run.cli, ["metrics"])
    assert result.exit_code == 0
    assert result.stdout == expected


def test_metrics_subcommand_output_contains_help_and_type_preambles(
    cli_runner: CliRunner,
) -> None:
    """``metrics`` output includes ``# HELP`` and ``# TYPE`` lines.

    Every pre-registered counter and histogram produces a preamble
    regardless of whether it has been incremented. A bare-registry
    exposition therefore MUST contain these preamble lines — their
    absence would indicate a registry-reset bug that also wiped the
    descriptions.
    """
    result = cli_runner.invoke(run.cli, ["metrics"])
    assert result.exit_code == 0
    assert "# HELP " in result.stdout
    assert "# TYPE " in result.stdout
    # Specific preambles for the observability-rule counters.
    for counter_name in (
        "pipeline_runs_total",
        "pipeline_rows_written_total",
        "games_failed_total",
        "nba_requests_total",
    ):
        assert f"# TYPE {counter_name} counter" in result.stdout, (
            f"Missing Prometheus TYPE preamble for {counter_name!r}"
        )


def test_metrics_subcommand_reflects_prior_increments(
    cli_runner: CliRunner,
) -> None:
    """``metrics`` exposition reflects the current registry state.

    Incrementing a counter before invoking the subcommand must be
    visible in the output; an isolation leak (e.g., a per-test
    registry copy that CLI doesn't see) would fail here.
    """
    metrics.registry.inc(
        "pipeline_runs_total",
        {"pipeline": "ingest_players", "outcome": "success"},
    )
    result = cli_runner.invoke(run.cli, ["metrics"])
    assert result.exit_code == 0
    # The Prometheus text format renders the increment as a line
    # ``pipeline_runs_total{pipeline="ingest_players",outcome="success"} 1``.
    # The label order inside ``{...}`` is controlled by
    # ``_freeze_labels`` (sorted alphabetically) — ``outcome`` follows
    # ``pipeline`` lexicographically.
    assert re.search(
        r'pipeline_runs_total\{[^}]*pipeline="ingest_players"[^}]*\}\s+1(\.0)?',
        result.stdout,
    ), (
        f"metrics output did not reflect the increment; got:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Phase 12 — Invariant / regression guards
# ---------------------------------------------------------------------------


def test_cli_module_defines_nine_commands() -> None:
    """The Click group has exactly nine registered subcommands.

    Inspects :attr:`run.cli.commands` directly to guarantee exactly
    the documented set. Extra commands would indicate a scope-creep
    regression; missing commands would fail Gate 13.
    """
    expected = {
        "players",
        "teams",
        "games",
        "lineups",
        "schedule",
        "all",
        "health",
        "ready",
        "metrics",
    }
    registered = set(run.cli.commands.keys())
    assert registered == expected, (
        f"run.cli.commands must equal {expected!r}; got {registered!r}"
    )


def test_all_subcommand_registered_under_literal_name_all() -> None:
    """The ``all`` subcommand's Click name is the literal string ``"all"``.

    The Python callback is named ``all_cmd`` (at run.py L420) to avoid
    shadowing the builtin :func:`all`, but the Click-level name is the
    bare string ``"all"`` so operators invoke ``python run.py all``.
    """
    assert "all" in run.cli.commands
    # Defensive: the registered command's callback is not the builtin.
    assert run.cli.commands["all"].callback is not all


def test_data_subcommands_accept_season_option() -> None:
    """Every data subcommand exposes a ``--season`` option.

    Walks the Click command objects for each data subcommand and
    asserts a :class:`click.Option` with the ``--season`` long form
    is present. Gate 12 requires that option default to
    :data:`config.DEFAULT_SEASON`.
    """
    for subcommand_name in ("players", "teams", "games", "lineups", "schedule", "all"):
        cmd = run.cli.commands[subcommand_name]
        season_opts = [
            param for param in cmd.params
            if isinstance(param, click.Option) and "--season" in param.opts
        ]
        assert len(season_opts) == 1, (
            f"Subcommand {subcommand_name!r} must expose exactly one "
            f"--season option; got {len(season_opts)}"
        )
        # Gate 12: the default is sourced from config.DEFAULT_SEASON at
        # command-definition time. A refactor that hardcoded the
        # season value would fail this assertion.
        assert season_opts[0].default == config.DEFAULT_SEASON


def test_diagnostic_subcommands_have_no_parameters() -> None:
    """``health``/``ready``/``metrics`` expose no Click parameters.

    The diagnostic subcommands are stateless and deterministic — they
    must accept no flags beyond the implicit ``--help`` (which Click
    registers globally and does NOT appear in ``cmd.params``).
    """
    for subcommand_name in ("health", "ready", "metrics"):
        cmd = run.cli.commands[subcommand_name]
        assert cmd.params == [], (
            f"Diagnostic subcommand {subcommand_name!r} must not expose "
            f"parameters; got {cmd.params!r}"
        )


def test_cli_module_does_not_import_requests() -> None:
    """Rule 1 enforcement at the CLI layer — ``run.py`` must not import requests.

    The CLI composes collaborators and dispatches — it must not make
    HTTP calls directly. A regression that added ``import requests``
    at module scope would be caught here.
    """
    import run as run_module
    assert "requests" not in dir(run_module), (
        "run.py must not expose a ``requests`` attribute (Rule 1 — "
        "single HTTP client lives only in api/nba_client.py)"
    )


def test_cli_module_exposes_expected_subcommand_callbacks() -> None:
    """Every registered command resolves to a Python callable.

    Click stores the callback on ``Command.callback``. If any command
    were mis-registered with ``callback=None`` (a missing
    ``@cli.command`` decorator arg), invocation would raise
    :class:`TypeError` at runtime. This test catches the
    configuration error at collection time.
    """
    for subcommand_name in run.cli.commands:
        cmd = run.cli.commands[subcommand_name]
        assert callable(cmd.callback), (
            f"Subcommand {subcommand_name!r} has non-callable callback "
            f"{cmd.callback!r}"
        )


def test_pipelines_run_attribute_is_callable_on_every_module() -> None:
    """Every ``pipelines.ingest_<domain>`` module exposes a callable ``run``.

    Mirror of Gate 9 (registration-invocation pairing): the CLI
    dispatches to ``<module>.run`` via keyword arguments, so the
    attribute must exist and be callable on every domain module.
    This test confirms the contract surface before dispatch rather
    than relying on an AttributeError at invocation time.
    """
    for module in (ingest_schedule, ingest_games, ingest_teams, ingest_players, ingest_lineups):
        assert hasattr(module, "run"), (
            f"Module {module.__name__} missing 'run' attribute"
        )
        assert callable(module.run), (
            f"Module {module.__name__}.run is not callable: {module.run!r}"
        )
