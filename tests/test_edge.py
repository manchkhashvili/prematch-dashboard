"""
Regression tests for prematch.src.edge — +EV and ARB detection.

Each test guards a specific behavior or bug from the build log:

  - +EV direction: high CB / low Pin → positive edge
  - ARB detection: 1/d1 + 1/d2 < 1 → arb row emitted
  - pin_no_vig has SAME meaning on +EV and ARB rows (2026-05-24 "Post-launch UX")
  - arb_partner_odds populated only on ARB rows
  - cb_event_id populated on every Opportunity (deep-link from arbs page)
  - Period-and-line filter in _find_pin_match prevents phantom edges

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.edge import compute_opportunities, _find_pin_match  # noqa: E402
from src.matcher import MatchedEvent  # noqa: E402
from src.models import Odds  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _odds(
    source: str,
    market_type: str,
    selections: dict,
    *,
    period: str = "FT",
    line: float | None = None,
    home: str = "Hawks",
    away: str = "Lakers",
    event_id: str = "evt-1",
) -> Odds:
    return Odds(
        source=source,  # type: ignore[arg-type]
        sport="basketball",
        home=home,
        away=away,
        market_type=market_type,  # type: ignore[arg-type]
        period=period,  # type: ignore[arg-type]
        selections=selections,
        fetched_at=NOW,
        line=line,
        start_time=NOW,
        league="NBA",
        raw_event_id=event_id,
    )


def _match(cb_list: list[Odds], pin_list: list[Odds]) -> MatchedEvent:
    return MatchedEvent(
        cb=cb_list, pin=pin_list,
        home="Hawks", away="Lakers", score=100.0,
    )


# ── +EV pass ──────────────────────────────────────────────────────────────────
class TestPositiveEV:
    def test_high_cb_low_pin_emits_positive_edge(self):
        """CB pays 2.10 on a side Pinnacle's fair price values at ~1.90."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.10, "away": 1.80})
        # Pinnacle: 1.91/1.91 (vigged) → devigs to 0.50/0.50 → fair 2.00/2.00
        pin = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        evs = [o for o in opps if o.kind == "+EV"]

        # Home side: cb 2.10 vs pin_fair 2.00 → +5% edge.
        home_ev = next((o for o in evs if o.side == "home"), None)
        assert home_ev is not None
        assert home_ev.edge_pct == pytest.approx(5.0, abs=0.01)
        # Away side: cb 1.80 vs pin_fair 2.00 → negative edge → not emitted.
        away_ev = next((o for o in evs if o.side == "away"), None)
        assert away_ev is None

    def test_negative_edge_suppressed_by_min_edge_threshold(self):
        """min_edge_pct=1.0 (default) drops opportunities below 1%."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.005, "away": 2.005})
        pin = _odds("pinnacle", "moneyline", {"home": 2.00, "away": 2.00})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=1.0)
        evs = [o for o in opps if o.kind == "+EV"]
        # Edge is ~0.25%, well below 1% threshold.
        assert evs == []


# ── ARB pass ──────────────────────────────────────────────────────────────────
class TestArbitrage:
    def test_arb_detected_when_inverse_sum_below_one(self):
        """
        CB 2.20 home + Pin 2.20 away (vigged on opposite side) → 1/2.20 + 1/2.20
        = 0.909 < 1 → ARB. Edge = 1 - 0.909 = ~9.1%.
        """
        cb = _odds("crystalbet", "moneyline", {"home": 2.20, "away": 1.50})
        pin = _odds("pinnacle", "moneyline", {"home": 1.50, "away": 2.20})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        arbs = [o for o in opps if o.kind == "ARB"]

        # The CB-home + Pin-away pairing yields the arb.
        cb_home_arb = next(
            (o for o in arbs if o.side == "home"), None,
        )
        assert cb_home_arb is not None
        assert cb_home_arb.edge_pct == pytest.approx(9.0909, abs=0.01)
        # Partner-leg is Pinnacle's away price.
        assert cb_home_arb.arb_partner_side == "away"
        assert cb_home_arb.arb_partner_odds == pytest.approx(2.20)

    def test_no_arb_when_inverse_sum_at_or_above_one(self):
        """Standard vigged-vs-vigged market — no arb."""
        cb = _odds("crystalbet", "moneyline", {"home": 1.91, "away": 1.91})
        pin = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        arbs = [o for o in opps if o.kind == "ARB"]
        assert arbs == []


# ── pin_no_vig semantics consistency ──────────────────────────────────────────
class TestPinNoVigConsistency:
    """
    The 2026-05-24 post-launch UX fix unified `pin_no_vig`'s meaning across
    kinds: it's always the devigged same-side fair price for the CB row's
    side. The partner-leg vigged price for ARB lives in arb_partner_odds.
    """

    def test_pin_no_vig_is_devigged_same_side_for_ev(self):
        cb = _odds("crystalbet", "moneyline", {"home": 2.50, "away": 1.50})
        pin = _odds("pinnacle", "moneyline", {"home": 2.00, "away": 2.00})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        ev_home = next(o for o in opps if o.kind == "+EV" and o.side == "home")
        # Pin 2.00/2.00 → fair 0.5/0.5 → fair_decimal 2.0/2.0.
        assert ev_home.pin_no_vig == pytest.approx(2.0)

    def test_pin_no_vig_matches_across_ev_and_arb_for_same_side(self):
        """Same CB side, same Pin market: the `pin_no_vig` value must be
        identical whether the row is rendered as +EV or ARB."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.20, "away": 1.50})
        pin = _odds("pinnacle", "moneyline", {"home": 1.55, "away": 3.00})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=-100.0)
        ev_home = next((o for o in opps if o.kind == "+EV" and o.side == "home"), None)
        arb_home = next((o for o in opps if o.kind == "ARB" and o.side == "home"), None)
        # Both must exist in this construction (large CB edge + arb-possible Pin).
        assert ev_home is not None
        assert arb_home is not None
        # Pin_no_vig values must match — same devigged fair price.
        assert ev_home.pin_no_vig == pytest.approx(arb_home.pin_no_vig)


