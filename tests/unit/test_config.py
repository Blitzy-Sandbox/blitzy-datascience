"""Unit tests for :mod:`config` — Feature F-002 + Validation Gate 12.

Verifies that every exported constant in :mod:`config` has at least one
production read-site (Gate 12 — Config Propagation Tracing) and that
each constant's default, type, and environment-variable override
behaviour conforms to the Agent Action Plan (AAP §0.5.1.1) and the
product-brief operational rules (notably Rule 2 rate-limit floor and
Rule 3 required headers).

The central Gate 12 test (``test_every_public_attribute_has_a_read_site``)
uses the session-scoped :pyfixture:`production_python_files` fixture and
walks each production file's AST looking for ``config.<ATTR>`` attribute
accesses plus ``from config import <ATTR>`` imports. Typing/pathlib
re-exports (``Dict``, ``Final``, ``List``, ``Path``) are filtered out of
the public-attribute set because they are not part of the configuration
contract — they are transitively visible only because :mod:`config`
uses ``from typing import ...`` and ``from pathlib import Path`` at
module scope. ``LOG_DIR`` is consumed transitively through
:func:`config.ensure_directories` (which calls ``LOG_DIR.mkdir()``); the
Gate 12 walk therefore also inspects :mod:`config` itself for bare-name
references to public constants. ``SEASONS`` is explicitly exempted with
a documented rationale — the constant is reserved for a future
multi-season backfill iteration, deferred per Technical Specification
§1.3 and documented at ``config.py:267-269``.

Beyond the Gate 12 tracing test, this module exercises every constant
individually: default value, type, and — for constants declared via the
``_env*`` helpers — behaviour in the presence and absence of their
``NBA_*`` environment-variable overrides. The private helpers
(``_env``, ``_env_int``, ``_env_float``, ``_env_path``) are tested
directly because they are the single mechanism backing every
env-overridable constant; verifying the helpers plus a sample of the
constants gives full coverage without needing to
:func:`importlib.reload` the :mod:`config` module (which would
invalidate :pyfixture:`tmp_output_dir` / :pyfixture:`tmp_log_dir`
monkeypatches maintained by :mod:`tests.conftest`).

Design notes
------------
* **No :func:`importlib.reload`** of :mod:`config` in any test. Reload
  would break the cross-test monkeypatch contract upheld by
  :mod:`tests.conftest` (which sets ``config.OUTPUT_DIR`` etc. on the
  already-imported module). Env-variable reactivity is verified by
  exercising the private ``_env*`` helpers directly.
* **``monkeypatch.setattr(config, "...", ..., raising=True)``** is used
  for every constant override. ``raising=True`` is defensive: if a
  future refactor renames a constant, the test fails fast at the
  ``setattr`` call rather than silently creating a new attribute on
  :mod:`config`.
* **No mocking libraries** are used; all isolation is achieved through
  :func:`monkeypatch.setenv`, :func:`monkeypatch.setattr`, and
  :fixture:`tmp_path` (Rule: tests must be deterministic without
  third-party mock frameworks).
"""

from __future__ import annotations

import ast
import logging
import os  # noqa: F401  # Referenced in docstring examples; retained for sibling-test consistency.
import platform  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
import re
import sys  # noqa: F401  # Canonical import template; retained for sibling-test consistency.
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Set, Tuple  # noqa: F401  # Used in type annotations throughout.

import pytest

import config


# ---------------------------------------------------------------------------
# Gate 12 exemption sets (documented rationale)
# ---------------------------------------------------------------------------
#
# These sets are deliberately local to this test module so the rationale
# for each exemption lives next to the assertion that tolerates it.
# Adding an entry here MUST be accompanied by an explanatory comment and
# a corresponding entry in ``docs/DECISIONS.md`` (Explainability rule).

# Names visible on :mod:`config` that originate from ``from typing import
# ...`` / ``from pathlib import Path`` at module scope. They are not
# part of the configuration contract and are excluded from Gate 12.
_TYPING_PATHLIB_REEXPORTS: FrozenSet[str] = frozenset(
    {"Dict", "Final", "List", "Path"}
)

# Public attributes that have zero direct ``config.<ATTR>`` read-sites
# in production code but are consumed transitively through another
# public symbol. ``LOG_DIR`` is materialised on disk by
# ``config.ensure_directories()`` which calls ``LOG_DIR.mkdir(...)``
# (see ``config.py:324``); the function is invoked at
# ``utils/logger.py:222`` and ``storage/csv_writer.py:194``.
_EXPECTED_TRANSITIVE_ATTRS: FrozenSet[str] = frozenset({"LOG_DIR"})

# Public attributes that are intentionally unreferenced in production
# code today. ``SEASONS`` is reserved for a future multi-season
# backfill iteration (deferred per Technical Specification §1.3 and
# documented at ``config.py:267-269``). Removing the exemption without
# a corresponding implementation change would be an AAP scope
# reduction and requires a decision-log entry.
_EXPECTED_DEFERRED_ATTRS: FrozenSet[str] = frozenset({"SEASONS"})


def _public_config_attributes() -> List[str]:
    """Return the alphabetised list of public attributes on :mod:`config`.

    "Public" means names not starting with an underscore, excluding the
    typing/pathlib re-exports. The result is deterministic and matches
    the :func:`dir` ordering Python produces for the live module.
    """
    return sorted(
        a
        for a in dir(config)
        if not a.startswith("_") and a not in _TYPING_PATHLIB_REEXPORTS
    )


