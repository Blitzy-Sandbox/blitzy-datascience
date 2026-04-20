# NBA Stats API — Endpoint Catalog

**Scope:** 15 endpoints across 6 data domains (Players, Teams, Games, Lineups, Tracking, Schedule).
**Authoritative upstream:** `https://stats.nba.com/stats/` (declared in `config.API_BASE_URL`).
**Authoritative downstream:** seven flat CSV artifacts under `output/` plus one JSON manifest at `output/checkpoint.json`.
**Maintained by:** the Blitzy repository owner. This catalog is the single source of truth for per-endpoint behavior; if an `endpoints/*.py` wrapper diverges from this document, the wrapper is the primary authority and this document MUST be updated to match.

---

## 1. How To Read This Catalog

This catalog is the authoritative per-endpoint reference for the NBA Data Ingestion Pipeline. Every entry describes a single upstream NBA Stats API endpoint, its parameters, the wrapper function that calls it, the pipeline that consumes it, and the CSV artifact its rows ultimately land in.

**Operational invariants that apply to every endpoint listed below:**

- **Rule 1 — Single HTTP Client.** Every endpoint is accessed only through `api.nba_client.NBAClient.get(endpoint: str, params: dict) -> dict`. No other module in the codebase calls `requests.get`, `requests.post`, or `requests.Session`. Verified by `tests/invariants/test_rule1_sole_http_client.py`.
- **Rule 2 — Rate Limit Floor.** Every outbound call is preceded by a `utils.rate_limiter.RateLimiter.wait()` invocation that enforces a minimum inter-request gap of `config.RATE_LIMIT_SECONDS` (default: `1.0` second). This floor is proactive and is the primary control preventing HTTP 429 responses during normal operation.
- **Rule 3 — Required Headers.** Every request carries `Referer: https://stats.nba.com` plus a browser-style `User-Agent` string. These headers are attached once to the `requests.Session` in `NBAClient.__init__` from `config.REQUIRED_HEADERS`.
- **Rule 4 — Flat CSV Cells.** The `utils.schema_normalizer.normalize_result_sets` function asserts that no cell in any output DataFrame contains a `dict` or `list` before returning. Every endpoint's response is therefore eligible for write only after flattening.
- **Rule 5 — Checkpoint After Every Pull.** Every successful pull concludes with `utils.checkpoint.CheckpointManager.mark_completed(key)` synchronously persisting the `(domain, endpoint, season[, per-entity-scope])` tuple to `output/checkpoint.json`.
- **Rule 6 — Fail-Safe Iteration.** Applies to `pipelines/ingest_games.py` only — the per-`GAME_ID` loop in that pipeline catches `Exception` so a single malformed game does not abort the full run.
- **Rule 7 — Pluggable Storage.** `DataFrame.to_csv` is called in exactly one location — `storage.csv_writer.CSVWriter.write` — and is never invoked by an endpoint wrapper or pipeline directly.

**Base URL:** `https://stats.nba.com/stats/` (concatenated with the endpoint name to form the full request URL, e.g., `https://stats.nba.com/stats/leaguedashplayerstats`).

**Response envelope:** every endpoint returns a JSON object of the shape:

```json
{
  "resource": "<endpoint_name>",
  "parameters": { ... },
  "resultSets": [
    { "name": "<TableName>", "headers": ["COL1", "COL2", ...], "rowSet": [[v1, v2, ...], ...] }
  ]
}
```

The number of entries in `resultSets` varies by endpoint (most endpoints return 1 table; some return multiple). Each entry is flattened into a separate `pandas.DataFrame` by `utils.schema_normalizer.normalize_result_sets`.

**Season string format:** `YYYY-YY` (e.g., `2025-26`). Used for the `Season` parameter across all season-scoped endpoints.

**Error taxonomy:**

- **HTTP 429 Too Many Requests** → retried automatically by `tenacity` in `NBAClient.get` with exponential backoff and jitter up to `config.RETRY_MAX_WAIT`.
- **HTTP 5xx Server Error** → retried automatically by `tenacity`.
- **HTTP 4xx non-429** → propagates (not retried; typically indicates a permanent parameter error).
- **Timeout / connection error** → retried automatically by `tenacity`.
- **Post-exhaustion** → exception propagates; in `ingest_games.py` the Rule 6 loop catches it and logs WARNING; in every other pipeline the exception aborts the current run and the checkpoint preserves prior progress for the next invocation.

---

## 2. Endpoint Index

| # | Endpoint Name | Domain | Wrapper Function | Destination CSV | Consuming Pipeline |
|---|---|---|---|---|---|
| 1 | `leaguedashplayerstats` | Players | `endpoints.players.fetch_leaguedashplayerstats` | `output/players.csv` | `pipelines/ingest_players.py` |
| 2 | `leaguedashplayerclutch` | Players | `endpoints.players.fetch_leaguedashplayerclutch` | `output/players.csv` | `pipelines/ingest_players.py` |
| 3 | `playercareerstats` | Players | `endpoints.players.fetch_playercareerstats` | `output/players.csv` | `pipelines/ingest_players.py` |
| 4 | `playergamelog` | Players | `endpoints.players.fetch_playergamelog` | `output/players.csv` | `pipelines/ingest_players.py` |
| 5 | `leaguedashteamstats` | Teams | `endpoints.teams.fetch_leaguedashteamstats` | `output/teams.csv` | `pipelines/ingest_teams.py` |
| 6 | `teamgamelog` | Teams | `endpoints.teams.fetch_teamgamelog` | `output/teams.csv` | `pipelines/ingest_teams.py` |
| 7 | `teamdashboardbygeneralsplits` | Teams | `endpoints.teams.fetch_teamdashboardbygeneralsplits` | `output/teams.csv` | `pipelines/ingest_teams.py` |
| 8 | `scoreboardv2` | Games | `endpoints.games.fetch_scoreboardv2` | `output/games.csv` | `pipelines/ingest_games.py` |
| 9 | `boxscoretraditionalv2` | Games | `endpoints.games.fetch_boxscoretraditionalv2` | `output/games.csv` | `pipelines/ingest_games.py` |
| 10 | `boxscoreadvancedv2` | Games | `endpoints.games.fetch_boxscoreadvancedv2` | `output/games.csv` | `pipelines/ingest_games.py` |
| 11 | `playbyplayv2` | Games | `endpoints.games.fetch_playbyplayv2` | `output/play_by_play.csv` | `pipelines/ingest_games.py` |
| 12 | `leaguedashlineups` | Lineups | `endpoints.lineups.fetch_leaguedashlineups` | `output/lineups.csv` | `pipelines/ingest_lineups.py` |
| 13 | `leaguedashplayerclutch` (on/off variant) | Lineups | `endpoints.lineups.fetch_leaguedashplayerclutch_onoff` | `output/lineups.csv` | `pipelines/ingest_lineups.py` |
| 14 | `leaguedashptstats` | Tracking | `endpoints.players.fetch_leaguedashptstats` | `output/player_tracking.csv` | `pipelines/ingest_players.py` |
| 15 | `leaguegamefinder` | Schedule | `endpoints.schedule.fetch_leaguegamefinder` | `output/schedule.csv` | `pipelines/ingest_schedule.py` (and consumed by `pipelines/ingest_games.py` via `endpoints.schedule.enumerate_game_ids`) |

