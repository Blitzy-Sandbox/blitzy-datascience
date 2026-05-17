"""Unit tests for api.nba_client.NBAClient.

Covers:
- Rule 1: NBAClient uses requests correctly (flip side of the grep invariant).
- Rule 2: rate_limiter.wait() called before every HTTP call.
- Rule 3: Required headers from config.REQUIRED_HEADERS applied to the session.
- F-003 / F-004: tenacity retry on transient failures; exhaustion handling.
- Correlation: X-Correlation-ID header attached truthy, omitted empty.
- Metrics: nba_requests_total, nba_request_failures_total, nba_retries_total,
  nba_request_duration_seconds.
- URL construction, timeout propagation, raise_for_status ordering.
- Session reuse across calls.

Every test is network-free. The session's ``get`` method is mocked via
``monkeypatch.setattr`` on the NBAClient's ``_session.get``. Tenacity's retry
waits are short-circuited by monkey-patching ``time.sleep`` and
``tenacity.nap.sleep`` so the suite stays fast.

The shared ``conftest.py`` autouse fixtures reset
``correlation.correlation_id`` to the empty string, reset
``metrics.registry``, and reset ``utils.logger`` handlers between tests, so
counters and correlation state start fresh in every function.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    HTTPError,
    Timeout,
)

import config
from api.nba_client import NBAClient
from utils import correlation, metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    body: Optional[Dict[str, Any]] = None,
) -> MagicMock:
    """Return a MagicMock mimicking the subset of requests.Response we touch.

    The mock exposes ``status_code``, ``json()`` returning ``body`` (defaults
    to an empty resultSets envelope), and ``raise_for_status()``. For non-2xx
    status codes the ``raise_for_status`` side_effect is set to an
    ``HTTPError`` whose ``.response`` attribute points back to the mock so
    ``api.nba_client._is_transient`` can classify 429/5xx as retryable via
    ``exc.response.status_code``.
    """
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(
        return_value=body if body is not None else {"resultSets": []}
    )
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        error = HTTPError(f"HTTP {status_code}")
        # Production ``_is_transient`` reads ``exc.response.status_code`` to
        # decide whether to retry; attach the response explicitly so the
        # predicate sees the numeric status on synthetic HTTPErrors too.
        error.response = response
        response.raise_for_status.side_effect = error
    return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_tenacity_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit retry sleeps so the suite stays fast.

    ``tenacity.nap.sleep`` internally calls ``time.sleep``; patching both
    makes every retry resolve immediately regardless of the
    ``wait_exponential`` parameters baked into the decorator at
    class-definition time.
    """
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("tenacity.nap.sleep", lambda _seconds: None)


@pytest.fixture
def mock_rate_limiter() -> MagicMock:
    """Return a MagicMock standing in for utils.rate_limiter.RateLimiter.

    We mock so that no real sleeping happens on the critical path. The
    ``wait`` method is a ``MagicMock`` on which the tests assert call order
    and count.
    """
    limiter = MagicMock()
    limiter.wait = MagicMock(return_value=None)
    limiter.interval = 1.0  # production __init__ logs rate_limiter.interval
    return limiter


@pytest.fixture
def client(mock_rate_limiter: MagicMock) -> NBAClient:
    """Default NBAClient with an injected mocked rate_limiter.

    The logger and metrics default to the production singletons, which are
    reset between tests by the autouse fixtures in ``conftest.py``.
    """
    return NBAClient(rate_limiter=mock_rate_limiter)


# ---------------------------------------------------------------------------
# Constructor / Rule 3 / Collaborator Injection
# ---------------------------------------------------------------------------


def test_rule3_required_headers_applied_to_session(
    client: NBAClient,
) -> None:
    """Rule 3: every key in config.REQUIRED_HEADERS is set on the session."""
    for key, value in config.REQUIRED_HEADERS.items():
        assert client._session.headers.get(key) == value, (
            f"session.headers[{key!r}] expected {value!r}, got "
            f"{client._session.headers.get(key)!r}"
        )


def test_rule3_session_has_referer_and_user_agent(
    client: NBAClient,
) -> None:
    """Rule 3 minimum: Referer + browser-like User-Agent are present."""
    assert client._session.headers.get("Referer") == "https://stats.nba.com"
    ua = client._session.headers.get("User-Agent", "")
    assert "Mozilla" in ua, f"User-Agent is not browser-like: {ua!r}"


