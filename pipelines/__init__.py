"""NBA Data Ingestion Pipelines — per-domain ETL orchestrators (F-009 – F-013).

This package contains exactly one module per NBA data domain; each module
owns the full enumerate → fetch → normalize → write → checkpoint lifecycle
for its feature (AAP §0.4.3, §0.5.1.6). The five pipeline submodules are:

* :mod:`pipelines.ingest_schedule` — Feature **F-013 Schedule**. Pulls the
  league-wide ``leaguegamefinder`` endpoint and emits
  ``output/schedule.csv``. The Schedule domain is also the source of the
  deduplicated ``GAME_ID`` list consumed by :mod:`pipelines.ingest_games`;
  that coupling is expressed through the
  :func:`endpoints.schedule.enumerate_game_ids` helper, never by reading
  ``output/schedule.csv`` back from disk.
* :mod:`pipelines.ingest_games` — Feature **F-011 Games**. Iterates the
  season's ``GAME_ID`` list and emits both ``output/games.csv`` (box
  scores) and ``output/play_by_play.csv``. This is the SOLE pipeline that
  implements **Rule 6** fail-safe per-``GAME_ID`` iteration
  (AAP §0.7.2.6) — a failure on a single game is logged at WARNING,
  increments the ``games_failed_total`` counter, and iteration continues.
* :mod:`pipelines.ingest_teams` — Feature **F-010 Teams**. Pulls
  ``leaguedashteamstats`` and emits ``output/teams.csv``.
* :mod:`pipelines.ingest_players` — Feature **F-009 Players**. Pulls the
  league-wide player statistics endpoints and emits both
  ``output/players.csv`` (from ``leaguedashplayerstats``) and
  ``output/player_tracking.csv`` (from ``leaguedashptstats``).
* :mod:`pipelines.ingest_lineups` — Feature **F-012 Lineups**. Pulls
  ``leaguedashlineups`` and emits ``output/lineups.csv``.

Pipeline contract
-----------------

Every pipeline submodule exposes exactly one public entry point::

    def run(
        client,
        writer,
        checkpoint,
        season: str,
        logger: Optional[logging.LoggerAdapter] = None,
        metrics: Optional[Any] = None,
    ) -> None: ...

The ``client`` (:class:`api.nba_client.NBAClient`), ``writer``
(:class:`storage.csv_writer.BaseWriter`), and ``checkpoint``
(:class:`utils.checkpoint.CheckpointManager`) collaborators are composed
by :mod:`run` (the Click CLI entry point) and injected as keyword
arguments — see AAP §0.4.1.2 for the composition contract. Pipelines
never instantiate HTTP clients, writers, or checkpoint managers
themselves; doing so would violate the dependency-injection seam used by
the unit tests in :mod:`tests.unit.pipelines`.

Binding runtime ordering
------------------------

Per AAP §0.4.5, the ``python run.py all --season <season>`` command
invokes the five pipelines in this exact order::

    schedule → games → teams → players → lineups

Schedule runs first because Games depends on its ``GAME_ID``
enumeration; the remaining three pipelines have no cross-pipeline
coupling and their ordering after Games is stable-but-incidental.
**Standalone invocations** such as ``python run.py games --season
<season>`` re-enumerate ``GAME_IDs`` on demand via
:func:`endpoints.schedule.enumerate_game_ids`, so the Games pipeline
does not require a prior Schedule invocation when run in isolation.

Rule scope
----------

* **Rule 1 — single HTTP client** (AAP §0.7.2.1). No module in this
  package imports :mod:`requests`; every HTTP call is routed through
  :class:`api.nba_client.NBAClient` via the endpoint wrappers.
  Enforced by :mod:`tests.invariants.test_rule1_sole_http_client`.
* **Rule 4 — flat CSV** (AAP §0.7.2.4). Every pipeline flattens the
  upstream ``resultSets`` envelope via
  :func:`utils.schema_normalizer.normalize_result_sets`, which asserts
  flatness before returning.
* **Rule 5 — checkpoint after every pull** (AAP §0.7.2.5). Every
  pipeline invokes
  :meth:`utils.checkpoint.CheckpointManager.mark_completed` immediately
  after each successful :meth:`storage.csv_writer.BaseWriter.write`.
  Verified for each pipeline by the corresponding
  ``tests/unit/pipelines/test_ingest_*.py`` module.
* **Rule 6 — fail-safe game iteration** (AAP §0.7.2.6). Applies ONLY to
  :mod:`pipelines.ingest_games`; all other pipelines propagate
  exceptions as-is so transient upstream failures can be observed and
  retried via checkpoint resume.
* **Rule 7 — pluggable storage** (AAP §0.7.2.7). No pipeline writes
  CSV files via the underlying dataframe API directly; every CSV
  write is routed through
  :class:`storage.csv_writer.BaseWriter.write`. Enforced by
  :mod:`tests.invariants.test_rule7_basewriter_only`.

Package re-exports and :data:`__all__`
--------------------------------------

This marker re-exports the five orchestrator submodules in alphabetical
order so that :mod:`run` and the unit tests can use the canonical
form::

    from pipelines import (
        ingest_games,
        ingest_lineups,
        ingest_players,
        ingest_schedule,
        ingest_teams,
    )

without triggering flake8's ``F401`` (imported-but-unused) check — the
submodule names appear in the :data:`__all__` tuple below, which
flake8 consults to confirm that a re-export is intentional. The tuple
is immutable by construction so downstream tooling cannot mutate the
package's public surface at runtime; it is also the authoritative
enumeration consulted by the CLI registration test
(:mod:`tests.unit.test_cli`) and by documentation tooling that emits a
canonical symbol inventory.

Side effects
------------

Importing this package has exactly one effect: the five submodules are
eagerly loaded so that ``pipelines.ingest_<domain>`` is resolvable as
an attribute immediately after ``import pipelines`` returns. The
submodules themselves perform no filesystem I/O, no network I/O, no
logging, and no metric registration at import time — they only declare
functions, dataclasses, and module-level constants. Any runtime cost
(HTTP, CSV writing, checkpoint persistence) is deferred to the
:func:`run` entry points and thus to the caller's invocation of
:mod:`run`.
"""

