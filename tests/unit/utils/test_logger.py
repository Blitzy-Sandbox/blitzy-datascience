"""Unit tests for ``utils.logger``.

Verifies the stdlib-only logging configuration (Feature F-008 of the
Agent Action Plan) and correlation-ID propagation (the Observability
rule, AAP §0.7.3.1). The tests cover:

  * Return type of :func:`utils.logger.get_logger` is a
    :class:`logging.LoggerAdapter` — specifically a
    :class:`utils.correlation.CorrelationAdapter`.
  * Handler idempotency — repeated ``get_logger`` calls never
    duplicate the root logger's two handlers (stdout
    :class:`logging.StreamHandler` +
    :class:`logging.handlers.RotatingFileHandler`).
  * Correlation-ID propagation into :class:`logging.LogRecord`
    objects via the adapter, captured via pytest's ``caplog``
    fixture.
  * Fallback behavior of ``_CorrelationFormatter`` when a log
    record lacks the ``correlation_id`` attribute entirely
    (as happens for records originating from third-party
    libraries). caplog is NOT used here — the formatter is
    instantiated and invoked directly because caplog captures
    records BEFORE the production formatter executes.
  * :class:`~logging.handlers.RotatingFileHandler` size-based
    rotation at :data:`config.LOG_FILE_MAX_BYTES`.
  * :func:`utils.logger._reset_for_tests` clears handlers and
    resets the ``_configured`` flag so a subsequent
    ``get_logger`` call reruns ``_configure``.
  * :func:`utils.logger._resolve_level` accepts integer constants,
    uppercase strings, lowercase strings, and falls back to
    :data:`logging.INFO` for unknown or ``None`` values.
  * Root logger level is read from :data:`config.LOG_LEVEL`.
  * Thread safety of ``_configure`` — 10 concurrent
    ``get_logger`` callers result in exactly two root handlers.
  * F-008 invariant — the module source does NOT import any
    third-party logging framework (``loguru``, ``structlog``,
    ``logbook``, ``coloredlogs``) and DOES import stdlib
    ``logging``.

All tests are network-free. Log files are isolated to
``tmp_path`` via the ``tmp_log_dir`` fixture defined in
``tests/conftest.py``; the real ``./logs`` directory is never
touched.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path
from typing import List

import pytest

import config
from utils import correlation, logger as logger_module


# ---------------------------------------------------------------------------
# Phase 2.1 — Local autouse reset fixture (defense-in-depth readability)
# ---------------------------------------------------------------------------
#
# ``tests/conftest.py`` already declares an autouse
# ``_reset_logger_handlers_between_tests`` fixture that invokes
# ``logger_module._reset_for_tests()`` before and after every test. The
# fixture below is intentionally redundant: it makes the reset explicit
# at this module's scope so a reader of these tests does not have to
# cross-reference ``conftest.py`` to understand why root-logger state is
# clean at the start of each test. The duplicate reset is cheap (two
# cleared-handler lists and a boolean flag flip) and defensive against
# future refactors of the shared conftest fixture.


@pytest.fixture(autouse=True)
def _reset_logger_module_state_before_test() -> None:
    """Reset logger module state before and after every test in this file."""
    logger_module._reset_for_tests()
    yield
    logger_module._reset_for_tests()


# ---------------------------------------------------------------------------
# Shared helper — filter pytest's ``LogCaptureHandler`` out of assertions.
# ---------------------------------------------------------------------------
#
# pytest's logging plugin automatically attaches two ``LogCaptureHandler``
# instances to the root logger during every test run (one for the captured
# "report section" output, one for live ``--log-cli`` output). These
# handlers are subclasses of :class:`logging.StreamHandler` and therefore
# indistinguishable from our stdout handler via ``isinstance`` alone. The
# helpers below return only the handlers whose *exact* type is
# :class:`logging.StreamHandler` or
# :class:`logging.handlers.RotatingFileHandler` — the two types our
# ``_configure`` function attaches — so the tests assert the expected
# count of "production" handlers independent of pytest's testing
# infrastructure.


def _production_handlers(log: logging.Logger) -> List[logging.Handler]:
    """Return handlers attached to ``log`` by ``utils.logger._configure``.

    Excludes pytest's :class:`_pytest.logging.LogCaptureHandler` (a
    subclass of :class:`logging.StreamHandler`) and any other non-stdlib
    handler types by requiring an exact type match against the two
    canonical classes.
    """
    return [
        h for h in log.handlers
        if type(h) is logging.StreamHandler
        or type(h) is logging.handlers.RotatingFileHandler
    ]


# ---------------------------------------------------------------------------
# Phase 2.2 — get_logger return type and basic wiring
# ---------------------------------------------------------------------------


def test_get_logger_returns_logger_adapter(tmp_log_dir: Path) -> None:
    """``get_logger`` must return a ``logging.LoggerAdapter`` instance.

    The adapter contract is the stdlib integration point that lets our
    custom ``CorrelationAdapter.process`` inject the correlation ID
    into every log record without requiring callers to pass ``extra``
    dicts manually (AAP §0.4.1.1).
    """
    adapter = logger_module.get_logger("test.module.name")
    assert isinstance(adapter, logging.LoggerAdapter)


def test_get_logger_returns_correlation_adapter_subclass(tmp_log_dir: Path) -> None:
    """The returned adapter must be a ``CorrelationAdapter`` — the
    subclass that implements correlation-ID injection via
    :meth:`utils.correlation.CorrelationAdapter.process`."""
    adapter = logger_module.get_logger("test.module.name")
    assert isinstance(adapter, correlation.CorrelationAdapter)


def test_get_logger_adapter_has_correct_underlying_logger_name(tmp_log_dir: Path) -> None:
    """The adapter must wrap ``logging.getLogger(name)`` where ``name``
    is the value passed to ``get_logger``. This is how per-module log
    levels set in tests via ``logging.getLogger("...").setLevel(...)``
    take effect at runtime."""
    adapter = logger_module.get_logger("nba.pipeline.games")
    assert adapter.logger.name == "nba.pipeline.games"


# ---------------------------------------------------------------------------
# Phase 2.3 — Handler idempotency (F-008 + Observability)
# ---------------------------------------------------------------------------


def test_single_get_logger_adds_exactly_two_handlers_to_root(tmp_log_dir: Path) -> None:
    """A single ``get_logger`` call must attach exactly two handlers to
    the root logger: one stdout :class:`logging.StreamHandler` and one
    :class:`logging.handlers.RotatingFileHandler`.

    Note: pytest's logging plugin attaches its own
    ``LogCaptureHandler`` instances (subclasses of
    :class:`logging.StreamHandler`) to the root logger; the
    :func:`_production_handlers` helper filters them out so the count
    reflects only the handlers that ``_configure`` installs.
    """
    logger_module.get_logger("x")
    root = logging.getLogger()
    production = _production_handlers(root)
    assert len(production) == 2, (
        f"Expected exactly 2 production handlers after one get_logger call; "
        f"got {[type(h).__name__ for h in production]} "
        f"(full root handler list: {[type(h).__name__ for h in root.handlers]})"
    )


def test_multiple_get_logger_calls_do_not_duplicate_handlers(tmp_log_dir: Path) -> None:
    """Five consecutive ``get_logger`` calls must leave exactly two
    production handlers attached. Duplicate handlers would multiply
    every log line written to disk and stdout — the single most
    impactful regression this test file guards against.

    Pytest's own ``LogCaptureHandler`` instances are excluded from the
    count via :func:`_production_handlers`.
    """
    for i in range(5):
        logger_module.get_logger(f"module_{i}")
    root = logging.getLogger()
    production = _production_handlers(root)
    assert len(production) == 2, (
        f"Expected exactly 2 production handlers after 5 get_logger calls; "
        f"got {[type(h).__name__ for h in production]} "
        f"(full root handler list: {[type(h).__name__ for h in root.handlers]})"
    )


def test_root_handlers_include_stream_and_rotating_file(tmp_log_dir: Path) -> None:
    """The two attached handlers must be exactly one stdout
    :class:`logging.StreamHandler` and one
    :class:`logging.handlers.RotatingFileHandler`.

    The inheritance chain is
    ``RotatingFileHandler → FileHandler → StreamHandler``, so a plain
    ``isinstance(h, logging.StreamHandler)`` check matches both — and
    pytest's ``LogCaptureHandler`` is also a :class:`StreamHandler`
    subclass. We therefore use exact-type comparisons
    (``type(h) is logging.StreamHandler``) to isolate the canonical
    stdout handler our ``_configure`` function installs.
    """
    logger_module.get_logger("x")
    root = logging.getLogger()
    stream_handlers = [
        h for h in root.handlers
        if type(h) is logging.StreamHandler
    ]
    rotating_handlers = [
        h for h in root.handlers
        if type(h) is logging.handlers.RotatingFileHandler
    ]
    assert len(stream_handlers) == 1, (
        f"Expected exactly one stdout StreamHandler with exact type "
        f"logging.StreamHandler; got stream_handlers="
        f"{[type(h).__name__ for h in stream_handlers]} "
        f"(full root handler list: {[type(h).__name__ for h in root.handlers]})"
    )
    assert len(rotating_handlers) == 1, (
        f"Expected exactly one RotatingFileHandler; "
        f"got {[type(h).__name__ for h in rotating_handlers]} "
        f"(full root handler list: {[type(h).__name__ for h in root.handlers]})"
    )


def test_rotating_file_handler_is_configured_from_config(tmp_log_dir: Path) -> None:
    """The :class:`RotatingFileHandler` must be constructed from
    ``config.LOG_FILE``, ``config.LOG_FILE_MAX_BYTES``,
    ``config.LOG_FILE_BACKUP_COUNT``, and ``encoding='utf-8'``.

    The ``tmp_log_dir`` fixture has monkeypatched
    ``config.LOG_FILE`` to a ``tmp_path``-rooted location — so the
    handler's ``baseFilename`` (which the stdlib resolves to an
    absolute path at construction time) must equal the resolved form
    of the current ``config.LOG_FILE`` value.
    """
    logger_module.get_logger("x")
    root = logging.getLogger()
    rfh = next(
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert Path(rfh.baseFilename) == Path(config.LOG_FILE).resolve()
    assert rfh.maxBytes == config.LOG_FILE_MAX_BYTES
    assert rfh.backupCount == config.LOG_FILE_BACKUP_COUNT
    assert rfh.encoding == "utf-8"


# ---------------------------------------------------------------------------
# Phase 2.4 — Correlation-ID injection via caplog
# ---------------------------------------------------------------------------


def test_log_record_includes_current_correlation_id_from_context(
    tmp_log_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ``correlation_id`` ContextVar has been set, the
    ``CorrelationAdapter`` must inject that value into every log
    record's ``extra`` dict — making it visible as
    ``record.correlation_id`` to caplog and to the
    ``%(correlation_id)s`` format specifier.
    """
    correlation.correlation_id.set("abc123")
    adapter = logger_module.get_logger("test.corr")
    with caplog.at_level(logging.INFO, logger="test.corr"):
        adapter.info("hello")
    records = [r for r in caplog.records if r.name == "test.corr"]
    assert records, "No log record captured for logger test.corr"
    assert records[-1].correlation_id == "abc123"
    assert records[-1].getMessage() == "hello"


