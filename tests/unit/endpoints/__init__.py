"""Unit tests for the production ``endpoints`` package.

Tests in this package mirror ``endpoints/*.py`` one-to-one using the
``recording_client`` fixture factory from ``tests/conftest.py`` and the
NBA Stats API sample payload fixtures (e.g. ``sample_single_table_payload``,
``sample_schedule_payload``). Every test exercises a single endpoint wrapper
and verifies:

* the correct upstream endpoint string is passed to ``NBAClient.get``,
* the required query parameters (``Season``, ``SeasonType``, ``LeagueID``,
  ``PerMode``, ``MeasureType``, domain-specific keys) are present,
* ``**kwargs`` from the caller are merged into the outbound params dict,
* the wrapper returns the raw dict produced by ``NBAClient.get`` without
  transformation (endpoint layer is a thin parameter-marshalling shim).

No test in this package imports ``requests`` directly; all HTTP interaction
is mediated by the in-memory ``RecordingClient`` double, preserving Rule 1
(single HTTP client) even inside the test suite.
"""
