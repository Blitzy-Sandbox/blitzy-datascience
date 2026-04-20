[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![LinkedIn][linkedin-shield]][linkedin-url]

<div align="center">
  <h1>blitzy-datascience</h1>
  <p>A modular Python pipeline for ingesting, normalizing, and persisting NBA statistics data from the NBA Stats API.</p>
  <a href="https://github.com/Blitzy-Sandbox/blitzy-datascience"><strong>Explore the docs</strong></a>
  &middot;
  <a href="https://github.com/Blitzy-Sandbox/blitzy-datascience/issues/new?labels=bug">Report Bug</a>
  &middot;
  <a href="https://github.com/Blitzy-Sandbox/blitzy-datascience/issues/new?labels=enhancement">Request Feature</a>
</div>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about-the-project">About The Project</a></li>
    <li><a href="#built-with">Built With</a></li>
    <li><a href="#getting-started">Getting Started</a></li>
    <li><a href="#observability">Observability</a></li>
    <li><a href="#documentation">Documentation</a></li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#structure">Structure</a></li>
    <li><a href="#data-domains">Data Domains</a></li>
    <li><a href="#output-files">Output Files</a></li>
    <li><a href="#architecture">Architecture</a></li>
    <li><a href="#tasks">Tasks</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

---

## About The Project

blitzy-datascience is a CLI-driven data ingestion pipeline that pulls comprehensive NBA statistics from the NBA Stats API (stats.nba.com). It covers 6 data domains — players, teams, games, lineups, tracking, and schedule — across 15+ API endpoints.

The pipeline normalizes all API responses into flat, schema-consistent CSV files suitable for downstream analytics, dashboarding, and BI tools. It supports configurable season targeting (default: 2025-26), checkpoint-based resumability, and exponential backoff to respect API rate limits.

Designed for composability: each layer (HTTP client, endpoints, pipelines, storage) is independent and swappable. The storage interface supports CSV out of the box, with a pluggable design for future database backends.

## Built With

[![Python][python-shield]][python-url]
[![Pandas][pandas-shield]][pandas-url]

## Getting Started

### Prerequisites

- Python 3.11+ and pip (verified against Python 3.11 and 3.12)

### Installation