def test_log_record_with_unset_correlation_id_has_empty_or_dash_value(
    tmp_log_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ``correlation_id`` ContextVar has NOT been set (its
    default is the empty string per ``utils/correlation.py``), the
    ``CorrelationAdapter`` still populates ``extra["correlation_id"]``
    with ``""`` so the format string ``%(correlation_id)s`` never
    raises ``KeyError``.

    Note: the ``"-"`` visual-fallback is performed ONLY inside
    :meth:`_CorrelationFormatter.format` when a record LACKS the
    attribute entirely. Since ``CorrelationAdapter.process`` always
    sets the attribute (possibly to ``""``), the value observed on
    caplog's captured record is ``""``, not ``"-"``.
    """
    adapter = logger_module.get_logger("test.corr.empty")
    with caplog.at_level(logging.INFO, logger="test.corr.empty"):
        adapter.info("world")
    records = [r for r in caplog.records if r.name == "test.corr.empty"]
    assert records, "No log record captured for logger test.corr.empty"
    assert records[-1].correlation_id == ""


def test_caller_supplied_correlation_id_overrides_context(
    tmp_log_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller that explicitly passes ``extra={"correlation_id": ...}``
    to the logging call must have their value preserved — the
    ContextVar value is used only when the caller did NOT supply one.

    This is the explicit-over-implicit contract asserted by
    ``CorrelationAdapter.process`` (see ``utils/correlation.py``
    Step 2).
    """
    correlation.correlation_id.set("from-context")
    adapter = logger_module.get_logger("test.corr.override")
    with caplog.at_level(logging.INFO, logger="test.corr.override"):
        adapter.info("override", extra={"correlation_id": "from-caller"})
    records = [r for r in caplog.records if r.name == "test.corr.override"]
    assert records, "No log record captured for logger test.corr.override"
    assert records[-1].correlation_id == "from-caller"


# ---------------------------------------------------------------------------
# Phase 2.5 — Direct ``_CorrelationFormatter`` exercise (NOT caplog)
# ---------------------------------------------------------------------------
#
# pytest's ``caplog`` fixture captures :class:`logging.LogRecord`
# objects BEFORE the production handlers' formatters run, so assertions
# about ``_CorrelationFormatter``'s dash fallback cannot be made via
# caplog. The tests below instantiate ``_CorrelationFormatter``
# directly and invoke :meth:`format` on synthetic records that lack
# the ``correlation_id`` attribute, verifying the formatter's
# defense-in-depth path for records originating in third-party
# libraries (urllib3, requests, etc.) whose loggers propagate through
# our root handlers without going through a ``CorrelationAdapter``.


def test_correlation_formatter_injects_dash_when_record_lacks_attribute(
    tmp_log_dir: Path,
) -> None:
    """A synthetic record WITHOUT a ``correlation_id`` attribute must
    be formatted with ``"-"`` in place of the correlation ID when
    the ContextVar is unset (empty string default)."""
    formatter = logger_module._CorrelationFormatter(
        fmt="corr=%(correlation_id)s msg=%(message)s"
    )
    record = logging.LogRecord(
        name="raw.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="no adapter was used",
        args=(),
        exc_info=None,
    )
    # Sanity check: the synthetic record was constructed WITHOUT
    # a correlation_id attribute so the formatter's fallback branch
    # is exercised.
    assert not hasattr(record, "correlation_id")
    formatted = formatter.format(record)
    assert "corr=-" in formatted
    assert "msg=no adapter was used" in formatted


def test_correlation_formatter_respects_context_var_when_record_lacks_attribute(
    tmp_log_dir: Path,
) -> None:
    """When the record lacks the attribute but the ContextVar is set,
    the formatter must read the ContextVar rather than substitute the
    literal ``"-"``."""
    correlation.correlation_id.set("ctxvar-cid")
    formatter = logger_module._CorrelationFormatter(
        fmt="corr=%(correlation_id)s msg=%(message)s"
    )
    record = logging.LogRecord(
        name="raw.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="context var should be picked up",
        args=(),
        exc_info=None,
    )
    assert not hasattr(record, "correlation_id")
    formatted = formatter.format(record)
    assert "corr=ctxvar-cid" in formatted


def test_correlation_formatter_preserves_explicit_attribute_on_record(
    tmp_log_dir: Path,
) -> None:
    """If a record already carries a ``correlation_id`` attribute
    (e.g. placed there by :class:`CorrelationAdapter.process`), the
    formatter must preserve it verbatim — its defensive branch is
    taken ONLY when ``hasattr(record, 'correlation_id')`` is False."""
    formatter = logger_module._CorrelationFormatter(
        fmt="corr=%(correlation_id)s msg=%(message)s"
    )
    record = logging.LogRecord(
        name="raw.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="explicit cid",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "explicit-id"
    formatted = formatter.format(record)
    assert "corr=explicit-id" in formatted


# ---------------------------------------------------------------------------
# Phase 2.6 — File handler writes and rotates
# ---------------------------------------------------------------------------


def test_log_file_written_to_configured_path(tmp_log_dir: Path) -> None:
    """Emitting a log record through the adapter must cause the
    :class:`RotatingFileHandler` to open and write the configured
    ``config.LOG_FILE``. The ``tmp_log_dir`` fixture redirects that
    path into ``tmp_path`` so the real ``./logs/pipeline.log`` is not
    touched during the test run."""
    adapter = logger_module.get_logger("test.filewrite")
    adapter.warning("first message")
    # Flush every handler so the file-backed bytes land on disk before
    # the subsequent read.
    for h in logging.getLogger().handlers:
        h.flush()
    log_path = Path(config.LOG_FILE)
    assert log_path.exists(), f"Log file not created at {log_path}"
    content = log_path.read_text(encoding="utf-8")
    assert "first message" in content


def test_rotating_file_handler_rotates_at_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_log_dir: Path,
) -> None:
    """The :class:`RotatingFileHandler` must rotate when the current
    log file exceeds ``config.LOG_FILE_MAX_BYTES``. To trigger rollover
    quickly we monkeypatch the size to a tiny value and emit many
    records. After rollover the file ``pipeline.log.1`` (the first
    rotation backup) must exist alongside the primary ``pipeline.log``.

    Note: we must call ``_reset_for_tests()`` AFTER the monkeypatch so
    the next ``get_logger`` call runs ``_configure`` with the patched
    size — otherwise the pre-existing handler would retain the
    10 MB default and no rotation would occur.
    """
    monkeypatch.setattr(config, "LOG_FILE_MAX_BYTES", 200, raising=True)
    monkeypatch.setattr(config, "LOG_FILE_BACKUP_COUNT", 3, raising=True)
    logger_module._reset_for_tests()
    adapter = logger_module.get_logger("test.rotate")

    # Sanity check: the RotatingFileHandler MUST have picked up the
    # patched maxBytes. If this fails the test is meaningless, so fail
    # it early with a clear diagnostic rather than letting the rotation
    # assertion below report a misleading symptom.
    root = logging.getLogger()
    rfh = next(
        h for h in root.handlers
        if type(h) is logging.handlers.RotatingFileHandler
    )
    assert rfh.maxBytes == 200, (
        f"RotatingFileHandler did not pick up patched maxBytes; "
        f"got {rfh.maxBytes} (expected 200)"
    )

    # Write enough data to exceed 200 bytes many times over. Use a
    # single ``%d`` placeholder with a single argument — the prior
    # approach ``"...%d..." * 2`` doubled the format string to two
    # placeholders but still passed only one argument, causing stdlib
    # logging to emit a format error instead of writing the record.
    # With the ~55-byte ``LOG_FORMAT`` prefix and the ~90-byte message
    # below, each record is ~140 bytes — rotation will fire after
    # roughly every second record at maxBytes=200.
    message = "rotation-probe line %d with lots of extra filler text to make rotation trigger"
    for i in range(50):
        adapter.warning(message, i)
    for h in root.handlers:
        h.flush()

    log_path = Path(config.LOG_FILE)
    assert log_path.exists(), f"Primary log file missing at {log_path}"
    backup = log_path.with_name(log_path.name + ".1")
    assert backup.exists(), (
        f"Expected rotation backup at {backup}; directory contents: "
        f"{sorted(p.name for p in log_path.parent.iterdir())}"
    )


# ---------------------------------------------------------------------------
# Phase 2.7 — ``_resolve_level`` behavior
# ---------------------------------------------------------------------------


def test_resolve_level_accepts_int_constants() -> None:
    """Integer level constants must round-trip unchanged through
    ``_resolve_level``."""
    assert logger_module._resolve_level(logging.DEBUG) == logging.DEBUG
    assert logger_module._resolve_level(logging.INFO) == logging.INFO
    assert logger_module._resolve_level(logging.WARNING) == logging.WARNING
    assert logger_module._resolve_level(logging.ERROR) == logging.ERROR
    assert logger_module._resolve_level(logging.CRITICAL) == logging.CRITICAL


def test_resolve_level_accepts_uppercase_string() -> None:
    """Uppercase level strings must resolve to their numeric form."""
    assert logger_module._resolve_level("INFO") == logging.INFO
    assert logger_module._resolve_level("WARNING") == logging.WARNING
    assert logger_module._resolve_level("DEBUG") == logging.DEBUG


def test_resolve_level_accepts_lowercase_string() -> None:
    """Lowercase level strings must also resolve correctly; the helper
    must uppercase before consulting :func:`logging.getLevelName`."""
    assert logger_module._resolve_level("info") == logging.INFO
    assert logger_module._resolve_level("debug") == logging.DEBUG
    assert logger_module._resolve_level("critical") == logging.CRITICAL


def test_resolve_level_falls_back_to_info_on_unknown_string() -> None:
    """Misspelled or non-existent level strings must fall back to
    :data:`logging.INFO` so a typo in ``NBA_LOG_LEVEL`` does not
    silence the pipeline (see ``utils/logger.py`` docstring)."""
    assert logger_module._resolve_level("not-a-real-level") == logging.INFO


def test_resolve_level_falls_back_to_info_on_none() -> None:
    """``None`` (neither int nor str) must fall back to
    :data:`logging.INFO` rather than raising ``TypeError``."""
    assert logger_module._resolve_level(None) == logging.INFO


# ---------------------------------------------------------------------------
# Phase 2.8 — Level read from ``config.LOG_LEVEL``
# ---------------------------------------------------------------------------


def test_root_logger_level_matches_config(tmp_log_dir: Path) -> None:
    """After ``_configure``, the root logger level must match
    ``_resolve_level(config.LOG_LEVEL)``."""
    logger_module.get_logger("x")
    expected = logger_module._resolve_level(config.LOG_LEVEL)
    assert logging.getLogger().level == expected


def test_monkeypatched_log_level_debug_takes_effect_after_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_log_dir: Path,
) -> None:
    """When ``config.LOG_LEVEL`` is monkeypatched to ``"DEBUG"`` and
    the logger state is reset, the next ``get_logger`` call must
    configure the root logger at :data:`logging.DEBUG`.

    The reset is required because ``_configure`` short-circuits via
    the module-level ``_configured`` flag — without the reset the
    level from the initial autouse fixture setup would persist.
    """
    monkeypatch.setattr(config, "LOG_LEVEL", "DEBUG", raising=True)
    logger_module._reset_for_tests()
    logger_module.get_logger("x")
    assert logging.getLogger().level == logging.DEBUG


