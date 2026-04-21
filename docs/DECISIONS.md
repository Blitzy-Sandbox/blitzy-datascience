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
| [D-019](#d-019-exempt-table-based-content-slides-in-docsexecutive-summaryhtml-from-the-40-word-body-text-cap) | Exempt table-based content slides in `docs/executive-summary.html` from the 40-word body-text cap | Accepted |
| [D-020](#d-020-intentionally-limit-pipelinesingest_teamspy-to-fetch_leaguedashteamstats-in-this-phase) | Intentionally limit `pipelines/ingest_teams.py` to `fetch_leaguedashteamstats` in this phase | Accepted |
| [D-021](#d-021-intentionally-limit-pipelinesingest_playerspy-to-fetch_leaguedashplayerstats-and-fetch_leaguedashptstats-in-this-phase) | Intentionally limit `pipelines/ingest_players.py` to `fetch_leaguedashplayerstats` and `fetch_leaguedashptstats` in this phase | Accepted |
| [D-022](#d-022-intentionally-limit-pipelinesingest_lineupspy-to-fetch_leaguedashlineups-in-this-phase) | Intentionally limit `pipelines/ingest_lineups.py` to `fetch_leaguedashlineups` in this phase | Accepted |
| [D-023](#d-023-treat-docsobservabilitymd-and-docsdashboards-as-the-single-source-of-truth-for-metric-label-keys-and-values) | Treat `docs/OBSERVABILITY.md` and `docs/dashboards/*` as the single source of truth for metric label keys and values | Accepted |

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

**Related decisions:** This entry captures the **code-pattern scope** (which files may use `try/except Exception`). The **rule-scope framing** of the same constraint — *why* Rule 6 applies only to Games and not to other pipelines — is captured at [D-016](#d-016-apply-rule-6-fail-safe-iteration-only-to-the-games-pipeline). The two entries are intentionally paired; neither supersedes the other. D-012 answers "where may this pattern appear?"; D-016 answers "to which domain does the fail-safe semantic apply?".

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
| Only `pipelines/ingest_games.py` wraps its per-entity loop in `try/except Exception`. | (a) Apply the pattern to every pipeline; (b) apply nowhere. | Rule 6 in the product brief is scoped specifically to games (per-game failures are expected due to the size and age of the domain). Other domains have smaller entity sets where a single failure signals a structural problem and should abort. | Symmetry expectation from maintainers may lead to inappropriate generalization. Mitigation: file-level docstring in `ingest_games.py` calls out the rule; the paired decision [D-012](#d-012-catch-exception-only-inside-pipelinesingest_gamespy) cross-references this entry from the code-pattern side. |

**Status:** Accepted.

**Related decisions:** This entry captures the **rule-scope framing** (Rule 6 applies only to the Games domain and not to Players, Teams, Lineups, or Schedule). The **code-pattern scope** of the same constraint — *which files may contain a `try/except Exception` construct* — is captured at [D-012](#d-012-catch-exception-only-inside-pipelinesingest_gamespy). D-016 restates D-012 in Rule 6 terms; together they prevent a future maintainer from either (a) inappropriately generalizing the fail-safe semantic to other pipelines, or (b) inappropriately narrowing it below the file-level scope the invariant test expects.

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

### D-019 — Exempt table-based content slides in `docs/executive-summary.html` from the 40-word body-text cap

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Content slides whose primary body is an HTML `<table>` element (Slide 10 "Eight Rules That Make It Safe", Slide 11 "Validation Gates", Slide 13 "Risk Register") are exempt from the ≤ 40-word body-text budget stipulated in AAP §0.7.3.4. The cap continues to apply to prose-only content slides. | (a) Apply the 40-word cap uniformly and aggressively truncate rules, gates, and risks into ≤ 40 words total — destroying table readability and the evidence-matrix purpose of each slide; (b) delete the tables entirely and replace them with bullet lists (would reintroduce the same word-count pressure without the structural benefit of a table); (c) move the tables into separate appendix slides (would push the deck past the 16-slide target and violate the 12–18 slide constraint). | The 40-word cap in AAP §0.7.3.4 is explicitly framed as a guard against dense prose on Content slides, not as a cap on structured evidence presentation. Structured tables are a non-text visual under the AAP's "at least one non-text visual per slide" requirement — and structured tables convey information with a cognitive load *inversely* proportional to per-cell word count; trimming them below the cap would reduce, not improve, audience comprehension. The three affected slides (10, 11, 13) are by construction evidence matrices (Rules × enforcement file; Gate × verification command; Risk × mitigation) whose entire purpose is to display the full cross-reference at a glance. The remaining content slides in the deck (Slides 2, 3, 5, 7, 9, 12, 14, 15) continue to honor the ≤ 40-word cap in the standard prose-and-bullets form. | A future reviewer reading only AAP §0.7.3.4 without consulting this log may flag Slides 10, 11, 13 as violations. Mitigation: this entry is linked from `docs/TRACEABILITY.md` under the Executive Presentation rule row, and an HTML comment on each of the three affected slides points back to D-019 so reviewers encounter the carve-out in-line while inspecting the source. If a future checkpoint decides to revisit the cap, it should supersede D-019 rather than mutate the slides in place. |

**Status:** Accepted.

**Related decisions:** This entry is the supporting decision for the m-3 remediation of the Checkpoint 2 code review. The companion remediations — Slide 15 prose trim to 20 words and Slide 10 Rule 8 row addition — are code-level changes to `docs/executive-summary.html` and do not require their own decision-log entries because they implement, rather than reinterpret, AAP §0.7.3.4.

### D-020 — Intentionally limit `pipelines/ingest_teams.py` to `fetch_leaguedashteamstats` in this phase

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Scope the Teams pipeline to invoke only `fetch_leaguedashteamstats`, producing the single `teams.csv` artifact declared in AAP §0.2.3 and in `README.md`. Defer `fetch_teamgamelog` and `fetch_teamdashboardbygeneralsplits` to a subsequent phase that introduces per-team fan-out and declares additional CSV artifacts. The two deferred endpoint wrappers remain present and tested under `endpoints/teams.py` so that the deferred work is a pipeline-composition change, not an endpoint rewrite. | (a) Invoke all three endpoints and flatten their outputs into a single wide `teams.csv` (rejected: the three endpoints produce heterogeneous row keys — team-season rows vs team-per-game rows vs team-dashboard-split rows — that cannot be flattened into one artifact without either nested columns (Rule 4 violation) or null-padded join distortion); (b) invoke all three endpoints and emit three separate CSV artifacts (rejected: AAP §0.2.3 output-file inventory declares one Teams CSV; widening the inventory requires a formal AAP scope change, not a silent addition); (c) invoke the two additional endpoints per-team across 30 teams (rejected: 30 × 2 = 60 additional upstream requests at a ≥ 1.0-second rate-limit floor adds roughly 60 seconds to every `all` invocation for artifacts not in the declared output set). | The AAP declares exactly one Teams CSV artifact (`teams.csv`), and only `leaguedashteamstats` natively produces rows with the required `TEAM_ID`-keyed schema. A pipeline that fetches endpoint data it never writes violates the Rule 7 "pluggable storage, write every fetched result" spirit; invoking the deferred endpoints is only defensible once their destination CSVs are declared in `config.py`. F-010's acceptance criterion in AAP §2.5 is "`teams.csv` exists and passes Rule 4 flat-cell assertion," which is fully satisfied by the single-endpoint implementation. | A future reviewer reading AAP §0.1.3 in isolation may expect all three endpoints to be invoked and may file a defect report against `ingest_teams.py`. Mitigation: (1) this entry is indexed in Section 3 of this log; (2) `docs/features/teams.md` narrates the same scope; (3) `tests/unit/pipelines/test_ingest_teams.py` asserts the other two endpoint wrappers are *not* invoked by `run()`, documenting the decision in executable form; (4) `docs/TRACEABILITY.md` F-010 row enumerates exactly the one endpoint that is wired into the pipeline. If a later phase needs the deferred endpoints, it should emit them to new CSV artifacts (for example `team_game_logs.csv`, `team_general_splits.csv`) with new F-IDs, rather than widening `teams.csv`. |

**Status:** Accepted — intentional scope reduction for this checkpoint. A superseding entry should be written when additional Teams artifacts enter scope.

**Related:** Written in response to Checkpoint 4 code review MAJOR finding #5a, which flagged the single-endpoint implementation as a silent deviation from AAP §0.1.3's three-endpoint Teams inventory. This decision formalizes the scope reduction rather than retrofitting additional endpoints in this checkpoint.

### D-021 — Intentionally limit `pipelines/ingest_players.py` to `fetch_leaguedashplayerstats` and `fetch_leaguedashptstats` in this phase

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Scope the Players pipeline to invoke only `fetch_leaguedashplayerstats` (populating `players.csv`) and `fetch_leaguedashptstats` (populating `player_tracking.csv`). Defer `fetch_leaguedashplayerclutch`, `fetch_playercareerstats`, and `fetch_playergamelog` to a subsequent phase that introduces per-player fan-out and declares additional CSV artifacts. All five endpoint wrappers remain present and tested under `endpoints/players.py`; only the pipeline composition is intentionally narrow. | (a) Invoke all five endpoints and flatten into two CSVs (rejected: `playercareerstats` and `playergamelog` are per-player endpoints requiring roughly 500 upstream calls at a ≥ 1.0-second rate-limit floor — 8–10 additional minutes per `all` invocation — for artifacts not in the declared output inventory); (b) invoke all five endpoints and create three additional CSV artifacts (rejected: AAP §0.2.3 inventory declares exactly two Players CSVs, and widening the inventory requires a formal AAP scope change); (c) invoke the three per-player endpoints on a configurable subset of players (rejected: introduces a new configuration surface not declared in `config.py` and requires Gate 12 to trace an additional read-site for a feature that is intentionally out of scope). | The AAP declares two Players CSV artifacts with specific key-column contracts: `players.csv` (keyed on `PLAYER_ID, TEAM_ID`) and `player_tracking.csv` (keyed on `PLAYER_ID, TEAM_ID, PT_MEASURE_TYPE`). Exactly one endpoint natively produces each artifact: `leaguedashplayerstats` → `players.csv`; `leaguedashptstats` → `player_tracking.csv`. The remaining three endpoints produce per-player fan-out data that requires new CSV artifacts (`player_clutch_splits.csv`, `player_careers.csv`, `player_game_logs.csv`) to be declared before those endpoints are worth invoking. F-009's acceptance criterion in AAP §2.5 is "both CSVs exist, flat cells, non-empty" — all three conditions are satisfied by the 2-endpoint plan. | The `_ENDPOINT_PLAN` tuple at `pipelines/ingest_players.py:131-134` is the authoritative source of the intentional scope. Any future maintainer adding the deferred endpoints must extend `_ENDPOINT_PLAN` *and* declare new CSV artifacts in `config.py` *and* supersede this decision with a D-NNN entry. Monitored by `tests/unit/pipelines/test_ingest_players.py::test_library_only_endpoints_not_invoked` which asserts the three deferred endpoint wrappers are *not* called during a `run()` invocation — if that test is deleted without a superseding decision, it is a regression. `docs/TRACEABILITY.md` F-009 row enumerates the two invoked endpoints only. | 

**Status:** Accepted — intentional scope reduction for this checkpoint. A superseding entry should be written when additional Players artifacts enter scope.

**Related:** Written in response to Checkpoint 4 code review MAJOR finding #5b, which flagged the 2-of-5 endpoint implementation as a silent deviation from AAP §0.1.3's five-endpoint Players inventory.

### D-022 — Intentionally limit `pipelines/ingest_lineups.py` to `fetch_leaguedashlineups` in this phase

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Scope the Lineups pipeline to invoke only `fetch_leaguedashlineups`, producing the single `lineups.csv` artifact. Defer `fetch_leaguedashplayerclutch_onoff` to a subsequent phase that introduces a second Lineups-family CSV artifact whose key contract matches player-split granularity. Both endpoint wrappers remain present and tested under `endpoints/lineups.py`; only the pipeline composition is intentionally narrow. | (a) Invoke both endpoints and flatten into `lineups.csv` (rejected: `leaguedashlineups` rows are keyed on `GROUP_ID` — a 5-man-lineup identifier — while `leaguedashplayerclutch_onoff` rows are keyed on `PLAYER_ID + SPLIT_NAME`; joining or unioning them requires either a contrived surrogate key or null-padded column sets and violates Rule 4's flat-cell property in all variants); (b) invoke both endpoints and emit two CSVs (`lineups.csv` + `player_clutch_splits.csv`, rejected because AAP §0.2.3 inventory declares exactly one Lineups CSV — adding a second artifact requires a formal AAP scope change); (c) invoke only the clutch-split endpoint (rejected: `lineups.csv`'s key contract is league-dashboard lineup leaderboard, which only `leaguedashlineups` natively produces). | The AAP declares one Lineups CSV artifact with the 5-man-lineup key contract. The two endpoints under F-012 produce fundamentally incompatible key columns — one per lineup, one per player-split — and cannot be flattened into a single artifact without schema distortion. The disambiguation is independently documented in `docs/features/lineups.md` (the 38-vs-39-key symmetric-difference narrative) and asserted at the endpoint layer by `tests/unit/endpoints/test_lineups.py`. | The closest risk is that a future reviewer counting endpoints in AAP §0.1.3 (2 for Lineups) will flag `ingest_lineups.py` as missing an endpoint. Mitigation: (1) `tests/unit/pipelines/test_ingest_lineups.py::test_clutch_onoff_endpoint_is_not_invoked` codifies the scope in executable form; (2) `docs/features/lineups.md` narrates the key-column mismatch; (3) `docs/TRACEABILITY.md` F-012 row enumerates exactly one endpoint. If a future phase introduces `player_clutch_splits.csv`, this decision should be superseded and the deferred endpoint wired up in a companion pipeline rather than widening `ingest_lineups.py`. |

**Status:** Accepted — intentional scope reduction for this checkpoint. A superseding entry should be written when a second Lineups-family CSV enters scope.

**Related:** Written in response to Checkpoint 4 code review MAJOR finding #5c, which flagged the 1-of-2 endpoint implementation as a silent deviation from AAP §0.1.3's two-endpoint Lineups inventory.

### D-023 — Treat `docs/OBSERVABILITY.md` and `docs/dashboards/*` as the single source of truth for metric label keys and values

| Decision | Alternatives | Rationale | Risk |
|---|---|---|---|
| Establish that `docs/OBSERVABILITY.md` and `docs/dashboards/operator_dashboard.{json,md}` are the authoritative specification of metric *label keys* and *label values*. Code in `run.py`, `pipelines/*`, and `api/*` must emit metrics whose label keys and values exactly match those documented: `pipeline_runs_total{pipeline=<name>, outcome="success"|"error"}`, `pipeline_rows_written_total{pipeline=<name>, artifact=<filename>.csv}`, `games_failed_total{reason=<exception class name>}`. Tests assert on the documented shape, not on the historical emitted shape. | (a) Treat code as the source of truth and update docs when code changes (rejected: dashboards and alert rules are deployed artifacts — changing them retroactively breaks operator muscle memory and production queries; code is cheaper to change); (b) auto-generate docs from code (rejected: requires introducing a code-scraping tool and a new build step not prescribed in AAP §8; also inverts the correctness direction — the docs *are* the operator-facing contract); (c) use string constants defined once and imported by both code and docs (rejected: docs are Markdown and JSON and cannot import Python constants without the same build step that (b) introduces). | Observability is a consumer-facing contract: dashboards, alert rules, and operator runbooks query metrics by label key and value. Drift between code emission and documented queries *silently* breaks production monitoring — the `PipelineErrorOutcome` alert (`increase(pipeline_runs_total{outcome="error"}[24h]) > 0`) will never fire if code emits `status="failure"` instead of `outcome="error"`. The docs therefore encode the contract, and the code must honor it. Tests assert against the documented shape so drift is caught locally rather than in production. | The principal risk is that a later code change introduces a new metric whose label keys inadvertently diverge from the convention (for example `{"pipeline_name"}` instead of `{"pipeline"}`). Mitigation: (1) every metric added to `utils/metrics.py` must be documented in `docs/OBSERVABILITY.md` *before* it is emitted from code; (2) pipeline unit tests assert the full label dict (keys and values) at every emission site; (3) a future checkpoint may introduce a grep-based invariant test that scans for `inc("...", {...})` sites and cross-references them against the documented metric catalog. Until that invariant is added, drift will be caught only by manual review and by the Checkpoint 4-class code-review exercise. |

**Status:** Accepted. Supersedes the implicit "emit whatever labels feel natural" convention that produced Findings #2, #3, and #4 at Checkpoint 4.

**Related:** Written in response to the Checkpoint 4 code review findings that identified systemic label drift between code and docs. The remediation touched `run.py` (12 `pipeline_runs_total` emission sites), the five pipeline modules (six `pipeline_rows_written_total` emission sites combined), and `pipelines/ingest_games.py` (one `games_failed_total` emission site for Rule 6), plus approximately twenty assertion sites across the pipeline unit-test suite. D-023 defines the convention that must govern any future metric addition.

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
