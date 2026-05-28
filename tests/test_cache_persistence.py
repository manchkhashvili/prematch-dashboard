"""
Tests for src/scrapers/cache_persistence.py — disk save/load for the CB cache.

Each test uses a tmp_path to keep state isolated from any real cache file.
After each test the module-level singletons (change_cache, crystalbet
_detail_odds_cache) are reset so state doesn't bleed between tests.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Odds  # noqa: E402
from src.scrapers import cache_persistence, change_cache, crystalbet  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _make_odds(event_id: str = "evt-1", line: float | None = None) -> Odds:
    return Odds(
        source="crystalbet",
        sport="basketball",
        home="Hawks",
        away="Lakers",
        market_type="moneyline" if line is None else "spread",
        period="FT",
        selections={"home": 1.91, "away": 1.91},
        fetched_at=NOW,
        line=line,
        start_time=NOW,
        league="NBA",
        raw_event_id=event_id,
    )


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """Reset module-level state before each test, restore after."""
    change_cache.reset_cache()
    saved = dict(crystalbet.get_detail_odds_cache())
    crystalbet.restore_detail_odds_cache({})
    try:
        yield
    finally:
        change_cache.reset_cache()
        crystalbet.restore_detail_odds_cache(saved)


# ── Save / load roundtrip ─────────────────────────────────────────────────────
class TestRoundtrip:
    def test_save_then_load_restores_entries(self, tmp_path):
        cache_file = tmp_path / "cb_change_cache.json"
        # Populate cache
        cache = change_cache.get_cache()
        cache.mark_loaded("evt-1", "loadinfo-A")
        cache.mark_expand_failed("evt-2", "loadinfo-B")
        crystalbet.restore_detail_odds_cache({"evt-1": [_make_odds("evt-1")]})

        assert cache_persistence.save(cache_file) is True
        assert cache_file.exists()

        # Wipe everything and reload
        change_cache.reset_cache()
        crystalbet.restore_detail_odds_cache({})
        assert cache_persistence.load(cache_file) is True

        # Verify cache entries restored
        loaded_cache = change_cache.get_cache()
        assert "evt-1" in loaded_cache.entries
        assert "evt-2" in loaded_cache.entries
        assert loaded_cache.entries["evt-1"].detail_status == "loaded"
        assert loaded_cache.entries["evt-2"].detail_status == "expand_failed"
        assert loaded_cache.entries["evt-1"].loadinfo_hash == \
            loaded_cache.hash_loadinfo("loadinfo-A")

        # Verify detail-odds restored
        detail = crystalbet.get_detail_odds_cache()
        assert "evt-1" in detail
        assert len(detail["evt-1"]) == 1
        assert detail["evt-1"][0].selections == {"home": 1.91, "away": 1.91}

    def test_roundtrip_preserves_alt_lines(self, tmp_path):
        """An expanded game with alt-lines (e.g. spread ladder) round-trips intact."""
        cache_file = tmp_path / "cb_change_cache.json"
        cache = change_cache.get_cache()
        cache.mark_loaded("evt-1", "loadinfo-A")

        # Simulate a game with spread alt-lines
        odds_list = [
            _make_odds("evt-1"),  # ML
            _make_odds("evt-1", line=-3.5),
            _make_odds("evt-1", line=-3.0),
            _make_odds("evt-1", line=-2.5),
        ]
        crystalbet.restore_detail_odds_cache({"evt-1": odds_list})

        cache_persistence.save(cache_file)
        crystalbet.restore_detail_odds_cache({})
        cache_persistence.load(cache_file)

        loaded = crystalbet.get_detail_odds_cache()
        assert len(loaded["evt-1"]) == 4
        lines = {o.line for o in loaded["evt-1"]}
        assert lines == {None, -3.5, -3.0, -2.5}

    def test_roundtrip_preserves_last_expanded_at(self, tmp_path):
        cache_file = tmp_path / "cb_change_cache.json"
        cache = change_cache.get_cache()
        cache.mark_loaded("evt-1", "loadinfo-A")
        original_ts = cache.entries["evt-1"].last_expanded_at
        assert original_ts is not None

        cache_persistence.save(cache_file)
        change_cache.reset_cache()
        cache_persistence.load(cache_file)

        loaded = change_cache.get_cache()
        assert loaded.entries["evt-1"].last_expanded_at == original_ts


# ── Failure modes ─────────────────────────────────────────────────────────────
class TestLoadFailures:
    def test_missing_file_returns_false(self, tmp_path):
        cache_file = tmp_path / "does_not_exist.json"
        assert cache_persistence.load(cache_file) is False

    def test_corrupt_json_returns_false(self, tmp_path):
        cache_file = tmp_path / "corrupt.json"
        cache_file.write_text("not valid json {{{")
        assert cache_persistence.load(cache_file) is False

    def test_version_mismatch_returns_false(self, tmp_path):
        cache_file = tmp_path / "old_version.json"
        payload = {
            "version": 99,
            "saved_at": NOW.isoformat(),
            "entries": {},
            "detail_odds": {},
        }
        cache_file.write_text(json.dumps(payload))
        assert cache_persistence.load(cache_file) is False

    def test_too_old_cache_returns_false(self, tmp_path):
        """Cache > MAX_AGE_HOURS old → discard. Stale odds aren't worth keeping."""
        cache_file = tmp_path / "old.json"
        ancient = (
            datetime.now(timezone.utc) -
            timedelta(hours=cache_persistence.MAX_AGE_HOURS + 1)
        )
        payload = {
            "version": cache_persistence.SCHEMA_VERSION,
            "saved_at": ancient.isoformat(),
            "entries": {},
            "detail_odds": {},
        }
        cache_file.write_text(json.dumps(payload))
        assert cache_persistence.load(cache_file) is False

    def test_recent_cache_within_max_age_loads(self, tmp_path):
        cache_file = tmp_path / "recent.json"
        recent = (
            datetime.now(timezone.utc) -
            timedelta(hours=cache_persistence.MAX_AGE_HOURS - 1)
        )
        payload = {
            "version": cache_persistence.SCHEMA_VERSION,
            "saved_at": recent.isoformat(),
            "entries": {},
            "detail_odds": {},
        }
        cache_file.write_text(json.dumps(payload))
        assert cache_persistence.load(cache_file) is True

    def test_bad_odds_dict_skipped_not_fatal(self, tmp_path):
        """One bad event_id's odds shouldn't take down the whole cache."""
        cache_file = tmp_path / "mixed.json"
        # Good odds for evt-1, missing-field garbage for evt-2.
        good = cache_persistence._odds_to_dict(_make_odds("evt-1"))
        bad = {"source": "crystalbet"}  # missing every required field
        payload = {
            "version": cache_persistence.SCHEMA_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "entries": {
                "evt-1": {
                    "loadinfo_hash": "abc",
                    "last_expanded_at": None,
                    "detail_status": "loaded",
                },
            },
            "detail_odds": {
                "evt-1": [good],
                "evt-2": [bad],
            },
        }
        cache_file.write_text(json.dumps(payload))
        assert cache_persistence.load(cache_file) is True
        # evt-1 should be present, evt-2 silently dropped
        loaded = crystalbet.get_detail_odds_cache()
        assert "evt-1" in loaded
        assert "evt-2" not in loaded


