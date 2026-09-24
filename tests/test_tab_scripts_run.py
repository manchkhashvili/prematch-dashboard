"""The Anomalies and New inconsistencies pages actually RENDER.

2026-09-24: moving the alert panel into /alert-panel.js left `consClass`
defined nowhere — it had lived in the block that moved. Nothing failed to
parse. The page loaded, fetched fine, and threw a ReferenceError inside
renderConsistency, where the catch turned it into "could not reach the
server (anomalies)". A server-side error message for a client-side typo, and
both tables dead.

Grepping for the symbols I remembered had missed it, which is the lesson: the
only check that covers a moved block is running the thing. So this evaluates
each page's real inline script against a stub DOM, renders one row into each
table, and asserts both tables came out non-empty.

Verified to catch the original bug: deleting the consClass line makes the
Anomalies case fail with `ReferenceError: consClass is not defined`.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.jsrun import NODE

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# Records what the page did rather than emulating a browser. Anything the page
# touches that is NOT stubbed throws, which is the point — a permissive stub
# would hide exactly the breakage this file exists to catch.
STUB = r"""
const store = {};
globalThis.localStorage = {
  getItem: k => (k in store ? String(store[k]) : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
function mkEl(id) {
  const cls = new Set();
  return { id, value: "", checked: false, textContent: "", _html: "", open: false,
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; },
    classList: { add: c => cls.add(c), remove: c => cls.delete(c),
                 contains: c => cls.has(c),
                 toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); } },
    addEventListener() {}, querySelectorAll() { return []; },
    closest() { return null; }, appendChild(c) { return c; },
    setAttribute() {}, getAttribute() { return null; } };
}
const els = {};
globalThis.document = {
  getElementById: id => (els[id] = els[id] || mkEl(id)),
  querySelectorAll: () => [], querySelector: () => mkEl("?"),
  createElement: t => mkEl("<" + t + ">"), addEventListener() {}, body: mkEl("body"),
};
globalThis.window = globalThis;
// The page schedules its pollers at load; let them be declared, never run.
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = () => Promise.reject(new Error("no network in this harness"));
globalThis.Marks = { load: () => Promise.resolve(), key: () => "k",
                     rowClass: () => "", buttonHTML: () => "", bind() {},
                     get: () => null };
"""

# One row of each shape the two tables render, built from the fields the
# server actually sends (see _consistency_to_dict and the ladder rows).
PROBE = r"""
const __flag = { book: "cb", sport: "soccer", match_label: "A — B",
  league: "L", kind: "ml_vs_spread", periods: "FT", detail: "d",
  severity: 13.5, odds: 2.0, first_seen: new Date().toISOString() };
const __lad = { book: "cb", sport: "soccer", league: "L",
  match_label: "A — B", market: "spread", period: "FT", side: "home",
  line_lo: -1, line_hi: -1.5, odds_lo: 1.9, odds_hi: 2.0, pct: 12,
  start_time: new Date().toISOString(), cb_event_id: "1" };
meta = { enabled: true, computed_at: new Date().toISOString(),
         coverage: {}, count: 1 };
rows = [__lad];
renderConsistency([__flag]);
render();
"""


def render_page(page: str) -> dict:
    """Evaluate the page's inline script and render one row into each table."""
    html = (STATIC / page).read_text(encoding="utf-8")
    inline = re.search(r"<script>\n([\s\S]*?)\n</script>", html)
    assert inline, f"{page}: no inline <script> block"
    panel = (STATIC / "alert-panel.js").read_text(encoding="utf-8")
    src = (STUB
           + "\n" + panel
           + "\nconst __page = " + json.dumps(inline.group(1) + PROBE) + ";"
           # ONE eval, so the probe shares scope with the page's own `let`
           # bindings — `meta` and `rows` are page-scoped and unreachable from
           # outside an eval.
           + "\neval(__page);"
           + "\nconsole.log('\\u0001' + JSON.stringify({"
             "cons: els['cons-body'] ? els['cons-body']._html : null,"
             "lad: els['anom-body'] ? els['anom-body']._html : null}));"
           + "\nprocess.exit(0);")
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, f"{page} threw while rendering:\n{r.stderr[:1500]}"
    for line in r.stdout.splitlines():
        if line.startswith("\u0001"):
            return json.loads(line[1:])
    raise AssertionError(f"{page}: no result\n{r.stdout}\n{r.stderr}")


PAGES = ["anomalies.html", "new_inconsistencies.html"]


@pytest.mark.parametrize("page", PAGES)
def test_the_page_script_runs_and_renders_both_tables(page):
    out = render_page(page)
    assert out["cons"], f"{page}: the consistency table rendered nothing"
    assert out["lad"], f"{page}: the ladder table rendered nothing"


@pytest.mark.parametrize("page", PAGES)
def test_the_consistency_row_carries_its_severity(page):
    """Proves the row template ran to completion rather than bailing to an
    empty-state placeholder — which is what a thrown ReferenceError looks
    like from outside."""
    out = render_page(page)
    assert "13.5" in out["cons"], (
        f"{page}: the severity never reached the row — the table fell back to "
        f"a placeholder: {out['cons'][:160]}")


@pytest.mark.parametrize("page", PAGES)
def test_the_ladder_row_carries_its_match(page):
    out = render_page(page)
    assert "A — B" in out["lad"], (
        f"{page}: the ladder row never rendered: {out['lad'][:160]}")
