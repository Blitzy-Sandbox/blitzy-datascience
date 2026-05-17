"""Unit tests for ``utils.rate_limiter``.

Verifies the :class:`utils.rate_limiter.RateLimiter` that enforces
**Rule 2 — Rate Limiting** (``>= 1.0`` second inter-request floor)
for every outbound NBA Stats API request, satisfying Feature F-004.

The :class:`RateLimiter` is the sole pre-request gate inside
``api/nba_client.NBAClient.get``; its correctness is the single load-
bearing invariant behind Gate 8 ("zero HTTP 429 responses during a
full run").

Test-file contract highlights
-----------------------------
* The ``clock`` and ``sleeper`` constructor kwargs bind
  :func:`time.monotonic` and :func:`time.sleep` as *defaults at class-
  definition time*. Monkeypatching those module-level names after
  import does NOT retarget the defaults on existing ``RateLimiter``
  instances. The tests therefore pass the :class:`tests.conftest.FakeClock`
  methods EXPLICITLY via ``clock=fake_clock.monotonic,
  sleeper=fake_clock.sleep`` for deterministic residual-sleep
  assertions. (The ``fake_clock`` fixture also monkeypatches
  ``time.monotonic``/``time.sleep`` globally as defence-in-depth for
  incidental timing elsewhere in the call stack, but the production
  rate limiter ignores that monkeypatch.)
* Only the Phase 2.6 thread-safety test uses REAL
  :func:`time.monotonic` / :func:`time.sleep` — it constructs
  ``RateLimiter(1.0)`` without the ``clock``/``sleeper`` kwargs to
  validate the production threading.Lock contract under actual wall-
  clock contention.
* All tests are network-free and filesystem-neutral: the module
  imports no ``requests``, writes no CSV, and never touches the real
  ``output/`` or ``logs/`` directories.

Authoritative references
------------------------
* AAP §0.1.3 — Rule 2 inter-request floor.
* AAP §0.5.1.2 Group 2 — ``utils/rate_limiter.py`` contract.
* AAP §0.7.2.2 Rule 2 — binding constraint ``>= 1.0`` seconds.
* Peer test file ``tests/unit/utils/__init__.py`` — enumerates the
  invariants this module covers (floor enforcement, FakeClock-driven
  wait() residuals, thread safety).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, List

import pytest

import config
from utils.rate_limiter import RateLimiter

if TYPE_CHECKING:
    # ``FakeClock`` lives in ``tests/conftest.py`` and is exposed to this
    # module via the ``fake_clock`` pytest fixture. The import is guarded
    # by :data:`typing.TYPE_CHECKING` so it is only evaluated by static
    # analysers (``mypy``, ``pyright``, IDE indexers) — at runtime pytest
    # collects the fixture by name without needing the class to be
    # importable. The ``from __future__ import annotations`` pragma above
    # turns every annotation in this file into a string, so the forward
    # reference ``FakeClock`` below resolves correctly at type-check time
    # without triggering an import cycle.
    from tests.conftest import FakeClock


# ---------------------------------------------------------------------------
# Phase 2.1 — Floor enforcement (Rule 2)
# ---------------------------------------------------------------------------
#
# The ``RateLimiter.__init__`` docstring (see ``utils/rate_limiter.py``
# lines 163-168) explicitly promises ValueError when the effective
# interval is below :attr:`RateLimiter.RULE2_FLOOR`. These five tests
# exhaustively cover: (a) a typical sub-floor value (0.5s), (b) the
# zero edge, (c) a negative value, (d) the exact floor (accepted), and
# (e) a value clearly above the floor (accepted).
#
# The first test additionally asserts that the error message contains
# the literal phrase "Rule 2 floor" so that operator stack traces self-
# explain the failure (per the production code comment at line 252:
# "message includes the literal phrase 'Rule 2 floor' to make the
# failure self-explanatory in stack traces").


def test_rate_limiter_rejects_interval_below_rule2_floor_zero_point_five() -> None:
    """``RateLimiter(0.5)`` must raise ValueError whose message names Rule 2.

    The error message must contain the substring ``"Rule 2 floor"`` to
    guarantee operators encountering the traceback can identify the
    constraint being violated without having to consult the source.
    """
    with pytest.raises(ValueError) as exc_info:
        RateLimiter(0.5)
    assert "Rule 2 floor" in str(exc_info.value)


def test_rate_limiter_rejects_interval_below_floor_zero() -> None:
    """``RateLimiter(0.0)`` must raise ValueError — zero is below the floor."""
    with pytest.raises(ValueError):
        RateLimiter(0.0)


def test_rate_limiter_rejects_interval_below_floor_negative() -> None:
    """``RateLimiter(-1.0)`` must raise ValueError — negative is below the floor."""
    with pytest.raises(ValueError):
        RateLimiter(-1.0)


def test_rate_limiter_accepts_exact_floor_one_second() -> None:
    """``RateLimiter(1.0)`` must succeed — the floor condition is ``>=`` not ``>``.

    The production check is ``interval < self.RULE2_FLOOR`` (strict
    less-than), so the exact floor value is admissible. The resulting
    ``interval`` attribute must be exactly 1.0.
    """
    rl = RateLimiter(1.0)
    assert rl.interval == 1.0


def test_rate_limiter_accepts_interval_above_floor() -> None:
    """``RateLimiter(2.5)`` must succeed with the configured interval exposed."""
    rl = RateLimiter(2.5)
    assert rl.interval == 2.5


# ---------------------------------------------------------------------------
# Phase 2.2 — Default interval sources from config
# ---------------------------------------------------------------------------
#
# When ``min_interval_seconds`` is None (the default), the constructor
# reads ``config.RATE_LIMIT_SECONDS`` at instantiation time and coerces
# it to float. The three tests below:
#
#   1) Confirm the default-read wiring matches the canonical config
#      value (so Gate 12's "every constant has a read-site" claim is
#      backed by a behavioral test, not just documentation).
#   2) Confirm the read happens at EACH ``__init__`` invocation by
#      monkeypatching config and verifying a subsequent ``RateLimiter()``
#      picks up the new value.
#   3) Confirm the Rule 2 floor is re-validated against the (possibly
#      monkeypatched) config value — the safeguard against an operator
#      setting the ``NBA_RATE_LIMIT_SECONDS`` environment variable to a
#      sub-floor value.
#
# The ``monkeypatch`` parameter is pytest's built-in fixture that
# automatically reverts the attribute after the test returns, so the
# manipulation cannot leak across tests.


def test_rate_limiter_default_interval_reads_config_rate_limit_seconds() -> None:
    """``RateLimiter()`` without args reads ``config.RATE_LIMIT_SECONDS``.

    The ``float(...)`` coercion in the production constructor tolerates
    an int declaration, so the test compares against
    ``float(config.RATE_LIMIT_SECONDS)`` rather than the raw value.
    """
    rl = RateLimiter()
    assert rl.interval == float(config.RATE_LIMIT_SECONDS)


def test_rate_limiter_default_respects_monkeypatched_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RateLimiter()`` re-reads ``config.RATE_LIMIT_SECONDS`` at each instantiation.

    Proves that the constructor does not cache the config value at
    import time — a subsequent mutation of ``config.RATE_LIMIT_SECONDS``
    (for example, via the ``NBA_RATE_LIMIT_SECONDS`` env var mechanism
    in tests that swap the value) is visible to newly-constructed
    instances.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 2.0, raising=True)
    rl = RateLimiter()
    assert rl.interval == 2.0


def test_rate_limiter_default_rejects_monkeypatched_config_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RateLimiter()`` must raise ValueError when the config value is below the floor.

    Covers the defense-in-depth branch in the production
    ``__init__``: even if config has been (mis)configured to a sub-
    floor value — e.g., an operator sets ``NBA_RATE_LIMIT_SECONDS=0.5``
    — the constructor must fail fast before any HTTP traffic is
    emitted.
    """
    monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0.5, raising=True)
    with pytest.raises(ValueError):
        RateLimiter()


