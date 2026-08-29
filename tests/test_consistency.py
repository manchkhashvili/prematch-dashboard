"""Tests for src/consistency.py — CB-internal contradiction flags.

Key guarantees: a clean game and mild period-to-period variation produce NO
flags (low false-positive), while genuine contradictions across markets/periods
are caught.
"""
from __future__ import annotations

import pytest

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
    # Both ends of the bettable range. 1/1 @ 1.10 violates dominance (legs
    # 1.60) but sits below the 1.15 floor; 2/2 @ 40.0 violates the product
    # bound but sits above the cap.
    #
    # The cap was 4.5 and this used 8.00 until 2026-08-29. It is 15.0 now,
    # because the correlation-fair model's p10-p90 dispersion is flat at
    # ~10-11 % all the way to 15 and only widens past it (19.2) — 8.00 is
    # inside the range where the model still holds, so suppressing it was
    # hiding findings rather than filtering noise. What the check is protecting
    # is unchanged: a price outside the range never fires.
    rows = _htft_fixture(combo_11=1.10, combo_22=40.0)
    assert not any(f.kind == "htft_combo" for f in find_consistency_flags(rows))


def test_htft_gate_admits_a_long_but_modellable_price():
    # The other half of the same invariant, and the case that forced the cap
    # up: Lech Poznan Uam II v Staszkowka posted 1/1 @5.90 against a
    # correlation-fair 5.25 — 12.4 % too generous, and invisible at cap 4.5.
    rows = _htft_fixture(combo_11=5.90, combo_22=2.80)
    flags = [f for f in find_consistency_flags(rows) if f.kind == "htft_combo"]
    assert flags, "a 5.90 combo is inside the model's reliable range"


def test_htft_fair_odds_range_gate():
    # An outcome priced outside the bettable range never emits a fair-model
    # flag, however large the model disagreement. Was 5.2 against a 4.5 cap;
    # 5.2 is inside the range now, so this uses a price outside the 15.0 one.
    prices = _fair_9(scale=0.85)
    prices["1/1"] = 40.0   # way over model fair AND outside the range
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


def test_ht_vs_ft_divergence_is_gone():
    """ht_vs_ft_divergence was removed 2026-08-03 — a big HT→FT win-prob gap is
    what a goal model requires (half time carries far more draw mass), not a
    contradiction. These are the exact shapes it used to flag: an ordinary heavy
    favourite. Nothing may fire on them again."""
    ordinary_favourites = [
        [_so("moneyline", "FT", {"home": 1.12, "draw": 8.0, "away": 15.0}),   # ~82% FT
         _so("moneyline", "H1", {"home": 1.70, "draw": 2.9, "away": 5.0})],   # ~52% HT
        [_o("moneyline", "FT", {"home": 1.12, "away": 4.35}),                 # ~80% FT
         _o("moneyline", "H1", {"home": 1.35, "away": 2.55})],                # ~65% HT
    ]
    for rows in ordinary_favourites:
        kinds = {f.kind for f in find_consistency_flags(rows)}
        assert "ht_vs_ft_divergence" not in kinds


# ── pick'em vs a 3-way moneyline (CrystalBet, 2026-08-23) ───────────────────
def _cb_lomza(ml=(3.35, 5.45, 1.55), pick=(5.25, 1.08)):
    """Lomza II v Ruch Wysokie Mazo. as CrystalBet posted it."""
    now = datetime.now(tz=timezone.utc)

    def o(mt, sel, line=None, period="FT"):
        return Odds(source="crystalbet", sport="soccer", home="Lomza II",
                    away="Ruch Wysokie", market_type=mt, period=period,
                    selections=sel, fetched_at=now, line=line,
                    league="Liga 2", raw_event_id="LOMZA")
    return [o("moneyline", {"home": ml[0], "draw": ml[1], "away": ml[2]}),
            o("spread", {"home": pick[0], "away": pick[1]}, line=0.0)]


