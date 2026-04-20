# Onboarding — NBA Data Ingestion Pipeline

Welcome. This guide takes you from a clean laptop to a running, modifiable NBA Data Ingestion Pipeline in under 15 minutes. It is the answer to every question a new developer typically asks on day one.

**Designed so you never have to ask a question to get unblocked.** Every prerequisite is enumerated, every command is copy-paste ready, every pitfall is called out.

**Authority:** the authoritative product brief is [`New_Product_Prompt_20260418.md`](./New_Product_Prompt_20260418.md). The decision log ([`DECISIONS.md`](./DECISIONS.md)) explains the "why" behind every non-obvious choice. The traceability matrix ([`TRACEABILITY.md`](./TRACEABILITY.md)) maps features and rules to implementing files. The observability guide ([`OBSERVABILITY.md`](./OBSERVABILITY.md)) explains the logging, metrics, health, and readiness surface.

**Time commitment:**

- 5 minutes — setup.
- 2 minutes — first smoke tests.
- 3–10 minutes — first live pipeline run (depends on NBA API latency).
- 5 minutes — domain orientation.

---

## Prerequisites

| Tool | Minimum Version | Why |
|---|---|---|
| Python | 3.11 | Language runtime; `contextvars` and dataclass syntax used |
| pip | 23.0+ | Modern resolver + `--upgrade-strategy eager` |
| Git | 2.x | Repository operations |
| A terminal | any | The entire workflow is CLI-driven |
| Internet access to `pypi.org` | — | Dependency installation |
| Internet access to `https://stats.nba.com` | — | Live data ingestion (Gate 1 / Gate 8) |

**What you do NOT need:**

- Docker.
- Node.js.
- A database server.
- Cloud credentials.
- An IDE — any text editor works; VS Code is suggested.

If you're missing Python 3.11+, see the platform-specific installation commands in the next section.

---

## Clean-Machine Setup

Pick the section that matches your operating system. Every command is copy-paste operable; nothing is templated with `<YOUR_PATH>` placeholders.

### macOS (Homebrew)

```bash
# 1. Install Python 3.12 (3.11+ is acceptable)
brew install python@3.12

# 2. Clone and enter the repository (skip if already cloned)
git clone https://github.com/Blitzy-Sandbox/blitzy-datascience.git
cd blitzy-datascience

# 3. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 4. Upgrade pip
pip install --upgrade pip

# 5. Install runtime + development dependencies
pip install -r requirements.txt
```

### Ubuntu / Debian

```bash
# 1. Install Python 3.12
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip git

# 2. Clone and enter the repository
git clone https://github.com/Blitzy-Sandbox/blitzy-datascience.git
cd blitzy-datascience

# 3. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 4. Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
# 1. Install Python from python.org (check "Add Python to PATH")
#    or via winget:
winget install Python.Python.3.12

# 2. Clone and enter the repository
git clone https://github.com/Blitzy-Sandbox/blitzy-datascience.git
cd blitzy-datascience

# 3. Create and activate a virtual environment
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 4. Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

If `.\.venv\Scripts\Activate.ps1` fails with an execution-policy error, run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` once, then retry.

### Cross-platform with `pyenv` or `uv`

If you prefer `pyenv`:

```bash
pyenv install 3.12.3
pyenv local 3.12.3
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you prefer `uv` (fast package manager, optional):

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

---

## First-Run Smoke Tests

Immediately after the install steps above, run these three commands in order. Any failure here indicates an environment problem that must be resolved before proceeding.

```bash
# 1. Validate that Python can import every module
python -c "import config, run; print('imports OK')"

# 2. Validate the environment is ready to run a pipeline
python run.py ready

