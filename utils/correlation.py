"""Correlation-ID primitives for observability.

A *correlation ID* is a UUID4 hex string minted once per CLI invocation
(in ``run.py``) and propagated through every logging call, outbound
NBA Stats API request (as the ``X-Correlation-ID`` header), and metric
label for that invocation. It gives operators a single handle by which
to correlate events in logs, metrics, and upstream API server traces.

Propagation uses :class:`contextvars.ContextVar` so the ID is available
anywhere in the call stack without threading plumbing, and it is
preserved across :mod:`asyncio` task boundaries should the codebase
ever become asynchronous.

Public API
----------
``correlation_id`` (``ContextVar[str]``)
    The context variable holding the current correlation ID. The
    default value is the empty string (``""``), NOT ``None``, so that
    :class:`logging.Formatter` resolving ``%(correlation_id)s`` never
    raises :class:`KeyError` when no ID has been bound yet.

``new_correlation_id() -> str``
    Mint a fresh UUID4 hex string. This is the SOLE place in the
    codebase that calls :func:`uuid.uuid4` for correlation purposes
    (folder brief rule).

``set_correlation_id(value: str = "") -> str``
    Mint-and-bind convenience wrapper: sets the context variable to
    ``value`` (or a fresh UUID4 hex if ``value`` is empty) and returns
    the value actually bound. Used at the top of every ``run.py``
    subcommand.

``CorrelationAdapter``
    A :class:`logging.LoggerAdapter` subclass whose
    :meth:`CorrelationAdapter.process` method injects the current
    correlation ID into the record's ``extra`` dict before it is
    forwarded to the underlying logger.

Design notes
------------
* **Zero intra-project imports.** This module is a foundational
  (Group-2) building block. Modules such as ``utils/logger.py`` import
  it, so importing them back would create a circular dependency.
  Only the Python standard library is imported here.
* **Stdlib-only logging.** Feature F-008 mandates that no third-party
  logging library (structlog, loguru, rich, ...) be introduced. This
  module extends the stdlib :class:`logging.LoggerAdapter` pattern
  rather than adding a new dependency.
* **``ContextVar`` over ``threading.local``.** Per PEP 567,
  :class:`contextvars.ContextVar` correctly propagates across
  synchronous calls (like :mod:`threading` local state),
  :mod:`asyncio` task boundaries, and
  :class:`concurrent.futures.ThreadPoolExecutor` — none of which the
  thread-local equivalent handles without manual seeding. Using
  :class:`contextvars.ContextVar` now means the correlation plumbing
  survives any future refactor to asynchronous or parallel execution.
* **Hex form over canonical UUID string.** ``uuid.uuid4().hex`` is 32
  lowercase hexadecimal characters with no hyphens. This is safe to
  use in HTTP headers (no quoting required), log format strings, and
  filesystem paths without any escaping.
"""

import logging
import uuid
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple


# =============================================================================
# Module-level context variable
# =============================================================================
#
# ``correlation_id`` is the process-wide :class:`contextvars.ContextVar` that
# carries the per-invocation UUID4 hex string. Typical lifecycle:
#
#     1. ``run.py`` mints and binds at the top of every CLI subcommand:
#            set_correlation_id()          # mint + bind
#     2. Any module in the call stack reads via:
#            correlation_id.get()
#        (returns the empty string if no ID has been bound).
#     3. ``CorrelationAdapter.process`` propagates the value into every
#        :class:`logging.LogRecord` via the ``extra`` dict.
#     4. ``api/nba_client.py`` reads the same value and attaches it to
#        outbound NBA Stats API requests as ``X-Correlation-ID``.
#
# The default value is the *empty string* (``""``), NOT ``None``, for three
# reasons:
#
#   (a) :class:`logging.Formatter` resolves ``%(correlation_id)s`` via
#       string interpolation and needs a stringifiable value. ``None``
#       would render as the literal text ``"None"``, which is visually
#       noisy. The empty string renders as ``""``.
#   (b) The companion ``_CorrelationFormatter`` in ``utils/logger.py``
#       substitutes the empty string with a dash (``"-"``) placeholder
#       for human readability in the console sink. That substitution
#       depends on an empty-string sentinel.
#   (c) An ``Optional[str]`` default would require null-checks at every
#       read-site, which is far more error-prone than a harmless empty
#       string.
#
# The :class:`contextvars.ContextVar` instance exposes the standard PEP 567
# interface — ``set(value) -> Token``, ``get(default=...) -> str``, and
# ``reset(token) -> None`` — which is all any consumer of this module needs.

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


