"""Centralized HTTP transport for the NBA Data Ingestion Pipeline.

This module is the **sole HTTP transport** in the production codebase. It
exposes a single class, :class:`NBAClient`, whose :meth:`NBAClient.get`
method is the only path by which any other production module may exchange
data with the upstream NBA Stats API at ``https://stats.nba.com/stats/``.

Operational rules enforced here
-------------------------------
* **Rule 1 — Single HTTP Client.** ``api/nba_client.py`` is the only file
  in production code permitted to import :mod:`requests` or to instantiate
  :class:`requests.Session`. This invariant is verified by the grep-based
  test ``tests/invariants/test_rule1_sole_http_client.py``.
* **Rule 2 — Rate Limiting.** Every outbound call invokes
  :meth:`utils.rate_limiter.RateLimiter.wait` before issuing HTTP, so no
  two requests are separated by less than
  :data:`config.RATE_LIMIT_SECONDS` (default 1.0s). The wait is *outside*
  the retry loop on purpose — the retry loop provides its own exponential
  back-off between attempts of the same request, whereas the rate-limit
  floor spaces *distinct* requests.
* **Rule 3 — Required Headers.** :data:`config.REQUIRED_HEADERS` is
  applied to ``self._session.headers`` in :meth:`NBAClient.__init__` so
  every outbound request inherits ``Referer``, ``User-Agent`` and the
  field-proven stabiliser headers. Per-request overrides (such as
  ``X-Correlation-ID``) merge on top — they never replace these base
  headers.

Features implemented
--------------------
* **F-003 — NBA API HTTP Client:** a single authenticated ``requests``
  session with headers, timeout, and observability wired in.
* **F-004 — Exponential Backoff Retry:** the private
  :meth:`NBAClient._request` is wrapped with :func:`tenacity.retry`
  configured via :data:`config.RETRY_ATTEMPTS`,
  :data:`config.RETRY_MULTIPLIER`, :data:`config.RETRY_MIN_WAIT`, and
  :data:`config.RETRY_MAX_WAIT`. The retry predicate is restricted to
  transient transport failures (``Timeout``, ``ConnectionError``,
  ``HTTPError``) — it never retries ``ValueError`` or generic
  ``Exception``.

Observability surface (Observability rule)
------------------------------------------
* **Metrics** (via :mod:`utils.metrics`): the counter
  ``nba_requests_total`` is incremented for every attempted request,
  ``nba_request_failures_total`` for every retry-exhausted failure,
  ``nba_retries_total`` for every retry attempt, and the histogram
  ``nba_request_duration_seconds`` observes the monotonic round-trip
  duration per call.
* **Structured logs** (via :mod:`utils.logger`): DEBUG for per-request
  details (param keys only — never values, to avoid leaking identifiers
  at INFO), INFO for request success, WARNING for each retry attempt,
  ERROR for final exhaustion. All log lines auto-carry the correlation
  ID from :mod:`utils.correlation`.
* **Single-hop distributed tracing:** when a non-empty correlation ID is
  bound to the current context, an ``X-Correlation-ID`` header is
  attached to the outbound request, linking upstream API logs to our
  local run logs.

Gate 12 — config read-sites
---------------------------
Every configuration constant relevant to HTTP transport is read in this
file by its *literal* dotted name so that static analysis can trace the
propagation:

* :data:`config.API_BASE_URL` — ``_request`` URL concatenation
* :data:`config.REQUIRED_HEADERS` — ``__init__`` session header update
* :data:`config.REQUEST_TIMEOUT_SECONDS` — ``session.get(timeout=)``
* :data:`config.RETRY_ATTEMPTS` — ``tenacity.stop_after_attempt``
* :data:`config.RETRY_MULTIPLIER` / :data:`config.RETRY_MIN_WAIT` /
  :data:`config.RETRY_MAX_WAIT` — ``tenacity.wait_exponential``

Do NOT cache these values in module-level locals; the read-site must be
discoverable by :meth:`str` search for ``config.<NAME>``.

Public contract
---------------
The only public surface is :class:`NBAClient` and its :meth:`NBAClient.get`
method. Private attributes (``_session``, ``_rate_limiter``, ``_logger``,
``_metrics``) exist for dependency-injection ergonomics in tests and MUST
NOT be consumed by other production modules.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests
from requests import Response
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, RequestException, Timeout
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import config
from utils.correlation import correlation_id
from utils.logger import get_logger
from utils.metrics import registry as metrics_registry
from utils.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Module-level tenacity ``before_sleep`` callback
# ---------------------------------------------------------------------------
# Tenacity invokes ``before_sleep`` with a :class:`RetryCallState`, not with
# a bound instance of the decorated method's class. We therefore implement
# the callback as a module-level function and obtain the metrics registry /
# logger by module import rather than via ``self``. This also sidesteps a
# bootstrapping ordering issue: the ``@retry`` decorator is applied at class-
# body evaluation time, before any :class:`NBAClient` instance exists, so
# the callback cannot legally refer to instance state anyway.
# ---------------------------------------------------------------------------
def _retry_log_before_sleep(retry_state: RetryCallState) -> None:
    """Log each retry attempt and increment the retry counter.

    Parameters
    ----------
    retry_state
        The :class:`tenacity.RetryCallState` passed by the library on each
        failed attempt. It carries ``attempt_number`` (1-indexed, referring
        to the attempt that just failed), ``outcome`` (whose
        :meth:`~concurrent.futures.Future.exception` yields the raised
        exception), and ``args`` (the positional arguments handed to the
        decorated function; here ``(self, endpoint, params)``).
    """
    # The outcome is guaranteed to be set once an attempt has been made,
    # but we guard defensively because ``RetryCallState.outcome`` is typed
    # as ``Optional`` in tenacity's own typeshed.
    exc: Optional[BaseException] = (
        retry_state.outcome.exception() if retry_state.outcome is not None else None
    )

    # Extract the endpoint label from the decorated call's positional args.
    # For ``NBAClient._request(self, endpoint, params)``:
    #   args[0] is ``self`` (the NBAClient instance)
    #   args[1] is the endpoint string
    # If the shape is unexpected (callback invoked outside the documented
    # path, or tenacity internals change), fall back to a sentinel label so
    # the retry counter still increments rather than crashing.
    endpoint = "unknown"
    if retry_state.args and len(retry_state.args) >= 2:
        endpoint = str(retry_state.args[1])

    # Use the module-level singleton directly: we cannot reach ``self`` here
    # and rebinding a registry per call would defeat metric aggregation.
    metrics_registry.inc("nba_retries_total", {"endpoint": endpoint})

    # ``get_logger`` is idempotent — the underlying ``logging.Logger`` is
    # cached by name and only the thin :class:`CorrelationAdapter` wrapper
    # is reconstructed. This is safe and inexpensive inside a retry path.
    callback_logger = get_logger("nba_client")
    # Log-hygiene: emit ONLY the exception class name and (when available)
    # the upstream HTTP status code. Formatting ``exc`` directly via
    # ``%s`` would invoke :meth:`BaseException.__str__`, which for
    # :class:`requests.HTTPError` / :class:`Timeout` embeds the full URL
    # — including any query-string parameters — and thereby bypasses the
    # param-key-only safeguard applied in :meth:`NBAClient._request`'s
    # DEBUG log. No secrets live in NBA Stats query parameters today, but
    # this logging pattern is intentionally conservative so that adding
    # an authenticated endpoint in future (or including PII-adjacent
    # query values) never introduces a leak regression.
    exc_class = type(exc).__name__ if exc is not None else "n/a"
    exc_response = getattr(exc, "response", None) if exc is not None else None
    status_code = (
        getattr(exc_response, "status_code", "n/a")
        if exc_response is not None
        else "n/a"
    )
    callback_logger.warning(
        "NBAClient retrying endpoint=%s attempt=%s exc_class=%s status=%s",
        endpoint,
        retry_state.attempt_number,
        exc_class,
        status_code,
    )


# ---------------------------------------------------------------------------
# Module-level tenacity ``retry`` predicate
# ---------------------------------------------------------------------------
# AAP §0.5.2.1 contract — the retry decorator MUST distinguish transient
# transport failures (which are worth retrying) from permanent client
# errors (which MUST propagate immediately so the caller can react).
#
# Using ``retry_if_exception_type(HTTPError)`` is too broad because
# :meth:`requests.Response.raise_for_status` raises :class:`HTTPError` for
# *every* non-2xx status uniformly — including permanent 4xx statuses
# (400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found,
# 418 I'm a teapot, 422 Unprocessable Entity, etc.). Retrying those
# wastes NBA Stats API budget, masks configuration errors behind long
# exponential-backoff delays, and risks triggering upstream abuse
# protections.
#
# This predicate inspects the HTTP status code attached to the
# :class:`HTTPError` and retries only on:
#   * 429 Too Many Requests (rate-limited; reactive backoff wins)
#   * any 5xx server error (server-side transient)
#   * :class:`Timeout` / :class:`ConnectionError` (transport-layer
#     transient)
#
# Everything else — including :class:`HTTPError` without an attached
# response, and any other exception type — is treated as non-transient
# and propagates on the first attempt.
# ---------------------------------------------------------------------------
def _is_transient(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents a transient transport failure.

    Parameters
    ----------
    exc
        The exception raised by the most recent attempt of the decorated
        function. Tenacity supplies it via
        :attr:`tenacity.RetryCallState.outcome`.

    Returns
    -------
    bool
        ``True`` when the caller should retry (socket timeout, dropped
        connection, HTTP 429, or HTTP 5xx); ``False`` otherwise. The
        default ``False`` covers the permanent-4xx case called out in
        AAP §0.5.2.1 and any non-allowlisted exception type.
    """
    # Transport-layer transients: retrying is almost always productive
    # because these failures rarely reflect a permanent state of the
    # upstream.
    if isinstance(exc, (Timeout, RequestsConnectionError)):
        return True

    # HTTP-level failures: discriminate by status code. The defensive
    # ``getattr`` chain accommodates both real ``requests``-produced
    # :class:`HTTPError` instances (which always carry a populated
    # ``.response``) and any synthetic instances that may not.
    if isinstance(exc, HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # Retry on rate-limit (429) or any server-side error (5xx).
        # Permanent client errors (other 4xx) and malformed errors
        # without a response are NOT retried.
        return status == 429 or (status is not None and status >= 500)

    # Anything else — ``ValueError``, ``KeyError``, programmer errors —
    # is by definition non-transient for this client.
    return False


class NBAClient:
    """Single HTTP client for the NBA Stats API.

    Responsibilities
    ----------------
    * Enforce the **single-HTTP-client** invariant (Rule 1): this class
      owns the only :class:`requests.Session` and the only
      :func:`requests.Session.get` call-site in production code.
    * Enforce the **≥ 1.0-second rate-limit floor** (Rule 2) by calling
      :meth:`utils.rate_limiter.RateLimiter.wait` before every outbound
      request.
    * Enforce the **required-headers contract** (Rule 3) by applying
      :data:`config.REQUIRED_HEADERS` to the session at construction time.
    * Provide resilience via tenacity-backed **retry-with-exponential-
      backoff** on transient transport failures (Feature F-004).
    * Emit **structured observability events** — logs, counters, and a
      latency histogram — for every request, retry, and failure
      (Observability rule).

    Collaborators are keyword-injected (AAP §0.4.1.2 — constructor
    injection):

    * ``rate_limiter`` — a :class:`RateLimiter` whose
      :meth:`~RateLimiter.wait` is invoked before each HTTP request.
    * ``logger`` — a :class:`logging.LoggerAdapter` (typically a
      :class:`utils.correlation.CorrelationAdapter`) returned by
      :func:`utils.logger.get_logger`.
    * ``metrics`` — the shared :class:`utils.metrics.MetricsRegistry`
      singleton exposed as :data:`utils.metrics.registry`; typed as
      :class:`~typing.Any` here because we only consume its ``inc`` and
      ``observe`` methods (duck typing).

    All three collaborators default to ``None``; when omitted, a sensible
    production default is wired in so that simple scripts and ad-hoc
    verification can use bare ``NBAClient()`` construction.

    Public contract
    ---------------
    The only public method is :meth:`get`. Private attributes exist solely
    for dependency-injection ergonomics and MUST NOT be consumed by other
    production modules.
    """

    # -----------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------
    def __init__(
        self,
        *,
        rate_limiter: Optional[RateLimiter] = None,
        logger: Optional[logging.LoggerAdapter] = None,
        metrics: Any = None,
    ) -> None:
        """Initialize the HTTP client and its collaborators.

        All three collaborator parameters are **keyword-only** (note the
        bare ``*`` separator in the signature). This is a deliberate
        architectural guardrail: pipeline and test callers instantiate
        :class:`NBAClient` via ``NBAClient(rate_limiter=..., logger=...,
        metrics=...)`` exclusively, eliminating an entire class of silent
        argument-reorder regressions (e.g. ``NBAClient(logger,
        rate_limiter, metrics)`` mis-binding the logger as the rate
        limiter) that would otherwise slip past the
        :class:`RecordingClient` spy used throughout the test suite.

        Parameters
        ----------
        rate_limiter
            Optional :class:`RateLimiter` instance. When ``None`` (the
            default), a fresh :class:`RateLimiter` is created, which in
            turn reads :data:`config.RATE_LIMIT_SECONDS`.
        logger
            Optional :class:`logging.LoggerAdapter`. When ``None`` (the
            default), :func:`utils.logger.get_logger` is invoked with the
            name ``"nba_client"``, yielding a
            :class:`~utils.correlation.CorrelationAdapter` that
            auto-injects the current correlation ID.
        metrics
            Optional metrics registry. When ``None`` (the default), the
            module-level :data:`utils.metrics.registry` singleton is used.
            Typed as :class:`~typing.Any` because only ``inc`` and
            ``observe`` are consumed; coupling to the concrete class name
            is deliberately avoided.
        """
        # Wire collaborators with explicit-``None`` fallbacks rather than
        # the shorter ``x or default`` idiom, so we do not silently
        # substitute defaults for *falsy-but-valid* injected objects.
        self._rate_limiter: RateLimiter = (
            rate_limiter if rate_limiter is not None else RateLimiter()
        )
        self._logger: logging.LoggerAdapter = (
            logger if logger is not None else get_logger("nba_client")
        )
        self._metrics: Any = metrics if metrics is not None else metrics_registry

        # Rule 1 read-site: the sole ``requests.Session`` in production.
        self._session: requests.Session = requests.Session()

        # Rule 3 read-site (Gate 12): apply the required headers once, at
        # the session level. Every subsequent ``session.get`` inherits
        # these automatically. ``.update`` (rather than assignment)
        # preserves whatever internal defaults :mod:`requests` attaches.
        self._session.headers.update(config.REQUIRED_HEADERS)

        # Emit one DEBUG line at construction so operators diagnosing a
        # config issue can see the effective values without first issuing
        # a request. Each literal ``config.<NAME>`` reference here is also
        # a Gate-12 read-site.
        self._logger.debug(
            "NBAClient initialized base_url=%s timeout=%s rate_limit=%s",
            config.API_BASE_URL,
            config.REQUEST_TIMEOUT_SECONDS,
            self._rate_limiter.interval,
        )

    # -----------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------
    def get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Issue a GET request to the NBA Stats API and return parsed JSON.

        This is the **sole** public method of the transport layer. All
        pipelines and endpoint wrappers must reach the upstream API via
        this function.

        Parameters
        ----------
        endpoint
            The endpoint name relative to :data:`config.API_BASE_URL`
            (e.g. ``"leaguedashplayerstats"``). It is appended directly
            to the base URL; callers MUST NOT pre-pend a slash because
            :data:`config.API_BASE_URL` already carries a trailing slash.
        params
            The query-string parameters dict. May be empty but must not
            be ``None``. Values are passed through unchanged to
            :meth:`requests.Session.get`.

        Returns
        -------
        dict
            The parsed JSON body, typically containing a ``resultSets``
            array per the upstream envelope contract.

        Raises
        ------
        TypeError
            If ``endpoint`` is not a non-empty :class:`str`, or if
            ``params`` is not a :class:`dict`. Raised synchronously at
            the trust boundary before any rate-limit spacing, metrics,
            or HTTP activity.
        requests.exceptions.HTTPError
            On a non-2xx final response after retry exhaustion.
        requests.exceptions.RequestException
            On final transport failure after retry exhaustion (for
            example, a :class:`~requests.exceptions.Timeout` or
            :class:`~requests.exceptions.ConnectionError` that persists
            across all attempts).
        """
        # --- Trust-boundary input validation -----------------------------------
        # :meth:`get` is the trust boundary between untrusted callers
        # (endpoint wrappers invoked from CLI commands and tests) and the
        # upstream HTTP transport. Validate the *kind* of each argument
        # here so that a mis-typed caller fails fast with an explicit
        # :class:`TypeError` at the boundary, rather than surfacing a
        # cryptic ``TypeError: unsupported operand type(s) for +`` during
        # URL concatenation (when ``endpoint is None``) or a misleading
        # upstream error from :mod:`requests` (when ``params is None``
        # silently passes through to :meth:`requests.Session.get` as
        # "no params"). This is the single enforcement point for CWE-20
        # (Improper Input Validation) in the transport layer; downstream
        # helpers assume well-typed inputs. Crucially, validation runs
        # BEFORE :meth:`RateLimiter.wait` so an ill-formed caller does
        # not burn a rate-limit budget slot while we sleep.
        if not isinstance(endpoint, str) or not endpoint:
            raise TypeError(
                "endpoint must be a non-empty str; "
                f"got {type(endpoint).__name__!r} ({endpoint!r})"
            )
        if not isinstance(params, dict):
            raise TypeError(
                "params must be a dict (empty dict is permitted); "
                f"got {type(params).__name__!r}"
            )

        # --- Rule 2 enforcement -------------------------------------------------
        # The rate-limit wait MUST be the first outbound-I/O statement in
        # ``get`` — it may be preceded only by the trust-boundary
        # argument validation above. Placing it *outside* the retry loop
        # is deliberate: the retry loop already supplies exponential
        # backoff between attempts of the *same* request (via
        # ``wait_exponential``), whereas this floor spaces *distinct*
        # requests so that, even after retries, we never exceed the
        # upstream rate budget.
        self._rate_limiter.wait()

        # --- Observability: attempt counter ------------------------------------
        # Increment BEFORE issuing the request so that a request that dies
        # on the wire (connection refused, DNS failure) is still represented
        # in the counter. Retry-attempts beyond the first are counted
        # separately by the ``before_sleep`` callback.
        self._metrics.inc("nba_requests_total", {"endpoint": endpoint})

        # Start timing AFTER the rate-limit wait so the duration histogram
        # measures only the HTTP round-trip, not the artificial spacing.
        # ``time.monotonic`` (not ``time.time``) is mandatory because
        # pipelines can run for tens of minutes and system clock
        # adjustments during that window would otherwise produce negative
        # or inflated durations.
        start = time.monotonic()
        try:
            payload = self._request(endpoint, params)
            return payload
        except RequestException as exc:
            # Retry has been exhausted (tenacity raised the original
            # exception via ``reraise=True``). Count the final failure and
            # re-raise unchanged so callers can ``except HTTPError`` /
            # ``except Timeout`` as they would with a plain ``requests``
            # call. The per-attempt WARNING lines were already emitted by
            # :func:`_retry_log_before_sleep`; at this terminal boundary
            # we emit ERROR without a traceback (``.error`` rather than
            # ``.exception``) to avoid duplicating stack traces.
            #
            # Classify the terminal failure into a coarse ``reason`` label
            # per the :file:`docs/OBSERVABILITY.md` metrics-catalog
            # contract so dashboard triage queries (filtered by ``reason``
            # to answer "which failure mode dominates this endpoint?")
            # return populated series. The taxonomy is closed and aligns
            # with the retry predicate's transient classes:
            #
            #   * ``timeout``           — :class:`requests.exceptions.Timeout`
            #                             OR :class:`requests.exceptions.ConnectionError`
            #                             (transport-level stall/failure).
            #   * ``http_5xx``          — server-side error;
            #                             :class:`HTTPError` with a response
            #                             whose ``status_code`` is ``>=500``.
            #   * ``http_4xx_non_429``  — any remaining client-side failure
            #                             (non-429 4xx, or an unclassified
            #                             :class:`RequestException` subclass).
            #                             A persistent 429 is retried to
            #                             exhaustion and falls into this
            #                             bucket if it surfaces here — the
            #                             bucket is indistinguishable from
            #                             any other permanent 4xx at this
            #                             boundary, which matches upstream
            #                             semantics.
            if isinstance(exc, (Timeout, RequestsConnectionError)):
                reason = "timeout"
            elif (
                isinstance(exc, HTTPError)
                and exc.response is not None
                and exc.response.status_code >= 500
            ):
                reason = "http_5xx"
            else:
                reason = "http_4xx_non_429"
            self._metrics.inc(
                "nba_request_failures_total",
                {"endpoint": endpoint, "reason": reason},
            )
            self._logger.error(
                "NBAClient request exhausted retries endpoint=%s reason=%s",
                endpoint,
                reason,
            )
            raise
        finally:
            # Observe duration regardless of success or failure so the
            # histogram represents the true population of request
            # latencies. A failing request's latency is still meaningful
            # data for capacity planning.
            duration = time.monotonic() - start
            self._metrics.observe(
                "nba_request_duration_seconds",
                duration,
                {"endpoint": endpoint},
            )

    # -----------------------------------------------------------------
    # Private: retried HTTP call
    # -----------------------------------------------------------------
    # The @retry decorator is assembled at class-body evaluation time and
    # therefore must read the tenacity-configuration constants at that
    # moment. Each literal ``config.<NAME>`` read here is a Gate-12 read-
    # site that static analysis can locate.
    #
    # ``reraise=True`` is CRITICAL: without it, tenacity wraps the final
    # exception in a :class:`tenacity.RetryError`, which would break
    # callers' ability to ``except HTTPError`` specifically. With
    # ``reraise=True``, the original exception from the final attempt
    # bubbles up unchanged.
    # -----------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(config.RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=config.RETRY_MULTIPLIER,
            min=config.RETRY_MIN_WAIT,
            max=config.RETRY_MAX_WAIT,
        ),
        retry=retry_if_exception(_is_transient),
        before_sleep=_retry_log_before_sleep,
        reraise=True,
    )
    def _request(
        self, endpoint: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Issue the HTTPS GET — decorated with tenacity retry.

        This method is intentionally **private**. All external access
        funnels through :meth:`get`, which provides the rate-limit wait,
        the attempt counter, and the duration histogram. Retry logic is
        confined to this method so that exponential-backoff sleeps are
        *inside* the measured duration window (an operator investigating
        a slow request sees the total cost, including retries).

        Parameters
        ----------
        endpoint
            Endpoint name (e.g. ``"leaguedashplayerstats"``).
        params
            Query-string parameters dict.

        Returns
        -------
        dict
            Parsed JSON body.

        Raises
        ------
        requests.exceptions.HTTPError
            On non-2xx response (raised by
            :meth:`~requests.Response.raise_for_status`). Caught by the
            tenacity predicate and retried until
            :data:`config.RETRY_ATTEMPTS` attempts are exhausted.
        requests.exceptions.Timeout
            On socket timeout. Caught by the tenacity predicate.
        requests.exceptions.ConnectionError
            On transport connection failure. Caught by the tenacity
            predicate.
        ValueError
            If the upstream returns a 2xx response whose body is not
            valid JSON. **Not retried** — the upstream contract
            guarantees JSON, and a malformed response is not a
            transient condition that a retry would resolve.
        """
        # Gate-12 read-site for ``config.API_BASE_URL``. The base URL is
        # documented to carry a trailing slash, so naive concatenation is
        # the correct and preferred join strategy (``os.path.join`` and
        # ``urllib.parse.urljoin`` both introduce surprising behaviour
        # that a grep of the source cannot easily audit).
        url = config.API_BASE_URL + endpoint

        # --- Per-request correlation-ID header -------------------------------
        # Single-hop distributed tracing per AAP §0.5.2.2: if the CLI
        # entry has minted a correlation ID and bound it to the
        # ``correlation_id`` ContextVar, attach it as
        # ``X-Correlation-ID`` on the outbound request. When no ID is
        # bound, :meth:`ContextVar.get` returns the empty string ``""``,
        # which we treat as "no header" to keep the outbound payload
        # minimal.
        per_request_headers: Optional[Dict[str, str]] = None
        cid = correlation_id.get()
        if cid:
            per_request_headers = {"X-Correlation-ID": cid}

        # DEBUG-level pre-request log. Only the param *keys* are logged
        # at DEBUG; parameter *values* (which include player and team
        # identifiers) are intentionally omitted to keep the log volume
        # bounded and to avoid leaking identifiers into long-lived log
        # archives. The sorted key list makes the output deterministic
        # across runs — useful for diffing log samples.
        self._logger.debug(
            "NBAClient GET endpoint=%s url=%s param_keys=%s",
            endpoint,
            url,
            sorted(list(params.keys())),
        )

        # Gate-12 read-site for ``config.REQUEST_TIMEOUT_SECONDS``.
        # A non-``None`` timeout is mandatory: without it a stalled
        # upstream can hang a pipeline indefinitely. The timeout applies
        # to both the connect and the read phase of the socket.
        response: Response = self._session.get(
            url,
            params=params,
            headers=per_request_headers,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )

        # Convert non-2xx responses into :class:`HTTPError` so tenacity's
        # predicate can see them and retry. ``raise_for_status`` MUST be
        # invoked before ``response.json()`` — otherwise a 4xx/5xx with a
        # JSON error body would be parsed as if it were a successful
        # response.
        response.raise_for_status()

        self._logger.info(
            "NBAClient GET ok endpoint=%s status=%s",
            endpoint,
            response.status_code,
        )

        # ``response.json()`` raises :class:`ValueError` on malformed
        # JSON. We deliberately do NOT catch this: the upstream contract
        # guarantees JSON, a malformed response is not transient, and
        # retrying would only amplify load without resolving the
        # underlying problem.
        return response.json()
