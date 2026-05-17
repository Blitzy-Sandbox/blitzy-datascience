"""Unit tests for :mod:`endpoints.players` (F-009 Players domain).

This module verifies the FIVE public Players-domain endpoint wrappers defined in
``endpoints/players.py``:

* :func:`endpoints.players.fetch_leaguedashplayerstats`
* :func:`endpoints.players.fetch_leaguedashplayerclutch`
* :func:`endpoints.players.fetch_playercareerstats`   (**NO ``season`` parameter**)
* :func:`endpoints.players.fetch_playergamelog`
* :func:`endpoints.players.fetch_leaguedashptstats`

Contract aspects covered by the 20 test functions in this module
----------------------------------------------------------------
1. **Correct upstream endpoint name** — each wrapper delegates to
   ``client.get(...)`` with the exact NBA Stats endpoint string
   (``"leaguedashplayerstats"``, ``"leaguedashplayerclutch"``,
   ``"playercareerstats"``, ``"playergamelog"``, ``"leaguedashptstats"``).
2. **Required param surface** — ``Season``, ``SeasonType``, ``LeagueID`` for the
   league-wide endpoints; ``PlayerID`` for the per-player endpoints.
3. **Documented defaults** — ``PerMode="PerGame"``, ``MeasureType="Base"``,
   ``PlayerOrTeam="Player"``, ``PointDiff="5"``, ``ClutchTime="Last 5 Minutes"``,
   ``AheadBehind="Ahead or Behind"`` propagate into the request params when the
   caller does not override them.
4. **Signature exception (playercareerstats)** — the endpoint returns a player's
   ENTIRE career, so the wrapper MUST NOT emit a ``Season`` key. A dedicated
   regression guard enforces this.
5. **Defensive type coercion** — integer ``player_id`` inputs are cast to
   :class:`str` via ``str(player_id)`` inside the wrapper.
6. **``**kwargs`` override precedence** — title-case kwargs (e.g.
   ``MeasureType="Advanced"``) merged via ``params.update(kwargs)`` override the
   wrapper-owned defaults.
7. **Pure pass-through** — wrappers return the raw dict from ``client.get``
   UNMODIFIED (no flattening, no filtering, no transformation).

Rule 1 isolation
----------------
Every test exercises the wrapper with the :class:`RecordingClient` spy produced
by the ``recording_client`` fixture factory in ``tests/conftest.py``. No real
:class:`~api.nba_client.NBAClient` is constructed, no real ``requests`` call is
issued, and no retry/rate-limit plumbing is exercised. The tests are therefore
fully deterministic and network-isolated (Rule 1 — Single HTTP Client).
"""

from __future__ import annotations

import pytest  # noqa: F401 — imported for parity with sibling test modules and
# to enable future addition of pytest marks, pytest.raises, or pytest.parametrize
# without requiring a separate import change.

from endpoints import players


# ---------------------------------------------------------------------------
# fetch_leaguedashplayerstats
# ---------------------------------------------------------------------------


def test_fetch_leaguedashplayerstats_calls_correct_endpoint(recording_client):
    """``fetch_leaguedashplayerstats`` MUST delegate to ``client.get`` with the
    upstream endpoint name ``"leaguedashplayerstats"``.
    """
    client = recording_client()

    players.fetch_leaguedashplayerstats(client, "2025-26", "Regular Season", "00")

    assert client.calls, "fetch_leaguedashplayerstats did not invoke client.get"
    assert client.calls[-1][0] == "leaguedashplayerstats"


def test_fetch_leaguedashplayerstats_required_params(recording_client):
    """The canonical required-param surface for ``leaguedashplayerstats``.

    Asserts on KEY params only (Season, SeasonType, LeagueID, PerMode,
    MeasureType) — does NOT assert on the full ~35-field filter scaffold to
    keep the test decoupled from the wrapper's internal default list.
    """
    client = recording_client()

    players.fetch_leaguedashplayerstats(client, "2025-26", "Regular Season", "00")

    params = client.calls[-1][1]
    assert params["Season"] == "2025-26"
    assert params["SeasonType"] == "Regular Season"
    assert params["LeagueID"] == "00"
    assert params["PerMode"] == "PerGame"
    assert params["MeasureType"] == "Base"