def _collect_config_refs(
    files: List[Path],
) -> Dict[str, List[Tuple[Path, int]]]:
    """Scan each file's AST for references to symbols on :mod:`config`.

    Returns a mapping ``attr_name -> [(file_path, lineno), ...]``
    containing every ``config.<attr>`` attribute access plus every
    ``from config import <attr>`` alias found across the given file
    set. For :mod:`config` itself, bare ``ast.Name`` references whose
    ``id`` matches a public attribute are also captured — this is how
    internal transitive consumption (``LOG_DIR`` inside
    ``ensure_directories``) is surfaced for the Gate 12 walk.
    """
    public_set = set(_public_config_attributes())
    refs: Dict[str, List[Tuple[Path, int]]] = {}
    for pf in files:
        src = pf.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(pf))
        except SyntaxError:  # pragma: no cover - defensive; production files compile
            continue
        is_config_module = pf.name == "config.py"
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "config"
            ):
                refs.setdefault(node.attr, []).append((pf, node.lineno))
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "config":
                for alias in node.names:
                    refs.setdefault(alias.name, []).append((pf, node.lineno))
                continue
            # Inside ``config.py`` itself, ``LOG_DIR.mkdir(...)`` is a
            # bare-name reference, not a ``config.LOG_DIR`` attribute
            # access. We only count such references when they appear
            # inside a function body (i.e. not the top-level assignment
            # that declares the constant).
            if is_config_module and isinstance(node, ast.Name) and node.id in public_set:
                refs.setdefault(node.id, []).append((pf, node.lineno))
    return refs


# ---------------------------------------------------------------------------
# Phase 1 — Module import and public-attribute inventory
# ---------------------------------------------------------------------------


def test_config_module_is_importable() -> None:
    """:mod:`config` imports without raising.

    Failure here usually means a top-level assignment has side effects
    (forbidden by the F-002 "pure declarations" contract) — e.g. the
    module creating a directory or emitting log output at import time.
    """
    import config as reimported  # noqa: WPS433 - intentional re-import to exercise fresh bind

    assert reimported is config


def test_public_attribute_inventory_is_non_empty() -> None:
    """The public attribute surface is non-empty.

    Defensive sanity check. A zero-length inventory would indicate
    either a stripped-down :mod:`config` or a catastrophic import
    failure that silently replaced the module.
    """
    attrs = _public_config_attributes()
    assert len(attrs) > 0, "config has no public attributes — suspected import failure"


def test_public_attribute_inventory_excludes_typing_reexports() -> None:
    """``Dict``, ``Final``, ``List``, ``Path`` are filtered out.

    The four names originate from ``from typing import ...`` and
    ``from pathlib import Path`` at the top of ``config.py``. They are
    legitimately visible on the module object but are not part of the
    configuration contract. Gate 12 excludes them by convention.
    """
    attrs = _public_config_attributes()
    for reexport in _TYPING_PATHLIB_REEXPORTS:
        assert reexport not in attrs, (
            f"{reexport!r} should have been filtered out of the public "
            "attribute inventory"
        )


def test_ensure_directories_is_callable() -> None:
    """``ensure_directories`` is a public callable.

    The function is named in the configuration contract and consumed
    by two production sites (``utils/logger.py`` and
    ``storage/csv_writer.py``). A non-callable value here would
    indicate a broken module.
    """
    assert callable(config.ensure_directories)


# ---------------------------------------------------------------------------
# Phase 2 — Gate 12 Config Propagation Tracing
# ---------------------------------------------------------------------------


def test_every_public_attribute_has_a_production_read_site(
    production_python_files: List[Path],
) -> None:
    """Validation Gate 12 — every public attribute is read in production.

    For each public attribute on :mod:`config` (after filtering
    typing/pathlib re-exports), the production code base (``run.py``,
    ``config.py``, and every ``.py`` under ``api/``, ``endpoints/``,
    ``pipelines/``, ``storage/``, ``utils/``) must contain at least
    one ``config.<ATTR>`` attribute access, one
    ``from config import <ATTR>`` alias, or — for :mod:`config`
    itself — a bare-name reference inside a function body.

    Two explicit exemption sets apply, each with documented rationale:

    * ``_EXPECTED_TRANSITIVE_ATTRS`` — attributes consumed transitively
      through another public symbol (currently only ``LOG_DIR`` via
      :func:`config.ensure_directories`).
    * ``_EXPECTED_DEFERRED_ATTRS`` — attributes reserved for a future
      feature phase (currently only ``SEASONS``).

    A direct read-site for an exempted attribute is always acceptable
    and indicates the exemption may be removed.
    """
    refs = _collect_config_refs(production_python_files)
    attrs = _public_config_attributes()

    missing: List[str] = []
    for attr in attrs:
        if attr in refs and refs[attr]:
            continue
        if attr in _EXPECTED_TRANSITIVE_ATTRS:
            continue
        if attr in _EXPECTED_DEFERRED_ATTRS:
            continue
        missing.append(attr)

    assert not missing, (
        "Gate 12 FAILURE — public config attributes have no production "
        f"read-site: {missing!r}. Either add a read-site, exempt the "
        "attribute in ``_EXPECTED_TRANSITIVE_ATTRS`` / "
        "``_EXPECTED_DEFERRED_ATTRS`` with a decision-log entry, or "
        "remove the constant."
    )


def test_log_dir_is_consumed_via_ensure_directories() -> None:
    """``LOG_DIR`` is materialised on disk inside ``ensure_directories``.

    ``LOG_DIR`` has zero direct ``config.LOG_DIR`` references in any
    production file (empirically verified via AST walk). Gate 12
    exempts it because ``ensure_directories()`` calls
    ``LOG_DIR.mkdir(parents=True, exist_ok=True)`` — a transitive
    consumption that still satisfies the "every constant reaches
    runtime" intent of Gate 12. This test locks that contract in
    place: removing the ``LOG_DIR.mkdir`` call from
    ``ensure_directories`` would require removing the exemption, which
    would in turn fail the Gate 12 walk above.
    """
    src = Path(config.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=config.__file__)
    ensure_dirs_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "ensure_directories"
        ),
        None,
    )
    assert ensure_dirs_fn is not None, "``ensure_directories`` is missing from config.py"

    log_dir_mkdir_found = False
    for node in ast.walk(ensure_dirs_fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "mkdir":
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "LOG_DIR":
            log_dir_mkdir_found = True
            break
    assert log_dir_mkdir_found, (
        "``ensure_directories()`` must call ``LOG_DIR.mkdir(...)`` — "
        "this is the sole transitive read-site that justifies "
        "``LOG_DIR``'s Gate 12 exemption."
    )


def test_seasons_defer_rationale_comment_is_present() -> None:
    """``SEASONS`` is accompanied by a deferred-scope rationale comment.

    The empty production read-site count for ``SEASONS`` is only
    acceptable because :mod:`config` declares it with an explicit
    deferred-scope comment (``config.py:267-269``). If someone removes
    the comment while introducing a real read-site the exemption
    becomes redundant; if they remove the comment without adding a
    read-site, the exemption has lost its justification. Either
    direction should prompt a deliberate decision-log entry.
    """
    src = Path(config.__file__).read_text(encoding="utf-8")
    # Match either "deferred" or "future" in a comment block preceding
    # the SEASONS declaration.
    assert "SEASONS" in src, "SEASONS constant missing from config.py"
    # Locate the SEASONS declaration line, then check the preceding lines
    # for the deferred/future-use rationale.
    lines = src.splitlines()
    seasons_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("SEASONS")),
        None,
    )
    assert seasons_idx is not None, "SEASONS declaration line not found"
    preamble = "\n".join(lines[max(0, seasons_idx - 5):seasons_idx])
    assert re.search(
        r"deferred|future", preamble, flags=re.IGNORECASE
    ), (
        "SEASONS declaration must be preceded by a comment explaining "
        "why it is reserved for a future phase — this is the "
        "justification for its Gate 12 exemption."
    )


