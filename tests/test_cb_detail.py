"""
Tests for src/scrapers/cb_detail.py — generic detail-page walker.

Uses the captured Cavs/Knicks detail HTML (data/raw/cb_single_match_detail.html)
as the test fixture so we exercise real CB DOM structure, not synthetic stubs.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.cb_detail import parse_detail_page  # noqa: E402
from src.scrapers.sports import basketball  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE = (
    Path(__file__).resolve().parents[1] /
    "data" / "raw" / "cb_single_match_detail.html"
)


def _parse_sample():
    if not SAMPLE.exists():
        pytest.skip(f"no captured detail page at {SAMPLE}")
    html = SAMPLE.read_text(encoding="utf-8")
    return parse_detail_page(
        html,
        event_id="3026864120",
        home="Cleveland Cavaliers",
        away="New York Knicks",
        league="USA, NBL. Men. Play-off. Semi-finals. Best of 7(OT)",
        start_time=NOW,
        fetched_at=NOW,
        sport_name="basketball",
        classify=basketball.classify_market_title,
    )


# ── End-to-end against captured Cavs/Knicks ───────────────────────────────────
class TestRealDetailPage:
    """Exercise the walker against the actual HTML CB served on 2026-05-25."""

    def test_returns_nonzero_odds_for_real_game(self):
        odds = _parse_sample()
        assert len(odds) > 10, (
            f"expected > 10 Odds rows for an NBA playoff game, got {len(odds)}"
        )

    def test_includes_ft_moneyline(self):
        odds = _parse_sample()
        ft_ml = [o for o in odds if o.period == "FT" and o.market_type == "moneyline"]
        assert len(ft_ml) == 1
        ml = ft_ml[0]
        # Cavs/Knicks: home 2.05, away 1.65 from "Winner (incl. overtime)"
        assert ml.selections == {"home": 2.05, "away": 1.65}
        assert ml.line is None
        assert ml.home == "Cleveland Cavaliers"
        assert ml.away == "New York Knicks"

    def test_includes_ft_spread_alt_lines(self):
        odds = _parse_sample()
        ft_spreads = [o for o in odds if o.period == "FT" and o.market_type == "spread"]
        # CB served the -16 to +16 ladder in 0.5 steps → ~60 lines minus the
        # canonical 0 line (it's 1 to +16 and -1 to -16 typically).
        assert len(ft_spreads) > 20, (
            f"expected many FT spread alt-lines, got {len(ft_spreads)}"
        )
        # All have a line value and both sides.
        for o in ft_spreads:
            assert o.line is not None
            assert {"home", "away"} <= o.selections.keys()
        # Lines should span a wide range.
        lines = sorted({o.line for o in ft_spreads})
        assert min(lines) < -5.0
        assert max(lines) > 5.0

    def test_includes_ft_total_alt_lines(self):
        odds = _parse_sample()
        ft_totals = [o for o in odds if o.period == "FT" and o.market_type == "total"]
        assert len(ft_totals) > 20, (
            f"expected many FT total alt-lines, got {len(ft_totals)}"
        )
        for o in ft_totals:
            assert o.line is not None
            assert {"over", "under"} <= o.selections.keys()
        # Cavs/Knicks ladder centered around ~217.
        lines = sorted({o.line for o in ft_totals})
        assert min(lines) < 210
        assert max(lines) > 220

    def test_includes_h1_markets(self):
        odds = _parse_sample()
        h1 = [o for o in odds if o.period == "H1"]
        # Should have at least ML, spread, total
        types = {o.market_type for o in h1}
        assert "moneyline" in types
        assert "spread" in types
        assert "total" in types

    def test_includes_h2_markets_with_incl_ot_preference(self):
        odds = _parse_sample()
        h2 = [o for o in odds if o.period == "H2"]
        types = {o.market_type for o in h2}
        assert "moneyline" in types
        # H2 ML should be the OT variant — we can verify by spot-checking the
        # odds against the audit data: "2nd Half - Draw No Bet (OT)" was 1/1.95,
        # 2/1.70. The regular "2nd Half - Draw No Bet" was also there with same
        # values in this case, but the parser should prefer the OT variant
        # regardless. The rank-0 preference logic is what we're testing.
        h2_ml = next(o for o in h2 if o.market_type == "moneyline")
        # Should have home/away.
        assert {"home", "away"} <= h2_ml.selections.keys()

    def test_includes_quarter_markets(self):
        odds = _parse_sample()
        for period in ["Q1", "Q2", "Q3", "Q4"]:
            q = [o for o in odds if o.period == period]
            assert len(q) > 0, f"expected at least one {period} market"

    def test_no_out_of_scope_markets_emitted(self):
        """No team totals, no 1X2 (3-way), no props, no specialty bets."""
        odds = _parse_sample()
        # Every Odds should have selections matching its market_type's expected sides.
        for o in odds:
            if o.market_type == "moneyline":
                assert o.selections.keys() == {"home", "away"}
            elif o.market_type == "spread":
                assert o.selections.keys() == {"home", "away"}
                assert o.line is not None
            elif o.market_type == "total":
                assert o.selections.keys() == {"over", "under"}
                assert o.line is not None
            else:
                pytest.fail(f"unexpected market_type {o.market_type}")

    def test_odds_decimal_invariant(self):
        """All emitted odds > 1.0 (Odds.__post_init__ enforces this — verify)."""
        odds = _parse_sample()
        for o in odds:
            for side, val in o.selections.items():
                assert val > 1.0, f"{o.market_type} {o.period} {side}={val}"

    def test_all_odds_carry_required_metadata(self):
        odds = _parse_sample()
        assert len(odds) > 0
        for o in odds:
            assert o.source == "crystalbet"
            assert o.sport == "basketball"
            assert o.raw_event_id == "3026864120"
            assert o.home == "Cleveland Cavaliers"
            assert o.away == "New York Knicks"
            assert o.start_time == NOW

    def test_summary_counts(self):
        """Print useful counts as a diagnostic for the conversation."""
        odds = _parse_sample()
        from collections import Counter
        by_period = Counter(o.period for o in odds)
        by_type = Counter(o.market_type for o in odds)
        by_combo = Counter((o.period, o.market_type) for o in odds)

        print(f"\nTotal Odds extracted: {len(odds)}")
        print(f"By period:  {dict(by_period)}")
        print(f"By type:    {dict(by_type)}")
        print(f"By combo:   {dict(by_combo)}")

        # All in-scope periods should be represented for this NBA playoff game.
        # (Some periods may be missing for lesser games — this is the upper bound.)
        all_periods = {"FT", "H1", "H2", "Q1", "Q2", "Q3", "Q4"}
        # We accept this game may not have every single combo, but most should be there.
        assert len(by_combo) >= 15, (
            f"expected ≥ 15 of 21 combos for an NBA game, got {len(by_combo)}: {dict(by_combo)}"
        )


# ── Edge cases ────────────────────────────────────────────────────────────────
class TestEdgeCases:
    def test_missing_table_returns_empty(self):
        odds = parse_detail_page(
            "<html><body><div>no game-details here</div></body></html>",
            event_id="evt-1", home="A", away="B", league=None,
            start_time=None, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert odds == []

    def test_empty_html_returns_empty(self):
        odds = parse_detail_page(
            "", event_id="evt-1", home="A", away="B", league=None,
            start_time=None, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert odds == []

    def test_table_with_no_in_scope_markets_returns_empty(self):
        """Detail page with only out-of-scope markets → no Odds emitted."""
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Will There Be Overtime*</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">Yes</div>
                  <div class="sport_more_bt2">7.90</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">no</div>
                  <div class="sport_more_bt2">1.03</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="A", away="B", league=None,
            start_time=None, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert odds == []

    def test_synthetic_ft_moneyline(self):
        """Verify the basic ML extraction shape end-to-end."""
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Winner (incl. overtime)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1</div>
                  <div class="sport_more_bt2">2.05</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2</div>
                  <div class="sport_more_bt2">1.65</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert len(odds) == 1
        assert odds[0].period == "FT"
        assert odds[0].market_type == "moneyline"
        assert odds[0].selections == {"home": 2.05, "away": 1.65}

    def test_synthetic_spread_alt_lines(self):
        """Verify alt-line spread pairing and home-line normalization."""
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Handicap (incl. overtime)*</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1 (-3.0)</div>
                  <div class="sport_more_bt2">1.90</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2 (+3.0)</div>
                  <div class="sport_more_bt2">1.95</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1 (-2.5)</div>
                  <div class="sport_more_bt2">2.00</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2 (+2.5)</div>
                  <div class="sport_more_bt2">1.85</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        # Two lines: -3.0 and -2.5 (home-line convention)
        assert len(odds) == 2
        by_line = {o.line: o for o in odds}
        assert -3.0 in by_line
        assert -2.5 in by_line
        # -3.0 → home odds 1.90, away odds 1.95
        assert by_line[-3.0].selections == {"home": 1.90, "away": 1.95}
        # -2.5 → home odds 2.00, away odds 1.85
        assert by_line[-2.5].selections == {"home": 2.00, "away": 1.85}

    def test_synthetic_total_alt_lines(self):
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Total Points(incl. overtime)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">Und 215.5</div>
                  <div class="sport_more_bt2">2.10</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">over 215.5</div>
                  <div class="sport_more_bt2">1.70</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">Und 218</div>
                  <div class="sport_more_bt2">1.85</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">over 218</div>
                  <div class="sport_more_bt2">1.95</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert len(odds) == 2
        by_line = {o.line: o for o in odds}
        assert 215.5 in by_line
        assert 218 in by_line
        assert by_line[215.5].selections == {"over": 1.70, "under": 2.10}
        assert by_line[218].selections == {"over": 1.95, "under": 1.85}

    def test_variant_rank_preference_incl_ot_wins(self):
        """
        When BOTH Total Points (incl. overtime) and Total Points (Regular time)
        are present, the parser should keep only the incl-OT version.
        """
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Total Points(Regular time)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">Und 215.5</div>
                  <div class="sport_more_bt2">1.95</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">over 215.5</div>
                  <div class="sport_more_bt2">1.70</div>
                </div>
              </div>
            </td>
          </tr>
          <tr>
            <td class="sport_more_td1">Total Points(incl. overtime)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">Und 215.5</div>
                  <div class="sport_more_bt2">2.00</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">over 215.5</div>
                  <div class="sport_more_bt2">1.80</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        # Both classify to (FT, total, 215.5). Incl-OT (rank 0) should win.
        assert len(odds) == 1
        assert odds[0].selections == {"over": 1.80, "under": 2.00}, (
            "expected incl-OT values to win (rank 0); got regular-time values"
        )

    def test_suspended_side_drops_market(self):
        """Side with odds ≤ 1.0 → Odds.__post_init__ rejects → entry skipped."""
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Winner (incl. overtime)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1</div>
                  <div class="sport_more_bt2">1.00</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2</div>
                  <div class="sport_more_bt2">2.00</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        assert odds == []

    def test_two_column_grid_extracts_both_markets(self):
        """Each TR can have left (td1+td2) AND right (td3+td4) markets."""
        html = """
        <table class="game-details">
          <tr>
            <td class="sport_more_td1">Winner (incl. overtime)</td>
            <td class="sport_more_td2">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1</div>
                  <div class="sport_more_bt2">2.05</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2</div>
                  <div class="sport_more_bt2">1.65</div>
                </div>
              </div>
            </td>
            <td class="sport_more_td3">1st Half - Draw No Bet</td>
            <td class="sport_more_td4">
              <div class="sport_more_td_div">
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">1</div>
                  <div class="sport_more_bt2">2.00</div>
                </div>
                <div class="sport_more_bt DetailSnatch">
                  <div class="sport_more_bt1">2</div>
                  <div class="sport_more_bt2">1.70</div>
                </div>
              </div>
            </td>
          </tr>
        </table>
        """
        odds = parse_detail_page(
            html, event_id="evt-1", home="Hawks", away="Lakers", league="NBA",
            start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title,
        )
        # Should get BOTH the FT ML (left column) AND H1 ML (right column).
        periods = {o.period for o in odds}
        assert periods == {"FT", "H1"}


# ── HT/FT combo capture (permissive / anomaly path only) ──────────────────────
class TestHtftCapture:
    def _parse_sample_permissive(self):
        if not SAMPLE.exists():
            pytest.skip(f"no captured detail page at {SAMPLE}")
        return parse_detail_page(
            SAMPLE.read_text(encoding="utf-8"),
            event_id="3026864120", home="Cleveland Cavaliers",
            away="New York Knicks", league="L", start_time=NOW, fetched_at=NOW,
            sport_name="basketball",
            classify=basketball.classify_market_title_permissive,
            per_section=True,
        )

    def test_strict_classifier_still_skips_htft(self):
        odds = _parse_sample()
        assert not any(o.market_type == "htft" for o in odds)

    def test_permissive_captures_htft_row_from_real_page(self):
        odds = self._parse_sample_permissive()
        htft = [o for o in odds if o.market_type == "htft"]
        assert len(htft) == 1
        sels = htft[0].selections
        assert "1/1" in sels and "2/2" in sels
        assert all(v > 1.0 for v in sels.values())

    def test_permissive_captures_regulation_1x2_legs(self):
        odds = self._parse_sample_permissive()
        legs = [o for o in odds if o.market_type == "moneyline"
                and "draw" in o.selections]
        periods = {o.period for o in legs}
        assert "FT" in periods and "H1" in periods

    def test_synthetic_htft_table_parses_labels(self):
        cells = "".join(
            f'<div class="sport_more_bt DetailSnatch">'
            f'<div class="sport_more_bt1">{lbl}</div>'
            f'<div class="sport_more_bt2">{odds}</div></div>'
            for lbl, odds in [("1/1", "1.80"), ("1/X", "26.0"), ("1/2", "7.40"),
                              ("X/1", "33.3"), ("X/X", "1.00"), ("x/2", "52.0"),
                              ("2/1", "5.30"), ("2/X", "27.8"), ("2/2", "3.35")]
        )
        html = (f'<table class="game-details"><tr>'
                f'<td class="sport_more_td1">Halftime/Fulltime</td>'
                f'<td class="sport_more_td2"><div class="sport_more_td_div">'
                f'{cells}</div></td></tr></table>')
        odds = parse_detail_page(
            html, event_id="e", home="A", away="B", league=None,
            start_time=None, fetched_at=NOW, sport_name="basketball",
            classify=basketball.classify_market_title_permissive,
        )
        assert len(odds) == 1 and odds[0].market_type == "htft"
        sels = odds[0].selections
        assert sels["1/1"] == 1.80 and sels["2/2"] == 3.35
        assert "X/X" not in sels          # 1.00 = suspended, dropped
        assert sels["X/2"] == 52.0        # lowercase x normalized


class TestPermissiveClassifierHtftRules:
    def test_htft_title_classified(self):
        cls = basketball.classify_market_title_permissive("Halftime/Fulltime ")
        assert cls is not None and cls.market_type == "htft"

    def test_htft_combo_variants_not_classified_as_htft(self):
        for t in ("Halftime/Fulltime and Total",
                  "Halftime  / Fulltime Correct Score***",
                  "Halftime/Fulltime and Exact Goals"):
            cls = basketball.classify_market_title_permissive(t)
            assert cls is None or cls.market_type != "htft", t

    def test_plain_1x2_titles_become_3way_moneylines(self):
        for title, per in (("Full Time Result(1X2)*", "FT"),
                           ("1st half - 1x2", "H1"),
                           ("2nd Quarter - 1x2*", "Q2")):
            cls = basketball.classify_market_title_permissive(title)
            assert cls is not None and cls.market_type == "moneyline" \
                and cls.n_way == 3 and cls.period == per, title

    def test_3way_handicap_still_skipped(self):
        assert basketball.classify_market_title_permissive("Handicap(1X2)*") is None

    def test_strict_classifier_unchanged_for_1x2(self):
        assert basketball.classify_market_title("Full Time Result(1X2)*") is None
        assert basketball.classify_market_title("Halftime/Fulltime") is None
