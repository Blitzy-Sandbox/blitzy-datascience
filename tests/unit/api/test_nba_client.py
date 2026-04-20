"""
Unit tests for api/nba_client.NBAClient — the sole HTTP transport in the project.

=========================================================================
CRITICAL RULE PRESERVATION
=========================================================================
Per Rule 1 (AAP §0.7.2.1) the project permits EXACTLY ONE module to import
the ``requests`` package: ``api/nba_client.py``. This test module therefore
NEVER imports ``requests`` directly. All ``requests``-adjacent symbols are
consumed through ``api.nba_client`` re-exports:

    from api.nba_client import (
        NBAClient,
        HTTPError,
        Timeout,
        RequestsConnectionError,
        RequestException,
        _is_transient,
    )

A local ``_FakeResponse`` helper satisfies the ``Response``-protocol surface
that ``NBAClient._request`` depends on (``status_code``, ``raise_for_status``,
``json``) without subclassing any ``requests`` type.

=========================================================================
COVERAGE MATRIX (mirrors QA Test Report Checkpoint IC-2)
=========================================================================
* **B.x Construction / session / Rule 3**
    - Persistent ``_session`` (``requests.Session`` instance)
    - Singleton session identity across accesses
    - ``Referer: https://stats.nba.com`` (Rule 3)
    - Browser-like ``User-Agent`` (Rule 3)
    - All ``REQUIRED_HEADERS`` present on the session
    - NBA Stats-specific headers (``x-nba-stats-origin``, ``x-nba-stats-token``)
    - Default-constructed client populates session headers
* **C.x Rule 2 — RateLimiter composition**
    - ``RateLimiter.wait()`` invoked exactly once per ``get()`` call
    - Strict ``wait → session.get`` ordering (event-capture)
    - Three back-to-back ``get()`` calls → three ``wait()`` invocations
* **D.x Retry predicate (the scope of this checkpoint's MAJOR fix)**
    - D.1: ConnectionError × 2 → success (3 attempts, 2 retries)
    - D.2: Timeout × 1 → success (2 attempts, 1 retry)
    - D.3: 500 → 503 → 200 chain (3 attempts, 2 retries)
    - D.4: 429 → success (2 attempts, 1 retry)
    - **D.5 regression**: 400/401/403/404/418/422 NOT retried (exactly 1 attempt)
    - D.6: ConnectionError × 5 → exhaustion; reraise original; failure
      counter incremented ONCE (not per retry); request counter incremented ONCE
    - D.7: non-retryable ``ValueError`` propagates with 1 attempt
* **Parametric `_is_transient` truth-table coverage**
    - All status-code buckets plus ``HTTPError`` without response,
      ``Timeout``, ``ConnectionError``, ``ValueError``, generic ``Exception``
* **E.x Metrics integration**
    - ``nba_requests_total`` labelled by endpoint; incremented 1× per ``get()``
    - ``nba_retries_total`` incremented N-1 for N-attempt call (via before_sleep)
    - ``nba_request_failures_total`` incremented exactly 1× on exhaustion
    - ``nba_request_duration_seconds`` histogram observation in ``finally``
    - Prometheus exposition carries ``# HELP`` and ``# TYPE`` metadata
* **F.x Correlation ID propagation**
    - ``X-Correlation-ID`` attached per-request when context is set
    - No header attached when correlation_id is empty
    - Same CID propagated across multiple sequential requests
    - ``X-Correlation-ID`` is per-request (NOT on session headers)
    - Operator-supplied deterministic CID propagates verbatim

=========================================================================
TECHNICAL STRATEGY
=========================================================================
* ``tenacity.nap.time.sleep`` is patched to avoid real backoff delay.
* The ``RateLimiter`` is replaced by ``MagicMock(spec=RateLimiter)`` with
  ``interval=1.0`` — this avoids the Rule 2 floor guard while preserving
  the spec surface (``wait``, ``reset``, ``interval``, ``RULE2_FLOOR``).
* HTTP transport is mocked via ``unittest.mock.patch.object`` on
  ``client._session.get`` so that no network traffic occurs.
* Each test resets the metrics registry and the correlation id context
  (the autouse fixtures in ``tests/conftest.py`` handle this).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

import config
from api.nba_client import (
    HTTPError,
    NBAClient,
    RequestException,
    RequestsConnectionError,
    Timeout,
    _is_transient,
)
from utils import metrics as metrics_module
from utils.correlation import correlation_id
from utils.rate_limiter import RateLimiter


# ======================================================================
# Shared test helpers
# ======================================================================


class _FakeResponse:
    """
    Minimal duck-type surface of ``requests.Response`` used by NBAClient.

    NBAClient._request only touches three members of the response object:

    * ``status_code`` — read by callers (not the client directly)
    * ``raise_for_status()`` — raises ``HTTPError`` with a ``response``
      attribute when ``status_code`` is in the 4xx/5xx range; otherwise no-op
    * ``json()`` — returns the parsed JSON body

    By providing this minimal surface we avoid importing ``requests`` directly
    from this test module (Rule 1 preservation).
    """

    def __init__(
        self,
        status_code: int,
        body: Optional[Dict[str, Any]] = None,
        json_side_effect: Optional[BaseException] = None,
    ) -> None:
        self.status_code = int(status_code)
        self._body: Dict[str, Any] = body if body is not None else {"ok": True}
        self._json_side_effect = json_side_effect

    def raise_for_status(self) -> None:
        """Raise ``HTTPError`` with ``response`` attribute for 4xx/5xx."""
        if self.status_code >= 400:
            err = HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err

    def json(self) -> Dict[str, Any]:
        if self._json_side_effect is not None:
            raise self._json_side_effect
        return self._body


def _make_client(
    *,
    rate_limiter: Optional[Any] = None,
    logger: Optional[Any] = None,
    metrics: Optional[Any] = None,
) -> Tuple[NBAClient, Any]:
    """
    Build an NBAClient with safe test doubles.

    The default test doubles:

    * ``rate_limiter`` → ``MagicMock(spec=RateLimiter)`` with
      ``interval=1.0`` (Rule 2 floor is handled by the real class; the mock
      bypasses it entirely so tests may run in < 1s without the floor).
    * ``logger`` → ``MagicMock()`` (allows arbitrary logging calls without
      side effects); caplog-based tests use the real ``get_logger`` path.
    * ``metrics`` → the singleton metrics registry (pre-reset by the autouse
      ``_reset_metrics_registry`` fixture from ``tests/conftest.py``).

    Returns (client, rate_limiter_mock) so tests can assert on the
    ``wait.call_count`` and call ordering.
    """
    if rate_limiter is None:
        rate_limiter = MagicMock(spec=RateLimiter)
        rate_limiter.interval = 1.0
    if logger is None:
        logger = MagicMock()
    if metrics is None:
        metrics = metrics_module.registry
    client = NBAClient(rate_limiter=rate_limiter, logger=logger, metrics=metrics)
    return client, rate_limiter


# ======================================================================
# B.x — Construction, session, Rule 3 required headers
# ======================================================================


class TestConstructionAndSession:
    """
    Covers QA Report §F-003 cases B.1–B.7 — NBAClient construction,
    session singleton, and Rule 3 required-header injection.
    """

    def test_b1_session_is_requests_session_instance(self) -> None:
        """B.1 — ``_session`` is a ``requests.Session``-compatible instance."""
        client, _ = _make_client()
        # We cannot ``import requests.Session`` (Rule 1), but a real
        # requests.Session exposes ``headers`` as a dict-like, ``get``
        # method, and ``__class__.__name__ == 'Session'``. Exercise those.
        assert hasattr(client._session, "headers")
        assert hasattr(client._session, "get")
        assert client._session.__class__.__name__ == "Session"

    def test_b2_session_is_singleton_across_accesses(self) -> None:
        """B.2 — same session object returned on every access."""
        client, _ = _make_client()
        assert client._session is client._session

    def test_b3_rule3_referer_header_is_configured(self) -> None:
        """B.3 — ``Referer: https://stats.nba.com`` per Rule 3 / AAP §0.7.2.3."""
        client, _ = _make_client()
        assert client._session.headers.get("Referer") == "https://stats.nba.com"

    def test_b4_rule3_user_agent_is_browser_like(self) -> None:
        """B.4 — ``User-Agent`` starts with a browser-like signature."""
        client, _ = _make_client()
        ua = client._session.headers.get("User-Agent", "")
        # Be slightly lenient — as long as it starts with "Mozilla/5.0"
        # and mentions a common browser engine, we accept it.
        assert isinstance(ua, str)
        assert ua.startswith("Mozilla/5.0"), ua
        # Presence of any rendering engine keyword is sufficient evidence
        # of a browser-like signature.
        assert any(
            token in ua for token in ("AppleWebKit", "Gecko", "Chrome", "Safari")
        ), ua

    def test_b5_all_required_headers_are_injected(self) -> None:
        """B.5 — every key in ``config.REQUIRED_HEADERS`` appears on the session."""
        client, _ = _make_client()
        for header_name, expected_value in config.REQUIRED_HEADERS.items():
            assert client._session.headers.get(header_name) == expected_value, (
                f"Missing or incorrect session header: {header_name}"
            )

    def test_b6_nba_stats_specific_headers_present(self) -> None:
        """B.6 — NBA Stats-specific headers set to expected sentinels."""
        client, _ = _make_client()
        assert client._session.headers.get("x-nba-stats-origin") == "stats"
        assert client._session.headers.get("x-nba-stats-token") == "true"
        # Accept/Origin must also be present as concrete strings.
        # Origin is the configured REQUIRED_HEADERS["Origin"] sentinel — we
        # read it from config to avoid lock-step duplication of the literal.
        assert client._session.headers.get("Accept")
        assert (
            client._session.headers.get("Origin")
            == config.REQUIRED_HEADERS["Origin"]
        )
        # And the configured Origin must be a stats.nba.com / www.nba.com
        # host (Rule 3 — browser-like request origin).
        assert client._session.headers.get("Origin", "").startswith("https://")
        assert "nba.com" in client._session.headers.get("Origin", "")

    def test_b7_default_constructor_populates_session_headers(self) -> None:
        """B.7 — ``NBAClient()`` with no kwargs also applies all headers."""
        client = NBAClient()
        for header_name, expected_value in config.REQUIRED_HEADERS.items():
            assert client._session.headers.get(header_name) == expected_value

    def test_default_rate_limiter_is_real_rate_limiter(self) -> None:
        """When no rate_limiter is injected, a real ``RateLimiter`` is used."""
        client = NBAClient()
        assert isinstance(client._rate_limiter, RateLimiter)

    def test_default_metrics_is_module_registry(self) -> None:
        """When no metrics is injected, the shared registry is used."""
        client = NBAClient()
        assert client._metrics is metrics_module.registry