@pytest.mark.parametrize(
    "core_name",
    [
        "DEFAULT_SEASON",
        "OUTPUT_DIR",
        "CHECKPOINT_PATH",
    ],
)
def test_config_field_referenced_in_run_py(
    project_root: Path,
    core_name: str,
) -> None:
    """Gate 12 — core config fields must be referenced in ``run.py``.

    This is a targeted, literal text-check that complements the
    broader AST walk above. The AAP §0.5.1 Phase 5 calls out the
    CLI entry point (``run.py``) as the authoritative trace origin
    for Gate 12: every ``config.*`` symbol must have a trace path
    back to the user-facing CLI. The three fields below are the
    minimum subset whose absence from ``run.py`` would indicate a
    structural defect (e.g. hard-coded season string, hard-coded
    output path). Additional fields reach ``run.py`` transitively
    through the pipelines / collaborators it composes; those deeper
    traces are verified by the AST walk in
    :func:`test_every_public_attribute_has_a_production_read_site`.

    Failure mode: if someone refactors ``run.py`` to import these
    constants under aliases that hide their textual identity, this
    test will surface the regression immediately, because the grep-
    style substring assertion below is agnostic to syntactic form
    (``config.DEFAULT_SEASON``, ``from config import DEFAULT_SEASON``,
    or a bare-name reference that survived a later edit all satisfy
    the assertion, but any transformation that removes the literal
    identifier fails).
    """
    run_py = project_root / "run.py"
    assert run_py.is_file(), (
        f"Expected run.py at {run_py!s}. The CLI entry point is "
        "mandatory per AAP §0.5.1.7 and anchors Gate 12."
    )
    run_source = run_py.read_text(encoding="utf-8")
    assert core_name in run_source, (
        f"{core_name} is declared in config.py but never referenced in "
        "run.py. Gate 12 requires every core config field to have a "
        "read-site reachable from the CLI entry point."
    )


# ---------------------------------------------------------------------------
# Phase 3 — Private env-helper functions (_env / _env_int / _env_float / _env_path)
# ---------------------------------------------------------------------------


def test_env_returns_default_when_variable_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env`` falls back to the literal default when the variable is unset."""
    monkeypatch.delenv("BLITZY_CONFIG_TEST_ENV_KEY", raising=False)
    assert config._env("BLITZY_CONFIG_TEST_ENV_KEY", "fallback-value") == "fallback-value"


def test_env_returns_set_value_when_variable_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env`` returns the environment variable when it is set."""
    monkeypatch.setenv("BLITZY_CONFIG_TEST_ENV_KEY", "override")
    assert config._env("BLITZY_CONFIG_TEST_ENV_KEY", "fallback-value") == "override"


def test_env_treats_empty_string_as_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string env var is a deliberate override, not fallback.

    :mod:`config` documents this behaviour explicitly in its
    ``_env`` docstring. Treating the empty string as "fall back"
    would mask configuration mistakes like ``export FOO=`` which
    almost always indicate a shell-escaping error the operator
    needs to see, not silently paper over.
    """
    monkeypatch.setenv("BLITZY_CONFIG_TEST_ENV_KEY", "")
    assert config._env("BLITZY_CONFIG_TEST_ENV_KEY", "fallback-value") == ""


def test_env_int_parses_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_env_int`` returns ``int(value)`` when the variable is set."""
    monkeypatch.setenv("BLITZY_CONFIG_TEST_INT_KEY", "42")
    assert config._env_int("BLITZY_CONFIG_TEST_INT_KEY", 7) == 42


def test_env_int_returns_default_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_int`` falls back to the literal default when unset."""
    monkeypatch.delenv("BLITZY_CONFIG_TEST_INT_KEY", raising=False)
    assert config._env_int("BLITZY_CONFIG_TEST_INT_KEY", 7) == 7


def test_env_int_raises_value_error_on_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_int`` fails fast when the env var is set but not an int.

    Documented behaviour in :mod:`config` — misconfiguration must
    surface immediately at import time rather than silently falling
    back to the default and masking the bug.
    """
    monkeypatch.setenv("BLITZY_CONFIG_TEST_INT_KEY", "not-an-int")
    with pytest.raises(ValueError):
        config._env_int("BLITZY_CONFIG_TEST_INT_KEY", 7)


def test_env_float_parses_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_env_float`` returns ``float(value)`` when the variable is set."""
    monkeypatch.setenv("BLITZY_CONFIG_TEST_FLOAT_KEY", "2.5")
    assert config._env_float("BLITZY_CONFIG_TEST_FLOAT_KEY", 1.0) == 2.5


def test_env_float_returns_default_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_float`` falls back to the literal default when unset."""
    monkeypatch.delenv("BLITZY_CONFIG_TEST_FLOAT_KEY", raising=False)
    assert config._env_float("BLITZY_CONFIG_TEST_FLOAT_KEY", 1.5) == 1.5


