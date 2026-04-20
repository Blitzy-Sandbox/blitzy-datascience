# Observability Guide — NBA Data Ingestion Pipeline

This document specifies how the NBA Data Ingestion Pipeline emits, collects, and exposes telemetry. It satisfies the Observability rule declared in the Agent Action Plan (AAP) §0.7.3.1 and is the primary reference for operators running the pipeline on a developer laptop or in a server-side batch environment.

**Scope:** structured logging, correlation IDs, metrics catalog, distributed-tracing posture for the single external service boundary, local health/readiness checks, and the dashboard templates that visualize the metrics.

**Authority:** [`New_Product_Prompt_20260418.md`](./New_Product_Prompt_20260418.md) for the product contract; `config.py` for the runtime-configurable values described here.

**Design tenet:** *If you cannot exercise it locally, it is not delivered.* Every surface in this document is demonstrable on a developer laptop with no third-party network dependency beyond the NBA Stats API itself.

---

## Quick-Start

After `pip install -r requirements.txt`, exercise every observability surface locally:

```bash
# Liveness probe — returns JSON with status, timestamp, and component info
python run.py health

# Readiness probe — verifies output/ is writable and config is complete
python run.py ready

# Metrics — Prometheus text-format exposition on stdout
python run.py metrics

# Tail the rotating log file
tail -F logs/pipeline.log
```

Each data-plane invocation of `run.py` (e.g., `run.py all`, `run.py games`) mints a fresh correlation ID and emits a `run_start` log record before executing. The three diagnostic subcommands above are intended to be safe to run at any time, including before the first pipeline invocation.

---

## Structured Logging

### Log format

The pipeline uses **Python's standard-library `logging` module only**. No third-party logging dependency is permitted (authority boundary Rule 8; AAP F-008). The logger configuration is centralized in `utils/logger.py` and reads format, level, and file path from `config.py`.

**Authoritative format string** (declared as `config.LOG_FORMAT`):

```
%(asctime)s %(levelname)s corr=%(correlation_id)s %(name)s %(message)s
```

Every record carries five fields:

| Field | Source | Example |
|---|---|---|
| `asctime` | `logging.Formatter` timestamp, formatted per `config.LOG_DATE_FORMAT` (`%Y-%m-%dT%H:%M:%S`) | `2026-04-19T12:03:44` |
| `levelname` | Python log level | `INFO`, `WARNING`, `ERROR`, `DEBUG` |
| `correlation_id` | Injected by `CorrelationAdapter` via `contextvars` | `7f1a9c04d8f14b7dba2fb12c94f6a1e0` (UUID4 hex) |
| `name` | Logger name (module-scoped) | `nba_client`, `ingest_games`, `csv_writer` |
| `message` | The log message text (plus any `%`-style args) | `GET leaguegamefinder status=200` |

Field order matches the format string exactly. Downstream text-processing pipelines (`cut`, `awk`, `jq`-via-line-splits) can therefore index fields by column position. Any future field additions MUST append at the end so column positions remain stable.

### Log destinations

Two handlers are attached to the root logger inside `utils/logger.py`:

1. **`StreamHandler(sys.stdout)`** — every record is mirrored to stdout so `tee`, `docker logs`, or a terminal capture works out of the box.
2. **`logging.handlers.RotatingFileHandler`** — writes to `config.LOG_FILE` (default `logs/pipeline.log`). Rotation parameters: `maxBytes = config.LOG_FILE_MAX_BYTES` (default 10 MiB = 10,485,760 bytes), `backupCount = config.LOG_FILE_BACKUP_COUNT` (default 5). Rotated files land alongside the primary file as `pipeline.log.1`, `pipeline.log.2`, … up to `pipeline.log.5`. File encoding is UTF-8.

The `logs/` directory is created on first import of `utils/logger.py` via `config.ensure_directories()`, which invokes `pathlib.Path.mkdir(parents=True, exist_ok=True)` for both `OUTPUT_DIR` and `LOG_DIR`.

### Log levels

