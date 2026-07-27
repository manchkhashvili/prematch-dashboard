"""Shared pytest fixtures.

The important one is `_isolate_runtime_config`: without it the suite reads the
developer's real `data/runtime_config.json`, so whatever you last toggled in
the Config tab silently changes test results. That actually happened — turning
CrystalBet off in the running dashboard made `test_opps_carry_sport_tag` fail,
because `/api/opportunities` correctly skips books that are switched off.

Tests get a pristine, env-seeded config in a temp dir instead, and every test
starts from the same state regardless of local machine state.
"""
from __future__ import annotations

import pytest

from src import runtime_config as _rc


@pytest.fixture(autouse=True)
def _isolate_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setattr(_rc, "CONFIG_PATH", tmp_path / "runtime_config.json")
    monkeypatch.setattr(_rc, "_cfg", {})
    monkeypatch.setattr(_rc, "_loaded", False)
    # Isolation only — no forced toggles. The env-seeded defaults already have
    # CrystalBet on, and tests that need an extra book declare it themselves
    # (see the cross-book fixtures), so this stays a pure sandbox rather than a
    # second source of truth the runtime_config tests would have to fight.
    yield
    monkeypatch.setattr(_rc, "_cfg", {})
    monkeypatch.setattr(_rc, "_loaded", False)