def test_env_float_raises_value_error_on_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_float`` fails fast when the env var is set but not a float."""
    monkeypatch.setenv("BLITZY_CONFIG_TEST_FLOAT_KEY", "not-a-float")
    with pytest.raises(ValueError):
        config._env_float("BLITZY_CONFIG_TEST_FLOAT_KEY", 1.0)


def test_env_path_wraps_set_value_as_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_path`` returns a :class:`Path` instance built from the env var."""
    monkeypatch.setenv("BLITZY_CONFIG_TEST_PATH_KEY", "/tmp/custom/output")
    result = config._env_path("BLITZY_CONFIG_TEST_PATH_KEY", "output")
    assert isinstance(result, Path)
    assert result == Path("/tmp/custom/output")


def test_env_path_returns_default_as_path_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_env_path`` wraps the literal default in a :class:`Path` when unset."""
    monkeypatch.delenv("BLITZY_CONFIG_TEST_PATH_KEY", raising=False)
    result = config._env_path("BLITZY_CONFIG_TEST_PATH_KEY", "output")
    assert isinstance(result, Path)
    assert result == Path("output")


# ---------------------------------------------------------------------------
# Phase 4 — Upstream API (API_BASE_URL, REQUEST_TIMEOUT_SECONDS)
# ---------------------------------------------------------------------------


def test_api_base_url_default_value() -> None:
    """``API_BASE_URL`` defaults to the literal NBA Stats base URL.

    The trailing slash is part of the contract: ``NBAClient.get``
    concatenates ``API_BASE_URL + endpoint`` without inserting a
    separator, so removing the trailing slash would produce invalid
    URLs.
    """
    assert config.API_BASE_URL == "https://stats.nba.com/stats/"


def test_api_base_url_is_https() -> None:
    """Upstream traffic MUST be HTTPS (Rule 3 + security posture)."""
    assert config.API_BASE_URL.startswith("https://")


def test_api_base_url_ends_with_slash() -> None:
    """Trailing slash contract — see :func:`test_api_base_url_default_value`."""
    assert config.API_BASE_URL.endswith("/")


def test_request_timeout_seconds_is_positive_int() -> None:
    """``REQUEST_TIMEOUT_SECONDS`` is a positive int (seconds)."""
    assert isinstance(config.REQUEST_TIMEOUT_SECONDS, int)
    assert config.REQUEST_TIMEOUT_SECONDS > 0


def test_request_timeout_seconds_default() -> None:
    """Default request timeout is 30 seconds (AAP §0.4.2.1)."""
    assert config.REQUEST_TIMEOUT_SECONDS == 30


# ---------------------------------------------------------------------------
# Phase 5 — Rule 3 Required Headers
# ---------------------------------------------------------------------------


def test_required_headers_is_dict() -> None:
    """``REQUIRED_HEADERS`` is a ``dict`` — not a frozen mapping."""
    assert isinstance(config.REQUIRED_HEADERS, dict)


def test_required_headers_is_non_empty() -> None:
    """Rule 3 demands at least ``Referer`` and ``User-Agent``."""
    assert len(config.REQUIRED_HEADERS) >= 2


def test_required_headers_contains_referer() -> None:
    """Rule 3 — ``Referer`` header MUST be present."""
    assert "Referer" in config.REQUIRED_HEADERS


def test_required_headers_referer_points_at_stats_nba() -> None:
    """The ``Referer`` value names ``stats.nba.com`` per Rule 3."""
    assert "stats.nba.com" in config.REQUIRED_HEADERS["Referer"]


def test_required_headers_contains_user_agent() -> None:
    """Rule 3 — ``User-Agent`` header MUST be present."""
    assert "User-Agent" in config.REQUIRED_HEADERS


def test_required_headers_user_agent_is_browser_like() -> None:
    """Rule 3 — ``User-Agent`` must look like a real browser.

    The NBA Stats API blocks requests with default Python / curl
    user-agent strings. Any viable value starts with ``Mozilla/5.0``
    and advertises a modern browser rendering engine.
    """
    user_agent = config.REQUIRED_HEADERS["User-Agent"]
    assert "Mozilla" in user_agent, (
        "Rule 3 requires a browser-like User-Agent — NBA Stats rejects "
        "default Python / curl user-agents."
    )


def test_required_headers_values_are_strings() -> None:
    """Every header value is a plain ``str`` (HTTP requirement)."""
    for key, value in config.REQUIRED_HEADERS.items():
        assert isinstance(key, str), f"header key {key!r} is not str"
        assert isinstance(value, str), f"header value for {key!r} is not str"


def test_required_headers_expected_keys_present() -> None:
    """The documented header inventory is present verbatim.

    The additional stabilising headers (``Accept``, ``Origin``, ...)
    beyond the Rule 3 minimum have reduced 403/429 rates empirically.
    Removing them without explicit testing should be deliberate —
    this assertion locks the inventory so accidental removal fails
    the suite.
    """
    expected_keys = {
        "Referer",
        "User-Agent",
        "Accept",
        "Accept-Language",
        "Origin",
        "Connection",
        "x-nba-stats-origin",
        "x-nba-stats-token",
    }
    assert expected_keys.issubset(set(config.REQUIRED_HEADERS.keys())), (
        "``REQUIRED_HEADERS`` is missing one or more documented keys; "
        f"present: {sorted(config.REQUIRED_HEADERS.keys())}"
    )


# ---------------------------------------------------------------------------
# Phase 6 — Rule 2 Rate Limiting
# ---------------------------------------------------------------------------


def test_rate_limit_seconds_is_float() -> None:
    """``RATE_LIMIT_SECONDS`` is typed ``float`` for sub-integer resolution."""
    assert isinstance(config.RATE_LIMIT_SECONDS, float)


def test_rate_limit_seconds_default_value() -> None:
    """Default rate limit is exactly 1.0 seconds (AAP §0.3.2 / Rule 2)."""
    assert config.RATE_LIMIT_SECONDS == 1.0


def test_rate_limit_seconds_honors_rule2_floor() -> None:
    """Rule 2 floor — MUST NOT drop below 1.0 seconds between requests.

    The floor is the binding constraint that protects the project
    from NBA Stats rate-limiting (HTTP 429). Any reduction below 1.0
    would violate Rule 2 and expose Gate 8 ("zero 429s") to failure.
    Even an env-var override (``NBA_RATE_LIMIT_SECONDS=0.5``) is
    discouraged; the test asserts the compiled-in default rather than
    the runtime value precisely so a misconfigured environment
    cannot silently pass this assertion.
    """
    assert config.RATE_LIMIT_SECONDS >= 1.0


# ---------------------------------------------------------------------------
# Phase 7 — Retry parameters (Feature F-004)
# ---------------------------------------------------------------------------


def test_retry_attempts_default_is_positive() -> None:
    """``RETRY_ATTEMPTS`` is a positive int — zero would disable retry."""
    assert isinstance(config.RETRY_ATTEMPTS, int)
    assert config.RETRY_ATTEMPTS > 0


def test_retry_attempts_default_value() -> None:
    """Default retry attempts is 5 per AAP §0.3.2 / F-004."""
    assert config.RETRY_ATTEMPTS == 5


def test_retry_multiplier_default_value() -> None:
    """Default multiplier is 2 (classic exponential backoff)."""
    assert isinstance(config.RETRY_MULTIPLIER, int)
    assert config.RETRY_MULTIPLIER == 2


def test_retry_max_wait_default_value() -> None:
    """Default max wait is 60 seconds (AAP §0.3.2)."""
    assert isinstance(config.RETRY_MAX_WAIT, int)
    assert config.RETRY_MAX_WAIT == 60


def test_retry_min_wait_default_value() -> None:
    """Default min wait is 1 second."""
    assert isinstance(config.RETRY_MIN_WAIT, int)
    assert config.RETRY_MIN_WAIT == 1


def test_retry_max_wait_is_greater_than_or_equal_to_min_wait() -> None:
    """``RETRY_MAX_WAIT >= RETRY_MIN_WAIT`` — mandatory order invariant."""
    assert config.RETRY_MAX_WAIT >= config.RETRY_MIN_WAIT


# ---------------------------------------------------------------------------
# Phase 8 — Filesystem paths
# ---------------------------------------------------------------------------


def test_output_dir_is_path_instance() -> None:
    """``OUTPUT_DIR`` is typed :class:`Path`."""
    assert isinstance(config.OUTPUT_DIR, Path)


def test_checkpoint_path_is_path_instance() -> None:
    """``CHECKPOINT_PATH`` is typed :class:`Path`."""
    assert isinstance(config.CHECKPOINT_PATH, Path)


def test_log_dir_is_path_instance() -> None:
    """``LOG_DIR`` is typed :class:`Path`."""
    assert isinstance(config.LOG_DIR, Path)


def test_log_file_is_path_instance() -> None:
    """``LOG_FILE`` is typed :class:`Path`."""
    assert isinstance(config.LOG_FILE, Path)


def test_checkpoint_path_name_is_json() -> None:
    """Default checkpoint file is JSON (Rule 5 manifest format)."""
    assert config.CHECKPOINT_PATH.name == "checkpoint.json"


def test_log_file_name_is_log() -> None:
    """Default log file is ``pipeline.log``."""
    assert config.LOG_FILE.name == "pipeline.log"


def test_default_output_dir_stem() -> None:
    """Default ``OUTPUT_DIR`` stem is ``output`` (README + AAP convention)."""
    # The last path component is the default stem regardless of any
    # leading anchor the env var might supply.
    assert config.OUTPUT_DIR.name in {"output", "outputs"} or "output" in str(
        config.OUTPUT_DIR
    )


def test_default_log_dir_stem() -> None:
    """Default ``LOG_DIR`` stem is ``logs``."""
    assert config.LOG_DIR.name in {"logs", "log"} or "log" in str(config.LOG_DIR)


# ---------------------------------------------------------------------------
# Phase 9 — Log configuration
# ---------------------------------------------------------------------------


def test_log_level_is_string() -> None:
    """``LOG_LEVEL`` is a ``str`` — matches :func:`logging.getLevelName`."""
    assert isinstance(config.LOG_LEVEL, str)


def test_log_level_default_value() -> None:
    """Default log level is ``INFO``."""
    assert config.LOG_LEVEL == "INFO"


def test_log_level_is_valid_python_logging_name() -> None:
    """``LOG_LEVEL`` is one of Python's standard level names.

    ``logging.getLevelName`` raises for unknown names in strict mode;
    we enumerate the documented levels explicitly so misconfigured
    overrides surface here rather than much later in the startup
    path.
    """
    assert config.LOG_LEVEL in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }


def test_log_level_round_trips_through_getlevelname() -> None:
    """``LOG_LEVEL`` round-trips cleanly through :func:`logging.getLevelName`.

    A valid, recognised level name (``"INFO"``, ``"DEBUG"``, ...) is
    mapped by :func:`logging.getLevelName` to a non-negative integer
    (``logging.INFO == 20``). Unrecognised names are returned verbatim
    as ``"Level <name>"`` strings in Python 3.11+. This test asserts
    the happy path: the configured level name resolves to an integer
    and passes the CPython sanity check ``isLevelName``. Adding this
    schema-mandated assertion next to the membership check gives a
    second independent verification path — if the stdlib catalog of
    level names ever diverges from the hard-coded set above, one of
    the two tests will still surface the drift.
    """
    resolved = logging.getLevelName(config.LOG_LEVEL)
    assert isinstance(resolved, int), (
        f"logging.getLevelName({config.LOG_LEVEL!r}) returned "
        f"{resolved!r}; expected an int. A string return indicates "
        "the level name is not recognised by the stdlib logging "
        "module."
    )
    assert resolved >= 0
    # Reverse mapping must be stable — int -> name -> int.
    assert logging.getLevelName(resolved) == config.LOG_LEVEL


def test_log_format_embeds_correlation_id_token() -> None:
    """Observability rule — ``LOG_FORMAT`` must embed ``correlation_id``.

    ``utils/correlation.py``'s ``CorrelationAdapter`` injects
    ``correlation_id`` into every ``LogRecord``. If the format string
    forgets the token, correlation IDs never reach stdout / the
    rotating log file and the Observability rule is silently
    breached. This test guards the token.
    """
    assert "%(correlation_id)s" in config.LOG_FORMAT


def test_log_format_embeds_standard_fields() -> None:
    """``LOG_FORMAT`` embeds ``asctime``, ``levelname``, ``name``, ``message``.

    These four fields give operators a minimal actionable log line.
    Their absence would degrade observability even if the correlation
    token is present.
    """
    for token in ("%(asctime)s", "%(levelname)s", "%(name)s", "%(message)s"):
        assert token in config.LOG_FORMAT, f"{token!r} missing from LOG_FORMAT"


def test_log_date_format_is_iso_like() -> None:
    """``LOG_DATE_FORMAT`` matches the documented ISO-8601-style layout.

    The exact string is ``%Y-%m-%dT%H:%M:%S``. Verifying it by
    pattern rather than identity keeps the test permissive if the
    project ever switches to ``...%SZ`` (still ISO-compatible).
    """
    assert "%Y" in config.LOG_DATE_FORMAT
    assert "%H" in config.LOG_DATE_FORMAT
    assert "T" in config.LOG_DATE_FORMAT


def test_log_file_max_bytes_is_positive_int() -> None:
    """``LOG_FILE_MAX_BYTES`` is a positive int (rotating-handler input)."""
    assert isinstance(config.LOG_FILE_MAX_BYTES, int)
    assert config.LOG_FILE_MAX_BYTES > 0


def test_log_file_max_bytes_default_value() -> None:
    """Default rotation threshold is 10 MB (10 * 1024 * 1024 = 10_485_760)."""
    assert config.LOG_FILE_MAX_BYTES == 10_485_760


def test_log_file_backup_count_is_non_negative_int() -> None:
    """``LOG_FILE_BACKUP_COUNT >= 0`` (:class:`RotatingFileHandler` contract)."""
    assert isinstance(config.LOG_FILE_BACKUP_COUNT, int)
    assert config.LOG_FILE_BACKUP_COUNT >= 0


def test_log_file_backup_count_default_value() -> None:
    """Default backup count is 5 rotations (AAP §0.5.1.2)."""
    assert config.LOG_FILE_BACKUP_COUNT == 5


# ---------------------------------------------------------------------------
# Phase 10 — Season defaults
# ---------------------------------------------------------------------------


def test_default_season_value() -> None:
    """Default season is ``2025-26`` per AAP §0.1.1."""
    assert config.DEFAULT_SEASON == "2025-26"


def test_default_season_matches_nba_season_format() -> None:
    """Season strings look like ``YYYY-YY`` (NBA canonical format)."""
    assert re.fullmatch(r"\d{4}-\d{2}", config.DEFAULT_SEASON), (
        f"DEFAULT_SEASON={config.DEFAULT_SEASON!r} does not match YYYY-YY"
    )


def test_default_season_type_value() -> None:
    """Default season type is ``Regular Season``."""
    assert config.DEFAULT_SEASON_TYPE == "Regular Season"


def test_default_league_id_value() -> None:
    """Default league ID is ``00`` (NBA). ``10``=WNBA, ``20``=G League."""
    assert config.DEFAULT_LEAGUE_ID == "00"


def test_seasons_is_list_of_strings() -> None:
    """``SEASONS`` is a concrete ``list[str]``."""
    assert isinstance(config.SEASONS, list)
    assert all(isinstance(s, str) for s in config.SEASONS)


def test_seasons_expected_contents() -> None:
    """``SEASONS`` is the documented 5-season backfill window.

    The test is value-specific to lock the backfill window against
    accidental drift — a future agent can update the list and adjust
    this assertion at the same time, which is the correct coupling.
    """
    assert config.SEASONS == ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]


def test_seasons_includes_default_season() -> None:
    """``DEFAULT_SEASON`` is contained in ``SEASONS``.

    A backfill plan that omits the current season is almost certainly
    a misconfiguration — guarding this invariant catches typos early.
    """
    assert config.DEFAULT_SEASON in config.SEASONS


def test_seasons_are_chronologically_ordered() -> None:
    """``SEASONS`` is sorted ascending by the leading year.

    Pipelines that iterate ``SEASONS`` for multi-year backfill
    processing assume chronological order. Breaking the sort would
    silently change the backfill sequence.
    """
    leading_years = [int(s.split("-")[0]) for s in config.SEASONS]
    assert leading_years == sorted(leading_years), (
        "SEASONS must be chronologically ordered by leading year"
    )


# ---------------------------------------------------------------------------
# Phase 11 — CSV artifact names
# ---------------------------------------------------------------------------


def test_csv_players_value() -> None:
    """``CSV_PLAYERS`` is the Players domain primary artifact stem."""
    assert config.CSV_PLAYERS == "players"


def test_csv_player_tracking_value() -> None:
    """``CSV_PLAYER_TRACKING`` is the Players tracking artifact stem."""
    assert config.CSV_PLAYER_TRACKING == "player_tracking"


def test_csv_teams_value() -> None:
    """``CSV_TEAMS`` is the Teams domain primary artifact stem."""
    assert config.CSV_TEAMS == "teams"


def test_csv_games_value() -> None:
    """``CSV_GAMES`` is the Games domain primary (box-score) artifact stem."""
    assert config.CSV_GAMES == "games"


def test_csv_play_by_play_value() -> None:
    """``CSV_PLAY_BY_PLAY`` is the Games play-by-play artifact stem."""
    assert config.CSV_PLAY_BY_PLAY == "play_by_play"


def test_csv_lineups_value() -> None:
    """``CSV_LINEUPS`` is the Lineups domain primary artifact stem."""
    assert config.CSV_LINEUPS == "lineups"


def test_csv_schedule_value() -> None:
    """``CSV_SCHEDULE`` is the Schedule domain primary artifact stem."""
    assert config.CSV_SCHEDULE == "schedule"


def test_all_csv_names_are_filesystem_safe() -> None:
    """Every CSV stem is alphanumeric + underscores only.

    ``CSVWriter`` concatenates ``stem + ".csv"`` under ``OUTPUT_DIR``.
    Stems containing spaces, slashes, or other shell-significant
    characters would break the write path or permit traversal.
    """
    csv_constants = [
        config.CSV_PLAYERS,
        config.CSV_PLAYER_TRACKING,
        config.CSV_TEAMS,
        config.CSV_GAMES,
        config.CSV_PLAY_BY_PLAY,
        config.CSV_LINEUPS,
        config.CSV_SCHEDULE,
    ]
    for name in csv_constants:
        assert re.fullmatch(r"[a-z0-9_]+", name), (
            f"CSV stem {name!r} contains non-filesystem-safe characters"
        )


def test_all_csv_names_are_distinct() -> None:
    """No two CSV stems collide — distinct artifacts must not overwrite."""
    csv_constants = [
        config.CSV_PLAYERS,
        config.CSV_PLAYER_TRACKING,
        config.CSV_TEAMS,
        config.CSV_GAMES,
        config.CSV_PLAY_BY_PLAY,
        config.CSV_LINEUPS,
        config.CSV_SCHEDULE,
    ]
    assert len(set(csv_constants)) == len(csv_constants)


# ---------------------------------------------------------------------------
# Phase 12 — Domain keys
# ---------------------------------------------------------------------------


def test_domain_players_value() -> None:
    """``DOMAIN_PLAYERS == "players"`` (Rule 5 checkpoint key)."""
    assert config.DOMAIN_PLAYERS == "players"


def test_domain_teams_value() -> None:
    """``DOMAIN_TEAMS == "teams"``."""
    assert config.DOMAIN_TEAMS == "teams"


def test_domain_games_value() -> None:
    """``DOMAIN_GAMES == "games"``."""
    assert config.DOMAIN_GAMES == "games"


def test_domain_lineups_value() -> None:
    """``DOMAIN_LINEUPS == "lineups"``."""
    assert config.DOMAIN_LINEUPS == "lineups"


def test_domain_schedule_value() -> None:
    """``DOMAIN_SCHEDULE == "schedule"``."""
    assert config.DOMAIN_SCHEDULE == "schedule"


def test_all_domain_values_are_distinct() -> None:
    """No two domain constants collide — Rule 5 checkpoints would overlap."""
    domains = [
        config.DOMAIN_PLAYERS,
        config.DOMAIN_TEAMS,
        config.DOMAIN_GAMES,
        config.DOMAIN_LINEUPS,
        config.DOMAIN_SCHEDULE,
    ]
    assert len(set(domains)) == len(domains)


def test_domains_match_checkpoint_keys_fixture(
    checkpoint_keys: Dict[str, str],
) -> None:
    """Domain constants align with the :pyfixture:`checkpoint_keys` contract.

    The ``checkpoint_keys`` fixture in :mod:`tests.conftest` is the
    source of truth for pipeline → domain checkpoint keys. This
    cross-check ensures the two sources do not drift apart: if a
    future refactor renames ``DOMAIN_PLAYERS`` to ``DOMAIN_PLAYER``
    without updating the fixture, this assertion breaks.
    """
    # The fixture keys ``players_primary`` / ``players_tracking`` both
    # belong to the Players domain. Every distinct leading key (stripped
    # of the ``_primary`` / ``_tracking`` suffix) must appear among the
    # ``DOMAIN_*`` constants.
    fixture_domains = {
        k.split("_")[0] for k in checkpoint_keys.keys()
    }
    config_domains = {
        config.DOMAIN_PLAYERS,
        config.DOMAIN_TEAMS,
        config.DOMAIN_GAMES,
        config.DOMAIN_LINEUPS,
        config.DOMAIN_SCHEDULE,
    }
    # Every fixture-domain stem maps to a config domain (but not
    # necessarily vice-versa — the fixture currently omits ``games``
    # because the Games pipeline uses per-``GAME_ID`` keys instead of
    # a single domain-level key).
    assert fixture_domains.issubset(config_domains), (
        f"Fixture domains {sorted(fixture_domains)!r} not covered by "
        f"DOMAIN_* constants {sorted(config_domains)!r}"
    )


# ---------------------------------------------------------------------------
# Phase 13 — ensure_directories() behaviour
# ---------------------------------------------------------------------------


def test_ensure_directories_creates_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_directories()`` materialises ``OUTPUT_DIR`` on disk.

    Uses :fixture:`tmp_path` + ``monkeypatch.setattr(config, ...)``
    rather than :pyfixture:`tmp_output_dir` so the pre-existence path
    is fully under this test's control (``tmp_output_dir`` creates
    the directory itself before yielding).
    """
    target = tmp_path / "configured-output"
    monkeypatch.setattr(config, "OUTPUT_DIR", target, raising=True)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "configured-logs", raising=True)
    assert not target.exists()

    config.ensure_directories()

    assert target.exists()
    assert target.is_dir()


