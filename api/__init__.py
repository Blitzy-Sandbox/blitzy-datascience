"""HTTP transport package for the NBA Data Ingestion Pipeline.

This package is the sole module tree in the production codebase permitted
to import ``requests`` or to instantiate ``requests.Session``; this
invariant is Operational Rule 1 of the product brief
(``docs/New_Product_Prompt_20260418.md`` §5) and is verified by the
grep-based invariant test ``tests/invariants/test_rule1_sole_http_client.py``.

The package exposes a single concrete class, ``NBAClient``, implemented in
``api.nba_client``; callers should import it directly::

    from api.nba_client import NBAClient

Related contracts:

* Rule 1 — Single HTTP Client (enforced by this package being the only
  importer of ``requests`` in production code).
* Rule 2 — Rate Limiting ≥ 1.0 second between requests (enforced inside
  ``NBAClient.get`` via ``utils.rate_limiter.RateLimiter.wait``).
* Rule 3 — Required Headers (``Referer`` and browser-like ``User-Agent``),
  applied to the ``requests.Session`` inside ``NBAClient.__init__``.
* Feature F-003 (NBA API HTTP Client) and Feature F-004 (Exponential
  Backoff Retry) are implemented in ``api.nba_client``.
"""
