"""Unit tests for :mod:`utils.health`.

Verifies the liveness and readiness probes exposed by the Observability
rule deliverable (AAP §0.7.3.1). ``check_health()`` is stateless and
always-ok. Each of the four readiness probes — ``output_dir_writable``,
``required_headers_present``, ``rate_limit_configured``,
``checkpoint_parseable`` — has dedicated positive and negative tests.

All tests are network-free and filesystem-isolated via the
``tmp_output_dir`` fixture (redirecting ``config.OUTPUT_DIR`` and
``config.CHECKPOINT_PATH`` under ``tmp_path``). No real ``output/``
artifacts are produced and no real HTTP traffic is generated.

Design notes
------------
* **``monkeypatch.setattr(config, "...", ..., raising=True)``** is used
  for every config override. ``raising=True`` is defensive: if a future
  refactor renames a constant, the test fails fast at the ``setattr``
  call rather than silently passing with stale state.
* **ISO-8601 parsing** uses :func:`datetime.fromisoformat` with a
  ``.replace("Z", "+00:00")`` shim so the same assertion works whether
  the production module emits a trailing ``Z`` or an explicit
  ``+00:00`` offset. Python 3.11+ parses both forms natively; the shim
  guarantees forward compatibility across minor-version upgrades.
* **No mocking libraries** are used; all isolation is achieved through
  ``monkeypatch`` and ``tmp_path`` (Rule: tests must be deterministic
  without third-party mock frameworks).
"""

from __future__ import annotations

import json
import platform  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
import re  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict  # noqa: F401  # Used in docstring examples and available for future expansion.

import pytest

import config
from utils import health


# ---------------------------------------------------------------------------
# Phase 2.1 — ``check_health()`` shape and semantics
# ---------------------------------------------------------------------------


def test_check_health_returns_dict() -> None:
    """``check_health()`` returns a plain ``dict``.

    Downstream tooling (dashboards, ``run.py health`` JSON emission)
    relies on the return type being a serialisable mapping. A subtype
    such as :class:`collections.OrderedDict` would still satisfy
    ``isinstance(result, dict)``; an unexpected type (list, tuple,
    dataclass) would break ``json.dumps`` downstream.
    """
    result = health.check_health()
    assert isinstance(result, dict)


def test_check_health_returns_required_keys() -> None:
    """The liveness probe returns the four documented keys.

    The AAP fixes the dict shape (``status``, ``timestamp``,
    ``python_version``, ``component``) so dashboards can parse it
    deterministically across runs and releases.
    """
    result = health.check_health()
    for key in ("status", "timestamp", "python_version", "component"):
        assert key in result, f"Missing key {key!r} in check_health() result"


def test_check_health_status_is_ok() -> None:
    """Liveness is always ``ok`` when the interpreter can execute the probe.

    If the Python process can run this function at all, by definition
    it is alive — there is no failure mode for liveness in the current
    design. Any non-``ok`` status would indicate a regression.
    """
    assert health.check_health()["status"] == "ok"


def test_check_health_python_version_matches_runtime() -> None:
    """``python_version`` reports the interpreter actually in use.

    Operators diagnosing multi-version environments (system Python vs.
    project ``.venv``) need confidence that the probe reflects the
    interpreter running the process, not a hard-coded string. We
    assert the current ``major.minor`` substring is present; the full
    ``patch`` version may vary across CI runners but the major/minor
    must match this test process exactly.
    """
    py = health.check_health()["python_version"]
    assert isinstance(py, str)
    runtime_major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert runtime_major_minor in py, (
        f"python_version {py!r} does not contain runtime {runtime_major_minor!r}"
    )


def test_check_health_component_matches_project_slug() -> None:
    """``component`` is the canonical project slug.

    A stable component identifier lets downstream log aggregators and
    dashboards group probe results across multiple services even if
    future phases introduce additional NBA pipelines.
    """
    assert health.check_health()["component"] == "nba-data-ingestion-pipeline"


def test_check_health_timestamp_is_iso_8601_utc() -> None:
    """Timestamp is ISO-8601 formatted and UTC-anchored.

    Cross-host log correlation (a prerequisite for the distributed
    tracing clause of the Observability rule) requires a timezone-
    aware timestamp. A naive ``datetime`` or a non-UTC timestamp would
    break log-line join operations in downstream dashboards.
    """
    ts = health.check_health()["timestamp"]
    # Shim Z -> +00:00 for forward compatibility; Python 3.11+ accepts both.
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, "Timestamp must be timezone-aware"
    assert parsed.utcoffset().total_seconds() == 0.0, "Timestamp must be UTC"


