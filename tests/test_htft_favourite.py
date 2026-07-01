"""Tests for src.htft_favourite — the soft-book HT/FT favourite screen."""
from src import htft_favourite as hf


def test_favourite_side_by_odds():
    assert hf.favourite_side(1.12, 6.0) == "home"
    assert hf.favourite_side(4.0, 1.25) == "away"
    assert hf.favourite_side(1.9, 2.1) is None          # no heavy favourite


def test_favourite_side_missing_odds_is_extreme_favourite():
    assert hf.favourite_side(None, 8.0) == "home"       # home price omitted = way fav
    assert hf.favourite_side(9.0, None) == "away"
    assert hf.favourite_side(None, None) is None         # can't tell


def test_flag_real_case_h1_1_1_htft_1_4():
    # Owner's real case: H1 home 1.1, HT/FT 1/1 @ 1.4 → 1.27× ≥ 1.2 → flag.
    got = hf.htft_flag("home", h1_home=1.1, h1_away=6.0, htft_11=1.4, htft_22=30.0)
    assert got and got[0] == "home" and got[3] == 1.27


def test_flag_away_side():
    got = hf.htft_flag("away", h1_home=6.0, h1_away=1.15, htft_11=30.0, htft_22=1.5)
    assert got and got[0] == "away" and round(got[3], 2) == 1.30


def test_tight_combo_not_flagged():
    # Typical: HTFT ≈ H1 (ratio ~1.0) → no flag.
    assert hf.htft_flag("home", 1.55, 6.0, 1.60, 30.0) is None   # 1.03×
    assert hf.htft_flag("home", 1.66, 6.0, 1.70, 30.0) is None   # 1.02×


def test_should_open_gate():
    assert hf.should_open(1.12, 6.0, "Brazil, Serie B") is True
    assert hf.should_open(None, 8.0, "Chile, Copa") is True         # missing-odds fav
    assert hf.should_open(1.12, 6.0, "World Cup 2026") is False     # top league
    assert hf.should_open(1.9, 2.1, "Brazil, Serie B") is False     # no favourite


def test_htft_vs_ml_flag():
    r = hf.htft_vs_ml_flag("home", 1.25, 6.0, 1.75, 30.0, ratio=1.35)   # 1.4x
    assert r and r[0] == "home" and r[3] == 1.4
    assert hf.htft_vs_ml_flag("home", 1.25, 6.0, 1.60, 30.0, ratio=1.35) is None  # 1.28x