# ── Field population: arb_partner_*, kelly_stake, cb_event_id ─────────────────
class TestFieldPopulation:
    def test_arb_partner_fields_populated_only_on_arb(self):
        cb = _odds("crystalbet", "moneyline", {"home": 2.20, "away": 1.50})
        pin = _odds("pinnacle", "moneyline", {"home": 1.50, "away": 2.20})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        for o in opps:
            if o.kind == "ARB":
                assert o.arb_partner_side is not None
                assert o.arb_partner_odds is not None
            else:
                assert o.arb_partner_side is None
                assert o.arb_partner_odds is None

    def test_kelly_stake_populated_only_on_ev(self):
        """ARB rows leave kelly_stake at 0 per the v1 scope note in edge.py."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.20, "away": 1.50})
        pin = _odds("pinnacle", "moneyline", {"home": 1.50, "away": 2.20})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        for o in opps:
            if o.kind == "+EV":
                assert o.kelly_stake >= 0.0  # may be 0 if Kelly says don't bet
            else:
                assert o.kelly_stake == 0.0

    def test_cb_event_id_populated_on_every_opportunity(self):
        """Drives the arbs→matches deep-link in the dashboard."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.20, "away": 1.50},
                   event_id="cb-evt-deep-link")
        pin = _odds("pinnacle", "moneyline", {"home": 1.50, "away": 2.20})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        assert opps  # must have produced something
        for o in opps:
            assert o.cb_event_id == "cb-evt-deep-link"


# ── _find_pin_match: period + line filter ─────────────────────────────────────
class TestFindPinMatch:
    """
    Drops in the brief about phantom edges all trace back to cross-source
    pairings that ignored period or line. This locks in the filter.
    """

    def test_pin_match_requires_matching_period(self):
        """CB FT must not pair with Pin H1 even if market_type matches."""
        cb_ft = _odds("crystalbet", "moneyline", {"home": 1.91, "away": 1.91},
                      period="FT")
        pin_h1 = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91},
                       period="H1")
        assert _find_pin_match(cb_ft, [pin_h1]) is None

    def test_pin_match_requires_matching_market_type(self):
        cb_ml = _odds("crystalbet", "moneyline", {"home": 1.91, "away": 1.91})
        pin_spread = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                           line=-3.5)
        assert _find_pin_match(cb_ml, [pin_spread]) is None

    def test_pin_match_requires_matching_line_for_spread(self):
        """CB spread -3.5 must NOT pair with Pin spread -6.5 — different
        line entirely (outside LINE_MATCH_TOLERANCE=0.5)."""
        cb_sp = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91},
                      line=-3.5)
        pin_sp_far = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                           line=-6.5)
        assert _find_pin_match(cb_sp, [pin_sp_far]) is None

    def test_pin_match_rejects_different_line(self):
        """Phase 3.1.2: -3.5 vs -3.0 are different bets, should NOT match.
        Pre-fix tolerance was 0.5 and this paired — false edges. User reported
        the same bug as CB +1.0 falsely matching Pin +1.5."""
        cb_sp = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91},
                      line=-3.5)
        pin_sp = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                       line=-3.0)
        assert _find_pin_match(cb_sp, [pin_sp]) is None

    def test_pin_match_accepts_float_noise(self):
        """Tolerance of 0.01 covers float-rounding noise; real line differences don't pair."""
        cb_sp = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91},
                      line=-3.5)
        pin_sp = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                       line=-3.5000001)
        assert _find_pin_match(cb_sp, [pin_sp]) is pin_sp

    def test_pin_match_picks_exact_line_among_candidates(self):
        """Multiple Pin candidates at different lines — only the exact-line one matches."""
        cb_sp = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91},
                      line=-3.5)
        pin_exact = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                          line=-3.5, event_id="pin-exact")
        pin_other = _odds("pinnacle", "spread", {"home": 1.91, "away": 1.91},
                          line=-3.0, event_id="pin-other")
        assert _find_pin_match(cb_sp, [pin_other, pin_exact]) is pin_exact


