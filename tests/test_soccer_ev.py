"""Tests for src.soccer_ev — own-book EV flagging off the goal model."""
import pytest

from src import soccer_ev


NEPEAN = {"ft_1x2:1": 8.90, "ft_1x2:X": 6.15, "ft_1x2:2": 1.18}


def test_generous_htft_flagged_as_positive_ev():
    posted = {**NEPEAN, "htft:2/2": 1.80}          # fair ~1.66 (power) → generous
    res = soccer_ev.analyze(posted, devig_method="power", split=0.45, ev_min=0.03)
    hit = [h for h in res.ev_hits if h.key == "htft:2/2"]
    assert hit and hit[0].ev == pytest.approx(0.082, abs=0.01)


def test_fair_priced_derivative_not_flagged():
    # price 2/2 exactly at model fair → EV ~0 → no flag.
    m = soccer_ev.analyze(NEPEAN, devig_method="power", split=0.45).model
    fair = m.odds("htft:2/2")
    res = soccer_ev.analyze({**NEPEAN, "htft:2/2": fair}, devig_method="power",
                            split=0.45, ev_min=0.03)
    assert not [h for h in res.ev_hits if h.key == "htft:2/2"]


def test_anchor_1x2_excluded_from_ev():
    res = soccer_ev.analyze(NEPEAN, devig_method="power", split=0.45, ev_min=0.0)
    assert not [h for h in res.ev_hits if h.key.startswith("ft_1x2:")]


def test_no_anchor_still_returns_identities():
    # HT/FT /2 column generous vs FT2, but no full 1X2 → model None, identities run.
    posted = {"ft_1x2:2": 1.18, "htft:1/2": 16.2, "htft:X/2": 4.85, "htft:2/2": 1.80}
    res = soccer_ev.analyze(posted)
    assert res.model is None
    assert any(f.kind == "partition" for f in res.identities)


def test_robustness_hint_present():
    posted = {**NEPEAN, "htft:2/2": 1.80}
    res = soccer_ev.analyze(posted, devig_method="power", split=0.45, ev_min=0.03)
    assert all(isinstance(h.robust, bool) for h in res.ev_hits)


def test_pathological_1x2_yields_no_nan_ev():
    # a degenerate 1X2 (draw a heavy favourite — a mislabelled market) must not
    # crash or produce NaN EV; the fit is bounded and the guard trips.
    import numpy as np
    posted = {"ft_1x2:1": 3.9, "ft_1x2:X": 1.05, "ft_1x2:2": 30.0,
              "htft:X/X": 3.0}
    res = soccer_ev.analyze(posted)
    for h in res.ev_hits:
        assert np.isfinite(h.ev) and np.isfinite(h.fair_odds)


def test_pretty_key_labels():
    assert soccer_ev.pretty_key("htft:2/2") == "HT/FT 2/2"
    assert soccer_ev.pretty_key("ft_total:over_2.5") == "over 2.5"
    assert soccer_ev.pretty_key("ah:home_-1") == "AH home -1"
    assert soccer_ev.pretty_key("dc:1X") == "double chance 1X"