# ======================================================================
# C.x — Rule 2 rate-limiter invocation
# ======================================================================


class TestRule2RateLimiterInvocation:
    """
    Rule 2 (AAP §0.7.2.2): ``RateLimiter.wait()`` must be called **before**
    every outbound HTTP request. This class verifies invocation count and
    strict ordering.
    """

    def test_c1_wait_called_once_per_get(self) -> None:
        """C.1 — single ``get()`` call triggers exactly one ``wait()`` call."""
        client, rl = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_c1", {"Season": "2025-26"})
        assert rl.wait.call_count == 1

    def test_c2_wait_called_before_session_get(self) -> None:
        """C.2 — strict ``wait → session.get`` ordering via event log."""
        client, rl = _make_client()
        event_log: List[str] = []
        rl.wait.side_effect = lambda: event_log.append("wait")

        def _capture_get(*args: Any, **kwargs: Any) -> _FakeResponse:
            event_log.append("get")
            return _FakeResponse(200)

        with patch("tenacity.nap.time.sleep"):
            with patch.object(client._session, "get", side_effect=_capture_get):
                client.get("endpoint_c2", {})
        assert event_log == ["wait", "get"]

    def test_c3_back_to_back_gets_have_wait_get_interleaving(self) -> None:
        """C.3 — three ``get()`` calls produce strict ``wait,get,wait,get,...`` order."""
        client, rl = _make_client()
        event_log: List[str] = []
        rl.wait.side_effect = lambda: event_log.append("wait")

        def _capture_get(*args: Any, **kwargs: Any) -> _FakeResponse:
            event_log.append("get")
            return _FakeResponse(200)

        with patch("tenacity.nap.time.sleep"):
            with patch.object(client._session, "get", side_effect=_capture_get):
                client.get("ep_a", {})
                client.get("ep_b", {})
                client.get("ep_c", {})

        assert rl.wait.call_count == 3
        assert event_log == ["wait", "get", "wait", "get", "wait", "get"]

    def test_wait_called_once_even_across_retries_within_same_get(self) -> None:
        """
        ``RateLimiter.wait()`` is invoked in ``get()`` BEFORE ``_request()``,
        not inside the retry loop. Two retries within a single ``get()``
        therefore still produce a single ``wait()`` call.
        """
        client, rl = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session,
                "get",
                side_effect=[
                    RequestsConnectionError("net-1"),
                    _FakeResponse(200),
                ],
            ) as mock_get:
                client.get("endpoint_wait_once", {})
        assert rl.wait.call_count == 1
        assert mock_get.call_count == 2