def test_pickem_gap_is_measured_on_the_no_draw_basis():
    """A pick'em VOIDS the draw, so it must be compared to P(home | no draw),
    not to the raw 3-way P(home). Comparing the wrong basis understates the gap
    (12.6pp instead of 17.0pp here) and can flag coherent books."""
    flags = [f for f in find_consistency_flags(_cb_lomza()) if f.kind == "ml_vs_spread"]
    assert len(flags) == 1
    assert flags[0].severity == pytest.approx(17.0, abs=0.5)
    assert "no-draw basis" in flags[0].detail


def test_three_way_moneyline_no_longer_skips_the_check():
    """The regression this fixes: ml_phome is only filled from a 2-way ML, so
    soccer's 3-way main result left it None and the comparison never ran."""
    from src.consistency import _build_period_view
    rows = _cb_lomza()
    view = _build_period_view({"moneyline": [rows[0]], "spread": [rows[1]]})
    assert view.ml_phome is None, "soccer has no 2-way ML — this was the blind spot"
    assert view.ml_phome3 is not None
    assert view.ml_pwin_nodraw is not None
    assert view.ml_pwin_nodraw > view.ml_phome3, "removing the draw must raise P(home)"


def test_pickem_lock_accounts_for_the_push():
    """Ignoring the push reads 1/5.25 + 1/5.45 + 1/1.55 = 1.0191 (no arb). The
    pick'em stake comes BACK on a draw, so the draw leg only has to cover
    1 - x, and the true outlay is 0.9842 = +1.61%."""
    flags = [f for f in find_consistency_flags(_cb_lomza()) if f.kind == "pickem_arb"]
    assert len(flags) == 1
    f = flags[0]
    assert f.severity == pytest.approx(1.61, abs=0.05)
    assert "0.9842" in f.detail and "1.0191" in f.detail
    assert f.outcome == "pickem_home"


def test_pickem_lock_pays_the_same_on_all_three_outcomes():
    """Re-derive the position from the stakes the flag prints."""
    P, D, A = 5.25, 5.45, 1.55
    x, z = 1.0 / P, 1.0 / A
    y = (1.0 - x) / D
    outlay = x + y + z
    assert P * x == pytest.approx(1.0)
    assert x + D * y == pytest.approx(1.0)      # draw: stake returned + draw win
    assert A * z == pytest.approx(1.0)
    assert outlay < 1.0


def test_no_pickem_lock_on_a_coherent_book():
    flags = find_consistency_flags(_cb_lomza(ml=(2.10, 3.40, 3.60), pick=(1.62, 2.30)))
    assert not [f for f in flags if f.kind == "pickem_arb"]
    assert not [f for f in flags if f.kind == "ml_vs_spread"]


def test_pickem_lock_needs_a_real_pickem_rung():
    """A -0.5 / +0.5 rung has no push, so its semantics differ and it must not
    be read as a pick'em."""
    rows = _cb_lomza()
    rows[1].line = -0.5
    assert not [f for f in find_consistency_flags(rows) if f.kind == "pickem_arb"]


def test_pickem_lock_refuses_legs_from_different_fetches():
    """Replaying this over the tick store produced 21 locks, every one of which
    paired an ML and a pick'em captured days apart (median 1.6 days). Two prices
    from different moments are not a position you can take."""
    from datetime import timedelta
    rows = _cb_lomza()
    assert [f for f in find_consistency_flags(rows) if f.kind == "pickem_arb"]
    rows[1].fetched_at = rows[0].fetched_at - timedelta(hours=6)
    assert not [f for f in find_consistency_flags(rows) if f.kind == "pickem_arb"]


def test_pickem_lock_tolerates_a_few_seconds_of_skew():
    """Within one poll cycle the two rows are stamped moments apart."""
    from datetime import timedelta
    rows = _cb_lomza()
    rows[1].fetched_at = rows[0].fetched_at - timedelta(seconds=20)
    assert [f for f in find_consistency_flags(rows) if f.kind == "pickem_arb"]
