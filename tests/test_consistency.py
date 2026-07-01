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
    # 1/1 @ 2.70 vs product 1.60*1.50=2.40 — too generous even if legs were
    # independent (they're positively correlated, so it should be SHORTER).
    # Priced inside the bettable range so the odds gate doesn't suppress it.
    rows = _htft_fixture(combo_11=2.70, combo_22=3.35)
    flags = [f for f in find_consistency_flags(rows) if f.kind == "htft_combo"]
    assert flags and "1/1" in flags[0].detail and "generous" in flags[0].detail
    assert flags[0].outcome == "1/1"


def test_htft_small_violation_within_tolerance_not_flagged():
    # Correlation-fair for the 1/1 legs (1.60, 1.50) = 1.60×(1+(1.50-1)/2)=2.00;
    # 2.03 is ~1.5% over → under the 2% gate, no flag.
    rows = _htft_fixture(combo_11=2.03, combo_22=3.35)
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_htft_combo_correlation_fair_flags_river_plate():
    # River Plate shape: H1 home 1.35, FT home 1.14, 1/1 @ 1.55. The naive
    # product 1.35×1.14=1.539 → only 0.7% over (missed). The correlation fair
    # 1.35×(1+(1.14-1)/2)=1.444 → 1.55 is ~7% over → flagged.
    rows = _htft_fixture(combo_11=1.55, combo_22=3.35,
                         h1=(1.35, 17.1, 3.05), ft=(1.14, 18.3, 5.35))
    flags = [f for f in find_consistency_flags(rows)
             if f.kind == "htft_combo" and f.outcome == "1/1"]
    assert flags and "generous" in flags[0].detail and flags[0].severity >= 5


def test_htft_odds_range_gate_suppresses_out_of_range_flags():
    # 2/2 @ 8.00 violates the product bound (6.10) but sits above the 4.5
    # bettable cap → suppressed. 1/1 @ 1.10 violates dominance (legs 1.60)
    # but sits below the 1.15 floor → suppressed.
    rows = _htft_fixture(combo_11=1.10, combo_22=8.00)
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_htft_fair_odds_range_gate():
    # An outcome priced above 4.5 never emits a fair-model flag, however
    # large the model disagreement.
    prices = _fair_9(scale=0.85)
    prices["1/1"] = 5.2   # way over model fair (~2.86) AND over the 4.5 cap
    flags = [f for f in find_consistency_flags(_even_game_rows(prices))
             if f.kind == "htft_fair" and "1/1" in f.detail]
    assert flags == []


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


# ── soccer (HT/FT) support — un-gated 2026-06-21 ──────────────────────────────
def _so(mt, per, sel, line=None, sec=None, event="RP"):
    return Odds(source="crystalbet", sport="soccer", home="River Plate", away="Boca",
                market_type=mt, period=per, selections=sel, fetched_at=NOW,
                line=line, league="Argentina", raw_event_id=event, section=sec or (mt + per))


def test_soccer_htft_combo_longer_than_product_flagged():
    # River Plate: FT 1X2 home 1.12, H1 1X2 home 1.35, HT/FT 1/1 @ 1.55.
    # 1.35 × 1.12 = 1.512; 1.55 is ~2.5% longer → too generous (correlation bound).
    rows = [
        _so("moneyline", "FT", {"home": 1.12, "draw": 8.0, "away": 15.0}),
        _so("moneyline", "H1", {"home": 1.35, "draw": 4.0, "away": 7.0}),
        _so("htft", "FT", {"1/1": 1.55, "2/2": 12.0}),
    ]
    combo = [f for f in find_consistency_flags(rows)
             if f.kind == "htft_combo" and f.outcome == "1/1"]
    assert combo and combo[0].sport == "soccer"


def test_ht_vs_ft_divergence_flagged():
    rows = [
        _so("moneyline", "FT", {"home": 1.12, "draw": 8.0, "away": 15.0}),   # P_home ~0.82
        _so("moneyline", "H1", {"home": 1.70, "draw": 2.9, "away": 5.0}),    # P_home ~0.52
    ]
    div = [f for f in find_consistency_flags(rows) if f.kind == "ht_vs_ft_divergence"]
    assert div and div[0].severity >= 18 and div[0].sport == "soccer"


def test_soccer_small_ht_ft_gap_not_flagged():
    rows = [
        _so("moneyline", "FT", {"home": 1.9, "draw": 3.3, "away": 4.0}),
        _so("moneyline", "H1", {"home": 2.2, "draw": 2.9, "away": 3.6}),
    ]
    div = [f for f in find_consistency_flags(rows) if f.kind == "ht_vs_ft_divergence"]
    assert not div


def test_basketball_ht_ft_divergence_river_plate():
    # River Plate: FT 2-way 1.12/4.35 (~80% home), HT 2-way 1.35/2.55 (~65%) → ~14pp.
    rows = [
        _o("moneyline", "FT", {"home": 1.12, "away": 4.35}),
        _o("moneyline", "H1", {"home": 1.35, "away": 2.55}),
    ]
    div = [f for f in find_consistency_flags(rows) if f.kind == "ht_vs_ft_divergence"]
    assert div and 12 <= div[0].severity <= 16
