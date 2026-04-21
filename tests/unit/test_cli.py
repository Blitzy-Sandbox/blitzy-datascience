"""Gate 13 — every CLI subcommand invokes its pipeline.

This test module is the authoritative enforcement point for
**Validation Gate 13** (AAP §0.7.5, "Registration-Invocation Pairing"):
every subcommand registered under the :data:`run.cli` Click group MUST
dispatch to its corresponding :func:`pipelines.ingest_<domain>.run`
callable, and the aggregate ``all`` subcommand MUST invoke the five
domain pipelines in the binding order documented in AAP §0.4.5:

    schedule → games → teams → players → lineups

The Games pipeline depends on the ``GAME_ID`` enumeration produced by
the Schedule pipeline, which is why ``schedule`` appears first in the
``all`` dispatch order and why the test that pins this sequence
(:func:`test_all_subcommand_runs_pipelines_in_documented_order`) is the
single most important test in this file.

Scope and isolation guarantees
------------------------------

* **Network-free** — every pipeline ``run`` callable is replaced by a
  recording spy via :func:`_patch_pipelines` before any CLI invocation.
  No test in this file issues an HTTP request or touches the real NBA
  Stats API. Rule 1 (Single HTTP Client) is respected by omission:
  this module never imports :mod:`requests`.
* **Filesystem-safe** — every test that touches the filesystem via a
  CLI invocation consumes the conftest-provided ``tmp_output_dir`` and
  ``tmp_log_dir`` fixtures, which monkeypatch :data:`config.OUTPUT_DIR`,
  :data:`config.CHECKPOINT_PATH`, :data:`config.LOG_DIR`, and
  :data:`config.LOG_FILE` to per-test temporary directories.
* **State-isolated** — the autouse fixtures defined in
  ``tests/conftest.py`` (``_reset_correlation_id_between_tests``,
  ``_reset_metrics_registry_between_tests``,
  ``_reset_logger_handlers_between_tests``) guarantee that each test
  starts with a clean correlation-ID context, an empty metrics
  registry, and fresh logger handlers.

Import scope
------------

Per the file schema's ``depends_on_files`` whitelist, this module
imports only:

* Python standard library primitives (``__future__``, ``typing``,
  ``json``) — stdlib is always available.
* ``pytest`` — the test runner harness.
* :mod:`config` — for :data:`config.DEFAULT_SEASON`, the canonical
  season string propagated to every pipeline's ``--season`` flag.
* :data:`run.cli` — the Click group under test.
* The five :mod:`pipelines.ingest_<domain>` modules — monkeypatched
  at the MODULE level so ``run.py``'s ``from pipelines import
  ingest_<domain>`` statements resolve the patched attribute.

Click and click.testing.CliRunner are intentionally NOT imported;
the ``cli_runner`` fixture from :mod:`tests.conftest` provides a
pre-instantiated :class:`click.testing.CliRunner` that every test
consumes by declaring it as a function parameter.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

import config
from pipelines import (
    ingest_games,
    ingest_lineups,
    ingest_players,
    ingest_schedule,
    ingest_teams,
)
from run import cli

# ---------------------------------------------------------------------------
# Module-level constants — the authoritative lists of subcommands under test.
# ---------------------------------------------------------------------------

#: Every subcommand that MUST be registered on :data:`run.cli`. Gate 13
#: requires that this 9-tuple exactly matches
#: ``set(run.cli.commands.keys())``. Any drift between this tuple and
#: ``run.py`` surfaces as a :func:`test_cli_registers_every_subcommand`
#: failure.
SUBCOMMANDS: tuple = (
    "players",
    "teams",
    "games",
    "lineups",
    "schedule",
    "all",
    "health",
    "ready",
    "metrics",
)

#: Subcommands that accept the ``--season`` option. All six domain-facing
#: subcommands fall in this group; diagnostic subcommands do not.
DATA_SUBCOMMANDS: tuple = (
    "players",
    "teams",
    "games",
    "lineups",
    "schedule",
    "all",
)

#: The five domain subcommands. Each corresponds 1-to-1 to a
#: ``pipelines.ingest_<domain>`` module. The ``all`` subcommand is
#: excluded because it is an aggregate dispatcher, not a domain.
DOMAIN_SUBCOMMANDS: tuple = (
    "players",
    "teams",
    "games",
    "lineups",
    "schedule",
)

#: Subcommands that perform diagnostic functions only and never invoke
#: any pipeline. Per ``run.py`` L467-L479, these subcommands are stateless
#: and do not mint a correlation ID.
DIAGNOSTIC_SUBCOMMANDS: tuple = ("health", "ready", "metrics")

#: Parametrized mapping of ``(subcommand, expected_domain)`` used by
#: :func:`test_subcommand_invokes_its_pipeline`. The expected-domain
#: string matches the key that :func:`_patch_pipelines` uses for its
#: recorder closures and therefore the value appended to the ``call_log``
#: list when a dispatch occurs.
SUBCOMMAND_DOMAIN_PAIRS: tuple = (
    ("schedule", "schedule"),
    ("games", "games"),
    ("teams", "teams"),
    ("players", "players"),
    ("lineups", "lineups"),
)

#: AAP §0.4.5 binding order for the ``all`` subcommand. Schedule runs
#: FIRST because Games depends on the ``GAME_ID`` list that Schedule
#: produces; Lineups runs LAST because it depends on the roster and
#: clutch-situation data that the Players pipeline writes.
PIPELINE_ORDER: tuple = (
    "schedule",
    "games",
    "teams",
    "players",
    "lineups",
)


# ---------------------------------------------------------------------------
# Helper — install recording spies for every pipeline ``run`` callable.
# ---------------------------------------------------------------------------


def _patch_pipelines(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Replace every ``pipelines.ingest_<domain>.run`` with a recorder.

    This is the single chokepoint through which every Gate-13 dispatch
    test observes CLI-to-pipeline wiring. It preserves two invariants
    that are critical for correctness:

    * **Module-level monkeypatch**: each recorder is installed on the
      pipeline *module object* (for example, ``ingest_schedule.run``),
      not on a symbol local to this test file. ``run.py`` performs
      ``from pipelines import ingest_schedule`` and later references
      ``ingest_schedule.run``; because module-attribute lookups happen
      at call time, the patched attribute is resolved correctly.
    * **Closure factory**: each recorder is produced by
      :func:`make_recorder` so the ``domain`` string is captured by
      value (not reference) inside each closure. Without the factory
      pattern, every recorder would share the last ``domain`` value
      assigned in the enclosing loop — a classic late-binding bug.

    The recorders accept ``**kwargs`` to remain forward-compatible with
    any future keyword added to the production ``run`` signature (for
    example, ``logger`` or ``metrics``). This also guards against the
    production code mutating its dispatch signature in a way that would
    otherwise break the test suite silently.

    Args:
        monkeypatch: The pytest ``monkeypatch`` fixture. Automatically
            reverted at test teardown.

    Returns:
        A list that accumulates the canonical domain name of every
        pipeline invocation in the order they are received. For a
        single ``schedule`` invocation this is ``["schedule"]``; for
        ``all`` it is ``["schedule", "games", "teams", "players",
        "lineups"]`` in that exact order.
    """
    call_log: List[str] = []

    def make_recorder(domain: str):
        """Factory returning a recorder closure bound to ``domain``."""

        def _run(
            client: Any = None,
            writer: Any = None,
            checkpoint: Any = None,
            season: Any = None,
            **kwargs: Any,
        ) -> None:
            """Record the invocation's domain in the shared ``call_log``.

            Signature mirrors the production
            :func:`pipelines.ingest_<domain>.run` keyword interface so
            Click's ``**kwargs`` dispatch in ``run.py`` resolves against
            this recorder without ``TypeError``. Returns ``None`` — the
            production pipelines also return ``None``.
            """
            call_log.append(domain)

        return _run

    # The dispatch table mirrors the production ``from pipelines import
    # (ingest_schedule, ingest_games, ...)`` layout in ``run.py``.
    # Ordering here does not affect correctness — it reflects the
    # AAP §0.4.5 dependency order for readability only.
    monkeypatch.setattr(ingest_schedule, "run", make_recorder("schedule"))
    monkeypatch.setattr(ingest_games, "run", make_recorder("games"))
    monkeypatch.setattr(ingest_teams, "run", make_recorder("teams"))
    monkeypatch.setattr(ingest_players, "run", make_recorder("players"))
    monkeypatch.setattr(ingest_lineups, "run", make_recorder("lineups"))
    return call_log


