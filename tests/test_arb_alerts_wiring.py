"""The Arbs alert panel writes settings that alerts.js reads (2026-08-12).

Same contract, and the same failure mode, as tests/test_anomaly_alerts_wiring.py:
arbs.html and alerts.js talk ONLY through localStorage key strings, and a typo
on either side fails in the worst possible way — the panel looks like it saved
and the chime simply never fires. Nothing at runtime complains.

The panel replaced a single "Alert ≥ N%" box. The reason is worth keeping: an
edge PERCENTAGE says nothing about whether a row is bettable. The largest
percentages sit on the longest prices, which is exactly where Pinnacle's fair
number is least certain and where the Kelly stake is smallest — so the old alert
was loudest where it was least useful. The gates below are what makes it
selective.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGE = STATIC / "arbs.html"
ALERTS = STATIC / "alerts.js"

PAGE_T = PAGE.read_text(encoding="utf-8")
ALERTS_T = ALERTS.read_text(encoding="utf-8")

# Every key the panel writes and the poller must read.
SHARED_KEYS = [
    "alert_enabled",
    "alert_threshold",
    "arb_alert_pp",
    "arb_alert_step",
    "arb_alert_odds_min",
    "arb_alert_odds_max",
    "arb_alert_kelly_min",
    "arb_alert_kelly_max",
    "arb_alert_pin_max_stake",
    "arb_alert_lead_min",
    "arb_alert_lead_max_h",
    "arb_alert_kinds",
    "arb_alert_conf",
    "arb_alert_sports",
    "arb_alert_books",
    "arb_alert_markets",
    "arb_alert_periods",
]

# id → the criterion it configures. Every one must exist AND be read.
CONTROL_IDS = [
    "alert-threshold", "alert-pp", "alert-step",
    "alert-odds-min", "alert-odds-max",
    "alert-kelly-min", "alert-kelly-max",
    "alert-pinmax", "alert-lead-min", "alert-lead-max",
]
CHIP_GRIDS = [
    "alert-kinds", "alert-conf", "alert-sports",
    "alert-books", "alert-markets", "alert-periods",
]


# ── the contract between the panel and the poller ────────────────────────────

@pytest.mark.parametrize("key", SHARED_KEYS)
def test_key_is_written_by_the_panel(key):
    assert f'"{key}"' in PAGE_T, (
        f"arbs.html never references {key} — the panel cannot save it")


@pytest.mark.parametrize("key", SHARED_KEYS)
def test_key_is_read_by_the_poller(key):
    assert f'"{key}"' in ALERTS_T, (
        f"alerts.js never references {key} — the panel writes a setting that "
        "nothing reads, so that criterion silently does nothing")


def test_no_orphan_arb_alert_keys_in_either_file():
    """Catches a rename applied to one file only: any arb_alert_* key must
    appear in BOTH."""
    pat = re.compile(r'"(arb_alert_[a-z_]+)"')
    page_keys = set(pat.findall(PAGE_T))
    alert_keys = set(pat.findall(ALERTS_T))
    assert page_keys - alert_keys == set(), (
        f"written by the panel but never read: {sorted(page_keys - alert_keys)}")
    assert alert_keys - page_keys == set(), (
        f"read by alerts.js but never written: {sorted(alert_keys - page_keys)}")


# ── the panel is actually wired to the DOM ───────────────────────────────────

@pytest.mark.parametrize("el_id", CONTROL_IDS)
def test_number_control_exists_and_is_read(el_id):
    """Since layers (2026-08-16) the controls are read through the NUM_GATES
    table rather than one getElementById per box, so membership of that table IS
    being wired: it drives both restore and save."""
    assert f'id="{el_id}"' in PAGE_T, f"no element with id={el_id}"
    assert f'"{el_id}"' in _block(PAGE_T, "const NUM_GATES = {"), (
        f"{el_id} is rendered but not in NUM_GATES — the control does nothing")


@pytest.mark.parametrize("el_id", CHIP_GRIDS)
def test_chip_grid_exists_and_is_populated(el_id):
    assert f'id="{el_id}"' in PAGE_T, f"no element with id={el_id}"
    assert f'"{el_id}"' in PAGE_T


def test_controls_persist_on_change():
    """Without a change listener the panel is decorative."""
    assert 'addEventListener("change", syncAlertCfg)' in PAGE_T, (
        "no control persists its value — settings are lost on reload")


def test_every_number_control_is_in_the_save_map():
    """A control the save map forgets looks live and silently resets on reload."""
    block = _block(PAGE_T, "const NUM_GATES = {")
    for el_id in CONTROL_IDS:
        assert f'"{el_id}"' in block, (
            f"{el_id} is not in NUM_GATES — it will never be saved or restored")


def test_every_chip_group_is_declared_with_seed_options():
    block = _block(PAGE_T, "const SET_GATES = {")
    for el_id in CHIP_GRIDS:
        assert f'"{el_id}"' in block, f"{el_id} missing from SET_GATES"


# ── the gates themselves ─────────────────────────────────────────────────────

def test_poller_evaluates_every_gate():
    """Each configured criterion must be consumed by passesGates — a key that is
    read into the gate object but never tested is a setting that does nothing."""
    gates = _block(ALERTS_T, "function readOppGates()")
    body = _block(ALERTS_T, "function passesGates(o, g)")
    for field in ("edge", "pp", "oddsMin", "oddsMax", "kellyMin", "kellyMax",
                  "pinMax", "leadMin", "leadMaxH", "kinds", "sports", "books",
                  "markets", "periods"):
        assert field in gates, f"{field} missing from readOppGates"
        assert f"g.{field}" in body, f"passesGates never checks g.{field}"


def test_blank_threshold_means_off_not_zero():
    """A blank box must disable that criterion. Treating it as 0 would fire on
    every row — the opposite of what emptying a field implies."""
    body = _block(ALERTS_T, "function cfgNum")
    assert "return null" in body and '=== ""' in body


def test_empty_chip_selection_means_all_not_none():
    """Unselecting every chip must WIDEN the alert, not silence it — silencing
    is what the master switch is for. It also means a sport or book added to the
    backend later keeps alerting instead of quietly dropping out."""
    body = _block(ALERTS_T, "function cfgSet(key)")
    assert "return null" in body
    save = _block(PAGE_T, "function saveChipSel(field, set)")
    assert "size === 0" in save


def test_pp_edge_is_probability_points_not_percent():
    """The whole point of the pp gate: it must be a difference of implied
    probabilities, not a ratio of prices. 1/fair − 1/book, times 100."""
    body = _block(ALERTS_T, "function ppEdge(o)")
    assert "1 / fair - 1 / book" in body.replace("  ", " "), (
        "ppEdge is not computing a probability difference")


def test_pp_edge_ranks_a_short_price_above_a_longshot():
    """A worked example from the docstring, asserted rather than trusted:
    10.0 vs an 8.0 fair is +25 % but 2.50pp; 2.0 vs a 1.9 fair is +5.3 % but
    2.63pp. Percent and pp disagree on which is better, and pp is right."""
    long_pp = (1 / 8.0 - 1 / 10.0) * 100
    short_pp = (1 / 1.9 - 1 / 2.0) * 100
    long_pct = (10.0 / 8.0 - 1) * 100
    short_pct = (2.0 / 1.9 - 1) * 100
    assert long_pct > short_pct        # percent prefers the longshot
    assert short_pp > long_pp          # probability points prefer the short price


def test_arb_rows_are_exempt_from_the_kelly_floor():
    """edge.py leaves ARB staking to the bettor, so kelly_stake is 0 on every
    ARB row. A Kelly floor that applied to them would silence arbs entirely —
    the exact class of bug this file exists to catch."""
    body = _block(ALERTS_T, "function passesGates(o, g)")
    assert 'o.kind !== "ARB"' in body, (
        "the Kelly gate is not exempting ARB rows, which always have kelly 0")


def test_missing_pin_limit_does_not_fail_a_row():
    """pin_max_stake is absent on books/markets where Pinnacle ships no limits.
    Absent means unknown, not zero — treating it as zero would silence every
    such row the moment a limit floor is set."""
    body = _block(ALERTS_T, "function passesGates(o, g)")
    assert "o.pin_max_stake != null" in body


def test_weak_pairings_stay_excluded_by_default():
    """The 2026-07-26 audit finding (big edges are overwhelmingly weak) must
    survive becoming configurable: with no confidence selection saved, the
    behaviour has to be exactly what it was before the panel."""
    assert 'conf !== "weak"' in ALERTS_T, (
        "the default no longer excludes weak pairings")


def test_re_alert_step_is_configurable_but_defaults_to_the_old_constant():
    assert "DEFAULT_RE_ALERT_PP = 5" in ALERTS_T
    assert "reAlertPp" in ALERTS_T
    assert "o.edge_pct - prevEdge >= 5" not in ALERTS_T, (
        "the re-alert step is still hardcoded next to the configurable one")


def test_query_floor_widens_for_a_sub_one_percent_edge_gate():
    """The server pre-filters at min_edge, so an edge gate below 1 % would ask
    about rows that never arrive — panel configured, alert silently dead. With
    layers the floor must come from the most generous ACTIVE layer, which is why
    it moved into its own function; the poll must actually use it."""
    body = _block(ALERTS_T, "function oppQueryFloor(layers)")
    assert "< 1" in body and "Math.min" in body, (
        "oppQueryFloor no longer takes the lowest edge across layers")
    poll = _block(ALERTS_T, "async function poll()")
    assert "oppQueryFloor(layers)" in poll and "min_edge=" in poll


def test_gates_are_read_fresh_every_poll():
    """Config changes must take effect on the next cycle without a reload."""
    body = _block(ALERTS_T, "async function poll()")
    assert "readLayers()" in body


def test_the_master_switch_still_gates_everything():
    body = _block(ALERTS_T, "async function poll()")
    assert "ENABLED_KEY" in body and "enabled" in body


def _block(text: str, start: str) -> str:
    """The source from `start` to the first line that closes it at indent 0-2.
    Crude, but these are small well-formatted functions/objects."""
    i = text.index(start)
    for terminator in ("\n  }", "\n}", "\n};"):
        j = text.find(terminator, i)
        if j != -1:
            return text[i:j]
    return text[i:]
