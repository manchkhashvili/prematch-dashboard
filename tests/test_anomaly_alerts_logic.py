"""Behavioural tests for the anomaly alert DECISION rules (2026-08-10).

The static guards in test_anomaly_alerts_wiring.py prove the panel and the
poller are connected. They cannot prove the rules are right, and the rules are
where this has gone wrong before: the collapse bug that re-fired the alarm on
every already-dismissed opportunity was a logic bug in exactly this file, in
code that read fine.

So these drive the REAL functions out of static/alerts.js under node, via the
test seam at the bottom of that file. Skipped (not failed) where node is
absent — it is not a dependency of the dashboard itself.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ALERTS = Path(__file__).resolve().parent.parent / "static" / "alerts.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(body: str, store: dict | None = None):
    """Execute `body` with alerts.js loaded and a localStorage stub seeded from
    `store`. The body assigns its result to `out`."""
    harness = textwrap.dedent("""
        const store = %s;
        globalThis.localStorage = {
          getItem: k => (k in store ? String(store[k]) : null),
          setItem: (k, v) => { store[k] = String(v); },
        };
        // alerts.js schedules timers and touches the DOM at load; stub both.
        globalThis.document = { addEventListener() {} };
        globalThis.setTimeout = () => 0;
        globalThis.setInterval = () => 0;
        globalThis.fetch = () => Promise.reject(new Error("no network in tests"));
        const A = require(%s);
        let out;
        %s
        console.log(JSON.stringify(out));
    """) % (json.dumps(store or {}), json.dumps(str(ALERTS)), body)
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


LAD_PCT = "anom_ladder_alert_pct"
LAD_DELTA = "anom_ladder_alert_delta"
LAD_STEP = "anom_ladder_alert_step"


def ladder_passes(row: dict, cfg: dict):
    return run_js(f"out = A.ladderPasses({json.dumps(row)});", cfg)


# ── ladder criteria: OR across the filled boxes ──────────────────────────────

ROW = {"pct": 8.0, "delta": 0.12, "step": 0.5}


def test_no_criterion_set_fires_nothing():
    """Enabling the checkbox but filling nothing must be silent, not a firehose."""
    assert ladder_passes(ROW, {}) is False


def test_blank_string_is_off_not_zero():
    """A cleared box must disable the criterion. As 0 it would match every row."""
    assert ladder_passes(ROW, {LAD_PCT: "", LAD_DELTA: "  "}) is False


def test_garbage_is_off():
    assert ladder_passes(ROW, {LAD_PCT: "abc"}) is False


@pytest.mark.parametrize("key,val", [(LAD_PCT, "8"), (LAD_DELTA, "0.12"), (LAD_STEP, "0.5")])
def test_each_criterion_fires_on_its_own_at_the_boundary(key, val):
    """Each of the three is independently sufficient — the 'or' the panel says."""
    assert ladder_passes(ROW, {key: val}) is True


@pytest.mark.parametrize("key,val", [(LAD_PCT, "8.1"), (LAD_DELTA, "0.13"), (LAD_STEP, "0.6")])
def test_each_criterion_is_silent_just_below_its_bar(key, val):
    assert ladder_passes(ROW, {key: val}) is False


def test_one_matching_criterion_is_enough_even_when_others_miss():
    cfg = {LAD_PCT: "99", LAD_DELTA: "99", LAD_STEP: "0.5"}
    assert ladder_passes(ROW, cfg) is True


def test_a_missing_field_on_the_row_does_not_fire():
    """`step` is null when a line is non-numeric; that must not read as 0."""
    assert ladder_passes({"pct": 1.0, "delta": 0.01, "step": None},
                         {LAD_STEP: "0.5"}) is False


# ── consistency: per-check bar, default inheritance, silencing ───────────────

def cons_passes(flag: dict, kinds: dict, default):
    return run_js(
        f"out = A.consPasses({json.dumps(flag)}, {json.dumps(kinds)}, {json.dumps(default)});")


F = {"kind": "htft_combo", "severity": 9.0}


def test_inherits_the_default_when_the_check_has_no_own_bar():
    assert cons_passes(F, {}, 8) is True
    assert cons_passes(F, {}, 10) is False


def test_blank_per_check_value_inherits_the_default():
    assert cons_passes(F, {"htft_combo": {"on": True, "sev": None}}, 8) is True


def test_per_check_bar_overrides_the_default_in_both_directions():
    """The point of the feature: severity units differ per check, so a check
    must be able to sit both above and below the shared default."""
    assert cons_passes(F, {"htft_combo": {"on": True, "sev": 15}}, 1) is False
    assert cons_passes(F, {"htft_combo": {"on": True, "sev": 2}}, 50) is True


def test_unticking_a_check_silences_it_regardless_of_severity():
    huge = {"kind": "htft_combo", "severity": 999.0}
    assert cons_passes(huge, {"htft_combo": {"on": False, "sev": 1}}, 1) is False


def test_other_checks_are_unaffected_by_one_being_silenced():
    other = {"kind": "ml_vs_spread", "severity": 9.0}
    kinds = {"htft_combo": {"on": False}}
    assert cons_passes(other, kinds, 8) is True


def test_no_default_and_no_per_check_bar_fires_nothing():
    assert cons_passes(F, {}, None) is False


# ── new-or-worsened dedup, seeding, collapse ─────────────────────────────────

def evaluate(rows, seen, seeded, *, magnitude="pct", passes="r => true"):
    """Drive evaluateFeed and return (fires, resulting seen-map)."""
    # "S" is the seeded-marker key; present => this is not the first pass.
    body = f"""
        const seen = new Map({json.dumps(list(seen.items()))});
        const rows = {json.dumps(rows)}.map(r => {{ r.__key = r.k; return r; }});
        const fires = A.evaluateFeed(rows, seen, {passes},
                                     r => r.{magnitude}, "S");
        out = {{ fires, seen: [...seen], seededAfter: !!localStorage.getItem("S") }};
    """
    return run_js(body, {"S": "1"} if seeded else {})


def test_first_pass_seeds_silently():
    """Turning the alert on must not chime once per finding already on screen."""
    rows = [{"k": "a", "pct": 50}, {"k": "b", "pct": 60}]
    res = evaluate(rows, {}, seeded=False)
    assert res["fires"] == 0
    assert dict(res["seen"]) == {"a": 50, "b": 60}
    assert res["seededAfter"] is True, "never marked seeded — it would stay silent forever"


def test_a_genuinely_new_row_fires_once_then_stays_quiet():
    rows = [{"k": "a", "pct": 50}]
    first = evaluate(rows, {}, seeded=True)
    assert first["fires"] == 1
    again = evaluate(rows, dict(first["seen"]), seeded=True)
    assert again["fires"] == 0


def test_a_row_that_gets_materially_worse_fires_again():
    worse = [{"k": "a", "pct": 80}]          # 50 -> 80 is >= 1.5x
    assert evaluate(worse, {"a": 50}, seeded=True)["fires"] == 1


def test_a_row_that_drifts_up_slightly_does_not_re_fire():
    nudged = [{"k": "a", "pct": 60}]          # 50 -> 60 is < 1.5x
    assert evaluate(nudged, {"a": 50}, seeded=True)["fires"] == 0


def test_a_below_threshold_row_is_tracked_but_silent_then_fires_when_it_clears():
    """Tracked as null so that clearing the bar later reads as NEW rather than
    being swallowed as 'already seen'."""
    rows = [{"k": "a", "pct": 1}]
    quiet = evaluate(rows, {}, seeded=True, passes="r => r.pct >= 10")
    assert quiet["fires"] == 0
    assert dict(quiet["seen"]) == {"a": None}
    risen = evaluate([{"k": "a", "pct": 30}], {"a": None}, seeded=True,
                     passes="r => r.pct >= 10")
    assert risen["fires"] == 1


def test_vanished_rows_are_pruned():
    res = evaluate([{"k": "a", "pct": 5}], {"a": 5, "gone": 9}, seeded=True)
    assert "gone" not in dict(res["seen"])


def test_a_collapsed_feed_keeps_the_seen_set():
    """The bug this guard exists for: a feed that briefly empties must not
    forget everything and re-alert on the whole board one poll later."""
    seen = {f"k{i}": 5 for i in range(10)}
    res = evaluate([{"k": "k0", "pct": 5}], seen, seeded=True)
    assert len(res["seen"]) == 10, "collapse wiped the seen-set"


def test_a_small_board_shrinking_is_not_treated_as_a_collapse():
    """MIN_KEYS_FOR_PRUNE — on a 2-row board, 2 -> 1 is normal, not a glitch."""
    res = evaluate([{"k": "a", "pct": 5}], {"a": 5, "b": 5}, seeded=True)
    assert "b" not in dict(res["seen"])


# ── keys are stable and distinguish rows that must not collide ───────────────

def test_ladder_key_separates_the_two_sides_of_one_rung_pair():
    base = {"book": "cb", "event_id": "E", "market": "spread FT",
            "period": "FT", "line_lo": 4.5, "line_hi": 5.0}
    home = run_js(f"out = A.ladderKey({json.dumps({**base, 'side': 'home'})});")
    away = run_js(f"out = A.ladderKey({json.dumps({**base, 'side': 'away'})});")
    assert home != away


def test_consistency_key_separates_outcomes_of_the_same_check():
    base = {"book": "cb", "event_id": "E", "kind": "htft_combo", "periods": "H1+FT"}
    a = run_js(f"out = A.consKey({json.dumps({**base, 'outcome': '1/1'})});")
    b = run_js(f"out = A.consKey({json.dumps({**base, 'outcome': '2/2'})});")
    assert a != b
