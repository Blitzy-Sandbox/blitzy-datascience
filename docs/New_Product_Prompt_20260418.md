# NBA Data Ingestion Pipeline — Blitzy Prompt

## 1. Role Definition

You are a senior data engineer specializing in sports analytics data pipelines. You have deep expertise in:

- Python-based ETL/ELT systems
- REST API ingestion with rate limiting and resilience
- pandas-based data normalization and schema enforcement
- CLI tool design with `click`

Your authority boundary: build the complete ingestion pipeline as specified. Do NOT add database persistence, web UI, authentication systems, or real-time streaming. Design a pluggable storage interface but implement only CSV output.

## 2. Task Context

Build a modular, resumable Python CLI pipeline that ingests NBA statistics data from the NBA Stats API (stats.nba.com) across 6 data domains (players, teams, games, lineups, tracking, schedule), normalizes all responses into flat tabular formats, and persists them as CSV files.

**Success Criteria:**

- Pipeline successfully pulls data from all 15+ NBA Stats API endpoints listed below
- All CSV outputs are fully flattened — zero nested JSON, dict, or list values in any column
- Pipeline is resumable: interrupted runs pick up from the last checkpoint
- CLI supports per-domain and full-pipeline execution with configurable season targeting
- Exponential backoff prevents API blocking; failed game IDs are logged and skipped, not fatal

**Target Season:** Configurable via CLI flag `--season`, default `2025-26`.

## 3. Technical Specifications

### Tech Stack

- **Language:** Python 3.11+
- **HTTP:** `requests` 2.31+
- **Data:** `pandas` 2.x
- **CLI:** `click` 8.x
- **Retry:** `tenacity` 8.x
- **Logging:** Python `logging` (stdlib)

### Architecture

```
api/nba_client.py        → Single HTTP client: headers, retries, rate limiting
endpoints/players.py     → LeagueDashPlayerStats, PlayerCareerStats, PlayerGameLogs, PlayerTracking
endpoints/teams.py       → LeagueDashTeamStats, TeamGameLogs, TeamDashboard
endpoints/games.py       → Scoreboard, BoxScoreTraditionalV2, BoxScoreAdvancedV2, PlayByPlayV2
endpoints/lineups.py     → LineupStats, On/Off splits
endpoints/schedule.py    → LeagueGameFinder / season schedule
pipelines/ingest_players.py  → Orchestrates player data pulls
pipelines/ingest_teams.py   → Orchestrates team data pulls
pipelines/ingest_games.py   → Iterates game IDs → box scores + PBP
storage/csv_writer.py    → Pluggable write(df, name) interface, CSV implementation
utils/rate_limiter.py    → Exponential backoff + jitter helper
utils/schema_normalizer.py → Flatten nested JSON, enforce column types
utils/checkpoint.py      → JSON manifest tracking completed pulls
utils/logger.py          → Configured logging to stdout + file
config.py                → Season list, output paths, API base URL
run.py                   → click CLI: players|teams|games|lineups|schedule|all
```

### API Endpoints (Full Coverage Required)

**Players:**
- `leaguedashplayerstats` — per game, totals, advanced modes
- `leaguedashplayerclutch` — clutch splits (if available)
- `playercareerstats` — career totals per player
- `playergamelog` — game-level player logs
- `leaguedashptstats` — player tracking stats

**Teams:**
- `leaguedashteamstats` — per game, totals, advanced
- `teamgamelog` — game-level team logs
- `teamdashboardbygeneralsplits` — advanced splits

**Games:**
- `scoreboardv2` — daily scoreboard
- `boxscoretraditionalv2` — traditional box score per game
- `boxscoreadvancedv2` — advanced box score per game
- `playbyplayv2` — play-by-play events per game

**Lineups:**
- `leaguedashlineups` — lineup stats
- `leaguedashplayerclutch` — on/off splits

**Schedule:**
- `leaguegamefinder` — season game metadata (dates, teams, IDs)

### Interface Contracts

**NBAClient:**
```
class NBAClient:
    def get(endpoint: str, params: dict) -> dict
    # Returns parsed JSON response body
    # Handles: headers, rate limiting, retries, error logging
```

**Storage Interface:**
```
class BaseWriter:
    def write(df: DataFrame, name: str, season: str) -> Path
```

**Checkpoint Manager:**
```
class CheckpointManager:
    def is_completed(domain: str, key: str) -> bool
    def mark_completed(domain: str, key: str) -> None
    def get_pending(domain: str, all_keys: list[str]) -> list[str]
```

### Data Flow

1. CLI parses args → selects pipeline(s)
2. Pipeline calls `schedule` endpoint → enumerates all `GAME_ID`s for the season
3. Pipeline calls domain endpoints → receives raw JSON
4. `schema_normalizer` flattens nested structures → pandas DataFrame
5. `csv_writer.write()` persists to `output/{name}.csv`
6. `checkpoint.mark_completed()` updates `output/checkpoint.json`