# ---------------------------------------------------------------------------
# Phase 2.3 — Wait behavior with FakeClock
# ---------------------------------------------------------------------------
#
# These six tests exercise every branch of :meth:`RateLimiter.wait`:
#
#   * First-call fast path (``_last_call is None``) — no sleep.
#   * Subsequent call under-interval — sleep for residual.
#   * Subsequent call equal or over interval — no sleep.
#   * Exact-boundary semantics (``elapsed == interval``) — no sleep
#     (production check is ``elapsed < interval``).
#   * Multiple under-interval calls — each sleeps its own residual
#     AND the production code re-reads the clock AFTER sleep, which
#     FakeClock.sleep emulates by auto-advancing ``now``.
#   * Non-default interval (2.0 s) — residual computation scales
#     correctly.
#
# Every test injects ``clock=fake_clock.monotonic,
# sleeper=fake_clock.sleep`` EXPLICITLY because the default
# :func:`time.monotonic` / :func:`time.sleep` bindings in
# ``RateLimiter.__init__`` are captured at class-definition time and
# are NOT retargeted by the fake_clock fixture's ``monkeypatch`` of
# the ``time`` module (see the module docstring for the full rationale).


def test_wait_first_call_does_not_sleep(fake_clock: FakeClock) -> None:
    """The very first ``wait()`` after construction records the timestamp and returns immediately.

    There is nothing to space against, so no sleep is owed. After the
    first call, ``fake_clock.sleeps`` must still be an empty list.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    assert fake_clock.sleeps == []


def test_wait_second_call_sleeps_residual_when_under_interval(
    fake_clock: FakeClock,
) -> None:
    """A second ``wait()`` 0.3 s after the first must sleep exactly 0.7 s.

    Clock starts at 1000.0. First ``wait`` records t=1000.0 as
    ``_last_call``. ``fake_clock.advance(0.3)`` simulates 0.3 seconds
    of other work. Second ``wait`` sees ``elapsed=0.3 < interval=1.0``
    and sleeps ``1.0 - 0.3 = 0.7`` seconds.

    ``pytest.approx`` with ``abs=1e-9`` absorbs any IEEE-754 rounding
    from the subtraction ``1.0 - 0.3``.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(0.3)
    rl.wait()
    assert fake_clock.sleeps == pytest.approx([0.7], abs=1e-9)