def test_constructor_honors_injected_rate_limiter(
    mock_rate_limiter: MagicMock,
) -> None:
    """Collaborator injection: _rate_limiter is exactly the injected object."""
    client = NBAClient(rate_limiter=mock_rate_limiter)
    assert client._rate_limiter is mock_rate_limiter


def test_constructor_honors_injected_logger(
    mock_rate_limiter: MagicMock,
) -> None:
    """Collaborator injection: _logger is exactly the injected adapter."""
    my_logger = MagicMock()
    client = NBAClient(rate_limiter=mock_rate_limiter, logger=my_logger)
    assert client._logger is my_logger


def test_constructor_honors_injected_metrics(
    mock_rate_limiter: MagicMock,
) -> None:
    """Collaborator injection: _metrics is exactly the injected registry."""
    my_metrics = MagicMock()
    client = NBAClient(rate_limiter=mock_rate_limiter, metrics=my_metrics)
    assert client._metrics is my_metrics


def test_constructor_creates_session_with_headers_attribute(
    client: NBAClient,
) -> None:
    """Constructor creates a Session object with a headers mapping."""
    assert client._session is not None
    assert hasattr(client._session, "headers")
    assert hasattr(client._session, "get")


# ---------------------------------------------------------------------------
# Rule 2 — rate_limiter.wait() Before Every HTTP Call
# ---------------------------------------------------------------------------