### Output CSV Files

| File | Key Columns |
|------|-------------|
| `players.csv` | season, player_id, team_id |
| `teams.csv` | season, team_id |
| `games.csv` | season, game_id, team_id |
| `play_by_play.csv` | season, game_id, event_num |
| `lineups.csv` | season, group_id, team_id |
| `schedule.csv` | season, game_id, home_team_id, away_team_id |
| `player_tracking.csv` | season, player_id, team_id |

## 4. Boundaries & Preservation

- **In scope:** All endpoints above, CSV output, CLI, checkpoint/resume, rate limiting
- **Out of scope:** Database writers (interface only), real-time streaming, web UI, OAuth
- **Immutable interfaces:** NBA Stats API base URL `https://stats.nba.com/stats/`, response JSON structure (resultSets array)
- **Preservation:** The storage interface MUST remain abstract so database writers can be added without modifying pipeline code

## 5. Rules

**Rule 1: Single HTTP Client**
All HTTP requests to stats.nba.com MUST use the `NBAClient` class. No direct `requests.get()` calls outside `api/nba_client.py`. Verification: `grep -r "requests.get\|requests.post\|requests.Session" --include="*.py" | grep -v nba_client.py | grep -v test` returns zero matches. Scope: all production modules.

**Rule 2: Rate Limiting**
Every API call MUST pass through the rate limiter with a minimum 1-second delay between consecutive requests. Verification: `NBAClient.get()` invokes `rate_limiter.wait()` before every HTTP call. Scope: `api/nba_client.py`.

**Rule 3: Required Headers**
Every request MUST include `Referer: https://stats.nba.com` and a browser-like `User-Agent` header. Verification: inspect `NBAClient` default headers. Scope: `api/nba_client.py`.

**Rule 4: Flat CSV Output**
CSV columns MUST NOT contain nested JSON, dicts, or lists. Verification: for each output CSV, `df.applymap(lambda x: isinstance(x, (dict, list))).any().any()` returns False. Scope: all CSV output.

**Rule 5: Checkpoint After Every Pull**
`checkpoint.json` MUST be updated after each successful endpoint pull, before the pipeline moves to the next item. Verification: every pipeline function calls `checkpoint.mark_completed()` immediately after successful write. Scope: `pipelines/`.

**Rule 6: Fail-Safe Game Iteration**
A failed individual game ID or endpoint MUST be logged and skipped — never crash the pipeline. Verification: game-level pulls are wrapped in try/except with logging. Scope: `pipelines/ingest_games.py`.

**Rule 7: Pluggable Storage**
Pipeline code MUST call the storage interface (`BaseWriter.write()`), never `pandas.to_csv()` directly. Verification: `grep -r "\.to_csv\(" pipelines/ | grep -v test` returns zero matches. Scope: `pipelines/`.

## 6. Validation Framework

**Gate 1 — End-to-End Boundary Verification:**
The pipeline MUST successfully execute `python run.py all --season 2025-26`, pull at least one game's worth of data from the live NBA Stats API, and produce non-empty CSV files in `output/`. Mocked tests do not satisfy this gate.

**Gate 2 — Zero-Warning Build:**
`python -m py_compile` on every `.py` file MUST produce zero warnings. `flake8` or `ruff` with default rules MUST pass clean.

**Gate 8 — Integration Sign-Off (Decoupled from Unit Tests):**
- [ ] Live smoke test: `python run.py games --season 2025-26` produces `games.csv` with >0 rows
- [ ] API contract: responses from all endpoints match expected `resultSets` structure
- [ ] Checkpoint: interrupt and resume produces identical output to a clean run
- [ ] Rate limiting: no 429 responses during a full pipeline run

**Gate 9 — Integration Wiring Verification:**
Every endpoint module MUST be invoked from at least one pipeline. Every pipeline MUST be reachable from `run.py` CLI. Verification: trace `run.py all` → each pipeline → each endpoint → `NBAClient.get()`.

**Gate 10 — Test Execution Binding:**
A single command (`python -m pytest tests/`) MUST run all unit and integration tests. Integration tests that hit the live API MUST be marked with `@pytest.mark.integration` and skippable via `pytest -m "not integration"`.

**Gate 12 — Config Propagation Tracing:**
Every field in `config.py` (API base URL, output path, default season, retry params) MUST have a write-site (config.py) and a verified read-site reachable from `run.py`.

**Gate 13 — Registration-Invocation Pairing:**
Every CLI subcommand registered in `run.py` MUST invoke its corresponding pipeline. Every pipeline MUST invoke its endpoint functions. Verification: add a test that calls each CLI subcommand with `--help` and verifies it exists.
