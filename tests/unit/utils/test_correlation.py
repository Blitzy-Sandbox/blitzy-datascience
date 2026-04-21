"""Unit tests for ``utils.correlation``.

Verifies the correlation-ID mechanism that propagates a per-run UUID4
across every log record, retry attempt, and checkpoint update. Satisfies
the project-level Observability rule (AAP §0.7.3.1) and Feature F-008.

Scope of coverage
-----------------
* :func:`utils.correlation.new_correlation_id` — shape (32-character
  lowercase UUID4 hex) and per-call uniqueness contract.
* :data:`utils.correlation.correlation_id` — :class:`contextvars.ContextVar`
  default-value contract (empty string, not ``None``) and
  ``set()``/``get()`` round-trip behavior.
* :func:`utils.correlation.set_correlation_id` — the mint-and-bind
  convenience wrapper: literal passthrough when called with a non-empty
  value, UUID4 minting when called with the default empty string or an
  explicit empty string.
* :class:`utils.correlation.CorrelationAdapter.process` — injection of
  the current correlation ID into ``kwargs["extra"]``, preservation of
  caller-supplied ``extra`` keys (including an explicit
  ``correlation_id`` override), preservation of sibling kwargs such as
  ``stacklevel``, empty-context fallback, and the
  :class:`logging.LoggerAdapter` ``(msg, kwargs)`` return-tuple
  contract.
* :class:`utils.correlation.CorrelationAdapter` integration with
  :class:`logging.Logger` — assertion via pytest's ``caplog`` fixture
  that the adapter successfully stamps ``record.correlation_id`` onto
  captured :class:`logging.LogRecord` objects.
* :class:`contextvars.ContextVar` threading semantics — a mutation in a
  child thread does NOT leak back to the main thread, and the child
  thread observes the declared default (empty string) on entry.
* Module public API surface — ``hasattr`` checks for every exported
  name plus :func:`issubclass` verification that
  :class:`CorrelationAdapter` is a :class:`logging.LoggerAdapter`
  subclass.

All tests are network-free, filesystem-neutral, and free of third-party
mocking libraries (AAP §0.6.2.8). Each test starts with a clean
correlation-ID context thanks to the autouse
``_reset_correlation_id_between_tests`` fixture defined in
``tests/conftest.py`` — no per-test reset boilerplate is required here.

Authoritative references
------------------------
* AAP §0.1.3 — "introduce a correlation-ID mechanism (UUID4 generated
  at CLI start, propagated via ``logging.LoggerAdapter`` or
  ``contextvars``)".
* AAP §0.5.1.2 Group 2 — ``utils/correlation.py`` contract.
* AAP §0.7.3.1 — Observability rule, structured logging with
  correlation IDs.
* Parent package docstring (``tests/unit/utils/__init__.py``) —
  enumerates this module's responsibilities: correlation-ID ContextVar,
  ``new_correlation_id()``, ``CorrelationAdapter``.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Dict, List, Tuple  # noqa: F401 - Tuple/List kept for Dict annotation vocabulary

import pytest  # noqa: F401 - required for fixture auto-discovery and caplog typing

from utils import correlation as correlation_module
from utils.correlation import (
    CorrelationAdapter,
    correlation_id,
    new_correlation_id,
    set_correlation_id,
)


# ---------------------------------------------------------------------------
# Shared regex — the 32-character lowercase hex UUID4 contract.
# ---------------------------------------------------------------------------
#
# ``uuid.uuid4().hex`` returns a 32-character string drawn from the
# alphabet ``[0-9a-f]`` (no hyphens, no uppercase). Every test that
# exercises a minting path re-uses this single compiled pattern so the
# intent is explicit and any future change to the ID format (e.g.
# switching to base62) has a single source of truth.

_UUID4_HEX_PATTERN = re.compile(r"[0-9a-f]{32}")


# ---------------------------------------------------------------------------
# Phase 2.1 — ``new_correlation_id`` behavior
# ---------------------------------------------------------------------------
#
# The production contract (``utils/correlation.py`` line 150) is simply
# ``return uuid.uuid4().hex``. These tests assert the *surface* contract
# — a 32-character lowercase hex string with effectively-unique values
# across calls — rather than the internals of ``uuid.uuid4``.


def test_new_correlation_id_returns_32_char_hex() -> None:
    """``new_correlation_id()`` must return a 32-character lowercase hex string.

    Verifies the three-clause contract of the production docstring:
    ``isinstance(result, str)``, exactly 32 characters long, and every
    character drawn from ``[0-9a-f]``. The regex check via
    :func:`re.fullmatch` is strictly stronger than the length check
    alone because it also rules out uppercase hex and accidental
    hyphens from ``str(uuid.uuid4())``.
    """
    result = new_correlation_id()

    assert isinstance(result, str), "new_correlation_id must return str"
    assert len(result) == 32, f"expected 32-char hex; got len={len(result)}"
    assert _UUID4_HEX_PATTERN.fullmatch(result) is not None, (
        f"expected lowercase hex [0-9a-f]{{32}}; got {result!r}"
    )


def test_new_correlation_id_returns_unique_values() -> None:
    """100 calls must produce 100 distinct values (UUID4 collision test).

    UUID4 has 122 random bits; the probability of a collision in 100
    draws is ~10**-33 — vastly smaller than the probability of a bit
    flip in the test harness itself. 100 iterations is a compromise
    between statistical signal and speed: each call is a few
    microseconds, so the whole test runs in well under a millisecond.
    """
    minted_ids = [new_correlation_id() for _ in range(100)]

    assert len(set(minted_ids)) == 100, (
        "expected 100 unique UUID4 hex strings; got duplicates"
    )


# ---------------------------------------------------------------------------
# Phase 2.2 — ``correlation_id`` ContextVar defaults and roundtrip
# ---------------------------------------------------------------------------
#
# The ContextVar is declared in ``utils/correlation.py`` line 105 with
# ``default=""`` (NOT ``None``). The empty-string sentinel guarantees
# that ``logging.Formatter`` references to ``%(correlation_id)s`` never
# raise :class:`KeyError` when no ID has been bound. These tests lock
# that design decision in.


def test_correlation_id_default_is_empty_string() -> None:
    """The ContextVar default must be the empty string, not ``None``.

    The autouse ``_reset_correlation_id_between_tests`` fixture sets
    the ContextVar to ``""`` before every test, so the value observed
    at test entry is the reset value — which is by design identical to
    the declared default. This test asserts the reset-value / default
    shape, relying on the fixture to provide the clean state.
    """
    assert correlation_id.get() == "", (
        "correlation_id ContextVar must start the test as empty string; "
        "check that the autouse reset fixture is active"
    )


def test_correlation_id_set_and_get_roundtrip() -> None:
    """``ContextVar.set(x)`` must make ``ContextVar.get()`` return ``x``.

    This is the PEP 567 round-trip contract. Asserting it explicitly
    documents the dependency chain: every consumer of
    :mod:`utils.correlation` (``utils/logger.py``,
    ``api/nba_client.py``, ``run.py``) relies on this invariant.
    """
    correlation_id.set("abc123")

    assert correlation_id.get() == "abc123"


# ---------------------------------------------------------------------------
# Phase 2.3 — ``set_correlation_id`` convenience wrapper
# ---------------------------------------------------------------------------
#
# The convenience wrapper at ``utils/correlation.py`` lines 153-196
# collapses the two-step ``cid = new_correlation_id(); set(cid)``
# idiom into a single call. The three tests below cover each branch
# of the ``value or new_correlation_id()`` expression: a truthy value
# passes through literally; an implicit-empty call mints; an explicit
# empty string also mints (both values are falsy for ``str``).


def test_set_correlation_id_with_value_uses_literal() -> None:
    """A non-empty ``value`` arg must pass through unchanged.

    Documents the "resume a captured ID" use case — the production
    docstring (line 171) notes this is useful when replaying from log
    analysis or for deterministic unit tests elsewhere in the suite.
    The returned value and the value observable via
    :meth:`correlation_id.get` must match the literal input.
    """
    result = set_correlation_id("literal-id")

    assert result == "literal-id"
    assert correlation_id.get() == "literal-id"


def test_set_correlation_id_with_empty_mints_new_id() -> None:
    """The default (no-arg) call must mint a fresh UUID4 hex string.

    Asserts three properties of the mint-and-bind idiom:
      1. The returned value matches the 32-char UUID4 hex contract.
      2. The same value is bound to the ContextVar (visible via
         :meth:`correlation_id.get`).
      3. Two consecutive no-arg calls return DIFFERENT values — i.e.
         the wrapper really does mint on every call rather than
         caching and returning a single sentinel.
    """
    first = set_correlation_id()

    assert _UUID4_HEX_PATTERN.fullmatch(first) is not None, (
        f"first mint: expected 32-char hex; got {first!r}"
    )
    assert correlation_id.get() == first

    second = set_correlation_id()

    assert _UUID4_HEX_PATTERN.fullmatch(second) is not None, (
        f"second mint: expected 32-char hex; got {second!r}"
    )
    assert second != first, (
        "two consecutive set_correlation_id() calls must mint distinct values"
    )
    assert correlation_id.get() == second, (
        "ContextVar must be updated to the most-recent mint"
    )


def test_set_correlation_id_with_empty_string_explicit_mints_new() -> None:
    """An explicit empty-string argument must also mint a fresh UUID4.

    The production contract at line 194 is ``cid = value or
    new_correlation_id()`` — Python truthiness, not an explicit
    ``is None`` check. This guarantees that both
    ``set_correlation_id()`` and ``set_correlation_id("")`` take the
    mint branch, which is the documented behavior in the function's
    "Parameters" docstring entry (line 168).
    """
    result = set_correlation_id("")

    assert _UUID4_HEX_PATTERN.fullmatch(result) is not None, (
        f"expected 32-char hex; got {result!r}"
    )
    assert correlation_id.get() == result


# ---------------------------------------------------------------------------
# Phase 2.4 — ``CorrelationAdapter.process`` behavior
# ---------------------------------------------------------------------------
#
# These tests exercise the adapter in isolation by calling
# :meth:`CorrelationAdapter.process` directly, bypassing
# :class:`logging.Logger` entirely. The adapter contract
# (``utils/correlation.py`` lines 265-354) is a four-step algorithm:
#   1. Start from ``dict(self.extra or {})`` (adapter defaults).
#   2. Merge caller-supplied ``kwargs["extra"]`` on top.
#   3. If no ``correlation_id`` key after step 2, inject from the
#      ContextVar via ``correlation_id.get() or ""``.
#   4. Write ``kwargs["extra"] = extra`` and return ``(msg, kwargs)``.
# Each test targets one branch of that algorithm.


def test_adapter_injects_current_correlation_id_when_absent() -> None:
    """Context-var value must populate ``kwargs["extra"]["correlation_id"]``.

    Base case: neither adapter defaults nor caller kwargs carry a
    ``correlation_id`` key, so step 3 of the algorithm must fill it
    in from the ContextVar.
    """
    correlation_id.set("inject-me")
    logger = logging.getLogger("test.adapter.inject")
    adapter = CorrelationAdapter(logger, {})

    msg, kwargs = adapter.process("hello", {})

    assert msg == "hello", "process must return the message unchanged"
    assert "extra" in kwargs, "process must populate kwargs['extra']"
    assert kwargs["extra"]["correlation_id"] == "inject-me"


def test_adapter_preserves_caller_supplied_correlation_id() -> None:
    """An explicit ``extra={"correlation_id": ...}`` must NOT be overwritten.

    Documents the "explicit-over-implicit" contract at
    ``utils/correlation.py`` line 286: the caller's value wins. This
    is the idiom that upstream tests use to assert on specific
    correlation IDs without mutating the ContextVar, and it must be
    exercised here against an ambient (ContextVar) value to prove the
    precedence rule isn't accidentally the other way round.
    """
    correlation_id.set("ambient-id")
    logger = logging.getLogger("test.adapter.preserve")
    adapter = CorrelationAdapter(logger, {})

    msg, kwargs = adapter.process("msg", {"extra": {"correlation_id": "caller-id"}})

    assert msg == "msg"
    assert kwargs["extra"]["correlation_id"] == "caller-id", (
        "caller-supplied correlation_id must override the ContextVar value"
    )


def test_adapter_preserves_other_caller_extras_and_still_injects_correlation_id() -> None:
    """Caller extras without ``correlation_id`` must be merged, not replaced.

    The caller's ``extra={"user": "alice"}`` must survive AND the
    adapter must still inject ``correlation_id`` from the ContextVar.
    This protects against a lazy implementation that would
    wholesale-replace ``kwargs["extra"]`` with its own dict.
    """
    correlation_id.set("ctx-id")
    logger = logging.getLogger("test.adapter.merge")
    adapter = CorrelationAdapter(logger, {})

    msg, kwargs = adapter.process("msg", {"extra": {"user": "alice"}})

    assert msg == "msg"
    assert kwargs["extra"] == {"user": "alice", "correlation_id": "ctx-id"}, (
        "adapter must merge caller extras with ContextVar-sourced correlation_id"
    )


def test_adapter_with_empty_context_injects_empty_string() -> None:
    """Empty ContextVar (post-reset) must yield ``correlation_id == ""``.

    Both branches of the production contract
    ``correlation_id.get() or ""`` collapse to the empty string when
    the ContextVar is at its default. The test proves that the
    adapter NEVER raises KeyError at format time: the key is always
    present, even if its value is empty. The autouse reset fixture
    guarantees the ContextVar is empty at the start of the test, so
    no explicit ``set()`` call is needed.
    """
    logger = logging.getLogger("test.adapter.empty")
    adapter = CorrelationAdapter(logger, {})

    msg, kwargs = adapter.process("msg", {})

    assert msg == "msg"
    assert kwargs["extra"]["correlation_id"] == "", (
        "empty ContextVar must produce empty-string correlation_id, not None or missing key"
    )


def test_adapter_does_not_modify_caller_kwargs_dict_in_place_beyond_extra() -> None:
    """Sibling kwargs (e.g., ``stacklevel``) must survive ``process``.

    The :class:`logging.LoggerAdapter` contract permits mutation of
    ``kwargs`` but forbids loss of data. A caller that passes
    ``stacklevel=2`` (common when re-wrapping log calls) must still
    see that value in the outgoing kwargs — the adapter must only
    REWRITE the ``extra`` sub-dict, not replace the whole kwargs.
    """
    correlation_id.set("sibling-id")
    logger = logging.getLogger("test.adapter.sibling")
    adapter = CorrelationAdapter(logger, {})

    kwargs_in = {"stacklevel": 2}
    msg, kwargs_out = adapter.process("msg", kwargs_in)

    assert msg == "msg"
    assert kwargs_out["stacklevel"] == 2, (
        "stacklevel kwarg must be preserved across adapter.process()"
    )
    assert "extra" in kwargs_out, "adapter must populate kwargs['extra']"
    assert kwargs_out["extra"]["correlation_id"] == "sibling-id"


def test_adapter_process_returns_tuple() -> None:
    """``process`` must honor the ``(msg, kwargs)`` 2-tuple return contract.

    PEP-less but firm convention documented in :class:`logging.LoggerAdapter`
    since Python 3.2 and referenced in the production docstring
    (``utils/correlation.py`` lines 272-276). Downstream stdlib code
    unpacks the result as ``msg, kwargs = self.process(msg, kwargs)``
    — violating the shape would break every logging call on the
    adapter.
    """
    logger = logging.getLogger("test.adapter.tuple")
    adapter = CorrelationAdapter(logger, {})

    result = adapter.process("body", {})

    assert isinstance(result, tuple), "process must return a tuple"
    assert len(result) == 2, "process must return a 2-tuple"
    message, kwargs = result
    assert message == "body"
    assert isinstance(kwargs, dict), "second element must be the kwargs mapping"


# ---------------------------------------------------------------------------
# Phase 2.5 — Adapter integration with real ``Logger.info`` via caplog
# ---------------------------------------------------------------------------
#
# This test exercises the full pipeline: the adapter's ``process``
# method fires inside :meth:`Logger.info`, constructs a
# :class:`logging.LogRecord`, and caplog captures the record BEFORE
# any :class:`logging.Formatter` runs. The record's
# ``correlation_id`` attribute is populated directly from the
# adapter's ``extra`` merge, proving end-to-end integration of the
# adapter with the stdlib logging machinery.


def test_adapter_info_emits_record_with_correlation_id_via_caplog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end: ``adapter.info(msg)`` must emit a record with ``correlation_id``.

    caplog captures :class:`logging.LogRecord` instances BEFORE
    formatting (see the ``utils/logger.py`` Phase 2.5 note), so the
    assertion here targets ``record.correlation_id`` — the attribute
    that the adapter stamps onto every record via its ``extra`` merge.
    If the adapter's ``process`` method silently dropped the
    correlation_id key, this test would fail with ``AttributeError``
    on the ``getattr(record, "correlation_id", None)`` check.
    """
    logger = logging.getLogger("test.adapter.caplog")
    set_correlation_id("caplog-id")
    adapter = CorrelationAdapter(logger, {})

    with caplog.at_level(logging.DEBUG, logger="test.adapter.caplog"):
        adapter.info("message-body")

    records = [r for r in caplog.records if r.name == "test.adapter.caplog"]
    assert len(records) == 1, (
        f"expected exactly one record on logger test.adapter.caplog; got {len(records)}"
    )
    assert records[0].getMessage() == "message-body"
    assert getattr(records[0], "correlation_id", None) == "caplog-id", (
        "CorrelationAdapter must stamp record.correlation_id from the ContextVar"
    )


