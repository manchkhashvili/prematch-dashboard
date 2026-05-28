"""
Tests for src/scrapers/sports/basketball.py.

Covers the detail-page market-name classifier (classify_market_title) for all
21 in-scope market×period combinations, plus the out-of-scope skip patterns.

The list-view loadinfo parser (parse_loadinfo / parse_div_odds) is already
covered end-to-end by test_crystalbet_parser.py, which exercises the full
parse_html path. No need to duplicate.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.sports.basketball import (  # noqa: E402
    SPORT_ID,
    classify_market_title,
)


# ── Sport config sanity ───────────────────────────────────────────────────────
def test_sport_id_is_17():
    """CB's DoSportTypePostBack id for basketball — verified in reference §3."""
    assert SPORT_ID == 17


# ── Classifier: FT markets ────────────────────────────────────────────────────
class TestClassifyFT:
    """FT markets — should map to (X, FT, rank=0) for incl-OT variants."""

    def test_winner_incl_overtime_is_ft_moneyline(self):
        r = classify_market_title("Winner (incl. overtime)")
        assert r is not None
        assert r.market_type == "moneyline"
        assert r.period == "FT"
        assert r.variant_rank == 0

    def test_total_points_incl_overtime_is_ft_total_rank_0(self):
        r = classify_market_title("Total Points(incl. overtime)")
        assert r is not None
        assert r.market_type == "total"
        assert r.period == "FT"
        assert r.variant_rank == 0

    def test_total_points_regular_time_is_ft_total_rank_1(self):
        """Regular-time fallback — same key but rank 1, so the caller keeps
        the incl-OT variant when both exist."""
        r = classify_market_title("Total Points(Regular time)")
        assert r is not None
        assert r.market_type == "total"
        assert r.period == "FT"
        assert r.variant_rank == 1

    def test_handicap_incl_overtime_is_ft_spread(self):
        r = classify_market_title("Handicap (incl. overtime)*")
        assert r is not None
        assert r.market_type == "spread"
        assert r.period == "FT"
        assert r.variant_rank == 0


# ── Classifier: H1 markets ────────────────────────────────────────────────────
class TestClassifyH1:
    def test_h1_draw_no_bet_is_h1_moneyline(self):
        """We use Draw-No-Bet as the 2-way ML for H1 (matches Pinnacle H1 ML
        behavior: push on tie)."""
        r = classify_market_title("1st Half - Draw No Bet")
        assert r is not None
        assert r.market_type == "moneyline"
        assert r.period == "H1"

    def test_h1_handicap_is_h1_spread(self):
        r = classify_market_title("1st Half - Handicap")
        assert r is not None
        assert r.market_type == "spread"
        assert r.period == "H1"

    def test_h1_total_is_h1_total(self):
        r = classify_market_title("1st Half - Total*")
        assert r is not None
        assert r.market_type == "total"
        assert r.period == "H1"

    def test_h1_1x2_is_skipped(self):
        """3-way ML out of scope."""
        assert classify_market_title("1st half - 1x2") is None

    def test_h1_team_total_is_skipped(self):
        assert classify_market_title("1st half - Home Team total") is None
        assert classify_market_title("1st half - Away Team total") is None


# ── Classifier: H2 markets ────────────────────────────────────────────────────
class TestClassifyH2:
    def test_h2_dnb_ot_is_h2_moneyline_rank_0(self):
        r = classify_market_title("2nd Half - Draw No Bet (OT)")
        assert r is not None
        assert r.market_type == "moneyline"
        assert r.period == "H2"
        assert r.variant_rank == 0

    def test_h2_dnb_reg_is_h2_moneyline_rank_1(self):
        """Regular-time fallback — same key, rank 1."""
        r = classify_market_title("2nd Half - Draw No Bet")
        assert r is not None
        assert r.market_type == "moneyline"
        assert r.period == "H2"
        assert r.variant_rank == 1

    def test_h2_handicap_incl_ot_rank_0(self):
        r = classify_market_title("2nd Half - Handicap (incl. overtime)")
        assert r is not None
        assert r.market_type == "spread"
        assert r.period == "H2"
        assert r.variant_rank == 0

    def test_h2_handicap_reg_rank_1(self):
        r = classify_market_title("2nd half - handicap")
        assert r is not None
        assert r.market_type == "spread"
        assert r.period == "H2"
        assert r.variant_rank == 1

    def test_h2_total_incl_ot_rank_0(self):
        r = classify_market_title("2nd Half - Total (incl. overtime)")
        assert r is not None
        assert r.market_type == "total"
        assert r.period == "H2"
        assert r.variant_rank == 0

    def test_h2_total_reg_rank_1(self):
        r = classify_market_title("2nd Half - Total*")
        assert r is not None
        assert r.market_type == "total"
        assert r.period == "H2"
        assert r.variant_rank == 1


