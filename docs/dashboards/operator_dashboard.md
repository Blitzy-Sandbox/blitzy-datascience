# NBA Pipeline — Operator Dashboard (Markdown fallback)

Audience: operators running the NBA Data Ingestion Pipeline locally, on a CI box,
on an SSH-reachable server, or anywhere else that lacks a Grafana installation.

This document is the Grafana-free equivalent of [`operator_dashboard.json`](./operator_dashboard.json).
If you have Grafana, import [`operator_dashboard.json`](./operator_dashboard.json). Otherwise this Markdown
template, combined with periodic `python run.py metrics` invocations, gives you the
same operational picture: the six Prometheus counters emitted by `utils/metrics.py`,
the PromQL expressions that produce Grafana panels, and the `grep`-based CLI
equivalents that produce the same insight from raw `python run.py metrics` output.

The conceptual parent for this dashboard is [`../OBSERVABILITY.md`](../OBSERVABILITY.md); the
counter names, log format, correlation-ID mechanism, and health/readiness surface
are defined there. This file is the visualization layer.

---

## 1. When to use this dashboard

- **Prefer `operator_dashboard.json`** if you already have Grafana and a Prometheus
  server scraping the pipeline host. That file renders the six counters plus the
  two companion histograms as time-series panels.
- **Use this Markdown file** if running locally, on a CI box, or anywhere without
  Grafana — combine the tables below with `python run.py metrics` invocations to
  produce the same operational picture on a developer laptop.
- **Use the runbook snippets** (Section 4) to diagnose anomalies surfaced by any
  of the six counters. Each snippet is copy-paste ready and requires only a
  terminal plus `logs/pipeline.log` open in another pane.

**Cross-reference note.** The six Prometheus counters are emitted by
`utils/metrics.py` and exposed via `python run.py metrics` in Prometheus text
exposition format (version 0.0.4). There is no HTTP scrape endpoint in the
current scope (AAP §0.6.2.4); every sample command in this document therefore
uses process substitution (`<(python run.py metrics)`) or redirection, never
`curl localhost:9090`. A future phase may introduce an HTTP `/metrics`
endpoint; the counter names and types in Section 2 will remain stable across
that change.

---

## 2. Quick-glance indicator table

| Indicator | Counter | PromQL (Grafana) | CLI snapshot (local) | How to read it | Action when anomalous |
|---|---|---|---|---|---|
| Total requests issued | `nba_requests_total` | `sum by (endpoint) (rate(nba_requests_total[5m]))` | `grep nba_requests_total <(python run.py metrics)` | Should match the expected endpoint × game count for the season. Steadily rises during a run. | If too low, the pipeline exited early — check `logs/pipeline.log` for WARNING/ERROR lines. |
| Permanent transport failures | `nba_request_failures_total` | `sum by (endpoint) (rate(nba_request_failures_total[5m]))` | `grep nba_request_failures_total <(python run.py metrics)` | Should be 0 after a clean run. Increments only after tenacity exhausts `RETRY_ATTEMPTS`. | If ≥ 1 with `reason=http_4xx_non_429`, check endpoint deprecation. If ≥ 1 with `reason=timeout`, raise `REQUEST_TIMEOUT_SECONDS`. |
| Transient retry pressure | `nba_retries_total` | `sum(rate(nba_retries_total[5m])) / clamp_min(sum(rate(nba_requests_total[5m])), 1)` | `grep nba_retries_total <(python run.py metrics)` | Expected > 0 under light load. Ratio to requests (`nba_retries_total / nba_requests_total`) is the real signal. | Ratio > 0.2 signals upstream instability or rate-limit contention; raise `RATE_LIMIT_SECONDS` from 1.0 toward 2.0 and re-run. |
| CSV rows written | `pipeline_rows_written_total` | `sum by (pipeline) (increase(pipeline_rows_written_total[1h]))` | `grep pipeline_rows_written_total <(python run.py metrics)` | Monotonically increasing during a run, broken down by pipeline and artifact. | Zero rows for a completed pipeline signals a normalizer drop (Rule 4 violation fixed upstream of the writer) or a bad `--season` parameter. |
| Pipeline invocations | `pipeline_runs_total` | `sum by (outcome) (increase(pipeline_runs_total[24h]))` | `grep pipeline_runs_total <(python run.py metrics)` | Increments once per pipeline per CLI invocation. `outcome=success` vs `outcome=error` shows aggregate health. | Mismatch with expected run count signals a dispatch bug in `run.py` or an exception escaping a pipeline (Rule 6 is scoped to Games only — all other pipelines propagate). |
| Games absorbed by Rule 6 | `games_failed_total` | `sum(increase(games_failed_total[24h]))` | `grep games_failed_total <(python run.py metrics)` | Per-`GAME_ID` failures swallowed by the Rule 6 try/except in `pipelines/ingest_games.py`. Expected small; 0 is ideal. | Tail `logs/pipeline.log \| grep 'game .* failed'` — the failing `GAME_ID`s and exception classes are logged at WARNING. |

