"""/api/anomalies/alerts — the cheap content feed for the Anomalies alert config.

Split from /api/anomalies deliberately: that one runs the fuzzy matcher to
attach Pinnacle fair prices, and the alert poller runs on EVERY page every 30s
from every open tab. This endpoint must stay a dumb read of in-memory state.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.app as app  # noqa: E402
from src.anomalies import find_ladder_anomalies  # noqa: E402
from src.models import Odds  # noqa: E402

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _spread(line, h, a, *, event="EVT1") -> Odds:
    return Odds(
        source="crystalbet", sport="basketball", home="HOME", away="AWAY",
        market_type="spread", period="FT", selections={"home": h, "away": a},
        fetched_at=NOW, line=line, league="Test Lg", start_time=NOW,
        raw_event_id=event,
    )


def _flag(kind="ml_vs_spread", severity=7.0, *, book="cb", event="EVT1",
          periods="FT", outcome=None) -> dict:
    return {
        "book": book, "sport": "basketball", "league": "Test Lg",
        "match_label": "HOME — AWAY", "home": "HOME", "away": "AWAY",
        "cb_event_id": event, "book_event_id": event, "start_time": None,
        "kind": kind, "periods": periods, "detail": "d",
        "severity": severity, "outcome": outcome,
    }


@pytest.fixture(autouse=True)
def _clean_state():
    """Every list the endpoint reads, reset — otherwise a flag left behind by
    another module's test leaks into these assertions."""
    saved = {n: getattr(app, n) for n in (
        "_recent_anomalies", "_extra_anomalies", "_book_anomalies",
        "_recent_consistency", "_betlive_consistency", "_soft_scan_flags",
        "_cb_soft_flags", "_betlive_soft_flags", "_extra_anom_consistency",
        "_book_consistency", "_anomalies_computed_at",
    )}
    app._recent_anomalies = []
    app._extra_anomalies = {}
    app._book_anomalies = {}
    app._recent_consistency = []
    app._betlive_consistency = []
    app._soft_scan_flags = []
    app._cb_soft_flags = []
    app._betlive_soft_flags = []
    app._extra_anom_consistency = {}
    app._book_consistency = {}
    app._anomalies_computed_at = NOW
    yield
    for n, v in saved.items():
        setattr(app, n, v)


def _get() -> dict:
    return asyncio.run(app.api_anomalies_alerts())


def _seed_ladder():
    # home spike: +4.5@1.60 -> +5.0@2.00. Expected 'down', so the home side is a
    # violation of 0.40 decimal = 25% of the smaller price, across a 0.5 step.
    anoms = find_ladder_anomalies([_spread(4.5, 1.60, 2.00), _spread(5.0, 2.00, 1.60)])
    app._recent_anomalies = [app._anomaly_base_row(a) for a in anoms]
    return anoms


# ── shape ────────────────────────────────────────────────────────────────────

def test_returns_both_feeds_and_the_scan_timestamp():
    j = _get()
    assert set(j) == {"enabled", "computed_at", "ladders", "consistency"}
    assert j["computed_at"] == NOW.isoformat()


def test_ladder_row_carries_the_three_alertable_quantities():
    """The panel offers % off, odds change and ladder step — all three must be
    on the row or the corresponding criterion can never fire."""
    _seed_ladder()
    row = _get()["ladders"][0]
    for f in ("pct", "delta", "step"):
        assert row[f] is not None, f"{f} missing — that alert criterion is dead"


def test_step_is_the_gap_between_the_two_rungs():
    """`step` is derived here because the row only stores line_lo/line_hi."""
    _seed_ladder()
    for row in _get()["ladders"]:
        assert row["step"] == pytest.approx(abs(row["line_hi"] - row["line_lo"]))
        assert row["step"] == pytest.approx(0.5)


def test_ladder_row_carries_an_identity_for_dedup():
    """Without a stable key the poller re-alerts on the same rung every 30s."""
    _seed_ladder()
    row = _get()["ladders"][0]
    for f in ("book", "event_id", "market", "period", "side", "line_lo", "line_hi"):
        assert f in row


def test_consistency_row_carries_kind_and_severity():
    app._recent_consistency = [_flag(kind="htft_combo", severity=9.5, outcome="1/1")]
    row = _get()["consistency"][0]
    assert row["kind"] == "htft_combo"
    assert row["severity"] == 9.5
    assert row["outcome"] == "1/1"
    for f in ("book", "event_id", "periods"):
        assert f in row


# ── every source is merged, not just the CB scan ─────────────────────────────

