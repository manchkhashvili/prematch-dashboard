"""Recall + section-isolation tests for the anomaly path.

These guard the 2026-05-31 fix where the detector was missing markets whose
detail-page title isn't in the strict allowlist (e.g. "Asian Handicap 1st
Period"), AND where naively broadening the classifier merged distinct sections
into one ladder and fabricated violations. The fix: a permissive classifier +
per-section parsing + section-keyed ladder grouping.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.anomalies import find_ladder_anomalies
from src.models import Odds
from src.scrapers.cb_detail import parse_detail_page
from src.scrapers.sports.basketball import (
    _derive_period,
    classify_market_title as STRICT,
    classify_market_title_permissive as PERM,
)

NOW = datetime(2026, 5, 31, tzinfo=timezone.utc)


def _spread(line, h, a, *, section=None, event="E1"):
    return Odds(source="crystalbet", sport="basketball", home="H", away="A",
                market_type="spread", period="FT", selections={"home": h, "away": a},
                fetched_at=NOW, line=line, league="L", raw_event_id=event, section=section)


# ── permissive classifier ─────────────────────────────────────────────────────

def test_permissive_catches_handicap_titles_strict_drops():
    for title, period in [
        ("Asian Handicap 1st Period", "H1"),
        ("Asian Handicap", "FT"),
        ("2nd Period Asian Handicap", "H2"),
        ("1st. Quarter - Handicap", "Q1"),
        ("4rt. Quarter - Handicap", "Q4"),
    ]:
        c = PERM(title)
        assert c is not None and c.market_type == "spread" and c.period == period, title
    # The strict classifier drops the unenumerated phrasings.
    assert STRICT("Asian Handicap 1st Period") is None


def test_permissive_still_skips_prop_and_3way_noise():
    for title in [
        "Total (over-exact-under)*(Regular time)",
        "HomeTeam Total (incl. overtime)*",
        "1 quarter - Home Team total",
        "player total points (OT) Harden, James/18.5",
        "Any Team Total Maximum Consecutive Points",
        "Highest scoring quarter - total",
        "Full Time Result (1x2)",
        "Odd/Even",
    ]:
        assert PERM(title) is None, title


def test_derive_period_handles_dotted_quarter():
    assert _derive_period("1st. quarter - handicap") == "Q1"
    assert _derive_period("4rt. quarter - handicap") == "Q4"
    assert _derive_period("asian handicap 1st period") == "H1"
    assert _derive_period("2nd half - handicap (incl. overtime)") == "H2"
    assert _derive_period("asian handicap") == "FT"


# ── per-section parse end-to-end (the screenshot) ─────────────────────────────

def _snatch(label, odds):
    return (f'<div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">{label}'
            f'</div><div class="sport_more_bt2">{odds}</div></div>')


def test_asian_handicap_section_is_parsed_and_flagged():
    cells = "".join([_snatch("1(+3.5)", "1.75"), _snatch("2(-3.5)", "1.85"),
                     _snatch("1(+4.5)", "1.90"), _snatch("2(-4.5)", "1.70")])
    html = (f'<table class="game-details"><tr>'
            f'<td class="sport_more_td1">Asian Handicap 1st Period</td>'
            f'<td class="sport_more_td2"><div class="sport_more_td_div">{cells}</div></td>'
            f'</tr></table>')
    odds = parse_detail_page(html, event_id="E", home="A", away="B", league="L",
                             start_time=NOW, fetched_at=NOW, sport_name="basketball",
                             classify=PERM, scope_to_event=False, per_section=True)
    assert odds and all(o.section == "Asian Handicap 1st Period" for o in odds)
    anoms = find_ladder_anomalies(odds)
    home = next(a for a in anoms if a.side == "home")
    assert (home.line_lo, home.line_hi) == (3.5, 4.5)
    assert home.section == "Asian Handicap 1st Period"
    assert round(home.pct, 1) == 8.6


# ── section isolation prevents cross-section interleave ───────────────────────

def test_distinct_sections_do_not_interleave():
    # Section A is internally clean; section B is a single rung at a line that
    # sits between A's lines. Merged into one ladder they'd fabricate a
    # violation; kept per-section they must not.
    a1 = _spread(1.0, 1.50, 2.50, section="Handicap (incl. overtime)")
    a2 = _spread(3.0, 1.30, 2.90, section="Handicap (incl. overtime)")
    b1 = _spread(2.0, 1.90, 2.10, section="Asian Handicap")
    assert find_ladder_anomalies([a1, a2, b1]) == []     # section-isolated → clean

    # Control: strip the section tags → they merge and the interleave shows up.
    merged = [_spread(1.0, 1.50, 2.50), _spread(2.0, 1.90, 2.10), _spread(3.0, 1.30, 2.90)]
    assert len(find_ladder_anomalies(merged)) >= 1