# ---------------------------------------------------------------------------
# Phase 2.9 — ``_reset_for_tests`` semantics
# ---------------------------------------------------------------------------


def test_reset_for_tests_clears_handlers_and_allows_reconfiguration(
    tmp_log_dir: Path,
) -> None:
    """``_reset_for_tests`` must detach every handler from the root
    logger (including pytest's ``LogCaptureHandler`` instances, because
    ``_reset_for_tests`` clears the ``handlers`` list unconditionally),
    leaving the subsequent ``get_logger`` call free to reconfigure a
    clean pair of handlers.

    After the initial ``get_logger`` we count production handlers only
    to avoid sensitivity to pytest's logging plugin. After the reset
    the handler list must be entirely empty. After the second
    ``get_logger`` the production handler count must once again be 2.
    """
    logger_module.get_logger("a")
    root = logging.getLogger()
    assert len(_production_handlers(root)) == 2, (
        f"Expected 2 production handlers after first get_logger call; "
        f"got {[type(h).__name__ for h in _production_handlers(root)]}"
    )
    logger_module._reset_for_tests()
    assert root.handlers == [], (
        f"Expected root.handlers == [] after _reset_for_tests; "
        f"got {[type(h).__name__ for h in root.handlers]}"
    )
    # After reset, a new get_logger call must re-add exactly 2
    # production handlers. Pytest does NOT re-inject its
    # LogCaptureHandler after we clear the list, so the raw list
    # length of 2 is the expected post-condition here; we assert with
    # the helper for defense-in-depth.
    logger_module.get_logger("b")
    assert len(_production_handlers(root)) == 2, (
        f"Expected 2 production handlers after second get_logger call; "
        f"got {[type(h).__name__ for h in _production_handlers(root)]}"
    )


