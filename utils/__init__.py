"""Cross-cutting utilities for the NBA Data Ingestion Pipeline.

This package collects the foundational, stdlib-only helpers that every
other layer composes with:

    rate_limiter        - enforces the >= 1.0s inter-request floor (Rule 2)
    schema_normalizer   - flattens NBA Stats resultSets envelopes (Rule 4)
    checkpoint          - tracks completed (domain, key) pairs (Rule 5)
    logger              - configures stdlib logging + rotating file handler
    correlation         - UUID4 correlation ID context variable and adapter
    metrics             - in-process Prometheus-style counter registry
    health              - liveness and readiness probes

The package marker is intentionally empty: callers import submodules
explicitly to keep the import graph explicit and avoid pulling in the
full observability stack when a module needs only one utility.
"""
