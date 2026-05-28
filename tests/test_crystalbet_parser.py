"""
Regression tests for prematch.src.scrapers.crystalbet parser internals.

Each test guards a specific bug from the build log:

  - DD.MM.YYYY date format (2026-05-24 "Date parser: CB live uses DD.MM.YYYY")
  - DD/MM/YYYY date format still works (backward compat)
  - Date carry across game-tables without their own title_block
    (2026-05-24 "Matcher upgrade" — parser fix bundled in that pass)
  - Naive Tbilisi → UTC conversion at parse time
    (2026-05-24 "Checkpoint 3-5: scope compression + bug fixes")
  - Format A: loadinfo locked-odds shift via _identify_loadinfo_roles
    (2026-05-25 "Locked-odds position shift in _parse_loadinfo")
  - Format B: col-positional divs parsing
  - Outright/specials league skip

Synthetic HTML mirrors the live CB shape (verified against reference §7 and
the saved sample at data/raw/cb_prematch_sample.html). The point is to
exercise the parser's branch logic, not to reproduce the full page chrome.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.crystalbet import (  # noqa: E402
    _identify_loadinfo_roles,
    _parse_loadinfo,
    parse_html,
)


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


# ── HTML fixture builders ─────────────────────────────────────────────────────
def _game_html(
    *,
    event_id: str,
    home: str,
    away: str,
    league: str = "Italy, Serie A",
    time_hhmm: str = "19:30",
    loadinfo_json: str | None = None,
    div_cols: dict[int, str] | None = None,
) -> str:
    """One GContainerList block. Either loadinfo (Format A) or div_cols (Format B)."""
    if loadinfo_json is not None:
        body = (
            f'<div class="game_loading" '
            f'data-loadinfo=\'{loadinfo_json}\'></div>'
        )
    elif div_cols is not None:
        divs = "".join(
            f'<div class="x_loop_res Snatch col{n}">{val}</div>'
            for n, val in div_cols.items()
        )
        body = divs
    else:
        body = ""
    return f"""
    <div class="GContainerList" data-id="{event_id}">
      <div class="game_hint"><label>{league}</label></div>
      <div class="teams_name">{home} - {away}</div>
      <span class="time">{time_hhmm}</span>
      {body}
    </div>
    """


def _page_with(date_blocks: list[tuple[str | None, list[str]]]) -> str:
    """
    Build a Sports.aspx-like HTML page.

    date_blocks = [(date_header_text, [game_html, ...]), ...]
    If date_header_text is None, the game-table has NO title_block — used to
    test the date-carry behavior. The previous date_str must carry over.
    """
    game_tables = []
    for header, games in date_blocks:
        title_div = (
            f'<div class="x_loop_title_block">{header}</div>'
            if header is not None else ""
        )
        game_tables.append(
            f'<div class="game-table">{title_div}{"".join(games)}</div>'
        )
    return f"<html><body>{''.join(game_tables)}</body></html>"


# ── Helpers for loadinfo entry construction ───────────────────────────────────
def _entry(name: str, bet: str, handicap: str = "") -> dict:
    return {"name": name, "bet": bet, "handicap": handicap}


# Canonical 8-entry baseline matching the docstring in _parse_loadinfo.
def _baseline_loadinfo() -> list[dict]:
    return [
        _entry("1", "1.80"),                       # ML home
        _entry(" 2", "2.05"),                      # ML away (LEADING SPACE)
        _entry("1", "1.85"),                       # AH home
        _entry("Handicap", "-3.0 +3.0", "handicap"),  # AH landmark
        _entry("2", "1.95"),                       # AH away
        _entry("Und", "1.92"),                     # OU under
        _entry("Point", "220.0", "total"),         # OU landmark
        _entry("Over", "1.88"),                    # OU over
    ]


# ── Date parsing ──────────────────────────────────────────────────────────────
class TestDateParsing:
    """Regression for the DD.MM.YYYY vs DD/MM/YYYY parser miss."""

    def test_dot_format_date_parses_to_utc(self):
        """CB live uses 'Monday - 25.05.2026'. Must parse + convert to UTC."""
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="22:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert len(odds) == 1
        # 22:00 Tbilisi (UTC+4) → 18:00 UTC.
        assert odds[0].start_time == datetime(2026, 5, 25, 18, 0,
                                               tzinfo=timezone.utc)

    def test_slash_format_date_parses(self):
        """Backward compat: older samples used 'Monday - 25/05/2026'."""
        html = _page_with([
            ("Monday - 25/05/2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="22:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert len(odds) == 1
        assert odds[0].start_time == datetime(2026, 5, 25, 18, 0,
                                               tzinfo=timezone.utc)

    def test_no_date_header_leaves_start_time_none(self):
        """First game-table has no title_block → no carried date yet → None."""
        html = _page_with([
            (None, [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="22:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert len(odds) == 1
        assert odds[0].start_time is None


# ── Date carry across game-tables ─────────────────────────────────────────────
class TestDateCarry:
    """
    CB groups multiple consecutive game-tables under one date header. Blocks
    without their own title_block must inherit the previous one. Regression
    for the matcher upgrade entry (parser fix bundled).
    """

    def test_second_game_table_inherits_previous_date(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="20:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
            (None, [
                _game_html(event_id="g2", home="Celtics", away="Heat",
                           time_hhmm="22:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert len(odds) == 2
        # Both Odds have start_time populated.
        g1 = next(o for o in odds if o.home == "Hawks")
        g2 = next(o for o in odds if o.home == "Celtics")
        # 20:00 Tbilisi → 16:00 UTC; 22:00 Tbilisi → 18:00 UTC.
        assert g1.start_time == datetime(2026, 5, 25, 16, 0, tzinfo=timezone.utc)
        assert g2.start_time == datetime(2026, 5, 25, 18, 0, tzinfo=timezone.utc)

    def test_new_date_header_overrides_previous(self):
        """When a later block has its own title_block, that becomes the new
        carrier."""
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="20:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
            ("Tuesday - 26.05.2026", [
                _game_html(event_id="g2", home="Celtics", away="Heat",
                           time_hhmm="20:00",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        g1 = next(o for o in odds if o.home == "Hawks")
        g2 = next(o for o in odds if o.home == "Celtics")
        # 20:00 Tbilisi on each day → 16:00 UTC on each day.
        assert g1.start_time == datetime(2026, 5, 25, 16, 0, tzinfo=timezone.utc)
        assert g2.start_time == datetime(2026, 5, 26, 16, 0, tzinfo=timezone.utc)


# ── Timezone conversion ───────────────────────────────────────────────────────
class TestTimezoneConversion:
    def test_tbilisi_local_subtracts_four_hours(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           time_hhmm="03:00",  # crosses midnight UTC → previous day
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert odds[0].start_time == datetime(
            2026, 5, 24, 23, 0, 0, tzinfo=timezone.utc
        )

    def test_start_time_is_utc_aware(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert odds[0].start_time.tzinfo is not None
        assert odds[0].start_time.utcoffset().total_seconds() == 0


# ── Format A: loadinfo + locked-odds role identification ──────────────────────
class TestLoadinfoRoleIdentification:
    """
    Regression for the 2026-05-25 locked-odds position shift bug. The parser
    must identify each market side by NAME + POSITION, not just position, so
    locked sides don't cause neighbors to fill the missing slot.
    """

    def test_baseline_eight_entries_all_roles_identified(self):
        items = _baseline_loadinfo()
        roles = _identify_loadinfo_roles(items, ah_idx=3, ou_idx=6)
        assert roles == {
            "ml_home": 0, "ml_away": 1,
            "ah_home": 2, "ah_away": 4,
            "ou_under": 5, "ou_over": 7,
        }

    def test_ml_home_locked_ml_away_role_is_none_not_shifted(self):
        """The exact bug: with ML home omitted from CB's JSON, every entry
        shifts. The verifier must NOT accept the shifted entry at the ML home
        slot. ML home stays None and the whole ML market is skipped."""
        items = _baseline_loadinfo()
        del items[0]  # ML home removed
        # AH landmark shifts to index 2; OU landmark shifts to 5.
        roles = _identify_loadinfo_roles(items, ah_idx=2, ou_idx=5)
        # ML away (" 2") is still findable by name in the pre-AH slice.
        assert roles["ml_home"] is None
        # AH and OU intact.
        assert roles["ah_home"] is not None
        assert roles["ah_away"] is not None
        assert roles["ou_under"] is not None
        assert roles["ou_over"] is not None

    def test_ah_home_locked_ah_role_is_none(self):
        items = _baseline_loadinfo()
        del items[2]  # AH home removed
        # AH landmark now at index 2; OU at 5.
        roles = _identify_loadinfo_roles(items, ah_idx=2, ou_idx=5)
        assert roles["ah_home"] is None
        # ML still findable.
        assert roles["ml_home"] is not None
        assert roles["ml_away"] is not None
        # OU still findable.
        assert roles["ou_under"] is not None
        assert roles["ou_over"] is not None

    def test_ah_away_locked_distinguished_from_ml_away(self):
        """
        ML away has name=' 2' (leading space); AH away has name='2' (no space).
        With AH away removed, the parser must NOT accept ML away in AH away's
        slot — the leading-space-vs-no-leading-space check is what saves us.
        """
        items = _baseline_loadinfo()
        del items[4]  # AH away removed
        # Landmarks at 3 (AH) and 5 (OU after shift).
        roles = _identify_loadinfo_roles(items, ah_idx=3, ou_idx=5)
        assert roles["ah_away"] is None

    def test_ou_under_locked(self):
        items = _baseline_loadinfo()
        del items[5]  # OU under removed
        # OU landmark now at 5.
        roles = _identify_loadinfo_roles(items, ah_idx=3, ou_idx=5)
        assert roles["ou_under"] is None
        # OU over still identifiable by name "Over" at position ou_idx+1.
        assert roles["ou_over"] is not None


class TestLoadinfoParse:
    """End-to-end Format A parse — verifies the role mapping flows through to
    emitted Odds objects."""

    def test_full_baseline_emits_three_markets(self):
        raw = json.dumps(_baseline_loadinfo())
        odds = _parse_loadinfo(
            raw, event_id="g1", home="Hawks", away="Lakers",
            league="NBA", start_time=NOW, fetched_at=NOW,
        )
        types = {o.market_type for o in odds}
        assert types == {"moneyline", "spread", "total"}

        ml = next(o for o in odds if o.market_type == "moneyline")
        assert ml.selections == {"home": 1.80, "away": 2.05}
        ah = next(o for o in odds if o.market_type == "spread")
        assert ah.line == -3.0
        assert ah.selections == {"home": 1.85, "away": 1.95}
        ou = next(o for o in odds if o.market_type == "total")
        assert ou.line == 220.0
        assert ou.selections == {"over": 1.88, "under": 1.92}

    def test_ml_home_locked_skips_only_ml(self):
        """The user-reported bug: HOME ML locked rendered as CB_H=8.40
        (actually AH home). With the fix the ML market is omitted entirely
        but AH and OU emit correctly."""
        items = _baseline_loadinfo()
        del items[0]
        raw = json.dumps(items)
        odds = _parse_loadinfo(
            raw, event_id="g1", home="Hawks", away="Lakers",
            league="NBA", start_time=NOW, fetched_at=NOW,
        )
        types = {o.market_type for o in odds}
        assert "moneyline" not in types
        assert types == {"spread", "total"}

    def test_malformed_json_returns_empty(self):
        odds = _parse_loadinfo(
            "not-json", event_id="g1", home="Hawks", away="Lakers",
            league="NBA", start_time=NOW, fetched_at=NOW,
        )
        assert odds == []


# ── Format B: col-positional divs ─────────────────────────────────────────────
class TestDivFormat:
    def test_moneyline_from_col0_col1(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           div_cols={0: "1.80", 1: "2.05"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        ml = [o for o in odds if o.market_type == "moneyline"]
        assert len(ml) == 1
        assert ml[0].selections == {"home": 1.80, "away": 2.05}

    def test_spread_with_slashed_line(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           div_cols={
                               2: "1.85", 3: "-3.0/+3.0", 4: "1.95",
                           }),
            ]),
        ])
        odds = parse_html(html, NOW)
        ah = [o for o in odds if o.market_type == "spread"]
        assert len(ah) == 1
        assert ah[0].line == -3.0
        assert ah[0].selections == {"home": 1.85, "away": 1.95}

    def test_total_from_col5_col6_col7(self):
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           div_cols={5: "1.92", 6: "220.0", 7: "1.88"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        ou = [o for o in odds if o.market_type == "total"]
        assert len(ou) == 1
        assert ou[0].line == 220.0
        # col7 = Over, col5 = Under (per the code comment)
        assert ou[0].selections == {"over": 1.88, "under": 1.92}

    def test_suspended_market_skipped(self):
        """Side rendered as '1.00' is suspended; _safe_float returns None
        and _make_odds returns None → market dropped."""
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g1", home="Hawks", away="Lakers",
                           div_cols={0: "1.00", 1: "2.05"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert [o for o in odds if o.market_type == "moneyline"] == []


# ── Outright / specials skip ──────────────────────────────────────────────────
class TestOutrightSkip:
    def test_outright_league_event_skipped(self):
        """game_hint label containing 'outright' → game skipped entirely."""
        html = _page_with([
            ("Monday - 25.05.2026", [
                _game_html(event_id="g-out", home="Hawks", away="Lakers",
                           league="NBA Outrights",
                           div_cols={0: "1.91", 1: "1.91"}),
            ]),
        ])
        odds = parse_html(html, NOW)
        assert odds == []


# ── Smoke: real saved sample ─────────────────────────────────────────────────
class TestSavedSample:
    """If the saved sample is on disk, parsing it should yield > 0 Odds.
    This catches gross parser breakage (parse_html returning 0 across the
    whole sample) without asserting a specific count."""

    SAMPLE = (
        Path(__file__).resolve().parents[1] /
        "data" / "raw" / "cb_prematch_sample.html"
    )

    def test_saved_sample_yields_odds(self):
        if not self.SAMPLE.exists():
            pytest.skip(f"no saved sample at {self.SAMPLE}")
        html = self.SAMPLE.read_text(encoding="utf-8")
        odds = parse_html(html, NOW)
        assert len(odds) > 0
        # Mix of market types expected.
        types = {o.market_type for o in odds}
        assert "moneyline" in types


# ── Per-game extraction (for the new detail-expansion flow) ──────────────────
class TestExtractGamesFromListHtml:
    """
    Tests the _extract_games_from_list_html helper that produces per-game
    metadata + loadinfo string + list-view Odds. Drives the change-cache
    decision in fetch_crystalbet_basketball_prematch.
    """

    SAMPLE = (
        Path(__file__).resolve().parents[1] /
        "data" / "raw" / "cb_prematch_sample.html"
    )

    def test_extraction_yields_games_with_metadata(self):
        if not self.SAMPLE.exists():
            pytest.skip(f"no saved sample at {self.SAMPLE}")
        from src.scrapers.crystalbet import _extract_games_from_list_html
        html = self.SAMPLE.read_text(encoding="utf-8")
        games = _extract_games_from_list_html(html, NOW)
        assert len(games) > 10, f"expected many games, got {len(games)}"
        # Every game has an event_id and team names.
        for g in games:
            assert g.event_id
            assert g.home
            assert g.away
            # League may be empty for some odd cases but usually populated.
        # At least some games use the loadinfo format (most basketball games).
        with_loadinfo = [g for g in games if g.loadinfo]
        assert len(with_loadinfo) > 0, "expected some games with loadinfo"

    def test_extracted_loadinfo_is_hashable(self):
        """Verify the loadinfo strings round-trip through change_cache hashing."""
        if not self.SAMPLE.exists():
            pytest.skip(f"no saved sample at {self.SAMPLE}")
        from src.scrapers.crystalbet import _extract_games_from_list_html
        from src.scrapers.change_cache import ChangeCache
        html = self.SAMPLE.read_text(encoding="utf-8")
        games = _extract_games_from_list_html(html, NOW)
        cache = ChangeCache()
        # First pass: every game needs expansion (first-seen).
        first_pass = [cache.needs_expansion(g.event_id, g.loadinfo) for g in games]
        assert all(first_pass), "all first-seen games should need expansion"
        # Mark them all as loaded.
        for g in games:
            cache.mark_loaded(g.event_id, g.loadinfo)
        # Second pass with same loadinfo → no expansion needed.
        second_pass = [cache.needs_expansion(g.event_id, g.loadinfo) for g in games]
        assert not any(second_pass), \
            "unchanged loadinfo should not need re-expansion"

    def test_empty_html_yields_no_games(self):
        """Phase 2.5 startup-race fix sanity: handing garbage / not-yet-
        rendered HTML to the extractor returns [] cleanly. The 'raise on
        0 games' decision lives in _fetch_for_sport — this just guards
        that the parser itself stays non-throwing on empty input."""
        from src.scrapers.crystalbet import _extract_games_from_list_html
        for html in ("", "<html></html>", "<html><body></body></html>"):
            games = _extract_games_from_list_html(html, NOW)
            assert games == [], f"expected [] for {html!r}, got {games}"