# =============================================================================
# Public functions
# =============================================================================


def new_correlation_id() -> str:
    """Return a fresh UUID4 correlation ID as a 32-character lowercase hex string.

    This function is the ONLY place in the codebase that mints UUIDs for
    correlation purposes (folder brief rule). Other modules READ the
    current correlation ID via ``correlation_id.get()`` but never mint.

    The hex form (no hyphens, exactly 32 lowercase hex digits in the set
    ``[0-9a-f]``) is chosen over ``str(uuid.uuid4())`` because:

    * It is safe for HTTP headers without any escaping.
    * It is safe for log format strings (no ``%`` or whitespace).
    * It is safe for filesystem paths.
    * It is a single atomic token in any regex-based log parser.

    Returns
    -------
    str
        A 32-character hexadecimal string, e.g.
        ``"3f4b8a2e1d7c4c8f9b1a2e3d4c5b6a7c"``.

    Notes
    -----
    This function does NOT call ``correlation_id.set()``. Minting and
    binding are kept separate so that tests can mint values (for example
    to compare against a captured :class:`logging.LogRecord`) without
    polluting the current execution context. Use
    :func:`set_correlation_id` if the mint-and-bind idiom is desired.

    Examples
    --------
    >>> cid = new_correlation_id()
    >>> len(cid)
    32
    >>> all(c in "0123456789abcdef" for c in cid)
    True
    """
    return uuid.uuid4().hex


def set_correlation_id(value: str = "") -> str:
    """Bind a correlation ID to the current context and return the value bound.

    This is the high-level "mint-and-bind" idiom used at the top of
    every ``run.py`` subcommand. It collapses the two-step pattern::

        cid = new_correlation_id()
        correlation_id.set(cid)

    into a single call::

        cid = set_correlation_id()

    Parameters
    ----------
    value : str, default ``""``
        The correlation ID to bind to the current context. If empty (or
        otherwise falsy, which for ``str`` means only the empty string),
        a fresh ID is minted via :func:`new_correlation_id`. Passing a
        non-empty value is useful when resuming a previously-captured
        run (e.g., replaying from log analysis) or for deterministic
        unit tests.

    Returns
    -------
    str
        The correlation ID that was actually bound — either ``value``
        if it was non-empty, or the newly-minted UUID4 hex otherwise.

    Examples
    --------
    >>> cid = set_correlation_id()           # mint + bind
    >>> cid == correlation_id.get()
    True

    >>> cid2 = set_correlation_id("custom")  # bind explicit value
    >>> cid2
    'custom'
    >>> correlation_id.get()
    'custom'
    """
    cid = value or new_correlation_id()
    correlation_id.set(cid)
    return cid


# =============================================================================
# LoggerAdapter
# =============================================================================