# ── Pinnacle max-stake passthrough (2026-06-12) ───────────────────────────────
def test_opportunity_carries_pin_max_stake():
    cb = _odds("crystalbet", "moneyline", {"home": 2.40, "away": 1.55})
    pin = _odds("pinnacle", "moneyline", {"home": 1.90, "away": 1.95})
    pin.max_stake = 525.0
    opps = compute_opportunities([MatchedEvent(cb=[cb], pin=[pin],
                                               home="Hawks", away="Lakers",
                                               score=100.0)],
                                 min_edge_pct=1.0)
    assert opps, "expected at least one opportunity from a 2.40 vs ~1.9 fair gap"
    assert all(o.pin_max_stake == 525.0 for o in opps)


# ── Match confidence (2026-06-19) ─────────────────────────────────────────────
from src.edge import match_confidence  # noqa: E402
from src.models import Opportunity  # noqa: E402


def _opp(**over):
    f = dict(start_time=NOW, match_label="A — B", market="ML FT", side="home",
             cb_odds=2.0, pin_no_vig=2.0, edge_pct=5.0, kind="+EV", kelly_stake=10.0,
             match_score=100.0, match_time_delta_sec=0.0)
    f.update(over)
    return Opportunity(**f)


def test_match_signals_plumbed_onto_opportunity():
    cb = _odds("crystalbet", "moneyline", {"home": 2.10, "away": 1.80})
    pin = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91})
    o = next(o for o in compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
             if o.side == "home")
    assert o.match_score == 100.0
    assert o.match_time_delta_sec == 0.0       # both start_time = NOW


def test_confidence_strong_clean_match():
    assert match_confidence(_opp(match_score=95.0, edge_pct=5.0, match_time_delta_sec=0)) == "strong"


def test_confidence_weak_on_implausible_edge():
    # The Serie D case: perfect name + same kickoff but a +145% edge = wrong game.
    assert match_confidence(_opp(match_score=100.0, edge_pct=145.0, match_time_delta_sec=0)) == "weak"


def test_confidence_weak_on_low_name_score():
    assert match_confidence(_opp(match_score=70.0, edge_pct=5.0)) == "weak"


def test_confidence_weak_on_far_kickoff():
    assert match_confidence(_opp(match_score=95.0, edge_pct=5.0,
                                 match_time_delta_sec=60 * 60)) == "weak"


def test_confidence_medium_when_uncertain():
    # decent (not strong) name, small edge → medium
    assert match_confidence(_opp(match_score=82.0, edge_pct=5.0)) == "medium"
    # a big-but-not-huge edge caps even a great name down to medium
    assert match_confidence(_opp(match_score=95.0, edge_pct=15.0)) == "medium"


def test_confidence_weak_on_notable_edge_with_imperfect_name():
    # the wrong-but-similar match: a 12% +EV only a so-so name (82) vouches for.
    assert match_confidence(_opp(match_score=82.0, edge_pct=12.0)) == "weak"


def test_confidence_arb_threshold_tighter_than_ev():
    # 15% is implausible for an ARB vs Pinnacle → weak (ARB huge=12), but only
    # big-ish for +EV → medium (EV huge=25).
    assert match_confidence(_opp(kind="ARB", edge_pct=15.0, match_score=100.0)) == "weak"
    assert match_confidence(_opp(kind="+EV", edge_pct=15.0, match_score=100.0)) == "medium"
