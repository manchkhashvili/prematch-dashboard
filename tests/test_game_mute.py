"""Muting a game silences its chimes on BOTH findings boards.

Owner (2026-09-24): "add button on anomalies tabs to mute games for alerts so
they wont refire alerts anomalies and new inconsistencies".

Two things have to hold and neither is visible at runtime if it breaks:
the button has to write the key the poller reads, and a muted row has to stay
in the seen-map rather than be dropped — otherwise un-muting retro-fires every
finding that arrived while the game was quiet.
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
ALERTS_T = (STATIC / "alerts.js").read_text(encoding="utf-8")
PAGES = ["anomalies.html", "new_inconsistencies.html"]


# ── the contract between the button and the poller ───────────────────────────

def test_both_sides_use_the_same_storage_key():
    """A typo here leaves the button looking like it worked while every chime
    keeps firing — the same silent failure as every other key in this chain."""
    assert '"findings_muted_games_v1"' in PANEL_T, "the panel writes no mute list"
    assert '"findings_muted_games_v1"' in ALERTS_T, (
        "alerts.js reads no mute list — the Mute button would do nothing")


def test_both_sides_build_the_same_game_key():
    """`sport|match_label`, lowercased. It is the only identity both sides can
    build: the compact alert feeds carry no event id the tables share."""
    pat = r'\(row\.sport \|\| ""\) \+ "\|" \+\s*\n?\s*String\(row\.match_label \|\| ""\)\.trim\(\)\.toLowerCase\(\)'
    for name, src in (("alert-panel.js", PANEL_T), ("alerts.js", ALERTS_T)):
        assert re.search(pat, src), f"{name} builds a different mute key"


def test_the_mute_is_keyed_by_game_not_by_row():
    """A row-level mute would not hold: the checks re-fire under a different
    kind, period or line as prices move, so each new row would chime again on
    a fixture that had already been dismissed."""
    i = ALERTS_T.index("function gameMuteKey")
    body = ALERTS_T[i:ALERTS_T.index("\n  }", i)]
    for field in ("kind", "periods", "line_lo", "market", "outcome"):
        assert field not in body, f"the mute key includes {field} — it is per row"


def test_a_muted_row_is_still_tracked_by_the_seen_map():
    """It must fail the PREDICATE, not be filtered out of the feed. Dropping
    it would make un-muting retro-fire everything that arrived meanwhile."""
    i = ALERTS_T.index("const unmuted =")
    body = ALERTS_T[i:i + 300]
    assert "passes(row)" in body, "the mute does not compose with the predicate"
    assert "filter" not in body, (
        "muted rows are filtered out of the feed rather than failed — they "
        "would never enter the seen-map, so un-muting fires them all at once")


def test_the_mute_list_is_shared_by_both_boards():
    """Unlike the thresholds, which are per board. A game you have dismissed
    is dismissed; muting it twice, once per tab, would be a bug."""
    i = PANEL_T.index('const MUTED_KEY')
    line = PANEL_T[i:PANEL_T.index("\n", i)]
    assert "prefix" not in line, "the mute list is per board — it must be shared"


# ── the button is on every row of every table ────────────────────────────────

@pytest.mark.parametrize("page", PAGES)
def test_both_tables_render_the_button(page):
    t = (STATIC / page).read_text(encoding="utf-8")
    assert t.count("Mutes.buttonHTML(") == 2, (
        f"{page}: expected the button in both the ladder and consistency rows")
    assert t.count("Mutes.bind(") == 2, (
        f"{page}: a table renders the button but never binds the click")
    assert t.count("Mutes.rowClass(") == 2, f"{page}: a muted row is not marked"


# ── it actually works ────────────────────────────────────────────────────────

STUB = r"""
const store = __STORE__;
globalThis.localStorage = {
  getItem: k => (k in store ? String(store[k]) : null),
  setItem: (k, v) => { store[k] = String(v); },
};
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
"""


def run(js: str, store: dict | None = None) -> dict:
    src = (STUB.replace("__STORE__", json.dumps(store or {}))
           + "\n" + PANEL_T + "\nconst M = AlertPanel.Mutes;\n" + js
           + "\nconsole.log('\\u0001' + JSON.stringify({store, out}));")
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:1200]
    for line in r.stdout.splitlines():
        if line.startswith("\u0001"):
            return json.loads(line[1:])
    raise AssertionError(r.stdout + r.stderr)


ROW = '{sport: "soccer", match_label: "Lech Poznan \\u2014 Staszkowka"}'


def test_toggling_stores_and_clears_the_game():
    r = run(f"M.toggle(M.key({ROW})); const out = M.has({ROW});")
    assert r["out"] is True
    assert r["store"]["findings_muted_games_v1"] == '["soccer|lech poznan — staszkowka"]'
    r = run(f"M.toggle(M.key({ROW})); M.toggle(M.key({ROW})); const out = M.has({ROW});")
    assert r["out"] is False


def test_the_same_game_is_muted_whatever_the_finding_on_it():
    """The point of keying by game: a ladder row and a consistency flag on one
    fixture resolve to one key."""
    lad = '{sport:"soccer", match_label:"A \\u2014 B", market:"spread", period:"FT"}'
    con = '{sport:"soccer", match_label:"A \\u2014 B", kind:"htft_combo", periods:"H1+FT"}'
    r = run(f"M.toggle(M.key({lad})); const out = [M.has({lad}), M.has({con})];")
    assert r["out"] == [True, True]


def test_a_different_game_is_untouched():
    other = '{sport:"soccer", match_label:"C \\u2014 D"}'
    r = run(f"M.toggle(M.key({ROW})); const out = M.has({other});")
    assert r["out"] is False


def test_the_button_reads_back_its_state():
    r = run(f"M.toggle(M.key({ROW})); const out = M.buttonHTML({ROW});")
    assert 'class="mute-btn muted"' in r["out"] and ">Muted<" in r["out"]
    r = run(f"const out = M.buttonHTML({ROW});")
    assert 'class="mute-btn"' in r["out"] and ">Mute<" in r["out"]


def test_a_corrupt_mute_list_does_not_take_the_board_down():
    r = run("const out = M.load().size;", {"findings_muted_games_v1": "{not json"})
    assert r["out"] == 0


# ── the poller's own rules, driven under node ────────────────────────────────

from tests.jsrun import run_js  # noqa: E402


MUTE_KEY = "findings_muted_games_v1"
LAD = {"sport": "soccer", "match_label": "A — B", "pct": 30.0,
       "odds_lo": 1.9, "odds_hi": 2.0}


def test_the_poller_builds_the_key_the_button_wrote():
    got = run_js(f"out = A.gameMuteKey({json.dumps(LAD)});")
    assert got == "soccer|a — b"


def test_the_poller_reads_the_list_the_button_wrote():
    got = run_js("out = [...A.loadMutedGames()];",
                 {MUTE_KEY: json.dumps(["soccer|a — b"])})
    assert got == ["soccer|a — b"]


def test_an_unmuted_game_still_fires():
    """Guards against the mute silencing everything — the failure that would
    look like 'alerts stopped working' rather than 'the mute is too wide'."""
    cfg = {"anom_ladder_alert_pct": "10"}
    n = run_js(
        "const seen = new Map();"
        f"const muted = A.loadMutedGames();"
        f"const rows = [{json.dumps(LAD)}].map(r => {{ r.__key = A.ladderKey(r); return r; }});"
        "out = A.evaluateFeed(rows, seen,"
        "  r => !muted.has(A.gameMuteKey(r)) && A.ladderPasses(r),"
        "  r => r.pct, 'seeded');", {**cfg, "seeded": "1"})
    assert n == 1


def test_a_muted_game_does_not_fire_but_is_remembered():
    """Both halves matter. Silence is the feature; being remembered is what
    stops un-muting from retro-firing everything that arrived meanwhile."""
    cfg = {"anom_ladder_alert_pct": "10", "seeded": "1",
           MUTE_KEY: json.dumps(["soccer|a — b"])}
    got = run_js(
        "const seen = new Map();"
        "const muted = A.loadMutedGames();"
        f"const rows = [{json.dumps(LAD)}].map(r => {{ r.__key = A.ladderKey(r); return r; }});"
        "const n = A.evaluateFeed(rows, seen,"
        "  r => !muted.has(A.gameMuteKey(r)) && A.ladderPasses(r),"
        "  r => r.pct, 'seeded');"
        "out = [n, seen.size];", cfg)
    assert got[0] == 0, "a muted game chimed"
    assert got[1] == 1, (
        "the muted row never entered the seen-map — un-muting would fire every "
        "finding that appeared while the game was quiet")


def test_both_boards_read_one_mute_list():
    """anom_ and lsp_ have their own thresholds and seen-maps; the mute list is
    deliberately NOT prefixed."""
    keys = run_js('out = [A.anomKeys("anom_").LAD_SEEN_KEY,'
                  '       A.anomKeys("lsp_").LAD_SEEN_KEY, A.MUTED_GAMES_KEY];')
    assert keys[0] != keys[1], "the boards share a seen-map"
    assert keys[2] == MUTE_KEY and not keys[2].startswith(("anom_", "lsp_"))