> **Note.** Counter names and labels are authoritative and must match the emitter
> declarations in `utils/metrics.py`. Any edit here without the corresponding
> change in `utils/metrics.py` will desynchronize the dashboard from the source
> of truth. The same discipline applies to [`./operator_dashboard.json`](./operator_dashboard.json)
> and the metrics catalog in [`../OBSERVABILITY.md`](../OBSERVABILITY.md).

---

## 3. Alerting thresholds

For Prometheus Alertmanager or external monitoring integrations, the following
thresholds define operational boundaries for the six counters. These thresholds
are advisory; tune per deployment.

| Alert | Condition | Severity | Rationale |
|---|---|---|---|
| NoPermanentFailures | `sum(increase(nba_request_failures_total[24h])) > 0` | Warning | Any permanent failure in 24h indicates tenacity exhausted all retries; investigate endpoint health. |
| RetryPressureHigh | `sum(rate(nba_retries_total[5m])) / clamp_min(sum(rate(nba_requests_total[5m])), 1) > 0.2` | Warning | Retry ratio exceeding 20% signals chronic upstream instability or insufficient `RATE_LIMIT_SECONDS`. |
| ZeroRequests | `sum(increase(nba_requests_total[10m])) == 0` during active run | Info | If a run is marked active but emits no requests for 10 minutes, it may be stalled before `rate_limiter.wait()`. |
| GamesRule6Breach | `sum(increase(games_failed_total[1h])) > 10` | Warning | More than 10 absorbed per-game failures per hour suggests a systemic endpoint problem upstream of Rule 6 absorption. |
| PipelineErrorOutcome | `increase(pipeline_runs_total{outcome="error"}[24h]) > 0` | Critical | Any error-outcome pipeline run means a non-Games pipeline propagated an exception; operator intervention required. |
| ZeroRowsWritten | `sum(increase(pipeline_rows_written_total[1h])) == 0` during active run | Warning | A run in progress that writes zero rows for an hour usually means the normalizer is dropping everything; correlate with ERROR logs. |

> **Note.** These thresholds are illustrative. Prometheus Alertmanager YAML is
> explicitly out of scope for this phase (AAP §0.6.2.4); future phases may
> materialize these rules into a `prometheus_rules.yml` artifact. Until then,
> operators should monitor the six counters via `python run.py metrics` and
> correlate anomalies against `logs/pipeline.log` using the runbook snippets
> in Section 4.

---

## 4. Runbook snippets

Shell commands that diagnose the most common failure modes surfaced by the six
counters. Every snippet assumes the operator is in the repository root with the
virtual environment activated (`source .venv/bin/activate`).

### 4.1 "Something's stalled"

```bash
tail -F logs/pipeline.log
# In another terminal:
python run.py metrics | grep -E "nba_requests_total|nba_retries_total"
```

Look for the correlation ID repeating without `pipeline_rows_written_total`
increasing. If log progress has halted but `nba_retries_total` is incrementing,
the client is locked in a backoff loop — Ctrl+C and re-run; the checkpoint
(`output/checkpoint.json`) preserves progress per Rule 5. If the log is
completely silent for more than 60 seconds and `nba_requests_total` is not
rising either, the pipeline is blocked before the HTTP layer — check
`python run.py ready` to validate local preconditions.

### 4.2 "HTTP 429 flood"