| Level | Use | Representative producers |
|---|---|---|
| `DEBUG` | Request/response envelopes, per-row counts, cache hits | `api.nba_client`, `utils.schema_normalizer` |
| `INFO` | Pipeline progress (pipeline start/end, endpoint pulled, rows written), CLI lifecycle | every pipeline, `run.py` |
| `WARNING` | Retry triggered, per-`GAME_ID` failure (Rule 6), checkpoint miss | `api.nba_client`, `pipelines.ingest_games` |
| `ERROR` | Non-recoverable failures that propagate out of a pipeline | every pipeline |
| `CRITICAL` | Logger initialization failure, config validation failure | `utils.logger`, `config` |

Default level is `INFO`, configurable via `config.LOG_LEVEL` or the `NBA_LOG_LEVEL` environment variable (config overrides are documented in `.env.example`).

### Example records

(Field order mirrors the format string exactly.)

```
2026-04-19T12:03:44 INFO corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 run run_start subcommand=all season=2025-26
2026-04-19T12:03:45 INFO corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 ingest_schedule begin endpoint=leaguegamefinder
2026-04-19T12:03:46 DEBUG corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 nba_client GET leaguegamefinder status=200 bytes=412033
2026-04-19T12:03:46 INFO corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 csv_writer wrote schedule.csv rows=1312
2026-04-19T12:03:46 INFO corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 checkpoint mark_completed domain=schedule key=leaguegamefinder:2025-26
2026-04-19T12:05:12 WARNING corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0 ingest_games game 0022500789 failed: HTTPError 500; continuing per Rule 6
```

---

## Correlation IDs

A single correlation ID is minted at CLI entry and threaded through every log record, every metric label (where keyed), and every outbound NBA Stats API request header. It enables an operator to filter logs across one end-to-end run without touching the source.

### Minting

The correlation-ID mechanism lives in `utils/correlation.py`:

```python
# utils/correlation.py
import logging
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

def new_correlation_id() -> str:
    """Mint a fresh UUID4 hex correlation ID and set it in the current context."""
    cid = uuid.uuid4().hex
    correlation_id.set(cid)
    return cid

class CorrelationAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("correlation_id", correlation_id.get() or "")
        return msg, kwargs
```

`run.py` calls `new_correlation_id()` at the top of every subcommand invocation. Every `get_logger(__name__)` call inside `utils/logger.py` returns a `CorrelationAdapter` wrapping the underlying `logging.Logger`, so the adapter's `process()` injects the context variable's current value into every record. When no correlation ID has been minted (for example, log records emitted very early in startup), the formatter falls back to `-` so columnar alignment is preserved.

The UUID4 hex representation is 32 lowercase hexadecimal characters (no hyphens). This format is safe for HTTP headers, filenames, and shell arguments without escaping.

### Propagation inside Python

Because the correlation ID is stored in a `contextvars.ContextVar`, it propagates through:

- Synchronous function calls at any depth.
- `threading.Thread` instances that inherit the current context (Python 3.7+).
- `concurrent.futures.ThreadPoolExecutor` (used only in tests today; the production pipeline is strictly serial per AAP §0.6.2.3).

It does **not** automatically propagate across process boundaries. `multiprocessing` is intentionally out of scope for the current phase.

### Propagation to the NBA Stats API

`api/nba_client.py` reads the current correlation ID and attaches it to every outbound request as the `X-Correlation-ID` header. The NBA Stats API is a public, unauthenticated service that does not consume this header in its responses, but the presence of the header lets network-capture tooling (Wireshark, `mitmproxy`, ISP logs) correlate a laptop-side log entry with a wire-level request when debugging.

### Reading correlation IDs from logs

To retrieve every record for one run, grep the log file for the run's minted ID:

```bash
# First, find the ID from the "run_start" line of interest
grep "run_start" logs/pipeline.log | tail -5

# Then, pull the full trace
grep "corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0" logs/pipeline.log
```

If a log aggregation tool (Loki, Splunk, Elastic) is introduced in a future phase, the `corr=` label should be exposed as a first-class index field.

---

## Metrics

`utils/metrics.py` implements a lightweight, thread-safe counter and histogram registry with no external dependency. It offers three primary public functions:

- `registry.inc(name: str, labels: dict[str, str] | None = None, n: float = 1.0) -> None`
- `registry.observe(name: str, value: float, labels: dict[str, str] | None = None) -> None` — for histograms
- `registry.render_prometheus() -> str` — emits Prometheus text exposition format (version 0.0.4)

