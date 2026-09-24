"""The Arbs alert panel's init path actually RUNS (2026-08-16).

Layers rewrote ~170 lines of arbs.html's config block, including the migration
that turns a pre-layers configuration into layer 1. That code runs at page load
inside the page's single inline <script>, so an exception in it does not just
break the panel — it kills everything after it in that script, including the
opportunity table's own render. Parsing the file proves nothing about that.

There is no jsdom here and adding one for this is not worth a dependency, so the
harness stubs the handful of DOM calls the block makes and executes the REAL
extracted source. Enough to answer: does it run, does it migrate, does it draw
the layer chips, does editing a layer persist.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.jsrun import NODE

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

ARBS = Path(__file__).resolve().parent.parent / "static" / "arbs.html"
ARBS_T = ARBS.read_text(encoding="utf-8")

# Minimal DOM. Deliberately small: it records what the panel did rather than
# emulating a browser, and anything the panel touches that is NOT stubbed throws
# — which is the point, since a silent no-op stub would hide a real breakage.
STUB = r"""
const store = __STORE__;
globalThis.localStorage = {
  getItem: k => (k in store ? String(store[k]) : null),
  setItem: (k, v) => { store[k] = String(v); },
};
function mkEl(id) {
  const cls = new Set();
  return {
    id, value: "", checked: false, disabled: false,
    textContent: "", _html: "", children: [],
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; if (v === "") this.children = []; },
    // A browser reflects className into classList and back. renderLayerBar
    // builds chips by assigning className, while paintAlertCfg uses
    // classList.toggle — a stub that kept them separate would report a
    // correctly-classed chip as unclassed.
    get className() { return [...cls].join(" "); },
    set className(v) {
      cls.clear();
      for (const c of String(v).split(/\s+/)) if (c) cls.add(c);
    },
    classList: {
      add: c => cls.add(c), remove: c => cls.delete(c),
      contains: c => cls.has(c),
      toggle: (c, on) => { if (on === undefined) { cls.has(c) ? cls.delete(c) : cls.add(c); }
                           else if (on) cls.add(c); else cls.delete(c); },
      _set: cls,
    },
    dataset: {}, title: "",
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this._on = this._on || {})[ev] = fn; },
    click() { if (this._on && this._on.click) this._on.click(); },
    fire(ev) { if (this._on && this._on[ev]) this._on[ev](); },
  };
}
const els = {};
globalThis.document = {
  getElementById(id) { return (els[id] = els[id] || mkEl(id)); },
  createElement(tag) { return mkEl("<" + tag + ">"); },
  querySelectorAll() { return []; },
  querySelector() { return mkEl("?"); },
  addEventListener() {},
};
// Defined earlier in the page than the config block; the block closes over it.
const alertEnabled = document.getElementById("alert-enabled");
"""


def _panel_src() -> str:
    """The alert-config block, verbatim from arbs.html."""
    start = ARBS_T.index("// ── Advanced alert config")
    end = ARBS_T.index("\nrestoreAlertCfg();", start) + len("\nrestoreAlertCfg();")
    return ARBS_T[start:end]


def run_panel(store: dict | None = None, after: str = "out = null;"):
    src = (STUB.replace("__STORE__", json.dumps(store or {}))
           + "\n" + _panel_src()
           + "\nlet out;\n" + after
           + "\nconsole.log('\\u0001' + JSON.stringify({out, store}));\n")
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, f"the panel threw at load:\n{r.stderr}"
    for line in r.stdout.splitlines():
        if line.startswith(""):
            return json.loads(line[1:])
    raise AssertionError(f"no result:\n{r.stdout}\n{r.stderr}")


# ── it runs at all ───────────────────────────────────────────────────────────

def test_panel_init_does_not_throw_on_a_clean_store():
    """A first-ever visit. If this throws, every script line after the panel in
    arbs.html — including the table render — never executes."""
    run_panel({})


def test_panel_init_does_not_throw_with_layers_already_saved():
    run_panel({"arb_alert_layers": json.dumps(
        [{"name": "a", "on": True, "g": {"edge": 10}}])})


@pytest.mark.parametrize("bad", ["", "not json", "[]", "null", "{}", '[{"g":null}]'])
def test_panel_survives_corrupt_layer_storage(bad):
    """Storage is user-editable and survives upgrades; it must never be able to
    take the page down."""
    run_panel({"arb_alert_layers": bad})


# ── migration ────────────────────────────────────────────────────────────────

def test_a_pre_layers_config_is_migrated_and_persisted():
    """The upgrade path: flat keys in, one named layer out, written back so the
    next load reads layers rather than migrating again."""
    res = run_panel({
        "alert_threshold": "10", "arb_alert_odds_min": "1",
        "arb_alert_odds_max": "3", "arb_alert_books": json.dumps(["cb"]),
    })
    saved = json.loads(res["store"]["arb_alert_layers"])
    assert len(saved) == 1
    assert saved[0]["name"] == "Layer 1" and saved[0]["on"] is True
    assert saved[0]["g"]["edge"] == 10
    assert saved[0]["g"]["oddsMin"] == 1 and saved[0]["g"]["oddsMax"] == 3
    assert saved[0]["g"]["books"] == ["cb"]


def test_migration_leaves_unset_gates_off_not_zero():
    res = run_panel({"alert_threshold": "10"})
    g = json.loads(res["store"]["arb_alert_layers"])[0]["g"]
    for f in ("pp", "oddsMin", "oddsMax", "kellyMin", "pinMax", "leadMin"):
        assert g[f] is None, f"{f} migrated as {g[f]!r}, should be off"


def test_a_clean_store_migrates_to_one_empty_layer():
    res = run_panel({})
    saved = json.loads(res["store"]["arb_alert_layers"])
    assert len(saved) == 1
    assert all(v is None for v in saved[0]["g"].values())


# ── the layer bar is drawn ───────────────────────────────────────────────────

def test_a_chip_is_rendered_per_layer_and_the_selected_one_is_marked():
    store = {"arb_alert_layers": json.dumps([
        {"name": "short", "on": True, "g": {"edge": 10}},
        {"name": "long", "on": True, "g": {"edge": 15}},
    ]), "arbs_alert_layer_sel": "1"}
    res = run_panel(store, after="""
      const tabs = document.getElementById("layer-tabs");
      out = { names: tabs.children.map(c => c.textContent),
              sel: tabs.children.map(c => c.classList.contains("sel")) };
    """)
    assert res["out"]["names"] == ["short", "long"]
    assert res["out"]["sel"] == [False, True], "the selected layer is not marked"


def test_an_inactive_layer_is_drawn_as_parked():
    store = {"arb_alert_layers": json.dumps([
        {"name": "a", "on": True, "g": {}}, {"name": "b", "on": False, "g": {}},
    ])}
    res = run_panel(store, after="""
      out = document.getElementById("layer-tabs").children
              .map(c => c.classList.contains("parked"));
    """)
    assert res["out"] == [False, True]


def test_the_editor_is_filled_from_the_selected_layer():
    store = {"arb_alert_layers": json.dumps([
        {"name": "short", "on": True, "g": {"edge": 10, "oddsMax": 3}},
        {"name": "long", "on": True, "g": {"edge": 15, "oddsMax": 5}},
    ]), "arbs_alert_layer_sel": "1"}
    res = run_panel(store, after="""
      out = { edge: document.getElementById("alert-threshold").value,
              oddsMax: document.getElementById("alert-odds-max").value,
              name: document.getElementById("layer-name").value };
    """)
    assert res["out"] == {"edge": 15, "oddsMax": 5, "name": "long"}


def test_delete_is_disabled_with_only_one_layer():
    """Zero layers would be an un-representable state — silencing is the master
    switch's job."""
    res = run_panel({}, after='out = document.getElementById("layer-del").disabled;')
    assert res["out"] is True