def test_wait_second_call_does_not_sleep_when_interval_elapsed(
    fake_clock: FakeClock,
) -> None:
    """A second ``wait()`` 2.0 s after the first must NOT sleep.

    ``elapsed=2.0 >= interval=1.0`` so the production code takes the
    ``else`` branch, updates ``_last_call`` to the current timestamp,
    and returns without invoking the sleeper.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(2.0)
    rl.wait()
    assert fake_clock.sleeps == []


def test_wait_second_call_does_not_sleep_at_exact_interval_boundary(
    fake_clock: FakeClock,
) -> None:
    """At ``elapsed == interval`` the wait does NOT sleep.

    The production conditional is ``elapsed < self._interval`` (strict
    less-than), so the exact boundary takes the no-sleep branch. This
    prevents a needless zero-duration sleep call and matches the Rule 2
    semantics: two calls spaced by exactly ``interval`` seconds have
    honoured the floor.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(1.0)
    rl.wait()
    assert fake_clock.sleeps == []


def test_wait_multiple_short_intervals_sleep_each_time(fake_clock: FakeClock) -> None:
    """Repeated under-interval calls each sleep their own residual.

    Timeline (``now`` evolves on each advance/sleep):

        t=1000.0  wait()            # first, no sleep; _last_call=1000.0
        advance(0.1)                # now=1000.1
        t=1000.1  wait()            # elapsed=0.1 -> sleep 0.9
                                    # FakeClock.sleep advances now by 0.9
                                    # now=1001.0; production code re-reads
                                    # clock AFTER sleep, so _last_call=1001.0
        advance(0.2)                # now=1001.2
        t=1001.2  wait()            # elapsed=0.2 -> sleep 0.8
                                    # now=1002.0; _last_call=1002.0

    This test therefore simultaneously verifies (a) that each sleep is
    sized against the fresh elapsed delta, and (b) that the production
    code re-reads the clock after sleeping (otherwise the second sleep
    would be ``1.0 - 0.1 - 0.2 = 0.7``, not 0.8).
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(0.1)
    rl.wait()
    fake_clock.advance(0.2)
    rl.wait()
    assert fake_clock.sleeps == pytest.approx([0.9, 0.8], abs=1e-9)


def test_wait_interval_two_seconds_enforces_longer_residual(
    fake_clock: FakeClock,
) -> None:
    """A limiter with interval=2.0s sleeps 1.5s when only 0.5s has elapsed.

    Proves the residual computation is parameterised on the configured
    interval, not hard-coded to 1.0. Confirms future config overrides
    (e.g., ``NBA_RATE_LIMIT_SECONDS=2.0`` for an extra-conservative
    schedule) would sleep longer automatically.
    """
    rl = RateLimiter(2.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(0.5)
    rl.wait()
    assert fake_clock.sleeps == pytest.approx([1.5], abs=1e-9)


# ---------------------------------------------------------------------------
# Phase 2.4 — reset() behavior
# ---------------------------------------------------------------------------
#
# :meth:`RateLimiter.reset` is a test-only utility (per the production
# docstring at ``utils/rate_limiter.py`` lines 429-458 — "Production
# pipelines MUST NOT call this method"). Its contract: clear
# ``_last_call`` so the next ``wait()`` behaves like a first call and
# returns immediately without sleeping.


def test_reset_restores_first_call_semantics(fake_clock: FakeClock) -> None:
    """After ``reset()``, the next ``wait()`` does not sleep regardless of elapsed time.

    This is the exact property tests rely on to re-run the "first-call
    fast path" branch without reconstructing the ``RateLimiter``.
    After ``wait(); reset()``, advancing only 0.1 s (normally enough
    to require a 0.9 s sleep against a 1.0 s interval) must NOT trigger
    any sleep.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    rl.reset()
    fake_clock.advance(0.1)
    rl.wait()
    assert fake_clock.sleeps == []