```bash
git clone https://github.com/Blitzy-Sandbox/blitzy-datascience.git
cd blitzy-datascience
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### First run

```bash
python run.py --help
python run.py all --season 2025-26
```

### Development setup

```bash
pip install -r requirements.txt                # includes pytest + flake8 pins
python -m pytest tests/ -m "not integration"   # unit + invariant tests only
python -m pytest tests/                        # full suite including @pytest.mark.integration
python -m flake8 .                             # lint gate (Gate 2)
python -m py_compile $(git ls-files '*.py')    # zero-warning compile gate
```

For a deeper clean-machine guide, including domain context, common pitfalls, and extension patterns, see [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Observability

Every run emits structured logs to stdout and a rotating file at `logs/pipeline.log`. Each log record carries a UUID4 correlation ID minted at CLI entry, so a single invocation is traceable end-to-end across every module. Diagnostic subcommands expose health and metrics on demand.

```bash
python run.py health      # liveness: always-on
python run.py ready       # readiness: output/ writable, config valid, checkpoint parseable
python run.py metrics     # Prometheus text-format counters for requests, retries, rows, failures
```

See [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) for the full log format, the metrics catalog, the dashboard template, and correlation-ID propagation details.

Exposed counters: `nba_requests_total`, `nba_request_failures_total`, `nba_retries_total`, `pipeline_rows_written_total`, `pipeline_runs_total`, `games_failed_total`.

## Documentation

| Document | Purpose |
|---|---|
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | Setup, domain context, pitfalls, extension patterns, next tasks. |
| [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) | Log format, correlation IDs, metrics catalog, health/readiness, dashboards. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log (Decision / Alternatives / Rationale / Risk). |
| [docs/TRACEABILITY.md](docs/TRACEABILITY.md) | Bidirectional matrix: features ↔ rules ↔ gates ↔ files. |
| [docs/api/endpoints_catalog.md](docs/api/endpoints_catalog.md) | Per-endpoint reference for all 15+ NBA Stats endpoints. |
| [docs/features/players.md](docs/features/players.md) | F-009 Players pipeline deep dive. |
| [docs/features/teams.md](docs/features/teams.md) | F-010 Teams pipeline deep dive. |
| [docs/features/games.md](docs/features/games.md) | F-011 Games pipeline deep dive (Rule 6 narrative). |
| [docs/features/lineups.md](docs/features/lineups.md) | F-012 Lineups pipeline deep dive. |
| [docs/features/schedule.md](docs/features/schedule.md) | F-013 Schedule pipeline deep dive (F-011 dependency). |
| [docs/dashboards/operator_dashboard.json](docs/dashboards/operator_dashboard.json) | Grafana-compatible dashboard template. |
| [docs/dashboards/operator_dashboard.md](docs/dashboards/operator_dashboard.md) | Markdown operator dashboard fallback. |
| [docs/executive-summary.html](docs/executive-summary.html) | Self-contained reveal.js executive deck (Blitzy brand). |
| [docs/New_Product_Prompt_20260418.md](docs/New_Product_Prompt_20260418.md) | Authoritative product brief (read-only). |

## Usage

### Run the full pipeline

```bash
python run.py all --season 2025-26
```

### Run specific domains

```bash
python run.py players --season 2025-26
python run.py teams --season 2025-26
python run.py games --season 2025-26
python run.py lineups --season 2025-26
python run.py schedule --season 2025-26
```

### Resume after interruption

Re-run the same command. The checkpoint manifest (`output/checkpoint.json`) tracks completed pulls and skips them automatically.

## Structure

```
blitzy-datascience/
├── run.py                     # CLI entry point (click subcommands + health/ready/metrics)
├── config.py                  # Season list, output paths, API base URL, retry params, headers
├── requirements.txt           # Pinned runtime + dev dependencies
├── pytest.ini                 # pytest markers and warning filters (Gate 10)
├── .flake8                    # Lint configuration (Gate 2)
├── .gitignore                 # Excludes output/, logs/, __pycache__/, .pytest_cache/, venv
├── .env.example               # Operator-overridable env vars reference
├── api/
│   ├── __init__.py
│   └── nba_client.py          # HTTP client: headers, retries, rate limiting
├── endpoints/
│   ├── __init__.py
│   ├── players.py             # 5 Players endpoints
│   ├── teams.py               # 3 Teams endpoints
│   ├── games.py               # 4 Games endpoints (box scores + PBP)
│   ├── lineups.py             # 2 Lineups endpoints (incl. on/off splits)
│   └── schedule.py            # leaguegamefinder + enumerate_game_ids helper
├── pipelines/
│   ├── __init__.py
│   ├── ingest_players.py      # F-009 orchestrator
│   ├── ingest_teams.py        # F-010 orchestrator
│   ├── ingest_games.py        # F-011 orchestrator (Rule 6 fail-safe iteration)
│   ├── ingest_lineups.py      # F-012 orchestrator
│   └── ingest_schedule.py     # F-013 orchestrator + GAME_ID enumeration
├── storage/
│   ├── __init__.py
│   └── csv_writer.py          # BaseWriter ABC + CSVWriter (sole to_csv caller)
├── utils/
│   ├── __init__.py
│   ├── rate_limiter.py        # ≥ 1.0s floor (Rule 2)
│   ├── schema_normalizer.py   # Flatten resultSets (Rule 4)
│   ├── checkpoint.py          # JSON manifest (Rule 5)
│   ├── logger.py              # Stdlib logging + rotating file + correlation ID
│   ├── correlation.py         # contextvars-based correlation ID propagation
│   ├── metrics.py             # Prometheus text-format counter registry
│   └── health.py              # Health and readiness probes
├── tests/
│   ├── conftest.py
│   ├── unit/                  # mirrors production tree
│   ├── integration/           # @pytest.mark.integration live tests (Gates 1, 8)
│   └── invariants/            # grep/DataFrame invariants (Rules 1, 4, 7)
├── docs/                      # Product brief + onboarding, observability, decisions, traceability, dashboards, executive deck, per-feature docs
└── output/                    # Runtime artifacts (excluded from VCS)
    └── checkpoint.json