# ── Classifier: Q1-Q4 markets ─────────────────────────────────────────────────
class TestClassifyQuarters:
    @pytest.mark.parametrize("title,period", [
        ("1st Quarter - Draw No Bet*", "Q1"),
        ("2nd Quarter - Draw No Bet*", "Q2"),
        ("3rd Quarter - Draw No Bet*", "Q3"),
        ("4th. Quarter - Draw No Bet*", "Q4"),
    ])
    def test_quarter_dnb_is_quarter_moneyline(self, title, period):
        r = classify_market_title(title)
        assert r is not None
        assert r.market_type == "moneyline"
        assert r.period == period

    @pytest.mark.parametrize("title,period", [
        ("1st. Quarter - Handicap", "Q1"),
        ("2nd Quarter - Handicap", "Q2"),
        ("3rd Quarter - Handicap", "Q3"),
        ("4rt. Quarter - Handicap", "Q4"),   # CB typo: "4rt"
    ])
    def test_quarter_handicap_is_quarter_spread(self, title, period):
        r = classify_market_title(title)
        assert r is not None
        assert r.market_type == "spread"
        assert r.period == period

    @pytest.mark.parametrize("title,period", [
        ("1st Quarter - Total Points", "Q1"),
        ("2nd Quarter - Total Points*", "Q2"),
        ("3rd Quarter - Total Points*", "Q3"),
    ])
    def test_quarter_total_is_quarter_total(self, title, period):
        r = classify_market_title(title)
        assert r is not None
        assert r.market_type == "total"
        assert r.period == period

    def test_q4_typo_handicap_4rt_dot(self):
        """CB seriously writes '4rt. Quarter' — verify our regex handles it."""
        r = classify_market_title("4rt. Quarter - Handicap")
        assert r is not None
        assert r.period == "Q4"
        assert r.market_type == "spread"

    def test_quarter_1x2_skipped(self):
        for title in [
            "1st. Quarter - 1x2", "2nd Quarter - 1x2*",
            "3rd Quarter - 1x2*", "4th Quarter - 1x2*",
        ]:
            assert classify_market_title(title) is None, f"should skip {title}"

    def test_quarter_odd_even_skipped(self):
        for title in [
            "1st. Quarter - Odd/Even Points",
            "2nd. Quarter - Odd/Even Points*",
            "3rd. Quarter - Odd/Even Points*",
            "4th. Quarter - Odd/Even Points*",
        ]:
            assert classify_market_title(title) is None, f"should skip {title}"


# ── Classifier: out-of-scope markets ──────────────────────────────────────────
class TestClassifyOutOfScope:
    """All these should return None — they're explicitly excluded from the
    21-combo scope per the user's decision."""

    @pytest.mark.parametrize("title", [
        # 3-way variants
        "Full Time Result(1X2)*",
        "Handicap(1X2)*",
        "Total (over-exact-under)*(Regular time)",
        # FT draw-no-bet (no period qualifier — 3-way derivative)
        "Draw No Bet",
        # Odd/even at all levels
        "Odd/Even (incl. overtime)*",
        "1st Half - Odd/Even Points",
        "2nd Half - Odd/Even Points*",
        # Will-there-be-overtime
        "Will There Be Overtime*",
        # Team totals (separate per team — out of scope)
        "HomeTeam Total (incl. overtime)*",
        "AwayTeam Total (incl. overtime)*",
        "1st half - Home Team total",
        "1st half - Away Team total",
        "1 quarter - Home Team total",
        "1 quarter - Away Team total",
        # Race-to
        "Race to 20 points (incl. overtime)",
        "Race to 30 points (incl. overtime)",
        # Lead-by
        "Any team to lead by 12",
        "competitor 1 to lead by 17",
        # Margin
        "Any Team Winning Margin (OT)",
        "1st Quarter - Winning margin",
        # Halftime/Fulltime grid
        "Halftime/Fulltime",
        # Highest/Lowest scoring quarter
        "Highest scoring quarter*",
        "Lowest scoring quarter - total",
        # Sequence
        "Team to win both halves",
        "{$competitor1} to win all quarters",   # CB template glitch
        "Any team to win all quarters",
        # Combo bets
        "Handicap (including OT) & Total (including OT) 2.5/217.5",
        "1st half handicap & 1st half total  1.5/111.5",
        "1st half 1x2 & 1st half total",
        "1st quarter handicap & 1st quarter total 0.5/55.5",
        "1st quarter 1x2 & 1st quarter total",
        # Player props
        "Assists Mobley, Evan (Cleveland Cavaliers)",
        "Rebounds Brunson, Jalen (New York Knicks)",
        "Steals (OT) Towns, Karl-Anthony (New York Knicks)",
        "Blocks Mobley, Evan (Cleveland Cavaliers)",
        "Points (OT) Brunson, Jalen (New York Knicks)",
        "3-Point Field Goals (OT) Wade, Dean (Cleveland Cavaliers)",
        "Double Double (incl. Overtime) Mitchell, Donovan (Cleveland Cavaliers)",
        "Triple Double (incl. Overtime) Harden, James (Cleveland Cavaliers)",
        "First Point Scorer (Allen, Jarrett (Cleveland Cavaliers))",
        "player total points (OT) Brunson, Jalen/26.5",
        "player total 3-point field goals (OT) Wade, Dean/0.5",
        "player total assists (OT)",
        "player total rebounds (OT)",
        # Specialty / specials
        "Any Team Total Maximum Consecutive Points",
        "Home Total Maximum Consecutive Points",
        "Point Range (regular time)",
        "1 Quarter - Last Point",
        "1st Free Throw Scored",
        # Combo "win 1st half/quarter and match"
        "To win 1st half and the match at last 1st team*",
        "To win 1st quarter and the match at last 1st team*",
        # Winner/Total combo
        "Winner / Total (incl. overtime)(OT)",
    ])
    def test_skipped_returns_none(self, title):
        assert classify_market_title(title) is None, \
            f"should skip {title!r} but got {classify_market_title(title)!r}"