def test_fetch_leaguedashplayerstats_kwargs_override_defaults(recording_client):
    """Title-case kwargs (e.g. ``MeasureType="Advanced"``) MUST override the
    wrapper-owned defaults via ``params.update(kwargs)`` precedence.
    """
    client = recording_client()

    players.fetch_leaguedashplayerstats(
        client, "2025-26", "Regular Season", "00", MeasureType="Advanced"
    )

    assert client.calls[-1][1]["MeasureType"] == "Advanced"


def test_fetch_leaguedashplayerstats_returns_raw_dict(
    recording_client, sample_single_table_payload
):
    """The wrapper MUST return the raw dict from ``client.get`` UNMODIFIED.

    Injects a canonical single-table payload via ``responses=``, invokes the
    wrapper, and asserts the returned value equals the injected payload. This
    is the pass-through / identity contract — no transformation, filtering, or
    flattening occurs inside the wrapper.
    """
    client = recording_client(
        responses={"leaguedashplayerstats": sample_single_table_payload}
    )

    result = players.fetch_leaguedashplayerstats(
        client, "2025-26", "Regular Season", "00"
    )

    assert result == sample_single_table_payload


# ---------------------------------------------------------------------------
# fetch_leaguedashplayerclutch
# ---------------------------------------------------------------------------


def test_fetch_leaguedashplayerclutch_calls_correct_endpoint(recording_client):
    """``fetch_leaguedashplayerclutch`` MUST delegate to ``client.get`` with
    the upstream endpoint name ``"leaguedashplayerclutch"``.
    """
    client = recording_client()

    players.fetch_leaguedashplayerclutch(client, "2025-26", "Regular Season", "00")

    assert client.calls, "fetch_leaguedashplayerclutch did not invoke client.get"
    assert client.calls[-1][0] == "leaguedashplayerclutch"


def test_fetch_leaguedashplayerclutch_includes_clutch_params(recording_client):
    """The clutch variant MUST emit the three clutch-specific params —
    ``ClutchTime``, ``AheadBehind``, and ``PointDiff`` — as presence-verifiable
    keys in the request params dict.
    """
    client = recording_client()

    players.fetch_leaguedashplayerclutch(client, "2025-26", "Regular Season", "00")

    params = client.calls[-1][1]
    assert "ClutchTime" in params
    assert params["ClutchTime"], "ClutchTime must be non-empty"
    assert "AheadBehind" in params
    assert "PointDiff" in params


def test_fetch_leaguedashplayerclutch_default_point_diff(recording_client):
    """Default ``PointDiff`` resolves to ``"5"`` when the caller does not
    override it.

    ``str(params["PointDiff"]) == "5"`` accepts either int or str encoding so
    the test is not coupled to the wrapper's internal cast choice. The
    production code casts to :class:`str` via ``point_diff=str(point_diff)``,
    but this assertion also passes if a future refactor stores the value as an
    :class:`int` before the upstream serialization step.
    """
    client = recording_client()

    players.fetch_leaguedashplayerclutch(client, "2025-26", "Regular Season", "00")

    assert str(client.calls[-1][1]["PointDiff"]) == "5"


def test_fetch_leaguedashplayerclutch_kwargs_passthrough(recording_client):
    """Title-case kwargs MUST override the wrapper-owned clutch defaults.

    Passes ``ClutchTime="Last 3 Minutes"`` as a kwarg (matching the NBA Stats
    API's title-case param convention) and asserts the value propagates
    verbatim into the request params, overriding the wrapper default
    ``"Last 5 Minutes"``.
    """
    client = recording_client()

    players.fetch_leaguedashplayerclutch(
        client, "2025-26", "Regular Season", "00", ClutchTime="Last 3 Minutes"
    )

    assert client.calls[-1][1]["ClutchTime"] == "Last 3 Minutes"


