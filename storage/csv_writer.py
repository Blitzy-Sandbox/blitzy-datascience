"""CSV storage backend and pluggable :class:`BaseWriter` abstract interface.

This module implements **Feature F-006 (CSV Writer with Pluggable Interface)**
and is the enforcement point for **Operational Rule 7 (Pluggable Storage)**
defined in ``docs/New_Product_Prompt_20260418.md`` §5.

Invariants
----------
* :meth:`CSVWriter.write` is the **SOLE** call site of
  :meth:`pandas.DataFrame.to_csv` in production code. The grep-based
  invariant test ``tests/invariants/test_rule7_basewriter_only.py`` verifies
  that ``.to_csv(`` appears nowhere else under ``pipelines/``,
  ``endpoints/``, ``utils/``, ``api/``, ``run.py``, or ``config.py``.
* :class:`BaseWriter` is an :class:`abc.ABC` — direct instantiation raises
  :class:`TypeError`. The interface is preserved as an extension point for
  future database (PostgreSQL, DuckDB, SQLite, BigQuery, Snowflake),
  object-storage (S3, GCS, Azure Blob), or columnar (Parquet, Avro, ORC)
  writers. None of those concrete backends ship in this release per
  Agent Action Plan §0.6.2.2.
* Writes are atomic-ish: the :class:`~pandas.DataFrame` is emitted to a
  sibling temporary file whose name is the target with ``.tmp`` appended
  to its suffix (e.g., ``players.csv.tmp``), then
  :meth:`pathlib.Path.replace` renames it atomically over the target. A
  crash between the ``to_csv`` and ``replace`` calls leaves the previous
  target (if any) intact. See AAP §0.4.2.2.
* Rule 4 (Flat CSV Output) is enforced defensively here in
  :meth:`CSVWriter._assert_flat` in addition to the upstream enforcement
  in :mod:`utils.schema_normalizer`. This ensures that even if a future
  producer constructs a :class:`~pandas.DataFrame` outside the normalizer
  path, a nested ``dict`` or ``list`` cell can never be persisted.

References
----------
* Agent Action Plan §0.1.3 — Technical Interpretation (sole ``to_csv`` caller)
* Agent Action Plan §0.4.1.1 — Integration Responsibility
* Agent Action Plan §0.4.2.2 — Atomic-ish write semantics
* Agent Action Plan §0.5.1.5 — Group 5 (Storage) construction plan
* Agent Action Plan §0.7.2.4 — Rule 4 (Flat CSV Output)
* Agent Action Plan §0.7.2.7 — Rule 7 (Pluggable Storage)
* Agent Action Plan §0.6.2.2 — Out-of-scope storage backends
* Product brief ``docs/New_Product_Prompt_20260418.md`` §3 and §5
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import pandas as pd

import config
from utils.logger import get_logger


# Module-level logger. ``get_logger`` is idempotent and thread-safe (it
# configures the root handlers exactly once behind a module-level lock in
# ``utils/logger.py``), so calling it at import time is safe and mirrors the
# convention used by the rest of the project.
_log = get_logger(__name__)


class BaseWriter(ABC):
    """Abstract pluggable-storage interface.

    Concrete subclasses persist :class:`pandas.DataFrame` objects identified
    by a stable logical ``name`` for a given ``season``, returning the
    resolved artifact path.

    The interface is deliberately minimal so future backends (PostgreSQL,
    DuckDB, Parquet, S3, ...) can be introduced without altering calling
    code. Operational Rule 7 (Pluggable Storage) requires that pipelines
    interact with this interface exclusively — never with backend-specific
    APIs such as :meth:`pandas.DataFrame.to_csv`, SQLAlchemy engines, or
    ``boto3`` clients.

    This release ships exactly one concrete implementation, :class:`CSVWriter`.
    No database writer is included; the extension point is preserved
    intentionally per Agent Action Plan §0.6.2.2.

    Notes
    -----
    Direct instantiation is rejected by the :mod:`abc` machinery:

    >>> BaseWriter()  # doctest: +SKIP
    Traceback (most recent call last):
        ...
    TypeError: Can't instantiate abstract class BaseWriter ...
    """

    @abstractmethod
    def write(self, df: pd.DataFrame, name: str, season: str) -> Path:
        """Persist ``df`` under logical name ``name`` for ``season``.

        Parameters
        ----------
        df : pandas.DataFrame
            Flat :class:`~pandas.DataFrame`. No cell may contain a
            :class:`dict` or :class:`list` per Rule 4 (Flat CSV Output).
            Implementations SHOULD validate this as defense-in-depth.
        name : str
            Stable artifact name (e.g., ``"players"``, ``"games"``). For
            CSV backends this is the filename stem; for database backends
            it is the table name; for object-storage backends it is the
            key stem. Must be a non-empty string without path separators.
        season : str
            NBA season string (e.g., ``"2025-26"``). Reserved for
            partitioning backends. The CSV backend logs it for
            traceability but does not embed it in the filename in the
            current release.

        Returns
        -------
        pathlib.Path
            Resolved absolute :class:`~pathlib.Path` to the written
            artifact. For non-filesystem backends this may be a synthetic
            path (e.g., ``s3://bucket/key`` modeled as a ``Path``).

        Raises
        ------
        NotImplementedError
            Always — this is an abstract method.
        """
        raise NotImplementedError


class CSVWriter(BaseWriter):
    """Concrete :class:`BaseWriter` that emits UTF-8 flat CSV files.

    :meth:`CSVWriter.write` is the ONLY place in production code that calls
    :meth:`pandas.DataFrame.to_csv`. Pipelines route every write through
    this method so that Rule 7 (Pluggable Storage) holds by construction.

    Writes are atomic-ish: the :class:`~pandas.DataFrame` is written to a
    sibling temporary file (``<target>.tmp``) and then
    :meth:`pathlib.Path.replace` is used to atomically rename it into
    place. A crash between the ``to_csv`` and ``replace`` calls leaves the
    previous target (if any) intact. See Agent Action Plan §0.4.2.2.

    Parameters
    ----------
    output_dir : pathlib.Path, optional
        Directory in which CSV files will be written. Defaults to
        :data:`config.OUTPUT_DIR` when ``None``. The directory and all
        parents are created if they do not exist.

    Attributes
    ----------
    output_dir : pathlib.Path
        The resolved absolute output directory. Read-only property.

    Examples
    --------
    Typical usage from ``run.py``::

        from storage.csv_writer import CSVWriter
        import config

        writer = CSVWriter(output_dir=config.OUTPUT_DIR)
        path = writer.write(df, name=config.CSV_PLAYERS, season="2025-26")
    """

    #: Cell value types that would violate Rule 4 (Flat CSV Output). Kept
    #: as a class-level attribute so tests can reference it and so the
    #: tuple is constructed exactly once at class-definition time rather
    #: than on every ``_assert_flat`` invocation.
    _NESTED_TYPES = (dict, list)

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        """Construct a writer rooted at ``output_dir``.

        Parameters
        ----------
        output_dir : pathlib.Path, optional
            Directory in which CSV files will be written. Defaults to
            :data:`config.OUTPUT_DIR` when ``None``. The directory and
            all intermediate parents are created if they do not exist.

        Raises
        ------
        NotADirectoryError
            If ``output_dir`` resolves to an existing path that is not a
            directory (e.g., an existing regular file).
        """
        # Resolve the base directory. When no override is supplied, fall
        # back to config.OUTPUT_DIR — the single read-site that satisfies
        # Validation Gate 12 (Config Propagation Tracing) for this module.
        base = Path(output_dir) if output_dir is not None else Path(config.OUTPUT_DIR)

        # Ensure the canonical project directories (OUTPUT_DIR, LOG_DIR)
        # exist. This mirrors the behaviour of utils.logger.get_logger and
        # keeps first-run bootstrap contained to the module that actually
        # needs the filesystem. The function is idempotent.
        config.ensure_directories()

        # If the caller supplied an override outside OUTPUT_DIR, make sure
        # *that* directory exists too. This must come BEFORE the
        # ``is_dir`` check so that on a fresh filesystem the directory is
        # materialised first and only then validated.
        base.mkdir(parents=True, exist_ok=True)
        if not base.is_dir():
            raise NotADirectoryError(
                f"CSVWriter output_dir is not a directory: {base}"
            )

        # Store the absolute resolved path so downstream path-confinement
        # checks operate on a canonical form that cannot be fooled by
        # ``.`` / ``..`` components or by symlink trickery inside the
        # artifact ``name``.
        self._output_dir: Path = base.resolve()

        _log.debug("csv_writer.initialized output_dir=%s", self._output_dir)

    @property
    def output_dir(self) -> Path:
        """Resolved absolute output directory.

        Returns
        -------
        pathlib.Path
            The absolute :class:`~pathlib.Path` under which this writer
            emits CSV artifacts. Equivalent to
            ``Path(output_dir_arg).resolve()`` or
            ``Path(config.OUTPUT_DIR).resolve()`` when no override was
            provided at construction time.
        """
        return self._output_dir

    def write(self, df: pd.DataFrame, name: str, season: str) -> Path:
        """Write ``df`` to ``<output_dir>/<name>.csv`` atomically.

        Parameters
        ----------
        df : pandas.DataFrame
            Flat :class:`~pandas.DataFrame`. Every cell must be a
            primitive; ``dict`` / ``list`` cells raise :class:`ValueError`
            (Rule 4 defense-in-depth).
        name : str
            Logical artifact name; must be a non-empty string with no
            path separators and not equal to ``"."`` or ``".."``. The
            seven canonical names come from :mod:`config` constants
            (``CSV_PLAYERS``, ``CSV_PLAYER_TRACKING``, ``CSV_TEAMS``,
            ``CSV_GAMES``, ``CSV_PLAY_BY_PLAY``, ``CSV_LINEUPS``,
            ``CSV_SCHEDULE``).
        season : str
            Season string (e.g., ``"2025-26"``). Logged for traceability;
            not used in filename construction in this release.

        Returns
        -------
        pathlib.Path
            Resolved absolute :class:`~pathlib.Path` to the written CSV.

        Raises
        ------
        TypeError
            If ``df`` is not a :class:`pandas.DataFrame`.
        ValueError
            If ``name`` is empty, not a string, contains a path separator,
            equals ``"."`` or ``".."``, resolves outside ``output_dir``,
            or if Rule 4 is violated.
        OSError
            On underlying filesystem errors (permission denied, disk
            full, etc.). Propagated from :meth:`pandas.DataFrame.to_csv`
            or :meth:`pathlib.Path.replace` as-is.
        """
        # ---- Input validation -------------------------------------------------
        if not isinstance(df, pd.DataFrame):
            raise TypeError(
                "CSVWriter.write expected a pandas DataFrame, got "
                f"{type(df).__name__}"
            )
        if not isinstance(name, str) or not name:
            raise ValueError("CSVWriter.write requires a non-empty string name")
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"CSVWriter.write received unsafe name {name!r}")

        # ---- Rule 4 defense-in-depth -----------------------------------------
        # utils/schema_normalizer.py enforces this upstream, but the writer
        # re-asserts at the persistence boundary so any future producer that
        # bypasses the normalizer (e.g., a pipeline that manually adds a
        # computed column) still cannot persist nested data.
        self._assert_flat(df, name=name)

        # ---- Path resolution and confinement ---------------------------------
        target = (self._output_dir / f"{name}.csv").resolve()
        # Confine output to ``output_dir`` so a malicious or buggy ``name``
        # cannot traverse into arbitrary filesystem locations. Using
        # ``relative_to`` (rather than ``is_relative_to``) keeps the failure
        # mode explicit: it raises :class:`ValueError` with information about
        # both endpoints of the comparison.
        try:
            target.relative_to(self._output_dir)
        except ValueError as exc:
            raise ValueError(
                "CSVWriter.write refused to write outside output_dir: "
                f"target={target} output_dir={self._output_dir}"
            ) from exc

        # Temporary sibling file for the atomic-write pattern. For a target
        # of ``players.csv`` this yields ``players.csv.tmp``. Keeping the
        # temporary file in the SAME DIRECTORY as the target guarantees that
        # ``Path.replace`` (which wraps ``os.rename``) stays on the same
        # filesystem and is therefore atomic on POSIX and Windows (NTFS).
        tmp = target.with_suffix(target.suffix + ".tmp")

        _log.info(
            "csv_writer.write.start name=%s season=%s target=%s rows=%d cols=%d",
            name,
            season,
            target,
            len(df),
            len(df.columns),
        )

        # ---- SOLE CALL SITE of DataFrame.to_csv in production code (Rule 7) --
        df.to_csv(tmp, index=False, encoding="utf-8")

        # ---- Atomic rename (AAP §0.4.2.2) ------------------------------------
        # On POSIX, ``Path.replace`` uses ``os.rename`` which is atomic within
        # a single filesystem. On Windows, ``os.rename`` has been atomic on
        # NTFS since Python 3.3 (via ``MoveFileExW`` with
        # ``MOVEFILE_REPLACE_EXISTING``). Because ``tmp`` and ``target`` share
        # the same parent directory they are guaranteed to be on the same
        # filesystem, so the atomicity guarantee holds in both environments.
        tmp.replace(target)

        _log.info(
            "csv_writer.write.complete name=%s season=%s target=%s bytes=%d",
            name,
            season,
            target,
            target.stat().st_size,
        )

        return target

    @classmethod
    def _assert_flat(cls, df: pd.DataFrame, *, name: str) -> None:
        """Raise :class:`ValueError` if any cell in ``df`` is a dict or list.

        Parameters
        ----------
        df : pandas.DataFrame
            The :class:`~pandas.DataFrame` to inspect.
        name : str
            Logical artifact name, surfaced in the error message so
            operators can identify the offending dataset without having
            to read the stack trace.

        Raises
        ------
        ValueError
            If any cell in ``df`` is a :class:`dict` or :class:`list`.
            The error message includes the names of the offending columns
            so the fault can be traced back to the upstream producer.

        Notes
        -----
        Empty DataFrames short-circuit: an empty DataFrame trivially
        satisfies Rule 4 because it contains no cells.

        The implementation prefers :meth:`pandas.DataFrame.map`
        (available since pandas 2.1) and falls back to
        :meth:`pandas.DataFrame.applymap` on pandas 2.0 where
        :meth:`~pandas.DataFrame.map` does not yet accept a callable.
        Both APIs produce an element-wise boolean DataFrame of identical
        shape.
        """
        if df.empty:
            return

        # Prefer DataFrame.map (pandas >= 2.1); fall back to applymap on the
        # 2.0.x line where DataFrame.map does not yet exist. Runtime-verified
        # environments target pandas 2.3.x so the ``df.map`` branch is the
        # hot path; ``applymap`` is retained for the full ``>=2.0,<3`` span
        # declared in ``requirements.txt``.
        element_map = getattr(df, "map", None)
        if callable(element_map):
            nested_mask = df.map(lambda x: isinstance(x, cls._NESTED_TYPES))
        else:  # pragma: no cover - exercised only against pandas < 2.1
            nested_mask = df.applymap(lambda x: isinstance(x, cls._NESTED_TYPES))

        if bool(nested_mask.any().any()):
            bad_cols = [str(col) for col in nested_mask.columns[nested_mask.any()]]
            raise ValueError(
                "CSVWriter.write refused Rule 4 violation: DataFrame for "
                f"{name!r} contains nested cells (dict/list). "
                f"Affected columns: {bad_cols}"
            )
