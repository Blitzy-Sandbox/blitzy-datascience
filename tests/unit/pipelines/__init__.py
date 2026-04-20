"""Unit tests for :mod:`pipelines` — the five ingest-pipeline orchestrators.

This package contains one test module per production pipeline module:

* :mod:`tests.unit.pipelines.test_ingest_schedule`  — Feature F-013 (schedule pipeline)
* :mod:`tests.unit.pipelines.test_ingest_teams`     — Feature F-010 (teams pipeline)
* :mod:`tests.unit.pipelines.test_ingest_lineups`   — Feature F-012 (lineups pipeline)
* :mod:`tests.unit.pipelines.test_ingest_players`   — Feature F-009 (players pipeline)
* :mod:`tests.unit.pipelines.test_ingest_games`     — Feature F-011 (games pipeline) and the
  Rule 6 fail-safe iteration canary (AAP §0.7.2.6)

All fixtures live in the top-level :mod:`tests.conftest`; no package-local fixtures are
required for this tree.
"""
