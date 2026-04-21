# Traceability Matrix — NBA Data Ingestion Pipeline

This document provides a bidirectional traceability matrix linking every feature, operational rule, and validation gate defined for the NBA Data Ingestion Pipeline to its implementing source files and verifying test files. It satisfies the Explainability rule (AAP §0.7.3.3) with 100% coverage.

**Scope:** features F-001 through F-013, operational rules 1 through 8, and validation gates 1, 2, 8, 9, 10, 12, 13.

**Authority:** [`New_Product_Prompt_20260418.md`](./New_Product_Prompt_20260418.md) is the authoritative source for features, rules, and gates. The Agent Action Plan section 0.7.5 provides the core rule-to-gate mapping used to seed this document. Any divergence between this matrix and the product brief is a defect in this matrix; the brief wins.

**Companion documents:**

- [`DECISIONS.md`](./DECISIONS.md) — records the "why" behind every non-obvious implementation decision (Explainability rule, decision-log half).
- [`ONBOARDING.md`](./ONBOARDING.md) — zero-to-running developer guide (Onboarding rule).
- [`OBSERVABILITY.md`](./OBSERVABILITY.md) — structured logging, correlation IDs, metrics, health, and readiness surface (Observability rule).

**Last updated:** 2026-04-19

---

## How to Use This Matrix

- **Adding a feature:** append a row to the Feature → Implementation table (Section 3) with the new feature's ID, description, and the files that will implement and verify it. Cross-reference the affected rules and gates. Add the new file paths to the Reverse Index (Section 6) under the appropriate architectural layer.
- **Auditing a rule:** the Rule → Implementation table (Section 4) reveals every source file and test file involved in enforcing that rule. Grep-based invariants live under `tests/invariants/`.
- **Reviewing a gate:** the Validation Gate → Verification table (Section 5) lists the specific test files that must pass for a gate to be considered satisfied.
- **Impact analysis:** before editing a source file, consult Section 6 (File → Requirements) to see which features and rules it implements — change at your own risk if untested.
- **Running coverage audits:** Section 7 lists exact `grep` commands that confirm 100% coverage has not regressed. These are the shell equivalent of the invariants the matrix enforces.

---

## Feature → Implementation Matrix

Every feature F-001 through F-013 is listed. Operational rules and validation gates applicable to the feature are cross-referenced. Every "Implementing File(s)" cell points to files already present in the repository plan (AAP §0.5.1); every "Verifying Test(s)" cell points to files predicted by AAP Group 8.

