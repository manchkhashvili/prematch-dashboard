"""The Anomalies alert panel writes settings that alerts.js reads (2026-08-10).

The two files talk to each other ONLY through localStorage key strings, and a
typo on either side fails silently in the worst possible way: the panel looks
like it saved, and the alert simply never fires. Nothing at runtime complains.
So the key names are pinned statically here, in both directions.

The same reasoning as tests/test_marks_wiring.py — that feature shipped
half-wired precisely because a page can look correct while doing nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGE = STATIC / "anomalies.html"
ALERTS = STATIC / "alerts.js"

PAGE_T = PAGE.read_text(encoding="utf-8")
ALERTS_T = ALERTS.read_text(encoding="utf-8")

# Every key the panel writes and the poller must read.
SHARED_KEYS = [
    "anom_ladder_alert_enabled",
    "anom_ladder_alert_pct",
    "anom_ladder_alert_delta",
    "anom_ladder_alert_step",
    "anom_cons_alert_enabled",
    "anom_cons_alert_default",
    "anom_cons_alert_kinds",
    # Odds vetoes (2026-08-27). Same failure mode as every key above: a typo
    # leaves the panel saving a cap that nothing enforces, and the user sees
    # longshot chimes they thought they had switched off.
    "anom_ladder_alert_max_odds",
    "anom_cons_alert_max_odds",
    # The floor (2026-08-29). Owner: "most of them are 1.02 1.05 7-8 odds and
    # they are noise". The cap only ever handled one end of that.
    "anom_ladder_alert_min_odds",
    "anom_cons_alert_min_odds",
]


# ── the contract between the panel and the poller ────────────────────────────

@pytest.mark.parametrize("key", SHARED_KEYS)
def test_key_is_written_by_the_panel(key):
    assert f'"{key}"' in PAGE_T, (
        f"anomalies.html never references {key} — the panel cannot save it")


@pytest.mark.parametrize("key", SHARED_KEYS)
def test_key_is_read_by_the_poller(key):
    assert f'"{key}"' in ALERTS_T, (
        f"alerts.js never references {key} — the panel writes a setting that "
        "nothing reads, so the alert silently never fires")


def test_no_orphan_anom_alert_keys_in_either_file():
    """Catches a rename applied to one file only: any anom_*alert* key must
    appear in BOTH. Seen-set keys are poller-private and excluded."""
    pat = re.compile(r'"(anom_[a-z_]*alert[a-z_]*)"')
    page_keys = set(pat.findall(PAGE_T))
    alert_keys = {k for k in pat.findall(ALERTS_T)}
    assert page_keys - alert_keys == set(), (
        f"keys written by the panel but not read by alerts.js: "
        f"{sorted(page_keys - alert_keys)}")
    assert alert_keys - page_keys == set(), (
        f"keys read by alerts.js but never written by the panel: "
        f"{sorted(alert_keys - page_keys)}")


# ── the panel is actually wired to the DOM ───────────────────────────────────

CONTROL_IDS = [
    "lad-alert-on", "lad-alert-pct", "lad-alert-delta", "lad-alert-step",
    "cons-alert-on", "cons-alert-default", "cons-kind-grid",
]


@pytest.mark.parametrize("el_id", CONTROL_IDS)
def test_control_exists_and_is_referenced(el_id):
    assert f'id="{el_id}"' in PAGE_T, f"no element with id={el_id}"
    assert f'getElementById("{el_id}")' in PAGE_T, (
        f"{el_id} is rendered but never read — the control does nothing")


def test_controls_persist_on_change():
    """Without a change listener the panel is decorative."""
    assert "addEventListener(\"change\", syncAlertCfg)" in PAGE_T, (
        "no control persists its value — settings are lost on reload")


def test_per_check_grid_is_built_from_the_rendered_kind_labels():
    """The per-check rows must come from KIND_LABEL, so a detector added to the
    table cannot be missing from the alert config (or vice versa)."""
    assert "Object.keys(KIND_LABEL)" in PAGE_T, (
        "the per-check grid is not derived from KIND_LABEL — a new check would "
        "render in the table but be unconfigurable")


def test_every_kind_has_a_severity_unit():
    """Severity is pp for some checks, points for others, % for the HT/FT ones.
    A missing unit renders a blank label next to a number that means nothing."""
    labels = set(re.findall(r"^\s{2}(\w+):\s*\"", _block(PAGE_T, "const KIND_LABEL"), re.M))
    units = set(re.findall(r"(\w+):\s*\"(?:pp|pts|%)\"", _block(PAGE_T, "const KIND_UNIT")))
    assert labels, "could not parse KIND_LABEL"
    missing = labels - units
    assert not missing, f"checks with no severity unit in KIND_UNIT: {sorted(missing)}"


def _block(text: str, start: str) -> str:
    i = text.index(start)
    return text[i:text.index("};", i)]


# ── the poller path exists and is scheduled ──────────────────────────────────

def test_poller_is_registered_on_an_interval():
    assert "setInterval(pollAnomalyFindings" in ALERTS_T, (
        "pollAnomalyFindings is defined but never scheduled — it would run "
        "at most once, or not at all")


def test_poller_uses_the_cheap_endpoint():
    """/api/anomalies runs the fuzzy matcher to attach Pinnacle prices. Polling
    it every 30s from every tab is exactly the CPU burn the paused-memo work
    went to remove."""
    assert '"/api/anomalies/alerts"' in ALERTS_T
    assert 'fetch("/api/anomalies")' not in ALERTS_T, (
        "the alert poller must not hit the enrichment endpoint")


def test_ladder_and_consistency_have_distinct_sounds():
    for fn in ("playLadderAlert", "playConsistencyAlert"):
        assert f"function {fn}(" in ALERTS_T, f"{fn} missing"
    assert "playLadderAlert()" in ALERTS_T and "playConsistencyAlert()" in ALERTS_T


def test_each_sound_claims_a_distinct_channel():
    """claimSound() de-duplicates across tabs per kind. Reusing another alert's
    channel name would let an opportunity alert mute a ladder alert."""
    kinds = set(re.findall(r'claimSound\("([^"]+)"\)', ALERTS_T))
    assert {"anom-ladder", "anom-cons"} <= kinds
    assert len(kinds) == len(set(kinds))
    # The pre-existing three must still be there and unshared.
    assert {"opps", "moves", "scan"} <= kinds


def test_seeded_silently_on_first_pass():
    """Turning the alert on must not blast one chime per existing finding."""
    assert "LAD_SEEDED_KEY" in ALERTS_T and "CONS_SEEDED_KEY" in ALERTS_T
    assert "isFirstPass" in ALERTS_T


def test_collapse_guard_is_applied_to_the_new_feeds():
    """Same failure mode the opportunity path already fixed: a feed that briefly
    empties must not wipe the seen-set and re-alert on recovery."""
    i = ALERTS_T.index("function evaluateFeed")
    body = ALERTS_T[i:ALERTS_T.index("\n  }", i)]
    assert "COLLAPSE_RATIO" in body and "MIN_KEYS_FOR_PRUNE" in body


def test_blank_threshold_means_off_not_zero():
    """A blank box must disable that criterion. Treating it as 0 would fire on
    literally every row — the opposite of what emptying a field implies."""
    i = ALERTS_T.index("function cfgNum")
    body = ALERTS_T[i:ALERTS_T.index("\n  }", i)]
    assert 'return null' in body and '=== ""' in body


# ── diagnostics that must not chime by default ───────────────────────────────

def _default_off(text: str) -> set[str]:
    """Parse `const ALERT_DEFAULT_OFF = new Set([...])` out of a page/script."""
    import re
    m = re.search(r"ALERT_DEFAULT_OFF\s*=\s*new Set\(\[(.*?)\]\)", text, re.S)
    assert m, "ALERT_DEFAULT_OFF not found"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_the_two_default_off_sets_are_identical():
    """The panel draws the checkbox from its copy; alerts.js decides the chime.

    A kind in one set and not the other gives the exact failure this file
    exists to prevent — a control that shows one state and behaves as another.
    """
    assert _default_off(PAGE_T) == _default_off(ALERTS_T)


def test_ml_vs_spread_is_a_diagnostic_not_an_alert():
    """It names no bet by construction, and measurement backs that up.

    Over the whole collected CrystalBet history — 2 147 events, 1 709 pricing
    both the 1X2 and a line-0 pick'em rung — it produced 2 flags and 0 locks.
    Its bettable sibling pickem_arb is what should chime.
    """
    off = _default_off(PAGE_T)
    assert "ml_vs_spread" in off
    assert "pickem_arb" not in off, "the check that IS a bet must keep alerting"


def test_a_default_off_kind_is_still_listed_and_labelled():
    """Silenced is not hidden. The row stays on the tab and in the alert grid so
    it can be switched on, and a book contradicting itself stays visible."""
    import re
    labels = set(re.findall(r"^\s{2}(\w+):\s*\"", _block(PAGE_T, "const KIND_LABEL"), re.M))
    for kind in _default_off(PAGE_T):
        assert kind in labels, f"{kind} is silenced AND unlabelled — invisible"


# ── the tab's odds bands, one per table ──────────────────────────────────────

def test_each_table_has_its_own_odds_band_on_the_tab():
    """A ladder rung and a consistency flag have different odds profiles — a
    rung deep in a handicap ladder legitimately sits at 1.05, while a
    consistency flag there is noise. One shared band could only ever be right
    for one of the two tables."""
    for el in ("lad-min-odds", "lad-max-odds", "cons-min-odds", "cons-max-odds"):
        assert f'id="{el}"' in PAGE_T, f"{el} input is missing from the page"
    for key in ("anom_lad_min_odds", "anom_lad_max_odds",
                "anom_cons_min_odds", "anom_cons_max_odds"):
        assert f'"{key}"' in PAGE_T, f"{key} is never persisted"


def test_the_two_bands_are_applied_to_different_tables():
    """The failure this guards against is a copy-paste that filters both tables
    by the same band while the page shows two controls."""
    assert "ladderWithinCap(r, ladBand.cap, ladBand.floor)" in PAGE_T
    assert "consWithinCap(f, consBand.cap, consBand.floor)" in PAGE_T


def test_an_existing_shared_setting_is_migrated_not_dropped():
    """The band shipped as one shared pair. Splitting it must not silently
    reset someone's filter to 'off'."""
    assert "anom_max_odds" in PAGE_T and "anom_min_odds" in PAGE_T, (
        "the legacy keys are gone, so an existing setting is lost on upgrade")
    assert "LEGACY_MAX" in PAGE_T and "LEGACY_MIN" in PAGE_T