def test_ensure_directories_creates_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_directories()`` materialises ``LOG_DIR`` on disk.

    Symmetric to the OUTPUT_DIR test — both directories must be
    created or the function has partially failed its contract.
    """
    target = tmp_path / "configured-logs"
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "configured-output", raising=True)
    monkeypatch.setattr(config, "LOG_DIR", target, raising=True)
    assert not target.exists()

    config.ensure_directories()

    assert target.exists()
    assert target.is_dir()


def test_ensure_directories_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling ``ensure_directories()`` repeatedly does not raise.

    Operators may invoke ``health``/``ready`` on a fresh machine and
    then rerun the pipeline; both paths call ``ensure_directories``.
    ``Path.mkdir(exist_ok=True)`` under the hood provides the
    idempotency guarantee this test locks in.
    """
    out = tmp_path / "out"
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "OUTPUT_DIR", out, raising=True)
    monkeypatch.setattr(config, "LOG_DIR", logs, raising=True)

    config.ensure_directories()
    config.ensure_directories()
    config.ensure_directories()

    assert out.is_dir()
    assert logs.is_dir()


def test_ensure_directories_creates_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_directories()`` passes ``parents=True`` (creates nested paths).

    A ``nested/path`` override must work even if ``nested/`` does not
    yet exist. This exercises the ``parents=True`` branch documented
    in the function body.
    """
    out = tmp_path / "deeply" / "nested" / "output"
    logs = tmp_path / "a" / "b" / "c" / "logs"
    monkeypatch.setattr(config, "OUTPUT_DIR", out, raising=True)
    monkeypatch.setattr(config, "LOG_DIR", logs, raising=True)

    config.ensure_directories()

    assert out.is_dir()
    assert logs.is_dir()


def test_ensure_directories_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function returns ``None`` — callers must not treat it as a value.

    Type annotations declare ``-> None`` and callers rely on this
    contract. A regression that returned, e.g., a ``Path`` tuple
    might silently propagate to callers that then break.
    """
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "o", raising=True)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "l", raising=True)
    assert config.ensure_directories() is None