| Feature ID | Description | Operational Rules | Validation Gates | Implementing File(s) | Verifying Test(s) |
|---|---|---|---|---|---|
| F-001 | Click CLI entry point with subcommands `players`, `teams`, `games`, `lineups`, `schedule`, `all` (plus diagnostic `health`, `ready`, `metrics`) | — | Gate 9, Gate 13 | `run.py` | `tests/unit/test_cli.py` |
| F-002 | Central configuration module declaring all tunables | — | Gate 12 | `config.py` | `tests/unit/test_config.py` |
| F-003 | Single HTTP client with session-attached headers, rate-limit invocation, tenacity retry | Rule 1, Rule 2, Rule 3 | Gate 1, Gate 8 | `api/nba_client.py` | `tests/unit/api/test_nba_client.py`, `tests/invariants/test_rule1_sole_http_client.py` |
| F-004 | Declarative retry/backoff with exponential jitter on transient HTTP errors | Rule 2 | Gate 8 | `api/nba_client.py` (tenacity decorator), `config.py` (tunables) | `tests/unit/api/test_nba_client.py` |
| F-005 | Schema normalizer flattening `resultSets` to scalar-cell DataFrames | Rule 4 | Gate 1 | `utils/schema_normalizer.py` | `tests/unit/utils/test_schema_normalizer.py`, `tests/invariants/test_rule4_no_nested_cells.py` |
| F-006 | Pluggable writer: `BaseWriter` abstract + `CSVWriter` concrete | Rule 7 | Gate 1 | `storage/csv_writer.py` | `tests/unit/storage/test_csv_writer.py`, `tests/invariants/test_rule7_basewriter_only.py` |
| F-007 | Crash-safe checkpoint manager with JSON manifest | Rule 5 | Gate 8 | `utils/checkpoint.py`, `output/checkpoint.json` (runtime artifact) | `tests/unit/utils/test_checkpoint.py`, `tests/integration/test_gate8_games_resume.py` |
| F-008 | Stdlib-logging-only logger with correlation IDs, stdout + rotating file handler | — | Gate 2 | `utils/logger.py`, `utils/correlation.py` | `tests/unit/utils/test_logger.py` |
| F-009 | Players pipeline; emits `players.csv` and `player_tracking.csv`. Pipeline scope in this phase: 2 of 5 Players endpoints invoked (`fetch_leaguedashplayerstats`, `fetch_leaguedashptstats`) per [D-021](./DECISIONS.md#d-021-intentionally-limit-pipelinesingest_playerspy-to-fetch_leaguedashplayerstats-and-fetch_leaguedashptstats-in-this-phase); `endpoints/players.py` exposes all 5 wrappers for future-phase expansion | Rule 4, Rule 5, Rule 7 | Gate 1 | `pipelines/ingest_players.py`, `endpoints/players.py` | `tests/unit/pipelines/test_ingest_players.py`, `tests/unit/endpoints/test_players.py` |
| F-010 | Teams pipeline; emits `teams.csv`. Pipeline scope in this phase: 1 of 3 Teams endpoints invoked (`fetch_leaguedashteamstats`) per [D-020](./DECISIONS.md#d-020-intentionally-limit-pipelinesingest_teamspy-to-fetch_leaguedashteamstats-in-this-phase); `endpoints/teams.py` exposes all 3 wrappers for future-phase expansion | Rule 4, Rule 5, Rule 7 | Gate 1 | `pipelines/ingest_teams.py`, `endpoints/teams.py` | `tests/unit/pipelines/test_ingest_teams.py`, `tests/unit/endpoints/test_teams.py` |
| F-011 | Games pipeline with fail-safe per-game iteration; emits `games.csv` and `play_by_play.csv` from 4 endpoints | Rule 4, Rule 5, Rule 6, Rule 7 | Gate 1, Gate 8 | `pipelines/ingest_games.py`, `endpoints/games.py` | `tests/unit/pipelines/test_ingest_games.py`, `tests/unit/endpoints/test_games.py`, `tests/integration/test_gate8_games_resume.py` |
| F-012 | Lineups pipeline; emits `lineups.csv`. Pipeline scope in this phase: 1 of 2 Lineups endpoints invoked (`fetch_leaguedashlineups`) per [D-022](./DECISIONS.md#d-022-intentionally-limit-pipelinesingest_lineupspy-to-fetch_leaguedashlineups-in-this-phase); `endpoints/lineups.py` exposes both wrappers for future-phase expansion | Rule 4, Rule 5, Rule 7 | Gate 1 | `pipelines/ingest_lineups.py`, `endpoints/lineups.py` | `tests/unit/pipelines/test_ingest_lineups.py`, `tests/unit/endpoints/test_lineups.py` |
| F-013 | Schedule pipeline; emits `schedule.csv` and exposes `GAME_ID` enumeration consumed by F-011 | Rule 4, Rule 5, Rule 7 | Gate 1 | `pipelines/ingest_schedule.py`, `endpoints/schedule.py` | `tests/unit/pipelines/test_ingest_schedule.py`, `tests/unit/endpoints/test_schedule.py` |

**Notes on the Feature table:**

- F-007's "Implementing File(s)" cell lists `output/checkpoint.json` even though it is a runtime artifact rather than a source file; it is cited because it is the persistence target that makes Rule 5 (checkpoint-after-every-pull) verifiable at rest.
- F-011's row lists the integration test `tests/integration/test_gate8_games_resume.py` as an additional verifier because Gate 8's "resume determinism" clause is a feature property of the Games pipeline (F-011) expressed end-to-end rather than a unit invariant.
- F-013 has a cross-dependency on F-011 via `endpoints/schedule.enumerate_game_ids` — the Games pipeline cannot enumerate game ids without the Schedule endpoint. This cross-dependency is traced explicitly in Section 6 (File → Requirements) under `pipelines/ingest_games.py` and `endpoints/schedule.py`.
- Features F-009 through F-013 all share the same triad of operational rules (Rule 4 for flat CSV, Rule 5 for checkpointing, Rule 7 for writer-only CSV emission). Rule 6 is scoped uniquely to F-011 per AAP §0.7.2.6.
- **Intentional pipeline-scope reduction in F-009, F-010, F-012:** while the respective `endpoints/*.py` wrappers expose every endpoint listed in AAP §0.1.3, three pipelines intentionally invoke a subset in this phase: F-010 Teams invokes 1 of 3, F-009 Players invokes 2 of 5, and F-012 Lineups invokes 1 of 2. The rationale for each reduction (output-schema incompatibility, per-player fan-out cost against the 1.0-second rate limit, preservation of the declared one-CSV-per-domain output contract in AAP §0.2.3) is recorded in [D-020](./DECISIONS.md#d-020-intentionally-limit-pipelinesingest_teamspy-to-fetch_leaguedashteamstats-in-this-phase), [D-021](./DECISIONS.md#d-021-intentionally-limit-pipelinesingest_playerspy-to-fetch_leaguedashplayerstats-and-fetch_leaguedashptstats-in-this-phase), and [D-022](./DECISIONS.md#d-022-intentionally-limit-pipelinesingest_lineupspy-to-fetch_leaguedashlineups-in-this-phase) respectively. Each decision lists the future-phase expansion path. Negative-space unit tests (`tests/unit/pipelines/test_ingest_{teams,players,lineups}.py`) assert the non-invoked wrappers are NOT called so accidental scope creep is caught.
- **Metric label contract reference:** the observability metric names and label keys referenced across the Implementing / Verifying cells (e.g., `pipeline_runs_total`, `pipeline_rows_written_total`, `games_failed_total`) follow the canonical schema defined in [`OBSERVABILITY.md`](./OBSERVABILITY.md) and [`docs/dashboards/`](./dashboards/); this schema is formally established as the authoritative source of truth in [D-023](./DECISIONS.md#d-023-treat-docsobservabilitymd-and-docsdashboards-as-the-single-source-of-truth-for-metric-label-keys-and-values).

**Supporting files shared across features:**

- `utils/rate_limiter.py` — consumed by `api/nba_client.py` on behalf of F-003 / Rule 2.
- `utils/metrics.py` — consumed by every pipeline and by `api/nba_client.py`; implements Observability-rule counters (`nba_requests_total`, `nba_retries_total`, `pipeline_rows_written_total`, `games_failed_total`, etc.).
- `utils/health.py` — consumed by `run.py health` and `run.py ready` subcommands.
- `utils/correlation.py` — consumed by `run.py`, `utils/logger.py`, and `api/nba_client.py`; propagates the per-invocation UUID4 correlation id via `contextvars.ContextVar`.

**Shared test fixtures:** `tests/conftest.py` provides the `resultSets` payloads, mocked `requests.get`, monkeypatched `time.sleep`, `tmp_path`-rooted config overrides, and `CliRunner` factories used by every test module above.

---

## Rule → Implementation Matrix

Every rule (1 through 8) is listed. Rules 1–7 are the operational rules enumerated in `docs/New_Product_Prompt_20260418.md` §5. Rule 8 is the authority-boundary constraint inherited from §1 of the product brief; its interpretation is recorded in [`DECISIONS.md`](./DECISIONS.md) entry **D-001**.

| Rule | Binding Constraint | Enforcing File(s) | Verifying Test(s) | Related Gate(s) |
|---|---|---|---|---|
| Rule 1 | Only `api/nba_client.py` may call `requests.get`, `requests.post`, or `requests.Session` | `api/nba_client.py` | `tests/invariants/test_rule1_sole_http_client.py` | Gate 1, Gate 8 |
| Rule 2 | ≥ 1.0s between any two outbound requests | `utils/rate_limiter.py`, `api/nba_client.py`, `config.RATE_LIMIT_SECONDS` | `tests/unit/utils/test_rate_limiter.py`, `tests/integration/test_gate8_games_resume.py` (zero-429 clause) | Gate 8 |
| Rule 3 | Every request carries `Referer: https://stats.nba.com` and a browser-like `User-Agent` | `config.REQUIRED_HEADERS`, `api/nba_client.py` | `tests/unit/api/test_nba_client.py` | Gate 1 |
| Rule 4 | No CSV cell may contain a `dict` or `list` | `utils/schema_normalizer.py` | `tests/invariants/test_rule4_no_nested_cells.py`, `tests/unit/utils/test_schema_normalizer.py` | Gate 1 |
| Rule 5 | Checkpoint persisted immediately after every successful pull | `utils/checkpoint.py`, every `pipelines/ingest_*.py` | `tests/unit/utils/test_checkpoint.py`, every `tests/unit/pipelines/test_ingest_*.py`, `tests/integration/test_gate8_games_resume.py` | Gate 8 |
| Rule 6 | Fail-safe per-`GAME_ID` iteration in the Games pipeline only | `pipelines/ingest_games.py` | `tests/unit/pipelines/test_ingest_games.py` | Gate 8 |
| Rule 7 | Only `storage/csv_writer.py::CSVWriter.write` may call `DataFrame.to_csv` | `storage/csv_writer.py` | `tests/invariants/test_rule7_basewriter_only.py` | Gate 1 |
| Rule 8 | Authority boundary: no database, no web UI, no auth, no streaming, no CI/CD in this phase | Enforced by omission across the codebase | Manual negative-space review; traced in [`DECISIONS.md`](./DECISIONS.md) entry **D-001** ("Eight-Rules Interpretation") | — |

**Notes on the Rule table:**

- Rule 1 and Rule 7 are enforced by grep-based invariant tests rather than unit tests. The grep assertion runs against the production tree (`endpoints/`, `pipelines/`, `storage/`, `utils/`, `run.py`, `config.py`) and asserts zero matches. See AAP §0.7.2.1 and §0.7.2.7 for the exact grep patterns.
- Rule 5 is verified at three layers: a unit test on the `CheckpointManager` itself, a unit test per pipeline verifying `mark_completed` is called after every successful write, and an integration test that interrupts and resumes a run to confirm deterministic output.
- Rule 6 is the *only* place in the codebase where a bare `except Exception` is permitted (AAP §0.5.2.1). Any addition of `except Exception` outside `pipelines/ingest_games.py` is a defect.
- Rule 8 is not verified by any automated test — the authority boundary cannot be enforced by presence, only by absence. The traceability here points at [`DECISIONS.md`](./DECISIONS.md) as the formal acknowledgment. If a future phase introduces a database writer or web surface, this row's "Enforcing File(s)" cell will read differently, and D-001 will need to be superseded.

---

## Validation Gate → Verification Matrix

Every validation gate enumerated in `docs/New_Product_Prompt_20260418.md` §6 that carries a test obligation is listed. Gates 1, 2, 8, 9, 10, 12, and 13 are in scope for this deliverable (AAP §0.1.1). The product brief skips Gate numbers 3 through 7 and 11 — those gate numbers are reserved for future phases and are out of scope here.

| Gate | Pass Criterion | Primary Verification File(s) | Supporting File(s) |
|---|---|---|---|
| Gate 1 | `python run.py all --season 2025-26` produces non-empty, fully flattened CSVs under `output/` | `tests/integration/test_gate1_all_live.py` | All seven `output/*.csv` artifacts (runtime), every pipeline module, `utils/schema_normalizer.py`, `storage/csv_writer.py` |
| Gate 2 | `python -m py_compile **/*.py` zero warnings AND `flake8` clean | `.flake8` (config), `pytest.ini` (warning filters), every `.py` file | `requirements.txt` (pinned dev deps) |
| Gate 8 | Live games smoke with zero HTTP 429 responses AND deterministic resume after interruption | `tests/integration/test_gate8_games_resume.py` | `utils/rate_limiter.py`, `utils/checkpoint.py`, `api/nba_client.py`, `pipelines/ingest_games.py` |
| Gate 9 | Every endpoint / pipeline is reachable from a `run.py` subcommand | `tests/unit/test_cli.py` | `run.py`, every `pipelines/ingest_*.py` |
| Gate 10 | `python -m pytest tests/` exits 0 | `pytest.ini` | Every file under `tests/` |
| Gate 12 | Every field declared in `config.py` has at least one production read-site | `tests/unit/test_config.py` | `config.py`, every consumer (`api/nba_client.py`, `utils/logger.py`, `utils/rate_limiter.py`, `utils/checkpoint.py`, `storage/csv_writer.py`, pipelines) |
| Gate 13 | Every CLI subcommand dispatches to its corresponding pipeline | `tests/unit/test_cli.py` | `run.py`, every `pipelines/ingest_*.py` |

**Notes on the Gate table:**

- Gate 1 and Gate 8 require live network access to `https://stats.nba.com/`. Their pytest files carry the `@pytest.mark.integration` marker and can be skipped offline via `pytest -m "not integration"` (product brief §6 Gate 10).
- Gate 2 is enforced by two independent tools: `python -m py_compile` for syntax/compile warnings, and `flake8` for style/lint. The `.flake8` configuration sets `max-line-length = 120` per AAP §0.5.1.1 to avoid false positives on the narrative-heavy tables in this and other documentation files.
- Gate 12 (config propagation) is verified by a test that iterates every public attribute of `config` and asserts it is imported by at least one module under `api/`, `endpoints/`, `pipelines/`, `storage/`, `utils/`, or `run.py`. The iteration is performed with `dir(config)` + `ast.walk` over the consumer files.
- Gate 13 is the companion to Gate 9. Gate 9 asks "is every pipeline reachable from `run.py`?"; Gate 13 asks "does every CLI subcommand actually call its pipeline?". Both are verified by `tests/unit/test_cli.py` using `click.testing.CliRunner` with mocked pipeline functions.

---

## Reverse Index: File → Requirements

This section is the bottom-up view. For every file in the repository plan, it lists the features, rules, and gates the file participates in. It enables impact analysis before a refactor: look up the file you want to change, then cross-reference the feature and rule rows above to see every test that must still pass.

### CLI Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `run.py` | F-001 | — | Gate 9, Gate 13 |

### Configuration Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `config.py` | F-002 | Rule 2 (declares `RATE_LIMIT_SECONDS`), Rule 3 (declares `REQUIRED_HEADERS`) | Gate 12 |

### Transport Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `api/__init__.py` | F-003 | — | — |
| `api/nba_client.py` | F-003, F-004 | Rule 1, Rule 2, Rule 3 | Gate 1, Gate 8 |

### Endpoint Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `endpoints/__init__.py` | F-009 … F-013 | — | — |
| `endpoints/players.py` | F-009 | — | Gate 1 |
| `endpoints/teams.py` | F-010 | — | Gate 1 |
| `endpoints/games.py` | F-011 | — | Gate 1 |
| `endpoints/lineups.py` | F-012 | — | Gate 1 |
| `endpoints/schedule.py` | F-013 (and F-011 via `enumerate_game_ids`) | — | Gate 1 |

### Pipeline Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `pipelines/__init__.py` | F-009 … F-013 | — | — |
| `pipelines/ingest_players.py` | F-009 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `pipelines/ingest_teams.py` | F-010 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `pipelines/ingest_games.py` | F-011 (consumes F-013) | Rule 4, Rule 5, Rule 6, Rule 7 | Gate 1, Gate 8 |
| `pipelines/ingest_lineups.py` | F-012 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `pipelines/ingest_schedule.py` | F-013 | Rule 4, Rule 5, Rule 7 | Gate 1 |

### Storage Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `storage/__init__.py` | F-006 | — | — |
| `storage/csv_writer.py` | F-006 | Rule 7 | Gate 1 |

### Utilities Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `utils/__init__.py` | — | — | — |
| `utils/rate_limiter.py` | F-004 | Rule 2 | Gate 8 |
| `utils/schema_normalizer.py` | F-005 | Rule 4 | Gate 1 |
| `utils/checkpoint.py` | F-007 | Rule 5 | Gate 8 |
| `utils/logger.py` | F-008 | — | Gate 2 |
| `utils/correlation.py` | F-008 (Observability extension) | — | — |
| `utils/metrics.py` | Observability extension | — | — |
| `utils/health.py` | Observability extension | — | — |

### Test Layer

| File | Features | Rules | Gates |
|---|---|---|---|
| `tests/conftest.py` | all (shared fixtures) | all (shared mocks) | Gate 10 |
| `tests/unit/test_cli.py` | F-001 | — | Gate 9, Gate 13 |
| `tests/unit/test_config.py` | F-002 | — | Gate 12 |
| `tests/unit/api/test_nba_client.py` | F-003, F-004 | Rule 1, Rule 2, Rule 3 | Gate 1, Gate 8 |
| `tests/unit/endpoints/test_players.py` | F-009 | — | Gate 1, Gate 9 |
| `tests/unit/endpoints/test_teams.py` | F-010 | — | Gate 1, Gate 9 |
| `tests/unit/endpoints/test_games.py` | F-011 | — | Gate 1, Gate 9 |
| `tests/unit/endpoints/test_lineups.py` | F-012 | — | Gate 1, Gate 9 |
| `tests/unit/endpoints/test_schedule.py` | F-013 | — | Gate 1, Gate 9 |
| `tests/unit/pipelines/test_ingest_players.py` | F-009 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `tests/unit/pipelines/test_ingest_teams.py` | F-010 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `tests/unit/pipelines/test_ingest_games.py` | F-011 | Rule 4, Rule 5, Rule 6, Rule 7 | Gate 1 |
| `tests/unit/pipelines/test_ingest_lineups.py` | F-012 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `tests/unit/pipelines/test_ingest_schedule.py` | F-013 | Rule 4, Rule 5, Rule 7 | Gate 1 |
| `tests/unit/storage/test_csv_writer.py` | F-006 | Rule 7 | Gate 1 |
| `tests/unit/utils/test_rate_limiter.py` | F-004 | Rule 2 | Gate 8 |
| `tests/unit/utils/test_schema_normalizer.py` | F-005 | Rule 4 | Gate 1 |
| `tests/unit/utils/test_checkpoint.py` | F-007 | Rule 5 | Gate 8 |
| `tests/unit/utils/test_logger.py` | F-008 | — | Gate 2 |
| `tests/unit/utils/test_metrics.py` | Observability | — | — |
| `tests/unit/utils/test_health.py` | Observability | — | — |
| `tests/integration/test_gate1_all_live.py` | F-009 … F-013 | Rule 1, Rule 2, Rule 3, Rule 4, Rule 5, Rule 7 | Gate 1 |
| `tests/integration/test_gate8_games_resume.py` | F-011 | Rule 2, Rule 5, Rule 6 | Gate 8 |
| `tests/invariants/test_rule1_sole_http_client.py` | — | Rule 1 | Gate 1, Gate 8 |
| `tests/invariants/test_rule4_no_nested_cells.py` | F-005 | Rule 4 | Gate 1 |
| `tests/invariants/test_rule7_basewriter_only.py` | F-006 | Rule 7 | Gate 1 |

### Configuration Files

| File | Purpose | Traceability Target |
|---|---|---|
| `requirements.txt` | Runtime and dev dependency pins | Gate 2 (zero-warning lint/build requires pinned `flake8`); Gate 10 (pinned `pytest`) |
| `pytest.ini` | Registers `integration` marker and default warning filters | Gate 2, Gate 10 |
| `.flake8` | Default lint rules, `max-line-length = 120` | Gate 2 |
| `.gitignore` | Excludes `output/`, `logs/`, `__pycache__/`, `.pytest_cache/`, venv directories | Runtime artifact hygiene |
| `.env.example` | Documents operator-settable overrides (e.g., `OUTPUT_DIR`, `LOG_LEVEL`, `RATE_LIMIT_SECONDS`) | F-002, Onboarding rule |

### Documentation Layer

| File | Purpose | Traceability Target |
|---|---|---|
| `README.md` | Entry-point narrative + Getting Started | All features, Onboarding rule |
| `docs/ONBOARDING.md` | Clean-machine onboarding guide | Onboarding rule |
| `docs/OBSERVABILITY.md` | Log format, correlation ID, metrics, health | F-008, Observability rule |
| `docs/DECISIONS.md` | Decision log with alternatives + rationale | Explainability rule |
| `docs/TRACEABILITY.md` | This document | Explainability rule |
| `docs/executive-summary.html` | reveal.js executive deck | Executive Presentation rule |
| `docs/api/endpoints_catalog.md` | Per-endpoint reference for 15+ endpoints | F-009 … F-013 |
| `docs/features/players.md` | F-009 deep dive | F-009 |
| `docs/features/teams.md` | F-010 deep dive | F-010 |
| `docs/features/games.md` | F-011 deep dive | F-011, Rule 6 |
| `docs/features/lineups.md` | F-012 deep dive | F-012 |
| `docs/features/schedule.md` | F-013 deep dive | F-013, F-011 cross-dep |
| `docs/dashboards/operator_dashboard.json` | Grafana dashboard | Observability rule |
| `docs/dashboards/operator_dashboard.md` | Markdown operator dashboard | Observability rule |

### Runtime Artifacts (generated; not committed)

| File | Purpose | Traceability Target |
|---|---|---|
| `output/players.csv` | F-009 primary artifact | F-009, Gate 1 |
| `output/player_tracking.csv` | F-009 tracking artifact | F-009, Gate 1 |
| `output/teams.csv` | F-010 primary artifact | F-010, Gate 1 |
| `output/games.csv` | F-011 box-score artifact | F-011, Gate 1 |
| `output/play_by_play.csv` | F-011 play-by-play artifact | F-011, Gate 1 |
| `output/lineups.csv` | F-012 primary artifact | F-012, Gate 1 |
| `output/schedule.csv` | F-013 primary artifact | F-013, Gate 1 |
| `output/checkpoint.json` | F-007 manifest | F-007, Rule 5, Gate 8 |
| `logs/pipeline.log` (+ rotations) | Stdlib logger sink | F-008, Gate 2 |

---

## Coverage Verification Checklist

Run these greps from the repository root to confirm 100% coverage has not regressed. If any command returns an unexpected count, this document has drifted from the codebase and must be updated.

- `grep -cE '^\| F-0(0[1-9]|1[0-3]) ' docs/TRACEABILITY.md` should return `13` — one row per feature in Section 3.
- `grep -cE '^\| Rule [1-8] ' docs/TRACEABILITY.md` should return `8` — one row per rule in Section 4.
- `grep -cE '^\| Gate (1|2|8|9|10|12|13) ' docs/TRACEABILITY.md` should return `7` — one row per gate in Section 5.
- No row in Sections 3, 4, or 5 contains the placeholder `TBD`, `N/A`, or `-` in its test column (Rule 8 is the single exception; its verification is manual review, pointed at [`DECISIONS.md`](./DECISIONS.md)).
- Every file path in this document either (a) matches an AAP §0.5.1 or §0.6.1 entry, or (b) is a `tests/` path predicted by Group 8. Run `grep -oE '(api|endpoints|pipelines|storage|utils|tests)/[A-Za-z0-9_/*]+\.py' docs/TRACEABILITY.md | sort -u` to produce the authoritative file inventory for audit.
- Every CSV artifact (`players.csv`, `teams.csv`, `games.csv`, `play_by_play.csv`, `lineups.csv`, `schedule.csv`, `player_tracking.csv`) is traceable via its pipeline row in Section 3 and via its entry in the Runtime Artifacts table in Section 6.
- Every cross-reference link (`./New_Product_Prompt_20260418.md`, `./DECISIONS.md`, `./ONBOARDING.md`, `./OBSERVABILITY.md`) resolves when the file is viewed on GitHub or in any Markdown previewer.

### Coverage Summary

| Dimension | Expected Count | Matrix Section |
|---|---|---|
| Features (F-001 … F-013) | 13 | Section 3 |
| Operational rules (1 … 8) | 8 | Section 4 |
| Validation gates (1, 2, 8, 9, 10, 12, 13) | 7 | Section 5 |
| Source files in Reverse Index | 26 (CLI 1 + Config 1 + Transport 2 + Endpoints 6 + Pipelines 6 + Storage 2 + Utilities 8 — `__init__.py` included) | Section 6 |
| Test files in Reverse Index | 26 (unit 20 + integration 2 + invariants 3 + `conftest.py` 1 — `__init__.py` package markers omitted) | Section 6 |
| CSV artifacts | 7 | Section 6 Runtime Artifacts |

If these counts drift without a corresponding update to this document, the matrix has decayed and Gate 10 must fail until restored.