# ======================================================================
# D.x — Retry predicate coverage including the D.5 regression fix
# ======================================================================


class TestRetryPredicate:
    """
    AAP §0.5.2.1 (verbatim):

        * Transient network failures and HTTP 429 / 5xx: handled reactively
          by tenacity inside api/nba_client.py with exponential backoff and
          jitter up to RETRY_MAX_WAIT. After RETRY_ATTEMPTS the exception
          propagates.
        * Permanent HTTP 4xx (excluding 429): propagate immediately;
          pipeline-level handling decides whether to skip or abort.

    The QA report's MAJOR finding Issue #1 was that non-429 4xx responses
    were being retried. This test class contains the regression coverage.
    """

    # ---- D.1: ConnectionError × 2 → success ----
    def test_d1_connection_error_twice_then_success(self) -> None:
        """D.1 — two ConnectionErrors then success → 3 attempts, 2 retries."""
        client, _ = _make_client()
        side_effect = [
            RequestsConnectionError("net-1"),
            RequestsConnectionError("net-2"),
            _FakeResponse(200, {"ok": True}),
        ]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                result = client.get("endpoint_d1", {"Season": "2025-26"})
        assert result == {"ok": True}
        assert mock_get.call_count == 3
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_d1"}
        ) == 2.0
        assert metrics_module.registry.get_counter_value(
            "nba_requests_total", {"endpoint": "endpoint_d1"}
        ) == 1.0
        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": "endpoint_d1"}
        ) == 0.0

    # ---- D.2: Timeout × 1 → success ----
    def test_d2_timeout_then_success(self) -> None:
        """D.2 — one Timeout then success → 2 attempts."""
        client, _ = _make_client()
        side_effect = [Timeout("slow"), _FakeResponse(200)]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                result = client.get("endpoint_d2", {})
        assert result == {"ok": True}
        assert mock_get.call_count == 2
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_d2"}
        ) == 1.0

    # ---- D.3: 500 → 503 → 200 ----
    def test_d3_5xx_chain_retried_then_success(self) -> None:
        """D.3 — 500 → 503 → 200 chain → 3 attempts, 2 retries."""
        client, _ = _make_client()
        side_effect = [
            _FakeResponse(500),
            _FakeResponse(503),
            _FakeResponse(200, {"ok": True}),
        ]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                result = client.get("endpoint_d3", {})
        assert result == {"ok": True}
        assert mock_get.call_count == 3
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_d3"}
        ) == 2.0

    # ---- D.4: 429 → success ----
    def test_d4_http_429_retried_then_success(self) -> None:
        """D.4 — HTTP 429 retried; then success → 2 attempts."""
        client, _ = _make_client()
        side_effect = [_FakeResponse(429), _FakeResponse(200)]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                result = client.get("endpoint_d4", {})
        assert result == {"ok": True}
        assert mock_get.call_count == 2
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_d4"}
        ) == 1.0

    # ---- D.5 REGRESSION: non-429 4xx NOT retried ----
    @pytest.mark.parametrize(
        "status",
        [400, 401, 403, 404, 418, 422],
        ids=[
            "http-400-bad-request",
            "http-401-unauthorized",
            "http-403-forbidden",
            "http-404-not-found",
            "http-418-teapot",
            "http-422-unprocessable",
        ],
    )
    def test_d5_non_429_4xx_not_retried(self, status: int) -> None:
        """
        D.5 REGRESSION — HTTP 400/401/403/404/418/422 must NOT be retried
        per AAP §0.5.2.1. Exactly one attempt; ``HTTPError`` propagates
        with a populated ``response.status_code``.
        """
        client, rl = _make_client()
        side_effect = [_FakeResponse(status)] * (config.RETRY_ATTEMPTS + 5)
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                with pytest.raises(HTTPError) as exc_info:
                    client.get(f"endpoint_d5_{status}", {})

        # Exactly 1 attempt (not retried)
        assert mock_get.call_count == 1, (
            f"Expected exactly 1 attempt for status {status} (AAP §0.5.2.1); "
            f"observed {mock_get.call_count}. "
            "Check retry predicate; the QA Report Issue #1 is a regression."
        )
        # Zero retry counter increments
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": f"endpoint_d5_{status}"}
        ) == 0.0
        # Exactly one failure counter increment
        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": f"endpoint_d5_{status}"}
        ) == 1.0
        # The status code is attached to the raised HTTPError
        assert exc_info.value.response is not None
        assert exc_info.value.response.status_code == status
        # Rate limiter still called exactly once
        assert rl.wait.call_count == 1

    # ---- D.6: Exhaustion path ----
    def test_d6_exhaustion_reraises_and_counters_update_once(self) -> None:
        """
        D.6 — five ConnectionErrors → exhaustion; original exception
        re-raised (reraise=True); ``nba_request_failures_total`` incremented
        EXACTLY ONCE; ``nba_requests_total`` incremented EXACTLY ONCE.
        """
        client, _ = _make_client()
        exceptions = [RequestsConnectionError(f"fail-{i}") for i in range(
            config.RETRY_ATTEMPTS
        )]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=exceptions
            ) as mock_get:
                with pytest.raises(RequestsConnectionError) as exc_info:
                    client.get("endpoint_d6", {})
        # Exhaustion: session.get called exactly RETRY_ATTEMPTS times
        assert mock_get.call_count == config.RETRY_ATTEMPTS
        # The LAST exception is re-raised (reraise=True semantics)
        assert str(exc_info.value) == f"fail-{config.RETRY_ATTEMPTS - 1}"
        # Metrics contract (empirically verified in AAP preparation):
        # * requests_total: 1 (once per client.get())
        # * retries_total: N-1 (before_sleep is not called after last attempt)
        # * failures_total: 1 (only on terminal exhaustion)
        assert metrics_module.registry.get_counter_value(
            "nba_requests_total", {"endpoint": "endpoint_d6"}
        ) == 1.0
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_d6"}
        ) == float(config.RETRY_ATTEMPTS - 1)
        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": "endpoint_d6"}
        ) == 1.0

    # ---- D.7: non-RequestException exceptions are not retried ----
    def test_d7_value_error_mid_call_not_retried(self) -> None:
        """
        D.7 — a non-allowlisted ``ValueError`` raised from ``session.get``
        must NOT trigger a retry (``_is_transient(ValueError()) is False``).
        Exactly 1 attempt; the exception propagates.
        """
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session,
                "get",
                side_effect=ValueError("malformed mid-call"),
            ) as mock_get:
                with pytest.raises(ValueError, match="malformed mid-call"):
                    client.get("endpoint_d7", {})
        assert mock_get.call_count == 1
        # ``ValueError`` is not a ``RequestException`` — the failure counter
        # is NOT incremented (only the ``except RequestException`` branch
        # in ``get()`` increments failures_total).
        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": "endpoint_d7"}
        ) == 0.0

    # ---- Supplementary: mixed transient sequences ----
    def test_mixed_transient_sequence_connection_then_timeout_then_429_then_500_then_ok(self) -> None:
        """
        Supplementary — combined transient chain exercises every transient
        exception type in one retry loop. 5 attempts total = 4 retries,
        then success.

        NOTE: tenacity's ``stop_after_attempt(RETRY_ATTEMPTS)`` means the
        5th attempt is allowed and produces the successful response.
        """
        assert config.RETRY_ATTEMPTS >= 5, (
            "This test assumes RETRY_ATTEMPTS >= 5; update if config changes."
        )
        client, _ = _make_client()
        side_effect = [
            RequestsConnectionError("net"),
            Timeout("slow"),
            _FakeResponse(429),
            _FakeResponse(500),
            _FakeResponse(200, {"ok": True}),
        ]
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", side_effect=side_effect
            ) as mock_get:
                result = client.get("endpoint_mixed", {})
        assert result == {"ok": True}
        assert mock_get.call_count == 5
        assert metrics_module.registry.get_counter_value(
            "nba_retries_total", {"endpoint": "endpoint_mixed"}
        ) == 4.0


