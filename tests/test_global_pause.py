"""Global pause — stop every loop, keep the server up (2026-07-28).

The point is to stop work overnight without killing the terminal, then resume
exactly as before. So pause is a TOP-LEVEL override, never a mutation of the
book/scan switches: there is no "previous state" to restore because nothing
was changed.
"""
from __future__ import annotations

import inspect

import pytest

from src import app as app_mod
from src import runtime_config


# tests/conftest.py's autouse _isolate_runtime_config already points
# CONFIG_PATH at a temp dir and clears the cache, so every test here starts
# from the env-seeded defaults with nothing paused.


# ── the switch itself ────────────────────────────────────────────────────────

def test_not_paused_by_default():
    assert runtime_config.is_paused() is False


def test_set_paused_round_trips():
    runtime_config.set_paused(True)
    assert runtime_config.is_paused() is True
    runtime_config.set_paused(False)
    assert runtime_config.is_paused() is False


def test_paused_survives_a_reload_from_disk():
    """Pausing then closing the laptop must not silently resume."""
    runtime_config.set_paused(True)
    runtime_config.load(force=True)
    assert runtime_config.is_paused() is True


def test_update_accepts_paused():
    runtime_config.update({"paused": True})
    assert runtime_config.is_paused() is True


def test_update_rejects_a_non_boolean_paused():
    with pytest.raises(ValueError):
        runtime_config.update({"paused": "yes"})


# ── the core promise: resume exactly as before ───────────────────────────────

def test_pause_does_not_change_any_toggle():
    runtime_config.update({"books": {"setanta": True, "liderbet": False},
                           "scans": {"soft_scan": True, "anomaly": False}})
    before = runtime_config.get()
    runtime_config.set_paused(True)
    after = runtime_config.get()
    assert after["books"] == before["books"]
    assert after["scans"] == before["scans"]
    assert after["cadence"] == before["cadence"]
    assert after["limits"] == before["limits"]


def test_resume_restores_the_exact_prior_state():
    runtime_config.update({"books": {"setanta": True, "crocobet": False}})
    before = runtime_config.get()
    runtime_config.set_paused(True)
    runtime_config.set_paused(False)
    assert runtime_config.get() == before


def test_toggles_changed_while_paused_are_kept_on_resume():
    """Pausing must not freeze the config — you can still retune, and the new
    settings are what resume uses."""
    runtime_config.set_paused(True)
    runtime_config.update({"books": {"setanta": True}})
    runtime_config.set_paused(False)
    assert runtime_config.is_on("books", "setanta") is True


# ── is_on reports the SWITCH, active() reports whether work runs ─────────────

def test_is_on_still_reports_the_switch_while_paused():
    """Otherwise the Config tab and every "scanner off" notice would show a
    page of false OFFs and you could not see what you had enabled."""
    runtime_config.update({"books": {"setanta": True}})
    runtime_config.set_paused(True)
    assert runtime_config.is_on("books", "setanta") is True


def test_active_is_false_while_paused():
    runtime_config.update({"books": {"setanta": True}})
    runtime_config.set_paused(True)
    assert runtime_config.active("books", "setanta") is False


def test_active_is_false_for_a_switched_off_book_even_when_running():
    runtime_config.update({"books": {"setanta": False}})
    assert runtime_config.is_paused() is False
    assert runtime_config.active("books", "setanta") is False


def test_active_is_true_only_when_on_and_not_paused():
    runtime_config.update({"books": {"setanta": True}})
    assert runtime_config.active("books", "setanta") is True


# ── coverage: no loop escapes the pause ──────────────────────────────────────

def test_gate_helpers_consult_active_not_is_on():
    """_gated / _sleep_gated are where every toggleable loop passes through.
    If either reverted to is_on(), pause would silently stop gating them."""
    for fn in (app_mod._gated, app_mod._sleep_gated):
        src = inspect.getsource(fn)
        assert "runtime_config.active(" in src, f"{fn.__name__} bypasses pause"