**Endpoint count:** 15 unique endpoint invocations (14 unique NBA Stats API endpoint names, because `leaguedashplayerclutch` is invoked from two different wrappers with distinct parameter profiles — once for Players basic clutch splits, once for Lineups on/off-court splits).

---

## 3. Players Domain

Four endpoints that produce per-player season aggregates, career totals, game logs, and clutch splits. All four wrappers live in `endpoints/players.py`. All four are consumed by `pipelines/ingest_players.py`. The combined output of these four endpoints lands in `output/players.csv`.

### leaguedashplayerstats

**Purpose:** Fetch league-wide per-player season-level aggregate statistics (per-game, totals, or advanced) for every active NBA player in the specified season.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | Also accepts `"Playoffs"`, `"Pre Season"`, `"All Star"` |
| `LeagueID` | `str` | `"00"` | `"00"` = NBA |
| `PerMode` | `str` | `"PerGame"` | Also accepts `"Totals"`, `"Per36"`, `"Per100Possessions"`, `"PerMinute"` |
| `MeasureType` | `str` | `"Base"` | Also accepts `"Advanced"`, `"Misc"`, `"Scoring"`, `"Usage"`, `"Defense"`, `"Opponent"` |

**Optional parameters (the NBA Stats API requires these present, but empty strings or zeros indicate "no filter"):**

| Name | Type | Default | Notes |
|---|---|---|---|
| `PlusMinus` | `str` | `"N"` | `"Y"` to include plus-minus columns |
| `PaceAdjust` | `str` | `"N"` | `"Y"` to pace-adjust |
| `Rank` | `str` | `"N"` | `"Y"` to include rank columns |
| `LastNGames` | `str` | `"0"` | `"0"` = no last-N filter |
| `Month` | `str` | `"0"` | `"0"` = all months |
| `OpponentTeamID` | `str` | `"0"` | `"0"` = all opponents |
| `Period` | `str` | `"0"` | `"0"` = all periods |
| `PORound` | `str` | `"0"` | Playoff round filter |
| `TeamID` | `str` | `"0"` | `"0"` = all teams |
| `DateFrom` / `DateTo` | `str` | `""` | Empty = no date filter |
| `GameSegment` / `Location` / `Outcome` / `SeasonSegment` / `ShotClockRange` / `VsConference` / `VsDivision` / `Conference` / `Division` / `GameScope` / `PlayerExperience` / `PlayerPosition` / `StarterBench` / `College` / `Country` / `DraftPick` / `DraftYear` / `Height` / `Weight` | `str` | `""` | Filter fields — empty = no filter |
| `TwoWay` | `str` | `"0"` | `"0"` = include all contract types |

**Response envelope:** `resultSets` contains one entry named `LeagueDashPlayerStats`. Columns include `PLAYER_ID`, `PLAYER_NAME`, `TEAM_ID`, `TEAM_ABBREVIATION`, `AGE`, `GP`, `W`, `L`, `W_PCT`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `TOV`, `STL`, `BLK`, `BLKA`, `PF`, `PFD`, `PTS`, `PLUS_MINUS`, `NBA_FANTASY_PTS`, `DD2`, `TD3`, plus rank columns when `Rank="Y"`.

**Key columns (in `output/players.csv`):** `(PLAYER_ID, SEASON_ID, TEAM_ID)` per `README.md` §Data Domains/Players.

**Wrapper function:** `endpoints.players.fetch_leaguedashplayerstats`

**Consuming pipeline:** `pipelines/ingest_players.py`

**Destination CSV:** `output/players.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor).

### leaguedashplayerclutch

**Purpose:** Fetch league-wide per-player clutch-time splits (performance during late-game high-leverage situations).

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PerMode` | `str` | `"PerGame"` | |
| `MeasureType` | `str` | `"Base"` | |
| `ClutchTime` | `str` | `"Last 5 Minutes"` | Also accepts `"Last 4 Minutes"`, `"Last 3 Minutes"`, `"Last 2 Minutes"`, `"Last 1 Minute"`, `"Last 30 Seconds"`, `"Last 10 Seconds"` |
| `AheadBehind` | `str` | `"Ahead or Behind"` | Also accepts `"Behind or Tied"`, `"Ahead or Tied"` |
| `PointDiff` | `str` | `"5"` | Absolute point-differential threshold defining a clutch game |

**Optional parameters:** Inherits the full optional-parameter set of `leaguedashplayerstats` (PlusMinus, PaceAdjust, Rank, LastNGames, Month, OpponentTeamID, Period, PORound, TeamID, DateFrom, DateTo, plus the filter-string fields). See the wrapper docstring in `endpoints/players.py` for the exhaustive list.

**Response envelope:** `resultSets` contains one entry named `LeagueDashPlayerClutch`. Columns are a clutch-filtered subset of `LeagueDashPlayerStats` — same `PLAYER_ID`/`TEAM_ID` natural keys, but statistics reflect only plays that occurred during the specified clutch window.

