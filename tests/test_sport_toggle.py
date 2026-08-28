"""Per-sport master switch (2026-08-14).

Owner: "also add sports in config to switch completly off/on by sport types".

Orthogonal to the book toggles on purpose, and the reason it had to exist is
that no combination of the existing switches could stop one sport:

  * `books.crystalbet` off stops CB for EVERY sport;
  * Pinnacle has no book toggle at all — it is the reference — so its heaviest
    poller kept running whatever you switched off;
  * the ladder scans are gated by `scans.*`, which is also all-or-nothing
    across sports;
  * `SPORTS=` does select per sport, but only at boot, and a restart is exactly
    what the Config tab exists to avoid.

Off means off everywhere: fetch, parse, tick write, scans — and the sport's
odds are CLEARED, matching what a book toggle does and for the same reason (a
switched-off sport must not keep feeding the Arbs and Cross-book tabs from a
frozen snapshot). Pause is deliberately the opposite: it keeps everything.
"""
from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

import pytest

from src import app as A
from src import runtime_config as rc


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated config file — never the developer's live one."""
    monkeypatch.setattr(rc, "CONFIG_PATH", tmp_path / "runtime_config.json")
    rc.load(force=True)
    yield rc
    rc.load(force=True)


# ── the store ────────────────────────────────────────────────────────────────

def test_every_established_sport_defaults_on(store):
    """The switch stops work you are already doing; it is not a second gate you
    must remember to open. SPORTS= still decides what runs at boot.

    The one exception is SPORTS_DEFAULT_OFF — a sport added AFTER the board was
    settled must not enrol itself onto everyone's machine on the next restart.
    See test_a_new_sport_ships_off_rather_than_turning_itself_on.
    """
    assert store.get()["sports"] == {
        s: s not in store.SPORTS_DEFAULT_OFF for s in store.SPORTS}
    for s in store.SPORTS:
        if s in store.SPORTS_DEFAULT_OFF:
            continue
        assert store.sport_on(s) is True


def test_toggling_one_sport_leaves_the_others_alone(store):
    store.update({"sports": {"soccer": False}})
    assert store.sport_on("soccer") is False
    assert all(store.sport_on(s) for s in store.SPORTS
               if s != "soccer" and s not in store.SPORTS_DEFAULT_OFF)


def test_the_setting_persists_across_a_reload(store):
    store.update({"sports": {"tennis": False}})
    saved = json.loads(Path(store.CONFIG_PATH).read_text())
    assert saved["sports"]["tennis"] is False
    store.load(force=True)
    assert store.sport_on("tennis") is False


def test_pause_is_orthogonal_to_the_sport_switch(store):
    """Pause must not destroy what you had enabled — resuming needs no memory
    of the previous state."""
    store.set_paused(True)
    assert store.sport_on("tennis") is True, "the SWITCH is untouched"
    assert store.sport_active("tennis") is False, "but no work should run"
    store.set_paused(False)
    assert store.sport_active("tennis") is True


def test_bad_input_is_rejected(store):
    with pytest.raises(ValueError):
        store.update({"sports": {"quidditch": False}})
    with pytest.raises(ValueError):
        store.update({"sports": {"soccer": "yes"}})


def test_an_unknown_sport_reads_as_on(store):
    """A typo in SPORTS= must not silently disable a sport — failing open is
    the safe direction for a switch whose off state stops all data."""
    assert store.sport_on("kabaddi") is True


# ── the gate is actually wired into every per-sport loop ─────────────────────

@pytest.mark.parametrize("loop,label", [
    ("_pinnacle_loop_for_sport", "Pinnacle has no book toggle — this is the "
                                 "only way to stop it for one sport"),
    ("_crystalbet_loop_for_sport", "the heaviest book"),
    ("_xbet_loop_for_sport", "second reference"),
    ("_extra_book_loop_for_sport", "lider / betlive / crocobet / setanta"),
])
def test_every_price_loop_consults_the_sport_switch(loop, label):
    src = inspect.getsource(getattr(A, loop))
    assert "_sport_gated(" in src, f"{loop} ignores the sport switch — {label}"
    # ...and before it does any work.
    gate = src.index("_sport_gated(")
    for work in ("_store_odds(", "record_cycle"):
        if work in src:
            assert gate < src.index(work), f"{loop} works before checking"


