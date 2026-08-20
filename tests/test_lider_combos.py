"""Lider combo bounds: containment + min-cost cover.

Every case that fired during development is pinned here, including the three
things that went wrong while building it:

  * a "too rich" test that compared MARGIN-INFLATED raw probability against a
    ceiling and produced 280 phantom violations;
  * an identity check that silently produced ZERO rows because Lider offers
    'Draw & Under' at 2.5 and 'Draw & Over' at 1.5, so the lines never paired
    and a clean-looking empty result was actually a dead detector;
  * a cover search too narrow to see the real shape (it rated the live Iwata
    arb at +1.4% when the partition cover was worth +7.17%).

The live case at the bottom is the one that was actually on the board on
2026-08-19 and held unchanged for 11+ minutes across three full passes.
"""
import pytest

from src import lider_combos as LC


# ── the atom algebra ─────────────────────────────────────────────────────────
def test_total_masks_partition_the_space():
    """The six atoms are mutually exclusive and collectively exhaustive."""
    union = 0
    for r in ("1", "X", "2"):
        for s in ("u", "o"):
            m = LC.t_mask(r, s)
            assert union & m == 0, "atoms must be disjoint"
            union |= m
    assert union == LC.T_FULL


def test_double_chance_mask_is_the_union_of_its_members():
    assert LC.t_mask("1X", "o") == LC.t_mask("1", "o") | LC.t_mask("X", "o")
    assert LC.t_mask("X2", "u") == LC.t_mask("X", "u") | LC.t_mask("2", "u")
    assert LC.t_mask("12", "o") == LC.t_mask("1", "o") | LC.t_mask("2", "o")


def test_htft_rows_and_columns_partition_the_grid():
    assert LC.h_row("1") | LC.h_row("X") | LC.h_row("2") == LC.H_FULL
    assert LC.h_col("1") | LC.h_col("X") | LC.h_col("2") == LC.H_FULL
    # a row and a column meet in exactly one cell
    assert bin(LC.h_row("1") & LC.h_col("2")).count("1") == 1


def test_h1_or_ft_union_strictly_contains_the_h1_leg():
    """The market on the owner's bet slip: '1st Half Result or Match Result'."""
    union = LC.h_row("1") | LC.h_col("1")
    assert union & LC.h_row("1") == LC.h_row("1")     # contains it
    assert union != LC.h_row("1")                     # strictly


# ── containment ──────────────────────────────────────────────────────────────
def test_containment_flags_subset_priced_longer_than_superset():
    """The owner's slip: H1 '1' @ 2.95 vs 'H1-or-FT 1' @ 3.10. The union
    contains the H1 win, so it can never pay more."""
    bets = [(LC.h_row("1"), 2.95, "H1 1", True),
            (LC.h_row("1") | LC.h_col("1"), 3.10, "1 in H1 or in match", True)]
    hits = LC.containment(bets)
    assert len(hits) == 1
    la, oa, lb, ob, gain = hits[0]
    assert (la, oa) == ("H1 1", 2.95)
    assert (lb, ob) == ("1 in H1 or in match", 3.10)
    assert gain == pytest.approx(5.08, abs=0.02)


def test_containment_silent_when_prices_are_ordered_correctly():
    """Same pair after Lider repriced the union to 1.95 — subset now pays more,
    which is the only coherent ordering."""
    bets = [(LC.h_row("1"), 2.95, "H1 1", True),
            (LC.h_row("1") | LC.h_col("1"), 1.95, "1 in H1 or in match", True)]
    assert LC.containment(bets) == []


def test_containment_ignores_overlapping_but_non_nested_sets():
    """A row and a column overlap without either containing the other, so no
    ordering is implied and nothing may be flagged."""
    bets = [(LC.h_row("1"), 2.0, "H1 1", True), (LC.h_col("2"), 9.0, "FT 2", True)]
    assert LC.containment(bets) == []


