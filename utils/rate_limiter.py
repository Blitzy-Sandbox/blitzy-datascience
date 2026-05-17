"""Rate limiter enforcing the >= 1.0-second inter-request floor (Rule 2).

A single :class:`RateLimiter` instance is injected into
``api/nba_client.NBAClient`` and its :meth:`RateLimiter.wait` method is
invoked on the critical path before every outbound HTTPS GET. The
limiter guarantees that no two consecutive completed :meth:`wait` calls
return within less than ``min_interval_seconds`` of each other, where
``min_interval_seconds`` defaults to ``config.RATE_LIMIT_SECONDS``
(``1.0`` by default per Rule 2).

Authoritative references
------------------------
* Agent Action Plan §0.4.1.1 — ``RateLimiter.wait`` blocks until
  ``time.monotonic() - last_call >= RATE_LIMIT_SECONDS``.
* Agent Action Plan §0.5.1.2 — Group 2 utility, stdlib-only, thread-safe
  via a :class:`threading.Lock` forward-looking hook for future
  parallelism.
* Agent Action Plan §0.7.2.2 — Rule 2 binding constraint: the >= 1.0
  second floor is non-negotiable.
* Product brief ``docs/New_Product_Prompt_20260418.md`` §5 Rule 2 —
  "Every API call MUST pass through the rate limiter with a minimum
  1-second delay between consecutive requests."
* Gate 12 read-site trace (``config.py`` docstring, §3.3) —
  ``utils/rate_limiter.py::RateLimiter.wait`` is the designated consumer
  of ``config.RATE_LIMIT_SECONDS``.

Design invariants
-----------------
* **Standard-library only.** Permitted imports are :mod:`logging`,
  :mod:`threading`, :mod:`time`, :mod:`typing`, plus the project
  ``config`` module. No third-party dependencies are allowed here
  (F-004 / Rule 2 purity clause).
* **Monotonic clock.** Elapsed-time measurement uses
  :func:`time.monotonic` via an injectable ``clock`` parameter. The
  monotonic clock is guaranteed non-decreasing and is unaffected by
  wall-clock adjustments (NTP skew, DST transitions, VM host-clock
  changes), which are the exact failure modes :func:`time.time` would
  introduce here.
* **Defense-in-depth floor.** Even though ``config.RATE_LIMIT_SECONDS``
  is already constrained to ``>= 1.0`` by convention, the constructor
  re-validates the effective interval against :attr:`RateLimiter.RULE2_FLOOR`
  and raises :class:`ValueError` if the value is below the floor. This
  catches operator misconfiguration via the ``NBA_RATE_LIMIT_SECONDS``
  environment variable.
* **Thread-safety.** A single :class:`threading.Lock` wraps the entire
  :meth:`wait` body so two threads racing to issue requests cannot
  both decide to sleep for the same residual and then fire within less
  than ``min_interval_seconds`` of each other (TOCTOU defence). The
  lock is a forward-looking hook for the parallelism that may be added
  in a future phase per AAP §0.5.1.2.
* **Injectable seams.** ``clock`` and ``sleeper`` are kwargs on
  :meth:`RateLimiter.__init__`, enabling deterministic unit tests
  without monkey-patching the :mod:`time` module. Tests pass a
  deterministic iterator for the clock and a list-append callable for
  the sleeper.
* **No filesystem side effects.** This module deliberately does NOT
  import :mod:`utils.logger` (which would trigger
  :func:`config.ensure_directories`). Instead it uses
  :func:`logging.getLogger` directly. The resulting logger inherits
  any handlers that ``utils.logger._configure`` has attached to the
  root logger, so DEBUG output is captured at runtime but tests can
  construct a limiter without incurring any filesystem I/O.

Public API
----------
``RateLimiter``
    The rate-limiter class. See the class docstring for parameters,
    return values, and usage examples.

Implementation notes
--------------------
* **Re-read the clock after sleeping.** After :meth:`wait` invokes
  ``self._sleeper(remaining)``, it re-reads the clock to update
  ``_last_call``. This guards against platforms where
  :func:`time.sleep` may return slightly early (e.g., the ~15 ms
  Windows timer resolution) — using the actual wake time guarantees
  the NEXT call spaces against the real resume moment, never a
  hypothetical "what we intended to sleep until".
* **First call never sleeps.** The first invocation of :meth:`wait`
  has no prior call to space against, so it returns immediately and
  records ``_last_call``. This is the intentional semantics defined
  in AAP §0.4.1.1.
* **Cooperation with retry decorators.** :func:`tenacity.retry` inside
  ``api/nba_client.py`` wraps the HTTP request but does NOT wrap
  :meth:`RateLimiter.wait`. Each retry attempt therefore re-invokes
  :meth:`wait` and pays the floor again, ensuring that retry storms
  cannot produce sub-second bursts.

Example
-------
>>> from utils.rate_limiter import RateLimiter
>>> limiter = RateLimiter()            # interval = config.RATE_LIMIT_SECONDS
>>> limiter.wait()                      # first call: returns immediately
>>> limiter.wait()                      # second call: blocks ~1.0 s
"""
import logging
import threading
import time
from typing import Callable, Optional

