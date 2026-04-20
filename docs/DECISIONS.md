# Decision Log — NBA Data Ingestion Pipeline

This document records every non-obvious implementation decision made while building the NBA Data Ingestion Pipeline. It satisfies the Explainability rule declared in the Agent Action Plan (AAP) §0.7.3.3: *"Every non-trivial implementation decision MUST be documented with rationale... Do not embed rationale in code comments."*

Each entry uses the mandated four-column schema:

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|

- **Decision** — the choice that was made, in imperative voice.
- **Alternatives** — other paths that were considered and why they were rejected.
- **Rationale** — the primary reason the decision is correct given the product brief's constraints.
- **Risk** — what could go wrong, plus the current mitigation or the monitor that detects the regression.

This log is written for future contributors, auditors, and the Blitzy platform itself; it is not disposable.

## How This Log Is Used

- **Reading:** scroll the index, jump to the decision of interest. Every non-trivial design choice in the codebase has an entry here; if it does not, that is a documentation defect.
- **Adding:** append a new subsection at the end of Section 4, then add a row to the index in Section 3. Use the template in the appendix. Never mutate an accepted decision in place — supersede it with a new entry whose title ends with `(supersedes #N)` and mark the prior entry's status as `Superseded`.
- **Status values:** `Accepted` (active), `Superseded` (replaced by a later decision), `Deferred` (decision acknowledged but implementation postponed).
- **Audit trail:** every decision entry must be committable in isolation — no implicit dependencies on other uncommitted entries.

## Decision Index

