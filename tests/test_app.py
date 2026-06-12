"""
Regression tests for prematch.src.app's _build_match_row.

Guards the 2026-05-24 "Bug: matches-page main row picked Pinnacle H1 ML
instead of FT" entry: the main row must filter Pinnacle ML by BOTH
market_type AND period, otherwise H1 prices get compared to CB's FT prices
and the dashboard renders phantom edges.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import _build_match_row  # noqa: E402
from src.matcher import MatchedEvent  # noqa: E402
from src.models import Odds  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _odds(source: str, market_type: str, selections: dict, *,
          period: str = "FT", line: float | None = None) -> Odds:
    return Odds(
        source=source,  # type: ignore[arg-type]
        sport="basketball",
        home="Hawks",
        away="Lakers",
        market_type=market_type,  # type: ignore[arg-type]
        period=period,  # type: ignore[arg-type]
        selections=selections,
        fetched_at=NOW,
        line=line,
        start_time=NOW,
        league="NBA",
        raw_event_id="evt-1",
    )


class TestMainRowPeriodFilter:
    """
    The original bug: `pin_ml` was picked with `next((o for o in match.pin if
    o.market_type == "moneyline"), None)` — no period filter. When H1 came
    first in match.pin, the main row showed H1 prices and computed edges
    against CB's FT prices.

    Verified bug repro (synthetic): CB FT 1.30/4.20, Pin FT 1.04/7.70, Pin H1
    1.12/5.04. Without the period filter, away_edge would compare 4.20 vs
    devig(1.12/5.04) ≈ negative edge but a different magnitude than the FT
    correct answer. With the filter, edge is computed from FT prices on both
    sides.
    """

    def test_main_row_uses_ft_pin_when_h1_appears_first(self):
        """If H1 is listed FIRST in match.pin, the period filter must still
        pick FT to match CB's FT moneyline."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20}, period="FT")
        pin_h1 = _odds("pinnacle", "moneyline",
                       {"home": 1.12, "away": 5.04}, period="H1")
        pin_ft = _odds("pinnacle", "moneyline",
                       {"home": 1.04, "away": 7.70}, period="FT")

        match = MatchedEvent(
            cb=[cb_ml],
            pin=[pin_h1, pin_ft],   # H1 FIRST — the bug condition
            home="Hawks", away="Lakers", score=100.0,
        )
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match)

        # Main row's Pin prices must be the FT ones (1.04 / 7.70), NOT H1.
        assert row["pin_home"] == pytest.approx(1.04)
        assert row["pin_away"] == pytest.approx(7.70)

    def test_main_row_uses_ft_pin_when_only_ft_available(self):
        """Sanity: with only FT available the row picks FT (baseline)."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20}, period="FT")
        pin_ft = _odds("pinnacle", "moneyline",
                       {"home": 1.04, "away": 7.70}, period="FT")
        match = MatchedEvent(
            cb=[cb_ml], pin=[pin_ft],
            home="Hawks", away="Lakers", score=100.0,
        )
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match)
        assert row["pin_home"] == pytest.approx(1.04)

    def test_main_row_null_pin_when_only_h1_pin_available(self):
        """If CB has FT but Pinnacle only offered H1 ML for this game, the
        period filter must produce no match → main row's pin columns stay
        None rather than borrowing the H1 price."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20}, period="FT")
        pin_h1 = _odds("pinnacle", "moneyline",
                       {"home": 1.12, "away": 5.04}, period="H1")
        match = MatchedEvent(
            cb=[cb_ml], pin=[pin_h1],
            home="Hawks", away="Lakers", score=100.0,
        )
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match)
        # No FT pin available → main row pin cols stay None.
        assert row["pin_home"] is None
        assert row["pin_away"] is None
        assert row["edge_home_pct"] is None

    def test_unmatched_event_still_renders_row_with_null_pin(self):
        """An unmatched CB event still appears in the matches table, with all
        Pinnacle columns null. Verifies has_pin=False path."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None)
        assert row["has_pin"] is False
        assert row["cb_home"] == 1.30
        assert row["cb_away"] == 4.20
        assert row["pin_home"] is None
        assert row["edge_home_pct"] is None


class TestMarketsStatus:
    """Verify the new markets_status field flows through to the row dict."""

    def test_status_loaded_when_event_in_status_map(self):
        """When the change cache reports 'loaded' for this event, the row
        should expose that — frontend shows the badge accordingly."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        status_map = {"evt-1": "loaded"}
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None,
                                detail_status=status_map)
        assert row["markets_status"] == "loaded"

    def test_status_expand_failed_passes_through(self):
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        status_map = {"evt-1": "expand_failed"}
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None,
                                detail_status=status_map)
        assert row["markets_status"] == "expand_failed"

    def test_status_defaults_to_list_only_when_event_unknown(self):
        """Event not in the status map (cache hasn't seen it yet) → list_only.
        This is the safe default — list-view Odds are always present."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None,
                                detail_status={})
        assert row["markets_status"] == "list_only"

    def test_status_defaults_to_list_only_when_status_map_none(self):
        """No status map provided at all (backward compat path) → list_only."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None)
        assert row["markets_status"] == "list_only"