# ---------------------------------------------------------------------------
# Submodule re-exports — five ingest orchestrators.
#
# The absolute import form ``from pipelines import ...`` is used in
# preference to the relative form ``from . import ...`` to mirror the
# convention established by :mod:`endpoints` (see
# ``endpoints/__init__.py``) and to make the import graph unambiguously
# traceable by readers who grep for fully-qualified package paths.
#
# The ordering below is alphabetical — matching :data:`__all__` — for
# diff stability. The AAP §0.4.5 runtime ordering (schedule → games →
# teams → players → lineups) is a concern of :mod:`run`, not of this
# package marker; the order in which the submodules are imported here
# has no observable runtime effect because none of them execute
# side-effecting code at import time.
# ---------------------------------------------------------------------------
from pipelines import (
    ingest_games,
    ingest_lineups,
    ingest_players,
    ingest_schedule,
    ingest_teams,
)

# ---------------------------------------------------------------------------
# __all__ manifest — canonical five-name public API surface of the package.
#
# Ordered strictly alphabetically (AAP §0.5.1.6 Phase 4 Style Constraint)
# for easy diff/review; the length MUST remain equal to 5 and every
# name MUST resolve to a submodule of this package that exposes a
# public ``run`` callable. The container is a tuple (immutable by
# construction) so downstream tooling cannot mutate the public API
# surface at runtime.
# ---------------------------------------------------------------------------
__all__ = (
    "ingest_games",
    "ingest_lineups",
    "ingest_players",
    "ingest_schedule",
    "ingest_teams",
)