# ---------------------------------------------------------------------------
# Phase 2.6 — ContextVar threading isolation
# ---------------------------------------------------------------------------
#
# :class:`contextvars.ContextVar` mutations in a child thread do NOT
# leak back into the parent thread's context. Equally, a child thread
# starts from the declared ContextVar default — NOT the parent's
# current value. This test documents both semantics so that any
# future refactor attempting to switch to ``threading.local`` or
# copy-on-spawn propagation will fail loudly rather than silently
# change observable behavior.


def test_correlation_id_thread_isolation() -> None:
    """Main-thread ContextVar must be isolated from child-thread mutations.

    Algorithm:
      1. Set the correlation ID to a known main-thread sentinel.
      2. Spawn a child thread that records ``get()`` on entry,
         sets its own ID, then records ``get()`` again.
      3. Wait for the child with a generous timeout.
      4. Assert the child observed the DEFAULT (empty string) on
         entry — NOT the parent's sentinel (proves isolation at
         spawn) — and its own sentinel after ``set``.
      5. Assert the main thread's value is still its original
         sentinel — NOT the child's — proving isolation of the
         mutation direction.
      6. Assert the child thread terminated (``is_alive() is
         False``) so a future implementation bug cannot hang the
         whole test suite.
    """
    results: Dict[str, str] = {}

    def _child() -> None:
        # Record the default observed on child-thread entry.
        results["child_before"] = correlation_id.get()
        # Mutate; this must NOT leak back to the main thread.
        correlation_id.set("child-thread-id")
        results["child_after"] = correlation_id.get()

    correlation_id.set("main-thread-id")
    thread = threading.Thread(target=_child)
    thread.start()
    # Generous 5-second timeout protects the suite from a hung child
    # thread without materially slowing the passing path — a healthy
    # run joins in microseconds.
    thread.join(timeout=5.0)

    assert thread.is_alive() is False, (
        "child thread did not terminate within 5s; check for deadlock"
    )
    # Main-thread value must be unchanged.
    assert correlation_id.get() == "main-thread-id", (
        "child thread's ContextVar.set leaked back into the main thread"
    )
    # Child observed the default on entry, NOT the parent's value.
    assert results["child_before"] == "", (
        "child thread observed parent's correlation_id on entry; "
        "ContextVar must default-isolate across threads"
    )
    # Child observed its own value after ``set``.
    assert results["child_after"] == "child-thread-id"