class TestLastExpandedAt:
    """The last_expanded_at field lets the frontend show a staleness
    indicator per game ("12m" / "2h ago")."""

    def test_iso_string_when_event_in_map(self):
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        when = datetime(2026, 5, 25, 18, 30, 0, tzinfo=timezone.utc)
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None,
                                last_expanded={"evt-1": when})
        assert row["last_expanded_at"] == "2026-05-25T18:30:00+00:00"

    def test_null_when_event_never_expanded(self):
        """Game we've never successfully expanded → no timestamp. Frontend
        will render this as "—" or similar; the FULL/LIST/FAIL badge already
        tells the user it's list-only."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None,
                                last_expanded={})
        assert row["last_expanded_at"] is None

    def test_null_when_map_not_provided(self):
        """Backward-compat: tests that don't pass last_expanded shouldn't break."""
        cb_ml = _odds("crystalbet", "moneyline",
                      {"home": 1.30, "away": 4.20})
        row = _build_match_row("Hawks", "Lakers", [cb_ml], match=None)
        assert row["last_expanded_at"] is None


# ── consistency-flag first_seen carry-over (2026-06-12) ───────────────────────
from src.app import _merge_flag_first_seen  # noqa: E402


def _flag(kind="htft_fair", eid="E1", outcome="1/1", detail="x", **over):
    f = {"kind": kind, "cb_event_id": eid, "periods": "HT/FT",
         "outcome": outcome, "detail": detail, "severity": 5.0}
    f.update(over)
    return f


def test_first_seen_set_on_new_flag():
    out = _merge_flag_first_seen([_flag()], [], "2026-06-12T10:00:00+00:00")
    assert out[0]["first_seen"] == "2026-06-12T10:00:00+00:00"


def test_first_seen_carries_across_scans_even_when_detail_changes():
    scan1 = _merge_flag_first_seen([_flag(detail="@3.4")], [],
                                   "2026-06-12T10:00:00+00:00")
    # next scan: same finding, repriced (different detail/severity)
    scan2 = _merge_flag_first_seen([_flag(detail="@3.6", severity=8.0)],
                                   scan1, "2026-06-12T10:30:00+00:00")
    assert scan2[0]["first_seen"] == "2026-06-12T10:00:00+00:00"


def test_first_seen_resets_for_different_outcome_or_event():
    scan1 = _merge_flag_first_seen([_flag(outcome="1/1")], [],
                                   "2026-06-12T10:00:00+00:00")
    scan2 = _merge_flag_first_seen(
        [_flag(outcome="2/2"), _flag(eid="E2")],
        scan1, "2026-06-12T10:30:00+00:00")
    assert all(f["first_seen"] == "2026-06-12T10:30:00+00:00" for f in scan2)
