"""
Tests for src/ticks.py — change-only tick store (ported from prematch_v2).

Semantics under test (the v2 design guarantees):
  - a tick is written ONLY when a selection's odds change;
  - `latest` mirrors the current value per (market, selection);
  - a market disappearing from a healthy full cycle gets one NULL tick,
    but failed/empty cycles never NULL the book;
  - series() seeds the window's left edge with the prior value so step
    charts start at the edge, not at the first change.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Odds  # noqa: E402
from src.ticks import Store, rows_from_odds  # noqa: E402

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ticks.db")


def _row(src="e1", mtype="moneyline", line=None, sels=None, **over):
    r = {"source_event_id": src, "home": "H", "away": "A", "league": "L",
         "start_time": "2026-06-13T12:00:00+00:00", "market_type": mtype,
         "period": "FT", "line": line, "team_side": None,
         "selections": sels or {"home": 1.9, "away": 1.9}, "meta": None}
    r.update(over)
    return r


def _tick_count(store):
    con = store._connect()
    try:
        return con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    finally:
        con.close()


class TestChangeOnly:
    def test_first_cycle_writes_ticks(self, store):
        res = store.record_cycle("cb", "basketball", [_row()], ts="T1")
        assert res == {"n_events": 1, "n_changes": 2}

    def test_unchanged_cycle_writes_nothing(self, store):
        store.record_cycle("cb", "basketball", [_row()], ts="T1")
        res = store.record_cycle("cb", "basketball", [_row()], ts="T2")
        assert res["n_changes"] == 0
        assert _tick_count(store) == 2  # still just the first cycle's ticks

    def test_changed_selection_writes_one_tick(self, store):
        store.record_cycle("cb", "basketball", [_row()], ts="T1")
        res = store.record_cycle(
            "cb", "basketball",
            [_row(sels={"home": 1.95, "away": 1.9})], ts="T2",
        )
        assert res["n_changes"] == 1
        assert _tick_count(store) == 3

    def test_disappearance_nulls_once_on_healthy_cycle(self, store):
        store.record_cycle("cb", "basketball",
                           [_row("e1"), _row("e2")], ts="T1")
        res = store.record_cycle("cb", "basketball", [_row("e1")], ts="T2")
        assert res["n_changes"] == 2  # e2's two selections -> NULL ticks
        # and they don't re-NULL next cycle
        res = store.record_cycle("cb", "basketball", [_row("e1")], ts="T3")
        assert res["n_changes"] == 0

    def test_failed_cycle_never_nulls(self, store):
        store.record_cycle("cb", "basketball", [_row()], ts="T1")
        store.record_cycle("cb", "basketball", [], ok=False,
                           error="boom", ts="T2")
        assert _tick_count(store) == 2  # no NULL ticks added

    def test_books_are_isolated(self, store):
        store.record_cycle("cb", "basketball", [_row("e1")], ts="T1")
        res = store.record_cycle("pin", "basketball", [_row("99")], ts="T1")
        assert res["n_changes"] == 2
        # pin pruning must not touch cb's keys
        res = store.record_cycle("pin", "basketball", [_row("99")], ts="T2")
        assert res["n_changes"] == 0


class TestSeries:
    def test_series_steps_and_left_edge_seed(self, store):
        store.record_cycle("cb", "basketball", [_row()], ts="2026-06-12T10:00:00")
        store.record_cycle("cb", "basketball",
                           [_row(sels={"home": 2.05, "away": 1.8})],
                           ts="2026-06-12T11:00:00")
        eid = store.event_id("cb", "basketball", "e1")
        # window starting AFTER the first tick: left edge seeded with 1.9
        ser = store.series(eid, "moneyline", "FT", None, None,
                           since_ts="2026-06-12T10:30:00")
        assert ser["home"][0] == ["2026-06-12T10:30:00", 1.9]
        assert ser["home"][1] == ["2026-06-12T11:00:00", 2.05]

    def test_series_line_tolerance_follows_nearest(self, store):
        store.record_cycle("cb", "basketball",
                           [_row(mtype="total", line=220.5,
                                 sels={"over": 1.9, "under": 1.9})], ts="T1")
        eid = store.event_id("cb", "basketball", "e1")
        ser = store.series(eid, "total", "FT", 220.25, None, since_ts="T0")
        assert "over" in ser            # 220.25 vs 220.5 within 0.26
        ser = store.series(eid, "total", "FT", 250.0, None, since_ts="T0")
        assert ser == {}

    def test_restart_recovers_latest_from_disk(self, store, tmp_path):
        store.record_cycle("cb", "basketball", [_row()], ts="T1")
        re = Store(tmp_path / "ticks.db")   # fresh instance, same file
        res = re.record_cycle("cb", "basketball", [_row()], ts="T2")
        assert res["n_changes"] == 0        # latest cache reloaded — no dupes


class TestOddsAdapter:
    def _odds(self, **over):
        base = dict(
            source="crystalbet", sport="soccer", home="H", away="A",
            market_type="total", period="FT",
            selections={"over": 1.9, "under": 1.9}, fetched_at=NOW,
            line=9.5, league="L", raw_event_id="77",
        )
        base.update(over)
        return Odds(**base)

    def test_submarket_folds_into_market_type(self):
        rows = rows_from_odds([self._odds(submarket="corners")])
        assert rows[0]["market_type"] == "corners_total"

    def test_max_stake_rides_in_meta(self):
        rows = rows_from_odds([self._odds(source="pinnacle", max_stake=400.0)])
        assert rows[0]["meta"] == {"max_stake": 400.0}
        rows = rows_from_odds([self._odds()])
        assert rows[0]["meta"] is None

    def test_cycle_status_heartbeat(self, store):
        store.record_cycle("cb", "basketball", [_row()], ts="T1", dur_ms=123)
        store.record_cycle("pin", "basketball", [], ok=False, error="x", ts="T2")
        status = {(c["book"], c["sport"]): c for c in store.cycle_status()}
        assert status[("cb", "basketball")]["ok"] == 1
        assert status[("cb", "basketball")]["dur_ms"] == 123
        assert status[("pin", "basketball")]["ok"] == 0
