"""Offline tests for the betlive watch engine's reconstruction + flag path
(no network — the refreshOdds rebuild and flag shaping are pure functions)."""
from __future__ import annotations

from src import betlive_watch as W
from src.betlive_anomalies import anomalies_for_event, pick_ml_markets


def _oc(name, oid, odd, line=0):
    return {"marketName": "_", "name": name, "id": oid, "odd": odd,
            "argumentX": line, "argumentString": str(line) if line else "",
            "statusId": 1, "marketStatusId": 1}


def _vba_full_event():
    """The VBA ladder with the home/away swap on the incl-OT 2-way."""
    return {
        "id": 68728161, "homeTeamName": "Ho Chi Minh City Wings",
        "awayTeamName": "Nha Trang Dolphins", "leagueName": "VBA",
        "sportName": "Basketball", "startDate": 0,
        "markets": [
            {"outcomes": [dict(_oc("1", 901, 1.6), marketName="12 (OT)", marketId=396),
                          dict(_oc("2", 902, 2.0), marketName="12 (OT)", marketId=396)]},
            {"outcomes": [dict(_oc("1", 903, 2.3), marketName="Fulltime Result", marketId=409),
                          dict(_oc("X", 904, 12.25), marketName="Fulltime Result", marketId=409),
                          dict(_oc("2", 905, 1.6), marketName="Fulltime Result", marketId=409)]},
            {"outcomes": [dict(_oc("1", 906, 1.8, -2.5), marketName="Points Spread (OT)", marketId=399),
                          dict(_oc("2", 907, 1.8, 2.5), marketName="Points Spread (OT)", marketId=399)]},
        ],
    }


def _watch_entry():
    fe = _vba_full_event()
    reg, ot = pick_ml_markets(fe)
    return {"meta": {k: fe.get(k) for k in
                     ("id", "homeTeamName", "awayTeamName", "leagueName",
                      "sportName", "startDate")},
            "reg": reg, "ot": ot}


def test_pick_ml_markets_grabs_the_two_moneylines_with_ids():
    reg, ot = pick_ml_markets(_vba_full_event())
    assert {o["name"] for o in reg["outcomes"]} == {"1", "X", "2"}
    assert {o["name"] for o in ot["outcomes"]} == {"1", "2"}
    assert {o["id"] for o in ot["outcomes"]} == {901, 902}     # outcome ids captured


def test_refresh_payload_lists_all_ml_outcomes():
    watch = {68728161: _watch_entry()}
    body = W._refresh_payload(watch)
    assert len(body) == 5                                      # 2 (OT) + 3 (FT)
    assert all(d["eventId"] == 68728161 for d in body)
    assert {d["outcomeId"] for d in body} == {901, 902, 903, 904, 905}


def test_reprice_rebuild_detects_flip_from_fresh_odds():
    # Fresh refreshOdds response that re-states the swapped incl-OT prices.
    entry = _watch_entry()
    fresh = {901: {"outcomeId": 901, "odd": 1.6, "suspend": False},
             902: {"outcomeId": 902, "odd": 2.0, "suspend": False},
             903: {"outcomeId": 903, "odd": 2.3, "suspend": False},
             904: {"outcomeId": 904, "odd": 12.25, "suspend": False},
             905: {"outcomeId": 905, "odd": 1.6, "suspend": False}}
    ev = {**entry["meta"], "markets": [W._reprice_market(entry["reg"], fresh),
                                       W._reprice_market(entry["ot"], fresh)]}
    anoms = anomalies_for_event(ev)
    assert len(anoms) == 1 and anoms[0].favourite_flip and anoms[0].side == "away"


def test_reprice_corrected_prices_are_clean():
    # The corrected incl-OT (away favourite) clears the flag through refresh.
    entry = _watch_entry()
    fresh = {901: {"outcomeId": 901, "odd": 2.3, "suspend": False},
             902: {"outcomeId": 902, "odd": 1.6, "suspend": False},
             903: {"outcomeId": 903, "odd": 2.3, "suspend": False},
             904: {"outcomeId": 904, "odd": 12.25, "suspend": False},
             905: {"outcomeId": 905, "odd": 1.6, "suspend": False}}
    ev = {**entry["meta"], "markets": [W._reprice_market(entry["reg"], fresh),
                                       W._reprice_market(entry["ot"], fresh)]}
    assert anomalies_for_event(ev) == []


def test_suspended_fresh_outcome_clears_flag():
    entry = _watch_entry()
    fresh = {901: {"outcomeId": 901, "odd": 1.6, "suspend": True},   # OT home suspended
             902: {"outcomeId": 902, "odd": 2.0, "suspend": False},
             903: {"outcomeId": 903, "odd": 2.3, "suspend": False},
             904: {"outcomeId": 904, "odd": 12.25, "suspend": False},
             905: {"outcomeId": 905, "odd": 1.6, "suspend": False}}
    ev = {**entry["meta"], "markets": [W._reprice_market(entry["reg"], fresh),
                                       W._reprice_market(entry["ot"], fresh)]}
    assert anomalies_for_event(ev) == []


def test_anomaly_to_flag_shape_and_first_seen_carry():
    anoms = anomalies_for_event(_vba_full_event())
    flags = W.flags_with_first_seen(anoms, [], now_iso="2026-06-17T10:00:00+00:00")
    f = flags[0]
    assert f["kind"] == "betlive_flip" and f["book"] == "betlive"
    assert f["betlive_event_id"] == "68728161" and f["cb_event_id"] is None
    assert f["severity"] > 10 and "incl-OT" in f["detail"]
    assert f["first_seen"] == "2026-06-17T10:00:00+00:00"
    # first_seen persists when the same flip is seen again on a later poll
    later = W.flags_with_first_seen(anoms, flags, now_iso="2026-06-17T10:05:00+00:00")
    assert later[0]["first_seen"] == "2026-06-17T10:00:00+00:00"
