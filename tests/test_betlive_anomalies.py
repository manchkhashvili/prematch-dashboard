"""Tests for the betlive incl-OT / regulation favourite-flip detector."""
from __future__ import annotations

from src.betlive_anomalies import anomalies_for_event, find_betlive_anomalies


def _mk(name, marketId, outcomes):
    """outcomes: list of (label, odd) or (label, odd, line)."""
    ocs = []
    for o in outcomes:
        label, odd = o[0], o[1]
        line = o[2] if len(o) > 2 else 0
        ocs.append({"marketName": name, "marketId": marketId, "name": label,
                    "odd": odd, "argumentX": line, "argumentString": str(line) if line else "",
                    "statusId": 1, "marketStatusId": 1})
    return {"outcomes": ocs}


def _event(markets, **kw):
    ev = {"id": 1, "homeTeamName": "Home", "awayTeamName": "Away",
          "sportName": "Basketball", "leagueName": "VBA", "markets": markets}
    ev.update(kw)
    return ev


# The exact VBA prices from the screenshot.
VBA = _event([
    _mk("12 (OT)", 396, [("1", 1.6), ("2", 2.0)]),
    _mk("Fulltime Result", 409, [("1", 2.3), ("X", 12.25), ("2", 1.6)]),
    _mk("Points Spread (OT)", 399, [("1", 1.8, -2.5), ("2", 1.8, 2.5)]),
])


def test_vba_favourite_flip_detected():
    anoms = anomalies_for_event(VBA)
    assert len(anoms) == 1
    a = anoms[0]
    assert a.side == "away"               # away lost probability folding in OT
    assert a.favourite_flip is True
    assert a.gap_pp > 10                   # ~13pp violation
    # away was the regulation favourite, home the incl-OT favourite
    assert a.fair_reg[2] > a.fair_reg[0]
    assert a.fair_ot[0] > a.fair_ot[1]


def test_coherent_event_is_clean():
    # The VBA bug fixed: incl-OT keeps away the favourite (the draw folds into
    # both sides, lifting each above its regulation prob). No violation.
    ev = _event([
        _mk("12 (OT)", 396, [("1", 2.3), ("2", 1.6)]),
        _mk("Fulltime Result", 409, [("1", 2.3), ("X", 12.25), ("2", 1.6)]),
    ])
    assert anomalies_for_event(ev) == []


def test_points_spread_not_treated_as_moneyline():
    # Only the spread (lined 1/2) is present besides the result — no clean 2-way
    # winner exists, so nothing to compare against -> no anomaly.
    ev = _event([
        _mk("Fulltime Result", 409, [("1", 2.3), ("X", 12.25), ("2", 1.6)]),
        _mk("Points Spread (OT)", 399, [("1", 1.6, -2.5), ("2", 2.0, 2.5)]),
    ])
    assert anomalies_for_event(ev) == []


def test_subperiod_and_prop_markets_ignored():
    # The China-U21 false-positive shape: a 2nd-half 1X2 and a "which team
    # scores" prop must NOT be paired as full-game result/winner.
    ev = _event([
        _mk("Which Team Scores  Point (OT)", 700, [("1", 1.75), ("2", 1.8)]),
        _mk("2nd Half - Winner (1X2) (OT)", 800, [("1", 1.65), ("X", 22), ("2", 2.0)]),
    ])
    assert anomalies_for_event(ev) == []


def test_soccer_style_skipped():
    # A 3-way result with no separate 2-way winner (plain soccer) never fires.
    ev = _event([_mk("Fulltime Result", 1, [("1", 2.5), ("X", 3.3), ("2", 2.7)])],
                sportName="Soccer")
    assert anomalies_for_event(ev) == []


def test_suspended_outcomes_filtered():
    # A suspended (statusId != 1) winner leg leaves no bettable 2-way -> skip.
    ev = _event([
        _mk("Fulltime Result", 409, [("1", 2.3), ("X", 12.25), ("2", 1.6)]),
        {"outcomes": [
            {"marketName": "12 (OT)", "marketId": 396, "name": "1", "odd": 1.6,
             "statusId": 2, "marketStatusId": 1, "argumentX": 0, "argumentString": ""},
            {"marketName": "12 (OT)", "marketId": 396, "name": "2", "odd": 2.0,
             "statusId": 1, "marketStatusId": 1, "argumentX": 0, "argumentString": ""},
        ]},
    ])
    assert anomalies_for_event(ev) == []


def test_threshold_and_sort():
    big = _event(VBA["markets"], id=1, homeTeamName="A", awayTeamName="B")
    small = _event([
        _mk("12 (OT)", 396, [("1", 1.95), ("2", 1.85)]),
        _mk("Fulltime Result", 409, [("1", 2.05), ("X", 15.0), ("2", 1.9)]),
    ], id=2, homeTeamName="C", awayTeamName="D")
    found = find_betlive_anomalies([small, big], min_gap_pp=1.5)
    assert [a.gap_pp for a in found] == sorted((a.gap_pp for a in found), reverse=True)
    # a high threshold filters the small one out
    assert all(a.gap_pp >= 10 for a in find_betlive_anomalies([small, big], min_gap_pp=10))