# ======================================================================
# Parametric `_is_transient` truth-table coverage
# ======================================================================


class TestIsTransientHelper:
    """
    Unit-level coverage of ``api.nba_client._is_transient``.

    The predicate is the single source of truth for retry scope after
    Issue #1 was resolved. Every branch of its conditional logic must be
    exercised here.
    """

    def _http_error(self, status: Optional[int]) -> HTTPError:
        """Construct an HTTPError with the indicated status attached."""
        err = HTTPError(f"HTTP {status}")
        if status is not None:
            err.response = _FakeResponse(status)  # type: ignore[assignment]
        return err

    @pytest.mark.parametrize("status", [429, 500, 501, 502, 503, 504, 599])
    def test_is_transient_true_for_429_and_5xx(self, status: int) -> None:
        """429 and all 5xx HTTP errors are transient (retryable)."""
        assert _is_transient(self._http_error(status)) is True

    @pytest.mark.parametrize(
        "status", [400, 401, 402, 403, 404, 405, 410, 418, 422, 426]
    )
    def test_is_transient_false_for_non_429_4xx(self, status: int) -> None:
        """Non-429 4xx HTTP errors are NOT transient — the fix to Issue #1."""
        assert _is_transient(self._http_error(status)) is False

    def test_is_transient_false_for_http_error_without_response(self) -> None:
        """HTTPError with no ``response`` attached is not retryable."""
        err = HTTPError("bare HTTPError with no response")
        assert _is_transient(err) is False

    def test_is_transient_true_for_timeout(self) -> None:
        """``Timeout`` is always transient."""
        assert _is_transient(Timeout("slow")) is True

    def test_is_transient_true_for_requests_connection_error(self) -> None:
        """``RequestsConnectionError`` is always transient."""
        assert _is_transient(RequestsConnectionError("disconnected")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad value"),
            KeyError("missing key"),
            TypeError("bad type"),
            RuntimeError("generic"),
            Exception("plain Exception"),
        ],
        ids=[
            "ValueError",
            "KeyError",
            "TypeError",
            "RuntimeError",
            "Exception",
        ],
    )
    def test_is_transient_false_for_non_retryable_exceptions(self, exc: BaseException) -> None:
        """Exceptions outside the allowlist are never transient."""
        assert _is_transient(exc) is False

    def test_is_transient_false_for_http_error_with_non_int_status(self) -> None:
        """
        Defensive — if an HTTPError's response has a non-integer
        ``status_code`` (e.g., ``None``), the predicate defaults to False
        rather than retrying.
        """
        err = HTTPError("odd response")
        err.response = _FakeResponse(200)  # type: ignore[assignment]
        err.response.status_code = None  # type: ignore[assignment]
        assert _is_transient(err) is False

    def test_is_transient_true_for_generic_request_exception_subclass(self) -> None:
        """
        ``Timeout`` and ``ConnectionError`` subclasses of
        ``RequestException`` are transient; other ``RequestException``
        subclasses without matching isinstance check are NOT transient.
        The predicate should not blanket-retry on ``RequestException``.
        """
        assert _is_transient(RequestException("generic requests error")) is False


