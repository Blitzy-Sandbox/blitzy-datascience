"""Resumable checkpoint manager backed by ``output/checkpoint.json``.

This module implements Feature F-007 and satisfies Rule 5 of the
NBA Data Ingestion Pipeline product brief
(``docs/New_Product_Prompt_20260418.md`` §5, Rule 5): after every
successful endpoint pull the orchestrating pipeline records the
``(domain, key)`` pair here; on subsequent runs the pipeline queries
this manager via :meth:`CheckpointManager.get_pending` or
:meth:`CheckpointManager.is_completed` to skip already-completed units
of work.

Persistence semantics
---------------------
* **Synchronous.** Every mutation persists immediately to disk before
  :meth:`mark_completed` returns, so a crash between pulls cannot
  silently lose progress.
* **Atomic.** Writes go to a sibling temporary file (``<name>.tmp``)
  and are renamed via :meth:`pathlib.Path.replace` so readers never
  observe a partial-write. This is the idiom mandated by
  Agent Action Plan §0.5.1.2 and §0.7.2.5.
* **Roll-back on failure.** If the disk write fails mid-persist, the
  in-memory state is rolled back to its pre-mutation form. This
  preserves a strict invariant: a ``True`` answer from
  :meth:`is_completed` is never inconsistent with the on-disk
  manifest.

On-disk format
--------------
The manifest is a JSON object whose top-level keys are *domain*
strings (``"players"``, ``"teams"``, ``"games"``, ``"lineups"``,
``"schedule"``) and whose values are dicts of ``key -> ISO-8601 UTC
completion timestamp``. A representative document::

    {
      "games": {
        "0022500001": "2026-04-19T10:30:45+00:00",
        "0022500002": "2026-04-19T10:31:52+00:00"
      },
      "players": {
        "leaguedashplayerstats:2025-26": "2026-04-19T10:29:10+00:00"
      },
      "schedule": {
        "leaguegamefinder:2025-26": "2026-04-19T10:28:55+00:00"
      }
    }

The JSON is serialised with ``indent=2`` and ``sort_keys=True`` so
successive runs produce byte-identical files when no new keys have
been marked completed — useful for diff-based verification in
Validation Gate 8 (resume determinism).

Authoritative references
------------------------
* Agent Action Plan §0.1.3 — checkpoint manager motivation.
* Agent Action Plan §0.4.1.1 — public interface contract.
* Agent Action Plan §0.5.1.2 — Group 2 utility, atomic write idiom.
* Agent Action Plan §0.7.2.5 — Rule 5 binding constraint.
* Product brief §5 Rule 5 — synchronous update after every pull.

Public API
----------
:class:`CheckpointManager`
    The checkpoint manager. Instantiated once per CLI invocation,
    typically in ``run.py`` and passed to every pipeline via
    explicit constructor injection (AAP §0.4.1.2).

Concurrency
-----------
The pipeline is single-threaded by design (Rule 2 enforces a
sequential inter-request floor). Even so, a :class:`threading.RLock`
serialises every read and write of the in-memory state and every
persistence operation so that future parallelism (or test fixtures
using :class:`concurrent.futures.ThreadPoolExecutor`) cannot silently
corrupt the manifest. The lock is **re-entrant** so internal method
chains such as :meth:`reset` -> :meth:`_persist` can re-acquire it
without deadlock.

Observability
-------------
Every noteworthy event is logged via the project-wide correlation-ID
adapter returned by :func:`utils.logger.get_logger`:

    * ``DEBUG`` on startup when the file is missing or successfully
      loaded (with domain count).
    * ``WARNING`` when the on-disk manifest is malformed.
    * ``INFO`` on every successful :meth:`mark_completed` call.

These records carry the current correlation ID and flow to both the
stdout handler and the rotating file handler configured by
:mod:`utils.logger`, satisfying the project-level Observability rule
(AAP §0.7.3.1).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

import config
from utils.logger import get_logger


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# ``get_logger`` returns a :class:`logging.LoggerAdapter`
# (:class:`utils.correlation.CorrelationAdapter`) bound to the project-wide
# root handlers. The first call in a process transparently invokes
# ``utils.logger._configure`` which attaches stdout and rotating file handlers
# via :func:`config.ensure_directories`. This module therefore emits
# correlation-tagged log records on every checkpoint mutation, supporting the
# Observability rule (AAP §0.7.3.1) and Feature F-008.
logger = get_logger(__name__)


class CheckpointManager:
    """Resumable checkpoint manager for Rule 5 compliance.

    Every successful endpoint pull must call
    :meth:`mark_completed(domain, key)`; the entire state is flushed
    to disk synchronously after every mutation so that an interrupted
    run can resume deterministically on its next invocation.

    Parameters
    ----------
    path:
        Optional path to the JSON manifest file. Accepts either a
        :class:`str` or :class:`pathlib.Path`. Defaults to
        :data:`config.CHECKPOINT_PATH` (``output/checkpoint.json``)
        when omitted or ``None``. Tests typically inject a
        ``tmp_path``-scoped :class:`~pathlib.Path` to isolate fixture
        state from the real output directory.

    Attributes
    ----------
    _path:
        Absolute or relative :class:`pathlib.Path` to the on-disk
        manifest. Resolved once in :meth:`__init__`; never reassigned.
    _lock:
        Re-entrant :class:`threading.RLock` guarding every read and
        write of ``_state`` and every filesystem mutation.
    _state:
        In-memory mirror of the manifest. Structure:
        ``Dict[domain: str, Dict[key: str, timestamp: str]]``. A
        missing domain key is equivalent to no completed work in that
        domain.

    Notes
    -----
    The public interface is the five methods below plus
    :meth:`snapshot`. The two underscore-prefixed helpers
    (:meth:`_load`, :meth:`_persist`) are implementation details and
    MUST NOT be called by consumer code.
    """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        """Initialise the manager and hydrate state from disk.

        The constructor performs a single :meth:`_load` call to
        populate ``_state`` from the on-disk manifest (or start
        empty if the file does not exist or is malformed). No
        filesystem write occurs during construction — the manifest
        is only written when a pipeline calls :meth:`mark_completed`
        or :meth:`reset`.

        Parameters
        ----------
        path:
            Optional override for the manifest location. ``None``
            defers to :data:`config.CHECKPOINT_PATH`. A string input
            is coerced to :class:`pathlib.Path` via the :class:`Path`
            constructor.
        """
        # Resolve the persistence path. Lazy dereference of
        # ``config.CHECKPOINT_PATH`` inside the constructor (rather
        # than as a default-argument value) means test fixtures that
        # monkeypatch the module constant see the updated value on
        # each instantiation, as required by AAP §0.4.1.2.
        if path is None:
            self._path: Path = Path(config.CHECKPOINT_PATH)
        else:
            self._path = Path(path)

        # Re-entrant lock so internal chains like reset -> _persist
        # can reacquire without deadlock. RLock is preferred over
        # Lock because the cost of re-entrance is negligible and the
        # correctness gain is real.
        self._lock: threading.RLock = threading.RLock()

        # In-memory mirror of the on-disk manifest.
        self._state: Dict[str, Dict[str, str]] = {}

        # Hydrate from disk. If the file is missing or malformed the
        # state remains an empty dict and an appropriate log event
        # is emitted by ``_load``.
        self._load()

    # ------------------------------------------------------------------
    # Private helpers — load and atomic persist
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load the manifest from disk into :attr:`_state`.

        Behaviour matrix
        ----------------

        =====================================  ====================
        Condition                              Outcome
        =====================================  ====================
        File does not exist                    Empty state, DEBUG
                                               log, no exception.
        File exists but is empty / whitespace  Empty state, DEBUG
                                               log, no exception.
        File exists but JSON is malformed      Empty state, WARNING
                                               log, no exception.
        File exists but top-level is not a     Empty state, WARNING
        dict                                   log, no exception.
        File exists and parses cleanly         State populated from
                                               normalised content;
                                               DEBUG log records the
                                               number of domains.
        =====================================  ====================

        The method never raises — a corrupt manifest degrades
        gracefully to a fresh-start scenario rather than blocking the
        pipeline entirely. This is the correct failure mode for
        Rule 5: a missing checkpoint means *redo the work*, which is
        expensive but safe; aborting would be worse.
        """
        with self._lock:
            if not self._path.exists():
                # Absent manifest is the normal first-run condition.
                # Start with empty state; a persist will be triggered
                # on the first ``mark_completed`` call.
                self._state = {}
                logger.debug(
                    "No checkpoint file at %s; starting fresh",
                    self._path,
                )
                return

            # File exists: read and parse defensively.
            try:
                raw = self._path.read_text(encoding="utf-8")
                # Treat a blank or whitespace-only file as "no
                # progress recorded yet" — an easy mistake to make
                # with manual edits — and short-circuit to empty
                # state without an error.
                parsed = json.loads(raw) if raw.strip() else {}
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Checkpoint file %s is malformed (%s); "
                    "starting with empty state",
                    self._path,
                    exc,
                )
                self._state = {}
                return

            if not isinstance(parsed, dict):
                logger.warning(
                    "Checkpoint file %s top-level is not a dict; "
                    "starting with empty state",
                    self._path,
                )
                self._state = {}
                return

            # Normalise: every domain value must be a dict of
            # str -> str. Entries that do not match this shape are
            # silently dropped. This protects the rest of the code
            # path from having to handle unexpected types on every
            # lookup.
            normalised: Dict[str, Dict[str, str]] = {}
            for domain, entries in parsed.items():
                if isinstance(entries, dict):
                    normalised[str(domain)] = {
                        str(k): str(v)
                        for k, v in entries.items()
                        if isinstance(k, str)
                    }
            self._state = normalised
            logger.debug(
                "Loaded checkpoint from %s: %d domain(s) tracked",
                self._path,
                len(self._state),
            )

    def _persist(self) -> None:
        """Atomically write :attr:`_state` to :attr:`_path`.

        Implementation detail
        ---------------------
        The method writes the serialised JSON to a *sibling*
        temporary file (``<name>.tmp`` in the same parent directory
        as ``_path``) and then calls :meth:`pathlib.Path.replace` to
        rename it over the target. This ensures atomicity on both
        POSIX (``rename(2)``) and Windows (``MoveFileEx`` with
        ``MOVEFILE_REPLACE_EXISTING``) — readers never observe a
        partial-write. A sibling, not a ``tempfile.NamedTemporaryFile``
        in ``/tmp``, is required because :meth:`Path.replace` is only
        atomic across paths on the same filesystem / volume.

        Side effects
        ------------
        * Creates :attr:`_path`'s parent directory tree if absent
          (``parents=True, exist_ok=True``).
        * Writes a new temp file (``<name>.tmp``) that briefly
          coexists with the target until the rename completes.

        Raises
        ------
        OSError
            Propagated as-is from the filesystem call if the parent
            directory cannot be created, the temp file cannot be
            written, or the rename fails. Callers (currently only
            :meth:`mark_completed` and :meth:`reset`) decide whether
            to roll back in-memory state before re-raising.
        """
        with self._lock:
            # Ensure the parent directory exists. The first-time
            # persist from a fresh clone typically runs before
            # ``ensure_directories`` has had a chance to create
            # ``output/`` via another code path, so this call is
            # defensive but necessary.
            self._path.parent.mkdir(parents=True, exist_ok=True)

            # ``sort_keys=True`` gives byte-deterministic output for
            # Gate 8 (resume-determinism diff checks). ``indent=2``
            # keeps the manifest human-readable for operator
            # troubleshooting — the folder brief explicitly specifies
            # this.
            payload = json.dumps(
                self._state,
                indent=2,
                sort_keys=True,
            )

            # Build the sibling temp path. Using ``with_suffix`` with
            # the existing suffix appended guarantees we write next
            # to the real file, not into a different directory. For
            # ``output/checkpoint.json`` the temp path is
            # ``output/checkpoint.json.tmp``.
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")

            # Atomic rename. After this line returns, readers see the
            # new content; before this line, they see the old content
            # (or nothing). There is no in-between state visible on
            # the filesystem.
            tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_completed(self, domain: str, key: str) -> bool:
        """Return whether ``(domain, key)`` has been marked completed.

        This is the membership query every pipeline's enumeration
        loop uses to skip already-persisted work. It is O(1) in the
        size of the manifest.

        Parameters
        ----------
        domain:
            Pipeline bucket — typically one of the
            ``config.DOMAIN_*`` constants (``"players"``,
            ``"teams"``, ``"games"``, ``"lineups"``, ``"schedule"``).
        key:
            Identifier within the domain. For ``games`` this is the
            ``GAME_ID`` (e.g. ``"0022500001"``); for endpoint-level
            domains it is typically a composite like
            ``"leaguedashplayerstats:2025-26"``.

        Returns
        -------
        bool
            ``True`` if the key exists under the domain in the
            manifest, ``False`` otherwise (including when the domain
            itself has no entries).

        Raises
        ------
        TypeError
            If ``domain`` or ``key`` is not a string. This defensive
            check catches accidental ``tuple`` or ``Path`` inputs at
            the call site rather than producing an obscure
            ``KeyError`` deeper in the lookup.
        """
        if not isinstance(domain, str) or not isinstance(key, str):
            raise TypeError("domain and key must be strings")
        with self._lock:
            return key in self._state.get(domain, {})

    def mark_completed(self, domain: str, key: str) -> None:
        """Record that ``(domain, key)`` finished and persist to disk.

        The ISO-8601 UTC timestamp (``isoformat(timespec="seconds")``)
        becomes the value for the key in the manifest. The write is
        synchronous and atomic — see :meth:`_persist`. If the disk
        write fails, the in-memory state is rolled back to its
        pre-mutation form so a retry produces a consistent outcome.

        Critical invariants
        -------------------
        * **Persist happens after the in-memory mutation.** That
          way, a successful persist reflects the new state.
        * **Rollback happens on OSError.** Without this, a later
          :meth:`is_completed` call would return ``True`` while the
          on-disk manifest still says the work was never done,
          causing data loss on the next restart.
        * **INFO log is emitted only after a successful persist.**
          Logging before persisting would create a false positive
          in the audit trail.

        Parameters
        ----------
        domain:
            Pipeline bucket; must be a non-empty string.
        key:
            Identifier within the domain; must be a non-empty string.

        Raises
        ------
        TypeError
            If ``domain`` or ``key`` is not a string.
        ValueError
            If ``domain`` or ``key`` is an empty string.
        OSError
            Propagated from :meth:`_persist` if the atomic write
            fails. The in-memory state is rolled back before the
            exception propagates.
        """
        if not isinstance(domain, str) or not isinstance(key, str):
            raise TypeError("domain and key must be strings")
        if not domain or not key:
            raise ValueError("domain and key must be non-empty strings")

        with self._lock:
            # ``setdefault`` creates the domain bucket lazily on
            # first use so the manifest contains exactly those
            # domains that have seen at least one completion.
            bucket = self._state.setdefault(domain, {})

            # Capture the previous value so we can roll back exactly
            # to the pre-call state (not merely delete the entry,
            # which would be wrong if we were overwriting an
            # existing entry with a newer timestamp).
            previous = bucket.get(key)

            # ISO-8601 UTC. ``timespec="seconds"`` strips sub-second
            # precision — sufficient for diagnostics and keeps the
            # manifest compact. ``timezone.utc`` guarantees portable
            # strings regardless of the operator's local TZ.
            timestamp = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            bucket[key] = timestamp

            try:
                self._persist()
            except OSError:
                # Roll back to pre-mutation state. This maintains
                # the invariant that ``is_completed(domain, key)``
                # never returns True for a key not present in the
                # persisted manifest.
                if previous is None:
                    bucket.pop(key, None)
                    # Additionally, if the domain had no entries
                    # prior to this call we leave the empty dict in
                    # place; the next successful persist will either
                    # populate it or a subsequent full reload will
                    # normalise it away. The in-memory empty dict is
                    # harmless — it does not cause false positives
                    # because membership checks use dict __contains__.
                else:
                    bucket[key] = previous
                raise

            # Success path: the on-disk manifest now reflects the
            # new completion. Emit the INFO log last so the log
            # reliably indicates durable progress.
            logger.info(
                "Checkpoint: marked completed domain=%s key=%s",
                domain,
                key,
            )

    def get_pending(
        self,
        domain: str,
        all_keys: Iterable[str],
    ) -> List[str]:
        """Return the subset of ``all_keys`` not yet marked completed.

        Pipelines enumerate the universe of keys they intend to pull
        (e.g. every ``GAME_ID`` for the season) and call this method
        to obtain just the still-to-do subset. Order of ``all_keys``
        is preserved in the returned list so callers that use a
        semantic ordering (chronological game IDs, for example) are
        not disturbed.

        Parameters
        ----------
        domain:
            Pipeline bucket string; must be a string (may be empty,
            in which case the result is simply ``list(all_keys)``
            because no domain can have empty-string as a key).
        all_keys:
            Any iterable of strings. Generators are supported and
            are consumed exactly once. Non-string elements are
            coerced to :class:`str` for the membership check — this
            lets callers pass collections of typed IDs (e.g. numeric
            game indices) without pre-conversion, though the
            returned list contains the original elements verbatim.

        Returns
        -------
        List[str]
            The subset of ``all_keys`` whose string form is not
            present under ``domain`` in the manifest, in their
            original input order.

        Raises
        ------
        TypeError
            If ``domain`` is not a string.
        """
        if not isinstance(domain, str):
            raise TypeError("domain must be a string")

        # Snapshot the completed set under the lock so we don't hold
        # the lock while iterating over a potentially expensive
        # generator. Iterating ``all_keys`` outside the lock also
        # avoids a deadlock risk if the generator itself tries to
        # call back into the manager (e.g. an unusual test fixture).
        with self._lock:
            completed = set(self._state.get(domain, {}).keys())

        # Order-preserving filter. Accepts any iterable including
        # generators; ``str(k)`` coercion is defensive so callers
        # can pass non-string IDs without surprises.
        return [k for k in all_keys if str(k) not in completed]

    def reset(self, domain: Optional[str] = None) -> None:
        """Remove checkpoint entries for a domain (or every domain).

        This is intended for test fixtures and operator
        troubleshooting — pipelines themselves do not call it. The
        update is persisted immediately so that a subsequent
        instantiation of :class:`CheckpointManager` reads the
        cleared state from disk.

        Parameters
        ----------
        domain:
            When ``None`` (the default), every domain is cleared and
            the manifest becomes ``{}`` on disk. Otherwise, only the
            named domain is removed; other domains are untouched.
            Calling :meth:`reset` with a domain that does not exist
            is a no-op at the state level but still triggers a
            persist call (so the file is created with the current
            state if it did not yet exist).

        Raises
        ------
        OSError
            Propagated from :meth:`_persist` if the atomic write
            fails. Unlike :meth:`mark_completed`, this method does
            not roll back the in-memory change on failure because
            :meth:`reset` is diagnostic / operator-initiated and the
            caller is expected to surface the error rather than
            retry silently. Operators can re-invoke :meth:`reset`
            after fixing the underlying filesystem issue.
        """
        with self._lock:
            if domain is None:
                # Wholesale wipe: useful for a fresh start in tests.
                self._state = {}
            else:
                # Targeted removal: ``dict.pop`` with a default is a
                # no-op when the key is absent.
                self._state.pop(domain, None)
            self._persist()

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        """Return a deep copy of the current in-memory state.

        Intended for diagnostics, integration tests (Gate 8 resume
        determinism), and operator-facing introspection. Callers
        receive an isolated copy — mutations to the returned dict
        do NOT affect the manager's internal state, and vice versa.
        This guarantees that the manager's state can never be
        corrupted by a caller that mutates the snapshot.

        Returns
        -------
        Dict[str, Dict[str, str]]
            A fresh :class:`dict` mapping every tracked domain to a
            fresh :class:`dict` of its ``key -> timestamp`` entries.
            Empty domains (should any exist in memory) are included
            so the snapshot is a faithful representation of the
            in-memory state.
        """
        with self._lock:
            # Two-level dict comprehension produces independent
            # inner dicts; a plain ``dict(self._state)`` would share
            # the inner references.
            return {
                domain: dict(entries)
                for domain, entries in self._state.items()
            }