def test_containment_equal_sets_never_flag():
    """'Team A win and score 2+' is the SAME set as the grid cell (A, Over 1.5),
    so the two prices are comparable but neither contains the other strictly."""
    m = LC.t_mask("2", "o")
    assert LC.containment([(m, 2.60, "2 win & score 2+", True),
                           (m, 2.40, "2&O1.5", True)]) == []


# ── min-cost cover ───────────────────────────────────────────────────────────
def test_cover_finds_the_three_leg_partition():
    """Iwata vs Tokushima, live 2026-08-19: buy both halves of the 'I Team Not
    lose' partition and hedge with the away win. Three disjoint outcomes."""
    bets = [
        (LC.t_mask("1X", "u"), 4.20, "1X&U1.5 Yes", True),
        (LC.t_mask("1X", "o"), 2.85, "1X&O1.5 Yes", True),
        (LC.t_mask("2", "u") | LC.t_mask("2", "o"), 2.85, "FT 2", True),
    ]
    cost, legs = LC.min_cover(bets, LC.T_FULL, 6)
    assert cost == pytest.approx(0.9398, abs=0.0005)
    assert (1 / cost - 1) * 100 == pytest.approx(6.40, abs=0.05)
    assert len(legs) == 3


def test_cover_prefers_the_cheaper_route_when_both_exist():
    """Adding an expensive direct route must not make the answer worse."""
    bets = [
        (LC.t_mask("1X", "u"), 4.20, "combo U", True),
        (LC.t_mask("1X", "o"), 2.85, "combo O", True),
        (LC.t_mask("1X", "u") | LC.t_mask("1X", "o"), 1.29, "DC 1X", True),
        (LC.t_mask("2", "u") | LC.t_mask("2", "o"), 2.85, "FT 2", True),
    ]
    cost, _ = LC.min_cover(bets, LC.T_FULL, 6)
    assert cost == pytest.approx(0.9398, abs=0.0005), "must still pick the combo pair"


def test_cover_returns_none_when_the_space_cannot_be_covered():
    bets = [(LC.t_mask("1", "u"), 3.0, "one atom only", True)]
    assert LC.min_cover(bets, LC.T_FULL, 6) is None


def test_cover_of_a_normal_book_is_above_one():
    """A coherent 1X2 plus a total cannot be arbed — the cover must cost more
    than 1, or the detector would fire on every healthy match."""
    bets = [
        (LC.t_mask("1", "u") | LC.t_mask("1", "o"), 2.25, "FT 1", True),
        (LC.t_mask("X", "u") | LC.t_mask("X", "o"), 2.95, "FT X", True),
        (LC.t_mask("2", "u") | LC.t_mask("2", "o"), 2.85, "FT 2", True),
    ]
    cost, _ = LC.min_cover(bets, LC.T_FULL, 6)
    assert cost > 1.0


def test_cover_never_understates_cost_when_legs_overlap():
    """Overlapping covers return MORE than 1 on the overlap, so summing 1/odds
    is an upper bound — the DP may miss a cheaper cover but must never invent
    an arb that is not there."""
    over = sum(1 << LC.TIX[(r, "o")] for r in ("1", "X", "2"))
    under = sum(1 << LC.TIX[(r, "u")] for r in ("1", "X", "2"))
    bets = [(over | LC.t_mask("1", "u"), 1.50, "wide A", True),
            (under, 2.50, "under", True)]
    cost, _ = LC.min_cover(bets, LC.T_FULL, 6)
    assert cost == pytest.approx(1 / 1.50 + 1 / 2.50, abs=1e-9)


# ── ladder direction: the shifted-rung rule ─────────────────────────────────
def _g(cells=None, tot=None, x12=None, dc=None):
    from collections import defaultdict
    g = {"x12": defaultdict(dict), "tot": defaultdict(lambda: defaultdict(dict)),
         "dc": dc or {}, "cells": defaultdict(list), "htft": [],
         "x12lab": defaultdict(dict),
         "totlab": defaultdict(lambda: defaultdict(dict)), "dclab": {}}
    for k, v in (x12 or {}).items():
        g["x12"][k] = v
    for per, rungs in (tot or {}).items():
        for line, sides in rungs.items():
            g["tot"][per][line] = sides
    for k, v in (cells or {}).items():
        g["cells"][k] = v
    return g