```

## Data Domains

### Players
| Endpoint | Description |
|----------|-------------|
| `leaguedashplayerstats` | Per game, totals, and advanced player stats |
| `leaguedashplayerclutch` | Clutch splits per player |
| `playercareerstats` | Career totals per player |
| `playergamelog` | Game-level player logs |
| `leaguedashptstats` | Player tracking stats |

### Teams
| Endpoint | Description |
|----------|-------------|
| `leaguedashteamstats` | Per game, totals, and advanced team stats |
| `teamgamelog` | Game-level team logs |
| `teamdashboardbygeneralsplits` | Advanced team splits |

### Games
| Endpoint | Description |
|----------|-------------|
| `scoreboardv2` | Daily scoreboard |
| `boxscoretraditionalv2` | Traditional box score per game |
| `boxscoreadvancedv2` | Advanced box score per game |
| `playbyplayv2` | Play-by-play events per game |

### Lineups
| Endpoint | Description |
|----------|-------------|
| `leaguedashlineups` | Lineup combination stats |
| `leaguedashplayerclutch` | On/off split lineup clutch data |

### Schedule
| Endpoint | Description |
|----------|-------------|
| `leaguegamefinder` | Season game metadata (dates, teams, IDs) |

## Output Files

| File | Key Columns |
|------|-------------|
| `players.csv` | season, player_id, team_id |
| `teams.csv` | season, team_id |
| `games.csv` | season, game_id, team_id |
| `play_by_play.csv` | season, game_id, event_num |
| `lineups.csv` | season, group_id, team_id |
| `schedule.csv` | season, game_id, home_team_id, away_team_id |
| `player_tracking.csv` | season, player_id, team_id |

All CSVs are fully flattened — no nested JSON. Every file includes metadata columns for season, relevant IDs, and timestamps.

## Architecture

```
CLI (run.py)
  │
  ├── pipelines/ingest_players.py ──→ endpoints/players.py ──→ api/nba_client.py
  ├── pipelines/ingest_teams.py   ──→ endpoints/teams.py   ──→ api/nba_client.py
  └── pipelines/ingest_games.py   ──→ endpoints/games.py   ──→ api/nba_client.py
                                      endpoints/schedule.py ──→ api/nba_client.py
  │
  ├── utils/checkpoint.py (tracks completed pulls)
  ├── utils/rate_limiter.py (backoff between requests)
  └── storage/csv_writer.py (writes DataFrames to CSV)
```

**Key design decisions:**

- **Single HTTP client:** All requests route through `NBAClient`, which handles headers, retries (exponential backoff with jitter), and rate limiting
- **Checkpoint resumability:** A JSON manifest tracks every completed pull; re-runs skip already-fetched data
- **Fail-safe iteration:** Failed game IDs are logged and skipped — one bad game never crashes the pipeline
- **Pluggable storage:** Pipelines call a `BaseWriter.write()` interface, making it straightforward to swap CSV for a database backend

## Tasks

### Completed in this release
- [x] Core pipeline modules implemented (config, api, endpoints, pipelines, storage, utils)
- [x] CLI entry point with players / teams / games / lineups / schedule / all subcommands
- [x] Diagnostic subcommands: health, ready, metrics
- [x] Unit, integration, and invariant test suite (`python -m pytest tests/`)
- [x] Structured logging with correlation IDs and rotating file sink
- [x] Decision log and bidirectional traceability matrix
- [x] Executive summary deck (reveal.js)

### Deferred to future phases
- [ ] Database writer implementation (PostgreSQL / DuckDB) — `BaseWriter` extension point preserved
- [ ] Parallel game-level ingestion with thread pool (requires revisiting Rule 2 floor)
- [ ] Scheduling support for incremental daily updates
- [ ] Remote metrics backend (Prometheus scraping / Pushgateway)

## Contributing

Contributions make the open source community an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/amazing-feature`)
3. Commit your Changes (`git commit -m 'Add amazing feature'`)
4. Push to the Branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Contact

Michael Montanaro

Project Link: [https://github.com/Blitzy-Sandbox/blitzy-datascience](https://github.com/Blitzy-Sandbox/blitzy-datascience)

## Acknowledgments

- [NBA Stats API](https://stats.nba.com)
- [pandas](https://pandas.pydata.org)
- [click](https://click.palletsprojects.com)
- [requests](https://docs.python-requests.org)
- [tenacity](https://tenacity.readthedocs.io)

---

<!-- Reference-style links -->
[contributors-shield]: https://img.shields.io/github/contributors/Blitzy-Sandbox/blitzy-datascience.svg?style=flat
[contributors-url]: https://github.com/Blitzy-Sandbox/blitzy-datascience/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/Blitzy-Sandbox/blitzy-datascience.svg?style=flat
[forks-url]: https://github.com/Blitzy-Sandbox/blitzy-datascience/network/members
[stars-shield]: https://img.shields.io/github/stars/Blitzy-Sandbox/blitzy-datascience.svg?style=flat
[stars-url]: https://github.com/Blitzy-Sandbox/blitzy-datascience/stargazers
[issues-shield]: https://img.shields.io/github/issues/Blitzy-Sandbox/blitzy-datascience.svg?style=flat
[issues-url]: https://github.com/Blitzy-Sandbox/blitzy-datascience/issues
[linkedin-shield]: https://img.shields.io/badge/-LinkedIn-blue.svg?style=flat&logo=linkedin
[linkedin-url]: https://linkedin.com/in/michael-montanaro
[python-shield]: https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white
[python-url]: https://python.org
[pandas-shield]: https://img.shields.io/badge/Pandas-150458?style=flat&logo=pandas&logoColor=white
[pandas-url]: https://pandas.pydata.org