All six counters and two histograms below are **pre-registered at module import time**, so `render_prometheus()` always emits complete `# HELP` / `# TYPE` header lines even before any increments have occurred. This guarantees dashboards always find the series they expect.

### Counter catalog

The following six counters are emitted by the pipeline. Names and semantics are binding — do not rename without updating this document, `utils/metrics.py`, and `docs/dashboards/operator_dashboard.json`.

| Metric name | Type | Typical labels | Incremented by | Purpose |
|---|---|---|---|---|
| `nba_requests_total` | counter | `endpoint` | `api.nba_client.NBAClient.get` on every outbound attempt | Total HTTPS GETs issued to NBA Stats |
| `nba_request_failures_total` | counter | `endpoint`, `reason` (e.g., `timeout`, `http_5xx`, `http_4xx_non_429`) | `api.nba_client` once retries are exhausted | Transport-level failures classified by cause |
| `nba_retries_total` | counter | `endpoint` | `tenacity.retry` before-sleep callback in `api.nba_client` | Number of retry attempts (exponential backoff) |
| `pipeline_rows_written_total` | counter | `pipeline`, `artifact` | Each `pipelines.ingest_*` after a successful `CSVWriter.write` | Cumulative rows committed to CSV |
| `pipeline_runs_total` | counter | `pipeline`, `outcome` (`success` / `error`) | Each `pipelines.ingest_*` on exit | Run-level success/failure accounting |
| `games_failed_total` | counter | `reason` | `pipelines.ingest_games` in its Rule 6 per-game `try/except` | Fail-safe iteration counter (Rule 6 observability hook) |

Two companion histograms provide latency visibility:

| Histogram name | Unit | Observed by | Purpose |
|---|---|---|---|
| `nba_request_duration_seconds` | seconds | `api.nba_client.NBAClient.get` around the HTTPS GET | Distribution of NBA Stats API round-trip durations |
| `pipeline_duration_seconds` | seconds | Each `pipelines.ingest_*` around its full `run()` | End-to-end pipeline duration |

Default histogram buckets are `(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)`, which cover the sub-millisecond-to-ten-second range typical of HTTP-bound ETL.

### Exposition format

```bash
python run.py metrics
```

produces output conformant with the Prometheus text format. Representative (abbreviated) example:

```
# HELP nba_requests_total Total number of HTTPS GET requests issued to the NBA Stats API.
# TYPE nba_requests_total counter
nba_requests_total{endpoint="leaguegamefinder"} 1
nba_requests_total{endpoint="leaguedashplayerstats"} 1
nba_requests_total{endpoint="boxscoretraditionalv2"} 1231

# HELP nba_request_failures_total Total number of NBA Stats API requests that exhausted retries.
# TYPE nba_request_failures_total counter
nba_request_failures_total{endpoint="boxscoretraditionalv2",reason="http_5xx"} 2

# HELP nba_retries_total Total number of retry attempts made by tenacity against NBA Stats API.
# TYPE nba_retries_total counter
nba_retries_total{endpoint="boxscoretraditionalv2"} 8

# HELP pipeline_rows_written_total Total number of rows written to CSV artifacts across all pipelines.
# TYPE pipeline_rows_written_total counter
pipeline_rows_written_total{pipeline="ingest_games",artifact="games.csv"} 1231
pipeline_rows_written_total{pipeline="ingest_games",artifact="play_by_play.csv"} 564812

# HELP pipeline_runs_total Total number of pipeline runs completed (success or failure).
# TYPE pipeline_runs_total counter
pipeline_runs_total{pipeline="ingest_games",outcome="success"} 1

# HELP games_failed_total Total number of per-game ingestion failures caught by Rule 6 fail-safe iteration.
# TYPE games_failed_total counter
games_failed_total{reason="http_5xx"} 2
```

Label values are emitted in the standard Prometheus quoting convention (`label="value"`). Escapable characters in label values — backslashes, double quotes, and newlines — are escaped per the exposition specification.

### Viewing metrics locally

Three ways, all of which work on a developer laptop:

1. **One-shot command:**

    ```bash
    python run.py metrics
    ```

2. **Poll with `watch`:**

    ```bash
    watch -n 5 'python run.py metrics | head -30'
    ```