def test_under_rung_at_or_above_the_line_is_a_valid_hedge():
    """Under M covers the three Under atoms at line L whenever M >= L: goals < L
    implies goals < M. This is what makes 'Over 5.5 priced but 4.5 absent'
    usable instead of skipped."""
    g = _g(cells={("FT", 4.5): [(LC.t_mask("2", "o"), 2.60, "cell", True)]},
           tot={"FT": {5.5: {"u": 1.05, "o": 7.7}}})
    labels = [lab for _m, _v, lab, _e in LC.total_bets(g, "FT", 4.5)]
    assert any("Under 5.5" in l or "Under 5.5" in l for l in labels)
    assert not any("Over 5.5" in l for l in labels), "Over 5.5 cannot cover Over 4.5"


def test_over_rung_at_or_below_the_line_is_a_valid_hedge():
    g = _g(cells={("FT", 4.5): [(LC.t_mask("2", "u"), 2.60, "cell", True)]},
           tot={"FT": {3.5: {"u": 1.21, "o": 3.45}}})
    labels = [lab for _m, _v, lab, _e in LC.total_bets(g, "FT", 4.5)]
    assert any("Over 3.5" in l for l in labels)
    assert not any("Under 3.5" in l for l in labels)


def test_exact_rung_supplies_both_sides():
    g = _g(cells={("FT", 2.5): [(LC.t_mask("1", "o"), 3.0, "cell", True)]},
           tot={"FT": {2.5: {"u": 1.60, "o": 2.05}}})
    labels = [lab for _m, _v, lab, _e in LC.total_bets(g, "FT", 2.5)]
    assert any("Under 2.5" in l for l in labels)
    assert any("Over 2.5" in l for l in labels)


# ── dedup keeps the best price ──────────────────────────────────────────────
def test_dedup_keeps_the_longest_odds_for_an_identical_outcome_set():
    """'Team 2 win and score more than 1.5 goals' and the grid cell 'Over/2' at
    1.5 are the same event priced twice; the cover must use the better one."""
    m = LC.t_mask("2", "o")
    out = LC._dedup([(m, 2.40, "grid cell", True), (m, 2.60, "win & score 2+", True)])
    assert out == [(m, 2.60, "win & score 2+", True)]


# ── end-to-end on one match ─────────────────────────────────────────────────
def _iwata_payload():
    """The live Iwata vs Tokushima block, trimmed to what the engine reads."""
    def oc(oid, v):
        return {oid: {"value": v}}
    mts = {
        "mt:16:500": {"outcomeTypes": [{"id": "ot:16:2", "name": "1"},
                                       {"id": "ot:16:1", "name": "X"},
                                       {"id": "ot:16:3", "name": "2"}]},
        "mt:16:1716": {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                        {"id": "n", "name": "No"}]},
        "mt:16:1718": {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                        {"id": "n", "name": "No"}]},
    }
    match = {"markets": {
        "a": {"typeId": "mt:16:500", "specifier": None,
              "outcomes": {"ot:16:2": {"value": 2.25}, "ot:16:1": {"value": 2.95},
                           "ot:16:3": {"value": 2.85}}},
        "b": {"typeId": "mt:16:1716", "specifier": {"special": "1.5"},
              "outcomes": {**oc("y", 4.20), **oc("n", 1.15)}},
        "c": {"typeId": "mt:16:1718", "specifier": {"special": "1.5"},
              "outcomes": {**oc("y", 2.85), **oc("n", 1.33)}},
    }}
    return match, mts


def test_end_to_end_finds_the_live_arb():
    match, mts = _iwata_payload()
    g = LC.parse_match(match, mts)
    flags = LC.analyse_match(g, "Iwata", "Tokushima", "J2 League", "pr:m:4981473")
    covers = [f for f in flags if f["kind"] == "combo_cover"]
    assert len(covers) == 1
    f = covers[0]
    assert f["severity"] == pytest.approx(6.4, abs=0.1)
    assert f["book"] == "liderbet" and f["sport"] == "soccer"
    assert f["match_label"] == "Iwata — Tokushima"
    assert "locked" in f["detail"]