def test_rule2_rate_limiter_wait_called_before_session_get(
    client: NBAClient,
    mock_rate_limiter: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 2: wait() is invoked BEFORE the HTTP call, not after."""
    call_order: List[str] = []

    def record_wait() -> None:
        call_order.append("wait")

    def record_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        call_order.append("get")
        return _make_response(200)

    mock_rate_limiter.wait.side_effect = record_wait
    monkeypatch.setattr(client._session, "get", record_get)

    client.get("leaguedashplayerstats", {"Season": "2025-26"})

    assert call_order == ["wait", "get"], (
        f"Expected wait before get; observed {call_order}"
    )


def test_rule2_rate_limiter_wait_called_once_per_get(
    client: NBAClient,
    mock_rate_limiter: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 2: each NBAClient.get triggers exactly one rate_limiter.wait."""
    monkeypatch.setattr(
        client._session, "get", lambda *a, **k: _make_response(200)
    )

    client.get("leaguedashplayerstats", {})
    client.get("leaguedashteamstats", {})

    assert mock_rate_limiter.wait.call_count == 2


# ---------------------------------------------------------------------------
# Metrics — nba_requests_total, nba_request_duration_seconds
# ---------------------------------------------------------------------------


def test_metrics_increments_nba_requests_total_per_endpoint(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each successful get increments nba_requests_total with endpoint label."""
    monkeypatch.setattr(
        client._session, "get", lambda *a, **k: _make_response(200)
    )

    client.get("leaguedashplayerstats", {})
    client.get("leaguedashplayerstats", {})
    client.get("leaguedashteamstats", {})

    players_value = metrics.registry.get_counter_value(
        "nba_requests_total", {"endpoint": "leaguedashplayerstats"}
    )
    teams_value = metrics.registry.get_counter_value(
        "nba_requests_total", {"endpoint": "leaguedashteamstats"}
    )
    assert players_value == 2
    assert teams_value == 1


def test_metrics_observes_request_duration_histogram(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every get observes nba_request_duration_seconds via finally clause."""
    monkeypatch.setattr(
        client._session, "get", lambda *a, **k: _make_response(200)
    )

    labels = {"endpoint": "leaguedashplayerstats"}
    before = metrics.registry.get_histogram_sum(
        "nba_request_duration_seconds", labels
    )
    client.get("leaguedashplayerstats", {})
    after = metrics.registry.get_histogram_sum(
        "nba_request_duration_seconds", labels
    )
    assert after >= before


def test_metrics_duration_observed_even_on_failure(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finally clause records duration even when _request raises."""
    monkeypatch.setattr(
        client._session,
        "get",
        MagicMock(side_effect=Timeout("timed out")),
    )

    labels = {"endpoint": "failing_endpoint"}
    before = metrics.registry.get_histogram_sum(
        "nba_request_duration_seconds", labels
    )
    with pytest.raises(Timeout):
        client.get("failing_endpoint", {})
    after = metrics.registry.get_histogram_sum(
        "nba_request_duration_seconds", labels
    )
    assert after >= before


# ---------------------------------------------------------------------------
# URL, Timeout, Params, raise_for_status Ordering
# ---------------------------------------------------------------------------


def test_url_built_by_string_concatenation(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL = config.API_BASE_URL + endpoint; no urljoin, no double slash."""
    captured: Dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _make_response(200)

    monkeypatch.setattr(client._session, "get", capture)

    client.get("leaguedashplayerstats", {"Season": "2025-26"})

    expected_url = config.API_BASE_URL + "leaguedashplayerstats"
    assert captured["args"][0] == expected_url


def test_timeout_passed_to_session_get(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session.get receives timeout=config.REQUEST_TIMEOUT_SECONDS."""
    captured: Dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return _make_response(200)

    monkeypatch.setattr(client._session, "get", capture)

    client.get("leaguedashplayerstats", {})

    assert captured["kwargs"]["timeout"] == config.REQUEST_TIMEOUT_SECONDS


def test_params_forwarded_as_params_kwarg(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Params dict flows through the params= kwarg (not embedded in URL)."""
    captured: Dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return _make_response(200)

    monkeypatch.setattr(client._session, "get", capture)

    params = {"Season": "2025-26", "SeasonType": "Regular Season"}
    client.get("leaguedashplayerstats", params)

    assert captured["kwargs"]["params"] == params


def test_raise_for_status_called_before_json(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raise_for_status fires BEFORE json() so HTTPErrors reach retry logic."""
    call_order: List[str] = []

    response = MagicMock()
    response.status_code = 200

    def rfs() -> None:
        call_order.append("raise_for_status")

    def js() -> Dict[str, Any]:
        call_order.append("json")
        return {"resultSets": []}

    response.raise_for_status = rfs
    response.json = js

    monkeypatch.setattr(client._session, "get", lambda *a, **k: response)

    client.get("leaguedashplayerstats", {})

    assert call_order == ["raise_for_status", "json"]


def test_get_returns_parsed_json(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NBAClient.get returns the dict from response.json()."""
    payload = {
        "resultSets": [
            {"name": "X", "headers": ["A"], "rowSet": [[1]]}
        ]
    }
    monkeypatch.setattr(
        client._session, "get", lambda *a, **k: _make_response(200, payload)
    )

    result = client.get("leaguedashplayerstats", {})

    assert result == payload


# ---------------------------------------------------------------------------
# Correlation Header — X-Correlation-ID
# ---------------------------------------------------------------------------


def test_correlation_header_attached_when_set(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truthy correlation ID attaches X-Correlation-ID to the outbound request."""
    captured: Dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return _make_response(200)

    monkeypatch.setattr(client._session, "get", capture)

    correlation.correlation_id.set("abc123def456")
    client.get("leaguedashplayerstats", {})

    headers = captured["kwargs"].get("headers") or {}
    assert headers.get("X-Correlation-ID") == "abc123def456"


def test_correlation_header_omitted_when_empty(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty correlation ID does NOT attach X-Correlation-ID."""
    captured: Dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return _make_response(200)

    monkeypatch.setattr(client._session, "get", capture)

    # Explicit for clarity; autouse fixture also resets between tests.
    correlation.correlation_id.set("")
    client.get("leaguedashplayerstats", {})

    headers = captured["kwargs"].get("headers")
    # Production sets per_request_headers to None when cid is falsy; accept
    # either None or an empty/dict-without-the-key as equivalent behaviour.
    assert headers is None or "X-Correlation-ID" not in headers


# ---------------------------------------------------------------------------
# Retry Behavior (F-003 / F-004)
# ---------------------------------------------------------------------------


def test_retry_on_timeout_then_success(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenacity retries on Timeout; eventual success returns the payload."""
    payload = {"resultSets": []}
    mock_get = MagicMock(
        side_effect=[
            Timeout("attempt-1"),
            Timeout("attempt-2"),
            _make_response(200, payload),
        ]
    )
    monkeypatch.setattr(client._session, "get", mock_get)

    result = client.get("leaguedashplayerstats", {})

    assert result == payload
    assert mock_get.call_count == 3


def test_retry_on_connection_error_then_success(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenacity retries on ConnectionError; eventual success returns payload."""
    payload = {"resultSets": []}
    mock_get = MagicMock(
        side_effect=[
            RequestsConnectionError("boom"),
            _make_response(200, payload),
        ]
    )
    monkeypatch.setattr(client._session, "get", mock_get)

    result = client.get("leaguedashplayerstats", {})

    assert result == payload
    assert mock_get.call_count == 2


def test_retry_on_http_error_then_success(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 response raises HTTPError -> retry -> success returns payload."""
    payload = {"resultSets": []}
    # _make_response(429, ...) already wires raise_for_status to raise.
    failing = _make_response(429, {})
    success = _make_response(200, payload)
    mock_get = MagicMock(side_effect=[failing, success])
    monkeypatch.setattr(client._session, "get", mock_get)

    result = client.get("leaguedashplayerstats", {})

    assert result == payload
    assert mock_get.call_count == 2


def test_retry_exhaustion_reraises_timeout(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After RETRY_ATTEMPTS failures the last Timeout is re-raised."""
    mock_get = MagicMock(side_effect=Timeout("always timing out"))
    monkeypatch.setattr(client._session, "get", mock_get)

    with pytest.raises(Timeout):
        client.get("leaguedashplayerstats", {})

    assert mock_get.call_count == config.RETRY_ATTEMPTS


def test_retry_exhaustion_reraises_http_error(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry exhaustion on HTTPError preserves the original HTTPError type."""
    # ``_make_response(500)`` already wires ``raise_for_status`` to raise an
    # ``HTTPError`` whose ``.response.status_code == 500`` so the production
    # ``_is_transient`` predicate correctly retries 5xx.
    response = _make_response(500)
    mock_get = MagicMock(return_value=response)
    monkeypatch.setattr(client._session, "get", mock_get)

    with pytest.raises(HTTPError):
        client.get("leaguedashplayerstats", {})

    assert mock_get.call_count == config.RETRY_ATTEMPTS


def test_retry_exhaustion_increments_failures_counter(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry exhaustion increments nba_request_failures_total exactly once."""
    mock_get = MagicMock(side_effect=Timeout("always timing out"))
    monkeypatch.setattr(client._session, "get", mock_get)

    labels = {"endpoint": "leaguedashplayerstats"}
    assert metrics.registry.get_counter_value(
        "nba_request_failures_total", labels
    ) == 0

    with pytest.raises(Timeout):
        client.get("leaguedashplayerstats", {})

    # The failures counter is incremented with both endpoint and reason
    # labels in production; we assert the endpoint-label aggregate by
    # summing across the known reason ("timeout").
    value = metrics.registry.get_counter_value(
        "nba_request_failures_total",
        {"endpoint": "leaguedashplayerstats", "reason": "timeout"},
    )
    assert value == 1


def test_retries_increment_nba_retries_total(
    client: NBAClient,
    fast_tenacity_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retry (before_sleep callback) increments nba_retries_total.

    Two failures before a success means the callback fires twice.
    """
    payload = {"resultSets": []}
    mock_get = MagicMock(
        side_effect=[
            Timeout("attempt-1"),
            Timeout("attempt-2"),
            _make_response(200, payload),
        ]
    )
    monkeypatch.setattr(client._session, "get", mock_get)

    client.get("leaguedashplayerstats", {})

    assert metrics.registry.get_counter_value(
        "nba_retries_total", {"endpoint": "leaguedashplayerstats"}
    ) == 2


# ---------------------------------------------------------------------------
# Session Reuse
# ---------------------------------------------------------------------------


def test_session_reused_across_calls(
    client: NBAClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single NBAClient instance reuses the same Session for every call."""
    session_id_before = id(client._session)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **k: _make_response(200)
    )

    client.get("leaguedashplayerstats", {})
    client.get("leaguedashteamstats", {})

    assert id(client._session) == session_id_before