**Key columns (in `output/players.csv`):** `(PLAYER_ID, SEASON_ID, TEAM_ID)` with implicit clutch-window discriminator stored as a descriptive column (or partitioned into a separate block inside `players.csv` — see `pipelines/ingest_players.py` for the join strategy).

**Wrapper function:** `endpoints.players.fetch_leaguedashplayerclutch`

**Consuming pipeline:** `pipelines/ingest_players.py`

**Destination CSV:** `output/players.csv`

**Cross-reference note:** The same upstream endpoint name (`leaguedashplayerclutch`) is invoked from a second wrapper — `endpoints.lineups.fetch_leaguedashplayerclutch_onoff` — with different parameters to produce Lineups-context on/off-court splits. See [Lineups Domain — `leaguedashplayerclutch` (on/off variant)](#leaguedashplayerclutch-1) below.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor).

### playercareerstats

**Purpose:** Fetch the full career statistical history (season-by-season totals and career aggregates) for a single player.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `PlayerID` | `str` | `"2544"` | NBA Player ID as string (defensive `str()` cast handles int inputs); 4-digit unpadded |

**Optional parameters:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `PerMode` | `str` | `"PerGame"` | Also accepts `"Totals"`, `"Per36"` |
| `LeagueID` | `str` | `"00"` | `"00"` = NBA |

**Response envelope:** `resultSets` contains multiple entries including `SeasonTotalsRegularSeason`, `CareerTotalsRegularSeason`, `SeasonTotalsPostSeason`, `CareerTotalsPostSeason`, `SeasonTotalsAllStarSeason`, `CareerTotalsAllStarSeason`, `SeasonTotalsCollegeSeason`, `CareerTotalsCollegeSeason`, `SeasonTotalsPreseason`, `CareerTotalsPreseason`, `SeasonRankingsRegularSeason`, `SeasonRankingsPostSeason`. The pipeline typically persists only the regular-season totals and career totals tables.

**Key columns (in `output/players.csv`):** `(PLAYER_ID, SEASON_ID)` for per-season rows; career totals rows have a sentinel `SEASON_ID` value.

**Wrapper function:** `endpoints.players.fetch_playercareerstats`

**Consuming pipeline:** `pipelines/ingest_players.py`

**Destination CSV:** `output/players.csv`

**Note:** This endpoint does NOT accept a `Season` parameter — it always returns the full career. The pipeline iterates per-`PLAYER_ID` (typically enumerated from the `LeagueDashPlayerStats` response).

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). With ~500 active players per season, a full per-player enumeration incurs ~500 seconds of mandatory wait.

### playergamelog

**Purpose:** Fetch the game-by-game box-score log for a single player in a given season.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `PlayerID` | `str` | `"2544"` | NBA Player ID |
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |

**Optional parameters:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `DateFrom` / `DateTo` | `str` | `""` | Empty = no date filter |

**Response envelope:** `resultSets` contains one entry named `PlayerGameLog` with columns including `SEASON_ID`, `Player_ID`, `Game_ID`, `GAME_DATE`, `MATCHUP`, `WL`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `STL`, `BLK`, `TOV`, `PF`, `PTS`, `PLUS_MINUS`, `VIDEO_AVAILABLE`.

**Key columns (in `output/players.csv`):** `(PLAYER_ID, GAME_ID)` for per-game rows, joinable to `games.csv` and `schedule.csv` on `GAME_ID`.

**Wrapper function:** `endpoints.players.fetch_playergamelog`

**Consuming pipeline:** `pipelines/ingest_players.py`

**Destination CSV:** `output/players.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Per-player enumeration cost matches `playercareerstats` at ~500 calls per season.

---

## 4. Teams Domain

Three endpoints that produce per-team season aggregates, game logs, and split-dimension dashboards. All three wrappers live in `endpoints/teams.py`. All three are consumed by `pipelines/ingest_teams.py`. The combined output lands in `output/teams.csv`.

### leaguedashteamstats

**Purpose:** Fetch league-wide per-team season-level aggregate statistics (per-game, totals, or advanced).

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PerMode` | `str` | `"PerGame"` | |
| `MeasureType` | `str` | `"Base"` | |

**Optional parameters:** PlusMinus, PaceAdjust, Rank, LastNGames, Month, OpponentTeamID, Period, PORound, TeamID, DateFrom, DateTo, GameSegment, Location, Outcome, SeasonSegment, ShotClockRange, VsConference, VsDivision, Conference, Division, GameScope, PlayerExperience, PlayerPosition, StarterBench, TwoWay — all accept empty strings or zero-strings to indicate "no filter". See the wrapper docstring for the complete enumeration.

**Response envelope:** `resultSets` contains one entry named `LeagueDashTeamStats`. Columns include `TEAM_ID`, `TEAM_NAME`, `GP`, `W`, `L`, `W_PCT`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `TOV`, `STL`, `BLK`, `BLKA`, `PF`, `PFD`, `PTS`, `PLUS_MINUS`.

**Key columns (in `output/teams.csv`):** `(TEAM_ID, SEASON_ID)` per `README.md` §Data Domains/Teams.

**Wrapper function:** `endpoints.teams.fetch_leaguedashteamstats`

**Consuming pipeline:** `pipelines/ingest_teams.py`

**Destination CSV:** `output/teams.csv`

**Note:** This endpoint does NOT require `TeamID` — it returns all 30 teams in a single call. The pipeline typically calls this first to seed the `TEAM_ID` enumeration for the per-team endpoints below.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor).

### teamgamelog

**Purpose:** Fetch the game-by-game log for a specific team in a specific season.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `TeamID` | `str` | `"1610612747"` | NBA Team ID — 10-digit; e.g., Lakers = `"1610612747"`. Defensive `str()` cast handles int inputs |
| `Season` | `str` | `"2025-26"` | |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |

**Optional parameters:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `DateFrom` / `DateTo` | `str` | `""` | Empty = no date filter |

