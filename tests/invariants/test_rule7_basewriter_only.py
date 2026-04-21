"""Rule 7 -- Pluggable Storage.

Asserts that only ``storage/csv_writer.py`` calls ``DataFrame.to_csv``.
All pipelines, endpoints, utilities, and the CLI MUST write CSVs via
the ``BaseWriter.write`` abstraction so the storage backend remains
swappable (e.g., a future Postgres or DuckDB writer).

The product brief's verbatim verification recipe limits the scan to
``pipelines/``, but Agent Action Plan (AAP) section 0.7.2.7 broadens
the scope to every production module. This test enforces the broader
rule because writing CSVs from any layer other than ``storage/``
defeats the abstraction.

The test uses pure-Python file walking and regex matching rather than
``subprocess.run(["grep", ...])`` so it is portable across operating
systems (Windows lacks ``grep``).

Why this file is safe from its own invariant
--------------------------------------------
This test file itself lives under ``tests/invariants/`` and is NEVER
scanned by :func:`_scan_targets` because ``tests/`` is deliberately
absent from :data:`SCAN_DIRS`. The raw regex source ``r"\\.to_csv\\("``
at module scope also contains a backslash before the open paren, so
even a naive grep over this file would not mistake the regex literal
itself for a forbidden call site.

Authoritative sources
---------------------
* Product brief section 5 Rule 7 -- binding rule text and original
  verification recipe.
* AAP section 0.2.3 -- registers this file as a required deliverable.
* AAP section 0.4.4 -- integration-invariants table (Rule 7 row).
* AAP section 0.5.1.8 -- Group 8 invariant tests.
* AAP section 0.7.2.7 -- Rule 7 binding constraint (expanded scope).
* AAP section 0.7.5 -- Rule-to-Gate verification matrix.
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
# The marker is NOT ``integration`` because invariants require zero network
# access and zero live filesystem writes -- they only read source files.
pytestmark = pytest.mark.invariant


# ---------------------------------------------------------------------------
# Forbidden pattern
# ---------------------------------------------------------------------------
#
# The single forbidden call-site shape: ``.to_csv(``. The leading escaped
# dot ensures the match is a method call on some object (DataFrame, Series,
# or any other pandas object that exposes ``to_csv``), not a bare identifier
# such as ``to_csv_result`` or a plain variable name. The trailing escaped
# open paren anchors on a call site rather than a mere reference -- so a
# docstring that mentions ``to_csv`` as a noun does not false-positive.
#
# Why one pattern instead of several: Rule 7 concerns a single method
# name. If pandas ever introduced an alias (e.g., ``to_flat_csv``) the
# rule text itself would need updating first, and this constant would
# grow to a tuple. For now, a single pattern keeps the invariant sharp.
TO_CSV_PATTERN: re.Pattern[str] = re.compile(r"\.to_csv\(")


# ---------------------------------------------------------------------------
# Scan-target specification
# ---------------------------------------------------------------------------
#
# Subdirectories whose every ``.py`` file is scanned recursively. The
# tuple intentionally OMITS ``storage/`` because the one allowed file
# lives there; ``storage/__init__.py`` is added back in by
# :func:`_scan_targets` so the package marker is still verified to be
# free of :func:`pandas.DataFrame.to_csv` calls.
#
# ``api/`` is included because the ``NBAClient`` has no business writing
# CSVs -- its sole responsibility is HTTP transport (Rule 1). If the
# client ever gained a cache-to-disk convenience method that bypassed
# ``BaseWriter``, this invariant would catch it.
SCAN_DIRS: Tuple[str, ...] = ("api", "endpoints", "pipelines", "utils")

# Top-level Python files at the repository root. ``run.py`` is the CLI
# entry point and ``config.py`` is the tunable-constants module; neither
# may call ``DataFrame.to_csv``. The tuple is declared at module scope
# so it can be referenced both by :func:`_scan_targets` and by future
# extensions (e.g., adding ``metrics.py`` at the root).
SCAN_ROOT_FILES: Tuple[str, ...] = ("run.py", "config.py")

# The single allowed file. Expressed as a tuple of path components
# (``Path.parts[-2:]``) rather than a single string so the identity
# check is OS-portable (``/`` on POSIX, ``\`` on Windows).
ALLOWED_FILE: Tuple[str, str] = ("storage", "csv_writer.py")


# ---------------------------------------------------------------------------
# Production-file iterator
# ---------------------------------------------------------------------------


def _scan_targets(project_root: Path) -> Iterator[Path]:
    """Yield every production ``.py`` file that must NOT call ``DataFrame.to_csv``.

    The iterator is the single source of truth for "what is scanned".
    It yields, in order:

    1. Every top-level script named in :data:`SCAN_ROOT_FILES` that
       exists on disk.
    2. Every ``.py`` file under each directory named in
       :data:`SCAN_DIRS`, sorted for deterministic test output.
    3. ``storage/__init__.py`` if present -- the package marker is
       in scope because it is not the allowed writer file.

    Missing directories are skipped silently so the test degrades
    gracefully against partially-built repositories (e.g., when
    ``pipelines/`` has not yet been created by a preceding agent).

    Parameters
    ----------
    project_root:
        Absolute :class:`~pathlib.Path` of the repository root, supplied
        by the session-scoped ``project_root`` fixture in
        ``tests/conftest.py``.

    Yields
    ------
    pathlib.Path
        An existing production ``.py`` file that must be free of
        ``DataFrame.to_csv`` call sites.
    """
    # ---- Top-level scripts at the project root -------------------------
    for root_file in SCAN_ROOT_FILES:
        candidate = project_root / root_file
        if candidate.exists():
            yield candidate

    # ---- Recursive scan of every production subpackage except storage/ -
    for subdir in SCAN_DIRS:
        folder = project_root / subdir
        if not folder.exists():
            # Pre-implementation tolerance: directory absent is legal.
            continue
        for path in sorted(folder.rglob("*.py")):
            yield path

    # ---- Package marker for storage/ is in scope; the writer file is NOT
    #
    # We intentionally yield ``storage/__init__.py`` so the package
    # marker is verified to be free of ``to_csv`` calls, but we never
    # yield ``storage/csv_writer.py`` because that is the one allowed
    # writer module (the whole point of Rule 7's carve-out).
    storage_init = project_root / "storage" / "__init__.py"
    if storage_init.exists():
        yield storage_init


# ---------------------------------------------------------------------------
# Primary invariant test
# ---------------------------------------------------------------------------


def test_only_csv_writer_calls_to_csv(project_root: Path) -> None:
    """Rule 7 -- every production ``.py`` file outside ``storage/csv_writer.py`` is ``to_csv``-free.

    Walks the production tree via :func:`_scan_targets`, reads each
    ``.py`` file line-by-line, skips blank lines and comment-only
    lines (so documentation that mentions the forbidden pattern inside
    a ``# comment`` does not trip the invariant), and fails with a
    diagnostic ``file:lineno: code`` list if any forbidden call site
    is found.

    Consumes the session-scoped ``project_root`` fixture declared in
    ``tests/conftest.py``.
    """
    violations: List[Tuple[Path, int, str]] = []

    for py_file in _scan_targets(project_root):
        # Defensive double-guard: even if :func:`_scan_targets` ever
        # regresses and starts yielding the allowed file, this check
        # inside the test keeps the invariant correct. ``parts[-2:]``
        # is OS-portable because :class:`~pathlib.PurePath` normalizes
        # separators.
        if py_file.parts[-2:] == ALLOWED_FILE:
            continue

        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip blank lines and comment-only lines. Docstrings are
            # NOT skipped -- writing ``.to_csv(`` inside a docstring
            # is still a documentation smell worth catching, because
            # it suggests the author was describing a call they should
            # not have been making.
            if not stripped or stripped.startswith("#"):
                continue
            if TO_CSV_PATTERN.search(line):
                violations.append((py_file, lineno, line.rstrip()))

    if violations:
        formatted = "\n".join(
            f"  {path.relative_to(project_root)}:{lineno}: {code}"
            for path, lineno, code in violations
        )
        pytest.fail(
            "Rule 7 violation -- `.to_csv(` called outside "
            "storage/csv_writer.py:\n" + formatted
        )


# ---------------------------------------------------------------------------
# Sanity test: the one allowed file IS permitted and does use to_csv
# ---------------------------------------------------------------------------


def test_allowed_file_does_call_to_csv(project_root: Path) -> None:
    """Sanity check -- ``storage/csv_writer.py`` actually calls ``DataFrame.to_csv``.

    Without this check, a future refactor that accidentally routes CSV
    emission through some other mechanism (e.g., ``csv.writer`` or a
    manual ``Path.write_text`` dump) would make the main Rule 7 test
    *vacuously true*: every production file would be ``to_csv``-free
    simply because the system no longer writes CSVs at all.

    The sanity test keeps the Rule 7 invariant honest: the main test
    proves exclusion, this test proves presence.

    If ``storage/csv_writer.py`` does not yet exist (pre-implementation
    state before its creating agent has run), the test is skipped
    rather than failed -- this lets incremental builds proceed without
    a false red from a file that is on the build-order roadmap.
    """
    csv_writer = project_root / "storage" / "csv_writer.py"
    if not csv_writer.exists():
        pytest.skip(
            "storage/csv_writer.py does not exist yet -- sanity test "
            "will activate once the writer module is implemented."
        )

    text = csv_writer.read_text(encoding="utf-8")

    # Mirror the main-test scan logic: skip blank lines and comment-only
    # lines so that the rule-describing ``# ---- SOLE CALL SITE...``
    # comment on line 316 of csv_writer.py (which does not contain
    # ``.to_csv(`` itself anyway) is treated consistently with the
    # main scan. Break on first match -- one call site is sufficient
    # to prove Rule 7 is not vacuous.
    found = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if TO_CSV_PATTERN.search(line):
            found = True
            break

    assert found, (
        "storage/csv_writer.py is the only file allowed to call "
        "`DataFrame.to_csv`, but it does not appear to. The Rule 7 "
        "invariant test is therefore vacuous. Either the CSV writer "
        "regressed and must be repaired, or the allowed file must "
        "be re-pointed to the new writer module."
    )