# ── a default the panel SHOWS must be a default the poller READS ─────────────

def _shown_defaults(text: str) -> dict[str, str]:
    """`consDef.value = lsGet(CONS_DEF, "12")` -> {"CONS_DEF": "12"}.

    Only non-empty fallbacks matter: a blank box honestly reads as "off", while
    a number rendered into the field reads as configured.
    """
    import re
    return {k: v for k, v in
            re.findall(r'lsGet\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)', text)
            if v not in ("", "0")}


def test_every_displayed_threshold_is_persisted():
    """The bug this pins, found 2026-09-05: consistency alerts had NEVER fired.

    `ladPct` rendered "10" and `consDef` rendered "12", but `lsSet` only ran
    from the change handler — so on a profile where nobody had edited those
    boxes, localStorage held nothing for them. alerts.js then read the bar with
    `cfgNum()`, got null, and `consPasses` rejected every row:

        const bar = (k.sev ?? dflt);
        if (bar === null) return false;      // every flag, every poll

    The ladder side failed the same way, falling through pct/delta/step. Both
    alert kinds were dead on arrival while the panel displayed a live-looking
    threshold — the exact "shows one state, behaves as another" failure the
    rest of this file guards, arriving through the one path it did not check.

    Measured against the live feed: 0 of 157 consistency rows would have fired
    before the fix, 19 after.
    """
    shown = _shown_defaults(PAGE_T)
    assert shown, "no non-empty defaults parsed — the regex has drifted"
    for const in shown:
        assert f"seedIfAbsent({const}" in PAGE_T, (
            f"{const} is rendered with a default the poller never sees; "
            f"seed it on load or make the field blank")


def test_seeding_never_overwrites_a_deliberate_choice():
    """Including a deliberately blank box — absent is not the same as empty."""
    import re
    i = PAGE_T.index("function seedIfAbsent")
    body = PAGE_T[i:i + 400]
    assert "getItem(key) === null" in body, "seeding must only fill ABSENT keys"
    assert 'value !== ""' in body, "an empty default must not be written"


def test_every_persisted_input_is_bound_to_the_handler():
    """syncAlertCfg() writes the four odds boxes, but nothing called it when
    only an odds box changed — so typing a veto and clicking away saved
    nothing until some other control happened to be touched."""
    import re
    # `.value` / `.checked` only — `lsSet(CONS_KINDS, JSON.stringify(...))`
    # is not an input and has no change event of its own.
    written = set(re.findall(r"lsSet\(\s*(\w+),\s*(\w+)\.(?:value|checked)",
                             PAGE_T))
    bound = re.search(r"for \(const el of \[(.*?)\]\)", PAGE_T, re.S).group(1)
    bound = {x.strip() for x in bound.replace("\n", " ").split(",") if x.strip()}
    for _key, el in written:
        assert el in bound, f"{el} is saved by syncAlertCfg but never triggers it"