**Response envelope:** `resultSets` contains one entry named `TeamGameLog`. Columns include `Team_ID`, `Game_ID`, `GAME_DATE`, `MATCHUP`, `WL`, `W`, `L`, `W_PCT`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `STL`, `BLK`, `TOV`, `PF`, `PTS`.

**Key columns (in `output/teams.csv`):** `(TEAM_ID, GAME_ID)` when per-game rows are persisted alongside season aggregates.

**Wrapper function:** `endpoints.teams.fetch_teamgamelog`

**Consuming pipeline:** `pipelines/ingest_teams.py`

**Destination CSV:** `output/teams.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). With 30 teams in the league, per-team enumeration incurs ~30 seconds of mandatory wait.

### teamdashboardbygeneralsplits

**Purpose:** Fetch a team dashboard sliced across multiple split dimensions (overall, location, wins/losses, month, pre/post All-Star, days rest).

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `TeamID` | `str` | `"1610612747"` | NBA Team ID |
| `Season` | `str` | `"2025-26"` | |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PerMode` | `str` | `"PerGame"` | |
| `MeasureType` | `str` | `"Base"` | |

**Optional parameters:** PlusMinus, PaceAdjust, Rank, LastNGames, Month, OpponentTeamID, Period, PORound, DateFrom, DateTo, GameSegment, Location, Outcome, SeasonSegment, ShotClockRange, VsConference, VsDivision — all accept empty/zero strings for "no filter".

**Response envelope:** `resultSets` contains MULTIPLE entries: `OverallTeamDashboard`, `LocationTeamDashboard`, `WinsLossesTeamDashboard`, `MonthTeamDashboard`, `PrePostAllStarTeamDashboard`, `DaysRestTeamDashboard`. This is the only Teams-domain endpoint that returns multiple tables in one payload — the normalizer produces multiple DataFrames and the pipeline selects which splits to persist.

**Key columns (in `output/teams.csv`):** `(TEAM_ID, SEASON_ID, GROUP_SET, GROUP_VALUE)` — the `GROUP_SET` column identifies which split (Overall, Location, etc.) each row belongs to.

**Wrapper function:** `endpoints.teams.fetch_teamdashboardbygeneralsplits`

**Consuming pipeline:** `pipelines/ingest_teams.py`

**Destination CSV:** `output/teams.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Per-team enumeration (30 teams) incurs ~30 seconds of mandatory wait.

---

## 5. Games Domain

Four endpoints that produce game-level artifacts: daily scoreboards, per-game box scores (traditional and advanced), and per-game play-by-play event streams. All four wrappers live in `endpoints/games.py`. All four are consumed by `pipelines/ingest_games.py` — the ONLY pipeline that applies Rule 6 (fail-safe iteration). Box scores write to `output/games.csv`; play-by-play writes to `output/play_by_play.csv`.

### scoreboardv2

**Purpose:** Fetch the scoreboard for a specific calendar date — daily snapshot of games, scores, standings, and win probabilities. Supplementary to `leaguegamefinder` for date-partitioned game enumeration.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `GameDate` | `str` | `"2025-10-21"` | ISO-8601 date string (`YYYY-MM-DD`) |
| `LeagueID` | `str` | `"00"` | |
| `DayOffset` | `str` | `"0"` | `"0"` = exact date; `"-1"` = day before; `"1"` = day after |

**Response envelope:** `resultSets` contains multiple entries: `GameHeader`, `LineScore`, `SeriesStandings`, `LastMeeting`, `EastConfStandingsByDay`, `WestConfStandingsByDay`, `Available`, `TeamLeaders`, `TicketLinks`, `WinProbability`.

**Key columns (in `output/games.csv`):** `(GAME_ID, SEASON_ID, TEAM_ID)` per `README.md` §Data Domains/Games. The pipeline derives these from `GameHeader` and `LineScore` tables.

**Wrapper function:** `endpoints.games.fetch_scoreboardv2`

**Consuming pipeline:** `pipelines/ingest_games.py`

**Destination CSV:** `output/games.csv`

**Note:** Primary game enumeration is handled by `endpoints.schedule.enumerate_game_ids` (which wraps `leaguegamefinder`). `scoreboardv2` is an alternative path for incremental runs focused on specific dates.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor).

### boxscoretraditionalv2

**Purpose:** Fetch the traditional box score (points, rebounds, assists, steals, blocks, etc.) for a specific game — per-player and per-team rows.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `GameID` | `str` | `"0022500001"` | 10-character zero-padded string — e.g., `"0022500001"` is the first regular-season game of the 2025-26 season. Defensive `str()` cast preserves the zero-padded format |
| `StartPeriod` | `str` | `"0"` | `"0"` = no lower bound |
| `EndPeriod` | `str` | `"10"` | `"10"` covers regulation (1-4) + up to 6 overtimes (5-10) |
| `StartRange` | `str` | `"0"` | NBA Stats API uses tenths-of-seconds granularity |
| `EndRange` | `str` | `"28800"` | `28800` = 48 minutes × 60 seconds × 10 tenths — full game |
| `RangeType` | `str` | `"0"` | `"0"` = whole game (the default); other values enable sub-period filtering |

**Response envelope:** `resultSets` contains two entries: `PlayerStats` and `TeamStats`.

**Key columns (in `output/games.csv`):** `(GAME_ID, PLAYER_ID)` for `PlayerStats`; `(GAME_ID, TEAM_ID)` for `TeamStats`.

**Wrapper function:** `endpoints.games.fetch_boxscoretraditionalv2`

**Consuming pipeline:** `pipelines/ingest_games.py`

**Destination CSV:** `output/games.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Called once per `GAME_ID` — a full regular season of ~1,230 games incurs ~1,230 seconds of mandatory wait for this endpoint alone.

### boxscoreadvancedv2

**Purpose:** Fetch the advanced box score (offensive/defensive rating, true shooting %, usage %, PIE, etc.) for a specific game.

**Required parameters:** Identical to `boxscoretraditionalv2` — `GameID`, `StartPeriod`, `EndPeriod`, `StartRange`, `EndRange`, `RangeType`.