def test_check_health_is_idempotent_and_stateless() -> None:
    """Repeated invocations return independent, identically-shaped dicts.

    The liveness probe must be safe to invoke repeatedly (e.g. from
    polling health-check pollers). Sharing state between invocations
    would be a memory-lifecycle hazard: a caller mutating one result
    must not affect a future or prior caller.
    """
    a = health.check_health()
    b = health.check_health()
    assert set(a) == set(b)
    assert a["status"] == b["status"] == "ok"
    # Mutating one dict must not affect the other — they are separate objects.
    a["status"] = "mutated"
    assert b["status"] == "ok"


# ---------------------------------------------------------------------------
# Phase 2.2 — ``check_readiness()`` happy path
# ---------------------------------------------------------------------------


def test_check_readiness_happy_path_is_ready(tmp_output_dir: Path) -> None:
    """With defaults, a fresh ``tmp_output_dir`` yields ``status='ready'``.

    The fixture creates a writable output directory and leaves the
    checkpoint file absent (== fresh run semantics). Combined with the
    default ``REQUIRED_HEADERS`` and ``RATE_LIMIT_SECONDS=1.0``, every
    sub-check must return ``ok`` and the aggregate status must be
    ``ready``.
    """
    result = health.check_readiness()
    assert result["status"] == "ready"


def test_check_readiness_happy_path_returns_required_keys(tmp_output_dir: Path) -> None:
    """The readiness dict carries ``status``, ``checks`` and the four sub-checks.

    Dashboards key off this shape. Any missing top-level key would
    break downstream parsers; any missing sub-check would hide a
    regression from operators.
    """
    result = health.check_readiness()
    assert "status" in result
    assert "checks" in result
    checks = result["checks"]
    for key in (
        "output_dir_writable",
        "required_headers_present",
        "rate_limit_configured",
        "checkpoint_parseable",
    ):
        assert key in checks, f"Missing sub-check {key!r}"


def test_check_readiness_happy_path_all_subchecks_ok(tmp_output_dir: Path) -> None:
    """Every sub-check reports ``ok`` when defaults are intact.

    We iterate over all four sub-checks rather than asserting the
    aggregate only, so that a failure surfaces the specific probe at
    fault — aiding debugging if a future refactor breaks one probe
    while the aggregate status coincidentally remains ``ready``.
    """
    result = health.check_readiness()
    for name, check in result["checks"].items():
        assert check["status"] == "ok", (
            f"{name} status was {check['status']!r}: {check}"
        )


def test_check_readiness_subcheck_includes_detail_field(tmp_output_dir: Path) -> None:
    """Every sub-check dict carries a human-readable ``detail`` string.

    Operators reading the JSON output of ``run.py ready`` need a
    textual diagnostic, not just an opaque status. The detail is
    always a string, never ``None`` or structured data — it must be
    directly printable.
    """
    result = health.check_readiness()
    for name, check in result["checks"].items():
        assert "detail" in check, f"{name} missing detail field"
        assert isinstance(check["detail"], str), (
            f"{name} detail is not a string: {type(check['detail']).__name__}"
        )


# ---------------------------------------------------------------------------
# Phase 2.3 — ``output_dir_writable`` probe
# ---------------------------------------------------------------------------


def test_output_dir_writable_ok_when_dir_writable(tmp_output_dir: Path) -> None:
    """The probe reports ``ok`` when the output dir accepts new files.

    ``tmp_output_dir`` provides a writable tmp-path-rooted directory,
    so the probe's ``tempfile.NamedTemporaryFile`` round trip must
    succeed cleanly.
    """
    result = health.check_readiness()
    assert result["checks"]["output_dir_writable"]["status"] == "ok"