3. **Feed a local Prometheus scraper** (optional): run `python run.py metrics > /tmp/metrics.prom` on a timer and point a local Prometheus at a `file_sd_config`. This is explicitly out of scope for the initial deliverable (AAP §0.6.2.4), but the exposition format makes it trivial to enable later.

**No network endpoint is exposed.** The project does not run a long-lived HTTP server because the authority boundary (Rule 8) forbids introducing a web surface in this phase. Future phases may add `/metrics` HTTP scraping; the counter names and types in this catalog will remain stable.

### Per-process lifetime

Metrics are in-memory per-process. Each invocation of `run.py` starts with zeroed counters (pre-registered but at value `0`), increments them during execution, and emits the final snapshot only if the invocation is `run.py metrics`. Cross-run cumulative counters are out of scope for this phase (no persistent metrics store is permitted). If you need aggregate counts across runs, grep the log file for the corresponding INFO records — `rows=`, `Retrying`, `game … failed`, and `pipeline_runs_total` increments all leave a log trail.

---

## Tracing Across Service Boundaries

The Observability rule calls for *"distributed tracing across service boundaries."* The NBA Data Ingestion Pipeline is a **single-process CLI**; its only external service boundary is the NBA Stats API. There is therefore exactly one egress boundary to instrument.

### Single-egress rationale

Implementing a full OpenTelemetry collector for one outbound boundary would add heavy infrastructure that Rule 8 (authority boundary) forbids. Instead, the pipeline implements a lightweight, purpose-built trace context:

- **Trace ID:** the same UUID4 hex used as the correlation ID serves as the trace ID for a run.
- **Span ID:** a per-request monotonic counter maintained by `api/nba_client.NBAClient` (`nba_req_<n>`), appended to the `X-Correlation-ID` header on the outbound request.
- **Span markers:** every request is bracketed by two log entries at DEBUG level — one `GET …` entry at submission, one `status=<code>` entry at completion — enabling reconstruction of a span's duration from timestamps. The `nba_request_duration_seconds` histogram records the measured duration directly.

### HTTP header

Every outbound request carries:

```
X-Correlation-ID: <correlation_id>-<req_n>
```

Example:

```
X-Correlation-ID: 7f1a9c04d8f14b7dba2fb12c94f6a1e0-000042
```

The NBA Stats API does not reflect this header in its response, but it is visible to any on-path network tooling (`tcpdump`, `mitmproxy`) and lets a debugging session tie a single HTTP call back to a run.

### Reconstructing a trace

```bash
# For run correlation id 7f1a9c... get every request attempt
grep -E "corr=7f1a9c04d8f14b7dba2fb12c94f6a1e0.*(GET |status=)" logs/pipeline.log
```

The `GET` lines are span starts; the `status=` lines are span ends. Durations are computed by subtracting timestamps — or read directly from `nba_request_duration_seconds` histogram buckets exposed by `run.py metrics`.

### Future upgrade path

When a future phase introduces a second outbound service (e.g., a database, an internal HTTP API), the recommended upgrade is OpenTelemetry with Jaeger or Tempo. The current single-egress pattern is forward-compatible because the correlation ID is already a UUID and the per-request span counter is already incrementing.

---

## Health and Readiness

The pipeline exposes two distinct probes. Neither probe calls the NBA Stats API — they are pure local checks that complete in well under a second. This is a deliberate design choice: a health check that probes an external public service would couple this pipeline's reported SLA to someone else's uptime. Upstream availability is surfaced via the request, failure, and retry counters in the metrics catalog instead.

### `run.py health` (liveness)

Prints a JSON object indicating process liveness:

```json
{
  "status": "ok",
  "timestamp": "2026-04-19T12:03:44+00:00",
  "python_version": "3.12.3",
  "component": "nba-ingestion"
}
```

Exit code `0` unless the Python process fails to import `utils.health` at all. A non-zero exit code indicates the pipeline cannot be invoked (typically a broken virtualenv or a syntax error introduced during editing). `health` does not call any collaborators — it is the lightest-weight probe available and is safe to invoke from a tight loop.

### `run.py ready` (readiness)

Performs a readiness check that asserts operationally-required preconditions are satisfied. Four probes are executed in order:

| Probe | What it checks | Pass criterion |
|---|---|---|
| `output_dir_writable` | Creates and deletes a temp file via `tempfile.NamedTemporaryFile(dir=config.OUTPUT_DIR, ...)` | Temp file created and cleaned up successfully |
| `required_headers_present` | `config.REQUIRED_HEADERS` contains both `Referer` and `User-Agent` with non-empty values | Both header keys present and populated |
| `rate_limit_configured` | `config.RATE_LIMIT_SECONDS >= 1.0` (Rule 2 floor) | Configured value meets or exceeds the 1.0-second floor |
| `checkpoint_parseable` | If `config.CHECKPOINT_PATH` exists, parses as valid JSON | File missing OR file parses as JSON |

Output on success:

```json
{
  "status": "ready",
  "timestamp": "2026-04-19T12:03:44+00:00",
  "checks": {
    "output_dir_writable": {"status": "ok", "detail": "wrote and removed temp file under output/"},
    "required_headers_present": {"status": "ok", "detail": "Referer and User-Agent present"},
    "rate_limit_configured": {"status": "ok", "detail": "RATE_LIMIT_SECONDS=1.0 meets Rule 2 floor"},
    "checkpoint_parseable": {"status": "ok", "detail": "checkpoint.json parsed successfully"}
  }
}
```

Output on failure:

```json
{
  "status": "not_ready",
  "timestamp": "2026-04-19T12:03:44+00:00",
  "checks": {
    "output_dir_writable": {"status": "fail", "detail": "Permission denied: 'output/'"},
    "required_headers_present": {"status": "ok", "detail": "Referer and User-Agent present"},
    "rate_limit_configured": {"status": "ok", "detail": "RATE_LIMIT_SECONDS=1.0 meets Rule 2 floor"},
    "checkpoint_parseable": {"status": "ok", "detail": "checkpoint.json not present"}
  }
}
```

Exit codes: `0` if overall `status == "ready"`, `1` otherwise. A non-zero exit is suitable as a gating signal for a future scheduled job — call `ready` before `all` to avoid launching a doomed run.

### Intended use

- **Smoke testing after install:** `ready` validates that the local environment is wired correctly before any live API traffic is attempted.
- **Pre-flight for future scheduler integration:** a future cron job or systemd timer could invoke `ready` before kicking off `all`, reducing partial-run probability.
- **CI/CD sanity check:** `health` is suitable for container orchestrators even though containerization itself is out of scope for this phase.

---

## Dashboards

Two dashboard templates ship with the repository to match two operator environments:

### Grafana (JSON)

[`operator_dashboard.json`](./dashboards/operator_dashboard.json) is a Grafana-compatible dashboard template that renders the six counters above as panels. Import it via *Dashboards → Import → Upload JSON*. It expects a Prometheus data source; point it at whatever mechanism scrapes `python run.py metrics` (file-sd, Pushgateway, or a future `/metrics` HTTP endpoint).

Panels included:

1. **NBA Requests** — `rate(nba_requests_total[5m])` by endpoint.
2. **Request Failures** — `rate(nba_request_failures_total[5m])` by endpoint and reason.
3. **Retry Pressure** — `rate(nba_retries_total[5m])`.
4. **Pipeline Throughput** — `rate(pipeline_rows_written_total[5m])` by pipeline and artifact.
5. **Pipeline Outcomes** — running totals of `pipeline_runs_total` by outcome (`success` vs. `error`).
6. **Games Failed (Rule 6)** — running total of `games_failed_total` by reason.
7. **NBA Request Latency** — `histogram_quantile(0.95, sum(rate(nba_request_duration_seconds_bucket[5m])) by (le, endpoint))`.
8. **Pipeline Duration** — `histogram_quantile(0.95, sum(rate(pipeline_duration_seconds_bucket[5m])) by (le, pipeline))`.

### Markdown (fallback)

[`operator_dashboard.md`](./dashboards/operator_dashboard.md) is a Grafana-free operator dashboard suitable for environments without Grafana. It lists the same six metrics with their PromQL queries, provides sample `grep`/`awk` commands that produce equivalent snapshots from the raw metrics output, and documents alerting thresholds for each counter.

---

## Troubleshooting Runbook