def test_reset_for_tests_sets_configured_flag_false(tmp_log_dir: Path) -> None:
    """``_reset_for_tests`` must flip the module-level ``_configured``
    flag back to ``False`` so the next ``_configure`` call re-runs
    rather than short-circuiting."""
    logger_module.get_logger("x")
    assert logger_module._configured is True
    logger_module._reset_for_tests()
    assert logger_module._configured is False


# ---------------------------------------------------------------------------
# Phase 2.10 — Thread safety of ``_configure``
# ---------------------------------------------------------------------------


def test_configure_is_thread_safe_no_duplicate_handlers_under_contention(
    tmp_log_dir: Path,
) -> None:
    """Ten concurrent ``get_logger`` callers must not race past the
    ``_configured`` guard and attach duplicate handlers. After all
    threads join, the root logger must have exactly 2 handlers — the
    same invariant as the single-threaded case.

    Without the ``_configure_lock`` guard, two threads could both
    observe ``_configured == False`` and each add the two-handler pair
    before either set the flag, producing 4 handlers and duplicate log
    lines. This test will reliably detect that regression.
    """
    logger_module._reset_for_tests()

    errors: List[BaseException] = []

    def _worker() -> None:
        try:
            logger_module.get_logger(f"concurrent.{threading.get_ident()}")
        except BaseException as exc:  # noqa: BLE001 - surface thread errors
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert all(not t.is_alive() for t in threads), "One or more worker threads hung"
    assert errors == [], f"Worker threads raised: {errors!r}"
    root = logging.getLogger()
    production = _production_handlers(root)
    assert len(production) == 2, (
        f"Expected 2 production handlers post-race; "
        f"got {[type(h).__name__ for h in production]} "
        f"(full root handler list: {[type(h).__name__ for h in root.handlers]})"
    )