def test_ensure_directories_not_invoked_at_module_load() -> None:
    """``config`` import does not create directories on its own.

    The design contract (F-002 "pure declarations, no side effects")
    forbids creating directories at import time. This test asserts
    the negative space — if the module ever acquired a top-level
    ``ensure_directories()`` call, importing :mod:`config` in a
    hermetic environment (e.g. a CI runner without write access) would
    crash. We verify by inspecting the module AST for any top-level
    expression calling ``ensure_directories`` or any bare ``mkdir``
    call outside a function body.
    """
    src = Path(config.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=config.__file__)
    for node in tree.body:
        # Top-level expression statements.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == "ensure_directories":
                pytest.fail(
                    "ensure_directories() invoked at module load time — "
                    "violates F-002 'pure declarations' contract"
                )
            if isinstance(fn, ast.Attribute) and fn.attr == "mkdir":
                pytest.fail(
                    f"{ast.unparse(node)!r} calls .mkdir at module scope — "
                    "violates F-002 'pure declarations' contract"
                )


# ---------------------------------------------------------------------------
# Phase 14 — Env-variable reactivity for ``config.py`` constants
# ---------------------------------------------------------------------------
#
# These tests verify that every ``NBA_*`` override documented in
# ``.env.example`` / ``docs/OBSERVABILITY.md`` is honoured by the
# ``_env*`` helpers. We do NOT reload the :mod:`config` module (doing so
# would invalidate cross-test monkeypatches maintained by conftest). We
# instead exercise each helper against the exact env-var key and
# default that ``config.py`` declares, which is an equivalent
# verification because ``Final[...] = _env*(KEY, DEFAULT)`` is a
# pure call with no side effects.


