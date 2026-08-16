"""Layered opportunity alerts — AND within a layer, OR across layers (2026-08-16).

The motivating request: "10 %+ for 1-3 odds, 15 %+ for 4-5". A single gate-set
cannot say that, because within a gate-set every criterion is ANDed and those two
rules contradict each other — no row is both ≤3 and ≥4. So the panel stores a
list of gate-sets and a row alerts if it clears ANY of them.

Driven against the real functions in static/alerts.js under node, because the
static wiring guards cannot show that the OR actually ORs, that a stricter band
does not leak into a looser one, or that an existing pre-layers configuration
survives the upgrade.
"""
from __future__ import annotations

import json

import pytest

from tests.jsrun import NODE, run_js

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

A_LAYERS = "arb_alert_layers"


def _row(**kw):
    """An opportunity row. Defaults are deliberately benign — a strong +EV
    moneyline — so each test varies only what it is about."""
    o = {"edge_pct": 12.0, "cb_odds": 2.0, "pin_no_vig": 1.9, "kind": "+EV",
         "kelly_stake": 20.0, "confidence": "strong", "sport": "soccer",
         "book": "cb", "market_type": "moneyline", "period": "FT",
         "start_time": None, "pin_max_stake": 500}
    o.update(kw)
    return o


def _layer(name, on=True, **gates):
    return {"name": name, "on": on, "g": gates}


def matched_names(row, layers, store=None):
    st = dict(store or {})
    st[A_LAYERS] = json.dumps(layers)
    return run_js(
        f"out = A.matchingLayers({json.dumps(row)}, A.readLayers()).map(l => l.name);", st)


# ── the requested feature, stated as the user stated it ──────────────────────

# 10%+ for odds 1-3, 15%+ for odds 4-5.
BANDS = [
    _layer("short", edge=10, oddsMin=1, oddsMax=3),
    _layer("long", edge=15, oddsMin=4, oddsMax=5),
]


@pytest.mark.parametrize("odds,edge,expected", [
    (2.0, 12.0, ["short"]),    # in the short band, clears its 10% bar
    (2.0,  8.0, []),           # in the short band, under its bar
    (4.5, 16.0, ["long"]),     # in the long band, clears its 15% bar
    (4.5, 12.0, []),           # 12% would pass the SHORT bar — must not leak here
    (4.5, 10.0, []),           # exactly the short bar, still short of the long one
    (2.0, 16.0, ["short"]),    # a big edge on a short price: short band only
    (3.5, 20.0, []),           # 3-4 is in neither band, however big the edge
    (1.0, 10.0, ["short"]),    # band edges are inclusive
    (3.0, 10.0, ["short"]),
    (4.0, 15.0, ["long"]),
    (5.0, 15.0, ["long"]),
])
def test_the_two_band_example_behaves_as_described(odds, edge, expected):
    assert matched_names(_row(cb_odds=odds, edge_pct=edge), BANDS) == expected


def test_a_stricter_band_does_not_suppress_a_looser_one():
    """The failure that would make layers useless: ANDing them instead of ORing.
    A row in the short band must alert even though it fails the long band."""
    assert matched_names(_row(cb_odds=2.0, edge_pct=12.0), BANDS) == ["short"]


def test_overlapping_layers_can_both_match():
    both = [_layer("wide", edge=5), _layer("narrow", edge=10, oddsMax=3)]
    assert matched_names(_row(cb_odds=2.0, edge_pct=12.0), both) == ["wide", "narrow"]


# ── AND is preserved INSIDE a layer ─────────────────────────────────────────

def test_within_a_layer_every_filled_gate_must_pass():
    L = [_layer("strict", edge=10, oddsMin=1, oddsMax=3, kellyMin=15, pinMax=400)]
    assert matched_names(_row(), L) == ["strict"]
    assert matched_names(_row(kelly_stake=5.0), L) == []      # kelly fails
    assert matched_names(_row(pin_max_stake=100), L) == []    # pin limit fails
    assert matched_names(_row(cb_odds=9.0), L) == []          # odds fails


def test_a_layer_with_no_criteria_matches_everything():
    """An empty layer is 'tell me about all of it' — that is what the master
    switch is for turning off, not a silently-dead layer."""
    assert matched_names(_row(edge_pct=1.0), [_layer("all")]) == ["all"]


# ── active / inactive ───────────────────────────────────────────────────────

def test_an_inactive_layer_never_matches():
    L = [_layer("parked", on=False, edge=1), _layer("live", edge=99)]
    assert matched_names(_row(edge_pct=50.0), L) == []


def test_parking_one_layer_leaves_the_others_working():
    L = [_layer("parked", on=False, edge=1), _layer("live", edge=10)]
    assert matched_names(_row(edge_pct=12.0), L) == ["live"]


# ── migration from the pre-layers configuration ─────────────────────────────