@pytest.mark.parametrize("fn", [
    "_anomaly_extra_loop", "_compute_anomalies", "_anomaly_watch_loop",
    "_opportunity_reverify_loop",
])
def test_every_scan_consults_the_sport_switch(fn):
    src = inspect.getsource(getattr(A, fn))
    assert "sport_active(" in src, f"{fn} would keep scanning a switched-off sport"


def test_switching_a_sport_off_clears_its_odds():
    """Same contract as a book toggle: stop working AND forget, so nothing
    downstream prices off a frozen snapshot. This is what makes it different
    from pause."""
    src = inspect.getsource(A._sport_gated)
    assert "_empty_source_state()" in src
    for store_name in ("_book_anomalies", "_book_consistency",
                       "_extra_anomalies", "_extra_anom_consistency"):
        assert store_name in src, f"{store_name} would keep the sport's flags"


def test_the_opportunity_feed_drops_switched_off_sports():
    """Belt and braces for the tick between the toggle and the first loop
    noticing — a switched-off sport must not be able to emit a row."""
    src = inspect.getsource(A._compute_opportunities_now)
    assert "sport_on(sport)" in src
    assert src.index("sport_on(sport)") < src.index("match_events(")


# ── the tab can render it ────────────────────────────────────────────────────

def test_the_config_api_offers_only_sports_this_process_runs():
    """A switch for a sport that was never started is a dead control."""
    src = inspect.getsource(A.api_config_get)
    assert '"sports": [s for s in runtime_config.SPORTS if s in SPORT_NAMES]' in src
    assert "sport_live" in src, "the tab shows rows/age as proof it really stopped"


def test_the_config_page_renders_the_section():
    html = (Path(A.__file__).resolve().parent.parent / "static" / "config.html").read_text()
    assert 'id="sports"' in html
    assert 'toggleRow("sports"' in html
    assert "SPORT_LIVE" in html


# ── a new sport must not enrol itself, and must say what it costs ────────────

def test_a_new_sport_ships_off_rather_than_turning_itself_on(store):
    """Adding a sport to `_ALL_SPORTS` makes it part of the default board.

    Ice hockey is what made that visible: it landed on a saturated box and cost
    a fifth full-detail CB sweep (~23 s per price cycle, ~13 s per anomaly
    sweep) for a board whose best CB-vs-Pinnacle leg was -3.6 % — nothing to
    price. Someone running `python main.py` with no SPORTS= had not asked for
    any of that.
    """
    assert "icehockey" in rc.SPORTS_DEFAULT_OFF
    assert rc.sport_on("icehockey") is False
    for established in ("basketball", "soccer", "tennis", "americanfootball"):
        assert rc.sport_on(established) is True, established


def test_an_explicit_choice_beats_the_default(store):
    """The default only decides while nobody has chosen. Once the toggle is set
    in the Config tab, the stored value wins — including switching it back off
    for a sport that defaults on."""
    rc.update({"sports": {"icehockey": True}})
    assert rc.sport_on("icehockey") is True
    rc.update({"sports": {"icehockey": False}})
    assert rc.sport_on("icehockey") is False
    rc.update({"sports": {"soccer": False}})
    assert rc.sport_on("soccer") is False


def test_an_unknown_sport_is_still_on(store):
    """A typo in SPORTS= must not silently disable a sport. Only the explicit
    default-off list flips that."""
    assert rc.sport_on("quidditch") is True


def test_every_sport_has_a_label_and_a_cost_in_the_config_tab():
    """The switch is unusable if a sport renders as a bare key with no cost.

    `toggleRow` falls back to the raw key when SPORT_LABEL has no entry, so a
    newly added sport appears as `icehockey` with no help text — a control the
    owner cannot make a decision with. This is the guard for the NEXT sport.
    """
    html = (Path(A.__file__).resolve().parent.parent / "static" / "config.html").read_text()
    labels = _js_object_keys(html, "const SPORT_LABEL")
    descs = _js_object_keys(html, "const SPORT_DESC")
    for sport in rc.SPORTS:
        assert sport in labels, f"{sport} has no SPORT_LABEL — renders as a bare key"
        assert sport in descs, f"{sport} has no SPORT_DESC — no cost stated"


def _js_object_keys(html: str, decl: str) -> set[str]:
    """Keys of a `const NAME = { ... };` object literal in the page source.

    Two keys can share a line (`basketball: "Basketball", soccer: "Soccer",`),
    so this matches every `word:` that follows a brace, comma or line start
    rather than only the first on each line.
    """
    import re
    start = html.index(decl)
    end = html.index("};", start)
    return set(re.findall(r"[{,\n]\s*(\w+)\s*:", html[start:end]))