def test_end_to_end_is_silent_on_a_coherent_match():
    """Same shape, but with the combo pair priced consistently with the 1X2 —
    the detector must produce nothing."""
    match, mts = _iwata_payload()
    match["markets"]["b"]["outcomes"]["y"]["value"] = 3.20   # 1X&U1.5
    match["markets"]["c"]["outcomes"]["y"]["value"] = 2.20   # 1X&O1.5
    g = LC.parse_match(match, mts)
    assert LC.analyse_match(g, "Iwata", "Tokushima", None, "x") == []


def test_min_edge_threshold_is_respected():
    match, mts = _iwata_payload()
    g = LC.parse_match(match, mts)
    assert LC.analyse_match(g, "a", "b", None, "x", min_edge=50.0) == []
    assert LC.analyse_match(g, "a", "b", None, "x", min_edge=1.0)


def test_suspended_prices_are_ignored():
    """Lider writes <= 1.0 for a pulled selection; treating that as odds would
    invent a free leg and an infinite edge."""
    match, mts = _iwata_payload()
    match["markets"]["b"]["outcomes"]["y"]["value"] = 1.0
    g = LC.parse_match(match, mts)
    labels = [lab for _m, _v, lab, _e in LC.total_bets(g, "FT", 1.5)]
    assert not any("1X&U1.5 Yes" in l for l in labels)


def test_winscore_market_maps_onto_the_over_1_5_cell():
    """Given A wins, A scoring 2+ is the same event as the match going over 1.5
    (A=1 forces 1-0). It must land on the (2, Over) atom at line 1.5."""
    mts = {"mt:16:2680": {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                           {"id": "n", "name": "No"}]}}
    match = {"markets": {"a": {"typeId": "mt:16:2680", "specifier": None,
                               "outcomes": {"y": {"value": 2.60}}}}}
    g = LC.parse_match(match, mts)
    cells = g["cells"][("FT", 1.5)]
    assert len(cells) == 1
    assert cells[0][0] == LC.t_mask("2", "o")
    assert cells[0][1] == 2.60


def test_flag_shape_matches_the_consistency_tab_contract():
    """The Anomalies tab renders these rows generically, so the keys it reads
    must all be present or the table breaks at runtime rather than in a test."""
    match, mts = _iwata_payload()
    g = LC.parse_match(match, mts)
    f = LC.analyse_match(g, "Iwata", "Tokushima", "J2", "pr:m:1")[0]
    for key in ("book", "sport", "kind", "match_label", "home", "away", "league",
                "cb_event_id", "book_event_id", "start_time", "periods",
                "detail", "severity", "outcome"):
        assert key in f, f"consistency rows must carry {key}"
    assert isinstance(f["severity"], float)


def test_scan_is_registered_as_a_runtime_toggle():
    from src import runtime_config
    assert "lider_combo" in runtime_config.SCANS
    assert "lider_combo_sec" in runtime_config.CADENCES
    for k in ("lider_combo_hours", "lider_combo_min_edge", "lider_combo_min_dom"):
        assert k in runtime_config.LIMITS


def test_scan_defaults_to_off():
    """It pays for its own detail fetch, so it must never start by surprise."""
    from src import runtime_config
    assert runtime_config._defaults()["scans"]["lider_combo"] is False