LEGACY = {"alert_threshold": "10", "arb_alert_odds_min": "1",
          "arb_alert_odds_max": "3", "arb_alert_pp": "2"}


def test_a_pre_layers_config_is_read_as_a_single_layer():
    """A browser that has not opened arbs.html since the upgrade must keep
    alerting on exactly what it was configured for, not revert to everything."""
    got = run_js("out = A.readLayers().map(l => [l.name, l.g.edge, l.g.oddsMax, l.g.pp]);",
                 LEGACY)
    assert got == [["Layer 1", 10, 3, 2]]


def test_migrated_gates_still_reject_what_they_rejected_before():
    inside = run_js(
        f"out = A.matchingLayers({json.dumps(_row(cb_odds=2.0, edge_pct=12.0))},"
        " A.readLayers()).length;", LEGACY)
    outside = run_js(
        f"out = A.matchingLayers({json.dumps(_row(cb_odds=9.0, edge_pct=12.0))},"
        " A.readLayers()).length;", LEGACY)
    assert (inside, outside) == (1, 0)


def test_stored_layers_win_over_the_legacy_keys():
    """Once migrated, the flat keys are history — a stale one must not re-narrow
    a layer the user has since widened."""
    st = dict(LEGACY)
    st[A_LAYERS] = json.dumps([_layer("new", edge=1)])
    got = run_js("out = A.readLayers().map(l => [l.name, l.g.edge, l.g.oddsMax]);", st)
    assert got == [["new", 1, None]]


@pytest.mark.parametrize("bad", ["", "not json", "{}", "[]", "null", "3"])
def test_malformed_layers_fall_back_to_the_legacy_single_layer(bad):
    """Corrupt storage must not silence the alert — it falls back, it does not
    throw or produce zero layers."""
    st = dict(LEGACY)
    st[A_LAYERS] = bad
    got = run_js("out = A.readLayers().map(l => l.g.edge);", st)
    assert got == [10]


def test_a_layer_with_missing_fields_is_filled_in_as_off():
    """Hand-written or partially-saved JSON must not make undefined gates behave
    like 0 — a 0 odds floor would be harmless but a 0 edge bar fires on all."""
    st = {A_LAYERS: json.dumps([{"name": "sparse", "on": True, "g": {"edge": 5}}])}
    got = run_js("out = A.readLayers()[0].g;", st)
    assert got["edge"] == 5
    for f in ("pp", "oddsMin", "oddsMax", "kellyMin", "kellyMax", "pinMax",
              "leadMin", "leadMaxH"):
        assert got[f] is None, f"{f} should be off, got {got[f]!r}"


# ── the query floor: the silent-starvation trap ─────────────────────────────

@pytest.mark.parametrize("layers,expected", [
    ([_layer("a", edge=10)], 1),                       # above 1 → server's 1 is fine
    ([_layer("a", edge=0.5)], 0.5),                    # below 1 → must widen
    ([_layer("a", edge=10), _layer("b", edge=0.4)], 0.4),   # widen to the loosest
    ([_layer("a", edge=0.4), _layer("b", edge=10)], 0.4),   # order must not matter
    ([_layer("a")], 1),                                # no edge gate at all
    ([_layer("a", edge=0.2), _layer("b")], 1),         # an unbounded layer wins
])
def test_query_floor_is_the_most_generous_active_layer(layers, expected):
    """/api/opportunities pre-filters at min_edge, so a layer asking below the
    floor would never see its rows — configured and silently dead."""
    st = {A_LAYERS: json.dumps(layers)}
    assert run_js("out = A.oppQueryFloor(A.readLayers());", st) == expected


def test_query_floor_ignores_inactive_layers():
    st = {A_LAYERS: json.dumps([_layer("off", on=False, edge=0.1),
                                _layer("on", edge=10)])}
    assert run_js("out = A.oppQueryFloor(A.readLayers());", st) == 1


# ── re-alert step becomes per-layer ─────────────────────────────────────────

def step_for(layers, row=None):
    st = {A_LAYERS: json.dumps(layers)}
    r = row or _row()
    return run_js(
        f"const m = A.matchingLayers({json.dumps(r)}, A.readLayers());"
        " out = A.effectiveStep(m);", st)


def test_step_defaults_to_the_old_constant():
    assert step_for([_layer("a", edge=1)]) == 5


def test_step_comes_from_the_matching_layer():
    assert step_for([_layer("a", edge=1, step=2)]) == 2


def test_step_is_the_most_eager_of_the_matching_layers():
    """OR semantics all the way down: if any matching layer wants to hear about
    a +1pp improvement, you hear about it."""
    L = [_layer("coarse", edge=1, step=10), _layer("fine", edge=1, step=1)]
    assert step_for(L) == 1


def test_a_non_matching_layers_step_is_ignored():
    L = [_layer("matches", edge=1, step=8), _layer("misses", edge=99, step=1)]
    assert step_for(L) == 8


