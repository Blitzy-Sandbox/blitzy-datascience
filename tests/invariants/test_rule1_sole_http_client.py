"""Rule 1 -- Single HTTP Client.

Asserts that no module outside ``api/nba_client.py`` directly imports
``requests`` or calls ``requests.get``, ``requests.post``,
``requests.Session``, or ``requests.sessions.Session``. Verified by
scanning every ``.py`` file under the production tree (``endpoints/``,
``pipelines/``, ``storage/``, ``utils/``, ``run.py``, ``config.py``,
and ``api/__init__.py``).

The test uses pure-Python file walking and regex matching rather than
``subprocess.run(["grep", ...])`` so it is portable across operating
systems and produces a precise, line-numbered failure message.

Lines inside triple-quoted strings (module docstrings, class and
function docstrings, multi-line string literals) are excluded from the
scan so that reference documentation that legitimately mentions a
forbidden call site -- such as ``config.py``'s Gate-12 read-site trace
table that names ``requests.get(..., timeout=...)`` as the canonical
consumer of :data:`config.REQUEST_TIMEOUT_SECONDS` -- does not generate
a false positive. Real, runtime-executed violations in module code are
still caught because Python source code that imports or calls
``requests`` always lives outside triple-quoted string regions.

Why this file is safe from its own invariant
--------------------------------------------
This test file itself lives under ``tests/invariants/`` and is NEVER
scanned by :func:`_scan_targets` because ``tests/`` is deliberately
absent from :data:`SCAN_DIRS` and :data:`SCAN_ROOT_FILES`. The raw
regex source strings embedded in :data:`FORBIDDEN_PATTERNS` spell
``requests\\.`` with an explicit backslash before the dot, so even a
naive grep over this file would not mistake the regex literals
themselves for forbidden call sites.

Authoritative sources
---------------------
* Product brief section 5 Rule 1 -- binding rule text and original
  verification recipe.
* Agent Action Plan (AAP) section 0.2.3 -- registers this file as a
  required deliverable.
* AAP section 0.4.4 -- integration-invariants table (Rule 1 row).
* AAP section 0.5.1.8 -- Group 8 invariant tests.
* AAP section 0.7.2.1 -- Rule 1 binding constraint.
* AAP section 0.7.5 -- Rule-to-Gate verification matrix
  (Rule 1 maps to Gates 1 and 8).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest


# ---------------------------------------------------------------------------
# Module-level pytest marker
# ---------------------------------------------------------------------------
#
# Every test in this file is auto-tagged with the ``invariant`` marker,
# declared in ``pytest.ini``. This keeps invariant tests selectable via
# ``pytest -m invariant`` and excludable via ``pytest -m "not invariant"``.
# The marker is NOT ``integration`` because invariants require zero
# network access and zero live filesystem writes -- they only read
# source files -- so they run in the default suite.
pytestmark = pytest.mark.invariant


# ---------------------------------------------------------------------------
# Forbidden pattern library
# ---------------------------------------------------------------------------
#
# Rule 1 is enforced through five complementary regex patterns. Each
# pattern targets a distinct syntactic shape that would constitute a
# direct use of the ``requests`` library outside ``api/nba_client.py``:
#
# 1. Top-level ``import requests`` (with optional ``as alias``). The
#    ``^\s*`` anchor allows indentation (conditional or function-scope
#    imports) while the trailing ``\b`` word boundary prevents matches
#    against packages whose names start with ``requests`` (for example
#    a hypothetical ``requests_cache`` or ``requests_oauthlib``).
# 2. ``from requests import ...`` and ``from requests.submodule import
#    ...``. Same anchoring rules as pattern 1.
# 3. Direct HTTP-method call sites: ``requests.get(``,
#    ``requests.post(``, ``requests.put(``, ``requests.patch(``,
#    ``requests.delete(``, ``requests.head(``, ``requests.options(``,
#    ``requests.request(``. The method list deliberately extends beyond
#    the product brief's verbatim ``get``/``post`` to cover every HTTP
#    verb exposed by the ``requests`` top-level module -- defence-in-
#    depth against future endpoints that adopt non-GET verbs.
# 4. ``requests.Session(`` session construction via the top-level alias.
# 5. ``requests.sessions.Session(`` fully-qualified session construction.
#
# All patterns that reference the library name anchor it with ``\b`` so
# substrings such as ``pyrequests`` or ``my_requests_helper`` do not
# false-positive. The trailing ``\(`` on call-site patterns ensures a
# bare attribute reference like ``fn = requests.get`` (still a violation
# of the rule in spirit, but not literally a call) is not misclassified
# -- the brief's recipe matches only call sites, and this test mirrors
# that contract.
FORBIDDEN_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*import\s+requests\b"),
    re.compile(r"^\s*from\s+requests\b"),
    re.compile(
        r"\brequests\.(?:get|post|put|patch|delete|head|options|request)\("
    ),
    re.compile(r"\brequests\.Session\("),
    re.compile(r"\brequests\.sessions\.Session\("),
)


# ---------------------------------------------------------------------------
# Triple-quoted-string detector
# ---------------------------------------------------------------------------
#
# Compiled once at import time. The character class ``[\s\S]`` matches
# every character including newlines, so a single regex can span the
# entire body of a multi-line docstring without depending on the
# ``re.DOTALL`` flag. The non-greedy ``*?`` quantifier ensures the
# regex stops at the first closing triple-quote rather than spanning
# multiple docstrings. Both ``"""`` and ``'''`` delimiters are handled
# through an alternation (``|``).
#
# String concatenation is used (rather than a single raw string) so
# that the pattern for single-quote triple-delimiters can be expressed
# with a ``r"'''...'''"`` raw string that uses ``"`` as the Python
# string delimiter -- this avoids backslash gymnastics around the
# internal apostrophes.
_TRIPLE_QUOTE_PATTERN: re.Pattern[str] = re.compile(
    r'"""[\s\S]*?"""' + r"|'''[\s\S]*?'''",
)


# ---------------------------------------------------------------------------
# Scan-target specification
# ---------------------------------------------------------------------------
#
# Subdirectories whose every ``.py`` file is scanned recursively. The
# tuple intentionally OMITS ``api`` because the ``api/`` package
# contains the one allowed file (``api/nba_client.py``); ``api/
# __init__.py`` is added back in by :func:`_scan_targets` so the
# package marker is still verified to be free of direct ``requests``
# usage. Only ``api/nba_client.py`` is exempted, not the whole
# ``api/`` package.
#
# ``storage`` is included because the ``CSVWriter`` has no business
# making HTTP requests -- its sole responsibility is filesystem
# persistence (Rule 7). ``utils`` is included for the same reason:
# the rate limiter, schema normalizer, checkpoint manager, logger,
# metrics registry, health probes, and correlation-ID helper are all
# pure-Python utilities without any outbound network role.
SCAN_DIRS: Tuple[str, ...] = ("endpoints", "pipelines", "storage", "utils")

# Top-level Python files at the repository root. ``run.py`` is the CLI
# entry point and ``config.py`` is the tunable-constants module;
# neither may directly call ``requests``. The tuple is declared at
# module scope so it can be referenced both by :func:`_scan_targets`
# and by future extensions (for example adding ``metrics.py`` at the
# root, should the architecture ever relocate the metrics helper).
SCAN_ROOT_FILES: Tuple[str, ...] = ("run.py", "config.py")

# The single allowed file. Expressed as a tuple of path components
# (``Path.parts[-2:]``) rather than a single string so the identity
# check is OS-portable (``/`` on POSIX, ``\`` on Windows).
ALLOWED_FILE: Tuple[str, str] = ("api", "nba_client.py")


# ---------------------------------------------------------------------------
# Production-file iterator
# ---------------------------------------------------------------------------


def _scan_targets(project_root: Path) -> Iterator[Path]:
    """Yield every production ``.py`` file that must NOT use ``requests`` directly.

    The iterator is the single source of truth for "what is scanned".
    It yields, in order:

    1. Every top-level script named in :data:`SCAN_ROOT_FILES` that
       exists on disk.
    2. Every ``.py`` file under each directory named in
       :data:`SCAN_DIRS`, sorted lexicographically for deterministic
       test output.
    3. ``api/__init__.py`` if present -- the api-package marker is in
       scope because ONLY ``api/nba_client.py`` is the Rule 1
       exception, NOT the entire ``api/`` package. The package
       ``__init__`` file must therefore remain free of direct
       ``requests`` usage.

    Missing directories and files are skipped silently so the test
    degrades gracefully against partially-built repositories -- for
    example, when ``pipelines/`` or ``run.py`` have not yet been
    created by a preceding agent during incremental implementation.

    Parameters
    ----------
    project_root:
        Absolute :class:`~pathlib.Path` of the repository root,
        supplied by the session-scoped ``project_root`` fixture
        declared in ``tests/conftest.py``.

    Yields
    ------
    pathlib.Path
        An existing production ``.py`` file that must be free of
        direct ``requests`` usage.
    """
    # ---- Top-level scripts at the project root -------------------------
    #
    # ``run.py`` and ``config.py`` live at the repo root. We probe
    # each candidate with ``exists()`` before yielding because the
    # build proceeds bottom-up and the root-level CLI is assembled
    # only after every dependency is in place.
    for root_file in SCAN_ROOT_FILES:
        candidate = project_root / root_file
        if candidate.exists():
            yield candidate

    # ---- Recursive scan of every production subpackage -----------------
    #
    # ``Path.rglob("*.py")`` visits every descendant Python file in
    # the given directory. The results are sorted so the failure
    # message produced by :func:`test_only_nba_client_imports_requests`
    # lists offending files in a stable, predictable order (makes
    # debugging easier and avoids flaky diff output on re-runs).
    for subdir in SCAN_DIRS:
        folder = project_root / subdir
        if not folder.exists():
            # Pre-implementation tolerance: an absent directory
            # simply means no files to scan under it yet.
            continue
        for path in sorted(folder.rglob("*.py")):
            yield path

    # ---- api/__init__.py is in scope; api/nba_client.py is NOT ---------
    #
    # We intentionally yield ``api/__init__.py`` so the package marker
    # is verified to be free of ``requests`` usage, but we never yield
    # ``api/nba_client.py`` because that is the one allowed transport
    # module (the entire purpose of Rule 1's carve-out).
    api_init = project_root / "api" / "__init__.py"
    if api_init.exists():
        yield api_init


# ---------------------------------------------------------------------------
# Triple-quoted-string line calculator
# ---------------------------------------------------------------------------


def _compute_docstring_lines(text: str) -> set[int]:
    """Return 1-indexed line numbers that lie inside a triple-quoted string.

    Walks ``text`` with :data:`_TRIPLE_QUOTE_PATTERN` and records every
    line number spanned by each match. Callers use the returned set to
    filter out lines that appear inside module, class, or function
    docstrings -- and inside any other triple-quoted string literal --
    before applying :data:`FORBIDDEN_PATTERNS`.

    This filter exists specifically so documentation text that mentions
    a forbidden pattern in an informative way does not generate a
    false positive. The motivating example in the current codebase is
    ``config.py``'s Gate-12 read-site reference table that names
    ``requests.get(..., timeout=...)`` (wrapped in reST double-backtick
    inline literals) as the canonical consumer of
    :data:`config.REQUEST_TIMEOUT_SECONDS`. That citation is
    documentation; it must not register as a Rule 1 breach.

    Real, runtime-executed violations in module code are still caught
    because any ``import requests`` or ``requests.<method>(`` that
    actually runs at Python import-time or call-time lives outside
    triple-quoted string regions.

    Parameters
    ----------
    text:
        The full source text of a ``.py`` file, as returned by
        :meth:`pathlib.Path.read_text`.

    Returns
    -------
    set[int]
        The set of 1-indexed line numbers that lie within any
        triple-quoted string region. An empty file, a file without
        triple-quoted strings, or a file whose triple-quoted strings
        are all single-line (delimiters and content on the same line)
        still yields correct line coverage -- the expression
        ``text.count(chr(10), 0, pos) + 1`` is valid for zero or more
        newlines preceding ``pos``.

    Notes
    -----
    The implementation is intentionally regex-only because the file's
    import whitelist is ``re`` + ``pathlib`` + ``typing`` + ``pytest``
    -- ``ast`` and ``tokenize`` are not available. Well-formed Python
    source is the assumed input domain; pathological corner cases
    (triple-quotes embedded inside single-quoted string literals,
    mismatched delimiters, f-strings whose expression parts contain
    literal triple-quotes) are treated with best-effort tolerance
    rather than strict parsing. The worst-case failure mode is that
    a handful of extra lines are flagged as "in docstring" and
    therefore skipped -- which only WEAKENS the invariant for those
    lines, it does not introduce false positives elsewhere.
    """
    docstring_lines: set[int] = set()
    for match in _TRIPLE_QUOTE_PATTERN.finditer(text):
        # ``str.count("\n", 0, pos)`` returns the number of newlines
        # strictly before position ``pos``; adding 1 converts to a
        # 1-indexed line number. This matches the numbering scheme
        # used by :func:`enumerate` with ``start=1`` in the main
        # test's line loop, so set-membership checks are direct.
        start_pos = match.start()
        end_pos = match.end()
        start_line = text.count("\n", 0, start_pos) + 1
        end_line = text.count("\n", 0, end_pos) + 1
        for lineno in range(start_line, end_line + 1):
            docstring_lines.add(lineno)
    return docstring_lines


# ---------------------------------------------------------------------------
# Primary invariant test
# ---------------------------------------------------------------------------


def test_only_nba_client_imports_requests(project_root: Path) -> None:
    """Rule 1 -- every production ``.py`` file outside ``api/nba_client.py`` is requests-free.

    Walks the production tree via :func:`_scan_targets`, reads each
    ``.py`` file line-by-line, skips blank lines, comment-only lines,
    and lines inside triple-quoted strings, and fails with a
    diagnostic ``file:lineno: code`` list if any forbidden call site
    is found.

    Consumes the session-scoped ``project_root`` fixture declared in
    ``tests/conftest.py``.

    The test passes vacuously on a pre-implementation repository
    (production directories missing) because :func:`_scan_targets`
    yields an empty iterator -- there are no files to scan. Once
    production code exists, the invariant activates automatically.
    """
    violations: List[Tuple[Path, int, str]] = []

    for py_file in _scan_targets(project_root):
        # Defensive double-guard: if :func:`_scan_targets` ever
        # regresses and starts yielding the allowed file, this check
        # keeps the invariant honest. ``parts[-2:]`` is OS-portable
        # because :class:`~pathlib.PurePath` normalizes separators
        # internally (``/`` on POSIX, ``\`` on Windows).
        if py_file.parts[-2:] == ALLOWED_FILE:
            continue

        text = py_file.read_text(encoding="utf-8")
        docstring_lines = _compute_docstring_lines(text)

        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip blank lines, comment-only lines, and docstring
            # content. Skipping comments means a developer can write
            # ``# requests.get is forbidden here per Rule 1`` inside
            # a file without tripping the invariant. Skipping
            # docstring lines means reST documentation that mentions
            # forbidden call sites as inline-literal references is
            # not a false positive.
            if not stripped or stripped.startswith("#"):
                continue
            if lineno in docstring_lines:
                continue

            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append((py_file, lineno, line.rstrip()))
                    # One violation per line is enough for diagnosis;
                    # ``break`` prevents the same offending line from
                    # being logged twice under overlapping patterns
                    # (for example a ``requests.get(`` that happens
                    # to also satisfy a broader regex in the future).
                    break

    if violations:
        formatted = "\n".join(
            f"  {path.relative_to(project_root)}:{lineno}: {code}"
            for path, lineno, code in violations
        )
        pytest.fail(
            "Rule 1 violation -- `requests` accessed outside "
            "api/nba_client.py:\n" + formatted
        )


# ---------------------------------------------------------------------------
# Sanity test: the one allowed file IS permitted and does use requests
# ---------------------------------------------------------------------------


def test_allowed_file_does_use_requests(project_root: Path) -> None:
    """Sanity check -- ``api/nba_client.py`` actually uses the ``requests`` library.

    Without this check, a future refactor that accidentally routes
    HTTP calls through a different mechanism (for example
    :mod:`urllib.request` or the third-party ``httpx`` library) would
    make the main Rule 1 test *vacuously true*: every production file
    would be ``requests``-free simply because the system no longer
    uses ``requests`` at all.

    The sanity test keeps the Rule 1 invariant honest: the main test
    proves exclusion, this test proves presence. If both pass, the
    carve-out for ``api/nba_client.py`` is actually doing work.

    If ``api/nba_client.py`` does not yet exist (pre-implementation
    state before Group 3 has run, per AAP section 0.5.1.3), the test
    is skipped rather than failed -- this lets incremental builds
    proceed without a false red from a file that is on the
    build-order roadmap.
    """
    nba_client = project_root / "api" / "nba_client.py"
    if not nba_client.exists():
        pytest.skip(
            "api/nba_client.py does not exist yet -- sanity test "
            "will activate once the HTTP transport module is "
            "implemented."
        )

    text = nba_client.read_text(encoding="utf-8")
    docstring_lines = _compute_docstring_lines(text)

    # Mirror the main-test scan logic: skip blank lines, comment-only
    # lines, and docstring content. If the ONLY ``requests`` reference
    # in the file is inside a docstring (for example, an example
    # snippet in the module docstring), the sanity test treats that
    # as vacuous and fails -- the allowed file must use ``requests``
    # in real, runtime-executed code, not just in documentation.
    found = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if lineno in docstring_lines:
            continue
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                found = True
                break
        if found:
            break

    assert found, (
        "api/nba_client.py is the only file allowed to use "
        "`requests`, but its production code does not appear to. "
        "The Rule 1 invariant test is therefore vacuous. Either "
        "the HTTP transport regressed (and must be repaired) or "
        "the allowed-file carve-out must be re-pointed to the "
        "new transport module."
    )