# ── Classifier: edge cases ────────────────────────────────────────────────────
class TestClassifyEdgeCases:
    def test_empty_string_returns_none(self):
        assert classify_market_title("") is None

    def test_whitespace_only_returns_none(self):
        assert classify_market_title("   ") is None

    def test_unknown_market_returns_none(self):
        """A market we've never seen → silently None, no crash."""
        assert classify_market_title("Some Future Niche Bet (OT)") is None

    def test_classifier_is_case_insensitive(self):
        """CB sometimes mixes case across the same market name."""
        r1 = classify_market_title("Winner (incl. overtime)")
        r2 = classify_market_title("WINNER (INCL. OVERTIME)")
        r3 = classify_market_title("winner (incl. overtime)")
        assert r1 == r2 == r3
        assert r1 is not None

    def test_classifier_handles_trailing_asterisk(self):
        """CB marks 'live-betting available' with trailing '*'. Ignore it."""
        r_no_star = classify_market_title("2nd Half - Total")
        r_with_star = classify_market_title("2nd Half - Total*")
        assert r_no_star == r_with_star


# ── Smoke test against the real captured detail page ─────────────────────────
class TestRealDetailPageCoverage:
    """
    Sanity check: when fed the actual market titles from the Cavs/Knicks
    detail page (captured 2026-05-25), the classifier should map a healthy
    chunk to in-scope, skip the rest cleanly, and never raise.
    """
    SAMPLE = (
        Path(__file__).resolve().parents[1] /
        "data" / "raw" / "cb_single_match_detail.html"
    )

    def test_classifier_handles_every_real_market_title(self):
        if not self.SAMPLE.exists():
            pytest.skip(f"no captured detail page at {self.SAMPLE}")
        from bs4 import BeautifulSoup
        html = self.SAMPLE.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        det = soup.select_one("table.game-details")
        assert det is not None

        # CB lays out markets in a 2-column grid per <tr>:
        #   sport_more_td1 (title) + sport_more_td2 (selections)  ← left column
        #   sport_more_td3 (title) + sport_more_td4 (selections)  ← right column
        # Extract titles from BOTH columns.
        in_scope = 0
        out_of_scope = 0
        unique_combos: set[tuple[str, str]] = set()
        for title_td in det.select("td.sport_more_td1, td.sport_more_td3"):
            title = title_td.get_text(" ", strip=True)
            if not title:
                continue
            result = classify_market_title(title)
            if result is None:
                out_of_scope += 1
            else:
                in_scope += 1
                unique_combos.add((result.period, result.market_type))

        # We should classify a healthy chunk of the 21 combos (this particular
        # game probably has 12-21 of them populated; if it has 0 something is
        # wrong with the classifier).
        assert in_scope > 10, (
            f"expected > 10 in-scope markets for a real game, got {in_scope}"
        )
        assert out_of_scope > 0, "expected at least some skipped markets"

        # Must include the FT essentials — every basketball detail page has these.
        for required in [("FT", "moneyline"), ("FT", "spread"), ("FT", "total")]:
            assert required in unique_combos, (
                f"FT essential {required} missing from {unique_combos}"
            )
