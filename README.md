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

- Python 3.11+
- pip

### Installation

```bash
git clone https://github.com/Blitzy-Sandbox/blitzy-datascience.git
cd blitzy-datascience
pip install -r requirements.txt
```

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
├── run.py                     # CLI entry point (click subcommands)
├── config.py                  # Season list, output paths, API base URL
├── requirements.txt
├── api/
│   └── nba_client.py          # HTTP client: headers, retries, rate limiting
├── endpoints/
│   ├── players.py             # Player stats endpoints
│   ├── teams.py               # Team stats endpoints
│   ├── games.py               # Box scores, play-by-play endpoints
│   ├── lineups.py             # Lineup stats endpoints
│   └── schedule.py            # Season schedule endpoint
├── pipelines/
│   ├── ingest_players.py      # Player data orchestration
│   ├── ingest_teams.py        # Team data orchestration
│   └── ingest_games.py        # Game-level iteration and ingestion
├── storage/
│   └── csv_writer.py          # CSV writer (pluggable interface)
├── utils/
│   ├── rate_limiter.py        # Exponential backoff + jitter
│   ├── schema_normalizer.py   # Flatten nested JSON, enforce types
│   ├── checkpoint.py          # JSON manifest for resumability
│   └── logger.py              # Logging configuration
└── output/                    # Generated CSV files
    └── checkpoint.json        # Pull completion manifest
```

## Data Domains

### Players
| Endpoint | Description |
|----------|-------------|
| `leaguedashplayerstats` | Per game, totals, and advanced player stats |
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

- [ ] Implement core pipeline modules
- [ ] Add integration tests with live API smoke tests
- [ ] Add database writer implementation (PostgreSQL / DuckDB)
- [ ] Add parallel game-level ingestion with thread pool
- [ ] Add scheduling support for incremental daily updates

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