# ---------------------------------------------------------------------------
# Phase 2.5 — interval property
# ---------------------------------------------------------------------------
#
# The :pyattr:`RateLimiter.interval` property is the public diagnostic
# accessor for the effective minimum interval. It must return the
# resolved float regardless of whether the caller passed a float, int,
# or other numeric type to ``__init__``.


def test_interval_property_returns_configured_value() -> None:
    """Two successive instances with different intervals each report their own.

    Confirms the property is instance-scoped (not a class-level cache)
    and returns the exact value that was validated against the floor.
    """
    assert RateLimiter(1.0).interval == 1.0
    assert RateLimiter(3.14).interval == 3.14


def test_interval_property_is_float_type() -> None:
    """An int argument is coerced to float via the ``float(...)`` call in ``__init__``.

    ``RateLimiter(1)`` must yield ``interval`` of type ``float`` with
    value ``1.0``; this locks in the coercion contract so a future
    refactor that uses e.g. :class:`decimal.Decimal` must deliberately
    update this test.
    """
    rl = RateLimiter(1)
    assert isinstance(rl.interval, float)
    assert rl.interval == 1.0


# ---------------------------------------------------------------------------
# Phase 2.6 — Thread safety (REAL time — no FakeClock)
# ---------------------------------------------------------------------------
#
# The production ``wait()`` body runs under ``self._lock = threading.Lock()``.
# Two threads racing to issue requests must serialise: thread B blocks
# on the lock until thread A completes (including any sleep it owes);
# thread B then computes ITS OWN residual against the newly-updated
# ``_last_call``.
#
# The first test below spawns 5 worker threads racing against a single
# :class:`RateLimiter` with a 1.0 s floor and asserts that the total
# wall-clock elapsed time is at least 4.0 s (N-1 residual sleeps of
# 1.0 s for N=5 threads). It uses REAL ``time.monotonic`` and
# ``time.sleep`` because the FakeClock fixture's purpose is
# determinism for residual-amount assertions — here we need actual
# wall-clock contention to validate the lock.
#
# The upper-bound ``elapsed < 10.0`` keeps the test fast even under
# moderate scheduler jitter; the ``t.join(timeout=15.0)`` catches any
# pathological deadlock without hanging the test suite.
#
# The second test is a single-threaded sanity check that invoking
# ``wait()`` twice in the same thread does NOT deadlock (the lock is
# a non-reentrant ``threading.Lock`` but the production code releases
# it at the end of each call). It uses the FakeClock to keep the test
# fast.


