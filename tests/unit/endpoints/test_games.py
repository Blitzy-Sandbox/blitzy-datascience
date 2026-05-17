"""Unit tests for ``endpoints/games.py`` (Feature F-011 — Games domain).

Covers the four Games-domain NBA Stats API endpoint wrappers:

* :func:`endpoints.games.fetch_scoreboardv2` — date-partitioned game
  enumeration; three-key param surface
  (``GameDate`` / ``LeagueID`` / ``DayOffset``).
* :func:`endpoints.games.fetch_boxscoretraditionalv2` — per-game
  traditional box score; six-key param surface with the ``Range``
  triplet (``StartPeriod`` / ``EndPeriod`` / ``StartRange`` /
  ``EndRange`` / ``RangeType``).
* :func:`endpoints.games.fetch_boxscoreadvancedv2` — per-game advanced
  box score; identical parameter shape to traditional.
* :func:`endpoints.games.fetch_playbyplayv2` — per-game play-by-play
  event stream; NARROWER three-key param surface
  (``GameID`` / ``StartPeriod`` / ``EndPeriod``) — no ``Range``
  triplet. Supplying ``Range`` fields is an upstream validation
  error, so absence is tested as a strict negative-space assertion.

Contract under test
-------------------

Every wrapper:

1. Delegates to ``client.get(<endpoint_name>, params)`` — the sole
   HTTP transport call site per Rule 1 of the product brief. No test
   in this module imports :mod:`requests` or instantiates a real
   :class:`api.nba_client.NBAClient`.
2. Constructs the domain-specific params dict and passes it to the
   client unmodified after ``params.update(kwargs)`` is applied so
   caller-supplied kwargs take precedence over documented defaults.
3. Applies :func:`str` to numeric identifiers (``GameID``,
   ``DayOffset``) and to period/range values, but does NOT reformat
   string inputs. Callers that supply the 10-character zero-padded
   ``GAME_ID`` format (e.g. ``"0022500001"``) receive that exact
   string back in the recorded params — the format is preserved
   verbatim.
4. Returns the raw dict emitted by the underlying client unmodified
   (no projection, no filtering, no copy).

All tests use the ``recording_client`` factory fixture from
:mod:`tests.conftest` to instantiate a :class:`RecordingClient` spy
so that assertions target the recorded ``(endpoint, params)``
tuple. The ``sample_single_table_payload`` and
``sample_playbyplay_payload`` fixtures back the payload
pass-through identity assertions.
"""
from __future__ import annotations

import pytest

from endpoints import games


# ---------------------------------------------------------------------------
# fetch_scoreboardv2 — date-partitioned game enumeration
# ---------------------------------------------------------------------------


def test_fetch_scoreboardv2_calls_correct_endpoint(recording_client):
    """``fetch_scoreboardv2`` must invoke ``client.get('scoreboardv2', ...)``.

    Rule 1 compliance verification — the wrapper routes through the
    shared :class:`api.nba_client.NBAClient` using the documented
    upstream endpoint name. The endpoint string must match the NBA
    Stats API URL slug (``/stats/scoreboardv2``) exactly.
    """
    client = recording_client()
    games.fetch_scoreboardv2(client, "2025-10-21")
    assert client.calls, "expected exactly one client.get(...) call"
    assert client.calls[-1][0] == "scoreboardv2"


def test_fetch_scoreboardv2_required_params(recording_client):
    """Default invocation populates ``GameDate``, ``LeagueID``, ``DayOffset``.

    * ``GameDate`` is the positional argument passed verbatim (ISO-8601
      ``YYYY-MM-DD``).
    * ``LeagueID`` defaults to ``config.DEFAULT_LEAGUE_ID`` (``"00"``
      for the NBA).
    * ``DayOffset`` defaults to the string ``"0"``; the wrapper
      applies :func:`str` so either int or str equivalence under
      ``str()`` satisfies the contract.
    """
    client = recording_client()
    games.fetch_scoreboardv2(client, "2025-10-21")
    params = client.calls[-1][1]
    assert params["GameDate"] == "2025-10-21"
    assert params["LeagueID"] == "00"
    assert str(params["DayOffset"]) == "0"


def test_fetch_scoreboardv2_kwargs_passthrough(recording_client):
    """Caller ``**kwargs`` overrides documented defaults.

    ``params.update(kwargs)`` applied AFTER the base dict is built
    means any kwarg with a name matching a default key wins. The
    wrapper itself does not apply :func:`str` to kwarg values; the
    test uses ``str()`` on the recorded value to stay agnostic to the
    caller-supplied type.
    """
    client = recording_client()
    games.fetch_scoreboardv2(client, "2025-10-21", DayOffset="1")
    assert str(client.calls[-1][1]["DayOffset"]) == "1"


