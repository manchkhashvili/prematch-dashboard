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


# ── htft_combo: HT/FT 1/1 & 2/2 vs their own legs ─────────────────────────────
# Legs are the REGULATION 3-way 1x2 moneylines (have a "draw" price).

def _htft_fixture(combo_11, combo_22, h1=(1.60, 15.0, 2.30), ft=(1.50, 15.2, 2.65)):
    return [
        _o("moneyline", "H1", {"home": h1[0], "draw": h1[1], "away": h1[2]}, sec="1st half - 1x2"),
        _o("moneyline", "FT", {"home": ft[0], "draw": ft[1], "away": ft[2]}, sec="Full Time Result(1X2)"),
        _o("htft", "FT", {"1/1": combo_11, "1/X": 26.0, "1/2": 7.40,
                          "X/1": 33.3, "X/X": 100.0, "X/2": 52.0,
                          "2/1": 5.30, "2/X": 27.8, "2/2": combo_22},
           sec="Halftime/Fulltime"),
    ]


def test_htft_healthy_combo_does_not_flag():
    # Live-captured shape (SAS/NYK 2026-06-12): 1/1=1.80 between max-leg 1.60
    # and product 1.60*1.50=2.40; 2/2=3.35 between 2.65 and 6.10.
    rows = _htft_fixture(combo_11=1.80, combo_22=3.35)
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_htft_combo_shorter_than_leg_flagged():
    # 1/1 @ 1.40 while the H1 home leg alone is 1.60 — logically impossible.
    rows = _htft_fixture(combo_11=1.40, combo_22=3.35)
    flags = [f for f in find_consistency_flags(rows) if f.kind == "htft_combo"]
    assert flags and "1/1" in flags[0].detail and "shorter" in flags[0].detail


def test_htft_combo_longer_than_independent_product_flagged():
    # 2/2 @ 8.00 vs product 2.30*2.65=6.10 — too generous even if legs were
    # independent (they're positively correlated, so it should be SHORTER).
    rows = _htft_fixture(combo_11=1.80, combo_22=8.00)
    flags = [f for f in find_consistency_flags(rows) if f.kind == "htft_combo"]
    assert flags and "2/2" in flags[0].detail and "generous" in flags[0].detail


def test_htft_small_violation_within_tolerance_not_flagged():
    # 1% past the product bound stays under the 2% gate (odds-step noise).
    rows = _htft_fixture(combo_11=1.80, combo_22=6.16)  # product = 6.0950
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_htft_missing_legs_skips_check():
    rows = [_o("htft", "FT", {"1/1": 1.40, "2/2": 3.35}, sec="Halftime/Fulltime"),
            _o("moneyline", "FT", {"home": 1.45, "away": 2.45})]  # 2-way, not a leg
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_3way_legs_do_not_corrupt_2way_ml_checks():
    # A 3-way 1x2 row next to the 2-way ML must not feed devig_2way(home, away)
    # — P(home) from 1.50/2.65 ignoring the draw would be wrong and could
    # fabricate favourite_flip / quarter_ml_extreme flags.
    rows = [_o("moneyline", "FT", {"home": 1.90, "away": 1.90}),
            _o("moneyline", "FT", {"home": 1.50, "draw": 15.2, "away": 2.65},
               sec="Full Time Result(1X2)"),
            _o("moneyline", "Q1", {"home": 1.85, "away": 1.95})]
    assert find_consistency_flags(rows) == []


# ── htft_fair: model-based HT/FT pricing (check 6) ────────────────────────────
# Even game fixture: spread ladder centered at 0 → mu=0; sigma defaults to 10
# (league "L" is unknown). Model fair 1/1 ≈ 2.86 at rho=0.70, 9-outcome.

def _even_game_rows(htft_prices):
    return [
        _o("spread", "FT", {"home": 1.95, "away": 1.85}, line=-1.0, sec="AH"),
        _o("spread", "FT", {"home": 1.85, "away": 1.95}, line=1.0, sec="AH"),
        _o("htft", "FT", htft_prices, sec="Halftime/Fulltime"),
    ]


def _fair_9(scale=1.0):
    from src.htft_model import htft_fair_probs
    fair = htft_fair_probs(0.0, sigma=10, rho=0.70, nine_outcome=True)
    return {k: round(scale / v, 2) for k, v in fair.items()}


def test_htft_fair_edge_flagged_when_posted_beats_model():
    prices = _fair_9(scale=0.85)          # typical vigged board, no edges...
    prices["1/1"] = round(1 / 0.30, 2)    # ...except 1/1 priced way too long
    flags = [f for f in find_consistency_flags(_even_game_rows(prices))
             if f.kind == "htft_fair"]
    assert flags, "expected an edge flag on the overpriced 1/1"
    assert "1/1" in flags[0].detail and "+EV" in flags[0].detail


def test_htft_fair_quiet_on_normally_vigged_board():
    # all outcomes at fair * 0.85 — vig present, shape consistent → no flags
    flags = [f for f in find_consistency_flags(_even_game_rows(_fair_9(0.85)))
             if f.kind == "htft_fair"]
    assert flags == []


def test_htft_fair_shape_flag_on_distorted_outcome():
    from src.htft_model import htft_fair_probs
    fair = htft_fair_probs(0.0, sigma=10, rho=0.70, nine_outcome=True)
    prices = {}
    for k, p in fair.items():
        # 2/2 carries twice its fair probability (shorter price), the rest
        # rebalanced longer — overall vig stays modest so only SHAPE is off.
        q = p * 2.0 if k == "2/2" else p * 0.92
        prices[k] = round(1 / q, 2)
    flags = [f for f in find_consistency_flags(_even_game_rows(prices))
             if f.kind == "htft_fair"]
    assert any("2/2" in f.detail and "shape" in f.detail for f in flags)


def test_htft_fair_ignores_longshot_outcomes():
    # X/X fair ~ hundreds — outside HTFT_FAIR_MAX_ODDS, never flagged even
    # when priced absurdly.
    prices = _fair_9(scale=0.85)
    prices["X/X"] = 13.0   # insanely short for a ~0.3% outcome
    flags = [f for f in find_consistency_flags(_even_game_rows(prices))
             if f.kind == "htft_fair" and "X/X" in f.detail]
    assert flags == []


def test_htft_fair_skipped_without_mu_source():
    rows = [_o("htft", "FT", _fair_9(0.85), sec="Halftime/Fulltime")]
    assert not any(f.kind == "htft_fair" for f in find_consistency_flags(rows))