# ======================================================================
# E.x — Metrics integration
# ======================================================================


class TestMetricsIntegration:
    """
    Verifies the end-to-end metrics contract:

    * ``nba_requests_total``      — counter, labelled by endpoint
    * ``nba_retries_total``       — counter, labelled by endpoint
    * ``nba_request_failures_total`` — counter, labelled by endpoint
    * ``nba_request_duration_seconds`` — histogram, labelled by endpoint
    """

    def test_e1_nba_requests_total_incremented_per_call_and_labelled(self) -> None:
        """
        E.1 — ``nba_requests_total`` increments by 1 on every ``get()``;
        separate endpoints accumulate independently.
        """
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_e1_a", {})
                client.get("endpoint_e1_a", {})
                client.get("endpoint_e1_b", {})

        assert metrics_module.registry.get_counter_value(
            "nba_requests_total", {"endpoint": "endpoint_e1_a"}
        ) == 2.0
        assert metrics_module.registry.get_counter_value(
            "nba_requests_total", {"endpoint": "endpoint_e1_b"}
        ) == 1.0

    def test_e2_prometheus_exposition_includes_labelled_counter(self) -> None:
        """
        E.2 — ``render_prometheus()`` output carries per-endpoint rows
        like ``nba_requests_total{endpoint="..."} <count>``.
        """
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_e2", {})

        text = metrics_module.registry.render_prometheus()
        assert 'nba_requests_total{endpoint="endpoint_e2"} 1' in text

    def test_e3_nba_request_duration_histogram_observed(self) -> None:
        """
        E.3 — the request-duration histogram is observed in the ``finally``
        block. ``_sum`` accumulates across calls with a positive value.
        """
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_e3", {})
                client.get("endpoint_e3", {})

        sum_val = metrics_module.registry.get_histogram_sum(
            "nba_request_duration_seconds", {"endpoint": "endpoint_e3"}
        )
        assert sum_val > 0.0

    def test_e4_prometheus_exposition_has_help_and_type_for_all_nba_metrics(
        self,
    ) -> None:
        """
        E.4 — ``render_prometheus()`` emits ``# HELP`` and ``# TYPE``
        metadata for the four NBA metrics registered at import.
        """
        text = metrics_module.registry.render_prometheus()
        required = [
            "nba_requests_total",
            "nba_retries_total",
            "nba_request_failures_total",
            "nba_request_duration_seconds",
        ]
        for metric in required:
            assert f"# HELP {metric}" in text, f"Missing HELP for {metric}"
            assert f"# TYPE {metric}" in text, f"Missing TYPE for {metric}"

    def test_failures_counter_not_incremented_on_success(self) -> None:
        """No failure increment on a successful ``get()`` call."""
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_success", {})

        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": "endpoint_success"}
        ) == 0.0

    def test_failures_counter_incremented_on_permanent_client_error(self) -> None:
        """
        The 403-style permanent client error path increments
        ``nba_request_failures_total`` exactly once (the except
        RequestException branch in ``get()`` runs even for non-retried
        HTTPErrors because the 4xx branch raises HTTPError — a
        RequestException subclass).
        """
        client, _ = _make_client()
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(403)
            ):
                with pytest.raises(HTTPError):
                    client.get("endpoint_403_fail", {})
        assert metrics_module.registry.get_counter_value(
            "nba_request_failures_total", {"endpoint": "endpoint_403_fail"}
        ) == 1.0


