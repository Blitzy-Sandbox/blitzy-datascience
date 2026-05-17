"""Unit tests for :mod:`storage.csv_writer` (Feature F-006, Rules 4 and 7).

This module is the sole unit-test surface for :mod:`storage.csv_writer` and
exercises every documented behaviour of the two exported classes:

* :class:`storage.csv_writer.BaseWriter` — the abstract pluggable-storage
  base class whose ``write(df, name, season) -> Path`` method must be
  implemented by any concrete writer.
* :class:`storage.csv_writer.CSVWriter` — the concrete implementation and
  the single production caller of :meth:`pandas.DataFrame.to_csv`
  (Rule 7, AAP §0.7.2.7).

Tests are grouped into eight classes, each asserting a distinct slice of
the production contract:

1. :class:`TestBaseWriterAbstractEnforcement` — abstract-method discipline
   on :class:`BaseWriter` (direct instantiation raises ``TypeError``,
   ``"write"`` is in ``__abstractmethods__``, subclass of :class:`abc.ABC`).
2. :class:`TestCSVWriterConstruction` — default-vs-override
   ``output_dir`` handling, directory creation, resolution to an absolute
   path, file-in-place rejection, and property exposure.
3. :class:`TestCSVWriterHappyPath` — Rule 7 round-trip: the sole
   ``to_csv`` caller produces a real, UTF-8, index-free CSV that can be
   read back with byte-exact Unicode fidelity (Dončić, Jokić).
4. :class:`TestCSVWriterAtomicWrite` — ``*.tmp`` + ``Path.replace`` atomic
   swap semantics; no temp file remains on success; same-name writes
   overwrite cleanly.
5. :class:`TestCSVWriterInputValidation` — ``TypeError`` and
   ``ValueError`` branches of the argument-validation cascade with the
   exact error-message substrings.
6. :class:`TestCSVWriterRule4` — defence-in-depth rejection of
   ``dict``/``list`` cells at the writer layer, naming the offending
   column(s) (Rule 4, AAP §0.7.2.4).
7. :class:`TestCSVWriterPathConfinement` — the resolved target path is
   a child of ``output_dir`` and traversal attempts produce no side
   effects outside the output directory.
8. :class:`TestCSVWriterLogging` — INFO-level ``csv_writer.write.start``
   and ``csv_writer.write.complete`` events carrying the season and
   target filename for operator observability (AAP §0.7.3.1).

References
----------
* AAP §0.5.1.5 (Group 5 — Storage) — construction contract and sole
  ``to_csv`` call-site rule.
* AAP §0.7.2.4 (Rule 4 — Flat CSV) and §0.7.2.7 (Rule 7 — Pluggable
  Storage).
* ``docs/New_Product_Prompt_20260418.md`` §5 — operational rules.
* ``storage/csv_writer.py`` — unit under test.
* ``tests/conftest.py`` — canonical fixtures
  (``flat_df``, ``nested_df``, ``list_cell_df``, ``empty_df``,
  ``tmp_output_dir``, ``csv_reader``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from storage.csv_writer import BaseWriter, CSVWriter


# ---------------------------------------------------------------------------
# 1. BaseWriter abstract enforcement (F-006)
# ---------------------------------------------------------------------------


class TestBaseWriterAbstractEnforcement:
    """BaseWriter must behave as a strict abstract base class.

    The abstract contract is the foundation of the pluggable-storage
    invariant (Rule 7): any concrete writer must provide its own
    ``write`` implementation, and BaseWriter itself cannot be used as a
    fallback.
    """

    def test_base_writer_cannot_be_instantiated_directly(self) -> None:
        """Direct instantiation of the ABC must raise ``TypeError``."""
        with pytest.raises(TypeError, match="abstract"):
            BaseWriter()  # type: ignore[abstract]

    def test_base_writer_declares_write_as_abstract(self) -> None:
        """``write`` must be listed in ``__abstractmethods__``."""
        assert "write" in BaseWriter.__abstractmethods__

    def test_base_writer_is_subclass_of_abc(self) -> None:
        """BaseWriter participates in the :mod:`abc` hierarchy."""
        from abc import ABC

        assert issubclass(BaseWriter, ABC)


# ---------------------------------------------------------------------------
# 2. CSVWriter construction (F-006)
# ---------------------------------------------------------------------------


class TestCSVWriterConstruction:
    """Construction-time contracts for :class:`CSVWriter`.

    The writer must accept ``None`` (falling back to ``config.OUTPUT_DIR``)
    or an explicit override, create any missing parent directories,
    resolve the final path to an absolute form, and refuse to operate
    when the supplied path already exists as a non-directory.
    """

    def test_csvwriter_default_uses_config_output_dir(
        self, tmp_output_dir: Path
    ) -> None:
        """Bare ``CSVWriter()`` resolves to the monkeypatched config dir."""
        writer = CSVWriter()

        assert writer.output_dir == tmp_output_dir.resolve()

    def test_csvwriter_custom_output_dir_override(self, tmp_path: Path) -> None:
        """Explicit ``output_dir`` overrides the config fallback."""
        custom = tmp_path / "custom_output"

        writer = CSVWriter(output_dir=custom)

        assert writer.output_dir == custom.resolve()
        assert custom.is_dir()

    def test_csvwriter_creates_output_dir_if_missing(self, tmp_path: Path) -> None:
        """Missing parent directories must be created via ``mkdir(parents=True)``."""
        deep = tmp_path / "deeply" / "nested" / "path"
        assert not deep.exists()

        writer = CSVWriter(output_dir=deep)

        assert deep.exists()
        assert deep.is_dir()
        assert writer.output_dir == deep.resolve()

    def test_csvwriter_output_dir_is_resolved_absolute(self, tmp_path: Path) -> None:
        """The stored ``output_dir`` must be an absolute, resolved path."""
        writer = CSVWriter(output_dir=tmp_path / "rel")

        assert writer.output_dir.is_absolute()

    def test_csvwriter_raises_when_output_dir_points_to_file(
        self, tmp_path: Path
    ) -> None:
        """A non-directory path must be rejected at construction time.

        The underlying ``Path.mkdir(parents=True, exist_ok=True)`` call
        raises ``FileExistsError`` on CPython 3.12 when the target
        already exists as a regular file; a fallback ``is_dir()`` check
        in the production code raises ``NotADirectoryError`` in the
        (unreachable on this platform) branch where ``mkdir`` would
        otherwise succeed. Both exceptions are subclasses of
        :class:`OSError`, which is the permissive catch used here.
        """
        file_path = tmp_path / "iam_a_file.txt"
        file_path.write_text("hello", encoding="utf-8")

        with pytest.raises(OSError):
            CSVWriter(output_dir=file_path)

    def test_csvwriter_is_instance_of_base_writer(
        self, tmp_output_dir: Path
    ) -> None:
        """CSVWriter satisfies the :class:`BaseWriter` contract by inheritance."""
        writer = CSVWriter()

        assert isinstance(writer, BaseWriter)

    def test_csvwriter_output_dir_is_a_property(self, tmp_path: Path) -> None:
        """``output_dir`` is a read-only property, not a plain attribute.

        Exposing it as a property protects against future refactors that
        might accidentally allow direct reassignment (which would break
        path-confinement guarantees).
        """
        descriptor = type(
            CSVWriter(output_dir=tmp_path / "p")
        ).__dict__["output_dir"]

        assert isinstance(descriptor, property)


# ---------------------------------------------------------------------------
# 3. CSVWriter happy-path write (Rule 7)
# ---------------------------------------------------------------------------


class TestCSVWriterHappyPath:
    """End-to-end Rule 7 round-trip: the sole ``to_csv`` caller is correct.

    Every test in this class exercises the real pandas ``to_csv`` path;
    the writer's output is read back via the ``csv_reader`` fixture
    (which never calls ``to_csv`` itself — Rule 7 is strictly preserved
    in the test surface).
    """

    def test_write_rule7_returns_resolved_absolute_path(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """``write`` returns the resolved absolute :class:`Path` of the file."""
        writer = CSVWriter()

        result = writer.write(flat_df, "players", "2025-26")

        assert isinstance(result, Path)
        assert result.is_absolute()
        assert result == (tmp_output_dir / "players.csv").resolve()

    def test_write_rule7_creates_expected_csv_file(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """The resulting file exists with the canonical ``<name>.csv`` shape."""
        writer = CSVWriter()

        result = writer.write(flat_df, "players", "2025-26")

        assert result.exists()
        assert result.suffix == ".csv"
        assert result.name == "players.csv"

    def test_write_rule7_round_trip_preserves_data(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        csv_reader,
    ) -> None:
        """Round-trip through :meth:`pd.read_csv` preserves headers + rows.

        We compare string representations per-column to tolerate
        harmless int/float dtype drift on re-read (e.g., int64 becoming
        int64 or float64 depending on whitespace), since the AAP only
        requires value-level fidelity, not dtype identity.
        """
        writer = CSVWriter()
        result = writer.write(flat_df, "players", "2025-26")

        loaded = csv_reader(result)

        assert list(loaded.columns) == list(flat_df.columns)
        assert len(loaded) == len(flat_df)
        for col in flat_df.columns:
            assert (
                flat_df[col].astype(str).tolist()
                == loaded[col].astype(str).tolist()
            )

    def test_write_utf8_non_ascii_roundtrip(
        self, tmp_output_dir: Path, csv_reader
    ) -> None:
        """Non-ASCII Unicode characters must round-trip byte-exactly.

        The writer passes ``encoding="utf-8"`` to ``to_csv`` and the
        ``csv_reader`` fixture reads with ``encoding="utf-8"``. This
        test catches any accidental encoding downgrade (e.g., a default
        that falls back to cp1252 on Windows runners).
        """
        df = pd.DataFrame(
            {
                "PLAYER_ID": [77, 15],
                "PLAYER_NAME": ["Luka Dončić", "Nikola Jokić"],
                "TEAM_NAME": ["Mavericks", "Nuggets"],
            }
        )
        writer = CSVWriter()

        result = writer.write(df, "players_unicode", "2025-26")

        loaded = csv_reader(result)
        assert loaded["PLAYER_NAME"].tolist() == ["Luka Dončić", "Nikola Jokić"]

    def test_write_excludes_index_column(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        csv_reader,
    ) -> None:
        """No pandas index column may appear in the output CSV.

        ``index=False`` must be the effective behaviour. We verify two
        ways: (1) re-reading produces no ``Unnamed: N`` columns;
        (2) the raw first line of the file equals
        ``",".join(df.columns)`` — no leading delimiter for an index
        column.
        """
        writer = CSVWriter()

        result = writer.write(flat_df, "players", "2025-26")

        loaded = csv_reader(result)
        assert not any(col.startswith("Unnamed") for col in loaded.columns)

        first_line = result.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == ",".join(flat_df.columns)

    def test_write_empty_dataframe_header_only(
        self,
        tmp_output_dir: Path,
        empty_df: pd.DataFrame,
        csv_reader,
    ) -> None:
        """A zero-row DataFrame produces a header-only CSV."""
        writer = CSVWriter()

        result = writer.write(empty_df, "empty_artifact", "2025-26")

        assert result.exists()
        loaded = csv_reader(result)
        assert loaded.empty
        assert list(loaded.columns) == list(empty_df.columns)

    def test_write_filename_is_independent_of_season(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """The ``season`` argument is logged but does **not** affect the filename."""
        writer = CSVWriter()

        path1 = writer.write(flat_df, "players", "2024-25")
        path2 = writer.write(flat_df, "players", "2025-26")

        assert path1 == path2

    def test_write_different_names_produce_different_files(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """Distinct ``name`` arguments yield distinct filenames."""
        writer = CSVWriter()

        p1 = writer.write(flat_df, "players", "2025-26")
        p2 = writer.write(flat_df, "teams", "2025-26")

        assert p1 != p2
        assert p1.name == "players.csv"
        assert p2.name == "teams.csv"


# ---------------------------------------------------------------------------
# 4. CSVWriter atomic write semantics (F-006)
# ---------------------------------------------------------------------------


class TestCSVWriterAtomicWrite:
    """Atomic ``write-then-rename`` semantics for crash consistency.

    The writer must emit the CSV to a ``<target>.tmp`` staging path and
    then call :meth:`Path.replace` to atomically swap it onto the final
    target. A successful write must leave **no** ``.tmp`` file behind,
    and a second write with the same name must replace the first file's
    contents cleanly.
    """

    def test_write_leaves_no_tmp_file_on_success(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """After a successful write, no ``*.tmp`` file remains."""
        writer = CSVWriter()

        writer.write(flat_df, "players", "2025-26")

        assert list(tmp_output_dir.glob("*.tmp")) == []
        assert list(tmp_output_dir.glob("*.csv.tmp")) == []

    def test_write_output_dir_contains_only_final_csv(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """Exactly one ``.csv`` and zero ``.tmp`` files exist after a write."""
        writer = CSVWriter()

        writer.write(flat_df, "players", "2025-26")

        csv_files = list(tmp_output_dir.glob("*.csv"))
        tmp_files = list(tmp_output_dir.glob("*.tmp"))

        assert len(csv_files) == 1
        assert csv_files[0].name == "players.csv"
        assert len(tmp_files) == 0

    def test_write_overwrites_existing_file_atomically(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """A same-name second write replaces the first file's contents."""
        writer = CSVWriter()

        path1 = writer.write(flat_df, "overwrite_test", "2025-26")
        first_content = path1.read_text(encoding="utf-8")

        second_df = pd.DataFrame({"X": [1, 2, 3], "Y": [4, 5, 6]})
        path2 = writer.write(second_df, "overwrite_test", "2025-26")

        assert path1 == path2
        assert path2.exists()
        second_content = path2.read_text(encoding="utf-8")
        assert second_content != first_content
        assert "X,Y" in second_content
        assert list(tmp_output_dir.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 5. CSVWriter input validation
# ---------------------------------------------------------------------------


class TestCSVWriterInputValidation:
    """Type- and value-error branches of the ``write`` validation cascade.

    Every assertion here uses ``pytest.raises(..., match=...)`` so the
    error-message contract is verified, not just the exception type. A
    message drift from "pandas DataFrame" to (say) "DataFrame object"
    would fail these tests and surface immediately, preserving the
    explainability property required by AAP §0.7.3.3.
    """

    def test_write_non_dataframe_raises_type_error(
        self, tmp_output_dir: Path
    ) -> None:
        """A plain string must be rejected with a ``TypeError``."""
        writer = CSVWriter()

        with pytest.raises(TypeError, match="pandas DataFrame"):
            writer.write("not a dataframe", "name", "2025-26")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad_df",
        ["string", 42, [1, 2, 3], {"col": [1, 2]}, None],
    )
    def test_write_various_non_dataframes_raise_type_error(
        self, tmp_output_dir: Path, bad_df
    ) -> None:
        """Each representative non-DataFrame input produces the same error."""
        writer = CSVWriter()

        with pytest.raises(TypeError, match="pandas DataFrame"):
            writer.write(bad_df, "name", "2025-26")  # type: ignore[arg-type]

    def test_write_empty_string_name_raises_value_error(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """Empty string ``name`` must raise ``ValueError``."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="non-empty string"):
            writer.write(flat_df, "", "2025-26")

    def test_write_none_name_raises_value_error(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """``None`` as ``name`` must raise ``ValueError``."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="non-empty string"):
            writer.write(flat_df, None, "2025-26")  # type: ignore[arg-type]

    def test_write_non_string_name_raises_value_error(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """A non-string ``name`` (integer) must raise ``ValueError``."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="non-empty string"):
            writer.write(flat_df, 123, "2025-26")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad_name",
        ["foo/bar", "foo\\bar", ".", "..", "../evil", "./local", "a/b/c"],
    )
    def test_write_unsafe_name_raises_value_error(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        bad_name: str,
    ) -> None:
        """Any ``name`` containing a path separator or dot-special is rejected."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="unsafe name"):
            writer.write(flat_df, bad_name, "2025-26")


# ---------------------------------------------------------------------------
# 6. CSVWriter Rule 4 defence-in-depth (§0.7.2.4)
# ---------------------------------------------------------------------------


class TestCSVWriterRule4:
    """The writer is the last line of defence against nested-cell leaks.

    Rule 4 (Flat CSV) is enforced upstream by
    :mod:`utils.schema_normalizer`, but the writer carries a belt-and-
    suspenders check so a buggy normaliser or hand-constructed
    DataFrame cannot silently produce a CSV containing ``dict`` or
    ``list`` cells. These tests pin down the rejection behaviour,
    including that the offending column names are surfaced in the
    error message.
    """

    def test_write_rule4_rejects_dict_cells(
        self, tmp_output_dir: Path, nested_df: pd.DataFrame
    ) -> None:
        """A DataFrame with ``dict`` cells must be rejected with ``ValueError``."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="Rule 4"):
            writer.write(nested_df, "nested_output", "2025-26")

    def test_write_rule4_rejects_list_cells(
        self, tmp_output_dir: Path, list_cell_df: pd.DataFrame
    ) -> None:
        """A DataFrame with ``list`` cells must be rejected with ``ValueError``."""
        writer = CSVWriter()

        with pytest.raises(ValueError, match="Rule 4"):
            writer.write(list_cell_df, "list_output", "2025-26")

    def test_write_rule4_error_names_affected_column(
        self, tmp_output_dir: Path, nested_df: pd.DataFrame
    ) -> None:
        """The error message must name the ``"STATS"`` column of ``nested_df``."""
        writer = CSVWriter()

        with pytest.raises(ValueError) as exc_info:
            writer.write(nested_df, "nested_output", "2025-26")

        assert "STATS" in str(exc_info.value)

    def test_write_rule4_error_for_list_names_affected_column(
        self, tmp_output_dir: Path, list_cell_df: pd.DataFrame
    ) -> None:
        """The error message must name the ``"ROSTER"`` column of ``list_cell_df``."""
        writer = CSVWriter()

        with pytest.raises(ValueError) as exc_info:
            writer.write(list_cell_df, "list_output", "2025-26")

        assert "ROSTER" in str(exc_info.value)

    def test_write_rule4_violation_creates_no_file(
        self, tmp_output_dir: Path, nested_df: pd.DataFrame
    ) -> None:
        """A rejected write must leave the filesystem untouched.

        Validation runs **before** any ``to_csv`` call, so no ``.csv``
        or ``.tmp`` file should appear in the output directory.
        """
        writer = CSVWriter()

        with pytest.raises(ValueError, match="Rule 4"):
            writer.write(nested_df, "should_not_exist", "2025-26")

        assert not (tmp_output_dir / "should_not_exist.csv").exists()
        assert list(tmp_output_dir.glob("*.tmp")) == []

    def test_write_flat_df_passes_rule4(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """A scalar-only DataFrame must pass the Rule 4 check and succeed."""
        writer = CSVWriter()

        result = writer.write(flat_df, "flat_passing", "2025-26")

        assert result.exists()

    def test_write_empty_df_bypasses_rule4_check(
        self, tmp_output_dir: Path, empty_df: pd.DataFrame
    ) -> None:
        """An empty DataFrame short-circuits the Rule 4 check and is written."""
        writer = CSVWriter()

        result = writer.write(empty_df, "empty_bypass", "2025-26")

        assert result.exists()


# ---------------------------------------------------------------------------
# 7. CSVWriter path confinement
# ---------------------------------------------------------------------------


class TestCSVWriterPathConfinement:
    """The final target path must always be a child of ``output_dir``.

    Even though the unsafe-name validator catches path-separator
    characters upstream, the writer also performs an explicit
    ``target.relative_to(self._output_dir)`` check as a defensive
    second gate. These tests verify both the positive case (the
    resolved target is confined) and the negative case (traversal
    attempts leave no external side-effects).
    """

    def test_write_result_path_confined_to_output_dir(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """The returned path's relative form is exactly ``<name>.csv``."""
        writer = CSVWriter()

        result = writer.write(flat_df, "players", "2025-26")

        rel = result.relative_to(writer.output_dir)
        assert rel == Path("players.csv")

    def test_write_traversal_attempt_blocked_and_no_external_side_effect(
        self, tmp_output_dir: Path, flat_df: pd.DataFrame
    ) -> None:
        """A ``../`` name is rejected and no file is created outside ``output_dir``.

        The unsafe-name validator catches ``../escape_attempt`` before
        any path construction, so no staging file can leak into the
        parent directory. We scan the parent explicitly to prove the
        filesystem has no ``escape_attempt*`` artefact.
        """
        writer = CSVWriter()

        with pytest.raises(ValueError):
            writer.write(flat_df, "../escape_attempt", "2025-26")

        for entry in tmp_output_dir.parent.iterdir():
            assert not entry.name.startswith("escape_attempt")


# ---------------------------------------------------------------------------
# 8. CSVWriter structured logging (Observability rule)
# ---------------------------------------------------------------------------


class TestCSVWriterLogging:
    """INFO-level start/complete events with season and filename payload.

    The writer emits two structured log events per successful write —
    ``csv_writer.write.start`` and ``csv_writer.write.complete`` — and
    both messages must include the season and target filename so
    operators can correlate the logs with the CSV artefacts they
    produced (AAP §0.7.3.1 Observability rule).

    We use pytest's built-in ``caplog`` fixture without specifying a
    logger name; the ``storage.csv_writer`` logger propagates records
    to the root logger where caplog attaches its capture handler.
    """

    def test_write_emits_start_event_at_info_level(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At least one INFO record carries the ``csv_writer.write.start`` token."""
        writer = CSVWriter()

        with caplog.at_level(logging.INFO):
            writer.write(flat_df, "logged_start", "2025-26")

        start_records = [
            r for r in caplog.records
            if "csv_writer.write.start" in r.getMessage()
        ]
        assert len(start_records) >= 1

    def test_write_emits_complete_event_at_info_level(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At least one INFO record carries the ``csv_writer.write.complete`` token."""
        writer = CSVWriter()

        with caplog.at_level(logging.INFO):
            writer.write(flat_df, "logged_complete", "2025-26")

        complete_records = [
            r for r in caplog.records
            if "csv_writer.write.complete" in r.getMessage()
        ]
        assert len(complete_records) >= 1

    def test_write_log_mentions_season(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The season parameter must appear in at least one captured log message."""
        writer = CSVWriter()

        with caplog.at_level(logging.INFO):
            writer.write(flat_df, "season_logged", "2025-26")

        assert any("2025-26" in r.getMessage() for r in caplog.records)

    def test_write_log_mentions_target_filename(
        self,
        tmp_output_dir: Path,
        flat_df: pd.DataFrame,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The ``name`` argument must appear in at least one captured log message."""
        writer = CSVWriter()

        with caplog.at_level(logging.INFO):
            writer.write(flat_df, "logged_name", "2025-26")

        assert any("logged_name" in r.getMessage() for r in caplog.records)
