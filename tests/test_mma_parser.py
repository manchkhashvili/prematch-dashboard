"""
Tests for src.scrapers.sports.mma — the MMA list-view loadinfo parser.

Phase 4.3 (2026-05-27). MMA's 5-entry layout differs from basketball/tennis
in two ways:
  1. No handicap section at all (you can't spread a fight).
  2. Und + Tot + Over share `handicap='total'` when OU is BLANK, but Over
     flips to `handicap=''` when OU is LIVE. We identify all three by name
     only — see mma.py for the full rationale.

End-to-end fixture: data/raw/cb_prematch_sample_mma.html (1.1MB, 58 fights).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.sports import mma  # noqa: E402

NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


# ── Synthetic loadinfo fixtures ─────────────────────────────────────────────

# Mirrors a LIVE-OU game from the real sample: Pereira A. vs Gane C.
# Over has handicap='' (flipped), the rest are 'total'.
LIVE_OU = (
    '[{"name":"1","bet":"2.10","handicap":""},'
    '{"name":"\\t2","bet":"1.80","handicap":""},'
    '{"name":"Und","bet":"1.90","handicap":"total"},'
    '{"name":"Tot","bet":"2.5","handicap":"total","HasAdditionalOdds":"True"},'
    '{"name":"Over","bet":"1.75","handicap":""}]'
)

# Mirrors the 38/53 BLANK-OU case: all three OU entries are blank + flagged 'total'.
BLANK_OU = (
    '[{"name":"1","bet":"1.50","handicap":""},'
    '{"name":"\\t2","bet":"2.30","handicap":""},'
    '{"name":"Und","bet":" ","handicap":"total"},'
    '{"name":"Tot","bet":" ","handicap":"total","HasAdditionalOdds":"True"},'
    '{"name":"Over","bet":" ","handicap":"total"}]'
)

# Edge case — ML only (no OU entries at all).
ML_ONLY = (
    '[{"name":"1","bet":"1.40","handicap":""},'
    '{"name":"\\t2","bet":"2.90","handicap":""}]'
)


def _parse(raw: str):
    return mma.parse_loadinfo(
        raw, event_id="test-1",
        home="FighterA", away="FighterB",
        league="UFC 316", start_time=NOW, fetched_at=NOW,
    )


class TestParseLoadinfo:
    def test_live_ou_emits_both_ml_and_total(self):
        rows = _parse(LIVE_OU)
        assert len(rows) == 2
        kinds = {r.market_type for r in rows}
        assert kinds == {"moneyline", "total"}

    def test_blank_ou_emits_ml_only(self):
        rows = _parse(BLANK_OU)
        # ML home + ML away are valid (1.50, 2.30); OU is blank → skipped.
        assert len(rows) == 1
        assert rows[0].market_type == "moneyline"
        assert rows[0].selections == {"home": 1.50, "away": 2.30}

    def test_ml_only_layout(self):
        rows = _parse(ML_ONLY)
        assert len(rows) == 1
        assert rows[0].market_type == "moneyline"

    def test_sport_stamp_is_mma(self):
        rows = _parse(LIVE_OU)
        assert all(r.sport == "mma" for r in rows)

    def test_no_spread_market_ever(self):
        """MMA has no handicap section. Parser must NEVER emit a spread row,
        even if some future layout shipped an 'AH' entry."""
        rows = _parse(LIVE_OU)
        assert all(r.market_type != "spread" for r in rows)

    def test_total_line_and_selections(self):
        rows = _parse(LIVE_OU)
        ou = next(r for r in rows if r.market_type == "total")
        assert ou.line == 2.5
        assert ou.selections == {"under": 1.90, "over": 1.75}
        assert ou.period == "FT"

    def test_ml_strip_tab_prefix_on_away(self):
        """'\\t2' → 'mma' away name. Same handling as soccer/tennis."""
        rows = _parse(LIVE_OU)
        ml = next(r for r in rows if r.market_type == "moneyline")
        # selections key is "away", value comes from items[1].bet
        assert ml.selections["away"] == 1.80

    def test_malformed_json_returns_empty(self):
        rows = _parse("not-json")
        assert rows == []

    def test_empty_string_returns_empty(self):
        rows = _parse("")
        assert rows == []


class TestSampleHtmlFixture:
    """End-to-end: parse the captured sample, verify aggregate counts match
    the discovery findings (58 containers → 58 ML + 15 total)."""

    def test_end_to_end_counts(self):
        from src.scrapers.crystalbet import dry_run_parse_saved_mma
        odds = dry_run_parse_saved_mma()
        ml = [o for o in odds if o.market_type == "moneyline"]
        total = [o for o in odds if o.market_type == "total"]
        # 53 Format-A + 5 Format-B = 58 ML rows total.
        assert len(ml) == 58, f"expected 58 ML rows, got {len(ml)}"
        # 15/53 Format-A games had live OU per discovery.
        assert len(total) == 15, f"expected 15 total rows, got {len(total)}"
        # No spread rows ever — MMA has no handicap.
        assert all(o.market_type != "spread" for o in odds)
        # Every row is stamped sport=mma.
        assert all(o.sport == "mma" for o in odds)

    def test_no_invalid_odds_emitted(self):
        from src.scrapers.crystalbet import dry_run_parse_saved_mma
        odds = dry_run_parse_saved_mma()
        for o in odds:
            for side, v in o.selections.items():
                assert v > 1.0, f"odds {v} (<= 1.0) leaked for {o.home}/{side}"


class TestClassifyMarketTitle:
    def test_list_only_mode_always_returns_none(self):
        # MMA runs list-only in v1 — detail-page classifier never matches.
        for title in ("Money Line", "Total Rounds", "Method of Victory",
                      "Will the fight go the distance?", "Round Betting"):
            assert mma.classify_market_title(title) is None