# ---------------------------------------------------------------------------
# Phase 2.11 — F-008: stdlib-only logging (no third-party library)
# ---------------------------------------------------------------------------


def test_logger_module_uses_stdlib_logging_only() -> None:
    """F-008 invariant — ``utils/logger.py`` must use Python's standard
    ``logging`` module exclusively. Third-party logging frameworks
    (``loguru``, ``structlog``, ``logbook``, ``coloredlogs``) are
    explicitly prohibited by the Technical Specification §3.2.1.

    We parse the module's source with :mod:`ast` and inspect the
    top-level module names referenced by :class:`ast.Import` and
    :class:`ast.ImportFrom` nodes. A substring search on the raw
    source text would false-positive on the module's docstring
    (which legitimately *names* the prohibited libraries as part of
    documenting the constraint), so AST-based scanning is required to
    distinguish documented intent from actual imports.

    This unit-level check complements the grep-based invariant in
    ``tests/invariants/`` by catching regressions at the same
    granularity as the rest of the logger test suite.
    """
    import ast
    import inspect

    source = inspect.getsource(logger_module)
    tree = ast.parse(source)

    imported_top_level_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_top_level_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_top_level_modules.add(node.module.split(".")[0])

    for forbidden in ("loguru", "structlog", "logbook", "coloredlogs"):
        assert forbidden not in imported_top_level_modules, (
            f"utils/logger.py must use stdlib logging only; "
            f"found import of {forbidden!r}. "
            f"Imported modules: {sorted(imported_top_level_modules)}"
        )

    # Positive half of the F-008 contract: the stdlib logging module
    # must be imported directly.
    assert "logging" in imported_top_level_modules, (
        f"utils/logger.py must import Python's standard 'logging' module. "
        f"Imported modules: {sorted(imported_top_level_modules)}"
    )