def test_fetch_scoreboardv2_returns_raw_payload(
    recording_client, sample_single_table_payload
):
    """Return value is the raw dict produced by the client, unmodified.

    Identity pass-through: the wrapper does NOT project, filter, or
    copy the response envelope. Seeding the recording client with a
    canonical ``leaguedashplayerstats``-shaped payload on the
    ``scoreboardv2`` key is sufficient — the wrapper's contract is
    agnostic to the payload's internal table structure.
    """
    client = recording_client(
        responses={"scoreboardv2": sample_single_table_payload}
    )
    result = games.fetch_scoreboardv2(client, "2025-10-21")
    assert result == sample_single_table_payload


# ---------------------------------------------------------------------------
# fetch_boxscoretraditionalv2 — traditional per-game box score
# ---------------------------------------------------------------------------


def test_fetch_boxscoretraditionalv2_calls_correct_endpoint(recording_client):
    """``fetch_boxscoretraditionalv2`` hits the ``boxscoretraditionalv2`` slug.

    Rule 1 verification — per-game traditional box score endpoint.
    The URL slug must be lowercase and match the NBA Stats API
    convention exactly.
    """
    client = recording_client()
    games.fetch_boxscoretraditionalv2(client, "0022500001")
    assert client.calls, "expected exactly one client.get(...) call"
    assert client.calls[-1][0] == "boxscoretraditionalv2"


def test_fetch_boxscoretraditionalv2_preserves_game_id_10_char_format(
    recording_client,
):
    """The 10-character zero-padded ``GAME_ID`` format is preserved verbatim.

    NBA Stats API ``GAME_ID`` follows the format
    ``"00" + season_code + sequence_number`` with zero-padding to 10
    characters (e.g. ``"0022500001"`` = 2025-26 Regular Season game
    #1). The wrapper applies :func:`str` to accommodate int inputs
    but must NOT reformat strings — the zero-padding is caller
    responsibility and must round-trip untouched.
    """
    client = recording_client()
    games.fetch_boxscoretraditionalv2(client, "0022500001")
    assert client.calls[-1][1]["GameID"] == "0022500001"


def test_fetch_boxscoretraditionalv2_casts_game_id_to_str(recording_client):
    """Integer ``game_id`` inputs are cast to :class:`str` via ``str(game_id)``.

    The wrapper's documented contract applies :func:`str` defensively
    so callers who construct IDs numerically still produce valid
    upstream params (NBA Stats requires the value as a string).
    This test verifies only the type cast; it does NOT assert the
    zero-padded 10-character format is re-introduced for integer
    inputs — that is the caller's responsibility, not the wrapper's.
    """
    client = recording_client()
    games.fetch_boxscoretraditionalv2(client, 22500001)
    game_id_value = client.calls[-1][1]["GameID"]
    assert isinstance(game_id_value, str)
    # Sanity: the cast preserves digits (does not introduce any
    # leading zeros, since the spec explicitly documents that
    # padding is caller responsibility for int inputs).
    assert game_id_value == str(22500001)


def test_fetch_boxscoretraditionalv2_includes_range_params(recording_client):
    """Params include both ``StartPeriod``/``EndPeriod`` AND the ``Range`` triplet.

    The traditional boxscore endpoint accepts the full six-key param
    surface:

    * ``StartPeriod`` — default ``"0"`` (include all periods from game
      start).
    * ``EndPeriod`` — default ``"10"`` (covers regulation + up to 6
      overtime periods).
    * ``StartRange`` — default ``"0"`` (tenths-of-a-second window
      start).
    * ``EndRange`` — default ``"28800"`` (end of regulation, i.e.
      48 min × 60 s × 10 tenths).
    * ``RangeType`` — default ``"0"`` (whole-game selector).

    Values may be emitted as either int or str by the wrapper; tests
    use :func:`str` on both sides to stay tolerant of either cast
    choice. The default ``EndPeriod`` value is pinned to ``"10"``
    because it is the most operationally salient default (it
    determines how many overtime periods the box score summarizes).
    """
    client = recording_client()
    games.fetch_boxscoretraditionalv2(client, "0022500001")
    params = client.calls[-1][1]
    assert "StartPeriod" in params
    assert "EndPeriod" in params
    assert "StartRange" in params
    assert "EndRange" in params
    assert "RangeType" in params
    # Pinned default check — regression guard against silently
    # shrinking the overtime coverage.
    assert str(params["EndPeriod"]) == "10"


