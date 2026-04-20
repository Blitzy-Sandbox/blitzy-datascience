"""Process-wide logging configuration for the NBA Data Ingestion Pipeline.

This module is the single logging entry point. Every other module calls
``get_logger(__name__)`` to obtain a :class:`logging.LoggerAdapter` that:

  * Writes to stdout via :class:`logging.StreamHandler`.
  * Writes to :data:`config.LOG_FILE` via
    :class:`logging.handlers.RotatingFileHandler` (size-based rotation
    with :data:`config.LOG_FILE_MAX_BYTES` and
    :data:`config.LOG_FILE_BACKUP_COUNT`).
  * Injects the current correlation ID (see :mod:`utils.correlation`)
    into every :class:`logging.LogRecord` so the
    ``%(correlation_id)s`` placeholder in :data:`config.LOG_FORMAT`
    resolves cleanly — including for log records originating in
    third-party libraries (``urllib3``, ``requests``) whose loggers
    propagate through our root handlers.

Handler attachment is **idempotent**: subsequent calls to
:func:`get_logger` do not duplicate handlers. The root logger is the
sole owner of handlers; child loggers (including third-party loggers)
inherit them via propagation. A :class:`threading.Lock` guards the
one-time setup so concurrent callers from
:mod:`concurrent.futures`-style executors cannot race and attach
duplicate handlers.

Feature F-008 of the Agent Action Plan mandates stdlib-only logging
(Technical Specification §3.2.1). Third-party logging frameworks such
as ``structlog``, ``loguru``, and ``rich.logging`` are explicitly
prohibited; this module uses only the Python standard library.

Typical usage (from any production module)::

    from utils.logger import get_logger

    logger = get_logger(__name__)
    logger.info("pipeline start")

The returned adapter is a
:class:`utils.correlation.CorrelationAdapter` whose underlying logger
is ``logging.getLogger(name)``. Callers that need direct access to the
base logger (e.g. to call ``setLevel`` in tests) can reach it via
``adapter.logger``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from typing import Any, Optional  # noqa: F401  (Optional reserved for future type-annotation flexibility)

import config
from utils.correlation import CorrelationAdapter, correlation_id


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
#
# ``_configured`` tracks whether ``_configure()`` has already attached our
# two handlers (stdout + rotating file) to the root logger. The accompanying
# ``_configure_lock`` guards the one-time setup path so that concurrent
# callers cannot attach duplicate handlers.
#
# The flag is preferred over ``logging.getLogger().hasHandlers()`` because
# an operator (or a misbehaving dependency) could attach their own handler
# via ``logging.basicConfig`` before this module is imported; the flag
# distinguishes **our** handlers from any that pre-existed.
_configured: bool = False
_configure_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Formatter — default-fills the ``correlation_id`` record attribute
# ---------------------------------------------------------------------------


class _CorrelationFormatter(logging.Formatter):
    """Formatter that defaults ``correlation_id`` to the context variable.

    The format string declared in :data:`config.LOG_FORMAT` contains the
    mandatory placeholder ``%(correlation_id)s``. When a
    :class:`logging.LogRecord` is produced by
    :class:`utils.correlation.CorrelationAdapter`, the record's
    ``correlation_id`` attribute is populated by
    :meth:`CorrelationAdapter.process` and the format resolves cleanly.

    However, log records can also arrive from third-party libraries
    (for example, ``urllib3.connectionpool`` during an HTTP request)
    whose loggers propagate up to our root logger's handlers without
    going through a :class:`CorrelationAdapter`. Such records lack the
    ``correlation_id`` attribute and would otherwise raise
    :exc:`KeyError` inside :meth:`logging.Formatter.format`.

    This formatter adds a defensive pre-step: if the record is missing
    the attribute, we read :data:`utils.correlation.correlation_id`
    directly from the :mod:`contextvars` surface and fall back to the
    single character ``"-"`` when no correlation ID has yet been minted
    (for example, during module import or an ad-hoc REPL session).

    Using a custom :class:`logging.Formatter` (rather than a
    :class:`logging.Filter`) means the fallback is bound to the
    formatter itself — a single instance is shared by both the
    :class:`~logging.StreamHandler` and the
    :class:`~logging.handlers.RotatingFileHandler`, avoiding any need
    to attach a filter per handler.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Guarantee the ``correlation_id`` placeholder resolves.

        Parameters
        ----------
        record:
            The :class:`logging.LogRecord` to be rendered.

        Returns
        -------
        str
            The rendered log line produced by the base
            :meth:`logging.Formatter.format`.
        """
        if not hasattr(record, "correlation_id"):
            # ``correlation_id`` is a ``ContextVar[str]`` whose default
            # is the empty string; we substitute a single dash so the
            # output line is never visually misaligned.
            record.correlation_id = correlation_id.get() or "-"
        return super().format(record)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_level(level: Any) -> int:
    """Translate a string or integer log level into the numeric form.

    :data:`config.LOG_LEVEL` is declared as a string (``"INFO"``,
    ``"DEBUG"``, ...) by default but may be overridden with an integer
    level constant (e.g. :data:`logging.INFO`). The Python logging
    module accepts only the integer form when calling
    :meth:`logging.Logger.setLevel`; this helper normalises both.

    Parameters
    ----------
    level:
        A log level expressed as either a string (case-insensitive,
        matching :func:`logging.getLevelName`) or an integer.

    Returns
    -------
    int
        The numeric log level. Falls back to :data:`logging.INFO` when
        the input cannot be resolved (for example, a misspelled string
        or a non-string, non-integer value). Falling back to a sensible
        default preserves Observability guarantees: the pipeline never
        silently runs without any log output because of a typo in
        ``NBA_LOG_LEVEL``.
    """
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        # ``logging.getLevelName`` is bidirectional: given the name it
        # returns the numeric level; given the number it returns the
        # name. A valid name resolves to an int; an unknown name
        # resolves to the string ``"Level <name>"``.
        resolved = logging.getLevelName(level.upper())
        if isinstance(resolved, int):
            return resolved
    return logging.INFO


# ---------------------------------------------------------------------------
# Private one-time configuration
# ---------------------------------------------------------------------------


def _configure() -> None:
    """Attach stdout + rotating file handlers to the root logger.

    The function is **idempotent**: it acquires :data:`_configure_lock`
    and short-circuits when the module-level :data:`_configured` flag
    is set, so concurrent callers from multiple threads cannot attach
    duplicate handlers.

    The configuration is applied to the **root** logger rather than to
    a named logger so that log records produced by any module in the
    process — including third-party libraries such as ``requests`` and
    ``urllib3`` — flow through our handlers and are tagged with the
    correlation ID.

    Side effects
    ------------
    * Creates :data:`config.OUTPUT_DIR` and :data:`config.LOG_DIR` via
      :func:`config.ensure_directories` so the
      :class:`logging.handlers.RotatingFileHandler` constructor does
      not raise :exc:`FileNotFoundError` on first write.
    * Sets the root logger level to the resolved
      :data:`config.LOG_LEVEL`.
    * Attaches a :class:`logging.StreamHandler` (stdout) and a
      :class:`logging.handlers.RotatingFileHandler`
      (:data:`config.LOG_FILE`) — both sharing a single
      :class:`_CorrelationFormatter` instance and the same log level.

    The rotating file handler is constructed with
    ``encoding="utf-8"`` to guarantee consistent output on Windows,
    whose default encoding (``cp1252``) cannot represent the full
    character repertoire that appears in NBA data payloads (e.g.
    accented player names).
    """
    global _configured
    with _configure_lock:
        if _configured:
            return

        # Ensure the log directory (and output directory) exist before
        # we attempt to open the rotating file handler; the handler
        # constructor would otherwise raise FileNotFoundError on the
        # first emission in a fresh clone of the repository.
        config.ensure_directories()

        level = _resolve_level(config.LOG_LEVEL)

        root = logging.getLogger()
        root.setLevel(level)

        # A single formatter instance is shared by both handlers so the
        # console and file outputs remain byte-for-byte consistent.
        formatter = _CorrelationFormatter(
            fmt=config.LOG_FORMAT,
            datefmt=config.LOG_DATE_FORMAT,
        )

        # 1) Standard-output handler — observable during interactive
        #    runs and captured by CI log collectors. Explicitly bind to
        #    ``sys.stdout`` so that operators tailing stdout see records
        #    and stdout/stderr log-capture policies classify them
        #    correctly (the default ``StreamHandler()`` stream is
        #    ``sys.stderr``, which contradicts the module-level contract
        #    documented above and in ``docs/OBSERVABILITY.md``).
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(level)
        root.addHandler(stream_handler)

        # 2) Rotating file handler — durable sink for post-run forensic
        #    log review (Observability rule, AAP §0.7.3.1).
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(config.LOG_FILE),
            maxBytes=config.LOG_FILE_MAX_BYTES,
            backupCount=config.LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

        _configured = True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_logger(name: str) -> logging.LoggerAdapter:
    """Return a :class:`logging.LoggerAdapter` wired to the shared handlers.

    The returned adapter is a
    :class:`utils.correlation.CorrelationAdapter` whose
    :meth:`~utils.correlation.CorrelationAdapter.process` method
    injects the current correlation ID into the ``extra`` dict of
    every log record. Callers therefore never need to plumb the ID
    manually via ``logger.info("...", extra={...})``.

    The first invocation in a process triggers :func:`_configure`,
    which attaches our two handlers to the root logger exactly once.
    Subsequent calls return **fresh adapter instances** that share the
    same underlying :class:`logging.Logger` (and therefore the same
    root handlers) — duplicate handlers are never attached.

    Parameters
    ----------
    name:
        The dotted module path, typically ``__name__``. Used as the
        base logger name so that per-module filtering via
        ``logging.getLogger("pipelines.ingest_games").setLevel(...)``
        remains possible in tests.

    Returns
    -------
    logging.LoggerAdapter
        A :class:`CorrelationAdapter` instance wrapping
        ``logging.getLogger(name)``.

    Examples
    --------
    >>> from utils.logger import get_logger
    >>> log = get_logger(__name__)
    >>> log.info("hello world")  # doctest: +SKIP
    """
    _configure()
    base = logging.getLogger(name)
    # ``extra={}`` is a no-op for :class:`CorrelationAdapter`, which
    # derives the correlation ID from the context variable rather than
    # from ``self.extra``. Passing an empty dict keeps the LoggerAdapter
    # contract explicit without introducing adapter-level defaults.
    return CorrelationAdapter(base, {})


# ---------------------------------------------------------------------------
# Test-only helper
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Remove all handlers from the root logger and reset configuration state.

    Intended **only** for use by ``tests/conftest.py`` to reconfigure
    logging against a ``tmp_path``-scoped :data:`config.LOG_FILE` — for
    example, after a fixture mutates ``config.LOG_FILE`` to point at a
    fresh temporary directory, the test must call this helper so the
    next :func:`get_logger` call re-runs :func:`_configure` with the
    new configuration.

    Production code must not call this function. The handler
    ``close()`` step is best-effort: we swallow any exception from
    ``close()`` because a stale or already-closed handler should not
    cause a test fixture to fail during teardown.

    Side effects
    ------------
    * Detaches and closes every handler currently attached to the root
      logger (including any handlers a test may have attached
      directly).
    * Resets :data:`_configured` to ``False`` so the next
      :func:`get_logger` call re-runs :func:`_configure`.
    """
    global _configured
    with _configure_lock:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - best-effort close in tests
                # A handler whose underlying file has already been
                # removed (e.g. the tmp_path was torn down before the
                # fixture reset) can raise on close(); that failure
                # must not propagate because the whole point of this
                # helper is to restore a clean state.
                pass
        _configured = False