def test_output_dir_writable_fails_when_output_dir_does_not_exist_and_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe reports ``fail`` when the output path cannot be realised.

    We point ``OUTPUT_DIR`` at a subpath of an existing *file* — a
    directory cannot be created beneath a file on POSIX filesystems,
    so ``mkdir(parents=True, exist_ok=True)`` raises ``NotADirectoryError``.
    The probe's broad ``except Exception`` must capture this and
    translate it into a ``fail`` verdict; the aggregate readiness
    must then be ``not_ready``.
    """
    blocker = tmp_path / "blocker_file.txt"
    blocker.write_text("not a dir", encoding="utf-8")
    child_under_blocker = blocker / "impossible_subdir"
    monkeypatch.setattr(config, "OUTPUT_DIR", child_under_blocker, raising=True)
    # Also redirect checkpoint path so that probe doesn't erroneously
    # succeed by finding the real checkpoint.json from another test run.
    monkeypatch.setattr(
        config, "CHECKPOINT_PATH", child_under_blocker / "checkpoint.json", raising=True
    )
    result = health.check_readiness()
    assert result["checks"]["output_dir_writable"]["status"] == "fail"
    assert result["status"] == "not_ready"


def test_output_dir_writable_creates_dir_when_missing_then_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing-but-creatable output dir is materialised and accepted.

    First-time operators on a fresh clone should get a ``ready``
    verdict without first running a pipeline to create ``output/``.
    The probe's one-and-only intentional side effect — an idempotent
    ``mkdir(parents=True, exist_ok=True)`` — must create the directory
    on demand. We also redirect ``CHECKPOINT_PATH`` into the same
    fresh directory to avoid tripping the checkpoint probe on an
    unrelated pre-existing file.
    """
    new_dir = tmp_path / "fresh_output"
    assert not new_dir.exists()
    monkeypatch.setattr(config, "OUTPUT_DIR", new_dir, raising=True)
    monkeypatch.setattr(
        config, "CHECKPOINT_PATH", new_dir / "checkpoint.json", raising=True
    )
    result = health.check_readiness()
    assert result["checks"]["output_dir_writable"]["status"] == "ok"
    # The side effect is observable on disk.
    assert new_dir.exists()
    assert new_dir.is_dir()


# ---------------------------------------------------------------------------
# Phase 2.4 — ``required_headers_present`` probe (Rule 3)
# ---------------------------------------------------------------------------


def test_required_headers_present_ok_with_default_config(tmp_output_dir: Path) -> None:
    """The shipped default ``REQUIRED_HEADERS`` satisfies Rule 3.

    ``config.REQUIRED_HEADERS`` declares both ``Referer`` and
    ``User-Agent`` among its eight entries; the probe must accept
    this default configuration.
    """
    result = health.check_readiness()
    assert result["checks"]["required_headers_present"]["status"] == "ok"


