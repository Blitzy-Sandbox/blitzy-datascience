"""Unit tests for :mod:`storage.csv_writer` (Feature F-006, Rules 4 and 7).

This module is the production-side enforcement point for Operational
Rule 7 (Pluggable Storage -- the sole ``DataFrame.to_csv`` call site in
production code) and the defence-in-depth enforcement of Rule 4 (Flat
CSV Output -- no nested ``dict`` / ``list`` cells in CSV output). The
tests in this file correspond directly to QA report Phase 2 cases A.1
through A.12 (primary path) and J.1 through J.6 (adversarial path).

Scope
-----
1. :class:`storage.csv_writer.BaseWriter` -- abstract-class semantics
   (direct instantiation rejected by the :mod:`abc` machinery).
2. :class:`storage.csv_writer.CSVWriter` -- construction with default
   and custom ``output_dir``; nested-directory creation on first use;
   rejection of non-directory ``output_dir`` (existing file) with
   :class:`FileExistsError` from the underlying
   :meth:`pathlib.Path.mkdir` call.
3. :meth:`CSVWriter.write` -- happy-path round-trip, atomic write
   (``.tmp`` suffix plus :meth:`pathlib.Path.replace`), input
   validation (:class:`TypeError` on non-DataFrame input;
   :class:`ValueError` on empty, non-string, path-separator-bearing,
   or dot-only ``name``), Rule 4 defence-in-depth
   (:class:`ValueError` on ``dict`` / ``list`` cells), path
   confinement (``target.relative_to(output_dir)``), UTF-8 round-trip
   fidelity, empty-DataFrame support, ``NaN`` / ``None`` handling,
   overwrite semantics, symlinked ``output_dir``, NUL byte in
   ``name``, filename-too-long, CSV-special-character columns, and
   large-DataFrame persistence.
4. Structured INFO logs -- ``csv_writer.write.start`` and
   ``csv_writer.write.complete`` must be emitted on every successful
   write.

Design invariants enforced by this test module
----------------------------------------------
* Fixtures defined in :mod:`tests.conftest` are the only source of
  DataFrames, canonical CSV readers, and writable temporary
  directories. No fixture logic is duplicated here.
* Each test is hermetic: it uses :func:`pytest.tmp_path` or the
  ``tmp_output_dir`` fixture, does not touch the real ``output/``
  directory, and does not import :mod:`requests`.
* Rule 7 is also enforced by the grep-based invariant test
  ``tests/invariants/test_rule7_basewriter_only.py`` (scope of a
  future checkpoint); this file verifies the *behaviour* that the
  writer performs the atomic-write dance correctly.
* A.7's "output_dir points to an existing file" case expects
  :class:`FileExistsError` -- the *observed* behaviour of the
  underlying :meth:`pathlib.Path.mkdir(exist_ok=True)` call when the
  supplied path already exists as a non-directory. The
  :class:`NotADirectoryError` branch in the production code is
  therefore unreachable on CPython 3.12 and is not asserted by this
  test module.

References
----------
* AAP §0.5.1.5 -- Group 5 construction plan (``storage/csv_writer.py``).
* AAP §0.7.2.4 -- Rule 4 (Flat CSV Output).
* AAP §0.7.2.7 -- Rule 7 (Pluggable Storage; sole ``to_csv`` call site).
* AAP §0.4.2.2 -- Atomic write semantics.
* QA Report "Findings by Feature/Module" -> "Feature: F-006" -- A.1--A.12, J.1--J.6.
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

import config
from storage.csv_writer import BaseWriter, CSVWriter


# ===========================================================================
# A.1 -- File existence and import surface
# ===========================================================================


class TestA1_ModuleSurface:
    """A.1: ``storage/csv_writer.py`` and ``storage/__init__.py`` exist and
    expose the expected public surface."""

    def test_basewriter_and_csvwriter_are_importable(self) -> None:
        """The two public classes are importable from the package root."""
        # These imports are performed at module scope above; the test
        # simply asserts that the symbols are the expected objects.
        assert BaseWriter is not None
        assert CSVWriter is not None

    def test_csvwriter_is_subclass_of_basewriter(self) -> None:
        """CSVWriter must specialise the BaseWriter interface."""
        assert issubclass(CSVWriter, BaseWriter)


# ===========================================================================
# A.3 -- BaseWriter abstract-class semantics
# ===========================================================================


class TestA3_BaseWriterAbstract:
    """A.3: :class:`BaseWriter` is abstract; direct instantiation raises
    :class:`TypeError`. :class:`CSVWriter` is a concrete subclass and
    exposes a callable :meth:`write`."""

    def test_a3_1_basewriter_cannot_be_instantiated(self) -> None:
        """A.3.1: ``BaseWriter()`` raises ``TypeError`` via the :mod:`abc`
        machinery."""
        with pytest.raises(TypeError) as excinfo:
            BaseWriter()  # type: ignore[abstract]
        # The CPython error message contains "abstract class BaseWriter";
        # we match on the fragment that is stable across CPython versions
        # rather than asserting the full wording.
        assert "BaseWriter" in str(excinfo.value)
        assert "abstract" in str(excinfo.value).lower()

    def test_a3_2_basewriter_is_reported_as_abstract(self) -> None:
        """A.3.2: :func:`inspect.isabstract` confirms BaseWriter is abstract."""
        assert inspect.isabstract(BaseWriter) is True

    def test_a3_3_csvwriter_is_subclass_of_basewriter(self) -> None:
        """A.3.3: ``issubclass(CSVWriter, BaseWriter)`` is ``True``."""
        assert issubclass(CSVWriter, BaseWriter)

    def test_a3_4_csvwriter_instance_is_basewriter(self, tmp_path: Path) -> None:
        """A.3.4: A constructed :class:`CSVWriter` is also a
        :class:`BaseWriter` instance (Liskov substitution)."""
        writer = CSVWriter(output_dir=tmp_path)
        assert isinstance(writer, BaseWriter)
        assert isinstance(writer, CSVWriter)

    def test_a3_5_csvwriter_write_is_callable(self, tmp_path: Path) -> None:
        """A.3.5: The concrete ``write`` method is callable on an instance."""
        writer = CSVWriter(output_dir=tmp_path)
        assert callable(writer.write)


# ===========================================================================
# A.10 -- Construction: default and custom output_dir; nested creation
# ===========================================================================


class TestA10_Construction:
    """A.10: Construction honors explicit ``output_dir``; falls back to
    ``config.OUTPUT_DIR`` when absent; creates missing intermediate
    directories."""

    def test_a10_1_explicit_output_dir_is_honored(self, tmp_path: Path) -> None:
        """A.10.1: A caller-supplied ``output_dir`` is used verbatim (after
        resolution)."""
        target = tmp_path / "custom-output"
        target.mkdir()
        writer = CSVWriter(output_dir=target)
        assert writer.output_dir == target.resolve()

    def test_a10_2_default_output_dir_comes_from_config(
        self,
        tmp_output_dir: Path,
    ) -> None:
        """A.10.2: With no explicit override, the writer falls back to
        :data:`config.OUTPUT_DIR`. The ``tmp_output_dir`` fixture
        monkeypatches :data:`config.OUTPUT_DIR` so we can assert this
        without touching the real ``output/`` directory."""
        writer = CSVWriter()
        assert writer.output_dir == tmp_output_dir.resolve()

    def test_a10_3_nested_output_dir_is_created_on_construction(
        self,
        tmp_path: Path,
    ) -> None:
        """A.10.3: Multi-level missing directories are created on
        construction via ``mkdir(parents=True, exist_ok=True)``."""
        nested = tmp_path / "a" / "b" / "c" / "output"
        assert not nested.exists()
        writer = CSVWriter(output_dir=nested)
        assert nested.exists()
        assert nested.is_dir()
        assert writer.output_dir == nested.resolve()

    def test_a10_idempotent_construction_on_existing_dir(
        self,
        tmp_path: Path,
    ) -> None:
        """Constructing two writers on the same existing directory is
        idempotent (``exist_ok=True``)."""
        target = tmp_path / "output"
        target.mkdir()
        _ = CSVWriter(output_dir=target)
        _ = CSVWriter(output_dir=target)  # Must not raise.


# ===========================================================================
# A.7 -- output_dir points to a file (not a directory)
# ===========================================================================


class TestA7_OutputDirNotDirectory:
    """A.7: Construction rejects an ``output_dir`` that names an existing
    regular file. CPython's :meth:`pathlib.Path.mkdir(exist_ok=True)`
    raises :class:`FileExistsError` (``errno 17``) in this case; the
    :class:`NotADirectoryError` branch in the production code is an
    unreachable defensive safety net and is not asserted here."""

    def test_a7_existing_file_rejected(self, tmp_path: Path) -> None:
        """When ``output_dir`` resolves to an existing regular file,
        construction raises :class:`FileExistsError`."""
        file_path = tmp_path / "i-am-a-file"
        file_path.write_text("not a directory")
        with pytest.raises(FileExistsError):
            CSVWriter(output_dir=file_path)


# ===========================================================================
# A.2 -- Happy-path write; round-trip preserves rows and columns
# ===========================================================================


class TestA2_HappyPathWrite:
    """A.2: A flat DataFrame is written to ``<output_dir>/<name>.csv``;
    round-tripping it through :func:`pandas.read_csv` preserves the
    logical row and column structure."""

    def test_a2_write_returns_resolved_target_path(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """``write`` returns the absolute resolved path of the written CSV."""
        writer = CSVWriter(output_dir=tmp_path)
        returned = writer.write(flat_df, "players", "2025-26")
        assert returned == (tmp_path / "players.csv").resolve()
        assert returned.is_file()

    def test_a2_roundtrip_preserves_rows_and_columns(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """Reading back the CSV yields the same rows and columns."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(flat_df, "players", "2025-26")
        read_back = csv_reader(target)
        assert list(read_back.columns) == list(flat_df.columns)
        assert len(read_back) == len(flat_df)
        # Numeric columns survive int/float round-trip; string columns
        # compare cleanly. Values-level comparison uses ``equals`` after
        # dtype alignment to tolerate Int64 -> int64 drift on CSV read.
        assert read_back["PLAYER_ID"].tolist() == flat_df["PLAYER_ID"].tolist()
        assert read_back["PLAYER_NAME"].tolist() == flat_df["PLAYER_NAME"].tolist()

    def test_a2_write_does_not_emit_index_column(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """The on-disk CSV must not contain a leading unnamed index
        column -- :meth:`CSVWriter.write` passes ``index=False``."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(flat_df, "players", "2025-26")
        # The first comma-separated token of the header is
        # ``"PLAYER_ID"`` (the first real column), not
        # ``"Unnamed: 0"`` / ``"index"``.
        first_line = target.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.split(",")[0] == "PLAYER_ID"


# ===========================================================================
# A.4 -- Rule 4 defence-in-depth: nested dict / list cells rejected
# ===========================================================================


class TestA4_Rule4DefenceInDepth:
    """A.4: :class:`CSVWriter` re-asserts Rule 4 before writing. DataFrames
    whose cells contain a :class:`dict` or :class:`list` must raise
    :class:`ValueError` and leave the filesystem untouched."""

    def test_a4_dict_cell_rejected(
        self,
        tmp_path: Path,
        nested_df: pd.DataFrame,
    ) -> None:
        """A DataFrame whose cell values are :class:`dict` is rejected."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            writer.write(nested_df, "players", "2025-26")
        msg = str(excinfo.value)
        assert "Rule 4" in msg
        assert "'players'" in msg or "players" in msg
        assert "STATS" in msg  # Offending column enumerated.

    def test_a4_list_cell_rejected(
        self,
        tmp_path: Path,
        list_cell_df: pd.DataFrame,
    ) -> None:
        """A DataFrame whose cell values are :class:`list` is rejected."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            writer.write(list_cell_df, "teams", "2025-26")
        msg = str(excinfo.value)
        assert "Rule 4" in msg
        assert "ROSTER" in msg

    def test_a4_no_file_written_on_rejection(
        self,
        tmp_path: Path,
        nested_df: pd.DataFrame,
    ) -> None:
        """On rejection, neither the target CSV nor the ``.tmp`` sibling
        is created: the writer fails before the ``to_csv`` call."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError):
            writer.write(nested_df, "players", "2025-26")
        assert not (tmp_path / "players.csv").exists()
        assert not (tmp_path / "players.csv.tmp").exists()

    def test_a4_empty_dataframe_is_not_flagged_as_rule4_violation(
        self,
        tmp_path: Path,
        empty_df: pd.DataFrame,
    ) -> None:
        """A.4 + A.8 intersection: empty DataFrames pass the Rule 4 guard
        (``_assert_flat`` short-circuits when ``df.empty``)."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(empty_df, "players", "2025-26")
        assert target.is_file()

    def test_a4_mixed_nested_columns_report_all_offenders(
        self,
        tmp_path: Path,
    ) -> None:
        """Multiple offending columns are all listed in the error."""
        bad = pd.DataFrame(
            {
                "PLAYER_ID": [1, 2],
                "STATS": [{"PTS": 30}, {"PTS": 20}],
                "ROSTER": [[101, 102], [201, 202]],
                "NAME": ["Jokic", "Doncic"],
            }
        )
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            writer.write(bad, "players", "2025-26")
        msg = str(excinfo.value)
        assert "STATS" in msg
        assert "ROSTER" in msg


# ===========================================================================
# A.5 / A.6 -- Unsafe name rejection (path traversal, absolute, nested, etc.)
# ===========================================================================


class TestA5A6_UnsafeNameRejected:
    """A.5, A.6: Unsafe artifact ``name`` values are rejected before any
    filesystem operation occurs. No file is created outside the
    configured ``output_dir``."""

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../escape",
            "..\\escape",
            "/absolute/path",
            "sub/dir/file",
            "file\\with\\backslash",
            ".",
            "..",
        ],
    )
    def test_a5_path_traversal_names_rejected(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
        bad_name: str,
    ) -> None:
        """Names containing path separators or dot-only components raise
        :class:`ValueError`."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError):
            writer.write(flat_df, bad_name, "2025-26")

    def test_a5_empty_name_rejected(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """An empty ``name`` raises :class:`ValueError` ("requires a
        non-empty string name")."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            writer.write(flat_df, "", "2025-26")
        assert "non-empty string name" in str(excinfo.value)

    @pytest.mark.parametrize(
        "non_str_name",
        [None, 1, 1.5, ["players"], {"name": "players"}, (b"players",)],
    )
    def test_a5_non_string_name_rejected(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
        non_str_name: object,
    ) -> None:
        """Non-string ``name`` values raise :class:`ValueError`."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError):
            # A non-string value is deliberately supplied; type-ignore so
            # static checkers do not mistake the negative test for a bug.
            writer.write(flat_df, non_str_name, "2025-26")  # type: ignore[arg-type]

    def test_a6_no_escape_file_created_outside_output_dir(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """No file is created at any of the path-traversal target locations
        after a rejected call."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError):
            writer.write(flat_df, "../escape", "2025-26")
        # The escape target would have been ``tmp_path.parent / "escape.csv"``;
        # confirm nothing was written.
        assert not (tmp_path.parent / "escape.csv").exists()
        # And nothing was written inside the actual output_dir either.
        assert list(tmp_path.iterdir()) == []


# ===========================================================================
# A.8 -- Empty DataFrame produces header-only CSV
# ===========================================================================


class TestA8_EmptyDataFrame:
    """A.8: An empty DataFrame yields a header-only CSV. Round-tripping it
    preserves the column schema and reports zero rows."""

    def test_a8_empty_df_writes_header_only(
        self,
        tmp_path: Path,
        empty_df: pd.DataFrame,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """The on-disk file contains exactly one line (the header)."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(empty_df, "players", "2025-26")
        assert target.is_file()
        assert target.stat().st_size > 0  # Header present -> non-zero size.
        # Exactly one line (header) with the expected columns.
        text = target.read_text(encoding="utf-8")
        # pandas emits a trailing newline after the header row.
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0] == "PLAYER_ID,PLAYER_NAME,PTS"
        # Round-trip preserves columns and produces zero rows.
        read_back = csv_reader(target)
        assert list(read_back.columns) == list(empty_df.columns)
        assert len(read_back) == 0


# ===========================================================================
# A.9 -- UTF-8 Unicode round-trip
# ===========================================================================


class TestA9_UnicodeRoundTrip:
    """A.9: Unicode cell values (Latin diacritics, CJK, emoji, embedded
    quotes) survive a round-trip through the CSV layer byte-for-byte."""

    def test_a9_unicode_cells_preserve_through_roundtrip(
        self,
        tmp_path: Path,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """A representative Unicode payload round-trips losslessly."""
        unicode_df = pd.DataFrame(
            {
                "PLAYER_NAME": [
                    "Nikola Jokić",
                    "Luka Dončić",
                    "山田太郎",
                    "🏀⭐",
                    'Kawhi "The Klaw" Leonard',
                ],
                "PLAYER_ID": [203999, 1629029, 9999001, 9999002, 202695],
            }
        )
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(unicode_df, "players", "2025-26")
        # Assert byte-level UTF-8 encoding at the file level.
        raw = target.read_bytes()
        assert "Jokić".encode("utf-8") in raw
        assert "Dončić".encode("utf-8") in raw
        assert "山田太郎".encode("utf-8") in raw
        assert "🏀⭐".encode("utf-8") in raw
        # Full logical round-trip through pandas.
        read_back = csv_reader(target)
        assert read_back["PLAYER_NAME"].tolist() == unicode_df["PLAYER_NAME"].tolist()


# ===========================================================================
# A.11 -- NaN / None cells are not flagged as nested types
# ===========================================================================


class TestA11_MissingValues:
    """A.11: :class:`None` / :class:`float.nan` scalar cells are treated as
    primitives (never flagged by Rule 4) and are written as empty CSV
    cells."""

    def test_a11_none_and_nan_are_scalars(
        self,
        tmp_path: Path,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """``None`` and ``float('nan')`` are persisted as empty CSV
        cells; round-trip preserves the count of missing values."""
        df = pd.DataFrame(
            {
                "PLAYER_ID": [1, 2, 3],
                "OPT_FIELD": [None, float("nan"), "hello"],
            }
        )
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(df, "players", "2025-26")
        read_back = csv_reader(target)
        # Two of the three OPT_FIELD cells must be NaN after round-trip.
        assert read_back["OPT_FIELD"].isna().sum() == 2
        assert read_back["OPT_FIELD"].iloc[2] == "hello"


# ===========================================================================
# A.12 / J.6 -- Atomic write: .tmp cleaned up; overwrite semantics
# ===========================================================================


class TestA12J6_AtomicWrite:
    """A.12, J.6: The atomic-write dance leaves no ``.tmp`` sibling after
    success, and a subsequent write on the same name replaces the
    previous file without leaving a ``.tmp`` residue."""

    def test_a12_no_tmp_leftover_after_successful_write(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """After a successful ``write``, the ``players.csv.tmp`` sibling
        does not exist."""
        writer = CSVWriter(output_dir=tmp_path)
        writer.write(flat_df, "players", "2025-26")
        assert (tmp_path / "players.csv").exists()
        assert not (tmp_path / "players.csv.tmp").exists()

    def test_j6_overwrite_replaces_atomically(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """Writing a second, different DataFrame to the same name
        replaces the on-disk file atomically. No ``.tmp`` residue
        remains after the second write."""
        writer = CSVWriter(output_dir=tmp_path)
        writer.write(flat_df, "players", "2025-26")
        # Second payload -- fewer rows, different values.
        second_df = pd.DataFrame(
            {
                "PLAYER_ID": [1],
                "PLAYER_NAME": ["Kawhi Leonard"],
                "PTS": [24.7],
                "AST": [3.6],
                "REB": [6.1],
                "TEAM_ABBREVIATION": ["LAC"],
                "NOTES": [None],
            }
        )
        target = writer.write(second_df, "players", "2025-26")
        read_back = csv_reader(target)
        assert len(read_back) == 1
        assert read_back["PLAYER_NAME"].iloc[0] == "Kawhi Leonard"
        assert not (tmp_path / "players.csv.tmp").exists()


# ===========================================================================
# A.2 input-type validation -- TypeError on non-DataFrame
# ===========================================================================


class TestInputTypeValidation:
    """:meth:`CSVWriter.write` rejects non-:class:`pandas.DataFrame` input
    with :class:`TypeError`. This complements A.2 (happy path) by
    asserting the negative case on the first arg."""

    @pytest.mark.parametrize(
        "non_df_value",
        [
            "not-a-dataframe",
            123,
            1.5,
            None,
            ["a", "b"],
            {"PLAYER_ID": [1]},
            (1, 2, 3),
            object(),
        ],
    )
    def test_non_dataframe_input_raises_type_error(
        self,
        tmp_path: Path,
        non_df_value: object,
    ) -> None:
        """Non-DataFrame input raises :class:`TypeError` with a message
        that names the expected type."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(TypeError) as excinfo:
            writer.write(non_df_value, "players", "2025-26")  # type: ignore[arg-type]
        assert "pandas DataFrame" in str(excinfo.value)


# ===========================================================================
# J.1 -- Symlinked output_dir
# ===========================================================================


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Windows symlink creation requires elevated privileges in CI.",
)
class TestJ1_SymlinkedOutputDir:
    """J.1: When ``output_dir`` is itself a symlink to a real directory,
    path confinement still holds: writes land in the resolved target,
    and ``target.relative_to(output_dir)`` succeeds because the stored
    ``output_dir`` is already ``.resolve()``-d."""

    def test_j1_symlinked_output_dir_writes_to_real_target(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """The CSV lands inside the resolved target directory."""
        real = tmp_path / "real-output"
        real.mkdir()
        symlink = tmp_path / "link-to-output"
        symlink.symlink_to(real, target_is_directory=True)

        writer = CSVWriter(output_dir=symlink)
        # output_dir is the resolved real path, not the symlink.
        assert writer.output_dir == real.resolve()
        target = writer.write(flat_df, "players", "2025-26")
        assert target == (real / "players.csv").resolve()
        assert target.is_file()


# ===========================================================================
# J.2 -- NUL byte in name
# ===========================================================================


class TestJ2_NullByteName:
    """J.2: A ``name`` containing a NUL byte (``\\x00``) is rejected by
    CPython at the syscall boundary. The writer's explicit validation
    does not catch a single NUL byte in an otherwise-valid string (no
    path separator, not dot-only), so the rejection manifests as a
    :class:`ValueError` from :meth:`pathlib.Path.resolve` /
    :func:`os.fspath` -- "embedded null byte"."""

    def test_j2_null_byte_name_rejected(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """A NUL byte in ``name`` ultimately raises :class:`ValueError`
        with the CPython "embedded null byte" message."""
        writer = CSVWriter(output_dir=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            writer.write(flat_df, "bad\x00name", "2025-26")
        # CPython's ``os.fspath``/``Path.resolve`` emits "embedded null
        # byte"; we match case-insensitively so minor wording drift
        # across CPython patch versions does not brittle-fail the test.
        assert "null byte" in str(excinfo.value).lower()


# ===========================================================================
# J.3 -- Name exceeds filesystem maximum length
# ===========================================================================


class TestJ3_FilenameTooLong:
    """J.3: A 300-character ``name`` exceeds the 255-byte POSIX filename
    limit. The OS raises :class:`OSError` (``errno 36``,
    ``ENAMETOOLONG``) when the writer attempts to create the
    ``.tmp`` file."""

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Windows NTFS filename limits differ; posix-only assertion.",
    )
    def test_j3_very_long_name_rejected_by_os(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """A 300-char ``name`` raises :class:`OSError` from the
        ``to_csv`` call. The writer's explicit validation only
        rejects path separators and dot-only names -- OS-level
        limits are delegated to the filesystem."""
        writer = CSVWriter(output_dir=tmp_path)
        long_name = "x" * 300
        with pytest.raises(OSError) as excinfo:
            writer.write(flat_df, long_name, "2025-26")
        # ENAMETOOLONG = 36 on Linux. We compare against the constant
        # from the errno module rather than the integer literal to
        # stay portable across POSIX variants.
        import errno
        assert excinfo.value.errno == errno.ENAMETOOLONG


# ===========================================================================
# J.4 -- Columns whose labels contain CSV special characters
# ===========================================================================


class TestJ4_CsvSpecialCharColumns:
    """J.4: Column labels that contain CSV special characters (``,``,
    ``\\n``, ``"``) are handled correctly by pandas' CSV-quoting
    default; round-trip preserves the labels."""

    def test_j4_special_character_columns_roundtrip(
        self,
        tmp_path: Path,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """Commas and newlines inside column labels survive round-trip."""
        df = pd.DataFrame(
            {
                "col,with,commas": [1, 2, 3],
                "col\nwith\nnewlines": ["a", "b", "c"],
                'col"with"quotes': [10.0, 20.0, 30.0],
            }
        )
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(df, "special_cols", "2025-26")
        read_back = csv_reader(target)
        assert list(read_back.columns) == list(df.columns)
        assert len(read_back) == 3


# ===========================================================================
# J.5 -- Large DataFrame
# ===========================================================================


class TestJ5_LargeDataFrame:
    """J.5: A 1000-row, 50-column DataFrame is written cleanly. The file
    size must be non-trivial and round-trip must preserve the logical
    structure."""

    def test_j5_large_dataframe_writes_cleanly(
        self,
        tmp_path: Path,
        large_df: pd.DataFrame,
        csv_reader: Callable[[Path], pd.DataFrame],
    ) -> None:
        """A 1000x50 frame writes without error and round-trips."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(large_df, "large_payload", "2025-26")
        assert target.is_file()
        assert target.stat().st_size > 10_000  # Sanity floor.
        read_back = csv_reader(target)
        assert read_back.shape == large_df.shape
        assert list(read_back.columns) == list(large_df.columns)


# ===========================================================================
# Structured INFO log events: csv_writer.write.start / .complete
# ===========================================================================


class TestWriteInfoLogs:
    """Structured observability contract: every successful write emits
    ``csv_writer.write.start`` and ``csv_writer.write.complete`` at
    INFO level on the ``storage.csv_writer`` logger. These events are
    consumed by the operator dashboard (AAP §0.7.3.1) and must not
    regress silently."""

    def test_write_emits_start_and_complete_info_logs(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both INFO log events are emitted on a successful write,
        with the expected structured field values."""
        writer = CSVWriter(output_dir=tmp_path)
        with caplog.at_level(logging.INFO, logger="storage.csv_writer"):
            writer.write(flat_df, "players", "2025-26")
        messages = [r.getMessage() for r in caplog.records]
        start_events = [m for m in messages if "csv_writer.write.start" in m]
        complete_events = [m for m in messages if "csv_writer.write.complete" in m]
        assert len(start_events) == 1, (
            f"Expected exactly one start event, got {start_events}"
        )
        assert len(complete_events) == 1, (
            f"Expected exactly one complete event, got {complete_events}"
        )
        # Structured fields on the start event.
        assert "name=players" in start_events[0]
        assert "season=2025-26" in start_events[0]
        assert f"rows={len(flat_df)}" in start_events[0]
        assert f"cols={len(flat_df.columns)}" in start_events[0]
        # Complete event reports the file size.
        assert "name=players" in complete_events[0]
        assert "bytes=" in complete_events[0]

    def test_no_complete_log_when_validation_fails(
        self,
        tmp_path: Path,
        nested_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A Rule 4 rejection happens before the ``.start`` log event;
        neither the ``.start`` nor the ``.complete`` event fires.
        Assertion formulated around the ``.complete`` event because it
        is the observability proof of successful persistence."""
        writer = CSVWriter(output_dir=tmp_path)
        with caplog.at_level(logging.INFO, logger="storage.csv_writer"):
            with pytest.raises(ValueError):
                writer.write(nested_df, "players", "2025-26")
        messages = [r.getMessage() for r in caplog.records]
        assert not any("csv_writer.write.complete" in m for m in messages)


# ===========================================================================
# Supplementary: Rule 7 behavioural anchor
# ===========================================================================


class TestRule7Behavioural:
    """Behavioural anchor for Rule 7 (the sole ``to_csv`` call site). The
    static grep-based invariant test lives in
    ``tests/invariants/test_rule7_basewriter_only.py``; here we only
    prove the *behavioural* claim: every successful write produces a
    file on disk whose first bytes match the column header."""

    def test_rule7_write_produces_file_whose_first_line_is_the_header(
        self,
        tmp_path: Path,
        flat_df: pd.DataFrame,
    ) -> None:
        """The first line of the written CSV is the comma-joined header."""
        writer = CSVWriter(output_dir=tmp_path)
        target = writer.write(flat_df, "players", "2025-26")
        first_line = target.read_text(encoding="utf-8").splitlines()[0]
        expected = ",".join(str(c) for c in flat_df.columns)
        assert first_line == expected


# ===========================================================================
# Supplementary: config-default wiring verified for default constructor
# ===========================================================================


class TestConfigDefaultWiring:
    """Verify that :class:`CSVWriter` reads :data:`config.OUTPUT_DIR` at
    construction time (not at class-definition time), so
    :func:`monkeypatch.setattr` on ``config.OUTPUT_DIR`` is honoured
    per the ``tmp_output_dir`` fixture contract. This is the Gate 12
    (Config Propagation Tracing) anchor for F-006."""

    def test_default_ctor_reads_config_output_dir_dynamically(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Monkeypatching ``config.OUTPUT_DIR`` after module import
        still influences :class:`CSVWriter` default construction."""
        custom = tmp_path / "dynamically-configured"
        custom.mkdir()
        monkeypatch.setattr(config, "OUTPUT_DIR", custom)
        writer = CSVWriter()
        assert writer.output_dir == custom.resolve()
