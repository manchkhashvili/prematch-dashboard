"""The shared alert panel RUNS, on both boards, writing each board's own keys.

Extracted from anomalies.html on 2026-09-24 when the owner asked for the same
settings on the New inconsistencies tab. Parsing the files proves the key names
line up; it does not prove the panel executes. That matters more here than
usual, because the panel runs inside each page's single inline <script> — an
exception in it kills every line after it, including the table render — and
because this exact chain has shipped broken three separate ways already: a
dead feed wiring, a ConsistencyFlag with no `odds` field for the band to read,
and defaults that were DISPLAYED but never persisted.

So this executes the real module under a stub DOM and reads what it wrote.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.jsrun import NODE

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

STATIC = Path(__file__).resolve().parent.parent / "static"
PANEL_T = (STATIC / "alert-panel.js").read_text(encoding="utf-8")

STUB = r"""
const store = __STORE__;
globalThis.localStorage = {
  getItem: k => (k in store ? String(store[k]) : null),
  setItem: (k, v) => { store[k] = String(v); },
};
function mkEl(id) {
  const cls = new Set();
  return {
    id, value: "", checked: false, textContent: "", _html: "", open: false,
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; },
    classList: { add: c => cls.add(c), remove: c => cls.delete(c),
                 contains: c => cls.has(c),
                 toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); } },
    addEventListener(ev, fn) { (this._on = this._on || {})[ev] = fn; },
    fire(ev) { if (this._on && this._on[ev]) this._on[ev](); },
  };
}
const els = {};
globalThis.document = {
  getElementById(id) { return (els[id] = els[id] || mkEl(id)); },
  querySelectorAll() { return []; },
};
globalThis.window = globalThis;
"""


def run_panel(prefix: str, store: dict | None = None, after: str = ""):
    src = (STUB.replace("__STORE__", json.dumps(store or {}))
           + "\n" + PANEL_T
           + f'\nAlertPanel.init("{prefix}");\n'
           + after
           + "\nconsole.log('\\u0001' + JSON.stringify({store, els: Object.keys(els)}));\n")
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, f"the panel threw at init:\n{r.stderr}"
    for line in r.stdout.splitlines():
        if line.startswith("\u0001"):
            return json.loads(line[1:])
    raise AssertionError(f"no result:\n{r.stdout}\n{r.stderr}")


# ── it runs ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["anom_", "lsp_"])
def test_init_does_not_throw_on_a_clean_store(prefix):
    """A first-ever visit. If this throws, every line after AlertPanel.init()
    in the page's inline script never executes — including the tables."""
    run_panel(prefix)


@pytest.mark.parametrize("prefix", ["anom_", "lsp_"])
def test_the_displayed_defaults_are_persisted(prefix):
    """The bug of 2026-09-05, which must not come back on a second board: the
    panel SHOWED 'sev >= 12' while localStorage held nothing, so the poller's
    cfgNum() returned null and consPasses rejected every row. Both alert kinds
    were dead and the panel looked configured the whole time."""
    r = run_panel(prefix)
    assert r["store"].get(prefix + "cons_alert_default") == "12"
    assert r["store"].get(prefix + "ladder_alert_pct") == "10"


@pytest.mark.parametrize("prefix", ["anom_", "lsp_"])
def test_seeding_never_overwrites_a_deliberate_choice(prefix):
    r = run_panel(prefix, {prefix + "cons_alert_default": "40"})
    assert r["store"][prefix + "cons_alert_default"] == "40"


# ── the two boards are genuinely separate ────────────────────────────────────

def test_one_board_writes_nothing_under_the_other_prefix():
    """The whole point of the prefix. If the LSport panel wrote anom_* keys,
    changing a bar on one tab would silently change the other."""
    r = run_panel("lsp_")
    stray = [k for k in r["store"] if k.startswith("anom_")]
    assert not stray, f"the LSport panel wrote Anomalies keys: {stray}"
    assert any(k.startswith("lsp_") for k in r["store"])


def test_anom_reproduces_the_names_that_already_shipped():
    """Existing users have settings saved under these exact names. A prefix
    typo would not error — it would quietly reset everyone's thresholds."""
    r = run_panel("anom_")
    for key in ("anom_cons_alert_default", "anom_ladder_alert_pct"):
        assert key in r["store"], f"{key} is no longer what the panel writes"


# ── editing persists ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["anom_", "lsp_"])
def test_every_control_persists_on_change(prefix):
    """The four odds boxes were once bound to nothing: syncAlertCfg wrote them
    but no listener called it, so typing a veto and clicking away saved
    nothing until some OTHER control was touched."""
    edits = """
    els["cons-alert-max-odds"].value = "9";
    els["cons-alert-min-odds"].value = "1.3";
    els["lad-alert-max-odds"].value = "8";
    els["lad-alert-min-odds"].value = "1.1";
    els["cons-alert-on"].checked = true;
    els["cons-alert-max-odds"].fire("change");
    """
    r = run_panel(prefix, after=edits)
    s = r["store"]
    assert s[prefix + "cons_alert_max_odds"] == "9"
    assert s[prefix + "cons_alert_min_odds"] == "1.3"
    assert s[prefix + "ladder_alert_max_odds"] == "8"
    assert s[prefix + "ladder_alert_min_odds"] == "1.1"
    assert s[prefix + "cons_alert_enabled"] == "1"


@pytest.mark.parametrize("prefix", ["anom_", "lsp_"])
def test_the_per_check_grid_is_drawn(prefix):
    r = run_panel(prefix)
    assert "cons-kind-grid" in r["els"], "the per-check grid element was never touched"