class CorrelationAdapter(logging.LoggerAdapter):
    """A :class:`logging.LoggerAdapter` that injects the current correlation ID.

    Every call to ``logger.info(...)``, ``logger.debug(...)``, etc. on a
    ``CorrelationAdapter`` instance reads ``correlation_id.get()`` and
    places the value into the record's ``extra`` dict under the key
    ``"correlation_id"``. That value is then accessible on the
    resulting :class:`logging.LogRecord` as ``record.correlation_id``
    and resolves cleanly in format strings such as
    ``"%(asctime)s corr=%(correlation_id)s %(message)s"`` — which is
    precisely the format declared in ``config.LOG_FORMAT``.

    Construction signature (inherited)
    ----------------------------------
    Constructed via the standard :class:`logging.LoggerAdapter`
    signature, ``CorrelationAdapter(logger, extra=None)``. The
    ``extra`` argument may be ``None`` (no adapter-level defaults),
    an empty ``dict`` (equivalent), or a populated mapping whose keys
    will propagate to every record (e.g.
    ``{"component": "nba_client"}``). The correlation ID is injected
    by :meth:`process` irrespective of which form is chosen.

    Typical usage
    -------------
    ``utils.logger.get_logger()`` is the canonical factory — end
    callers should rarely instantiate ``CorrelationAdapter`` directly::

        from utils.logger import get_logger
        from utils.correlation import set_correlation_id

        log = get_logger(__name__)          # returns a CorrelationAdapter
        set_correlation_id()                # mint + bind
        log.info("pipeline start")          # record.correlation_id is set

    Design rationale: adapter over filter
    -------------------------------------
    A :class:`logging.Filter` attached to the root logger's handlers
    could also populate ``record.correlation_id``. The adapter
    approach is preferred because:

      * **AAP contract.** Agent Action Plan §0.4.1.1 specifies a
        ``LoggerAdapter`` subclass, not a filter.
      * **Zero caller ceremony.** Callers write ``log.info("x")``
        without thinking about ``extra=``. A filter would force every
        call site to remember to pass ``extra`` for guaranteed
        coverage.
      * **Pre-format injection.** Adapter :meth:`process` runs before
        the :class:`logging.LogRecord` is constructed, guaranteeing
        the ``correlation_id`` attribute exists before any
        :class:`logging.Formatter` formats the record. A filter on a
        handler fires after the record is created, which can interact
        awkwardly with multi-handler configurations that share a
        formatter.

    The complementary ``_CorrelationFormatter`` in ``utils/logger.py``
    is a defense-in-depth safety net that handles log records
    originating from third-party libraries (urllib3, requests, etc.)
    which emit through their own :func:`logging.getLogger` rather than
    through a ``CorrelationAdapter``.
    """

    def process(
        self,
        msg: Any,
        kwargs: MutableMapping[str, Any],
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Inject the current correlation ID into ``kwargs["extra"]``.

        This method implements the :class:`logging.LoggerAdapter`
        contract introduced in Python 3.2: it MAY rewrite the message
        and keyword arguments before they are passed to the underlying
        :class:`logging.Logger`. It MUST return the possibly-modified
        ``(msg, kwargs)`` pair.

        Algorithm
        ---------
        1. Start from a defensive copy of ``self.extra`` so the
           adapter's persistent defaults (passed to ``__init__``)
           propagate to every record without being mutated by callers.
           Note that ``self.extra`` is ``None`` by default when
           :class:`logging.LoggerAdapter` is constructed without an
           ``extra`` argument; the ``or {}`` guards that case.
        2. Merge any caller-supplied ``kwargs["extra"]`` on top of the
           adapter defaults. This preserves the explicit-over-implicit
           principle: if a caller writes
           ``log.info("x", extra={"correlation_id": "forced"})`` the
           caller's value wins. This is the idiom tests use to assert
           on specific correlation IDs without mutating the context
           variable.
        3. If neither the adapter defaults nor the caller supplied a
           ``correlation_id`` key, fill it in from the current
           :class:`contextvars.ContextVar`. The belt-and-braces
           ``or ""`` retains the empty-string sentinel if
           ``correlation_id.get()`` ever returns a falsy value
           (unreachable under the current default but safe against
           future changes).
        4. Write the fully-merged ``extra`` back into ``kwargs`` and
           return.

        Invariants
        ----------
        * Never raises: only dict copies and ``ContextVar.get()`` are
          invoked, neither of which can raise when ``ContextVar`` has
          a configured default.
        * Caller-supplied ``correlation_id`` is preserved.
        * Always populates ``kwargs["extra"]["correlation_id"]`` with
          a string (possibly empty), guaranteeing that
          ``logging.Formatter`` references to ``%(correlation_id)s``
          never raise :class:`KeyError`.

        Parameters
        ----------
        msg : Any
            The log record message. Returned unchanged.
        kwargs : MutableMapping[str, Any]
            The keyword arguments that will be forwarded to the
            underlying :class:`logging.Logger` method. Mutated in
            place (``kwargs["extra"]`` is rewritten) and returned.

        Returns
        -------
        Tuple[Any, MutableMapping[str, Any]]
            The (unmodified) message and the rewritten ``kwargs``
            mapping, per the :class:`logging.LoggerAdapter` contract.
        """
        # Step 1 — start from adapter-level defaults. ``self.extra`` can
        # legitimately be ``None`` (the stdlib default when no ``extra``
        # is passed to ``__init__``), so normalise to an empty dict.
        extra: MutableMapping[str, Any] = dict(self.extra or {})

        # Step 2 — merge caller-supplied extra on top of adapter defaults.
        # The caller's keys override adapter-level defaults for that one
        # record (explicit > implicit). We check for truthiness rather
        # than ``is not None`` so that callers passing ``extra={}``
        # neither add nor raise.
        caller_extra = kwargs.get("extra")
        if caller_extra:
            extra.update(caller_extra)

        # Step 3 — populate ``correlation_id`` from the context variable
        # ONLY if neither source above already provided one. The
        # ``or ""`` is belt-and-braces: ``correlation_id.get()`` already
        # returns ``""`` by default, so this branch effectively just
        # ensures a string is present.
        if "correlation_id" not in extra:
            extra["correlation_id"] = correlation_id.get() or ""

        # Step 4 — write back and return the (msg, kwargs) pair per the
        # LoggerAdapter contract.
        kwargs["extra"] = extra
        return msg, kwargs