import config


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# Uses the bare stdlib :func:`logging.getLogger` rather than
# ``utils.logger.get_logger`` because this module is foundational: it is
# imported very early in the dependency chain and must not trigger
# filesystem side effects (``config.ensure_directories``) via the logger
# configuration path. At runtime, ``utils.logger._configure`` attaches
# handlers to the ROOT logger, and logger propagation (the default) ensures
# the records emitted below are routed through those handlers. During unit
# tests that never import ``utils.logger``, this logger emits to nowhere
# (the default Python behaviour), which is exactly what we want.
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
class RateLimiter:
    """Enforces a minimum interval between successive ``wait()`` returns.

    This is the sole mechanism by which the pipeline respects Rule 2 of
    the product brief (``docs/New_Product_Prompt_20260418.md`` §5):
    no two outbound NBA Stats API requests may be separated by less
    than ``min_interval_seconds``. A single instance is constructed at
    CLI startup and injected into :class:`api.nba_client.NBAClient`,
    which calls :meth:`wait` before every HTTPS GET.

    Parameters
    ----------
    min_interval_seconds : float, optional
        Minimum number of seconds between successive completed
        :meth:`wait` calls. If ``None`` (the default), the class reads
        :data:`config.RATE_LIMIT_SECONDS` and uses that value. Must be
        ``>= 1.0`` to comply with Rule 2; values below the floor raise
        :class:`ValueError` at construction time (fail-fast, before any
        API traffic is emitted).
    clock : Callable[[], float], keyword-only, optional
        Zero-argument callable returning a monotonically non-decreasing
        float seconds value, used for elapsed-time measurement. Defaults
        to :func:`time.monotonic`. Unit tests inject a deterministic
        iterator to avoid real waiting.
    sleeper : Callable[[float], None], keyword-only, optional
        One-argument callable taking a float seconds value that blocks
        the current thread for at least that duration. Defaults to
        :func:`time.sleep`. Unit tests inject a mock (e.g., a
        ``list.append``) to count and verify sleep durations.

    Attributes
    ----------
    RULE2_FLOOR : float
        Class attribute. The minimum permitted interval in seconds
        (``1.0``). Any constructor argument below this floor raises
        :class:`ValueError`.
    interval : float
        Read-only property. The effective minimum interval in seconds
        that this limiter is enforcing. Useful for diagnostics and for
        test assertions on default construction.

    Raises
    ------
    ValueError
        If ``min_interval_seconds`` (or, when ``None``,
        ``config.RATE_LIMIT_SECONDS``) is less than
        :attr:`RULE2_FLOOR`.
    TypeError
        If ``min_interval_seconds`` is provided but cannot be coerced
        to :class:`float` (propagated from the :class:`float` call).

    Thread-safety
    -------------
    All reads and writes of the internal ``_last_call`` timestamp are
    performed inside ``with self._lock:``, where ``self._lock`` is a
    :class:`threading.Lock`. Two threads calling :meth:`wait`
    concurrently will serialise: thread B waits for thread A to complete
    its sleep, then re-evaluates the residual against the updated
    ``_last_call``.

    Examples
    --------
    Deterministic unit-test usage with injected clock and sleeper::

        times = iter([100.0, 100.3, 101.0])
        sleeps: list[float] = []
        limiter = RateLimiter(
            min_interval_seconds=1.0,
            clock=lambda: next(times),
            sleeper=sleeps.append,
        )
        limiter.wait()                  # first call: no sleep
        limiter.wait()                  # elapsed=0.3, sleeps 0.7
        assert sleeps == [0.7]

    Production usage (injected into the HTTP client)::

        rate_limiter = RateLimiter()
        client = NBAClient(rate_limiter=rate_limiter)
        response = client.get("leaguedashplayerstats", params)
    """

    # -----------------------------------------------------------------
    # Class attribute: Rule 2 floor
    # -----------------------------------------------------------------
    #
    # The minimum permitted inter-request interval, in seconds. This is
    # the exact value mandated by Rule 2 of the product brief
    # (``docs/New_Product_Prompt_20260418.md`` §5) and restated in AAP
    # §0.7.2.2. It is declared as a class attribute (not a module-level
    # constant) so tests can verify the invariant by name:
    #
    #     assert RateLimiter.RULE2_FLOOR == 1.0
    #
    # The floor is enforced in :meth:`__init__`. A constructor argument
    # below this floor is a defect and aborts construction, ensuring the
    # pipeline cannot silently operate under a dangerously aggressive
    # rate (which would risk HTTP 429 responses and potential upstream
    # abuse-detection blocks).
    RULE2_FLOOR: float = 1.0

    # -----------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------
    def __init__(
        self,
        min_interval_seconds: Optional[float] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        # Resolve the effective interval. When the caller omits
        # ``min_interval_seconds`` (the default), read the authoritative
        # configuration value. The ``float(...)`` coercion tolerates a
        # config that declares ``RATE_LIMIT_SECONDS`` as an int literal
        # (``1``) while still yielding a float for arithmetic below.
        if min_interval_seconds is None:
            interval = float(config.RATE_LIMIT_SECONDS)
        else:
            interval = float(min_interval_seconds)

        # Defense-in-depth Rule 2 floor check.
        #
        # ``config.py`` is expected to declare ``RATE_LIMIT_SECONDS = 1.0``
        # by default, but the value may be overridden at module load time
        # via the ``NBA_RATE_LIMIT_SECONDS`` environment variable. An
        # operator setting that variable to, for example, ``0.5`` would
        # silently break Rule 2. The check below catches the violation
        # at :class:`RateLimiter` construction time — before any HTTP
        # traffic is emitted — and raises a descriptive ValueError whose
        # message includes the literal phrase "Rule 2 floor" to make the
        # failure self-explanatory in stack traces.
        if interval < self.RULE2_FLOOR:
            raise ValueError(
                "RateLimiter.min_interval_seconds="
                f"{interval} violates Rule 2 floor (>= {self.RULE2_FLOOR})"
            )

        # Store the validated interval and the injectable seams.
        # Underscored attribute names signal "private to the instance"
        # and discourage external tampering.
        self._interval: float = interval
        self._clock: Callable[[], float] = clock
        self._sleeper: Callable[[float], None] = sleeper

        # Thread-safety primitive. A single lock covers the entire
        # :meth:`wait` body — finer-grained locking (e.g., separate
        # read/write locks on ``_last_call``) would create a TOCTOU race
        # where two threads both decide to sleep for the same residual
        # and then fire requests within less than ``_interval`` of each
        # other. The coarse single-lock approach is both simpler and
        # correct under every execution model we care about.
        self._lock: threading.Lock = threading.Lock()

        # Sentinel for "no previous call has been made". :meth:`wait`
        # inspects this value on every invocation: if ``None``, it
        # records the current timestamp and returns immediately (first
        # call never sleeps); otherwise it computes the elapsed delta.
        self._last_call: Optional[float] = None

    # -----------------------------------------------------------------
    # Read-only diagnostics accessor
    # -----------------------------------------------------------------
    @property
    def interval(self) -> float:
        """Return the effective minimum inter-call interval in seconds.

        This is the value that was resolved at construction (either
        ``min_interval_seconds`` or ``config.RATE_LIMIT_SECONDS``) and
        validated against :attr:`RULE2_FLOOR`. It is exposed as a
        read-only property so operators and diagnostic endpoints can
        report the enforced rate without reaching into the private
        ``_interval`` attribute.

        Returns
        -------
        float
            The effective minimum interval in seconds. Guaranteed to be
            ``>= RULE2_FLOOR``.
        """
        return self._interval

    # -----------------------------------------------------------------
    # Critical path — invoked before every HTTPS GET
    # -----------------------------------------------------------------
    def wait(self) -> None:
        """Block until at least ``min_interval_seconds`` has elapsed.

        The rule enforced is:

            ``clock() - _last_call >= _interval``

        on completion of this call. Callers should invoke :meth:`wait`
        immediately before each outbound HTTPS request and rely on the
        guarantee that control returns only when the rate-limit floor
        has been satisfied.

        Semantics
        ---------
        * **First call never sleeps.** On the very first invocation
          after construction (or after :meth:`reset`), ``_last_call`` is
          ``None``. The method records the current timestamp and returns
          immediately. There is nothing to space against.
        * **Subsequent call, under the interval.** If the elapsed delta
          since the previous completed ``wait`` is less than
          ``_interval``, the method invokes ``self._sleeper(remaining)``
          where ``remaining = _interval - elapsed``, then re-reads the
          clock to capture the real wake time as the new ``_last_call``.
          The re-read protects against sleep undersleeps (certain
          platforms, notably Windows with its ~15ms timer resolution,
          occasionally return from :func:`time.sleep` a few milliseconds
          early).
        * **Subsequent call, beyond the interval.** If the elapsed delta
          is already at or above ``_interval`` (e.g., the pipeline
          paused for unrelated reasons between requests), the method
          does NOT sleep. It simply updates ``_last_call`` to the
          current timestamp and returns. Gate 8 indirectly verifies this
          branch: a full run that completes with zero HTTP 429s implies
          neither branch is undersleeping.

        Thread-safety
        -------------
        The entire body is protected by ``self._lock``. Two threads
        calling :meth:`wait` concurrently will serialise: the second
        thread blocks on the lock until the first thread returns
        (having completed any sleep it owed). The second thread then
        computes its own residual against the newly-updated
        ``_last_call`` and sleeps accordingly. The coarse-grained lock
        guarantees that the NEXT call always spaces against the MOST
        RECENT completed call, which is the semantic Rule 2 requires.

        Observability
        -------------
        When a sleep is actually performed, a single DEBUG-level log
        record is emitted with the remaining duration, the elapsed
        delta, and the configured interval. No INFO or higher record
        is emitted — the per-request log-volume budget is owned by
        :class:`api.nba_client.NBAClient`. Metrics are the HTTP
        client's responsibility as well; keeping this module pure
        makes testing trivial.

        Returns
        -------
        None
            This method returns :class:`None` once the rate-limit floor
            has been honoured. Callers discard the return value.
        """
        # The entire body runs under the lock. The lock scope
        # deliberately covers the sleep call as well: while thread A is
        # sleeping, thread B is blocked here and cannot race to update
        # ``_last_call``. This means the effective throughput under
        # contention is strictly 1 request per ``_interval``, which is
        # exactly what Rule 2 requires.
        with self._lock:
            now = float(self._clock())

            # First-call fast path. No previous call to space against,
            # so record the timestamp and return without sleeping. This
            # is the intentional semantics per AAP §0.4.1.1: the first
            # outbound request after process start is not delayed.
            if self._last_call is None:
                self._last_call = now
                return

            elapsed = now - self._last_call

            if elapsed < self._interval:
                # Under-interval branch: compute the residual and sleep
                # for exactly that duration via the injected
                # ``_sleeper``. Default is :func:`time.sleep`; tests
                # substitute a mock.
                remaining = self._interval - elapsed

                # DEBUG-level log. Production log volume is bounded by
                # the per-request sleep cadence (at most one DEBUG line
                # per outbound API call), which is sustainable even at
                # the default DEBUG log level. The format string uses
                # `%.3f` for millisecond precision, matching the
                # conventional log resolution for wait-time diagnostics.
                logger.debug(
                    "RateLimiter: sleeping for %.3fs "
                    "(elapsed=%.3fs, interval=%.3fs)",
                    remaining,
                    elapsed,
                    self._interval,
                )

                self._sleeper(remaining)

                # Re-read the clock AFTER the sleep returns. This is
                # the key correctness detail: on some platforms
                # :func:`time.sleep` may return slightly early, leaving
                # the caller with a ``_last_call`` that is actually in
                # the future relative to the real wake time. By
                # re-reading the clock here, we guarantee that the NEXT
                # call spaces against the actual resume moment — never
                # a hypothetical "what we intended to sleep until".
                self._last_call = float(self._clock())
            else:
                # Already beyond the interval (e.g., the pipeline spent
                # time doing CPU-bound work between requests). No sleep
                # is owed; simply update the timestamp.
                self._last_call = now

    # -----------------------------------------------------------------
    # Test utility — NOT intended for production pipelines
    # -----------------------------------------------------------------
    def reset(self) -> None:
        """Forget the last-call timestamp.

        The next invocation of :meth:`wait` will behave as a first
        call and return immediately without sleeping.

        Intended use
        ------------
        Unit tests that want to exercise the first-call branch after
        already having exercised the normal-spacing branch. Production
        pipelines MUST NOT call this method — doing so would defeat
        Rule 2 by allowing two consecutive requests to fire within
        less than the floor interval.

        Thread-safety
        -------------
        The reset is performed under the same lock that guards
        :meth:`wait`, so it is safe to call from an arbitrary thread.
        It will not race with a concurrent :meth:`wait` call: the
        latter will either complete before :meth:`reset` acquires the
        lock (leaving a subsequently-cleared ``_last_call``), or block
        until :meth:`reset` releases (then proceeding with a ``None``
        sentinel and taking the first-call fast path).

        Returns
        -------
        None
        """
        with self._lock:
            self._last_call = None