# ---------------------------------------------------------------------------
# fetch_playercareerstats — the SIGNATURE EXCEPTION (no ``season`` parameter)
# ---------------------------------------------------------------------------


def test_fetch_playercareerstats_calls_correct_endpoint(recording_client):
    """``fetch_playercareerstats`` MUST delegate to ``client.get`` with the
    upstream endpoint name ``"playercareerstats"``.
    """
    client = recording_client()

    players.fetch_playercareerstats(client, "2544")

    assert client.calls, "fetch_playercareerstats did not invoke client.get"
    assert client.calls[-1][0] == "playercareerstats"


def test_fetch_playercareerstats_includes_player_id_as_string(recording_client):
    """Integer ``player_id`` inputs MUST be coerced to :class:`str`.

    The NBA Stats API requires ``PlayerID`` as a JSON-serializable string
    ("2544"), so the wrapper calls ``str(player_id)`` defensively. Passing
    the integer ``2544`` (not the string ``"2544"``) and asserting the
    resulting ``params["PlayerID"] == "2544"`` verifies the cast.
    """
    client = recording_client()

    players.fetch_playercareerstats(client, 2544)  # int input — must be str-cast

    assert client.calls[-1][1]["PlayerID"] == "2544"


def test_fetch_playercareerstats_no_season_param(recording_client):
    """``playercareerstats`` returns the ENTIRE career — no ``Season`` param.

    The NBA Stats ``playercareerstats`` endpoint returns an aggregated career
    history, so scoping by season would defeat its purpose. This test is the
    dedicated regression guard against a future refactor that mistakenly adds
    a ``Season`` key to the params dict.
    """
    client = recording_client()

    players.fetch_playercareerstats(client, "2544")

    assert "Season" not in client.calls[-1][1]


def test_fetch_playercareerstats_kwargs_passthrough(recording_client):
    """Title-case kwargs MUST override the wrapper-owned ``PerMode`` default.

    Passing ``PerMode="Totals"`` overrides the wrapper default ``"PerGame"``
    via ``params.update(kwargs)`` precedence.
    """
    client = recording_client()

    players.fetch_playercareerstats(client, "2544", PerMode="Totals")

    assert client.calls[-1][1]["PerMode"] == "Totals"


# ---------------------------------------------------------------------------
# fetch_playergamelog
# ---------------------------------------------------------------------------


def test_fetch_playergamelog_calls_correct_endpoint(recording_client):
    """``fetch_playergamelog`` MUST delegate to ``client.get`` with the
    upstream endpoint name ``"playergamelog"``.
    """
    client = recording_client()

    players.fetch_playergamelog(client, "2544", "2025-26", "Regular Season", "00")

    assert client.calls, "fetch_playergamelog did not invoke client.get"
    assert client.calls[-1][0] == "playergamelog"


def test_fetch_playergamelog_includes_player_id_and_season(recording_client):
    """``fetch_playergamelog`` MUST emit ``PlayerID`` (as :class:`str`),
    ``Season``, ``SeasonType``, and ``LeagueID`` in the request params.

    Passes the integer ``2544`` as ``player_id`` to verify the
    ``str(player_id)`` defensive cast, and asserts the remaining three
    required params propagate from the positional call arguments.
    """
    client = recording_client()

    players.fetch_playergamelog(client, 2544, "2025-26", "Regular Season", "00")

    params = client.calls[-1][1]
    assert params["PlayerID"] == "2544"  # integer cast to string
    assert params["Season"] == "2025-26"
    assert params["SeasonType"] == "Regular Season"
    assert params["LeagueID"] == "00"