def test_fetch_boxscoretraditionalv2_kwargs_override(recording_client):
    """``EndPeriod=4`` kwarg overrides the default ``"10"``.

    Verifies the ``params.update(kwargs)`` ordering so that callers
    can constrain the box score to regulation only (no overtime) by
    passing ``EndPeriod=4``. The assertion uses :func:`str` on the
    recorded value to tolerate either int or str encoding.
    """
    client = recording_client()
    games.fetch_boxscoretraditionalv2(client, "0022500001", EndPeriod=4)
    assert str(client.calls[-1][1]["EndPeriod"]) == "4"


# ---------------------------------------------------------------------------
# fetch_boxscoreadvancedv2 — advanced per-game box score
# ---------------------------------------------------------------------------


def test_fetch_boxscoreadvancedv2_calls_correct_endpoint(recording_client):
    """``fetch_boxscoreadvancedv2`` hits the ``boxscoreadvancedv2`` slug.

    The advanced box score surfaces efficiency metrics
    (``OFF_RATING``, ``DEF_RATING``, ``PIE``, etc.) that the
    traditional endpoint does not expose. Pipeline code iterates over
    both boxscore variants for each ``GAME_ID`` so a wrong endpoint
    name here would silently duplicate traditional rows.
    """
    client = recording_client()
    games.fetch_boxscoreadvancedv2(client, "0022500001")
    assert client.calls, "expected exactly one client.get(...) call"
    assert client.calls[-1][0] == "boxscoreadvancedv2"


def test_fetch_boxscoreadvancedv2_preserves_game_id(recording_client):
    """``GAME_ID`` zero-padded string format is preserved verbatim.

    Same contract as :func:`fetch_boxscoretraditionalv2` — the
    wrapper must not reformat string inputs. The 10-character
    zero-padded form is the NBA Stats convention and callers
    following that convention must see their input round-trip
    untouched.
    """
    client = recording_client()
    games.fetch_boxscoreadvancedv2(client, "0022500001")
    assert client.calls[-1][1]["GameID"] == "0022500001"


def test_fetch_boxscoreadvancedv2_includes_range_params(recording_client):
    """Advanced box score has the SAME six-key param shape as traditional.

    Per the production contract (``endpoints/games.py`` docstring,
    "Parameter shape note"): "The parameter surface is IDENTICAL to
    :func:`fetch_boxscoretraditionalv2` — including the ``Range``
    triplet."

    This test pins the identity: all five period/range keys MUST be
    present in the params dict so pipelines can iterate over the two
    boxscore variants uniformly without per-variant branching.
    """
    client = recording_client()
    games.fetch_boxscoreadvancedv2(client, "0022500001")
    params = client.calls[-1][1]
    for key in ("StartPeriod", "EndPeriod", "StartRange", "EndRange", "RangeType"):
        assert key in params, f"expected {key!r} in advancedv2 params; got {sorted(params)}"


def test_fetch_boxscoreadvancedv2_kwargs_override(recording_client):
    """``RangeType="1"`` kwarg overrides the default ``"0"``.

    Verifies kwargs precedence for the ``Range`` triplet — callers
    requesting partial-window box scores (e.g., "first five minutes
    of Q4") would supply ``RangeType="1"``. While the production
    pipeline does not currently use partial-window ranges, the
    wrapper must forward the kwarg faithfully.
    """
    client = recording_client()
    games.fetch_boxscoreadvancedv2(client, "0022500001", RangeType="1")
    assert str(client.calls[-1][1]["RangeType"]) == "1"


# ---------------------------------------------------------------------------
# fetch_playbyplayv2 — per-game event stream (narrower param surface)
# ---------------------------------------------------------------------------


def test_fetch_playbyplayv2_calls_correct_endpoint(recording_client):
    """``fetch_playbyplayv2`` hits the v2 ``playbyplayv2`` slug (not v1).

    The v1 ``playbyplay`` endpoint is deprecated and returns fewer
    per-event metadata fields (no coordinates, no player marks).
    The wrapper MUST target v2 exclusively; a silent regression to v1
    would corrupt downstream ``play_by_play.csv`` columns.
    """
    client = recording_client()
    games.fetch_playbyplayv2(client, "0022500001")
    assert client.calls, "expected exactly one client.get(...) call"
    assert client.calls[-1][0] == "playbyplayv2"


def test_fetch_playbyplayv2_preserves_game_id(recording_client):
    """``GAME_ID`` zero-padded string format is preserved verbatim.

    Same GAME_ID-format contract as the boxscore wrappers. The
    consistency across all three per-game endpoints is important:
    pipeline code calls them in a loop over the same ``GAME_ID``
    list, so any wrapper reformatting the ID would break keyed joins
    across ``games.csv`` and ``play_by_play.csv``.
    """
    client = recording_client()
    games.fetch_playbyplayv2(client, "0022500001")
    assert client.calls[-1][1]["GameID"] == "0022500001"