```bash
grep -E "status=429|Too Many Requests" logs/pipeline.log
# Count retries attributable to 429:
grep nba_retries_total <(python run.py metrics)
# Remediate:
RATE_LIMIT_SECONDS=2.0 python run.py all --season 2025-26
```

Raising `RATE_LIMIT_SECONDS` doubles the proactive inter-request floor
(default 1.0s). Gate 8 requires zero 429s across a live `games` run;
persistent 429s indicate the proactive floor is insufficient for the
current upstream weather. If raising the floor to 2.0s does not resolve the
flood, capture a reproducible timestamp range from `logs/pipeline.log` and
defer the run — NBA Stats API rate limits fluctuate during nationally
broadcast game windows.

### 4.3 "Zero rows written"

```bash
python run.py metrics | grep pipeline_rows_written_total
# If output shows 0 rows, sanity-check the season:
python run.py schedule --season 2025-26
ls -lh output/schedule.csv
wc -l output/schedule.csv
```

`schedule` is the smallest live smoke — a single endpoint call producing one
CSV. If this succeeds with > 0 rows, the `--season` parameter format is
valid and upstream is reachable. If `schedule.csv` has only a header row,
verify the season string format (`YYYY-YY`, e.g., `2025-26`). If the
`pipeline_rows_written_total` series is missing an expected label entirely
(for example, no row for `pipeline="ingest_players"`), the pipeline never
reached its first successful `CSVWriter.write` call — grep the log for the
pipeline's `run_start` line to find the failure point.

### 4.4 "Rule 6 absorption spike"

```bash
grep games_failed_total <(python run.py metrics)
grep -E "game .* failed" logs/pipeline.log | awk '{print $NF}' | sort | uniq -c | sort -rn | head
# Identify correlation-ID for the current run:
grep "run start" logs/pipeline.log | tail -1
```

Aggregates failure counts by exception class or `GAME_ID`. A cluster of
failures sharing a single exception class points to a systemic endpoint
issue upstream of the per-game try/except block (e.g., a deprecated query
parameter for `boxscoreadvancedv2`). The correlation ID scopes any follow-up
`grep` invocations to a single run. Rule 6 is working as designed when
`games_failed_total` > 0 **and** `pipeline_runs_total{pipeline="ingest_games",outcome="success"}` == 1
in the same invocation — per-game failures are absorbed; the pipeline still
reports `success`.

### 4.5 "Full correlation-ID trace"

```bash
# Replace CORR with the correlation ID from `run start` line:
grep "corr=CORR" logs/pipeline.log > /tmp/run-CORR.log
wc -l /tmp/run-CORR.log
grep -E "GET |status=|WARNING|ERROR" /tmp/run-CORR.log | head -50
```

The structured log format `%(asctime)s %(levelname)s corr=%(correlation_id)s %(name)s %(message)s`
enables single-run isolation. Combined with the metrics output, a
correlation-ID slice of the log gives the full story of a single CLI
invocation without separate request IDs or span IDs. The
`X-Correlation-ID` request header (documented in
[`../OBSERVABILITY.md`](../OBSERVABILITY.md) "Tracing Across Service Boundaries") carries the
same UUID to any on-path network tooling that wants to correlate the wire
trace with these log lines.

---

## 5. Text-mode snapshot example

What a healthy mid-run `python run.py metrics` output looks like, annotated
line-by-line. Copy the output of `python run.py metrics` on a real run and
compare it against this template to spot deviations.