def test_wait_is_thread_safe_sleeps_serialized() -> None:
    """5 threads racing against a 1.0s floor must serialise — elapsed >= 4.0s.

    N=5 workers: the first wins the lock race and takes the first-call
    fast path (no sleep). The remaining four each serialise behind the
    lock and each sleep ~1.0 s residual against the previous completed
    call. Minimum expected wall time therefore 4.0 s (4 residual sleeps
    of 1.0 s each). Upper bound 10.0 s tolerates scheduler jitter while
    keeping the unit-test suite fast.

    ``errors`` collects any exception raised inside a worker; a clean
    run must leave it empty. ``all(not t.is_alive() for t in threads)``
    guards against the deadlock scenario where a thread is still
    holding the lock after the join timeout.
    """
    # Uses real time.monotonic / time.sleep via the constructor defaults.
    rl = RateLimiter(1.0)
    errors: List[BaseException] = []

    def _worker() -> None:
        try:
            rl.wait()
        except BaseException as exc:  # noqa: BLE001 - capture everything so no thread dies silently
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
    elapsed = time.monotonic() - start

    assert all(not t.is_alive() for t in threads)
    assert errors == []
    # At least 4 sleeps of 1.0s (N-1 residuals for N=5 threads).
    assert elapsed >= 4.0
    # Upper bound keeps the test fast — no thread should add more than ~2s of overhead.
    assert elapsed < 10.0


def test_wait_single_thread_respects_lock_without_deadlock(
    fake_clock: FakeClock,
) -> None:
    """Invoking ``wait()`` twice in the same thread does not deadlock.

    The production ``threading.Lock`` is non-reentrant, but the
    production code acquires-and-releases it on every call. A naive
    implementation that forgot to release (e.g., ``self._lock.acquire()``
    without a paired ``release()``) would deadlock the second call —
    this test would hang forever in that failure mode, so pytest's
    default test timeout (if configured) would catch it; otherwise
    the FakeClock ensures the test runs in microseconds on success.
    """
    rl = RateLimiter(1.0, clock=fake_clock.monotonic, sleeper=fake_clock.sleep)
    rl.wait()
    fake_clock.advance(2.0)
    rl.wait()  # Must return, not deadlock.


# ---------------------------------------------------------------------------
# Phase 2.7 — Float coercion
# ---------------------------------------------------------------------------
#
# ``float(min_interval_seconds)`` in ``__init__`` accepts any numeric
# type Python can coerce to float. Python's boolean inherits from int,
# so ``float(True) == 1.0`` — which happens to equal the Rule 2 floor
# and therefore succeeds. ``float(2) == 2.0`` is the canonical int
# coercion.
#
# Locking in these coercion semantics protects a future refactor
# (e.g., switching to :class:`decimal.Decimal` for exact arithmetic)
# from silently breaking caller expectations.


def test_integer_interval_is_accepted_and_coerced() -> None:
    """``RateLimiter(2)`` coerces the int to 2.0 and exposes it as a float.

    Confirms both the value (``== 2.0``) and the type (``isinstance
    float``). The type check is meaningful because a Decimal-based
    refactor would fail ``isinstance(..., float)``.
    """
    rl = RateLimiter(2)
    assert rl.interval == 2.0
    assert isinstance(rl.interval, float)


def test_boolean_interval_raises_or_is_coerced_to_float_via_numeric() -> None:
    """``RateLimiter(True)`` coerces to 1.0 — exactly the Rule 2 floor.

    Python's ``bool`` is a subclass of ``int``, so ``float(True) == 1.0``.
    This happens to satisfy the ``>= 1.0`` floor check (the exact-
    boundary case). The resulting interval is ``1.0`` and is neither a
    ValueError nor a special-cased failure. If a future refactor
    chooses to reject non-numeric-intended booleans explicitly, this
    test will catch the behavior change and require a deliberate
    update.
    """
    rl = RateLimiter(True)
    assert rl.interval == 1.0


# ---------------------------------------------------------------------------
# Phase 2.8 — Rule 2 floor is a class attribute
# ---------------------------------------------------------------------------
#
# :attr:`RateLimiter.RULE2_FLOOR` is declared as a class attribute so
# tests — AND production auditing tools — can check the invariant by
# name without constructing an instance. The value 1.0 is the exact
# rule mandated by ``docs/New_Product_Prompt_20260418.md`` §5 Rule 2.
# Any future reduction of this constant below 1.0 would be a Rule 2
# violation.


def test_rule2_floor_class_attribute_is_exactly_one_second() -> None:
    """``RateLimiter.RULE2_FLOOR`` is exactly ``1.0`` — the Rule 2 binding constant."""
    assert RateLimiter.RULE2_FLOOR == 1.0