def test_fetch_playergamelog_kwargs_passthrough(recording_client):
    """Title-case kwargs MUST override the wrapper-owned date-filter defaults.

    Passes ``DateFrom="10/01/2025"`` as a kwarg; the wrapper default is the
    empty string ``""``. ``params.update(kwargs)`` merges the kwarg with
    override precedence so the resulting ``params["DateFrom"]`` is the caller-
    supplied value.
    """
    client = recording_client()

    players.fetch_playergamelog(
        client, "2544", "2025-26", "Regular Season", "00", DateFrom="10/01/2025"
    )

    assert client.calls[-1][1]["DateFrom"] == "10/01/2025"


# ---------------------------------------------------------------------------
# fetch_leaguedashptstats — player-tracking stats
# ---------------------------------------------------------------------------


def test_fetch_leaguedashptstats_calls_correct_endpoint(recording_client):
    """``fetch_leaguedashptstats`` MUST delegate to ``client.get`` with the
    upstream endpoint name ``"leaguedashptstats"``.
    """
    client = recording_client()

    players.fetch_leaguedashptstats(client, "2025-26", "Regular Season", "00")

    assert client.calls, "fetch_leaguedashptstats did not invoke client.get"
    assert client.calls[-1][0] == "leaguedashptstats"


def test_fetch_leaguedashptstats_includes_tracking_params(recording_client):
    """The player-tracking variant MUST emit a ``PtMeasureType`` discriminator
    plus ``PlayerOrTeam`` and ``Season`` in the request params.

    The NBA Stats ``leaguedashptstats`` endpoint requires a ``PtMeasureType``
    discriminator ("SpeedDistance", "Drives", "Passing", etc.) to select the
    tracking measurement category. Asserts on presence and non-emptiness for
    ``PtMeasureType``, presence for ``PlayerOrTeam``, and verbatim value for
    ``Season``.
    """
    client = recording_client()

    players.fetch_leaguedashptstats(client, "2025-26", "Regular Season", "00")

    params = client.calls[-1][1]
    assert "PtMeasureType" in params
    assert params["PtMeasureType"], "PtMeasureType must be non-empty"
    assert "PlayerOrTeam" in params
    assert params["Season"] == "2025-26"


def test_fetch_leaguedashptstats_default_player_or_team(recording_client):
    """Because this wrapper lives in the Players domain module (F-009), the
    default ``PlayerOrTeam`` discriminator MUST be ``"Player"``.

    The Teams-domain consumer can opt into ``"Team"`` via the explicit
    ``player_or_team="Team"`` keyword argument, but the default (as established
    by the wrapper's default param value) resolves to ``"Player"``.
    """
    client = recording_client()

    players.fetch_leaguedashptstats(client, "2025-26", "Regular Season", "00")

    assert client.calls[-1][1]["PlayerOrTeam"] == "Player"


def test_fetch_leaguedashptstats_kwargs_override(recording_client):
    """Title-case kwargs MUST override the wrapper-owned ``PtMeasureType``
    default.

    Passing ``PtMeasureType="Drives"`` overrides the wrapper default
    ``"SpeedDistance"`` via ``params.update(kwargs)`` precedence.
    """
    client = recording_client()

    players.fetch_leaguedashptstats(
        client, "2025-26", "Regular Season", "00", PtMeasureType="Drives"
    )

    assert client.calls[-1][1]["PtMeasureType"] == "Drives"


def test_fetch_leaguedashptstats_returns_raw_payload(
    recording_client, sample_single_table_payload
):
    """The wrapper MUST return the raw dict from ``client.get`` UNMODIFIED.

    Injects a canonical single-table payload via ``responses=``, invokes the
    wrapper, and asserts the returned value equals the injected payload. This
    is the pass-through / identity contract — no transformation, filtering, or
    flattening occurs inside the wrapper.
    """
    client = recording_client(
        responses={"leaguedashptstats": sample_single_table_payload}
    )

    result = players.fetch_leaguedashptstats(
        client, "2025-26", "Regular Season", "00"
    )

    assert result == sample_single_table_payload