Common issues and the commands that diagnose them.

### "No logs appear in `logs/pipeline.log`"

```bash
# Verify the file exists
ls -la logs/

# Verify the process can write to it
python run.py ready
```

If `ready` reports `output_dir_writable` as `fail`, fix the permissions of the project directory (the readiness check also implies `logs/` writability because `config.ensure_directories()` is invoked at import time and would have surfaced the problem earlier). If `logs/` is missing, confirm your working directory: the rotating file handler creates `logs/` relative to the current working directory, not relative to the repo root, so always invoke `python run.py` from the project root.

### "Metrics are zero after a successful run"

Metrics are in-memory per-process. Each invocation of `run.py` starts with zeroed counters. To see non-zero values, you must invoke `run.py metrics` **in the same process** as the pipeline run — but the current design runs each subcommand as a separate process. The intended observation path is:

```bash
# 1. Run the pipeline
python run.py all --season 2025-26

# 2. Tail the log file — INFO records include row counts equivalent to counters
grep "rows=" logs/pipeline.log | tail -20

# 3. Count retries and failures in the log
grep -c "Retrying" logs/pipeline.log
grep -c "continuing per Rule 6" logs/pipeline.log
```

Cross-run cumulative counters are out of scope for this phase (no persistent metrics store is permitted). A future phase may introduce a long-lived exporter process that accumulates metrics across invocations.

### "I see HTTP 429 in the logs despite Rule 2"

Rule 2 is proactive (≥ 1.0s between requests). HTTP 429 responses indicate NBA Stats tightened throttles mid-run, which the tenacity retry layer handles by backing off exponentially. Expected behavior:

```bash
# Inspect retry activity
grep -E "Retrying|WARNING" logs/pipeline.log | tail -20

# Check retry counter
python run.py metrics | grep nba_retries_total
```

If retries are high (> 5 per run), raise `config.RATE_LIMIT_SECONDS` via the `NBA_RATE_LIMIT_SECONDS` environment variable (e.g., `export NBA_RATE_LIMIT_SECONDS=1.5`) and re-run. The checkpoint will resume from where the previous run stopped (Rule 5). Note that the in-code floor in `utils/rate_limiter.py` prevents values below 1.0 as a defense-in-depth measure for Rule 2.

### "Correlation IDs are all `-` in log output"

The `correlation_id` context variable defaults to an empty string, which the `_CorrelationFormatter` renders as `-`. This happens when a log record is emitted **before** `new_correlation_id()` runs — typically module-import-time logs. Move log statements out of module scope into function bodies to fix. The CLI entry point in `run.py` mints the correlation ID as its very first action, so any log emitted while executing a subcommand will have a populated correlation ID.

### "`python run.py ready` says `checkpoint_parseable: fail`"

Delete the corrupted checkpoint:

```bash
rm output/checkpoint.json
```

Then re-run the pipeline. The pipeline treats a missing checkpoint as a fresh run per AAP §0.4.1.2 and Rule 5 — it is not an error. Corruption typically indicates a process was killed mid-write; the atomic temp-file-plus-`Path.replace()` pattern used by `CheckpointManager` minimizes but cannot eliminate this risk under filesystem-level faults.

### "Pipeline appears hung"

The pipeline is almost certainly waiting on the rate limiter or a slow NBA Stats response. Check the last log line:

```bash
tail -3 logs/pipeline.log
```

If the last line is a `GET` without a matching `status=` line, the request is in flight. The request timeout is `config.REQUEST_TIMEOUT_SECONDS` (default 30 seconds). If the last line is `INFO csv_writer wrote …` followed by silence, the rate limiter is holding the loop — inspect `RATE_LIMIT_SECONDS` and wait one cycle.

### "Log file is growing uncontrollably"

The rotating file handler caps each file at `config.LOG_FILE_MAX_BYTES` (10 MiB) with `config.LOG_FILE_BACKUP_COUNT` (5) rotations — so the ceiling is ~60 MiB across all `pipeline.log*` files. If your logs exceed this, either the configuration was overridden (check `NBA_LOG_FILE_MAX_BYTES`) or `DEBUG` logging is enabled. Set `NBA_LOG_LEVEL=INFO` and re-run.

---

## Configuration Reference