# ======================================================================
# F.x — Correlation ID propagation (Observability rule + distributed tracing)
# ======================================================================


class TestCorrelationIdPropagation:
    """
    AAP §0.5.2.2 + Observability rule: the correlation ID is propagated
    to the NBA Stats API as an ``X-Correlation-ID`` request header when
    a non-empty value is present in the ``correlation_id`` contextvar.
    """

    def test_f1_x_correlation_id_attached_when_cid_set(self) -> None:
        """F.1 — ``X-Correlation-ID`` header equals the bound CID."""
        client, _ = _make_client()
        correlation_id.set("corr-f1-XYZ")

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                client.get("endpoint_f1", {})

        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs.get("headers") == {"X-Correlation-ID": "corr-f1-XYZ"}

    def test_f2_no_correlation_id_header_when_context_empty(self) -> None:
        """
        F.2 — when ``correlation_id`` is empty (default), no per-request
        ``headers`` override is passed. ``session.get`` receives
        ``headers=None`` (or an absent keyword); no CID leaks.
        """
        # The autouse fixture resets correlation_id to "" at every test start.
        client, _ = _make_client()

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                client.get("endpoint_f2", {})

        # ``headers`` may be None or missing — both are acceptable per
        # ``api/nba_client.py`` lines 510-513. What MUST NOT happen is an
        # ``X-Correlation-ID`` key making its way into the outbound request.
        headers = mock_get.call_args.kwargs.get("headers")
        assert headers is None or "X-Correlation-ID" not in headers

    def test_f3_single_correlation_id_propagates_across_multiple_requests(
        self,
    ) -> None:
        """
        F.3 — the same CID is attached to every request within the same
        context. Three sequential calls all carry the same header value.
        """
        client, _ = _make_client()
        correlation_id.set("corr-f3-SAME")

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                client.get("endpoint_f3_a", {})
                client.get("endpoint_f3_b", {})
                client.get("endpoint_f3_c", {})

        assert mock_get.call_count == 3
        for call in mock_get.call_args_list:
            assert call.kwargs.get("headers") == {"X-Correlation-ID": "corr-f3-SAME"}

    def test_f4_x_correlation_id_is_per_request_not_on_session_headers(self) -> None:
        """
        F.4 — ``X-Correlation-ID`` is per-request; it MUST NOT be set on
        the long-lived ``_session.headers`` (otherwise rotating the CID
        would require session reset).
        """
        client, _ = _make_client()
        correlation_id.set("corr-f4-ROTATION")

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ):
                client.get("endpoint_f4", {})

        assert "X-Correlation-ID" not in client._session.headers

    def test_f5_operator_supplied_cid_propagates_verbatim(self) -> None:
        """
        F.5 — a deterministic CID supplied by the operator propagates
        verbatim (no UUID mint, no transformation).
        """
        client, _ = _make_client()
        correlation_id.set("custom-deterministic-id")

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                client.get("endpoint_f5", {})

        assert mock_get.call_args.kwargs.get("headers") == {
            "X-Correlation-ID": "custom-deterministic-id"
        }

    def test_cid_rotates_between_get_calls_when_context_changes(self) -> None:
        """
        Context-var-based CID: changing the contextvar between ``get()``
        calls yields different outbound header values without any session
        reset.
        """
        client, _ = _make_client()

        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                correlation_id.set("cid-alpha")
                client.get("endpoint_rot", {})
                correlation_id.set("cid-beta")
                client.get("endpoint_rot", {})

        calls = mock_get.call_args_list
        assert calls[0].kwargs.get("headers") == {"X-Correlation-ID": "cid-alpha"}
        assert calls[1].kwargs.get("headers") == {"X-Correlation-ID": "cid-beta"}