**Response envelope:** `resultSets` contains two entries: `PlayerStats` and `TeamStats` — schemas parallel the traditional variant but columns reflect advanced metrics (`E_OFF_RATING`, `OFF_RATING`, `E_DEF_RATING`, `DEF_RATING`, `E_NET_RATING`, `NET_RATING`, `AST_PCT`, `AST_TOV`, `AST_RATIO`, `OREB_PCT`, `DREB_PCT`, `REB_PCT`, `TM_TOV_PCT`, `EFG_PCT`, `TS_PCT`, `USG_PCT`, `E_USG_PCT`, `E_PACE`, `PACE`, `PACE_PER40`, `POSS`, `PIE`).

**Key columns (in `output/games.csv`):** `(GAME_ID, PLAYER_ID)` for `PlayerStats`; `(GAME_ID, TEAM_ID)` for `TeamStats`.

**Wrapper function:** `endpoints.games.fetch_boxscoreadvancedv2`

**Consuming pipeline:** `pipelines/ingest_games.py`

**Destination CSV:** `output/games.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Adds a second per-game call atop `boxscoretraditionalv2`.

### playbyplayv2

**Purpose:** Fetch the event-by-event play-by-play log for a specific game — every possession, shot, foul, timeout, substitution recorded as an individual event row.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `GameID` | `str` | `"0022500001"` | 10-character zero-padded |
| `StartPeriod` | `str` | `"0"` | Quarter lower bound |
| `EndPeriod` | `str` | `"10"` | Quarter upper bound |

**Optional parameters:** None — the v2 endpoint does NOT accept the `StartRange`/`EndRange`/`RangeType` triplet that box scores support. Use period bounds only.

**Response envelope:** `resultSets` contains two entries: `PlayByPlay` (the event stream) and `AvailableVideo` (auxiliary). The pipeline persists only `PlayByPlay` rows. Columns include `GAME_ID`, `EVENTNUM`, `EVENTMSGTYPE`, `EVENTMSGACTIONTYPE`, `PERIOD`, `WCTIMESTRING`, `PCTIMESTRING`, `HOMEDESCRIPTION`, `NEUTRALDESCRIPTION`, `VISITORDESCRIPTION`, `SCORE`, `SCOREMARGIN`, `PERSON1TYPE`, `PLAYER1_ID`, `PLAYER1_NAME`, `PLAYER1_TEAM_ID`, `PLAYER1_TEAM_CITY`, `PLAYER1_TEAM_NICKNAME`, `PLAYER1_TEAM_ABBREVIATION`, and parallel `PERSON2*`/`PERSON3*` fields.

**Key columns (in `output/play_by_play.csv`):** `(GAME_ID, EVENTNUM)` per `README.md` §Data Domains — a composite natural key.

**Wrapper function:** `endpoints.games.fetch_playbyplayv2`

**Consuming pipeline:** `pipelines/ingest_games.py`

**Destination CSV:** `output/play_by_play.csv`

**Note:** `playbyplayv2` is the v2 endpoint; the legacy `playbyplay` (v1) is deprecated and NOT used. The v2 variant returns richer metadata (player marks, team-affiliation fields per event participant).

**Note on Rule 4 enforcement:** Play-by-play payloads can carry per-event coordinate or metadata fields that appear list-like upstream. The `utils/schema_normalizer.py` function asserts every cell is scalar before return; any violation is a signal of upstream schema change and MUST be investigated rather than silently coerced.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Called once per `GAME_ID` — a full regular season of ~1,230 games incurs ~1,230 seconds of mandatory wait for this endpoint alone. Combined with the two box-score endpoints, the Games pipeline's per-season mandatory wait budget is ~3,690 seconds (~62 minutes).

---

## 6. Lineups Domain

Two endpoints that produce five-man on-court lineup aggregates and on/off-court impact splits. Both wrappers live in `endpoints/lineups.py`. Both are consumed by `pipelines/ingest_lineups.py`. The combined output lands in `output/lineups.csv`.

### leaguedashlineups

**Purpose:** Fetch league-wide multi-player lineup aggregates (five-man units by default; also supports two-, three-, and four-man groupings) with efficiency metrics.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PerMode` | `str` | `"PerGame"` | |
| `MeasureType` | `str` | `"Base"` | |
| `GroupQuantity` | `str` | `"5"` | N-man lineup size; `"5"` = standard starting-five analysis; also accepts `"2"`, `"3"`, `"4"` |

**Optional parameters:** PlusMinus, PaceAdjust, Rank, LastNGames, Month, OpponentTeamID, Period, PORound, TeamID, DateFrom, DateTo, GameSegment, Location, Outcome, SeasonSegment, ShotClockRange, VsConference, VsDivision, Conference, Division, GameScope, PlayerExperience, PlayerPosition, StarterBench, TwoWay — all accept empty/zero strings for "no filter".

**Response envelope:** `resultSets` contains one entry named `Lineups`. Columns include `GROUP_SET`, `GROUP_ID`, `GROUP_NAME`, `TEAM_ID`, `TEAM_ABBREVIATION`, `GP`, `W`, `L`, `W_PCT`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `TOV`, `STL`, `BLK`, `BLKA`, `PF`, `PFD`, `PTS`, `PLUS_MINUS`.

**Key columns (in `output/lineups.csv`):** `(GROUP_ID, SEASON_ID, TEAM_ID)` per `README.md` §Data Domains/Lineups. `GROUP_ID` is a hyphen-delimited composite of player IDs (e.g., `"-201939-202681-203081-203507-203954-"`) — Rule 4 requires this string be preserved as a scalar cell, NEVER decomposed into a list.

**Wrapper function:** `endpoints.lineups.fetch_leaguedashlineups`

**Consuming pipeline:** `pipelines/ingest_lineups.py`

**Destination CSV:** `output/lineups.csv`

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor).

### leaguedashplayerclutch

