"""Rule 4 — Flat CSV Output (no nested JSON, dicts, or lists).

Asserts that every DataFrame produced by
:func:`utils.schema_normalizer.normalize_result_sets` contains only
scalar cells. The verification is the literal pandas expression from
the product brief:

    df.applymap(lambda x: isinstance(x, (dict, list))).any().any()

which MUST evaluate to ``False``.

This invariant is exercised against the canonical ``sample_*_payload``
fixtures from ``tests/conftest.py``, covering single-table, multi-table,
schedule, play-by-play, empty-rowSet, and the singular ``resultSet``
envelope variants. A negative test confirms that a pathological payload
containing a dict cell triggers a :class:`ValueError` with ``"Rule 4"``
in the message from the normalizer itself.

Rule 4 cannot be verified with static ``grep`` — it is a *runtime
behavioural* invariant over the output of the normalizer. This test is
the canonical DataFrame-level verification that complements the
schema_normalizer unit tests by exercising the full fixture catalog at
the normalization boundary.

Authoritative sources
---------------------

* Product brief ``docs/New_Product_Prompt_20260418.md`` §5 Rule 4:
  "CSV columns must contain scalar values — no nested JSON."
* AAP §0.2.3 — required test file.
* AAP §0.4.4 — integration invariants table (Rule 4 enforced here).
* AAP §0.5.1.8 — Group 8 invariant tests.
* AAP §0.7.2.4 — Rule 4 binding constraint.
* AAP §0.7.5 — Rule-to-Gate verification matrix (Rule 4 → Gate 1).
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
import pytest

from utils.schema_normalizer import normalize_result_sets


# Every test in this module is a project-wide invariant assertion; the
# ``invariant`` marker is registered in ``pytest.ini`` so that the
# ``--strict-markers`` option does not reject unknown marks at
# collection time.
pytestmark = pytest.mark.invariant


# Canonical catalog of *valid* resultSets envelope fixtures defined in
# ``tests/conftest.py``. Each name MUST resolve to an existing fixture
# because the parameterized positive test below resolves them via
# ``request.getfixturevalue``. The six envelopes collectively cover:
#
#   - single-table standard envelopes (``leaguedashplayerstats``)
#   - multi-table envelopes (``boxscoretraditionalv2``)
#   - schedule envelopes with duplicate ``GAME_ID``s
#     (``leaguegamefinder``)
#   - play-by-play envelopes with sparse columns (``playbyplayv2``)
#   - empty ``rowSet`` envelopes (headers but zero rows)
#   - the singular ``resultSet`` API variant (no trailing ``s``)
#
# ``sample_nested_violation_payload`` is deliberately *excluded* from
# this tuple — it is the payload used by the dedicated negative test
# ``test_nested_violation_payload_is_rejected`` and must never be fed
# to the positive test.
VALID_PAYLOAD_FIXTURES = (
    "sample_single_table_payload",
    "sample_multi_table_payload",
    "sample_schedule_payload",
    "sample_playbyplay_payload",
    "sample_empty_payload",
    "sample_result_set_singular_payload",
)


def _map_cells(
    df: pd.DataFrame,
    predicate: Callable[[object], bool],
) -> pd.DataFrame:
    """Apply ``predicate`` to every cell of ``df``.

    Prefers :meth:`pandas.DataFrame.map` on pandas ≥ 2.1 and falls back
    to :meth:`pandas.DataFrame.applymap` on pandas 2.0.x.

    Rationale
    ---------
    The project declares ``pandas>=2.0,<3`` (AAP §0.3.1).
    ``DataFrame.map`` only arrived in pandas 2.1, and ``applymap`` is
    deprecated from 2.1 onward — it emits a ``FutureWarning`` that
    ``pytest.ini``'s ``filterwarnings = error`` policy promotes into a
    test failure for non-pandas callers. This shim keeps the invariant
    test clean on both ends of the supported range without tripping
    Gate 2.

    The pattern intentionally mirrors the one used inside
    :func:`utils.schema_normalizer._assert_rule4_flat` for consistency
    — both the production Rule 4 assertion and the test-side Rule 4
    assertion select the same underlying pandas method.
    """
    mapper = getattr(df, "map", None)
    if callable(mapper):
        return mapper(predicate)
    return df.applymap(predicate)


def _assert_flat(df: pd.DataFrame, table_name: str) -> None:
    """Assert that ``df`` contains no ``dict`` or ``list`` cells.

    The verification recipe is taken verbatim from product brief §5
    Rule 4 with the ``applymap`` call swapped for the
    forward-compatible ``map`` when available.

    Parameters
    ----------
    df
        The DataFrame to validate.
    table_name
        Human-readable identifier of the result-set table — included
        in the failure message to make Rule 4 violations easy to
        localise.
    """
    # An empty DataFrame has no cells to inspect; Rule 4 is trivially
    # satisfied. ``pd.DataFrame().any().any()`` returns ``False`` on
    # empty frames, but short-circuiting here makes the intent
    # explicit and documents the edge case that
    # ``sample_empty_payload`` exercises.
    if df.empty:
        return

    nested_mask = _map_cells(df, lambda x: isinstance(x, (dict, list)))

    # ``nested_mask.any().any()`` collapses the boolean DataFrame first
    # along the column axis (returning a Series of per-column anys)
    # and then along the row axis (returning a single bool). This is
    # the exact expression prescribed by product brief §5 Rule 4.
    assert not nested_mask.any().any(), (
        f"Rule 4 violation in table '{table_name}': "
        f"{int(nested_mask.sum().sum())} cells contain dict or list values"
    )


@pytest.mark.parametrize("payload_fixture", VALID_PAYLOAD_FIXTURES)
def test_normalized_result_sets_have_flat_cells(
    request: pytest.FixtureRequest,
    payload_fixture: str,
) -> None:
    """Every DataFrame returned by ``normalize_result_sets`` must be flat.

    For each valid payload fixture in :data:`VALID_PAYLOAD_FIXTURES`,
    invoke the normalizer and verify:

    1. The return value is a ``dict`` mapping table-name →
       :class:`pandas.DataFrame`.
    2. The mapping is non-empty — at least one table must be produced
       even for ``sample_empty_payload``, whose lone table has an
       empty ``rowSet`` but still yields a DataFrame with declared
       columns.
    3. Every produced object is an instance of
       :class:`pandas.DataFrame`.
    4. Every cell of every produced DataFrame is scalar — no ``dict``,
       no ``list`` (Rule 4).

    Negative cases (rejected payloads) are covered by
    :func:`test_nested_violation_payload_is_rejected`; this test must
    never trigger a :class:`ValueError` from the normalizer — if it
    does, the fixture has regressed and should be treated as a bug in
    the fixture itself, not a Rule 4 violation.
    """
    # ``pytest.mark.parametrize`` cannot directly inject fixtures as
    # parameter values; resolve the fixture by name via the built-in
    # ``request`` fixture. This is the standard pytest idiom for
    # parametrizing over fixture names.
    payload = request.getfixturevalue(payload_fixture)

    dfs = normalize_result_sets(payload)

    assert isinstance(dfs, dict), (
        "`normalize_result_sets` must return a Dict[str, DataFrame] for "
        f"{payload_fixture}, got {type(dfs).__name__}"
    )
    assert dfs, (
        "`normalize_result_sets` returned an empty mapping for "
        f"{payload_fixture}; expected at least one table"
    )

    for name, df in dfs.items():
        assert isinstance(df, pd.DataFrame), (
            f"Table '{name}' from {payload_fixture} is not a DataFrame "
            f"(got {type(df).__name__})"
        )
        _assert_flat(df, name)


def test_nested_violation_payload_is_rejected(
    sample_nested_violation_payload: dict,
) -> None:
    """A payload with a nested dict cell MUST be rejected.

    The contract of :func:`utils.schema_normalizer.normalize_result_sets`
    (enforced in ``utils.schema_normalizer._assert_rule4_flat``) is to
    raise :class:`ValueError` whose message includes ``"Rule 4"`` when
    any cell of any produced DataFrame contains a ``dict`` or
    ``list``.

    This test verifies the *active-rejection* path — the normalizer
    must not silently return a DataFrame that violates the invariant.
    A regex anchor of ``r"Rule 4"`` confirms the error message
    explicitly attributes the failure to Rule 4, making debugging
    trivial for downstream developers.
    """
    with pytest.raises(ValueError, match=r"Rule 4"):
        normalize_result_sets(sample_nested_violation_payload)