# ---------------------------------------------------------------------------
# Phase 2.7 — Module surface (public API contract)
# ---------------------------------------------------------------------------
#
# The tests below document the public names that ``utils.correlation``
# must expose and the invariant that ``CorrelationAdapter`` is a
# proper :class:`logging.LoggerAdapter` subclass — which is what makes
# it drop-in usable anywhere in the codebase that expects a
# ``logging.LoggerAdapter``.


def test_module_exposes_expected_public_names() -> None:
    """Every name imported by ``utils.logger`` and ``run.py`` must exist.

    Asserts ``hasattr`` for each of the four public names:
      * ``correlation_id`` — the ContextVar.
      * ``new_correlation_id`` — the mint-only function.
      * ``CorrelationAdapter`` — the :class:`LoggerAdapter` subclass.
      * ``set_correlation_id`` — the mint-and-bind convenience wrapper.

    Also asserts ``issubclass(CorrelationAdapter, logging.LoggerAdapter)``
    — the load-bearing relationship that lets ``utils.logger.get_logger``
    return a ``CorrelationAdapter`` whose consumers can treat it as a
    plain :class:`logging.LoggerAdapter` without knowing about the
    correlation-ID mechanism.
    """
    expected_names = (
        "correlation_id",
        "new_correlation_id",
        "CorrelationAdapter",
        "set_correlation_id",
    )
    missing = [name for name in expected_names if not hasattr(correlation_module, name)]
    assert not missing, f"utils.correlation missing public names: {missing}"

    assert issubclass(CorrelationAdapter, logging.LoggerAdapter), (
        "CorrelationAdapter must subclass logging.LoggerAdapter for drop-in usage"
    )


def test_correlation_id_is_contextvar() -> None:
    """``correlation_id`` must be an instance of ``contextvars.ContextVar``.

    A class-name string check documents the intent without requiring
    this test module to import :class:`contextvars.ContextVar`
    directly — the production module imports it, and this test
    verifies the object that emerges is of the expected type. Any
    future refactor that swapped the ContextVar for a plain module
    global or a :class:`threading.local` would trip this check.
    """
    assert correlation_module.correlation_id.__class__.__name__ == "ContextVar", (
        "utils.correlation.correlation_id must be a contextvars.ContextVar instance"
    )
