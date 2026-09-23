"""Volleyball — an LSport-only sport on the New inconsistencies cycle (2026-09-21).

A best-of-5 match is over-determined the way tennis is: the correct score, the
sets handicap, the total sets, the set winners and the match winner are all
functions of the same six outcomes. These tests pin:

  * the detail-page classifier on LSport's vocabulary (the feed 75 % of the
    board comes from), with the sets markets on their own submarket so a
    -1.5 SETS rung never shares a ladder with a -11.5 POINTS rung;
  * the best-of-5 latent-strength model: partition sums to 1, reduces to the
    IID binomial at zero variance, and reproduces the measured board shape
    (sweeps above IID, five-setters below);
  * the model-free identities across the sets markets on real boards:
    the Boca v Vélez duplicate, the USA v Canada dominance, and the two
    guards that keep noise out (integer lines push; a 1.01-pinned rung is
    not a price);
  * total_additivity NOT firing on set-periods, which it did on every game
    with per-set totals before the guard;
  * the wiring: sport registry, parse pool, the LSport cycle's sport list,
    tab labels.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import comb
from pathlib import Path

import pytest

from src import consistency as C
from src.models import Odds
from src.scrapers import cb_detail
from src.scrapers.sports import volleyball as VB

NOW = datetime(2026, 9, 21, 19, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


# ── classifier ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,mt,per,sub,side", [
    ("Main result", "moneyline", "FT", None, None),
    ("Winner", "moneyline", "FT", None, None),
    ("1st Period Winner", "moneyline", "H1", None, None),
    ("2nd Period Winner", "moneyline", "H2", None, None),
    ("1st Set - Winner", "moneyline", "H1", None, None),
    ("Correct Score", "correct_score", "FT", None, None),
    ("Asian Handicap Sets", "spread", "FT", "sets", None),
    ("Set Handicap", "spread", "FT", "sets", None),
    ("Total sets", "total", "FT", "sets", None),
    ("total points", "total", "FT", None, None),
    ("Point handicap", "spread", "FT", None, None),
    ("Total hometeam", "team_total", "FT", None, "home"),
    ("Away Team", "team_total", "FT", None, "away"),
    ("1 Set - Total Points", "total", "H1", None, None),
    ("1 set - Point Handicap", "spread", "H1", None, None),
    ("1st Period - Home Team", "team_total", "H1", None, "home"),
    ("1st Period - Away Team", "team_total", "H1", None, "away"),
    ("2 set - total points", "total", "H2", None, None),
    ("2 set - point handicap", "spread", "H2", None, None),
    ("2nd Period - Away Team", "team_total", "H2", None, "away"),
])
def test_lsport_titles_classify(title, mt, per, sub, side):
    c = VB.classify_market_title(title)
    assert c is not None, title
    assert (c.market_type, c.period, c.submarket, c.team_side) == (mt, per, sub, side)


@pytest.mark.parametrize("title", [
    "3rd Period Winner", "3 set - point handicap", "5th Set - Winner*",
    "1st Period Odd/Even", "odd/even", "1st Period Race To 3.0",
    "Correct Score 1st Period", "Exact sets", "Will There Be a 4th Set",
    "Home Team To Win a Set", "Home Team To Win Exactly 1 Set", "",
])
def test_projections_and_later_sets_stay_unclassified(title):
    assert VB.classify_market_title(title) is None


def test_permissive_is_the_same_classifier():
    assert VB.classify_market_title_permissive is VB.classify_market_title


# ── a detail page, parsed end to end ─────────────────────────────────────────

def _cell(lab, o):
    return (f'<div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">{lab}</div>'
            f'<div class="sport_more_bt2">{o}</div></div>')


def _page(markets: dict[str, list[tuple[str, str]]], eid="1") -> str:
    rows = "".join(
        f'<tr><td class="sport_more_td1">{t}</td><td class="sport_more_td2">'
        f'<div class="sport_more_td_div">{"".join(_cell(l, o) for l, o in sels)}</div></td></tr>'
        for t, sels in markets.items())
    return f'<div class="GContainerList" data-id="{eid}"><table class="game-details">{rows}</table></div>'


# Boca Juniors v Vélez Sarsfield, Copa Metropolitana, as posted 2026-09-21.
BOCA = {
    "Main result": [("1", "2.65"), ("2", "1.45")],
    "1st Period Winner": [("1", "2.25"), ("2", "1.60")],
    "Correct Score": [("3:0", "6.65"), ("0:3", "3.00"), ("3:1", "6.35"),
                      ("1:3", "3.60"), ("3:2", "5.90"), ("2:3", "4.20")],
    "Asian Handicap Sets": [("1(+2.5)", "1.25"), ("2(-2.5)", "3.65"),
                            ("1(+1.5)", "1.60"), ("2(-1.5)", "2.20")],
    "Total sets": [("Under 3.5", "2.60"), ("Over 3.5", "1.45"),
                   ("Under 4.5", "1.32"), ("Over 4.5", "3.10")],
    "Point handicap": [("1(+9.5)", "1.80"), ("2(-9.5)", "1.90"),
                       ("1(+11.5)", "1.65"), ("2(-11.5)", "2.10")],
    "total points": [("Under 176.5", "1.85"), ("Over 176.5", "1.85")],
    "1 Set - Total Points": [("Under 44.5", "1.80"), ("Over 44.5", "1.90")],
}


def _parse(markets, eid="1", home="Boca Juniors", away="Velez Sarsfield"):
    return cb_detail.parse_detail_page(
        _page(markets, eid), event_id=eid, home=home, away=away,
        league="Argentina, Copa Metropolitana", start_time=NOW, fetched_at=NOW,
        sport_name="volleyball", classify=VB.classify_market_title,
        scope_to_event=True, per_section=True)


def test_boca_page_parses_with_sets_on_their_own_submarket():
    odds = _parse(BOCA)
    keys = {(o.market_type, o.period, o.submarket, o.line) for o in odds}
    assert ("moneyline", "FT", None, None) in keys
    assert ("moneyline", "H1", None, None) in keys
    assert ("correct_score", "FT", None, None) in keys
    assert ("spread", "FT", "sets", 2.5) in keys and ("spread", "FT", "sets", 1.5) in keys
    assert ("total", "FT", "sets", 3.5) in keys and ("total", "FT", "sets", 4.5) in keys
    assert ("spread", "FT", None, 9.5) in keys       # points, no submarket
    assert ("total", "FT", None, 176.5) in keys
    assert ("total", "H1", None, 44.5) in keys
    cs = next(o for o in odds if o.market_type == "correct_score")
    assert set(cs.selections) == {"3-0", "3-1", "3-2", "2-3", "1-3", "0-3"}
    assert cs.selections["0-3"] == 3.0


# ── the best-of-5 model ──────────────────────────────────────────────────────

def _iid5(p):
    q = 1 - p
    return {f"3-{k}": comb(2 + k, k) * p ** 3 * q ** k for k in range(3)} | \
           {f"{k}-3": comb(2 + k, k) * q ** 3 * p ** k for k in range(3)}


@pytest.mark.parametrize("p_away", [0.2, 0.36, 0.5, 0.63, 0.9])
def test_partition_sums_to_one_and_reproduces_the_match_price(p_away):
    fair = C._cs5_fair_from_match(p_away)
    assert abs(sum(fair.values()) - 1.0) < 1e-9
    assert abs(fair["0-3"] + fair["1-3"] + fair["2-3"] - p_away) < 1e-6


def test_zero_variance_is_the_iid_binomial():
    p_away = 0.63
    fair = C._cs5_fair_from_match(p_away, sigma2=1e-6)
    p_home = 1.0 - C._bo5_set_prob_from_match(p_away)    # _iid5 is stated on HOME
    iid = _iid5(p_home)
    for k in fair:
        assert abs(fair[k] - iid[k]) < 2e-3, k


def test_correlation_fattens_the_sweeps():
    """The measured board shape: sweeps above IID, five-setters below."""
    p_away = 0.36
    fair = C._cs5_fair_from_match(p_away)                  # sigma2 = 0.032
    iid = C._cs5_fair_from_match(p_away, sigma2=1e-6)
    assert fair["3-0"] > iid["3-0"] and fair["0-3"] > iid["0-3"]
    assert fair["3-2"] < iid["3-2"] and fair["2-3"] < iid["2-3"]


def test_mirror_below_half():
    a = C._cs5_fair_from_match(0.3)
    b = C._cs5_fair_from_match(0.7)
    assert abs(a["3-0"] - b["0-3"]) < 1e-9 and abs(a["3-2"] - b["2-3"]) < 1e-9


def test_bo5_inversion_roundtrips():
    for p in (0.3, 0.5, 0.66, 0.9):
        assert abs(C._bo5_match_prob_from_set(C._bo5_set_prob_from_match(
            C._bo5_match_prob_from_set(p))) - C._bo5_match_prob_from_set(p)) < 1e-9


# ── the checks, on real boards ───────────────────────────────────────────────

def _flags(markets, **kw):
    return C.find_consistency_flags(_parse(markets, **kw))


def test_a_duplicate_whose_long_side_is_under_fair_is_not_a_row():
    """Boca as posted: CS 0-3 @3.00 vs sets away -2.5 @3.65, 22 % apart — and
    the longer price still reads -6 % against the fair from the match price.
    The first cut put this on top of the tab at severity 21.7."""
    assert not [f for f in _flags(BOCA) if f.kind.startswith("vb_sets_")]


def test_a_duplicate_fires_when_the_long_side_clears_fair():
    m = dict(BOCA)
    m["Asian Handicap Sets"] = [("1(+2.5)", "1.20"), ("2(-2.5)", "4.30")]
    flags = [f for f in _flags(m) if f.kind == "vb_sets_duplicate"]
    assert len(flags) == 1
    f = flags[0]
    assert f.outcome == "sets away -2.5" and f.odds == 4.3
    # severity is the EDGE against fair (the engine's own devig), not the 43 % gap
    assert C.VB_SETS_MIN_EV * 100 <= f.severity < 30
    assert "against a fair" in f.detail and "43% apart" in f.detail and "CS 0-3" in f.detail


def test_correct_score_check_does_not_fire_on_a_derived_board():
    """Every Boca leg sits 15-30 % under the model fair — the ordinary board."""
    assert not [f for f in _flags(BOCA) if f.kind == "vb_correct_score"]


def test_total_additivity_never_fires_on_set_periods():
    """Set 1 + set 2 points vs match points read as 'off by 91 pts' before."""
    m = dict(BOCA)
    m["2 set - total points"] = [("Under 45.5", "1.85"), ("Over 45.5", "1.85")]
    assert not [f for f in _flags(m) if f.kind == "total_additivity"]


def test_usa_canada_is_outside_the_calibrated_range():
    """CS 3-0 @1.08 shorter than 'under 3.5 sets' @1.16 is a real dominance
    violation — but the favourite is 88 % and the model was fitted on 31-63 %
    away boards, where it prices 3-0 at 1.62 here. No model row outside the
    range (VB_CS_MAX_FAV); the exact covers would still fire."""
    m = {
        "Main result": [("1", "1.05"), ("2", "8.00")],
        "Correct Score": [("3:0", "1.08"), ("0:3", "31.0"), ("3:1", "8.50"),
                          ("1:3", "26.0"), ("3:2", "17.0"), ("2:3", "24.0")],
        "Total sets": [("Under 3.5", "1.16"), ("Over 3.5", "4.80")],
    }
    flags = _flags(m, home="USA W", away="Canada W")
    assert not [f for f in flags if f.kind in ("vb_sets_dominance", "vb_sets_duplicate", "vb_correct_score")]


def test_dominance_fires_when_the_superset_clears_fair():
    """P(away) 0.27: the model prices 'under 3.5 sets' at 2.36. Posted 2.60
    (+10 %), while CS 3-0 — a subset of it — is 2.20: shorter than the event
    that contains it."""
    m = {
        "Main result": [("1", "1.30"), ("2", "3.50")],
        "Correct Score": [("3:0", "2.20"), ("0:3", "10.0"), ("3:1", "3.40"),
                          ("1:3", "8.00"), ("3:2", "5.50"), ("2:3", "8.00")],
        "Total sets": [("Under 3.5", "2.60"), ("Over 3.5", "1.50")],
    }
    flags = [f for f in _flags(m) if f.kind == "vb_sets_dominance"]
    assert len(flags) == 1
    f = flags[0]
    assert f.outcome == "sets under 3.5" and f.odds == 2.6
    assert C.VB_SETS_MIN_EV * 100 <= f.severity < 20      # the edge, engine devig
    assert "CS 3-0" in f.detail and "against a fair" in f.detail and "superset" in f.detail
    assert not [f for f in _flags(m) if f.kind == "vb_correct_score"], "no cell beats fair"


def test_integer_lines_push_and_never_take_part():
    """'over 4' sets vs 'over 4.5' read as a 46 % duplicate until excluded —
    exercised on the pure function so the EV gate cannot mask it."""
    cs = {k: v for k, v in [("3-0", 6.65), ("3-1", 6.35), ("3-2", 5.9), ("2-3", 4.2), ("1-3", 3.6), ("0-3", 3.0)]}
    fair = C._cs5_fair_from_match(0.63)
    rows = C._vb_sets_identities(cs, {"home": 2.65, "away": 1.45}, [],
                                 [(4.0, {"under": 1.85, "over": 9.00}), (4.5, {"under": 1.29, "over": 2.55})],
                                 fair=fair)
    assert not [r for r in rows if "over 4 " in r[2] or "under 4 " in r[2]]


def test_a_pinned_rung_is_not_a_price():
    """1(+2.5) @1.01 / 2(-2.5) @8.05 against CS 0:3 @32.6 is the floor artifact,
    not a 305 % duplicate."""
    m = {
        "Main result": [("1", "1.15"), ("2", "4.00")],
        "Correct Score": [("3:0", "2.50"), ("0:3", "32.6"), ("3:1", "2.60"),
                          ("1:3", "13.5"), ("3:2", "4.45"), ("2:3", "7.30")],
        "Asian Handicap Sets": [("1(+2.5)", "1.01"), ("2(-2.5)", "8.05"),
                                ("1(-2.5)", "2.55"), ("2(+2.5)", "1.35")],
    }
    flags = _flags(m, home="Slovenia", away="Serbia")
    assert not [f for f in flags if f.kind.startswith("vb_sets_")]


def test_set_match_hard_rule_in_best_of_five():
    m = {
        "Main result": [("1", "1.70"), ("2", "2.10")],          # home 55 %
        "1st Period Winner": [("1", "1.25"), ("2", "3.90")],   # home 75 % for set 1
    }
    flags = [f for f in _flags(m) if f.kind == "vb_set_match"]
    assert len(flags) == 1 and "impossible in a best-of-5" in flags[0].detail


def test_a_cover_is_reported_as_a_lock():
    m = {
        "Main result": [("1", "1.15"), ("2", "4.00")],
        "Correct Score": [("3:0", "2.20"), ("0:3", "30.0"), ("3:1", "2.60"),
                          ("1:3", "13.5"), ("3:2", "4.45"), ("2:3", "7.30")],
        # away +2.5 = everything but 3-0, priced far too long against CS 3-0 @2.20
        "Asian Handicap Sets": [("1(-2.5)", "2.10"), ("2(+2.5)", "2.20")],
    }
    flags = [f for f in _flags(m) if f.kind == "vb_sets_cover"]
    assert len(flags) == 1
    assert flags[0].severity > 0 and "locked" in flags[0].detail


# ── wiring ───────────────────────────────────────────────────────────────────

def test_registered_everywhere_the_cycle_looks():
    from src.scrapers.crystalbet import _SPORT_MODULES
    from src.scrapers import cb_parse_pool
    assert _SPORT_MODULES["volleyball"] is VB
    assert cb_parse_pool._classifier("volleyball", "permissive") is VB.classify_market_title
    assert "volleyball" in C.CONSISTENCY_SPORTS and "volleyball" in C.SETS_AS_PERIODS
    import src.app as app
    assert "volleyball" in app._lsport_sports()
    assert "volleyball" not in app.SPORT_NAMES, "LSport-only: no price poll, no Pinnacle"


def test_tab_labels_cover_every_volleyball_kind():
    kinds = {"vb_set_match", "vb_correct_score", "vb_sets_duplicate",
             "vb_sets_dominance", "vb_sets_cover"}
    for page in ("static/anomalies.html", "static/new_inconsistencies.html"):
        t = (ROOT / page).read_text(encoding="utf-8")
        for k in kinds:
            assert f"  {k}:" in t, (page, k)


def test_list_view_parses_like_basketball():
    from src.scrapers.crystalbet import _extract_games_from_list_html
    html = ('<html><body><div class="game-table"><div class="x_loop_title_block">21.09.2026</div>'
            '<div class="GContainerList" data-id="7"><div class="game-row" data-game-code="20264337">'
            '<div class="game_hint"><label>Belarus, League Pro</label></div>'
            '<div class="teams_name">Molot-Pro - Vityaz-Pro</div><span class="time">07:30</span></div>'
            '<div class="game_loading" data-loadinfo=\'[{"name":"1","bet":"1.80","handicap":""},'
            '{"name":" 2","bet":"2.05","handicap":""}]\'></div></div></div></body></html>')
    games = _extract_games_from_list_html(html, NOW, sport=VB)
    assert len(games) == 1 and games[0].provider == "lsport"
    o = games[0].list_odds[0]
    assert (o.sport, o.market_type, o.selections) == ("volleyball", "moneyline", {"home": 1.8, "away": 2.05})