# ── Save behavior ─────────────────────────────────────────────────────────────
class TestSave:
    def test_save_to_nonexistent_directory_creates_it(self, tmp_path):
        cache_file = tmp_path / "nested" / "dir" / "cache.json"
        assert not cache_file.parent.exists()
        change_cache.get_cache().mark_loaded("evt-1", "loadinfo-A")
        assert cache_persistence.save(cache_file) is True
        assert cache_file.exists()

    def test_save_uses_atomic_rename(self, tmp_path):
        """save() writes to .tmp then renames — no partial files left behind."""
        cache_file = tmp_path / "cache.json"
        cache_persistence.save(cache_file)
        # No .tmp file left behind
        tmps = list(tmp_path.glob("*.tmp"))
        assert tmps == []

    def test_save_empty_cache_is_valid(self, tmp_path):
        cache_file = tmp_path / "empty.json"
        assert cache_persistence.save(cache_file) is True
        payload = json.loads(cache_file.read_text())
        assert payload["version"] == cache_persistence.SCHEMA_VERSION
        assert payload["entries"] == {}
        assert payload["detail_odds"] == {}

    def test_save_overwrites_existing_file(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        change_cache.get_cache().mark_loaded("evt-1", "loadinfo-A")
        cache_persistence.save(cache_file)
        old_size = cache_file.stat().st_size

        # Add more state and re-save
        change_cache.get_cache().mark_loaded("evt-2", "loadinfo-B" * 100)
        cache_persistence.save(cache_file)
        new_size = cache_file.stat().st_size
        assert new_size > old_size