def test_step_falls_back_when_nothing_matched():
    """A row that matches no layer still enters the seen-set, and needs some
    step to be tracked against."""
    assert step_for([_layer("a", edge=99)]) == 5


# ── confidence default is per-layer ─────────────────────────────────────────

def test_weak_rows_are_excluded_by_default_in_every_layer():
    """The 2026-07-26 finding (big edges are overwhelmingly weak) must survive
    becoming per-layer."""
    assert matched_names(_row(confidence="weak"), [_layer("a", edge=1)]) == []


def test_a_layer_can_opt_into_weak_without_affecting_another():
    L = [_layer("strict", edge=1), _layer("loose", edge=1, conf=["weak"])]
    assert matched_names(_row(confidence="weak"), L) == ["loose"]
    assert matched_names(_row(confidence="strong"), L) == ["strict"]


# ── ARB rows keep their Kelly exemption per layer ───────────────────────────

def test_arb_rows_are_still_exempt_from_a_kelly_floor():
    """edge.py leaves ARB staking to the bettor so kelly_stake is 0 on every ARB
    row; a Kelly floor that applied would silence arbs entirely."""
    L = [_layer("a", kellyMin=10)]
    assert matched_names(_row(kind="ARB", kelly_stake=0.0), L) == ["a"]
    assert matched_names(_row(kind="+EV", kelly_stake=0.0), L) == []


# ── the gap warning: bands that leave a hole ────────────────────────────────
# Configure "1-3" and "4-5" and a 40 % edge at 3.50 falls between them and never
# chimes. The panel looks fully configured; the row is simply never heard. That
# is worth warning about in the UI, so the detection is worth testing — against
# the function as it actually ships in arbs.html, not a reimplementation.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from tests.jsrun import NODE as _NODE  # noqa: E402,F401

_ARBS = (Path(__file__).resolve().parent.parent / "static" / "arbs.html").read_text()


def _odds_gaps_src() -> str:
    m = re.search(r"\nfunction oddsGaps\(\) \{.*?\n\}\n", _ARBS, re.S)
    assert m, "oddsGaps() not found in arbs.html — did it get renamed?"
    return m.group(0)


def odds_gaps(layers):
    """Run arbs.html's real oddsGaps() with loadLayers() stubbed."""
    import json as _json
    import subprocess
    from tests.jsrun import NODE
    src = (f"const LAYERS = {_json.dumps(layers)};\n"
           "function loadLayers() { return LAYERS.map(l => ({on: l.on !== false, "
           "g: Object.assign({oddsMin: null, oddsMax: null}, l.g || {})})); }\n"
           + _odds_gaps_src()
           + "\nconsole.log(JSON.stringify(oddsGaps()));\n")
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout.strip())


def test_the_reported_examples_gap_is_detected():
    """1-3 and 4-5 leaves 3-4 uncovered."""
    assert odds_gaps([_layer("short", edge=10, oddsMin=1, oddsMax=3),
                      _layer("long", edge=15, oddsMin=4, oddsMax=5)]) == [[3, 4]]


def test_adjacent_bands_have_no_gap():
    assert odds_gaps([_layer("a", oddsMin=1, oddsMax=3),
                      _layer("b", oddsMin=3, oddsMax=5)]) == []


def test_overlapping_bands_have_no_gap():
    assert odds_gaps([_layer("a", oddsMin=1, oddsMax=4),
                      _layer("b", oddsMin=3, oddsMax=6)]) == []


def test_an_unbounded_layer_covers_everything():
    """One layer with no odds bounds means there is no hole to warn about."""
    assert odds_gaps([_layer("any", edge=20),
                      _layer("b", oddsMin=4, oddsMax=5)]) == []


def test_an_open_ended_top_band_closes_the_range():
    assert odds_gaps([_layer("a", oddsMin=1, oddsMax=3),
                      _layer("b", oddsMin=3)]) == []


def test_a_single_layer_never_warns():
    """One band is a deliberate choice, not an oversight."""
    assert odds_gaps([_layer("only", oddsMin=1, oddsMax=3)]) == []


def test_inactive_layers_do_not_fill_a_gap():
    """A parked layer is not alerting, so it cannot be what covers the band."""
    assert odds_gaps([_layer("a", oddsMin=1, oddsMax=3),
                      _layer("mid", on=False, oddsMin=3, oddsMax=4),
                      _layer("b", oddsMin=4, oddsMax=5)]) == [[3, 4]]


def test_multiple_gaps_are_all_reported():
    assert odds_gaps([_layer("a", oddsMin=1, oddsMax=2),
                      _layer("b", oddsMin=3, oddsMax=4),
                      _layer("c", oddsMin=5, oddsMax=6)]) == [[2, 3], [4, 5]]


def test_band_order_does_not_matter():
    assert odds_gaps([_layer("b", oddsMin=4, oddsMax=5),
                      _layer("a", oddsMin=1, oddsMax=3)]) == [[3, 4]]
