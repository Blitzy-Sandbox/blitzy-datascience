"""Unit tests for the ``storage`` production package.

This subpackage mirrors ``storage/`` in the production tree. It contains
unit tests that verify the behaviour of:

* ``storage/csv_writer.py`` -- Feature F-006 (CSV Writer with Pluggable
  Interface). The tests enforce Operational Rule 7 (sole
  ``DataFrame.to_csv`` call site) and the defence-in-depth enforcement
  of Rule 4 (no nested ``dict`` / ``list`` cells in CSV output) that
  the writer performs before each write.

Test modules
------------
* ``test_csv_writer.py`` -- exercises the abstract ``BaseWriter`` base
  class, ``CSVWriter`` construction (default and custom ``output_dir``),
  the atomic write semantics (``.tmp`` suffix plus ``Path.replace``),
  input validation (``TypeError`` on non-DataFrame input; ``ValueError``
  on empty, unsafe, or path-separator-bearing ``name``), Rule 4
  defence-in-depth, path confinement
  (``target.relative_to(output_dir)``), UTF-8 round-trip fidelity, and
  the structured INFO-level log events ``csv_writer.write.start`` and
  ``csv_writer.write.complete``.

Shared fixtures consumed by this subpackage (defined in
``tests/conftest.py``):

* ``tmp_output_dir`` -- writable temp directory with ``config.OUTPUT_DIR``
  monkeypatched to match.
* ``flat_df`` -- scalar-only DataFrame used for happy-path round-trip
  verification.
* ``nested_df``, ``list_cell_df`` -- pathological DataFrames used to
  verify Rule 4 defence-in-depth.
* ``empty_df`` -- zero-row DataFrame used to verify header-only CSV
  output and to confirm ``_assert_flat``'s early return on empty input.
* ``csv_reader`` -- UTF-8-aware reader used in place of direct
  ``pd.read_csv`` calls so reading semantics stay centralised.

The package marker is intentionally empty of behaviour: any module-level
imports or side effects here would execute once per pytest session
during collection and are a common source of subtle ordering bugs.
Shared fixtures and helper classes live in ``tests/conftest.py``;
nothing is re-exported from this package.

References
----------
* AAP §0.5.1.5 -- production contract for ``storage/csv_writer.py``.
* AAP §0.7.2.4 -- Rule 4 (flat CSV output).
* AAP §0.7.2.7 -- Rule 7 (pluggable storage; sole ``to_csv`` call site).
"""