# ── exactness: the bug the first live run exposed ───────────────────────────
def test_shifted_total_rung_is_a_cover_leg_but_not_a_containment_operand():
    """The first live sweep reported 98 dominance rows and the biggest were all
    "FT Over 2.5 is contained in <combo> No". They were artefacts: inside the
    4.5 space "Over 2.5" is given only the Over-4.5 atoms, yet it also wins on
    3 and 4 goals. Understating a COVER leg is safe; understating a CONTAINMENT
    operand invents subset relations. Only an exact rung may be compared."""
    g = _g(cells={("FT", 4.5): [(LC.t_mask("X2", "u"), 1.90, "X2&U4.5 Yes", True),
                                (LC.T_FULL & ~LC.t_mask("X2", "u"), 3.65,
                                 "X2&U4.5 No", True)]},
           tot={"FT": {2.5: {"u": 1.90, "o": 1.85}}})
    bets = LC.total_bets(g, "FT", 4.5)
    shifted = [b for b in bets if "Over 2.5" in b[2]]
    assert shifted and shifted[0][3] is False, "a shifted rung must not be exact"
    # it is still available to the cover search
    assert any("Over 2.5" in b[2] for b in bets)
    # ...and it must not produce a containment row
    assert not any("Over 2.5" in la or "Over 2.5" in lb
                   for la, _oa, lb, _ob, _g in LC.containment(bets))


def test_exact_rung_still_participates_in_containment():
    """The guard must not silence the real thing: at M == L the rung IS its own
    winning set, so it compares normally."""
    over = sum(1 << LC.TIX[(r, "o")] for r in ("1", "X", "2"))
    g = _g(cells={("FT", 2.5): [(LC.t_mask("1", "o"), 9.0, "1&O2.5", True)]},
           tot={"FT": {2.5: {"u": 1.60, "o": 2.05}}})
    bets = LC.total_bets(g, "FT", 2.5)
    ex = {b[2]: b for b in bets}
    assert ex["FT Over 2.5"][3] is True
    assert ex["FT Over 2.5"][0] == over
    # 1&O2.5 (one atom) sits inside FT Over 2.5 (three atoms) and pays LESS at
    # 9.0 vs... no violation here, but the pair must be considered at all.
    hits = LC.containment(bets)
    assert isinstance(hits, list)


# ── duplicate pricing: the same event quoted twice ──────────────────────────
def test_duplicates_finds_two_prices_for_one_outcome_set():
    """Lider prices one event in several places. 'Team 1 Win and score more
    than 1.5 goals' IS the 1X2/Total grid cell 'Over / 1' at 1.5 — measured
    live at 6.90 and 3.10 on the same match."""
    m = LC.t_mask("1", "o")
    bets = [(m, 6.90, "1 win & score 2+", True), (m, 3.10, "1&O1.5", True)]
    hits = LC.duplicates(bets)
    assert len(hits) == 1
    hi_lab, hi_v, lo_lab, lo_v, gap = hits[0]
    assert (hi_lab, hi_v) == ("1 win & score 2+", 6.90)
    assert (lo_lab, lo_v) == ("1&O1.5", 3.10)
    assert gap == pytest.approx(122.58, abs=0.05)


def test_duplicates_respects_the_gap_floor_and_ignores_singletons():
    m = LC.t_mask("1", "o")
    assert LC.duplicates([(m, 6.90, "a", True), (m, 3.10, "b", True)], 200.0) == []
    assert LC.duplicates([(m, 6.90, "a", True)]) == []


def test_duplicate_needs_the_model_to_say_which_side_is_wrong():
    """A duplicate says one of the two is wrong, not which. Framing the gap as
    an overlay produced 3240 board-wide rows whose biggest were cases where the
    LONG side was the correct one and the short side the mistake — and a bad
    short price is not bettable. So no model, no row."""
    mts = {"mt:16:2679": {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                           {"id": "n", "name": "No"}]}}
    match = {"markets": {"a": {"typeId": "mt:16:2679", "specifier": None,
                               "outcomes": {"y": {"value": 6.90}}},
                         "b": {"typeId": "mt:16:1080",
                               "specifier": {"special": "1.5", "total": "1.5"},
                               "outcomes": {"ot:16:1531": {"value": 3.10}}}}}
    g = LC.parse_match(match, mts)
    assert len(g["cells"][("FT", 1.5)]) == 2      # both parsed, same mask
    # no 1X2 and no ladder -> no model -> nothing claimed
    assert [f for f in LC.analyse_match(g, "a", "b", None, "x")
            if f["kind"] == "combo_duplicate"] == []