def test_untoggleable_loops_check_pause_explicitly():
    """Pinnacle has no on/off switch and the extra-book loop owns its own gate
    (it also clears odds), so neither goes through _gated. Both must still
    honour a pause or "stop everything" leaves pollers running."""
    for fn in (app_mod._pinnacle_loop_for_sport, app_mod._extra_book_loop_for_sport):
        src = inspect.getsource(fn)
        assert "_paused_idle(" in src, f"{fn.__name__} keeps polling while paused"


def test_pause_does_not_clear_book_state():
    """A toggle-off clears a book's odds so nothing prices off a frozen
    snapshot; a pause deliberately keeps them so resume looks unchanged."""
    src = inspect.getsource(app_mod._paused_idle)
    assert "_empty_source_state" not in src


def test_paused_idle_returns_false_when_running():
    import asyncio
    assert asyncio.run(app_mod._paused_idle("probe")) is False


def test_paused_idle_returns_true_when_paused():
    import asyncio
    runtime_config.set_paused(True)
    assert asyncio.run(app_mod._paused_idle("probe")) is True


# ── while paused, request-driven work must not recompute ─────────────────────
# The user's terminal kept scrolling with matcher output after pausing: the
# poll loops were idle, but every open browser tab polls /api/opportunities,
# /api/matches, /api/unmatched and /api/cross_book, and each of those re-ran
# the matcher per sport per book. Matching is ~64 % of a cycle's CPU, so
# "paused" still burned CPU proportional to how many tabs were open.

def test_paused_memo_recomputes_every_call_while_running():
    calls = []
    for _ in range(3):
        app_mod._paused_memo(("k",), lambda: calls.append(1))
    assert len(calls) == 3


def test_paused_memo_computes_once_while_paused():
    runtime_config.set_paused(True)
    calls = []
    for _ in range(5):
        app_mod._paused_memo(("k",), lambda: calls.append(1) or "v")
    assert len(calls) == 1


def test_paused_memo_keys_on_request_params():
    """Two tabs with different filters must not serve each other's results."""
    runtime_config.set_paused(True)
    a = app_mod._paused_memo(("opportunities", 1.0), lambda: "min1")
    b = app_mod._paused_memo(("opportunities", 4.5), lambda: "min45")
    assert (a, b) == ("min1", "min45")


def test_cache_is_dropped_on_resume():
    """Otherwise the board would stay frozen after unpausing."""
    runtime_config.set_paused(True)
    app_mod._paused_memo(("k",), lambda: "stale")
    assert app_mod._paused_result_cache
    runtime_config.set_paused(False)
    assert app_mod._paused_memo(("k",), lambda: "fresh") == "fresh"
    assert not app_mod._paused_result_cache


@pytest.mark.parametrize("fn", [
    "api_opportunities", "api_matches", "api_unmatched", "api_cross_book",
])
def test_heavy_handlers_are_memoised(fn):
    """Each of these re-runs the matcher; all must go through the memo."""
    src = inspect.getsource(getattr(app_mod, fn))
    assert "_paused_memo(" in src, f"{fn} recomputes on every request while paused"


# ── long scans must abort mid-flight, not run to completion ──────────────────

def test_long_scans_pass_a_pause_aware_should_continue():
    """A CB full-detail sweep is the longest single unit of work in the app —
    the tennis anomaly scan expands 420 games. Without a pause-aware
    should_continue, hitting Pause waits for all of it."""
    src = inspect.getsource(app_mod)
    for needle in ('should_continue=lambda: runtime_config.active("books", "crystalbet")',
                   'should_continue=lambda: runtime_config.active("scans", "anomaly")'):
        assert needle in src, f"missing pause-aware abort: {needle}"
    assert 'runtime_config.active(\n                        "scans", "anomaly_extra")' in src \
        or 'runtime_config.active("scans", "anomaly_extra")' in src, \
        "anomaly_extra scan cannot be aborted mid-flight"


def test_no_scan_uses_the_pause_blind_book_on_gate():
    """book_on() ignores pause by design (it reports the switch). A
    should_continue built on it would keep scraping through a pause."""
    src = inspect.getsource(app_mod)
    assert "should_continue=lambda: runtime_config.book_on(" not in src