# 3. Run the non-live unit + invariant test suite
python -m pytest -m "not integration"
```

Expected results:

- Step 1 prints `imports OK`.
- Step 2 prints a JSON document with `"status": "ready"` and all checks `true`.
- Step 3 exits with `0` and "N passed" where N is the test count (≥ 20).

If step 2 reports `"status": "not_ready"`, read the `errors` array and fix each item (typically a `logs/` or `output/` directory permission issue).

If any of these three commands fail, stop here and consult the [Common Pitfalls](#common-pitfalls) section below before proceeding — do not run a live pipeline with a broken environment.

---

## Your First Pipeline Run

With the environment ready, run a small domain end-to-end. Start with schedule — it makes one API call and writes one CSV:

```bash
python run.py schedule --season 2025-26
```

You should see a stream of INFO log lines followed by a line like:

```
2026-04-19 12:03:46,892 INFO  corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 csv_writer wrote schedule.csv rows=1312
```

Inspect the output:

```bash
ls -la output/
head -5 output/schedule.csv
cat output/checkpoint.json | python -m json.tool | head -20
```

Now run the full pipeline (this is Gate 1):

```bash
python run.py all --season 2025-26
```

This takes longer — the Games pipeline alone can make 1000+ calls, and the ≥ 1.0 second inter-request floor means minimum pipeline runtime is bounded below by request count. Watch the logs in real time:

```bash
# In another terminal:
tail -F logs/pipeline.log
```

When it completes, you will have seven CSV files in `output/` plus the checkpoint manifest.

If the run is interrupted (Ctrl-C, network drop, laptop sleep), simply re-run the same command. The checkpoint manifest (`output/checkpoint.json`) ensures resumption from the last successful pull without duplicating work — this is Rule 5 in action.

---

## Architecture at a Glance

Before the domain deep-dive, orient yourself with the layered module map. The flow of control runs top-to-bottom; each arrow represents an import edge.

```
run.py (click CLI)
  │
  ├─▶ pipelines/ingest_schedule.py  ─┐
  ├─▶ pipelines/ingest_games.py     ─┤
  ├─▶ pipelines/ingest_teams.py     ─┼─▶ endpoints/*.py ─▶ api/nba_client.py ─▶ NBA Stats API
  ├─▶ pipelines/ingest_players.py   ─┤                     (Rule 1: sole HTTP client)
  └─▶ pipelines/ingest_lineups.py   ─┘                     (Rule 2: ≥1.0s floor)
                                                           (Rule 3: Referer + UA)
  shared collaborators (injected at the CLI entry point):
    utils/rate_limiter.py       — enforces the inter-request floor
    utils/schema_normalizer.py  — flattens resultSets → DataFrame (Rule 4)
    utils/checkpoint.py         — JSON manifest, atomic writes (Rule 5)
    utils/logger.py             — stdout + rotating file + correlation IDs
    utils/correlation.py        — contextvars-backed UUID for every run
    utils/metrics.py            — counters + Prometheus text exposition
    utils/health.py             — health/readiness probes
    storage/csv_writer.py       — sole to_csv call site (Rule 7)
    config.py                   — all tunables; env-var overrides
```

Three reading orders through the codebase all work:

- **Top-down (CLI-first):** `run.py` → a `pipelines/ingest_*.py` → `endpoints/*.py` → `api/nba_client.py`. Best if you want to follow a single `--season 2025-26` invocation end-to-end.
- **Bottom-up (transport-first):** `config.py` → `utils/*` → `api/nba_client.py` → `endpoints/*` → `pipelines/*` → `run.py`. Best if you want to build mental scaffolding in dependency order (this is also the construction order used to build the project originally).
- **Rule-first:** pick a rule in `docs/DECISIONS.md` and read only the files it names. Best if you're debugging a specific invariant.

The six integration invariants (Rules 1–7, plus the authority boundary at Rule 8) are all surfaced by automated tests under `tests/invariants/` so you cannot accidentally break them.

---

## Domain Context

### The NBA Stats API

The pipeline consumes the NBA's public statistics API at `https://stats.nba.com/stats/`. Key properties:

- **Unauthenticated** — no API keys, no tokens, no OAuth.
- **Rate-limited** — the API is sensitive to aggressive clients; Rule 2 enforces a proactive ≥ 1.0 second inter-request floor.
- **Requires browser-like headers** — without `Referer: https://stats.nba.com` and a browser-style `User-Agent`, requests are rejected (Rule 3).
- **No SLA** — a public API with no uptime guarantee. Retries via `tenacity` absorb transient 429/5xx conditions.
- **Single-client contract** — Rule 1 forbids direct `requests.get` calls outside `api/nba_client.py`; every outbound HTTP call funnels through `NBAClient.get(endpoint, params)`.

### The `resultSets` envelope

Every NBA Stats API response is a JSON document with a top-level `resultSets` array. Each entry has three keys:

```json
{
  "resultSets": [
    {
      "name": "LeagueDashPlayerStats",
      "headers": ["PLAYER_ID", "PLAYER_NAME", "PTS", "REB", "..."],
      "rowSet": [
        [2544, "LeBron James", 27.3, 7.3, "..."],
        [201142, "Kevin Durant", 29.1, 6.6, "..."]
      ]
    }
  ]
}
```

Our `utils/schema_normalizer.py` flattens this into `pandas.DataFrame(rowSet, columns=headers)` for each entry, asserting that no cell ends up containing a dict or list (Rule 4). Some endpoints return multiple entries per call (e.g., `boxscoretraditionalv2` returns `PlayerStats` AND `TeamStats`); the normalizer returns a dict of `name → DataFrame` so the caller can select.

### Season string conventions

NBA seasons span two calendar years. The API consumes strings in the form `YYYY-YY`:

- `2025-26` — the 2025–2026 season (default in `config.DEFAULT_SEASON`).
- `2024-25` — the 2024–2025 season.
- `2023-24` — the 2023–2024 season.

Always pass `--season 2025-26`, never `2025-2026` or `2025`. The pipeline does not perform string coercion; a malformed season string will propagate to the API, which typically responds with an empty `rowSet` rather than an error — see Pitfall 3 below.

### Output artifacts

After a successful `all` run, you have the following in `output/`:

| File | Primary columns | Produced by |
|---|---|---|
| `schedule.csv` | `GAME_ID`, `GAME_DATE`, `TEAM_ID`, `MATCHUP` | `pipelines/ingest_schedule.py` |
| `games.csv` | `GAME_ID`, `PLAYER_ID`, `TEAM_ID` box score columns | `pipelines/ingest_games.py` |
| `play_by_play.csv` | `GAME_ID`, `EVENTNUM`, `EVENTMSGTYPE` | `pipelines/ingest_games.py` |
| `teams.csv` | `TEAM_ID`, team-season stat columns | `pipelines/ingest_teams.py` |
| `players.csv` | `PLAYER_ID`, player-season stat columns | `pipelines/ingest_players.py` |
| `player_tracking.csv` | `PLAYER_ID`, tracking metrics | `pipelines/ingest_players.py` |
| `lineups.csv` | `GROUP_ID`, five-man lineup stats | `pipelines/ingest_lineups.py` |

Plus `output/checkpoint.json` — the run manifest (Rule 5). Delete it to force a fresh run; the pipeline will re-pull everything.

All CSVs are UTF-8 encoded and written with `index=False` so there is no leading unnamed column. Cells are strictly scalar — Rule 4 forbids nested `dict` or `list` values — which means every file is instantly ingestible by pandas, DuckDB, SQLite, spreadsheets, or any BI tool that reads flat CSV.

---

## Common Pitfalls

### Pitfall 1: HTTP 429 floods

**Symptom:** log lines like `WARNING nba_client Retrying due to HTTP 429` appearing dozens of times per minute.

**Cause:** the ≥ 1.0s floor is too aggressive for the upstream's current throttling posture, OR another process on your machine is hitting `stats.nba.com`.

**Fix:** raise `config.RATE_LIMIT_SECONDS` to 1.5 or 2.0 and re-run. The checkpoint will resume from where you stopped. If the problem persists, inspect running processes for other ingestion tools or browser tabs open on the stats site.

### Pitfall 2: Checkpoint skips endpoints

**Symptom:** you expected a fresh run but the pipeline logs lines like `INFO ingest_teams skip: already completed`.

**Cause:** a prior run's checkpoint is still in `output/checkpoint.json`.

**Fix:** `rm output/checkpoint.json` to force a fresh run, or use `--force` once that flag is implemented. (Not implemented now — see [Suggested Next Tasks](#suggested-next-tasks).)

### Pitfall 3: "No data for season"

**Symptom:** a CSV emits zero rows despite no error.

**Cause:** the NBA Stats API returns empty `rowSet` arrays for seasons before or after the league's data window, or when the season parameter is malformed.

**Fix:** double-check `--season`. It must match `YYYY-YY`, e.g., `2025-26`, not `2025` or `2025-2026`. For very current seasons, verify that regular-season games have actually started; preseason `SeasonType` values require a different API parameter.

### Pitfall 4: `ImportError: No module named 'requests'`

**Symptom:** `python run.py` fails with an import error on third-party packages.

**Cause:** the virtualenv is not activated.

**Fix:** `source .venv/bin/activate` (macOS/Linux) or `.\.venv\Scripts\Activate.ps1` (Windows). Confirm activation by running `which python` (Unix) or `Get-Command python` (PowerShell) — it should point inside `.venv`. Your shell prompt should also show `(.venv)` as a prefix.

### Pitfall 5: Correlation IDs show `corr=-`

**Symptom:** log records have `corr=-` instead of a UUID.

**Cause:** a log call happened before `new_correlation_id()` executed (usually a module-level log at import time).

**Fix:** move the log call into a function body. This is a Rule 1 / Observability cleanliness issue — not a bug. The correlation ID context variable has a default empty string value, and the logger adapter substitutes `-` when the value is missing, so stray import-time log lines remain legible but unlinkable to a run.

### Pitfall 6: `TypeError: Can't instantiate abstract class BaseWriter`

**Symptom:** when writing a new storage backend you get the type error.

**Cause:** your subclass forgot to implement the abstract `write(df, name, season) -> Path` method.

**Fix:** implement the exact signature. See `storage/csv_writer.py::CSVWriter` as the reference implementation. The signature is non-negotiable because every pipeline invokes `writer.write(df, name, season)` with positional arguments in that exact order.

### Pitfall 7: `PermissionError: [Errno 13]` on `output/` or `logs/`

**Symptom:** the pipeline fails immediately with a permission error when creating the output or log directory.

**Cause:** the process does not have write permission to the project root (common on shared servers or restricted corporate laptops).

**Fix:** either `chmod u+w .` on the project root, or relocate the output via `export BLITZY_OUTPUT_DIR=$HOME/nba_output` before running. The `config.py` module picks up environment-variable overrides for `OUTPUT_DIR` and `LOG_FILE`; see `.env.example` for the full list.

### Pitfall 8: Pipeline hangs with no log output

**Symptom:** `python run.py all` prints nothing for minutes.

**Cause:** DNS resolution to `stats.nba.com` is stalled, or a corporate proxy is intercepting HTTPS without returning an error. `requests` uses system-level DNS and will block until the OS times out.

**Fix:** run `curl -I https://stats.nba.com/stats/leaguegamefinder` to verify raw connectivity. If that hangs, fix DNS / proxy first. On macOS a common fix is `scutil --dns` and then a network reset; on Linux, check `/etc/resolv.conf`.

---

## How to Extend the Project

The architecture is deliberately layered so that each extension pattern touches exactly the files it should — and no others.

### Add a new NBA Stats endpoint

1. Identify the domain (`players`, `teams`, `games`, `lineups`, `schedule`).
2. Add a wrapper function to `endpoints/<domain>.py`:

   ```python
   def fetch_<endpoint_name>(client, season, **kwargs):
       params = {"Season": season, "SeasonType": "Regular Season", **kwargs}
       return client.get("<endpoint_name>", params)
   ```

3. Update `pipelines/ingest_<domain>.py` to call the new wrapper, normalize the result, write via the writer, and checkpoint.
4. Add a unit test at `tests/unit/endpoints/test_<domain>.py` asserting the wrapper calls `client.get` with the correct endpoint name and params.
5. Add a row to `docs/api/endpoints_catalog.md`.
6. Update `docs/TRACEABILITY.md` with the endpoint's feature/rule/gate mapping if it materially changes coverage.

**What you must not do:** call `requests.get` directly inside the new wrapper. Rule 1 funnels every request through `NBAClient.get` and is enforced by an invariant test in `tests/invariants/test_rule1_sole_http_client.py`.

### Add a new writer (e.g., Parquet)

1. Create `storage/parquet_writer.py` with a class inheriting `BaseWriter`:

   ```python
   from storage.csv_writer import BaseWriter

   class ParquetWriter(BaseWriter):
       def write(self, df, name, season):
           path = self.output_dir / f"{name}.parquet"
           df.to_parquet(path, index=False)
           return path
   ```

2. Update `run.py` to accept a `--format csv|parquet` flag and select the writer accordingly.
3. Update `tests/unit/storage/test_parquet_writer.py`.
4. Add `pyarrow` to `requirements.txt` (Parquet dependency).
5. Record the addition in `docs/DECISIONS.md` using the template in its appendix.

**Important:** Rule 7 constrains `.to_csv()` to `storage/csv_writer.py`. A symmetric constraint (`.to_parquet()` only inside `storage/parquet_writer.py`) should be added as a new invariant test so future contributors cannot accidentally couple a pipeline to Parquet.

### Add a new pipeline

1. Create `pipelines/ingest_<domain>.py` following the shape of `pipelines/ingest_schedule.py` (the simplest reference).
2. Register a new subcommand in `run.py` that dispatches to `run()` on the pipeline.
3. Update the `all` subcommand's ordered dispatch list — consider dependency on other pipelines (e.g., Games depends on Schedule for `GAME_ID` enumeration) and insert appropriately.
4. Add `tests/unit/pipelines/test_ingest_<domain>.py`.
5. Update `docs/TRACEABILITY.md` and `docs/features/<domain>.md`.

**Rule 6 scope:** `except Exception:` is permitted **only** inside `pipelines/ingest_games.py` — never replicate that pattern in a new pipeline. All other pipelines propagate exceptions so that schedule or data-pull regressions surface immediately rather than hiding in logs.

### Change configuration without editing `config.py`

`config.py` reads a handful of values from environment variables at import time, with sensible defaults baked in. To override any of them for a single run:

```bash
export BLITZY_OUTPUT_DIR=/tmp/my_nba_output
export BLITZY_RATE_LIMIT_SECONDS=2.0
export BLITZY_LOG_LEVEL=DEBUG
python run.py all --season 2025-26
```

The complete list of supported overrides is in `.env.example` at the project root.

---

## Suggested Next Tasks

When the core pipeline is green and you have time to harden it, these items provide the most engineering leverage.

| # | Task | Why it matters |
|---|---|---|
| 1 | Implement a `--force` flag on every subcommand that ignores the checkpoint | Simplifies re-runs for debugging; Rule 5 remains intact because the flag is opt-in. |
| 2 | Add `tests/invariants/test_bare_except_scope.py` that greps for `except Exception:` outside `pipelines/ingest_games.py` | Enforces Rule 6 scope mechanically so it cannot expand accidentally. |
| 3 | Implement a `DuckDBWriter` subclass of `BaseWriter` | Opens a zero-infrastructure analytical surface while respecting Rule 7. Requires Rule 8 revisit — gates this on a decision-log entry. |
| 4 | Parallelize the `ingest_games` per-game loop via `asyncio` + a shared `RateLimiter` | Biggest runtime reduction available; requires the rate limiter to become process-wide. |
| 5 | Add a `Makefile` or `just` target for each validation gate | Removes the last bit of friction from Gate 2 and Gate 10 verification. |
| 6 | Add a `prometheus` scraper sidecar doc + optional HTTP `/metrics` endpoint behind a feature flag | Respects Rule 8 by remaining opt-in while unlocking dashboards. |
| 7 | Add retry-budget accounting — alert when a single endpoint consumes more than N% of retries | Detects upstream drift early without adding infrastructure. |
| 8 | Add schema drift detection — compare `resultSets.headers` against a committed baseline | Catches NBA Stats API schema changes on the first affected run. |
| 9 | Add coverage reporting (`pytest-cov`) | Optional for Gate 10 but useful to identify test gaps. |
| 10 | Port the pipeline's `click` CLI to `typer` on a feature branch | Ergonomic improvement; supersede the `click` decision if the benefit is real. |

Each task should be added to the decision log (`docs/DECISIONS.md`) with a `Deferred` status before work begins. Never skip the decision-log entry — Rule-8 adjacent work (items 3 and 6) specifically requires explicit rationale before implementation.

---

## Where to Go Next

- **Running the pipeline:** you just did it. Repeat with `--season 2024-25` for a historical run.
- **Understanding the why:** [`DECISIONS.md`](./DECISIONS.md) enumerates every non-obvious design choice with its alternatives, rationale, and risk.
- **Tracing features to files:** [`TRACEABILITY.md`](./TRACEABILITY.md) maps features, rules, and gates to implementing files with 100% coverage.
- **Observability surface:** [`OBSERVABILITY.md`](./OBSERVABILITY.md) documents structured logging, correlation IDs, the six Prometheus counters, health and readiness, and the two dashboard templates.
- **Feature deep dives:** one doc per domain under [`features/`](./features/): [`players.md`](./features/players.md), [`teams.md`](./features/teams.md), [`games.md`](./features/games.md), [`lineups.md`](./features/lineups.md), [`schedule.md`](./features/schedule.md).
- **Endpoint reference:** [`api/endpoints_catalog.md`](./api/endpoints_catalog.md) catalogs all 15+ NBA Stats endpoints with their parameters and output columns.
- **Operator dashboards:** [`dashboards/operator_dashboard.json`](./dashboards/operator_dashboard.json) (Grafana) and [`dashboards/operator_dashboard.md`](./dashboards/operator_dashboard.md) (Markdown fallback).
- **Executive summary:** [`executive-summary.html`](./executive-summary.html) — open in any browser for a 16-slide leadership deck.
- **The authoritative contract:** [`New_Product_Prompt_20260418.md`](./New_Product_Prompt_20260418.md) — the product brief that everything traces back to.

Welcome aboard.
