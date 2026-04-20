"""Invariant tests that enforce Rules 1, 4, and 7 across the
production tree.

These tests use ``pathlib.Path`` + ``re`` file scanning and pandas
DataFrame property assertions to verify that the source code adheres
to the operational rules declared in ``docs/New_Product_Prompt_20260418.md``
§5:

* Rule 1 -- only ``api/nba_client.py`` may call ``requests`` directly
  (``test_rule1_sole_http_client.py``).
* Rule 4 -- every DataFrame emitted by
  ``utils.schema_normalizer.normalize_result_sets`` contains only
  scalar cells (``test_rule4_no_nested_cells.py``).
* Rule 7 -- only ``storage/csv_writer.py`` may call
  ``DataFrame.to_csv`` (``test_rule7_basewriter_only.py``).

Invariant tests are not integration tests: they require no live API
access, no filesystem writes beyond reading source files, and run in
the default pytest suite.
"""