def test_required_headers_missing_referer(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Removing the Referer key produces a ``fail`` verdict.

    Rule 3 explicitly requires ``Referer: https://stats.nba.com`` on
    every request. A missing Referer would break the NBA Stats API
    integration (403 responses), so the probe must flag the gap
    before any HTTPS traffic is attempted.
    """
    broken = dict(config.REQUIRED_HEADERS)
    broken.pop("Referer", None)
    monkeypatch.setattr(config, "REQUIRED_HEADERS", broken, raising=True)
    result = health.check_readiness()
    assert result["checks"]["required_headers_present"]["status"] == "fail"
    assert "Referer" in result["checks"]["required_headers_present"]["detail"]
    assert result["status"] == "not_ready"


def test_required_headers_missing_user_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Removing the User-Agent key produces a ``fail`` verdict.

    Rule 3 requires a browser-like ``User-Agent``; without one, the
    NBA Stats API returns 403. The probe must catch the missing key
    and include its name in the ``detail`` for operator diagnostics.
    """
    broken = dict(config.REQUIRED_HEADERS)
    broken.pop("User-Agent", None)
    monkeypatch.setattr(config, "REQUIRED_HEADERS", broken, raising=True)
    result = health.check_readiness()
    assert result["checks"]["required_headers_present"]["status"] == "fail"
    assert "User-Agent" in result["checks"]["required_headers_present"]["detail"]


def test_required_headers_empty_dict_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """An empty headers dict is invalid, even though it technically has 'dict' type.

    ``config.REQUIRED_HEADERS = {}`` would leave outbound requests
    with zero configured headers — catastrophically wrong for Rule 3.
    The probe's ``not headers`` branch is responsible for catching
    this; we verify it here.
    """
    monkeypatch.setattr(config, "REQUIRED_HEADERS", {}, raising=True)
    result = health.check_readiness()
    assert result["checks"]["required_headers_present"]["status"] == "fail"


def test_required_headers_rejects_non_dict_type(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Non-``dict`` values for ``REQUIRED_HEADERS`` are rejected.

    ``_probe_required_headers`` guards against the structural mistake
    where ``config.REQUIRED_HEADERS`` is declared as a ``list`` (e.g.
    a sequence of header names) rather than the canonical ``dict``
    mapping of header name to value. The probe's
    ``not isinstance(headers, dict)`` branch must catch this and
    report ``fail`` with a diagnostic message — otherwise downstream
    code (``NBAClient.Session.headers.update(...)``) would raise a
    bare ``TypeError`` at the first request instead of surfacing a
    structured readiness failure the operator can act on.

    Note that the current probe contract (per ``utils/health.py``'s
    own schema) verifies *presence* of the ``Referer`` and
    ``User-Agent`` keys rather than non-empty values: operators
    retain freedom to tune header values (e.g. rotating the
    ``User-Agent`` string) without tripping the probe. This test
    therefore exercises the complementary type-safety guard rail
    rather than value-emptiness semantics.
    """
    monkeypatch.setattr(
        config, "REQUIRED_HEADERS", ["Referer", "User-Agent"], raising=True
    )
    result = health.check_readiness()
    assert result["checks"]["required_headers_present"]["status"] == "fail"
    assert result["status"] == "not_ready"


# ---------------------------------------------------------------------------
# Phase 2.5 — ``rate_limit_configured`` probe (Rule 2 floor)
# ---------------------------------------------------------------------------


def test_rate_limit_configured_ok_at_default(tmp_output_dir: Path) -> None:
    """The default ``RATE_LIMIT_SECONDS=1.0`` satisfies Rule 2.

    Rule 2 floor is exactly 1.0 seconds between requests. The
    shipped default equals the floor, which must be accepted (``>=``
    not ``>``).
    """
    result = health.check_readiness()
    assert result["checks"]["rate_limit_configured"]["status"] == "ok"


def test_rate_limit_configured_fails_below_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Values below 1.0 violate Rule 2 and are rejected with a clear detail.

    Operators who attempt to tighten the rate limit below the Rule 2
    floor (e.g. ``NBA_RATE_LIMIT_SECONDS=0.5``) must be caught before
    any traffic is issued. The detail string includes the explicit
    "Rule 2 floor" token so operators grep-ing logs find consistent
    messaging across both the readiness probe and the runtime rate
    limiter.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0.5, raising=True)
    result = health.check_readiness()
    check = result["checks"]["rate_limit_configured"]
    assert check["status"] == "fail"
    assert "Rule 2 floor" in check["detail"]
    assert result["status"] == "not_ready"


def test_rate_limit_configured_fails_at_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Zero delay is explicitly rejected.

    A zero-second rate limit would flood the NBA Stats API, producing
    HTTP 429 avalanches. The probe must reject ``0.0`` even though
    it is a valid numeric value.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0.0, raising=True)
    result = health.check_readiness()
    assert result["checks"]["rate_limit_configured"]["status"] == "fail"


def test_rate_limit_configured_ok_above_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Values above 1.0 are accepted — operators may be more conservative.

    The Rule 2 floor is a *minimum*, not a maximum. Operators running
    against a constrained network or a staging NBA endpoint may
    legitimately raise ``RATE_LIMIT_SECONDS`` (e.g. to 2.5 s) without
    tripping the probe.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 2.5, raising=True)
    result = health.check_readiness()
    assert result["checks"]["rate_limit_configured"]["status"] == "ok"


def test_rate_limit_configured_ok_at_exact_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """``RATE_LIMIT_SECONDS=1.0`` (boundary) is accepted.

    The Rule 2 comparison is ``>=``, not strict ``>``. Setting the
    constant to exactly the floor is legal and must not trip the
    probe.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 1.0, raising=True)
    result = health.check_readiness()
    assert result["checks"]["rate_limit_configured"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Phase 2.6 — ``checkpoint_parseable`` probe
# ---------------------------------------------------------------------------


def test_checkpoint_parseable_ok_when_missing_fresh_run(tmp_output_dir: Path) -> None:
    """A missing checkpoint file is ``ok`` (fresh-run semantics).

    First-time operators and operators who have just cleared
    ``output/`` must get a ``ready`` verdict. The ``detail`` string
    should convey that no checkpoint was found — we match loosely on
    any of several near-synonyms to avoid brittle exact-string
    assertions if the wording is tuned later.
    """
    assert not Path(config.CHECKPOINT_PATH).exists()
    result = health.check_readiness()
    check = result["checks"]["checkpoint_parseable"]
    assert check["status"] == "ok"
    detail_lower = check["detail"].lower()
    assert any(
        word in detail_lower for word in ("fresh", "missing", "not present")
    ), f"Fresh-run detail not recognisable: {check['detail']!r}"


def test_checkpoint_parseable_ok_with_valid_dict(tmp_output_dir: Path) -> None:
    """A valid-dict checkpoint file parses successfully.

    A resume-from-checkpoint scenario: ``checkpoint.json`` already
    contains one or more ``{domain: {key: iso_timestamp}}`` entries
    from a prior partial run. The probe must load and accept it.
    """
    manifest = {"games": {"0022500001": "2026-04-19T00:00:00+00:00"}}
    Path(config.CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.CHECKPOINT_PATH).write_text(json.dumps(manifest), encoding="utf-8")
    result = health.check_readiness()
    assert result["checks"]["checkpoint_parseable"]["status"] == "ok"


def test_checkpoint_parseable_fails_with_malformed_json(tmp_output_dir: Path) -> None:
    """Malformed JSON produces a ``fail`` verdict (Rule 5 risk).

    Rule 5 makes ``checkpoint.json`` the resumability pivot. A
    corrupted file would crash the pipeline mid-run; the probe must
    catch the corruption before any work begins. The aggregate
    readiness must correspondingly be ``not_ready``.
    """
    Path(config.CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.CHECKPOINT_PATH).write_text(
        "{ this is not valid json", encoding="utf-8"
    )
    result = health.check_readiness()
    assert result["checks"]["checkpoint_parseable"]["status"] == "fail"
    assert result["status"] == "not_ready"


def test_checkpoint_parseable_fails_with_non_dict_top_level(tmp_output_dir: Path) -> None:
    """A JSON list at top level is invalid — the manifest must be a dict.

    The :class:`utils.checkpoint.CheckpointManager` contract (AAP
    §0.4.1.1) requires a ``{domain: {key: timestamp}}`` mapping at
    the top level. A list would be valid JSON but invalid schema.
    """
    Path(config.CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.CHECKPOINT_PATH).write_text(
        json.dumps(["games", "players"]), encoding="utf-8"
    )
    result = health.check_readiness()
    assert result["checks"]["checkpoint_parseable"]["status"] == "fail"


def test_checkpoint_parseable_fails_with_top_level_string(tmp_output_dir: Path) -> None:
    """A JSON string at top level is invalid — the manifest must be a dict.

    A valid JSON document may be any primitive type (string, number,
    bool, null) or container (list, dict). The checkpoint contract
    narrows the top level to ``dict`` only; the probe must enforce
    the narrower contract.
    """
    Path(config.CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.CHECKPOINT_PATH).write_text('"just a string"', encoding="utf-8")
    result = health.check_readiness()
    assert result["checks"]["checkpoint_parseable"]["status"] == "fail"


def test_checkpoint_parseable_ok_with_empty_dict(tmp_output_dir: Path) -> None:
    """An empty dict is a valid (if uninformative) checkpoint.

    A zero-entry checkpoint is semantically equivalent to a fresh run
    — nothing has been completed yet. The probe must accept it; the
    pipeline will proceed to populate it.
    """
    Path(config.CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.CHECKPOINT_PATH).write_text("{}", encoding="utf-8")
    result = health.check_readiness()
    assert result["checks"]["checkpoint_parseable"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Phase 2.7 — Aggregate ``not_ready`` status when any sub-check fails
# ---------------------------------------------------------------------------


def test_check_readiness_not_ready_when_rate_limit_fails_only(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """A single failing sub-check propagates to ``not_ready``.

    The aggregate must be ``not_ready`` if *any* sub-check fails,
    even when the other three are ``ok``. We verify this by breaking
    the rate-limit check alone and confirming the other three remain
    ``ok`` while the aggregate flips.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0.5, raising=True)
    result = health.check_readiness()
    assert result["checks"]["rate_limit_configured"]["status"] == "fail"
    assert result["checks"]["output_dir_writable"]["status"] == "ok"
    assert result["checks"]["required_headers_present"]["status"] == "ok"
    assert result["checks"]["checkpoint_parseable"]["status"] == "ok"
    assert result["status"] == "not_ready"


def test_check_readiness_not_ready_when_multiple_checks_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_output_dir: Path
) -> None:
    """Multiple sub-check failures still produce a single ``not_ready`` verdict.

    The aggregate cannot double-fail; ``not_ready`` is a single
    verdict. We verify that individual sub-checks continue to report
    independently so operators can see *all* problems at once rather
    than being forced to fix them sequentially.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0.5, raising=True)
    monkeypatch.setattr(config, "REQUIRED_HEADERS", {}, raising=True)
    result = health.check_readiness()
    assert result["status"] == "not_ready"
    assert result["checks"]["rate_limit_configured"]["status"] == "fail"
    assert result["checks"]["required_headers_present"]["status"] == "fail"


def test_check_readiness_timestamp_present_and_iso_utc(tmp_output_dir: Path) -> None:
    """Readiness includes a UTC ISO-8601 timestamp, like liveness does.

    Cross-host log correlation requires a timezone-aware UTC
    timestamp on every probe response. The probe must emit the same
    stable format as :func:`check_health` so dashboards can apply one
    parser to both streams.
    """
    result = health.check_readiness()
    assert "timestamp" in result
    ts = result["timestamp"]
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0.0


# ---------------------------------------------------------------------------
# Phase 2.8 — Architectural boundary: no CheckpointManager import
# ---------------------------------------------------------------------------


def test_health_module_does_not_import_utils_checkpoint() -> None:
    """Verify the AAP §0.5.1.2 boundary: ``utils/health.py`` imports ``config`` only.

    The readiness probe parses the checkpoint JSON itself rather than
    delegating to :class:`utils.checkpoint.CheckpointManager`. This
    preserves two invariants:

    1. The probe is a pure ``config`` + filesystem reader, cheap to
       invoke and free of import-time cost from
       ``utils.logger`` / ``utils.correlation``.
    2. If ``CheckpointManager`` has a bug (e.g. in its thread lock or
       load semantics), the probe remains useful as an independent
       sanity check.

    The check uses :mod:`ast` rather than brittle substring matching so
    that legitimate prose references to ``CheckpointManager`` in the
    module and function docstrings — which intentionally explain *why*
    the class is not used — do not trip the test. The architectural
    invariant is about actual import and symbol usage, not about
    prohibiting documentation that names the collaborator. An
    ``ast.Import`` or ``ast.ImportFrom`` referencing
    :mod:`utils.checkpoint` would produce real runtime coupling; a
    prose mention cannot.
    """
    import ast
    import inspect

    import utils.health

    source = inspect.getsource(utils.health)
    tree = ast.parse(source)

    # 1) No ``from utils.checkpoint import ...`` anywhere in the module.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            assert "utils.checkpoint" not in module_name, (
                "utils/health.py must not import from utils.checkpoint "
                f"(found: from {module_name} import ...); "
                "AAP §0.5.1.2 architectural boundary"
            )
            imported_symbols = {alias.name for alias in node.names}
            assert "CheckpointManager" not in imported_symbols, (
                "utils/health.py must not import the CheckpointManager symbol "
                "(AAP §0.5.1.2 architectural boundary)"
            )

    # 2) No ``import utils.checkpoint`` (or ``import utils.checkpoint as ...``)
    #    anywhere in the module.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "utils.checkpoint" not in alias.name, (
                    "utils/health.py must not import utils.checkpoint "
                    f"(found: import {alias.name}); "
                    "AAP §0.5.1.2 architectural boundary"
                )

    # 3) No executable reference to the ``CheckpointManager`` name. Prose
    #    mentions live inside string-literal docstrings (``ast.Constant``
    #    nodes in 3.8+) and are therefore not ``ast.Name`` references; the
    #    walk below only flags real symbol usage.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "CheckpointManager", (
                "utils/health.py must not reference the CheckpointManager "
                "symbol in executable code (AAP §0.5.1.2 architectural "
                "boundary)"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr != "CheckpointManager", (
                "utils/health.py must not access a CheckpointManager "
                "attribute in executable code (AAP §0.5.1.2 architectural "
                "boundary)"
            )