def test_consistency_merges_every_source():
    """Six lists feed the tab. Missing one silently means those checks can
    never alert — and they are exactly the ones (soft scan, betlive) the
    per-check thresholds exist for."""
    app._recent_consistency = [_flag(kind="ml_vs_spread", event="A")]
    app._betlive_consistency = [_flag(kind="betlive_flip", book="betlive", event="B")]
    app._soft_scan_flags = [_flag(kind="soccer_htft", book="lider", event="C")]
    app._cb_soft_flags = [_flag(kind="basketball_fav", event="D")]
    app._betlive_soft_flags = [_flag(kind="basketball_fav", book="betlive", event="E")]
    app._extra_anom_consistency = {"soccer": [_flag(kind="htft_combo", event="F")]}
    app._book_consistency = {("lider", "soccer"): [_flag(kind="htft_fair", event="G")]}
    got = {r["event_id"] for r in _get()["consistency"]}
    assert got == {"A", "B", "C", "D", "E", "F", "G"}


def test_ladders_merge_extra_sports_and_other_books():
    _seed_ladder()
    app._extra_anomalies = {"soccer": [{"pct": 3.0, "delta": 0.1, "line_lo": 1.0,
                                        "line_hi": 2.0, "book": "cb"}]}
    app._book_anomalies = {("lider", "soccer"): [{"pct": 4.0, "delta": 0.2,
                                                  "line_lo": 0.0, "line_hi": 1.5,
                                                  "book": "lider"}]}
    books = {r["book"] for r in _get()["ladders"]}
    assert "lider" in books and "cb" in books


# ── ordering + the payload cap ───────────────────────────────────────────────

def test_rows_are_sorted_biggest_first():
    app._recent_consistency = [_flag(severity=s, event=f"E{s}") for s in (2.0, 30.0, 11.0)]
    assert [r["severity"] for r in _get()["consistency"]] == [30.0, 11.0, 2.0]


def test_cap_drops_the_smallest_not_the_biggest():
    """The cap is a payload guard. Because alerting is a magnitude threshold,
    truncating the SMALLEST rows can never hide one that would have fired."""
    n = app.ALERT_FEED_CAP + 50
    app._recent_consistency = [_flag(severity=float(i), event=f"E{i}") for i in range(n)]
    rows = _get()["consistency"]
    assert len(rows) == app.ALERT_FEED_CAP
    assert rows[0]["severity"] == float(n - 1)
    assert min(r["severity"] for r in rows) == float(n - app.ALERT_FEED_CAP)


# ── it must stay cheap ───────────────────────────────────────────────────────

def test_endpoint_does_no_pinnacle_enrichment():
    """The whole reason this endpoint exists. If it ever starts matching, the
    poller becomes the CPU burn that /api/anomalies is."""
    import inspect
    src = inspect.getsource(app.api_anomalies_alerts)
    for banned in ("_enrich_anomaly_rows", "match_with_diagnostics", "asyncio.to_thread"):
        assert banned not in src, f"{banned} in the alert feed — it is meant to be cheap"


def test_empty_state_is_a_clean_empty_feed():
    j = _get()
    assert j["ladders"] == [] and j["consistency"] == []


# ── every scan that can fill the tab must reach the alert feed too ───────────
# The Lider combo scan was wired into /api/anomalies and NOT into the alert
# feed. So its flags were on screen, configurable in the alert grid, and
# incapable of ever firing — 81 of the 97 flags on the board when this was
# found. A settings panel that can configure a feed which never emits is worse
# than no panel, because it says otherwise.

def _lider_flag(**kw):
    base = {"book": "liderbet", "sport": "soccer", "kind": "combo_duplicate",
            "match_label": "A — B", "book_event_id": "E1", "periods": "FT",
            "outcome": None, "severity": 30.0, "odds": 6.5}
    base.update(kw)
    return base


def test_lider_combo_flags_reach_the_alert_feed(monkeypatch):
    monkeypatch.setattr(app, "_lider_combo_flags", [_lider_flag()])
    rows = app._consistency_alert_rows()
    assert any(r["kind"] == "combo_duplicate" for r in rows), \
        "lider combo flags are on the tab but invisible to alerts"


def test_the_alert_row_carries_the_bettable_price(monkeypatch):
    """Without it the client cannot gate on odds, which is the whole point of
    the veto: a flag on a 40.0 leg is noise however big its severity."""
    monkeypatch.setattr(app, "_lider_combo_flags", [_lider_flag(odds=6.5)])
    row = next(r for r in app._consistency_alert_rows()
               if r["kind"] == "combo_duplicate")
    assert row["odds"] == 6.5


def test_a_flag_without_a_price_still_reaches_the_feed(monkeypatch):
    """`odds` is optional — checks that name a relationship rather than one leg
    must not be dropped on the way to the client."""
    monkeypatch.setattr(app, "_lider_combo_flags", [_lider_flag(odds=None)])
    row = next(r for r in app._consistency_alert_rows()
               if r["kind"] == "combo_duplicate")
    assert row["odds"] is None


def test_the_feed_reports_enabled_when_only_the_lider_scan_is_on(monkeypatch):
    """The client bails on `enabled: false` before reading its own rows, so a
    scan missing from this disjunction can never fire even once its flags are
    in the feed."""
    from src import runtime_config
    monkeypatch.setattr(runtime_config, "is_on",
                        lambda group, name: name == "lider_combo")
    feed = asyncio.run(app.api_anomalies_alerts())
    assert feed["enabled"] is True