# ── model fair pricing ──────────────────────────────────────────────────────
def _iwata_full():
    """Iwata's real 1X2 + totals ladder, including the INTEGER rungs."""
    mts = {"mt:16:500": {"name": "Full Time Result",
                         "outcomeTypes": [{"id": "ot:16:2", "name": "1"},
                                          {"id": "ot:16:1", "name": "X"},
                                          {"id": "ot:16:3", "name": "2"}]},
           "mt:16:502": {"name": "Total",
                         "outcomeTypes": [{"id": "ot:16:6", "name": "Under"},
                                          {"id": "ot:16:7", "name": "Over"}]}}
    mk = {"x": {"typeId": "mt:16:500", "specifier": None,
                "outcomes": {"ot:16:2": {"value": 2.25}, "ot:16:1": {"value": 2.95},
                             "ot:16:3": {"value": 2.85}}}}
    ladder = {0.5: (5.7, 1.06), 1: (5.1, 1.08), 1.5: (2.7, 1.33), 2: (2.1, 1.55),
              2.5: (1.6, 2.05), 3: (1.29, 2.9), 3.5: (1.21, 3.45), 4: (1.07, 5.4),
              4.5: (1.05, 5.8), 5: (1.01, 7.6), 5.5: (1.01, 7.7)}
    for i, (L, (u, o)) in enumerate(ladder.items()):
        mk[f"t{i}"] = {"typeId": "mt:16:502",
                       "specifier": {"special": str(L), "total": str(L)},
                       "outcomes": {"ot:16:6": {"value": u}, "ot:16:7": {"value": o}}}
    return {"markets": mk}, mts


def test_model_fit_reproduces_the_posted_half_line_ladder():
    match, mts = _iwata_full()
    g = LC.parse_match(match, mts)
    F = LC.fit_score_model(g, "J2 League")
    assert F is not None, "the fit must succeed on a normal match"
    ap = LC._atom_probs(F, 1.5)
    # the owner's market: 'Team 1 Win and score more than 1.5 goals' @ 6.90.
    # Fair sits near 3.45 — the book's own other price for it was 3.10.
    assert ap[("1", "o")] == pytest.approx(0.29, abs=0.02)


def test_integer_rungs_are_excluded_because_they_push():
    """A total of exactly L is a PUSH on an integer rung, so the two sides do
    not partition and a proportional devig of them is meaningless. Feeding them
    in put the fitted model 19.9pp off the posted ladder and killed the fit."""
    match, mts = _iwata_full()
    g = LC.parse_match(match, mts)
    assert LC.fit_score_model(g, "J2 League") is not None
    # strip every half-line, leaving only pushable integer rungs
    for k in [k for k, m in match["markets"].items()
              if m["typeId"] == "mt:16:502"
              and abs(float(m["specifier"]["total"]) % 1 - 0.5) < 1e-9]:
        del match["markets"][k]
    g2 = LC.parse_match(match, mts)
    assert LC.fit_score_model(g2, "J2 League") is None


def test_model_is_rejected_when_it_cannot_reproduce_the_ladder():
    """A fair price is only as good as the fit under it."""
    match, mts = _iwata_full()
    # bend one half-line rung far away from the rest
    for m in match["markets"].values():
        if m["typeId"] == "mt:16:502" and m["specifier"]["total"] == "3.5":
            m["outcomes"]["ot:16:6"]["value"] = 5.0
            m["outcomes"]["ot:16:7"]["value"] = 1.1
    g = LC.parse_match(match, mts)
    assert LC.fit_score_model(g, "J2 League") is None


def test_fair_flag_fires_on_the_owners_market():
    match, mts = _iwata_full()
    mts["mt:16:2679"] = {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                          {"id": "n", "name": "No"}]}
    match["markets"]["w"] = {"typeId": "mt:16:2679", "specifier": None,
                             "outcomes": {"y": {"value": 6.90}}}
    g = LC.parse_match(match, mts)
    fair = [f for f in LC.analyse_match(g, "Iwata", "Tokushima", "J2 League", "x")
            if f["kind"] == "combo_fair"]
    assert len(fair) == 1
    assert fair[0]["severity"] > 80, "6.90 against a ~3.45 fair is a huge overlay"
    assert "model fair" in fair[0]["detail"]


