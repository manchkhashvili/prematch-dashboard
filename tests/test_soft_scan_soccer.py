"""Tests for the Family-E soccer wiring inside src.soft_scan (the game gate,
posted-dict builders, and the engine-flag shaping)."""
from src import soft_scan as ss


def test_soccer_gate_opens_below_1_5_skips_top_league():
    # a 1.45 favourite in a normal league → open (looser than the 1.30 htft gate)
    assert ss._open("soccer", 1.45, 2.9, "Argentina Primera")
    # top league never opens
    assert not ss._open("soccer", 1.10, 8.0, "UEFA Champions League")
    # nobody under 1.5 → closed
    assert not ss._open("soccer", 1.8, 2.1, "Argentina Primera")
    # one side priced, the other missing = extreme favourite → open
    assert ss._open("soccer", None, 1.6, "Argentina Primera")


def test_basketball_gate_unchanged():
    # basketball still uses the 1.30 htft.should_open threshold
    assert ss._open("basketball", 1.25, 4.0, "NBA G League") == \
        ss.hf.should_open(1.25, 4.0, "NBA G League")


def test_posted_soccer_builds_model_keys():
    posted = ss._posted_soccer(
        {"home": 8.90, "draw": 6.15, "away": 1.18},
        {"home": 3.0, "away": 1.55},
        {"1/1": 11.0, "2/2": 1.80, "X/2": 4.85},
    )
    assert posted["ft_1x2:2"] == 1.18
    assert posted["htft:2/2"] == 1.80
    assert posted["ht_1x2:2"] == 1.55
    # junk odds (<=1) dropped
    assert all(v > 1 for v in posted.values())


def test_engine_flags_shape_ev_and_identity():
    posted = ss._posted_soccer(
        {"home": 8.90, "draw": 6.15, "away": 1.18},
        None,
        {"1/1": 11.0, "X/1": 21.0, "2/1": 150.0, "1/X": 9.0, "X/X": 7.0,
         "2/X": 4.5, "1/2": 16.2, "X/2": 4.85, "2/2": 1.80},
    )
    flags = ss._soccer_engine_flags("betlive", "Nepean", "Mounties",
                                    "Argentina Primera", "999", posted, None, None, None)
    kinds = {f["kind"] for f in flags}
    assert "soccer_fair" in kinds          # the generous 2/2 EV
    assert "soccer_identity" in kinds       # the /2-column partition
    ev = next(f for f in flags if f["kind"] == "soccer_fair")
    assert ev["book"] == "betlive" and ev["sport"] == "soccer"
    assert ev["detail"].startswith("[betlive] HT/FT 2/2")
    assert ev["severity"] > 3


def test_engine_no_flags_without_posted():
    assert ss._soccer_engine_flags("cb", "A", "B", "L", "1", {}, None, None, None) == []
