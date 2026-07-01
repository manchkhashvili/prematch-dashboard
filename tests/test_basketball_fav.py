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