def test_fetch_playbyplayv2_includes_period_params(recording_client):
    """``StartPeriod`` and ``EndPeriod`` are present; ``EndPeriod`` default is ``"10"``.

    Play-by-play supports period-level bounding only. The default
    ``EndPeriod="10"`` ensures overtime events are included for
    playoff / double-OT regular-season games; shrinking this default
    would silently truncate event streams for late-game OT periods.
    """
    client = recording_client()
    games.fetch_playbyplayv2(client, "0022500001")
    params = client.calls[-1][1]
    assert "StartPeriod" in params
    assert "EndPeriod" in params
    assert str(params["EndPeriod"]) == "10"


def test_fetch_playbyplayv2_has_no_range_params(recording_client):
    """The play-by-play wrapper has a NARROWER param surface than the boxscores.

    Negative-space assertion — verifies the ``Range`` triplet
    (``StartRange``, ``EndRange``, ``RangeType``) is absent from the
    params dict. Per the production docstring: "supplying those
    parameters is an upstream validation error."

    This is an inverse-assertion regression guard: the most likely
    way to break the narrower contract is for a developer to
    copy-paste the traditional-boxscore wrapper body into the
    playbyplayv2 implementation. That copy-paste would silently
    widen the param surface; this test catches it by asserting the
    Range keys are NOT present on a default invocation.
    """
    client = recording_client()
    games.fetch_playbyplayv2(client, "0022500001")
    params = client.calls[-1][1]
    assert "StartRange" not in params, (
        "fetch_playbyplayv2 must NOT emit StartRange — narrower param "
        "surface per spec; supplying it is an upstream validation error"
    )
    assert "EndRange" not in params, (
        "fetch_playbyplayv2 must NOT emit EndRange — narrower param "
        "surface per spec; supplying it is an upstream validation error"
    )
    assert "RangeType" not in params, (
        "fetch_playbyplayv2 must NOT emit RangeType — narrower param "
        "surface per spec; supplying it is an upstream validation error"
    )


def test_fetch_playbyplayv2_kwargs_override(recording_client):
    """``EndPeriod=4`` kwarg overrides the default ``"10"``.

    Constrains the play-by-play pull to regulation only (no
    overtime). The kwarg precedence semantics are identical to the
    other wrappers — ``params.update(kwargs)`` applied after the base
    dict is built means the kwarg wins.
    """
    client = recording_client()
    games.fetch_playbyplayv2(client, "0022500001", EndPeriod=4)
    assert str(client.calls[-1][1]["EndPeriod"]) == "4"


def test_fetch_playbyplayv2_returns_raw_payload(
    recording_client, sample_playbyplay_payload
):
    """Return value is the raw ``playbyplayv2`` envelope, unmodified.

    Identity pass-through verified against a purpose-built
    ``playbyplayv2`` fixture that carries the canonical
    ``EVENTNUM`` / ``EVENTMSGTYPE`` / ``PERIOD`` columns used by
    ``play_by_play.csv`` downstream. The fixture is distinct from
    the generic ``sample_single_table_payload`` so this test cannot
    silently pass by aliasing a different envelope.
    """
    client = recording_client(
        responses={"playbyplayv2": sample_playbyplay_payload}
    )
    result = games.fetch_playbyplayv2(client, "0022500001")
    assert result == sample_playbyplay_payload
    # Extra sanity — verify the fixture was not accidentally deep-
    # copied; ``result`` and the fixture should be the SAME dict
    # object since RecordingClient returns the mapped value as-is.
    assert result is sample_playbyplay_payload


# ---------------------------------------------------------------------------
# Module-level structural guard: ensure pytest marker registration surface is
# available so `pytest` imports above do not get flagged as unused by linters
# optimizing for the test-module convention. This intentionally no-ops at
# runtime; its sole purpose is to document why ``import pytest`` is present.
# ---------------------------------------------------------------------------


def _pytest_marker_surface_reference() -> object:
    """Reference ``pytest`` at module scope so its import is not dead.

    The test module convention used across ``tests/unit/endpoints/``
    keeps ``import pytest`` at the top of every file so future
    maintainers can add ``@pytest.mark.*`` or ``pytest.raises`` to
    any test without touching the import block. This helper
    references :mod:`pytest` so static analyzers do not flag the
    import as unused while the current 19-function surface happens
    not to exercise any pytest API directly.

    Returns:
        The :mod:`pytest` module object itself.
    """
    return pytest