# ======================================================================
# Logging — pre/post-request and retry WARNING lines
# ======================================================================


class TestStructuredLogging:
    """
    Observability rule: every outbound request emits a pre-request DEBUG
    log and a post-response INFO log; retry attempts emit WARNING logs;
    exhaustion emits an ERROR log. caplog captures via the root logger
    since ``utils/logger`` configures propagation.
    """

    def test_post_response_info_log_emitted_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO log ``NBAClient GET ok endpoint=... status=200`` is emitted."""
        # Use the real logger via get_logger path — do NOT pass MagicMock().
        client = NBAClient(
            rate_limiter=_mock_rate_limiter(),
            metrics=metrics_module.registry,
        )
        with caplog.at_level(logging.INFO, logger="nba_client"):
            with patch("tenacity.nap.time.sleep"):
                with patch.object(
                    client._session, "get", return_value=_FakeResponse(200)
                ):
                    client.get("endpoint_log_info", {})

        info_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        ]
        assert any(
            "NBAClient GET ok" in msg and "endpoint=endpoint_log_info" in msg
            for msg in info_messages
        ), info_messages

    def test_exhaustion_error_log_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ERROR log emitted after retries are exhausted."""
        client = NBAClient(
            rate_limiter=_mock_rate_limiter(),
            metrics=metrics_module.registry,
        )
        with caplog.at_level(logging.ERROR, logger="nba_client"):
            exceptions = [
                RequestsConnectionError(f"err-{i}")
                for i in range(config.RETRY_ATTEMPTS)
            ]
            with patch("tenacity.nap.time.sleep"):
                with patch.object(
                    client._session, "get", side_effect=exceptions
                ):
                    with pytest.raises(RequestsConnectionError):
                        client.get("endpoint_log_err", {})

        error_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ]
        assert any(
            "exhausted retries" in msg and "endpoint=endpoint_log_err" in msg
            for msg in error_messages
        ), error_messages

    def test_retry_warning_log_emitted_with_attempt_number(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Each retry (via ``_retry_log_before_sleep``) emits a WARNING log
        with the format ``NBAClient retrying endpoint=... attempt=N error=...``.
        Two retries → two WARNING records with attempts 1 and 2.
        """
        client = NBAClient(
            rate_limiter=_mock_rate_limiter(),
            metrics=metrics_module.registry,
        )
        with caplog.at_level(logging.WARNING, logger="nba_client"):
            exceptions = [
                RequestsConnectionError("err-1"),
                RequestsConnectionError("err-2"),
                _FakeResponse(200),
            ]
            with patch("tenacity.nap.time.sleep"):
                with patch.object(
                    client._session, "get", side_effect=exceptions
                ):
                    client.get("endpoint_log_retry", {})

        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        retry_messages = [
            msg for msg in warning_messages if "NBAClient retrying" in msg
        ]
        # before_sleep runs once per retry (between attempts); two retries expected
        assert len(retry_messages) == 2, retry_messages
        assert any(
            "attempt=1" in msg and "endpoint=endpoint_log_retry" in msg
            for msg in retry_messages
        )
        assert any("attempt=2" in msg for msg in retry_messages)

    def test_no_retry_warning_for_non_429_4xx(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        For a non-retried 403 response, no ``NBAClient retrying`` WARNING
        should be emitted — the predicate rejects the exception immediately.
        """
        client = NBAClient(
            rate_limiter=_mock_rate_limiter(),
            metrics=metrics_module.registry,
        )
        with caplog.at_level(logging.WARNING, logger="nba_client"):
            with patch("tenacity.nap.time.sleep"):
                with patch.object(
                    client._session, "get", return_value=_FakeResponse(403)
                ):
                    with pytest.raises(HTTPError):
                        client.get("endpoint_no_retry_log", {})

        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        retry_messages = [
            msg for msg in warning_messages if "NBAClient retrying" in msg
        ]
        assert retry_messages == [], retry_messages


# ======================================================================
# Composition ordering — Rule 2 composed with retry
# ======================================================================


class TestCompositionOrdering:
    """
    End-to-end behavioural tests exercising the full ``get()`` composition:
    ``rate_limiter.wait → metrics.inc(requests) → time.monotonic → _request
    → tenacity retry → session.get → raise_for_status → json``.
    """

    def test_start_time_taken_after_rate_limit_wait(self) -> None:
        """
        Duration measurement excludes the rate-limit wait (Rule 2): the
        ``start = time.monotonic()`` call happens AFTER ``wait()`` returns.
        Verifies with event capture on the mock ordering.
        """
        event_log: List[str] = []
        rl = MagicMock(spec=RateLimiter)
        rl.interval = 1.0
        rl.wait.side_effect = lambda: event_log.append("wait")
        client, _ = _make_client(rate_limiter=rl)

        def _capture_get(*args: Any, **kwargs: Any) -> _FakeResponse:
            event_log.append("get")
            return _FakeResponse(200)

        with patch("tenacity.nap.time.sleep"):
            with patch.object(client._session, "get", side_effect=_capture_get):
                client.get("endpoint_order", {})

        assert event_log == ["wait", "get"]

    def test_return_value_is_parsed_json_dict(self) -> None:
        """``get()`` returns the parsed JSON body as a dict."""
        client, _ = _make_client()
        payload = {
            "resultSets": [
                {
                    "name": "LeagueDashPlayerStats",
                    "headers": ["PLAYER_ID", "PTS"],
                    "rowSet": [[1, 10], [2, 20]],
                }
            ]
        }
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200, payload)
            ):
                result = client.get("leaguedashplayerstats", {"Season": "2025-26"})
        assert result == payload

    def test_endpoint_and_params_passed_to_session_get(self) -> None:
        """
        ``session.get`` is called with the correctly-assembled URL
        (``API_BASE_URL + endpoint``) and the caller's params dict.
        """
        client, _ = _make_client()
        params = {"Season": "2025-26", "SeasonType": "Regular Season"}
        with patch("tenacity.nap.time.sleep"):
            with patch.object(
                client._session, "get", return_value=_FakeResponse(200)
            ) as mock_get:
                client.get("leaguedashteamstats", params)

        call_args = mock_get.call_args
        # URL is the first positional argument
        assert call_args.args == (
            config.API_BASE_URL + "leaguedashteamstats",
        )
        # Params are forwarded verbatim
        assert call_args.kwargs.get("params") == params
        # Timeout honours config
        assert call_args.kwargs.get("timeout") == config.REQUEST_TIMEOUT_SECONDS


# ======================================================================
# Module-level helpers used by the test classes above
# ======================================================================


def _mock_rate_limiter() -> Any:
    """Shared factory for a spec'd RateLimiter mock (``interval=1.0``)."""
    rl = MagicMock(spec=RateLimiter)
    rl.interval = 1.0
    return rl