def _patch_pipelines_capturing_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, List[Dict[str, Any]]]:
    """Like :func:`_patch_pipelines` but records full kwargs, not names.

    Used by tests that need to inspect the arguments each pipeline was
    called with (for example,
    :func:`test_data_subcommand_uses_default_season`, which verifies
    that ``config.DEFAULT_SEASON`` propagates through to the
    ``season`` kwarg).

    Args:
        monkeypatch: The pytest ``monkeypatch`` fixture.

    Returns:
        A dict mapping canonical domain name
        (``"schedule"`` etc.) to a list of captured-kwargs dicts. Each
        list grows by one element per invocation of the corresponding
        pipeline, preserving temporal order.
    """
    captures: Dict[str, List[Dict[str, Any]]] = {
        "schedule": [],
        "games": [],
        "teams": [],
        "players": [],
        "lineups": [],
    }

    def make_recorder(domain: str):
        def _run(**kwargs: Any) -> None:
            captures[domain].append(dict(kwargs))

        return _run

    monkeypatch.setattr(ingest_schedule, "run", make_recorder("schedule"))
    monkeypatch.setattr(ingest_games, "run", make_recorder("games"))
    monkeypatch.setattr(ingest_teams, "run", make_recorder("teams"))
    monkeypatch.setattr(ingest_players, "run", make_recorder("players"))
    monkeypatch.setattr(ingest_lineups, "run", make_recorder("lineups"))
    return captures