def test_fair_flag_silent_when_the_combo_is_priced_sanely():
    match, mts = _iwata_full()
    mts["mt:16:2679"] = {"outcomeTypes": [{"id": "y", "name": "Yes"},
                                          {"id": "n", "name": "No"}]}
    match["markets"]["w"] = {"typeId": "mt:16:2679", "specifier": None,
                             "outcomes": {"y": {"value": 3.20}}}   # below fair
    g = LC.parse_match(match, mts)
    assert [f for f in LC.analyse_match(g, "a", "b", None, "x")
            if f["kind"] == "combo_fair"] == []


def test_model_can_be_switched_off():
    match, mts = _iwata_full()
    mts["mt:16:2679"] = {"outcomeTypes": [{"id": "y", "name": "Yes"}]}
    match["markets"]["w"] = {"typeId": "mt:16:2679", "specifier": None,
                             "outcomes": {"y": {"value": 6.90}}}
    g = LC.parse_match(match, mts)
    kinds = {f["kind"] for f in LC.analyse_match(g, "a", "b", None, "x", model=False)}
    assert "combo_fair" not in kinds and "combo_duplicate" not in kinds


def test_new_limits_are_registered():
    from src import runtime_config
    for k in ("lider_combo_min_dup", "lider_combo_min_ev"):
        assert k in runtime_config.LIMITS


def test_scan_reports_started_separately_from_completed():
    """A first pass takes ~50s. Reporting only computed_at makes an in-flight
    sweep indistinguishable from a dead loop — the exact ambiguity that made
    this scan look broken on its first deploy."""
    from src import app
    assert hasattr(app, "_lider_combo_started")
    assert hasattr(app, "_lider_combo_passes")
    import inspect
    src = inspect.getsource(app._lider_combo_loop)
    assert "_lider_combo_started = datetime.now" in src, \
        "the start stamp must be written BEFORE the sweep, not after"
    assert src.index("_lider_combo_started = datetime") < src.index("await asyncio.to_thread")


# ── the book's own wording, so a row can be found on the site ───────────────
def test_disp_substitutes_the_line_and_strips_ui_decoration():
    mts = {"mt:16:1716": {"name": "I Team Not lose and Total Under {1}",
                          "outcomeTypes": [{"id": "y", "name": "Yes"}]},
           "mt:16:1080": {"name": "1X2 / Total ①",
                          "outcomeTypes": [{"id": "ot:16:1531", "name": "Over / 1 "}]}}
    assert LC._disp(mts, "mt:16:1716", "y", 4.5) == "I Team Not lose and Total Under 4.5 - Yes"
    # the circled glyph the UI decorates with is noise for a search box
    assert LC._disp(mts, "mt:16:1080", "ot:16:1531", 1.5) == "1X2 / Total 1.5 - Over / 1"


def test_disp_appends_the_line_when_the_name_has_no_placeholder():
    mts = {"t": {"name": "Total", "outcomeTypes": [{"id": "o", "name": "Over"}]}}
    assert LC._disp(mts, "t", "o", 2.5) == "Total 2.5 - Over"


def test_disp_falls_back_to_the_typeid_when_undocumented():
    assert LC._disp({}, "mt:16:9999") == "mt:16:9999"


def test_flag_detail_carries_the_books_wording_not_internal_shorthand():
    """The row has to be findable on the site; '1&O1.5' is not a thing you can
    search for, 'Team 1 Win and score more than 1.5 goals' is."""
    match, mts = _iwata_full()
    mts["mt:16:2679"] = {"name": "Team 1 Win and score more than 1.5 goals ①",
                         "outcomeTypes": [{"id": "y", "name": "Yes"},
                                          {"id": "n", "name": "No"}]}
    match["markets"]["w"] = {"typeId": "mt:16:2679", "specifier": None,
                             "outcomes": {"y": {"value": 6.90}}}
    g = LC.parse_match(match, mts)
    f = [x for x in LC.analyse_match(g, "Iwata", "Tokushima", "J2 League", "x")
         if x["kind"] == "combo_fair"][0]
    assert "Team 1 Win and score more than 1.5 goals - Yes" in f["detail"]
    assert "&O1.5" not in f["detail"], "internal shorthand must not reach the tab"