| # | Decision | Status |
|---|---|---|
| [D-001](#d-001-interpret-the-user-instructions-8-rules-as-7-product-brief-rules-plus-the-authority-boundary-constraint) | Interpret the user instructions' "8 rules" as 7 product-brief rules plus the authority-boundary constraint | Accepted |
| [D-002](#d-002-use-tenacity-8x-for-retryback-off-instead-of-a-hand-rolled-loop) | Use `tenacity` 8.x for retry/back-off instead of a hand-rolled loop | Accepted |
| [D-003](#d-003-persist-the-checkpoint-as-a-single-json-file-not-a-sqlite-database) | Persist the checkpoint as a single JSON file, not a SQLite database | Accepted |
| [D-004](#d-004-propagate-the-correlation-id-via-contextvarscontextvar-not-threadinglocal) | Propagate the correlation id via `contextvars.ContextVar`, not `threading.local` | Accepted |
| [D-005](#d-005-expose-metrics-via-an-on-demand-cli-subcommand-not-a-persistent-http-metrics-endpoint) | Expose metrics via an on-demand CLI subcommand, not a persistent HTTP `/metrics` endpoint | Accepted |
| [D-006](#d-006-log-to-stdout-rotatingfilehandler-not-syslog) | Log to stdout + `RotatingFileHandler`, not `syslog` | Accepted |
| [D-007](#d-007-build-the-cli-with-click-not-argparse) | Build the CLI with `click`, not `argparse` | Accepted |
| [D-008](#d-008-dispatch-the-all-subcommand-in-the-order-schedule-games-teams-players-lineups) | Dispatch the `all` subcommand in the order schedule → games → teams → players → lineups | Accepted |
| [D-009](#d-009-implement-per-domain-pipelines-as-dedicated-modules-rather-than-inlining-schedule-and-lineups-inside-their-endpoint-modules) | Implement per-domain pipelines as dedicated modules rather than inlining schedule and lineups inside their endpoint modules | Accepted |
| [D-010](#d-010-keep-basewriter-abstract-and-ship-only-csvwriter-in-this-phase) | Keep `BaseWriter` abstract and ship only `CSVWriter` in this phase | Accepted |
| [D-011](#d-011-inject-collaborators-explicitly-no-di-container) | Inject collaborators explicitly; no DI container | Accepted |
| [D-012](#d-012-catch-exception-only-inside-pipelinesingest_gamespy) | Catch `Exception` only inside `pipelines/ingest_games.py` | Accepted |
| [D-013](#d-013-pin-dependencies-with-upper-bounds-in-requirementstxt) | Pin dependencies with upper bounds in `requirements.txt` | Accepted |
| [D-014](#d-014-manual-invocation-for-gate-verification-no-cicd-in-this-phase) | Manual invocation for gate verification; no CI/CD in this phase | Accepted |
| [D-015](#d-015-attach-required-headers-at-the-requestssession-level-not-per-request) | Attach required headers at the `requests.Session` level, not per-request | Accepted |
| [D-016](#d-016-apply-rule-6-fail-safe-iteration-only-to-the-games-pipeline) | Apply Rule 6 fail-safe iteration only to the Games pipeline | Accepted |
| [D-017](#d-017-enforce-rules-1-and-7-with-grep-based-invariant-tests) | Enforce Rules 1 and 7 with grep-based invariant tests | Accepted |
| [D-018](#d-018-document-every-config-value-with-a-trace-to-a-read-site-for-gate-12) | Document every `config` value with a trace to a read-site for Gate 12 | Accepted |

## Decisions

### D-001 — Interpret the user instructions' "8 rules" as 7 product-brief rules plus the authority-boundary constraint

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Treat the user's "8 rules" reference as the seven operational rules listed in `docs/New_Product_Prompt_20260418.md` §5 plus an eighth constraint: the authority boundary from §1 (no database, no web UI, no auth, no streaming, no CI/CD in this phase). | (a) Treat "8 rules" as literal and invent an eighth rule of our own choosing; (b) treat "8 rules" as a typo and ship only the seven brief rules. | The product brief is the authoritative source and contains seven explicit rules. The user's "8" is either an overcount or an implicit count including the §1 boundary. Naming the boundary as Rule 8 honors the user's numbering while remaining grounded in the brief. | If a future review introduces a *different* eighth rule, this interpretation must be superseded. Monitored by the Rule 8 entry in `docs/TRACEABILITY.md` — if that row's "Enforcing file(s)" becomes ambiguous, revisit here. |

**Status:** Accepted.

### D-002 — Use `tenacity` 8.x for retry/back-off instead of a hand-rolled loop

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Wrap `api/nba_client.NBAClient._request` with `@tenacity.retry(stop=stop_after_attempt(RETRY_ATTEMPTS), wait=wait_exponential_jitter(...), retry=retry_if_exception_type(...))`. | (a) Hand-written `while` loop with `time.sleep(2**attempt)`; (b) `urllib3.util.Retry` via a `requests.adapters.HTTPAdapter`. | The product brief sanctions `tenacity` 8.x as the retry library. Tenacity offers declarative `retry_if_*` predicates that distinguish transient (429, 5xx, connection) from permanent (non-429 4xx) failures — exactly the Rule 2 + Rule-silence taxonomy the brief requires. Hand-rolled code regresses silently. `urllib3.util.Retry` cannot distinguish retry predicates by exception type at the `requests` layer. | Tenacity's exception semantics change across majors. The pin `tenacity>=8.0,<9` in `requirements.txt` prevents a silent major upgrade. |

**Status:** Accepted.

### D-003 — Persist the checkpoint as a single JSON file, not a SQLite database

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `utils/checkpoint.py` writes `output/checkpoint.json` with `json.dumps(..., indent=2)` after every `mark_completed` call. | (a) SQLite database via the stdlib `sqlite3` module (OUT of scope in this phase and deferred per Rule 8); (b) newline-delimited JSON append log. | The authority boundary (Rule 8) forbids introducing a database layer in this phase. A single JSON file is (a) human-inspectable, (b) trivially deletable to force a fresh run, (c) atomically replaceable via `pathlib.Path.write_text`. The checkpoint cardinality (one entry per successful endpoint pull) is small — O(hundreds) per season — so no indexing is required. | Corrupt JSON (power loss mid-write) would orphan the run. Mitigation: each write goes to `checkpoint.json.tmp` first and then `Path.replace`s atomically; `run.py ready` validates JSON parseability on start. |

**Status:** Accepted.

### D-004 — Propagate the correlation id via `contextvars.ContextVar`, not `threading.local`

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `utils/correlation.py` exposes `correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")`. | (a) `threading.local()`; (b) pass the correlation id as an explicit function parameter at every layer. | `contextvars` is the modern Python 3.7+ replacement for `threading.local`. It propagates correctly through `asyncio` tasks (future-proofing) and across `ThreadPoolExecutor` submissions. Threading-local state works but breaks under asyncio. Explicit parameter passing pollutes every signature in the codebase and fails Rule 6 (which needs correlation in logs emitted from a bare `except`). | `contextvars` requires Python 3.7+. Our supported floor is 3.11; no risk from this pin. |

**Status:** Accepted.

### D-005 — Expose metrics via an on-demand CLI subcommand, not a persistent HTTP `/metrics` endpoint

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `run.py metrics` prints Prometheus text format to stdout using `utils/metrics.render_prometheus()`. | (a) Embed a `starlette`/`flask` server exposing `/metrics` (OUT of scope per Rule 8 — no web surface in this phase); (b) push to a Prometheus Pushgateway (deferred to a future phase); (c) write metrics to `output/metrics.prom` on a timer. | The authority boundary forbids introducing a web surface in this phase. An on-demand subcommand offers the same visibility without violating Rule 8. The Prometheus text format is scrapable by any future collector with zero client-side changes. | Metrics are in-memory per process — they reset every invocation. Mitigation: counters mirror key numbers into `logs/pipeline.log` INFO records (`rows=…`, `retries=…`) so durable history lives in the log. Future-phase upgrade documented in `docs/OBSERVABILITY.md` §Metrics. |

**Status:** Accepted.

### D-006 — Log to stdout + `RotatingFileHandler`, not `syslog`

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `utils/logger.py` attaches one `StreamHandler(sys.stdout)` and one `logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_485_760, backupCount=5)` to the root logger. | (a) `SysLogHandler` to a local `/dev/log`; (b) `WatchedFileHandler` with external logrotate; (c) `QueueHandler` + `QueueListener` for async writes. | The product targets a developer laptop (macOS, Windows, Linux). `/dev/log` is not universally present. External `logrotate` requires OS configuration that the onboarding rule forbids us from asking the new developer to set up. `RotatingFileHandler` is stdlib, cross-platform, and self-managing. Stdout duplication keeps the laptop experience familiar (Ctrl-C shows recent output). | If log volume exceeds the 10 MiB × 5 rotation capacity (50 MiB total), old records are lost. Mitigation: observed volumes at INFO level are under 1 MiB per full-season run; DEBUG volume is higher but opt-in. |

**Status:** Accepted.

### D-007 — Build the CLI with `click`, not `argparse`

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `run.py` uses `@click.group()` and `@click.command()` decorators; the CLI is a top-level click group with nine subcommands. | (a) `argparse` with subparsers; (b) `typer` (which wraps click). | `click` is sanctioned by the product brief (§3.2). Its decorator model keeps `run.py` under 200 lines even with nine subcommands plus shared option groups. `argparse` can produce the same UX but requires dataclass-style manual plumbing that bloats the CLI layer. `typer` is a click wrapper; it would add a dependency without net benefit and is not on the sanctioned list. | Click's `CliRunner` test helper differs between 7.x and 8.x. Pin `click>=8.0,<9` in `requirements.txt`. |

**Status:** Accepted.

### D-008 — Dispatch the `all` subcommand in the order schedule → games → teams → players → lineups

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `run.py all` invokes pipelines in this exact order. | (a) Alphabetical order; (b) parallel dispatch (OUT of scope — Rule 2 forbids concurrent requests without a cross-process rate limiter, deferred to a future phase). | The Games pipeline depends on `GAME_ID`s enumerated by the Schedule pipeline (AAP §0.4.5). Running schedule first guarantees the dependency. Teams is placed before players because team-level metadata is referenced inside player records. Lineups is last because it is the smallest domain and its failure does not block other artifacts. | If a future domain adds another cross-dependency, this order must be revisited. Monitored via `tests/integration/test_gate1_all_live.py` — reordering would surface through the test's expected artifact-count check. |

**Status:** Accepted.

### D-009 — Implement per-domain pipelines as dedicated modules rather than inlining schedule and lineups inside their endpoint modules

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `pipelines/ingest_schedule.py` and `pipelines/ingest_lineups.py` are standalone modules, same as `ingest_players`, `ingest_teams`, `ingest_games`. | Inline the two smaller domains' orchestration inside their respective `endpoints/*.py` files. | Gate 13 (every CLI subcommand invokes a pipeline) is cleaner when every domain has a symmetric pipeline module. Uniformity also simplifies `tests/unit/pipelines/test_ingest_*.py` (one test file per domain, same shape). | Slightly more files than strictly necessary. Accepted: the cost is five small modules; the benefit is code-review symmetry. |

**Status:** Accepted.

### D-010 — Keep `BaseWriter` abstract and ship only `CSVWriter` in this phase

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `storage/csv_writer.py` defines `BaseWriter(ABC)` with abstract `write(df, name, season) -> Path`; only `CSVWriter` is instantiated by pipelines. | (a) Concrete-only `CSVWriter` without a base class; (b) ship both `CSVWriter` and an early `DuckDBWriter` skeleton (OUT of scope and deferred — introducing a database writer violates Rule 8 in this phase). | Rule 7 demands pluggability. The abstract base class makes the extension seam explicit and test-enforceable. Shipping a DuckDB writer now would violate Rule 8 (no database layer in this phase). | Abstract base classes in Python can be subverted (`__init_subclass__` skipped, multi-inheritance quirks). Mitigation: `tests/unit/storage/test_csv_writer.py` asserts that a subclass missing `write` raises `TypeError` on instantiation. |

**Status:** Accepted.

### D-011 — Inject collaborators explicitly; no DI container

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `run.py` constructs `NBAClient`, `CSVWriter`, `CheckpointManager`, `RateLimiter`, and passes them into each pipeline's `run()` function as parameters. | (a) A DI container such as `dependency-injector`; (b) module-global singletons. | The pipeline is small (< 10 collaborators). A DI container adds dependency weight and cognitive load without payback at this scale. Module-global singletons hide the wiring and break Gate 12 (config propagation tracing) because read-sites become non-local. | If the codebase grows past ~20 collaborators, this decision should be revisited. Monitored: if `run.py` grows past 250 lines, open an ADR to reintroduce a container. |

**Status:** Accepted.

### D-012 — Catch `Exception` only inside `pipelines/ingest_games.py`

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| The per-`GAME_ID` loop in `pipelines/ingest_games.py` uses `try/except Exception`; all other pipelines let exceptions propagate. | Catch `Exception` in every pipeline loop. | Rule 6 applies only to the Games pipeline per product brief §5. Silencing exceptions elsewhere would hide rule violations and schema drift. The only allowed bare-`except` site is `ingest_games`. | A future maintainer may copy-paste the pattern into another pipeline. Mitigation: `tests/invariants/test_bare_except_scope.py` (optional future test) greps for `except Exception:` outside `pipelines/ingest_games.py`. Document the constraint in the file header so the reviewer sees it. |

**Status:** Accepted.

### D-013 — Pin dependencies with upper bounds in `requirements.txt`

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `requirements.txt` uses range specifiers: `requests>=2.31,<3`, `pandas>=2.0,<3`, `click>=8.0,<9`, `tenacity>=8.0,<9`. | (a) Exact pins (`==2.33.1`); (b) no upper bounds. | Range pins block silent major-version upgrades (which have historically broken APIs) while allowing patch-level security updates. Exact pins are too rigid for a library project. No upper bound is too lax — a future `pandas 3.0` could break `DataFrame.to_csv`. | Range pins allow minor-version drift that could theoretically introduce regressions. Mitigation: Gate 10 (`pytest`) runs on every change; Gate 1 exercises the live wire format. |

**Status:** Accepted.

### D-014 — Manual invocation for gate verification; no CI/CD in this phase

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| The onboarding guide documents each gate's invocation command; no `.github/workflows/*.yml` or `.gitlab-ci.yml` ships. | Author a GitHub Actions workflow at the outset (OUT of scope — Rule 8 forbids CI/CD infrastructure in this phase; deferred to a future phase). | Rule 8 forbids introducing CI/CD infrastructure in this phase. Gates 2 and 10 are one-line invocations (`flake8`, `pytest`); Gates 1 and 8 require live NBA Stats access. Shipping CI now would either require secret management (Rule 8 violates) or run flaky against a public API. | A future regression could slip through if a developer forgets to run the commands. Mitigation: the onboarding guide enumerates the four commands; a future phase can wrap them in a Makefile or GitHub Actions job. |

**Status:** Accepted.

### D-015 — Attach required headers at the `requests.Session` level, not per-request

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `NBAClient.__init__` creates a single `requests.Session` and assigns `self._session.headers.update(config.REQUIRED_HEADERS)`. | Pass `headers=` on every `session.get()` call. | Rule 3 requires the two headers on every request. Session-level attachment is idempotent — no way to accidentally omit them. Per-request passing relies on convention; a future refactor could drop a call site. | `requests.Session.headers` is mutable. If another module holds a reference and mutates the dict, the headers could change mid-run. Mitigation: the session is encapsulated as a private attribute (`self._session`) with no accessor. |

**Status:** Accepted.

### D-016 — Apply Rule 6 fail-safe iteration only to the Games pipeline

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Only `pipelines/ingest_games.py` wraps its per-entity loop in `try/except Exception`. | (a) Apply the pattern to every pipeline; (b) apply nowhere. | Rule 6 in the product brief is scoped specifically to games (per-game failures are expected due to the size and age of the domain). Other domains have smaller entity sets where a single failure signals a structural problem and should abort. | Symmetry expectation from maintainers may lead to inappropriate generalization. Mitigation: file-level docstring in `ingest_games.py` calls out the rule; the decision log entry above cross-references it. |

**Status:** Accepted.

### D-017 — Enforce Rules 1 and 7 with grep-based invariant tests

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `tests/invariants/test_rule1_sole_http_client.py` and `tests/invariants/test_rule7_basewriter_only.py` use `subprocess.run(["grep", ...])` to assert zero matches outside the sanctioned module. | (a) AST walks via `ast.parse`; (b) import-time instrumentation. | Grep is simple, fast, CI-friendly, and catches the exact regression shape (a call-site appearing outside its allowed scope). AST walks are more powerful but overkill for a three-pattern match. Import-time instrumentation is fragile and can miss dynamic imports. | Grep misses imports that rename `requests` (`import requests as http`). Mitigation: the invariant test also checks for `import requests` outside `api/nba_client.py` — any alias would require an aliased import, which is itself banned by convention. |

**Status:** Accepted.

### D-018 — Document every `config` value with a trace to a read-site for Gate 12

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| `tests/unit/test_config.py` imports every field from `config.py` and grep-asserts at least one consumer module references the name. | Document read-sites only in comments. | Gate 12 requires that every `config` constant is *actually* consumed. Runtime enforcement catches dead configuration. Comment-only documentation drifts. | A future refactor might move a constant to a lazy import, which grep would not catch. Mitigation: the test also imports from every consumer module and confirms the attribute access via `hasattr(module, field)`. |

**Status:** Accepted.

## Appendix — Template for Adding a New Decision

Copy this template when appending a new decision:

````markdown
### D-XXX — <Decision in imperative voice>

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| <What was decided> | <Options considered + why rejected> | <Why this is correct given the constraints> | <What could go wrong + current mitigation> |

**Status:** Accepted | Superseded by D-YYY | Deferred.
````

1. Increment the decision number (e.g., D-019).
2. Add the corresponding row to Section 3 — Decision Index.
3. Commit the two edits in a single change so the index never drifts from the decision set.