**Purpose:** Fetch clutch-time player statistics with parameter profile tuned for on/off-court lineup-context splits. This subsection documents the **Lineups-domain usage** of the `leaguedashplayerclutch` endpoint — the same upstream endpoint is also invoked from the Players domain via a different wrapper with different parameter defaults (see [Players Domain — `leaguedashplayerclutch`](#leaguedashplayerclutch) above).

**Required parameters:** Same shape as Players-domain variant — `Season`, `SeasonType`, `LeagueID`, `PerMode`, `MeasureType`, `ClutchTime`, `AheadBehind`, `PointDiff`. The Lineups pipeline uses the same defaults (`ClutchTime="Last 5 Minutes"`, `AheadBehind="Ahead or Behind"`, `PointDiff="5"` — the NBA's canonical clutch definition) but downstream logic computes on/off-court aggregation per lineup rather than per-player.

**Optional parameters:** Inherits the full optional-parameter set of `leaguedashplayerstats` (PlusMinus, PaceAdjust, Rank, LastNGames, Month, OpponentTeamID, Period, PORound, TeamID, DateFrom, DateTo, plus the extensive filter-string fields).

**Response envelope:** `resultSets` contains one entry named `LeagueDashPlayerClutch`. Columns are the clutch-filtered subset of `LeagueDashPlayerStats` shown in the Players-domain subsection above.

**Key columns (in `output/lineups.csv`):** `(PLAYER_ID, TEAM_ID, SEASON_ID)` for the raw clutch rows; the pipeline aggregates these into on/off-court contributions per lineup before final persist.

**Wrapper function:** `endpoints.lineups.fetch_leaguedashplayerclutch_onoff`

**Consuming pipeline:** `pipelines/ingest_lineups.py`

**Destination CSV:** `output/lineups.csv`

**Disambiguation:** AAP §0.1.1 explicitly counts `leaguedashplayerclutch` once in Players (basic clutch splits) and once in Lineups (on/off-court context) to achieve the 15-endpoint total across 6 domains. The upstream endpoint name is literally identical; the two wrappers differ in their consuming-pipeline semantics, not in their HTTP surface. Both endpoint wrappers share `api.nba_client.NBAClient.get("leaguedashplayerclutch", params)` as their delegate call — only the `params` dict content differs between invocations.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). The Players pipeline and the Lineups pipeline each call this endpoint independently; the rate-limit floor applies independently to each invocation because `NBAClient` does not cache responses across calls.

---


## 7. Tracking Domain

One endpoint that produces SportVU-derived player tracking statistics (speed, distance, touches, defensive matchup counts, etc.). The wrapper lives in `endpoints/players.py` (file-location rationale: the tracking endpoint accepts a `PlayerOrTeam` parameter and is most naturally grouped with the other player-scoped endpoints). The output lands in `output/player_tracking.csv`.

### leaguedashptstats

**Purpose:** Fetch league-wide player tracking statistics for a season. Tracking metrics are SportVU/optical-tracking-derived and include measures that traditional box scores do not capture — speed, distance covered, touches, passing behavior, defensive matchups, paint touches, etc.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PerMode` | `str` | `"PerGame"` | Also accepts `"Totals"` |
| `PtMeasureType` | `str` | `"SpeedDistance"` | Valid: `"SpeedDistance"`, `"Rebounding"`, `"Possessions"`, `"CatchShoot"`, `"PullUpShot"`, `"Defense"`, `"Drives"`, `"Passing"`, `"ElbowTouch"`, `"PostTouch"`, `"PaintTouch"`, `"Efficiency"` |
| `PlayerOrTeam` | `str` | `"Player"` | Also accepts `"Team"` for team-level tracking aggregates |

**Optional parameters:** LastNGames, Month, OpponentTeamID, TeamID, DateFrom, DateTo, GameScope, Location, Outcome, SeasonSegment, VsConference, VsDivision, College, Conference, Country, DraftPick, DraftYear, Division, Height, PlayerExperience, PlayerPosition, StarterBench, Weight — all accept empty/zero strings for "no filter".

**Response envelope:** `resultSets` contains one entry named `LeagueDashPtStats`. Columns vary by `PtMeasureType` — for example, `PtMeasureType="SpeedDistance"` returns `PLAYER_ID`, `PLAYER_NAME`, `TEAM_ID`, `TEAM_ABBREVIATION`, `GP`, `W`, `L`, `MIN`, `DIST_FEET`, `DIST_MILES`, `DIST_MILES_OFF`, `DIST_MILES_DEF`, `AVG_SPEED`, `AVG_SPEED_OFF`, `AVG_SPEED_DEF`. Other measure types produce different column sets — see the upstream documentation for the per-measure schema.

**Key columns (in `output/player_tracking.csv`):** `(PLAYER_ID, SEASON_ID, TEAM_ID)` per `README.md` §Data Domains/Tracking. For richer tracking coverage, the pipeline may iterate across all `PtMeasureType` values and wide-join the results on `PLAYER_ID`.

**Wrapper function:** `endpoints.players.fetch_leaguedashptstats`

**Consuming pipeline:** `pipelines/ingest_players.py`

**Destination CSV:** `output/player_tracking.csv` (distinct from `players.csv`)

**Domain-file placement rationale:** Although the Tracking domain is logically separate from the Players domain (the product brief §2 enumerates six domains: players, teams, games, lineups, tracking, schedule), the Tracking endpoint wrapper is physically located in `endpoints/players.py` because its `PlayerOrTeam` parameter surface naturally belongs alongside the other per-player wrappers. The pipeline and CSV output, however, remain distinct: `pipelines/ingest_players.py` emits BOTH `players.csv` (from the four Players-domain endpoints above) AND `player_tracking.csv` (from this Tracking-domain endpoint) as separate artifacts with distinct key columns.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). A single `PtMeasureType` requires one call; full tracking coverage across all 12 measure types requires 12 calls (~12 seconds of mandatory wait).

---

## 8. Schedule Domain

One endpoint that enumerates the season's game-level metadata (dates, teams, game IDs). The wrapper lives in `endpoints/schedule.py`. The output lands in `output/schedule.csv`, and — critically — the derived helper `endpoints.schedule.enumerate_game_ids(client, season)` seeds the Games pipeline's per-`GAME_ID` iteration loop per AAP §0.4.5.

### leaguegamefinder

**Purpose:** Fetch the season's complete game metadata — every regular-season, preseason, and playoff game identified by `GAME_ID`, `GAME_DATE`, participating teams, and matchup description. This is the foundational reference dataset that anchors all other pipelines via `GAME_ID` joins.

**Required parameters:**

| Name | Type | Example | Notes |
|---|---|---|---|
| `Season` | `str` | `"2025-26"` | Format `YYYY-YY` |
| `SeasonType` | `str` | `"Regular Season"` | |
| `LeagueID` | `str` | `"00"` | |
| `PlayerOrTeam` | `str` | `"T"` | `"T"` = team-level rows (one row per team per game); `"P"` = player-level rows. The pipeline uses `"T"` by convention |

**Optional parameters:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `PlayerID` | `str` | `""` | Empty when `PlayerOrTeam="T"` |
| `TeamID` | `str` | `""` | Empty = all teams |
| `Outcome` / `Location` / `VsConference` / `VsDivision` / `Conference` / `Division` / `SeasonSegment` / `GameID` / `DateFrom` / `DateTo` | `str` | `""` | Filter fields — empty = no filter |

**Response envelope:** `resultSets` contains one entry named `LeagueGameFinderResults`. Columns include `SEASON_ID`, `TEAM_ID`, `TEAM_ABBREVIATION`, `TEAM_NAME`, `GAME_ID`, `GAME_DATE`, `MATCHUP`, `WL`, `MIN`, `FGM`, `FGA`, `FG_PCT`, `FG3M`, `FG3A`, `FG3_PCT`, `FTM`, `FTA`, `FT_PCT`, `OREB`, `DREB`, `REB`, `AST`, `STL`, `BLK`, `TOV`, `PF`, `PTS`, `PLUS_MINUS`. When `PlayerOrTeam="T"`, each game appears twice in the response — once per participating team.

**Key columns (in `output/schedule.csv`):** `(GAME_ID, SEASON_ID, HOME_TEAM_ID, AWAY_TEAM_ID)` after deduplication and team-role assignment. Alternatively, `(GAME_ID, TEAM_ID)` if per-team rows are preserved — see `pipelines/ingest_schedule.py` for the chosen shape.

**Wrapper function:** `endpoints.schedule.fetch_leaguegamefinder`

**Derived helper:** `endpoints.schedule.enumerate_game_ids(client, season, ...) -> List[str]` — called by `pipelines/ingest_games.py` at the top of its `run()` function to obtain the deduplicated, first-seen-ordered list of `GAME_ID` strings for the season. This is the sole cross-pipeline dependency in the system (AAP §0.4.5: Schedule → Games).

**Consuming pipelines:**
- `pipelines/ingest_schedule.py` — for `output/schedule.csv` emission.
- `pipelines/ingest_games.py` — via `enumerate_game_ids`, for per-`GAME_ID` iteration driving `boxscoretraditionalv2`, `boxscoreadvancedv2`, and `playbyplayv2` calls.

**Destination CSV:** `output/schedule.csv`

**Note on deduplication:** `leaguegamefinder` with `PlayerOrTeam="T"` returns roughly 2,460 rows for a full NBA regular season (~1,230 games × 2 team rows per game). The `enumerate_game_ids` helper deduplicates to ~1,230 unique `GAME_ID`s. The `schedule.csv` emission may preserve both team-role rows (for easy home/away joins) or deduplicate — see `pipelines/ingest_schedule.py` for the chosen convention.

**Rate-limit note:** subject to Rule 2 (≥ 1.0s floor). Only one call per season — this is the fastest pipeline by wall-clock time.

---

## 9. Cross-Cutting Notes

### Rule 1 Enforcement — Request Flow

Every endpoint call follows this exact chain:

```
pipelines/ingest_*.py
   │
   ├── endpoints/<domain>.fetch_<endpoint>(client, ...)
   │       │
   │       └── api.nba_client.NBAClient.get(endpoint, params)
   │               │
   │               ├── utils.rate_limiter.RateLimiter.wait()   ← Rule 2
   │               ├── attach config.REQUIRED_HEADERS           ← Rule 3
   │               ├── @tenacity.retry(...)                     ← F-004 retry
   │               └── requests.Session.get(url, params, ...)   ← Rule 1 SOLE SITE
   │
   └── (back to pipeline)
```

No module outside `api/nba_client.py` calls `requests.*` in production code. Verified by `tests/invariants/test_rule1_sole_http_client.py` via `grep -rn "requests\.\(get\|post\|Session\)"`.

### Response-Envelope Shape — `resultSets`

Every endpoint returns the same top-level JSON shape:

```json
{
  "resource": "<endpoint_name>",
  "parameters": { "Season": "2025-26", "SeasonType": "Regular Season", ... },
  "resultSets": [
    {
      "name": "<TableName>",
      "headers": ["COL1", "COL2", "COL3", ...],
      "rowSet": [
        [v1, v2, v3, ...],
        [v1, v2, v3, ...],
        ...
      ]
    },
    ...
  ]
}
```

Flattening is performed by `utils.schema_normalizer.normalize_result_sets(payload)` which iterates `payload["resultSets"]`, constructs `pandas.DataFrame(entry["rowSet"], columns=entry["headers"])` per entry, and asserts `df.applymap(lambda x: isinstance(x, (dict, list))).any().any() == False` before returning a `Dict[str, pandas.DataFrame]` keyed by table name.

### Season String Format

The `Season` parameter uses the `YYYY-YY` convention that NBA.com and the NBA Stats API recognize:

| Season | Parameter Value | Notes |
|---|---|---|
| 2025-26 | `"2025-26"` | Default in `config.DEFAULT_SEASON` |
| 2024-25 | `"2024-25"` | |
| 1999-2000 | `"1999-00"` | Two-digit end year wraps at century boundary |

The pipeline does NOT attempt to validate season strings upstream — invalid values produce empty `rowSet` arrays, which the normalizer handles gracefully.

### Error Taxonomy — Per-Endpoint Behavior

| Error Class | `api/nba_client.py` Behavior | Pipeline Behavior |
|---|---|---|
| HTTP 429 Too Many Requests | Retried by `tenacity` with exponential backoff + jitter | Transparent |
| HTTP 5xx (500, 502, 503, 504) | Retried by `tenacity` | Transparent |
| HTTP 4xx non-429 (400, 401, 403, 404) | Propagates immediately (not retried) | Abort (or Rule 6 catch in `ingest_games.py`) |
| `requests.exceptions.Timeout` | Retried by `tenacity` | Transparent |
| `requests.exceptions.ConnectionError` | Retried by `tenacity` | Transparent |
| `utils.schema_normalizer` Rule 4 assertion | Raised post-response | Abort (signals upstream schema change; investigate) |
| `CSVWriter` I/O error | Raised by `pandas.to_csv` | Abort (environmental; operator fix required) |
| `CheckpointManager` I/O error | Raised by `json.dump`/`Path.replace` | Abort (Rule 5 integrity) |

### Rate Limit Strategy

**Proactive control:** `utils/rate_limiter.py::RateLimiter.wait()` enforces a minimum gap of `config.RATE_LIMIT_SECONDS` (default `1.0`) between consecutive calls by calling `time.sleep(delta)` when the elapsed-since-last-call time is below the floor. This is the primary control.

**Reactive control:** `tenacity` retry decorator in `api/nba_client.py::NBAClient.get` catches HTTP 429, 5xx, timeouts, and connection errors; retries with exponential backoff (`wait_exponential(multiplier=config.RETRY_MULTIPLIER, max=config.RETRY_MAX_WAIT)`) up to `config.RETRY_ATTEMPTS` attempts.

**Combined effect:** Under normal operation, the 1.0-second floor is sufficient to avoid HTTP 429 responses entirely — Validation Gate 8 specifically verifies "zero 429s during a full games run against the live API". If 429s are observed in practice, raise `config.RATE_LIMIT_SECONDS` to `1.5` or `2.0` as the first mitigation.

### Cross-Domain Dependency — Schedule → Games

Per AAP §0.4.5, the Games pipeline depends on the Schedule pipeline for `GAME_ID` enumeration:

- `pipelines/ingest_games.py::run()` calls `endpoints.schedule.enumerate_game_ids(client, season)` at the top of its execution to obtain the deduplicated list of `GAME_ID` strings.
- `run.py all` dispatches pipelines in dependency order: `schedule → games → teams → players → lineups` so that `output/schedule.csv` is materialized before Games begins.
- Standalone `python run.py games --season <season>` still works without a prior `schedule` invocation — Games re-enumerates `GAME_IDs` fresh against the live API. The dependency is expressed through the shared `enumerate_game_ids` function call, NOT through reading `schedule.csv`.

### Endpoint Reuse — `leaguedashplayerclutch`

The upstream endpoint `leaguedashplayerclutch` is invoked from TWO distinct wrapper functions in this codebase:

| Wrapper | Module | Usage Context |
|---|---|---|
| `fetch_leaguedashplayerclutch` | `endpoints/players.py` | Players-domain clutch splits (per-player basic aggregates during clutch time) |
| `fetch_leaguedashplayerclutch_onoff` | `endpoints/lineups.py` | Lineups-domain on/off-court splits (same endpoint, different downstream aggregation) |

Both wrappers call `NBAClient.get("leaguedashplayerclutch", params)` — they differ in the `params` dict and in the consuming pipeline's post-processing logic. AAP §0.1.1 counts the endpoint once toward the Players 5-endpoint tally and once toward the Lineups 2-endpoint tally, summing to the 15-endpoint total.

---

## 10. Links

### Back-references

- [`../ONBOARDING.md`](../ONBOARDING.md) — clean-machine setup, domain context, common pitfalls, extension patterns.
- [`../TRACEABILITY.md`](../TRACEABILITY.md) — bidirectional feature/rule/gate/file traceability matrix; every endpoint in this catalog maps back to a feature ID and validation gate there.
- [`../OBSERVABILITY.md`](../OBSERVABILITY.md) — per-endpoint metric names (`nba_requests_total{endpoint=...}`, `nba_request_failures_total`, `nba_retries_total`), log format, correlation-ID propagation.
- [`../DECISIONS.md`](../DECISIONS.md) — decision log including endpoint-level design choices (empty-string-as-filter convention, `leaguedashplayerclutch` dual-wrapping, `scoreboardv2` vs `leaguegamefinder` enumeration).

### Feature deep-dives

- [`../features/players.md`](../features/players.md) — F-009 Players pipeline narrative (consumes endpoints 1-4 and 14).
- [`../features/teams.md`](../features/teams.md) — F-010 Teams pipeline narrative (consumes endpoints 5-7).
- [`../features/games.md`](../features/games.md) — F-011 Games pipeline narrative (consumes endpoints 8-11; includes the Rule 6 fail-safe iteration discussion).
- [`../features/lineups.md`](../features/lineups.md) — F-012 Lineups pipeline narrative (consumes endpoints 12-13).
- [`../features/schedule.md`](../features/schedule.md) — F-013 Schedule pipeline narrative (consumes endpoint 15; producer of `enumerate_game_ids` for F-011).

### Source code

- [`../../endpoints/players.py`](../../endpoints/players.py) — wrappers for endpoints 1-4 and 14.
- [`../../endpoints/teams.py`](../../endpoints/teams.py) — wrappers for endpoints 5-7.
- [`../../endpoints/games.py`](../../endpoints/games.py) — wrappers for endpoints 8-11.
- [`../../endpoints/lineups.py`](../../endpoints/lineups.py) — wrappers for endpoints 12-13.
- [`../../endpoints/schedule.py`](../../endpoints/schedule.py) — wrapper for endpoint 15, plus the `enumerate_game_ids` helper.
- [`../../api/nba_client.py`](../../api/nba_client.py) — the single HTTP client (Rule 1) that every endpoint wrapper delegates to.
- [`../../config.py`](../../config.py) — `API_BASE_URL`, `REQUIRED_HEADERS`, `RATE_LIMIT_SECONDS`, `RETRY_*` constants referenced throughout this catalog.

