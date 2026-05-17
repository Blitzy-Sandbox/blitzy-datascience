"""Test package for the NBA Data Ingestion Pipeline.

This package marks ``tests/`` as an importable Python package so that
fully-qualified references such as ``from tests.conftest import
RecordingClient`` resolve deterministically during pytest collection,
IDE test discovery, and the invariant suites that walk the package tree
programmatically.

Layout:

* ``tests/conftest.py`` - shared pytest fixtures consumed by every test
  module (handwritten spy classes ``RecordingClient``,
  ``RecordingWriter``, ``RecordingCheckpoint``; deterministic
  ``FakeClock``; ``tmp_path``-rooted config overrides; canonical
  ``resultSets`` payload fixtures; ``click.testing.CliRunner`` factory).
* ``tests/unit/`` - unit tests mirroring the production package tree
  (``config``, ``run``, ``api``, ``endpoints``, ``pipelines``,
  ``storage``, ``utils``); exercised offline with mocked collaborators.
* ``tests/integration/`` - live-API smoke tests marked
  ``@pytest.mark.integration``; satisfies Validation Gates 1 and 8 of
  the product brief (``docs/New_Product_Prompt_20260418.md`` §6).
* ``tests/invariants/`` - cross-repository grep assertions and DataFrame
  property checks enforcing Operational Rules 1 (Single HTTP Client),
  4 (Flat CSV Output), and 7 (Pluggable Storage).

Running the suite:

    python -m pytest tests/                      # full suite (Gate 10)
    python -m pytest tests/ -m "not integration"  # offline unit + invariants
    python -m pytest tests/ -m integration        # live-API smoke only

The package marker is intentionally empty of behaviour: any module-level
imports or side effects here would execute once per pytest session during
collection and are a common source of subtle ordering bugs. Shared
fixtures and helper classes live in ``tests/conftest.py``; nothing is
re-exported from this package.
"""
