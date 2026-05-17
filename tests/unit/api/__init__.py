"""Unit tests for the production ``api`` package -- the SOLE HTTP transport.

This subpackage mirrors the production ``api/`` tree one-to-one and exists to
exercise the contract of ``api.nba_client.NBAClient`` at the behavioural level
using the handwritten spy collaborators and canonical ``resultSets`` payload
fixtures defined in ``tests/conftest.py``. Every test in this package verifies
one of the following operational rules or features:

* Rule 1 (Single HTTP Client) -- the ``NBAClient`` is the ONLY code path that
  may call ``requests.get`` / ``requests.post`` / ``requests.Session``. The
  repository-wide grep invariant for Rule 1 lives in
  ``tests/invariants/test_rule1_sole_http_client.py``; the behavioural
  flip-side (the client actually performs the HTTP work via a single
  ``requests.Session``) is exercised here.
* Rule 2 (Rate Limiting) -- ``NBAClient.get`` invokes the injected
  ``RateLimiter.wait()`` before every outbound request, guaranteeing the
  >= 1.0-second inter-request floor from AAP section 0.7.2.2.
* Rule 3 (Required Headers) -- ``config.REQUIRED_HEADERS`` (``Referer``,
  browser-like ``User-Agent``) are applied to the session at construction
  time and accompany every request.
* F-003 / F-004 (Retry / Backoff) -- the ``tenacity``-decorated request path
  retries on ``requests.Timeout``, ``requests.ConnectionError``, and HTTP
  5xx / 429 ``HTTPError``; increments ``nba_retries_total`` through a
  ``before_sleep`` callback; re-raises the final exception on exhaustion
  (``reraise=True``); and increments ``nba_request_failures_total`` exactly
  once per failed invocation.

No test in this package imports ``requests`` directly; all HTTP interaction
is mediated by ``monkeypatch``-patched session methods or the
``RecordingClient`` double, preserving Rule 1 even inside the test suite.
See ``tests/unit/api/test_nba_client.py`` for the test implementations.
"""
