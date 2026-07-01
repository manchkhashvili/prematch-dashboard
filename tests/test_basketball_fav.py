"""Tests for src.basketball_fav — favourite disagreement across ML markets."""
from src.basketball_fav import fav_disagreement


def test_favourite_flips_between_2way_and_3way():
    # 2-way home favourite (p~0.60), 3-way home underdog (p~0.41) → flip.
    r = fav_disagreement(ml2=(1.6, 2.4), ml3=(2.3, 1.6))
    assert r and r["flip"] and r["gap_pp"] >= 15


def test_ht_flip_flagged():
    r = fav_disagreement(ml3=(1.6, 2.3), ht=(2.4, 1.55))   # FT home fav, HT away fav
    assert r and r["flip"]


def test_agreeing_markets_not_flagged():
    # all three favour home, small spread → clean.
    assert fav_disagreement(ml2=(1.6, 2.4), ml3=(1.7, 2.2), ht=(1.8, 2.0)) is None


def test_large_gap_without_flip_flagged():
    # both home-favoured but very different strength (0.86 vs 0.59) → 27pp.
    r = fav_disagreement(ml2=(1.1, 7.0), ml3=(1.6, 2.3))
    assert r and not r["flip"] and r["gap_pp"] >= 15


def test_needs_two_markets():
    assert fav_disagreement(ml2=(1.6, 2.4)) is None
    assert fav_disagreement() is None


def test_htft_winner_and_position_join():
    from src.basketball_fav import htft_winner, fav_disagreement
    htft = {"1/1": 2.0, "X/1": 10, "2/1": 20, "1/2": 30, "X/2": 15, "2/2": 3.0}
    h, a = htft_winner(htft)                    # p_home .65→1.54, p_away .433→2.31
    assert round(h, 2) == 1.54 and round(a, 2) == 2.31
    # the HT/FT-implied position joins the 2-way/3-way/HT comparison
    assert fav_disagreement(ml2=(1.5, 2.6), htft=(2.5, 1.5))["flip"]
