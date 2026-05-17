"""Live NBA Stats API integration tests.

Every test in this package is decorated at module scope with
``pytestmark = pytest.mark.integration`` and is therefore excluded by
default via ``pytest -m "not integration"``. To include, run
``pytest -m integration`` or ``pytest tests/integration``.

Satisfies:

* Validation Gate 1 — ``python run.py all --season 2025-26`` produces
  non-empty CSV files in ``output/`` (``test_gate1_all_live.py``). See
  ``docs/New_Product_Prompt_20260418.md`` §6 Gate 1.
* Validation Gate 8 — Live ``games`` smoke + checkpoint interrupt /
  resume determinism + zero HTTP 429s
  (``test_gate8_games_resume.py``). See
  ``docs/New_Product_Prompt_20260418.md`` §6 Gate 8.

Both tests exercise the live upstream at ``https://stats.nba.com/stats/``
and are skipped automatically when the host is unreachable, preserving
``pytest`` exit 0 on offline machines (Gate 10).
"""