Every observability behavior is tunable via `config.py`. Defaults are suitable for a developer laptop; tune in production as needed. Each field accepts an override via the corresponding `NBA_*` environment variable (see `.env.example` for the full list).

| Config field | Default | Environment variable | Effect |
|---|---|---|---|
| `LOG_LEVEL` | `"INFO"` | `NBA_LOG_LEVEL` | Root logger level; set `"DEBUG"` to include request/response envelopes |
| `LOG_FORMAT` | `"%(asctime)s %(levelname)s corr=%(correlation_id)s %(name)s %(message)s"` | *(not overridable)* | Format string applied to every record |
| `LOG_DATE_FORMAT` | `"%Y-%m-%dT%H:%M:%S"` | *(not overridable)* | Timestamp format for `%(asctime)s` |
| `LOG_FILE` | `Path("logs/pipeline.log")` | `NBA_LOG_FILE` | Target for `RotatingFileHandler` |
| `LOG_DIR` | `Path("logs")` | `NBA_LOG_DIR` | Parent directory for `LOG_FILE`; created by `ensure_directories()` |
| `LOG_FILE_MAX_BYTES` | `10_485_760` (10 MiB) | `NBA_LOG_FILE_MAX_BYTES` | Rotation threshold |
| `LOG_FILE_BACKUP_COUNT` | `5` | `NBA_LOG_FILE_BACKUP_COUNT` | Rotated files retained |
| `RATE_LIMIT_SECONDS` | `1.0` | `NBA_RATE_LIMIT_SECONDS` | Proactive inter-request floor; raise to soften 429 pressure |
| `RETRY_ATTEMPTS` | `5` | `NBA_RETRY_ATTEMPTS` | Tenacity retry ceiling |
| `RETRY_MULTIPLIER` | `2` | `NBA_RETRY_MULTIPLIER` | Exponential backoff multiplier |
| `RETRY_MIN_WAIT` | `1` | `NBA_RETRY_MIN_WAIT` | Minimum backoff wait in seconds |
| `RETRY_MAX_WAIT` | `60` | `NBA_RETRY_MAX_WAIT` | Maximum backoff wait in seconds |
| `REQUEST_TIMEOUT_SECONDS` | `30` | `NBA_REQUEST_TIMEOUT_SECONDS` | Per-request socket timeout |
| `OUTPUT_DIR` | `Path("output")` | `NBA_OUTPUT_DIR` | Target directory for CSV artifacts; probed by `output_dir_writable` readiness check |
| `CHECKPOINT_PATH` | `Path("output/checkpoint.json")` | `NBA_CHECKPOINT_PATH` | Location probed by `checkpoint_parseable` readiness check |
| `REQUIRED_HEADERS` | Dict containing `Referer`, `User-Agent`, plus compatibility headers | *(not overridable)* | Rule 3 binding; probed by `required_headers_present` readiness check |

Environment variables override the corresponding config fields at import time. The default `.env.example` at the repository root documents every supported override with a representative value.

### Overriding for a single run

```bash
# Quieter run with DEBUG off but verbose retries surfaced
NBA_LOG_LEVEL=INFO NBA_RATE_LIMIT_SECONDS=1.5 python run.py games --season 2025-26

# Louder run with full request/response bodies
NBA_LOG_LEVEL=DEBUG python run.py schedule --season 2025-26
```

### Verifying an override took effect

```bash
# Dump effective config as JSON for inspection
python -c "import json, config; print(json.dumps({k: str(v) for k, v in vars(config).items() if k.isupper()}, indent=2))" | grep LOG
```

---

## Further reading

- [`New_Product_Prompt_20260418.md`](./New_Product_Prompt_20260418.md) — authoritative product contract (seven operational rules, authority boundary, tech stack).
- [`ONBOARDING.md`](./ONBOARDING.md) — clean-machine to first successful run in under 15 minutes.
- [`DECISIONS.md`](./DECISIONS.md) — architectural decision log explaining the "why" behind observability design choices (custom metrics registry over `prometheus_client`, single-egress trace pattern, rotating file handler over syslog, etc.).
- [`TRACEABILITY.md`](./TRACEABILITY.md) — bidirectional matrix linking features, rules, and gates to implementing files (including the observability surface).

