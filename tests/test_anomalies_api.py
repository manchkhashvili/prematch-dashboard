"""Tests for the anomaly scanner wiring in src/app.py.

Covers the bet-rung selection, the /api/anomalies filtering/sorting, and the
request-time Pinnacle attachment (fair-at-line + edge). The browser-dependent
scrape (_compute_anomalies) is NOT exercised here — it needs Playwright; CC
verifies that live. We seed the module state directly instead.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.app as app  # noqa: E402
from src.anomalies import find_ladder_anomalies  # noqa: E402
from src.models import Odds  # noqa: E402

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _spread(line, h, a, *, event="EVT1", home="HOME", away="AWAY") -> Odds:
    return Odds(
        source="crystalbet", sport="basketball", home=home, away=away,
        market_type="spread", period="FT", selections={"home": h, "away": a},
        fetched_at=NOW, line=line, league="Test Lg", start_time=NOW, raw_event_id=event,
    )


def _seed(cb_odds, *, pin_odds=None, moves=None):
    anoms = find_ladder_anomalies(cb_odds, markets=("spread", "total"), min_pct=0.0)
    app._anomaly_cb_odds = cb_odds
    app._recent_anomalies = [app._anomaly_base_row(a) for a in anoms]
    app._anomalies_computed_at = NOW
    app._anomalies_cb_fetched_at = NOW
    app._anomalies_error = None
    app._recent_consistency = []   # reset to avoid cross-test pollution
    app._anomaly_watchlist = set()
    if "basketball" in app._state:
        app._state["basketball"]["pin"]["odds"] = pin_odds or []
    app._recent_moves["basketball"] = moves or []
    return anoms


# ── bet-rung direction ────────────────────────────────────────────────────────

def test_base_row_bet_rung_picks_inflated_price():
    # home spike: +4.5@1.60 -> +5.0@2.00 (expected down). The inflated rung is
    # the higher line at the higher price.
    anoms = find_ladder_anomalies([_spread(4.5, 1.60, 2.00), _spread(5.0, 2.00, 1.60)])
    home = next(a for a in anoms if a.side == "home")
    row = app._anomaly_base_row(home)
    assert row["expected"] == "down"
    assert row["bet_line"] == 5.0 and row["bet_odds"] == 2.00

    away = next(a for a in anoms if a.side == "away")
    rowa = app._anomaly_base_row(away)
    # away expected 'up'; the inflated price is at the lower line.
    assert rowa["expected"] == "up"
    assert rowa["bet_line"] == 4.5 and rowa["bet_odds"] == 2.00


# ── endpoint filtering + sorting ────────────────────────────────────────────--

def test_endpoint_filters_and_sorts():
    _seed([
        _spread(1.0, 1.80, 1.90),
        _spread(2.0, 1.85, 1.88),   # small violations (~1-3%)
        _spread(3.0, 2.40, 1.80),   # big home violation (~30%)
    ])
    res = asyncio.run(app.api_anomalies(min_pct=5.0, spread_only=False))
    assert res["count"] >= 1
    pcts = [r["pct"] for r in res["anomalies"]]
    assert pcts == sorted(pcts, reverse=True)        # sorted desc
    assert all(p >= 5.0 for p in pcts)               # threshold respected

    # min_pct=0 surfaces strictly more rows.
    res_all = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False))
    assert res_all["count"] >= res["count"]


def test_endpoint_spread_only_excludes_totals():
    def _total(line, o, u):
        return Odds(source="crystalbet", sport="basketball", home="HOME", away="AWAY",
                    market_type="total", period="FT", selections={"over": o, "under": u},
                    fetched_at=NOW, line=line, league="Test Lg", start_time=NOW, raw_event_id="EVT1")
    _seed([
        _spread(1.0, 2.00, 1.65), _spread(2.0, 2.20, 1.60),     # spread violation
        _total(210.0, 1.50, 2.40), _total(211.0, 1.45, 2.50),   # total violation
    ])
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=True))
    assert {r["market_type"] for r in res["anomalies"]} == {"spread"}


# ── Pinnacle attachment ───────────────────────────────────────────────────────

def test_pin_fair_and_edge_attached_on_matching_line():
    cb = [_spread(4.0, 1.50, 2.20), _spread(4.5, 1.60, 2.00),
          _spread(5.0, 2.00, 1.60), _spread(6.0, 1.80, 1.75)]
    pin = [Odds(source="pinnacle", sport="basketball", home="HOME", away="AWAY",
                market_type="spread", period="FT", line=5.0,
                selections={"home": 1.80, "away": 2.00}, fetched_at=NOW,
                start_time=NOW, league="Test Lg", raw_event_id="PIN1")]
    moves = [{"match_label": "HOME — AWAY", "market": "moneyline FT",
              "side": "home", "delta_pp": 6.4}]
    _seed(cb, pin_odds=pin, moves=moves)
    res = asyncio.run(app.api_anomalies(min_pct=5.0, spread_only=False))

    # The home +5.0 rung (bet_line 5.0) must get a fair price + a positive edge.
    spike = next(r for r in res["anomalies"]
                 if r["side"] == "home" and r["bet_line"] == 5.0)
    assert spike["pin_fair_odds"] is not None
    assert spike["pin_edge_pct"] is not None and spike["pin_edge_pct"] > 0
    assert spike["pin_move_pp"] == 6.4           # match-level move attached

    # A rung whose bet_line Pinnacle doesn't quote stays None on fair/edge.
    no_line = next((r for r in res["anomalies"]
                    if r["side"] == "away" and r["bet_line"] == 4.5), None)
    if no_line is not None:
        assert no_line["pin_fair_odds"] is None
        assert no_line["pin_edge_pct"] is None


def test_endpoint_exposes_consistency_with_severity_filter():
    _seed([_spread(1.0, 2.00, 1.65), _spread(2.0, 2.20, 1.60)])
    app._recent_consistency = [
        {"sport": "basketball", "league": "L", "match_label": "H — A",
         "home": "H", "away": "A", "cb_event_id": "X", "start_time": None,
         "kind": "total_additivity", "periods": "H1+H2 vs FT",
         "detail": "off by -20 pts", "severity": 20.0},
        {"sport": "basketball", "league": "L", "match_label": "H — A",
         "home": "H", "away": "A", "cb_event_id": "X", "start_time": None,
         "kind": "ml_vs_spread", "periods": "FT", "detail": "gap 6pp", "severity": 6.0},
    ]
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False, min_severity=0.0))
    assert res["consistency_count"] == 2
    res2 = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False, min_severity=10.0))
    assert res2["consistency_count"] == 1
    assert res2["consistency"][0]["kind"] == "total_additivity"


def test_scan_history_appends_with_header_once(tmp_path):
    app._ANOM_HISTORY = tmp_path / "anomalies_history.csv"
    app._CONS_HISTORY = tmp_path / "consistency_history.csv"
    anoms = [{"league": "L", "match_label": "H — A", "start_time": None,
              "cb_event_id": "X", "period": "FT", "market": "spread FT",
              "section": "Handicap", "side": "home", "line_lo": 4.5, "line_hi": 5.0,
              "odds_lo": 1.6, "odds_hi": 2.0, "pct": 25.0, "bet_line": 5.0,
              "bet_odds": 2.0, "pin_fair_odds": 1.9, "pin_edge_pct": 5.3}]
    flags = [{"league": "L", "match_label": "H — A", "cb_event_id": "X",
              "kind": "total_additivity", "periods": "H1+H2 vs FT",
              "detail": "off", "severity": 20.0}]
    app._append_scan_history("2026-05-31T18:45:00+00:00", anoms, flags)
    app._append_scan_history("2026-05-31T19:45:00+00:00", anoms, flags)

    a_lines = app._ANOM_HISTORY.read_text().strip().splitlines()
    assert a_lines[0].startswith("scan_ts,")     # header once
    assert len(a_lines) == 3                       # header + 2 appended rows
    c_lines = app._CONS_HISTORY.read_text().strip().splitlines()
    assert len(c_lines) == 3


def test_coverage_stats_counts_ladders_and_misses():
    # E1: a real 3-rung spread ladder. E2: a single list-view rung (no ladder).
    cb = [_spread(1.0, 2.0, 1.8, event="E1"), _spread(2.0, 1.9, 1.9, event="E1"),
          _spread(3.0, 1.8, 2.0, event="E1"),
          _spread(0.0, 1.9, 1.9, event="E2")]
    cov = app._coverage_stats(cb)
    assert cov["games"] == 2
    assert cov["ladders"] == 1            # only E1 has >=2 rungs in a section
    assert cov["rungs"] == 4
    assert cov["games_without_ladder"] == 1   # E2 couldn't be ladder-checked


def test_endpoint_exposes_coverage():
    _seed([_spread(1.0, 2.0, 1.8), _spread(2.0, 1.9, 1.9)])
    app._anomaly_coverage = {"odds": 5, "games": 1, "ladders": 1, "rungs": 2,
                             "moneylines": 0, "games_without_ladder": 0}
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False, min_severity=0.0))
    assert res["coverage"]["games"] == 1 and res["coverage"]["ladders"] == 1


def _anom_ladder(event):
    # home +4.5@1.60 -> +5.0@2.00 = a home-side violation.
    return [_spread(4.5, 1.60, 2.00, event=event), _spread(5.0, 2.00, 1.60, event=event)]


def _clean_ladder(event):
    return [_spread(1.0, 2.10, 1.60, event=event), _spread(2.0, 1.90, 1.75, event=event)]


def test_merge_watch_rescan_drops_clean_keeps_anomalous_leaves_others():
    # Current state: E1 anomalous, E3 anomalous (untouched), E2 had nothing.
    cur_cb = _anom_ladder("E1") + _clean_ladder("E2") + _anom_ladder("E3")
    cur_anoms = ([app._anomaly_base_row(a) for a in
                  find_ladder_anomalies(_anom_ladder("E1"))]
                 + [app._anomaly_base_row(a) for a in
                    find_ladder_anomalies(_anom_ladder("E3"))])
    cur_watch = {"E1", "E3"}
    # Re-scan E1 + E2: E1 now clean, E2 now anomalous.
    fresh = _clean_ladder("E1") + _anom_ladder("E2")

    new_cb, new_anoms, new_flags, new_watch = app._merge_watch_rescan(
        {"E1", "E2"}, fresh, cur_cb, cur_anoms, [], cur_watch)

    assert new_watch == {"E2", "E3"}              # E1 dropped, E2 added, E3 kept
    anom_events = {r["cb_event_id"] for r in new_anoms}
    assert anom_events == {"E2", "E3"}            # E1's rows gone, E2's added, E3 untouched
    assert {o.raw_event_id for o in new_cb} == {"E1", "E2", "E3"}  # E1 still present (clean)


def test_endpoint_exposes_watch_fields():
    _seed([_spread(1.0, 2.0, 1.8), _spread(2.0, 1.9, 1.9)])
    app._anomaly_watchlist = {"EVT1"}
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False, min_severity=0.0))
    assert res["watch_count"] == 1 and res["watch_sec"] == app.ANOMALY_WATCH_SEC


def test_default_min_pct_is_point_five():
    import inspect
    sig = inspect.signature(app.api_anomalies)
    assert sig.parameters["min_pct"].default.default == 0.5


def test_endpoint_exposes_clock_alignment_fields():
    _seed([_spread(1.0, 2.00, 1.65), _spread(2.0, 2.20, 1.60)])
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False, min_severity=0.0))
    assert "at_minutes" in res and "next_scan_in_sec" in res
    if res["at_minutes"] is not None:
        # next scan is within the next hour and strictly positive.
        assert 0 < res["next_scan_in_sec"] <= 3600


def test_seconds_until_minutes_picks_soonest():
    now = datetime(2026, 5, 31, 10, 20, 0)   # next of [15,45] is 10:45 (1500s)
    assert app._seconds_until_minutes([15, 45], now=now) == 25 * 60
    now2 = datetime(2026, 5, 31, 10, 50, 0)  # next is 11:15 (25 min)
    assert app._seconds_until_minutes([15, 45], now=now2) == 25 * 60


def test_status_endpoint_is_cheap_and_has_computed_at():
    _seed([_spread(1.0, 2.00, 1.65), _spread(2.0, 2.20, 1.60)])
    res = asyncio.run(app.api_anomalies_status())
    assert "computed_at" in res and "anomalies" in res and "consistency" in res
    # heartbeat reports raw counts (no min_severity filtering)
    assert res["anomalies"] == len(app._recent_anomalies)


# ── clock-aligned scheduling ──────────────────────────────────────────────────

def test_seconds_until_minute_is_within_the_hour():
    for m in (0, 15, 45, 59):
        s = app._seconds_until_minute(m)
        assert 0 < s <= 3600


def test_seconds_until_minute_targets_correct_minute():
    from datetime import timedelta
    now = datetime(2026, 5, 31, 10, 12, 30)  # 10:12:30 → next :45 is 10:45:00
    s = app._seconds_until_minute(45, now=now)
    assert s == (45 - 12) * 60 - 30          # 32 min 30 s = 1950 s
    assert (now + timedelta(seconds=s)).replace(microsecond=0) == \
        datetime(2026, 5, 31, 10, 45, 0)


def test_seconds_until_minute_rolls_to_next_hour_when_past():
    now = datetime(2026, 5, 31, 10, 50, 0)   # already past :45 → next is 11:45
    s = app._seconds_until_minute(45, now=now)
    assert s == 55 * 60                       # 55 minutes


def test_no_pin_state_leaves_pin_fields_none():
    _seed([_spread(1.0, 2.00, 1.65), _spread(2.0, 2.20, 1.60)], pin_odds=[], moves=[])
    res = asyncio.run(app.api_anomalies(min_pct=0.0, spread_only=False))
    assert res["anomalies"]
    assert all(r["pin_fair_odds"] is None and r["pin_edge_pct"] is None
               for r in res["anomalies"])