# ── add / duplicate / delete / edit round-trip ──────────────────────────────

def test_add_appends_a_layer_and_selects_it():
    res = run_panel({}, after="""
      document.getElementById("layer-add").click();
      out = { n: loadLayers().length, sel: selIdx() };
    """)
    assert res["out"] == {"n": 2, "sel": 1}
    assert len(json.loads(res["store"]["arb_alert_layers"])) == 2


def test_duplicate_copies_the_gates_without_aliasing_them():
    """A shallow copy would make the two bands edit each other — the bug that
    would quietly make a second odds band impossible."""
    store = {"arb_alert_layers": json.dumps(
        [{"name": "src", "on": True, "g": {"edge": 10, "oddsMax": 3}}])}
    res = run_panel(store, after="""
      document.getElementById("layer-dup").click();
      const all = loadLayers();
      all[1].g.edge = 99;
      out = { names: all.map(l => l.name), first: all[0].g.edge, second: all[1].g.edge };
    """)
    assert res["out"]["names"] == ["src", "src copy"]
    assert res["out"]["first"] == 10, "editing the copy changed the original"
    assert res["out"]["second"] == 99


def test_delete_removes_the_selected_layer_and_clamps_the_selection():
    store = {"arb_alert_layers": json.dumps([
        {"name": "a", "on": True, "g": {}}, {"name": "b", "on": True, "g": {}},
    ]), "arbs_alert_layer_sel": "1"}
    res = run_panel(store, after="""
      document.getElementById("layer-del").click();
      out = { names: loadLayers().map(l => l.name), sel: selIdx() };
    """)
    assert res["out"] == {"names": ["a"], "sel": 0}


