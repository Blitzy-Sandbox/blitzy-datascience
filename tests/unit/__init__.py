"""Unit tests mirroring the production package tree.

This package contains deterministic, network-free tests that exercise
each production module in isolation. Shared fixtures are provided by
``tests/conftest.py``.

Tests in this package:

- Are strictly unit-level -- no live API calls, no real filesystem state
  outside ``tmp_path``, no real ``time.sleep`` except in the one
  intentional ``RateLimiter`` thread-safety test.
- Leave global state untouched via autouse ``conftest`` fixtures that
  reset the correlation-id ``ContextVar``, the metrics registry, and the
  root logger handlers between every test.
- Do NOT use the ``integration`` or ``invariant`` pytest markers declared
  in ``pytest.ini`` -- those markers are reserved for
  ``tests/integration/`` and ``tests/invariants/`` respectively.
- Mirror the production package layout one-to-one so every production
  module has an adjacent test module: ``test_config.py`` and
  ``test_cli.py`` at the top of this package, with ``api/``,
  ``endpoints/``, ``pipelines/``, ``storage/``, and ``utils/``
  sub-packages mirroring their production counterparts.

The package marker is intentionally empty of behaviour: any module-level
imports or side effects here would execute once per pytest session
during collection and are a common source of subtle ordering bugs.
Shared fixtures and helper classes live in ``tests/conftest.py``;
nothing is re-exported from this package.
"""