def test_primitive_hedge_legs_also_use_the_books_wording():
    """A cover names legs you have to go and place, so the 1X2 / total legs need
    the same treatment as the combo cells."""
    match, mts = _iwata_full()
    mts["mt:16:500"]["name"] = "Full Time Result"
    g = LC.parse_match(match, mts)
    labels = [lab for _m, _v, lab, _e in LC.total_bets(g, "FT", 2.5)]
    assert any(l.startswith("Full Time Result - ") for l in labels)
    assert any("Total 2.5 - " in l for l in labels)


# ── dominance must not fire on a pair where both prices are bad ─────────────
def test_dominance_needs_the_superset_to_be_worth_backing():
    """Eintracht Trier v RB Leipzig, 2026-08-20. The book posted

        1X2   1: 28.00  X: 10.00  2: 1.02      -> devigged P(1X) = 0.075, fair 13.31
        DC    1X: 4.50                          -> grossly underpriced
        totals Over 0.5 @ 1.01, Over 1.5 @ 1.01 -> pinned at the book's floor

    and the check fired "'Double Chance - 1X' @ 4.50 is contained in 'Team 2
    Win and score more than 1.5 goals - No' @ 7.70, the superset pays 71% more".
    Logically true, and useless: 1X at 4.50 is worth 13.31 so nobody would back
    it, and the superset is itself EV -27%. The model could not even be fitted
    here because the floored totals rungs are unreproducible. A row that invites
    a bet must have that bet clear model fair."""
    bets = [(LC.t_mask("1", "u") | LC.t_mask("1", "o")
             | LC.t_mask("X", "u") | LC.t_mask("X", "o"), 4.50, "DC 1X", True),
            (LC.T_FULL & ~LC.t_mask("2", "o"), 7.70, "2 win & score 2+ - No", True)]
    # the raw containment relation is real and must still be detectable
    hits = LC.containment(bets)
    assert len(hits) == 1 and hits[0][4] == pytest.approx(71.11, abs=0.05)
    # ...but with no model behind it, nothing may be published
    g = _g(cells={("FT", 1.5): bets}, x12={"FT": {"1": 28.0, "X": 10.0, "2": 1.02}})
    assert [f for f in LC.analyse_match(g, "Trier", "Leipzig", "Cup", "x")
            if f["kind"] == "combo_dominance"] == []


def test_dominance_still_fires_when_the_superset_is_good():
    """The gate must not silence the real ones: Iwata's '1X2 / Total 4.5 -
    Over / 1' @ 16 inside 'I Team Not lose and Total Over 4.5 - Yes' @ 24,
    where the superset is +39.8% against model fair."""
    match, mts = _iwata_full()
    mts["mt:16:1718"] = {"name": "I Team Not lose and Total Over {1}",
                         "outcomeTypes": [{"id": "y", "name": "Yes"},
                                          {"id": "n", "name": "No"}]}
    mts["mt:16:1080"] = {"name": "1X2 / Total",
                         "outcomeTypes": [{"id": "ot:16:1531", "name": "Over / 1"}]}
    match["markets"]["a"] = {"typeId": "mt:16:1718",
                             "specifier": {"special": "4.5"},
                             "outcomes": {"y": {"value": 24.0}}}
    match["markets"]["b"] = {"typeId": "mt:16:1080",
                             "specifier": {"special": "4.5", "total": "4.5"},
                             "outcomes": {"ot:16:1531": {"value": 16.0}}}
    g = LC.parse_match(match, mts)
    dom = [f for f in LC.analyse_match(g, "Iwata", "Tokushima", "J2", "x")
           if f["kind"] == "combo_dominance"]
    assert len(dom) == 1
    assert dom[0]["severity"] > 20, "severity is now the superset's EV, not the gap"
    assert "EV" in dom[0]["detail"] and "model fair" in dom[0]["detail"]
