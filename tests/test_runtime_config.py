"""Runtime config store — the Config tab's backing store.

Covers the invariants that matter operationally: env seeds the defaults (so an
existing deployment is unchanged until someone touches the UI), saved values
survive a reload, and the write path refuses anything that could be used to
hammer a book (unknown keys, wrong types, out-of-range cadences).
"""
from __future__ import annotations

import json

import pytest

from src import runtime_config as rc


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(rc, "_cfg", {})
    monkeypatch.setattr(rc, "_loaded", False)
    yield


def test_env_seeds_defaults(monkeypatch):
    monkeypatch.setenv("SETANTA", "1")
    monkeypatch.setenv("LIDERBET", "0")
    monkeypatch.setenv("EXTRA_BOOK_POLL_SEC", "222")
    cfg = rc.load(force=True)
    assert cfg["books"]["setanta"] is True
    assert cfg["books"]["liderbet"] is False
    assert cfg["cadence"]["extra_book_poll_sec"] == 222


def test_crystalbet_defaults_on_without_env():
    """CB + Pinnacle are the original pipeline — CB must not silently vanish
    just because nobody set CRYSTALBET=1."""
    assert rc.load(force=True)["books"]["crystalbet"] is True


def test_update_persists_and_survives_reload():
    rc.load(force=True)
    rc.update({"books": {"setanta": True}})
    assert rc.book_on("setanta") is True
    saved = json.loads(rc.CONFIG_PATH.read_text())
    assert saved["books"]["setanta"] is True
    # a fresh load (new process) must see it
    assert rc.load(force=True)["books"]["setanta"] is True


def test_saved_value_beats_env(monkeypatch):
    monkeypatch.setenv("SETANTA", "0")
    rc.load(force=True)
    rc.update({"books": {"setanta": True}})
    assert rc.load(force=True)["books"]["setanta"] is True


def test_unknown_keys_rejected():
    rc.load(force=True)
    with pytest.raises(ValueError):
        rc.update({"books": {"nosuchbook": True}})
    with pytest.raises(ValueError):
        rc.update({"nosuchsection": {"x": 1}})


def test_type_enforcement():
    rc.load(force=True)
    with pytest.raises(ValueError):
        rc.update({"books": {"setanta": "yes"}})     # must be a real bool
    with pytest.raises(ValueError):
        rc.update({"cadence": {"pinnacle_poll_sec": True}})   # bool is not a number


def test_cadence_clamped_to_safe_range():
    """A 0 s poll would hammer the book into rate-limiting us."""
    rc.load(force=True)
    cfg = rc.update({"cadence": {"pinnacle_poll_sec": 0}})
    lo, hi = rc.CADENCES["pinnacle_poll_sec"][1:3]
    assert cfg["cadence"]["pinnacle_poll_sec"] == lo
    cfg = rc.update({"cadence": {"pinnacle_poll_sec": 10 ** 9}})
    assert cfg["cadence"]["pinnacle_poll_sec"] == hi


def test_limits_clamped_and_float():
    rc.load(force=True)
    cfg = rc.update({"limits": {"setanta_detail_hours": 0.001}})
    assert cfg["limits"]["setanta_detail_hours"] == rc.LIMITS["setanta_detail_hours"][1]


def test_reset_returns_to_env_defaults(monkeypatch):
    monkeypatch.setenv("SETANTA", "0")
    rc.load(force=True)
    rc.update({"books": {"setanta": True}})
    assert rc.book_on("setanta") is True
    rc.reset()
    assert rc.book_on("setanta") is False


def test_corrupt_file_falls_back_to_env(monkeypatch):
    rc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rc.CONFIG_PATH.write_text("{not json")
    monkeypatch.setenv("SETANTA", "1")
    cfg = rc.load(force=True)          # must not raise
    assert cfg["books"]["setanta"] is True


def test_stale_keys_in_saved_file_are_ignored():
    rc.load(force=True)
    rc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rc.CONFIG_PATH.write_text(json.dumps({"books": {"retiredbook": True, "setanta": True}}))
    cfg = rc.load(force=True)
    assert "retiredbook" not in cfg["books"]
    assert cfg["books"]["setanta"] is True


def test_secs_helper_falls_back():
    rc.load(force=True)
    assert rc.secs("pinnacle_poll_sec", 999) == rc.load()["cadence"]["pinnacle_poll_sec"]
    assert rc.secs("nosuchkey", 999) == 999