```
# HELP nba_requests_total Total HTTPS GETs issued to the NBA Stats API
# TYPE nba_requests_total counter
nba_requests_total{endpoint="leaguegamefinder"} 1
nba_requests_total{endpoint="leaguedashplayerstats"} 1
nba_requests_total{endpoint="leaguedashteamstats"} 1
nba_requests_total{endpoint="boxscoretraditionalv2"} 1230
nba_requests_total{endpoint="boxscoreadvancedv2"} 1230
nba_requests_total{endpoint="playbyplayv2"} 1230

# HELP nba_request_failures_total Transport-level NBA Stats API request failures
# TYPE nba_request_failures_total counter
nba_request_failures_total{endpoint="boxscoretraditionalv2",reason="http_5xx"} 2

# HELP nba_retries_total Number of tenacity retry attempts
# TYPE nba_retries_total counter
nba_retries_total{endpoint="boxscoretraditionalv2"} 37
nba_retries_total{endpoint="playbyplayv2"} 12

# HELP pipeline_rows_written_total Rows written by pipelines to CSV artifacts
# TYPE pipeline_rows_written_total counter
pipeline_rows_written_total{pipeline="ingest_schedule",artifact="schedule.csv"} 1230
pipeline_rows_written_total{pipeline="ingest_games",artifact="games.csv"} 1228
pipeline_rows_written_total{pipeline="ingest_games",artifact="play_by_play.csv"} 564812
pipeline_rows_written_total{pipeline="ingest_teams",artifact="teams.csv"} 30
pipeline_rows_written_total{pipeline="ingest_players",artifact="players.csv"} 572

# HELP pipeline_runs_total Pipeline invocations by outcome
# TYPE pipeline_runs_total counter
pipeline_runs_total{pipeline="ingest_schedule",outcome="success"} 1
pipeline_runs_total{pipeline="ingest_games",outcome="success"} 1
pipeline_runs_total{pipeline="ingest_teams",outcome="success"} 1
pipeline_runs_total{pipeline="ingest_players",outcome="success"} 1
pipeline_runs_total{pipeline="ingest_lineups",outcome="success"} 1

# HELP games_failed_total Games whose ingestion failed in the Rule 6 per-game try/except
# TYPE games_failed_total counter
games_failed_total{reason="http_5xx"} 2
```

### How to read this snapshot

- `nba_requests_total{endpoint="boxscoretraditionalv2"} 1230`: the pipeline has
  issued exactly 1230 GETs to the `boxscoretraditionalv2` endpoint — one per
  enumerated `GAME_ID`.
- `nba_request_failures_total{...,reason="http_5xx"} 2`: tenacity exhausted
  `RETRY_ATTEMPTS` (default 5) twice for this endpoint; both failed calls were
  5xx responses.
- `nba_retries_total{endpoint="boxscoretraditionalv2"} 37`: 37 retry attempts
  total across all GETs to this endpoint; paired with 1230 requests that is a
  3% retry ratio — healthy.
- `pipeline_rows_written_total{pipeline="ingest_games",artifact="games.csv"} 1228`:
  1228 rows in `games.csv`, 2 fewer than the 1230 `GAME_ID`s because two games
  were absorbed by Rule 6.
- `pipeline_runs_total{pipeline="ingest_games",outcome="success"} 1`: the
  Games pipeline completed with `outcome=success` despite the 2 absorbed
  failures — this is the precise behavior Rule 6 prescribes.
- `games_failed_total{reason="http_5xx"} 2`: both absorbed failures were
  HTTP 5xx responses — matches the `nba_request_failures_total` count and
  gives confidence that the failures are upstream-transient, not
  schema-breaking.

> **Consistency note.** The whole picture is internally consistent:
> 1230 requested − 2 absorbed = 1228 rows written. If these numbers ever
> disagree by more than the absorbed-failure count, investigate the
> normalizer or writer for silent drops. The inequality
> `rows_written(games.csv) == requests(boxscoretraditionalv2) − games_failed_total`
> is a useful post-run audit that any operator can perform directly from
> the metrics exposition snapshot.

---

## 6. Related documents

- [`../OBSERVABILITY.md`](../OBSERVABILITY.md) — Conceptual parent. Defines the log format, the
  correlation-ID mechanism, the six Prometheus counters, and the
  health/readiness surface this dashboard visualizes.
- [`./operator_dashboard.json`](./operator_dashboard.json) — Grafana JSON version of this dashboard.
  Importable via *Dashboards → Import → Upload JSON* with a Prometheus data
  source selected at import time.
- [`../ONBOARDING.md`](../ONBOARDING.md) — Clean-machine setup guide. Includes quickstart for
  exercising the observability surface locally (`python run.py health`,
  `python run.py ready`, `python run.py metrics`).
- [`../TRACEABILITY.md`](../TRACEABILITY.md) — Bidirectional traceability matrix. Both files in
  `docs/dashboards/` are listed under the Observability rule row.
- [`../DECISIONS.md`](../DECISIONS.md) — Decision log. Records why counters use labels
  (`endpoint`, `reason`, `pipeline`, `artifact`, `outcome`) rather than being
  pre-aggregated.
