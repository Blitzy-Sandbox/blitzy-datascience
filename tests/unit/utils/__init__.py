"""Unit tests for cross-cutting utility modules under `utils/`.

This package mirrors the production `utils/` directory one-to-one. Each
`test_*.py` sibling exercises the companion production module:

* ``test_correlation.py``      -> ``utils/correlation.py``
  Correlation-ID ContextVar, new_correlation_id(), CorrelationAdapter.

* ``test_rate_limiter.py``     -> ``utils/rate_limiter.py``
  Rule 2 >= 1.0s floor, FakeClock-driven wait() residuals, thread safety.

* ``test_schema_normalizer.py`` -> ``utils/schema_normalizer.py``
  Rule 4 flat-cells post-condition, resultSets/resultSet flattening,
  snake_case naming, duplicate-name uniquification.

* ``test_checkpoint.py``       -> ``utils/checkpoint.py``
  Rule 5 synchronous persistence, atomic replace, malformed-JSON recovery,
  domain-scoped reset, deep-copy snapshot, thread safety.

* ``test_logger.py``           -> ``utils/logger.py``
  F-008 stdlib-logging, handler idempotency, RotatingFileHandler,
  _CorrelationFormatter '-' fallback.

* ``test_metrics.py``          -> ``utils/metrics.py``
  Counter/histogram registry, label canonicalization, Prometheus exposition.

* ``test_health.py``           -> ``utils/health.py``
  check_health() liveness, four-probe check_readiness() with ISO-8601 UTC
  timestamps.

Every test file is network-free and filesystem-isolated. The autouse
fixtures in ``tests/conftest.py`` reset correlation ID, metrics registry,
and logger handlers between tests.
"""