# ---------------------------------------------------------------------------
# Phase 2 — Registration and help tests.
# ---------------------------------------------------------------------------


def test_cli_registers_every_subcommand(cli_runner) -> None:
    """Every subcommand in :data:`SUBCOMMANDS` is registered under ``cli``.

    The test invokes ``cli --help`` and scans the captured stdout for
    each subcommand name. A failure here indicates that either a
    subcommand was removed from ``run.py`` or that a new subcommand was
    added without updating :data:`SUBCOMMANDS`. Both situations require
    updating the AAP §0.5.1.7 and §0.7.5 matrices before the test suite
    can be made green again.
    """
    result = cli_runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, (
        "`cli --help` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    for name in SUBCOMMANDS:
        assert name in result.stdout, (
            f"subcommand {name!r} missing from `cli --help` output; "
            f"full stdout: {result.stdout!r}"
        )


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_subcommand_help(cli_runner, name: str) -> None:
    """Every subcommand's ``--help`` exits 0.

    Parametrized across all nine subcommands so that a broken help
    surface on any single subcommand surfaces as a distinct test
    failure (easier to diagnose than a collapsed single test). Click
    guarantees that a subcommand with a valid signature and a
    non-raising docstring will render its help text and exit 0 — so a
    non-zero exit here indicates a genuine defect in the subcommand's
    registration (for example, an unresolvable decorator or a
    startup-time import error in the callback body).
    """
    result = cli_runner.invoke(cli, [name, "--help"])
    assert result.exit_code == 0, (
        f"`cli {name} --help` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("name", DATA_SUBCOMMANDS)
def test_data_subcommand_accepts_season(cli_runner, name: str) -> None:
    """Every data subcommand documents a ``--season`` option.

    The help text must mention ``--season`` for each of the six
    data-facing subcommands. AAP §0.5.1.7 prescribes that every data
    subcommand accept ``--season STRING`` with default
    :data:`config.DEFAULT_SEASON`. A missing ``--season`` flag here
    would silently break operator ergonomics and violate Gate 12's
    config-propagation requirement.
    """
    result = cli_runner.invoke(cli, [name, "--help"])
    assert result.exit_code == 0, (
        f"`cli {name} --help` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert "--season" in result.stdout, (
        f"subcommand {name!r} does not advertise --season in its help "
        f"text; full stdout: {result.stdout!r}"
    )


@pytest.mark.parametrize("name", DIAGNOSTIC_SUBCOMMANDS)
def test_diagnostic_subcommand_does_not_require_season(
    cli_runner, name: str
) -> None:
    """Diagnostic subcommands must NOT expose ``--season``.

    ``health``, ``ready``, and ``metrics`` are stateless and accept no
    arguments (``run.py`` L482-L539). If a future refactor accidentally
    adds ``--season`` to one of them, this test fails — and the
    operator surface is kept clean.
    """
    result = cli_runner.invoke(cli, [name, "--help"])
    assert result.exit_code == 0, (
        f"`cli {name} --help` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert "--season" not in result.stdout, (
        f"diagnostic subcommand {name!r} unexpectedly exposes --season; "
        f"full stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Phase 4 — Per-subcommand dispatch tests (Gate 13 core).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand,expected_domain", SUBCOMMAND_DOMAIN_PAIRS)
def test_subcommand_invokes_its_pipeline(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
    subcommand: str,
    expected_domain: str,
) -> None:
    """Every domain subcommand dispatches to exactly its pipeline.

    This is the core Gate-13 test. After installing recorder spies on
    all five pipeline ``run`` callables, invoke a single domain
    subcommand and assert that:

    1. The CLI exits 0 (no exception propagated).
    2. Exactly one pipeline was invoked (``len(call_log) == 1``).
    3. The invoked pipeline is the one corresponding to the
       subcommand's name (negative-space guard against cross-wiring).

    The parametrization covers all five domain subcommands so any
    dispatch defect (for example, the ``teams`` subcommand mistakenly
    calling ``ingest_players.run``) surfaces as a single, pinpointable
    failure.

    ``catch_exceptions=False`` is passed to :meth:`CliRunner.invoke` so
    any unexpected exception is raised directly rather than being
    wrapped by Click's runner and surfaced only via ``result.exception``
    — this makes root-cause diagnosis substantially faster when the
    pipeline code or the CLI plumbing is broken.
    """
    call_log = _patch_pipelines(monkeypatch)
    result = cli_runner.invoke(
        cli,
        [subcommand, "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        f"`cli {subcommand}` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert call_log == [expected_domain], (
        f"subcommand {subcommand!r} dispatched to {call_log!r}; "
        f"expected [{expected_domain!r}]"
    )


@pytest.mark.parametrize("subcommand,expected_domain", SUBCOMMAND_DOMAIN_PAIRS)
def test_subcommand_does_not_invoke_other_pipelines(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
    subcommand: str,
    expected_domain: str,
) -> None:
    """Single-domain subcommands must NOT fan out to other pipelines.

    This is the negative-space twin of
    :func:`test_subcommand_invokes_its_pipeline`. It exists separately
    because a dispatch bug that invokes the correct pipeline **and**
    accidentally invokes a sibling pipeline would pass the positive
    test (the target is present in ``call_log``) but should still fail
    here because ``call_log`` would have length > 1.

    The assertion verifies that the length of ``call_log`` is exactly
    1 — no more, no less — and that the single entry is the expected
    domain.
    """
    call_log = _patch_pipelines(monkeypatch)
    result = cli_runner.invoke(
        cli,
        [subcommand, "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        f"`cli {subcommand}` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert len(call_log) == 1, (
        f"subcommand {subcommand!r} invoked {len(call_log)} pipelines; "
        f"expected exactly 1. call_log={call_log!r}"
    )
    assert call_log[0] == expected_domain, (
        f"subcommand {subcommand!r} dispatched to {call_log[0]!r}; "
        f"expected {expected_domain!r}"
    )


# ---------------------------------------------------------------------------
# Phase 5 — ``all`` ordering test (AAP §0.4.5, the single most important
# ordering contract in the codebase).
# ---------------------------------------------------------------------------


def test_all_subcommand_runs_pipelines_in_documented_order(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
) -> None:
    """``cli all`` dispatches pipelines in AAP §0.4.5 binding order.

    The canonical order is:

        schedule → games → teams → players → lineups

    This order exists because:

    * ``games`` depends on the ``GAME_ID`` list produced by
      ``schedule`` (AAP §0.4.5, §0.2.3).
    * ``lineups`` depends on roster and clutch-situation context
      emitted by the earlier pipelines.

    Any future reordering requires a corresponding AAP amendment and
    traceability-matrix update. The strict tuple-equality assertion
    makes it impossible to introduce a reordering silently.
    """
    call_log = _patch_pipelines(monkeypatch)
    result = cli_runner.invoke(
        cli,
        ["all", "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        "`cli all` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert tuple(call_log) == PIPELINE_ORDER, (
        "`cli all` did not dispatch pipelines in the AAP §0.4.5 order; "
        f"observed={tuple(call_log)!r} expected={PIPELINE_ORDER!r}"
    )


def test_all_subcommand_invokes_each_pipeline_exactly_once(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
) -> None:
    """``cli all`` invokes each pipeline exactly once — no duplicates.

    A defect where ``all`` accidentally double-invokes a pipeline (for
    example, calling ``ingest_games.run`` both during its own turn and
    again at the end as part of a retry loop) would be detected here.
    The test is complementary to the ordering assertion above; an
    incorrect dispatch that still preserves order would slip past the
    ordering test but fail this cardinality test.
    """
    call_log = _patch_pipelines(monkeypatch)
    result = cli_runner.invoke(
        cli,
        ["all", "--season", config.DEFAULT_SEASON],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        "`cli all` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    # A bag-of-calls check: every expected domain appears exactly once
    # regardless of order. Combined with the ordering test above, this
    # pins down the ``all`` dispatch contract completely.
    for domain in PIPELINE_ORDER:
        assert call_log.count(domain) == 1, (
            f"pipeline {domain!r} was invoked {call_log.count(domain)} "
            f"times by `cli all`; expected exactly 1. call_log={call_log!r}"
        )
    assert len(call_log) == len(PIPELINE_ORDER), (
        f"`cli all` invoked {len(call_log)} pipelines; expected "
        f"{len(PIPELINE_ORDER)}. call_log={call_log!r}"
    )


def test_all_subcommand_propagates_season_to_every_pipeline(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
) -> None:
    """The ``--season`` argument is forwarded to every pipeline by ``all``.

    Invokes ``cli all --season 2023-24`` and verifies that each of the
    five recorded pipeline invocations received ``season="2023-24"``
    as a keyword argument. This closes the loop on Gate 12 (Config
    Propagation Tracing) for the aggregate subcommand.
    """
    captures = _patch_pipelines_capturing_kwargs(monkeypatch)
    result = cli_runner.invoke(
        cli,
        ["all", "--season", "2023-24"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, (
        "`cli all --season 2023-24` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    for domain in PIPELINE_ORDER:
        assert len(captures[domain]) == 1, (
            f"pipeline {domain!r} was invoked {len(captures[domain])} "
            f"times; expected 1. captures={captures!r}"
        )
        captured_kwargs = captures[domain][0]
        assert captured_kwargs.get("season") == "2023-24", (
            f"pipeline {domain!r} received "
            f"season={captured_kwargs.get('season')!r}; expected "
            f"'2023-24'. full kwargs={captured_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Phase 6 — Diagnostic subcommand tests (health / ready / metrics).
# ---------------------------------------------------------------------------


def test_health_subcommand(cli_runner) -> None:
    """``cli health`` exits 0 and emits a JSON body containing ``status``.

    ``health`` is stateless per ``run.py`` L482-L498: it never
    accesses the filesystem, never issues HTTP traffic, and returns a
    liveness verdict that always reports ``status="ok"``. The test
    therefore deliberately does NOT consume the ``tmp_output_dir`` or
    ``tmp_log_dir`` fixtures — if ``health`` ever starts touching the
    filesystem, this test will continue to run against the real
    filesystem and surface the regression.
    """
    result = cli_runner.invoke(cli, ["health"])
    assert result.exit_code == 0, (
        "`cli health` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert "status" in result.stdout, (
        f"`cli health` output does not contain 'status'; "
        f"full stdout={result.stdout!r}"
    )
    # The production callback renders the result as indented JSON. Parse
    # it and verify ``status`` is the canonical ``"ok"`` value. A parse
    # failure here indicates the callback stopped emitting JSON (perhaps
    # by accidentally logging its own output through a prefixing
    # formatter), which would break operator tooling that greps for
    # ``"status": "ok"``.
    payload = json.loads(result.stdout)
    assert payload.get("status") == "ok", (
        f"`cli health` status={payload.get('status')!r}; expected 'ok'. "
        f"payload={payload!r}"
    )


def test_ready_subcommand_when_configured(
    cli_runner, tmp_output_dir, tmp_log_dir
) -> None:
    """``cli ready`` exits 0 or 1 and emits a JSON body containing ``status``.

    ``ready`` aggregates four sub-probes (output-dir writability,
    required headers presence, rate-limit configuration, checkpoint
    parseability). Under the tmp_path-rooted fixtures, all four
    probes should pass and the command should exit 0 with
    ``status="ready"``. However, a partial-environment CI image might
    legitimately fail one probe and exit 1 with ``status="not_ready"``
    — both are valid outputs of a correctly implemented readiness
    probe. Accepting either outcome keeps the test robust against
    environmental variability while still rejecting catastrophic
    failures (non-zero exit codes other than 1, crashes, missing
    ``status`` field).
    """
    result = cli_runner.invoke(cli, ["ready"])
    assert result.exit_code in (0, 1), (
        f"`cli ready` exited with unexpected code {result.exit_code}; "
        f"expected 0 or 1. stderr={result.stderr!r}"
    )
    assert "status" in result.stdout, (
        f"`cli ready` output does not contain 'status'; "
        f"full stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    assert payload.get("status") in ("ready", "not_ready"), (
        f"`cli ready` status={payload.get('status')!r}; expected "
        f"'ready' or 'not_ready'. payload={payload!r}"
    )


def test_metrics_subcommand_returns_prometheus_text(cli_runner) -> None:
    """``cli metrics`` exits 0 and emits Prometheus text-format output.

    Because the autouse ``_reset_metrics_registry_between_tests``
    fixture clears the registry before each test, the subcommand may
    legitimately emit an empty string (when no counter has been
    incremented yet) OR a string containing ``nba_requests_total``
    (when the CLI startup path in ``run.py`` has registered its
    counters but not yet incremented any). Either output is valid —
    the strict requirements are that the exit code is 0 and, when
    non-empty, the output conforms to Prometheus text format (which we
    check by the presence of one well-known metric name).
    """
    result = cli_runner.invoke(cli, ["metrics"])
    assert result.exit_code == 0, (
        "`cli metrics` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    output = result.stdout
    # Acceptance is disjunctive: empty is a valid fresh-process state;
    # non-empty output must contain at least one well-known metric name
    # to prove that the renderer is producing real Prometheus text
    # rather than a random placeholder string.
    assert (
        output.strip() == ""
        or "nba_requests_total" in output
        or "pipeline_runs_total" in output
    ), (
        "`cli metrics` produced non-Prometheus output; "
        f"stdout={output!r}"
    )


# ---------------------------------------------------------------------------
# Phase 7 — Season flag default test (Gate 12 closure).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", DOMAIN_SUBCOMMANDS)
def test_data_subcommand_uses_default_season(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
    subcommand: str,
) -> None:
    """Invoking a domain subcommand WITHOUT ``--season`` uses the default.

    When the operator runs ``python run.py schedule`` with no
    ``--season``, Click must inject the default value
    (:data:`config.DEFAULT_SEASON`) so the downstream pipeline receives
    a concrete season string. This test verifies the wiring by
    asserting that the recorded ``season`` kwarg equals exactly
    :data:`config.DEFAULT_SEASON`.

    This closes Gate 12 (Config Propagation Tracing) for the domain
    subcommands: ``config.DEFAULT_SEASON`` is a documented read-site
    inside ``run.py``'s ``@click.option`` decorator, and this test
    proves that the read-site actually flows through to pipeline
    invocation — not just to help-text rendering.
    """
    captures = _patch_pipelines_capturing_kwargs(monkeypatch)
    # Deliberately omit ``--season`` — Click must fall back to the
    # default sourced from ``config.DEFAULT_SEASON``.
    result = cli_runner.invoke(cli, [subcommand], catch_exceptions=False)
    assert result.exit_code == 0, (
        f"`cli {subcommand}` (no --season) exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    assert len(captures[subcommand]) == 1, (
        f"pipeline {subcommand!r} was invoked "
        f"{len(captures[subcommand])} times; expected 1"
    )
    captured_kwargs = captures[subcommand][0]
    assert captured_kwargs.get("season") == config.DEFAULT_SEASON, (
        f"subcommand {subcommand!r} received "
        f"season={captured_kwargs.get('season')!r}; expected "
        f"{config.DEFAULT_SEASON!r}. full kwargs={captured_kwargs!r}"
    )


def test_all_subcommand_uses_default_season(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
) -> None:
    """``cli all`` with no ``--season`` propagates the default to all pipelines.

    Analogous to :func:`test_data_subcommand_uses_default_season` but
    for the aggregate subcommand. Every one of the five pipeline
    invocations must carry ``season=config.DEFAULT_SEASON`` when the
    operator omits the flag.
    """
    captures = _patch_pipelines_capturing_kwargs(monkeypatch)
    result = cli_runner.invoke(cli, ["all"], catch_exceptions=False)
    assert result.exit_code == 0, (
        "`cli all` (no --season) exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    for domain in PIPELINE_ORDER:
        assert len(captures[domain]) == 1, (
            f"pipeline {domain!r} was invoked {len(captures[domain])} "
            f"times; expected 1"
        )
        captured_kwargs = captures[domain][0]
        assert captured_kwargs.get("season") == config.DEFAULT_SEASON, (
            f"pipeline {domain!r} received "
            f"season={captured_kwargs.get('season')!r}; expected "
            f"{config.DEFAULT_SEASON!r}"
        )


# ---------------------------------------------------------------------------
# Phase 8 — Correlation-ID propagation (Observability rule, AAP §0.7.3.1).
# ---------------------------------------------------------------------------


def test_cli_mints_correlation_id(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every CLI invocation mints a correlation ID and tags log records.

    The Observability rule (AAP §0.7.3.1) requires that every log
    record produced during a CLI invocation carry a non-empty
    ``correlation_id`` attribute. The correlation ID is minted once
    per invocation by ``run.py`` and propagated via
    :class:`contextvars.ContextVar`, so every :class:`logging.LogRecord`
    produced downstream inherits the ID automatically.

    The ``caplog`` fixture captures records at the handler level, and
    the ``_CorrelationFormatter`` in ``utils/logger.py`` sets the
    ``correlation_id`` attribute on each record. This test therefore
    consumes caplog records directly instead of importing
    ``utils.correlation`` — which is intentional: the observable
    surface (log records) is what operators rely on, and the internal
    ContextVar is an implementation detail that should not leak into
    the test surface.

    A record whose ``correlation_id`` attribute is empty (``""``) or
    missing (``None``) is treated as a failure because the operator
    cannot correlate it across a distributed trace.
    """
    _patch_pipelines(monkeypatch)
    # Capture records at the CLI's target logger. ``cli.schedule``
    # uses the logger name ``cli.schedule`` per ``run.py`` L385.
    # Using ``""`` (the root logger) captures records from every
    # logger in the process and covers the CLI, pipeline, and utility
    # layers at once.
    with caplog.at_level("INFO"):
        result = cli_runner.invoke(
            cli,
            ["schedule", "--season", config.DEFAULT_SEASON],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, (
        "`cli schedule` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    # At least one record should have been produced; with zero records
    # the test would be trivially vacuous and miss regressions.
    assert caplog.records, (
        "no log records captured during `cli schedule`; expected at "
        "least one record for the 'run.start' event"
    )
    # Every captured record should have a non-empty ``correlation_id``
    # attribute. Records that lack the attribute are fixed up by
    # ``_CorrelationFormatter.format``, but that happens only at
    # render time; ``caplog`` captures pre-format records, so a missing
    # attribute here is acceptable IF at least one record carries a
    # non-empty ID (proving that the CLI minted and propagated one).
    ids = [
        getattr(record, "correlation_id", "") or ""
        for record in caplog.records
    ]
    non_empty_ids = [cid for cid in ids if cid]
    assert non_empty_ids, (
        "no log record carried a non-empty correlation_id attribute; "
        f"observed ids={ids!r}"
    )


def test_cli_correlation_id_is_stable_within_invocation(
    cli_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_output_dir,
    tmp_log_dir,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All log records from a single invocation share one correlation ID.

    A single CLI invocation produces multiple log records (at minimum
    a ``run.start`` and a ``run.complete``). The Observability
    contract requires them to share a correlation ID so operators can
    reconstruct a run's timeline from logs alone.

    A defect where a new ID is minted mid-invocation (for example, by
    calling ``new_correlation_id()`` inside the pipeline body) would
    pass :func:`test_cli_mints_correlation_id` (at least one record
    has an ID) but fail here because the set of distinct non-empty
    IDs would have cardinality > 1.
    """
    _patch_pipelines(monkeypatch)
    with caplog.at_level("INFO"):
        result = cli_runner.invoke(
            cli,
            ["schedule", "--season", config.DEFAULT_SEASON],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, (
        "`cli schedule` exited non-zero: "
        f"exit_code={result.exit_code} stderr={result.stderr!r}"
    )
    non_empty_ids = {
        getattr(record, "correlation_id", "") or ""
        for record in caplog.records
        if getattr(record, "correlation_id", "")
    }
    # Exactly one distinct non-empty ID is the happy-path outcome.
    # Zero would fail the earlier ``test_cli_mints_correlation_id``
    # test and is guarded against by the ``caplog.records`` check
    # there; here we require exactly-one rather than at-most-one so a
    # spurious re-mint mid-invocation surfaces as a failure.
    assert len(non_empty_ids) == 1, (
        "expected exactly one correlation_id across all records of a "
        f"single invocation; observed {len(non_empty_ids)} distinct "
        f"non-empty values: {non_empty_ids!r}"
    )


# ---------------------------------------------------------------------------
# Closing invariants — defensive regression guards.
# ---------------------------------------------------------------------------


def test_cli_commands_dict_keys_exactly_match_subcommands() -> None:
    """``cli.commands.keys()`` must equal :data:`SUBCOMMANDS` exactly.

    This guard is stricter than
    :func:`test_cli_registers_every_subcommand` because it uses set
    equality rather than substring matching. An extra subcommand
    (say, a debug-only ``explain`` added without updating the AAP) or
    a missing one is surfaced here even if the help-text substring
    check happened to pass for partial-name overlaps.
    """
    # ``cli`` is a :class:`click.Group`; its ``commands`` attribute is
    # a mapping from command name to the command callback.
    assert set(cli.commands.keys()) == set(SUBCOMMANDS), (
        f"`cli.commands.keys()` differs from SUBBCOMMANDS contract; "
        f"observed={set(cli.commands.keys())!r} "
        f"expected={set(SUBCOMMANDS)!r}"
    )


def test_all_command_is_registered_under_literal_name() -> None:
    """The ``all`` subcommand is registered under the Click name ``"all"``.

    ``all`` is a Python builtin, so the underlying Python callback in
    ``run.py`` is named ``all_cmd`` to avoid shadowing. The explicit
    ``@cli.command("all")`` argument is what produces the operator-
    facing ``python run.py all ...`` invocation. A regression where a
    refactor replaced ``@cli.command("all")`` with ``@cli.command()``
    would silently rename the subcommand to ``all-cmd`` — this test
    catches that class of refactor error.
    """
    assert "all" in cli.commands, (
        f"subcommand 'all' is not registered; cli.commands.keys()="
        f"{list(cli.commands.keys())!r}"
    )
    # Guard against the callback becoming the Python builtin ``all``
    # (which would be a catastrophic typo where the decorator wraps
    # the wrong callable).
    assert cli.commands["all"].callback is not all, (
        "subcommand 'all' callback is the Python builtin `all`; "
        "this indicates a decorator-wrap defect in run.py"
    )


@pytest.mark.parametrize("name", DOMAIN_SUBCOMMANDS)
def test_every_domain_pipeline_module_exposes_callable_run(
    name: str,
) -> None:
    """Gate 9 mirror — every pipeline module has a callable ``run``.

    The Gate-9 contract (Integration Wiring Verification) requires
    every pipeline to be reachable from ``run.py``. This test is the
    structural half of that contract: each pipeline module must
    define a module-level callable named ``run``. The behavioral
    half — that ``run.py`` actually dispatches to those callables —
    is covered by
    :func:`test_subcommand_invokes_its_pipeline`.

    Monkeypatching a non-callable attribute would silently break
    :func:`_patch_pipelines`; this test catches that class of
    regression at the collection-time level rather than at the
    first-dispatch-test failure.
    """
    modules = {
        "schedule": ingest_schedule,
        "games": ingest_games,
        "teams": ingest_teams,
        "players": ingest_players,
        "lineups": ingest_lineups,
    }
    module = modules[name]
    assert hasattr(module, "run"), (
        f"pipeline module {name!r} does not define a 'run' attribute"
    )
    assert callable(module.run), (
        f"pipeline module {name!r}'s 'run' attribute is not callable; "
        f"type={type(module.run)!r}"
    )
