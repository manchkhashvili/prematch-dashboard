"""Tests for src/consistency.py — CB-internal contradiction flags.

Key guarantees: a clean game and mild period-to-period variation produce NO
flags (low false-positive), while genuine contradictions across markets/periods
are caught.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.consistency import find_consistency_flags
from src.models import Odds
from src.scrapers.cb_detail import parse_detail_page
from src.scrapers.sports.basketball import classify_market_title_permissive

NOW = datetime(2026, 5, 31, tzinfo=timezone.utc)


def _o(mt, per, sel, line=None, sec=None, event="X"):
    return Odds(source="crystalbet", sport="basketball", home="H", away="A",
                market_type=mt, period=per, selections=sel, fetched_at=NOW,
                line=line, league="L", raw_event_id=event, section=sec or (mt + per))


def _total_ladder(per, center):
    return [_o("total", per, {"over": 1.95, "under": 1.85}, line=center - 0.5, sec="T" + per),
            _o("total", per, {"over": 1.85, "under": 1.95}, line=center + 0.5, sec="T" + per)]


# ── low false-positive guarantees ─────────────────────────────────────────────

def test_clean_captured_game_yields_no_flags():
    html = Path("data/raw/cb_single_match_detail.html").read_text()
    cb = parse_detail_page(html, event_id="E", home="HOME", away="AWAY", league="L",
                           start_time=NOW, fetched_at=NOW, sport_name="basketball",
                           classify=classify_market_title_permissive,
                           scope_to_event=False, per_section=True)
    assert find_consistency_flags(cb) == []


def test_mild_period_variation_does_not_flag():
    # the user's example: H1 1.6/2.0 next to FT 1.55/2.1 is normal, not weird.
    mild = [_o("moneyline", "H1", {"home": 1.6, "away": 2.0}),
            _o("moneyline", "FT", {"home": 1.55, "away": 2.1})]
    assert find_consistency_flags(mild) == []


# ── genuine contradictions are caught ─────────────────────────────────────────

def test_ml_vs_spread_disagreement_flagged():
    # Genuine contradiction read off a TRUE pick'em rung (line 0.0): the ML says
    # home ~83%, but the line-0 handicap devigs to ~61%.
    rows = [
        _o("moneyline", "FT", {"home": 1.20, "away": 4.50}),                 # ML home ~83%
        _o("spread", "FT", {"home": 1.55, "away": 2.45}, line=0.0, sec="Hc"),  # pick'em → ~61%
        _o("spread", "FT", {"home": 1.40, "away": 2.80}, line=-1.0, sec="Hc"),
    ]
    kinds = {f.kind for f in find_consistency_flags(rows)}
    assert "ml_vs_spread" in kinds


def test_no_pickem_rung_does_not_flag_ml_vs_spread():
    # The false-positive pattern that motivated the fix: a heavy favourite whose
    # handicap ladder doesn't reach line 0. We must NOT extrapolate/clamp and
    # fabricate a gap — with no pick'em rung the check simply doesn't run.
    rows = [
        _o("moneyline", "FT", {"home": 1.20, "away": 4.50}),       # ML home ~83%
        _o("spread", "FT", {"home": 1.55, "away": 2.45}, line=-3.0, sec="Hc"),
        _o("spread", "FT", {"home": 1.80, "away": 2.00}, line=-5.0, sec="Hc"),
    ]
    assert not any(f.kind == "ml_vs_spread" for f in find_consistency_flags(rows))


def test_total_additivity_flagged():
    rows = _total_ladder("H1", 110) + _total_ladder("H2", 110) + _total_ladder("FT", 240)
    flags = [f for f in find_consistency_flags(rows) if f.kind == "total_additivity"]
    assert flags and flags[0].severity >= 5.0


def test_favourite_flip_flagged():
    rows = [_o("moneyline", "FT", {"home": 1.50, "away": 2.60}),   # FT home fav
            _o("moneyline", "H1", {"home": 2.60, "away": 1.50})]   # H1 away fav
    assert any(f.kind == "favourite_flip" for f in find_consistency_flags(rows))


def test_quarter_more_extreme_than_ft_flagged():
    rows = [_o("moneyline", "FT", {"home": 1.90, "away": 1.90}),   # FT ~even
            _o("moneyline", "Q1", {"home": 1.30, "away": 3.40})]   # Q1 very lopsided
    assert any(f.kind == "quarter_ml_extreme" for f in find_consistency_flags(rows))


def test_flags_sorted_by_severity_desc():
    rows = (_total_ladder("H1", 110) + _total_ladder("H2", 110) + _total_ladder("FT", 240)
            + [_o("moneyline", "FT", {"home": 1.50, "away": 2.60}),
               _o("moneyline", "H1", {"home": 2.60, "away": 1.50})])
    sev = [f.severity for f in find_consistency_flags(rows)]
    assert sev == sorted(sev, reverse=True)