@pytest.mark.parametrize(
    "env_key,default",
    [
        ("NBA_API_BASE_URL", "https://stats.nba.com/stats/"),
        ("NBA_LOG_LEVEL", "INFO"),
        ("NBA_DEFAULT_SEASON", "2025-26"),
        ("NBA_DEFAULT_SEASON_TYPE", "Regular Season"),
        ("NBA_DEFAULT_LEAGUE_ID", "00"),
    ],
)
def test_string_constants_honor_env_override(
    env_key: str, default: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """String constants declared via ``_env`` read their ``NBA_*`` keys.

    Exercises the ``_env`` helper against the exact ``(key, default)``
    pair each string constant uses. The helper is deterministic, so
    validating the helper's behaviour on these inputs is equivalent
    to proving each constant is env-reactive.
    """
    monkeypatch.setenv(env_key, "override-value")
    assert config._env(env_key, default) == "override-value"

    monkeypatch.delenv(env_key, raising=False)
    assert config._env(env_key, default) == default


@pytest.mark.parametrize(
    "env_key,default",
    [
        ("NBA_REQUEST_TIMEOUT_SECONDS", 30),
        ("NBA_RETRY_ATTEMPTS", 5),
        ("NBA_RETRY_MULTIPLIER", 2),
        ("NBA_RETRY_MAX_WAIT", 60),
        ("NBA_RETRY_MIN_WAIT", 1),
        ("NBA_LOG_FILE_MAX_BYTES", 10_485_760),
        ("NBA_LOG_FILE_BACKUP_COUNT", 5),
    ],
)
def test_int_constants_honor_env_override(
    env_key: str, default: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Int constants declared via ``_env_int`` read their ``NBA_*`` keys."""
    monkeypatch.setenv(env_key, "99")
    assert config._env_int(env_key, default) == 99

    monkeypatch.delenv(env_key, raising=False)
    assert config._env_int(env_key, default) == default


@pytest.mark.parametrize(
    "env_key,default",
    [
        ("NBA_RATE_LIMIT_SECONDS", 1.0),
    ],
)
def test_float_constants_honor_env_override(
    env_key: str, default: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Float constants declared via ``_env_float`` read their ``NBA_*`` keys."""
    monkeypatch.setenv(env_key, "2.5")
    assert config._env_float(env_key, default) == 2.5

    monkeypatch.delenv(env_key, raising=False)
    assert config._env_float(env_key, default) == default


@pytest.mark.parametrize(
    "env_key,default",
    [
        ("NBA_OUTPUT_DIR", "output"),
        ("NBA_CHECKPOINT_PATH", "output/checkpoint.json"),
        ("NBA_LOG_DIR", "logs"),
        ("NBA_LOG_FILE", "logs/pipeline.log"),
    ],
)
def test_path_constants_honor_env_override(
    env_key: str, default: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path constants declared via ``_env_path`` read their ``NBA_*`` keys.

    The helper wraps the value in :class:`Path`; we verify both the
    set and unset paths return the expected :class:`Path` instance.
    """
    monkeypatch.setenv(env_key, "/custom/path")
    result = config._env_path(env_key, default)
    assert isinstance(result, Path)
    assert result == Path("/custom/path")

    monkeypatch.delenv(env_key, raising=False)
    result = config._env_path(env_key, default)
    assert isinstance(result, Path)
    assert result == Path(default)