def test_editing_a_box_writes_into_the_selected_layer_only():
    store = {"arb_alert_layers": json.dumps([
        {"name": "a", "on": True, "g": {"edge": 10}},
        {"name": "b", "on": True, "g": {"edge": 15}},
    ]), "arbs_alert_layer_sel": "1"}
    res = run_panel(store, after="""
      document.getElementById("alert-threshold").value = "22";
      syncAlertCfg();
      out = loadLayers().map(l => l.g.edge);
    """)
    assert res["out"] == [10, 22]
    assert [l["g"]["edge"] for l in json.loads(res["store"]["arb_alert_layers"])] == [10, 22]


def test_clearing_a_box_turns_that_gate_off_rather_than_zero():
    store = {"arb_alert_layers": json.dumps(
        [{"name": "a", "on": True, "g": {"edge": 10}}])}
    res = run_panel(store, after="""
      document.getElementById("alert-threshold").value = "";
      syncAlertCfg();
      out = loadLayers()[0].g.edge;
    """)
    assert res["out"] is None


def test_the_gap_warning_reaches_the_hint_element():
    """Detection is unit-tested elsewhere; this is that it is actually shown."""
    store = {"arb_alert_layers": json.dumps([
        {"name": "short", "on": True, "g": {"oddsMin": 1, "oddsMax": 3}},
        {"name": "long", "on": True, "g": {"oddsMin": 4, "oddsMax": 5}},
    ])}
    res = run_panel(store, after='out = document.getElementById("layer-hint").textContent;')
    assert "3–4" in res["out"] and "⚠" in res["out"], res["out"]


def test_the_closed_summary_lists_every_active_layer():
    store = {"alert_enabled": "1", "arb_alert_layers": json.dumps([
        {"name": "short", "on": True, "g": {"edge": 10, "oddsMax": 3}},
        {"name": "long", "on": True, "g": {"edge": 15, "oddsMin": 4}},
        {"name": "parked", "on": False, "g": {"edge": 1}},
    ])}
    res = run_panel(store, after='out = document.getElementById("alert-cfg-summary").textContent;')
    s = res["out"]
    assert "short" in s and "long" in s
    assert "parked" not in s, "an inactive layer is advertised as live"


def test_names_are_referenced_by_the_wiring_guard():
    """Keeps this file honest: if the extraction markers move, the test above
    silently tests nothing, so assert the markers exist."""
    assert "// ── Advanced alert config" in ARBS_T
    assert re.search(r"\nrestoreAlertCfg\(\);", ARBS_T)


# ── the panel is findable at all ─────────────────────────────────────────────
# Reported 2026-08-16: "I dont see any changes in ui". The layer bar had shipped
# correctly but lives inside a <details>, which defaults to closed and whose
# state browsers do not persist — so every reload hid the entire config and a
# shipped feature looked like it had not shipped.

@pytest.mark.parametrize("page", ["arbs.html", "anomalies.html"])
def test_the_alert_panel_remembers_being_open(page):
    # The Anomalies-style panel moved its behaviour into /alert-panel.js on
    # 2026-09-24 so the New inconsistencies tab could share it; the markup
    # still lives on each page. Read both where the page loads the module.
    t = (ARBS.parent / page).read_text(encoding="utf-8")
    assert 'id="alert-config"' in t, f"{page}: no alert panel"
    if "alert-panel.js" in t:
        t += "\n" + (ARBS.parent / "alert-panel.js").read_text(encoding="utf-8")
    assert "rememberPanelOpen" in t, (
        f"{page}: the panel does not persist its open state — every reload "
        "collapses it and hides the settings")
    # Must both restore and record, or it only works in one direction.
    assert re.search(r"d\.open\s*=\s*localStorage\.getItem", t), \
        f"{page}: open state is saved but never restored"
    assert re.search(r'addEventListener\("toggle"', t), \
        f"{page}: open state is restored but never saved"
