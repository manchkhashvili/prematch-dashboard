"""
duplicate_fixture — the same match listed twice by one book.

Every other consistency check compares two markets inside one event. This one
compares two EVENTS, which is why it needs its own guards: the normaliser that
makes "Man Utd" and "Manchester United" the same team also makes a youth side
the same as its senior one.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.consistency import find_consistency_flags  # noqa: E402
from src.models import Odds  # noqa: E402

T0 = datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc)


def _ev(eid, home, away, ml, start=T0, sport="soccer", league="Test League"):
    """One event: a full-time moneyline, keyed by its own event id."""
    return [Odds(source="crystalbet", sport=sport, home=home, away=away,
                 market_type="moneyline", period="FT", selections=dict(ml),
                 fetched_at=T0, start_time=start, league=league,
                 raw_event_id=eid)]


def _dups(odds):
    return [f for f in find_consistency_flags(odds) if f.kind == "duplicate_fixture"]


class TestFires:
    def test_same_teams_same_kickoff_is_flagged(self):
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.5, "draw": 3.4, "away": 2.8})
                + _ev("B", "SSC Napoli", "Arsenal FC",
                      {"home": 2.5, "draw": 3.4, "away": 2.8}))
        f = _dups(odds)
        assert len(f) == 1
        assert "A" in f[0].detail and "B" in f[0].detail

    def test_one_flag_per_pair_not_two(self):
        """ConsistencyFlag is keyed to a single event, so the pair is reported
        from one side — reporting it twice would double every alert."""
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8})
                + _ev("B", "SSC Napoli", "Arsenal FC", {"home": 2.5, "away": 2.8}))
        assert len(_dups(odds)) == 1

    def test_mispriced_duplicate_is_flagged_and_names_the_locked_cover(self):
        """Best price per outcome across BOTH listings: 2.10 home from one,
        2.10 away from the other covers the market for 0.952 — money with no
        losing branch. Severity is the disagreement (23.5%); the locked figure
        goes in the detail text."""
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.10, "away": 1.70})
                + _ev("B", "SSC Napoli", "Arsenal FC", {"home": 1.70, "away": 2.10}))
        f = _dups(odds)
        assert len(f) == 1
        assert f[0].severity == 100.0
        assert "LOCKED" in f[0].detail

    def test_severity_is_a_flat_100_even_with_identical_prices(self):
        """A duplicate always alerts, whatever the panel threshold. Two earlier
        versions failed this: the locked edge scored an identically-priced pair
        at -5.3 (the book's overround) and the disagreement scored it 0 —
        neither clears a positive bar."""
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 1.90, "away": 1.90})
                + _ev("B", "SSC Napoli", "Arsenal FC", {"home": 1.90, "away": 1.90}))
        f = _dups(odds)
        assert len(f) == 1 and f[0].severity == 100.0
        assert 'cover 1.0526' in f[0].detail


class TestSwappedSides:
    """A duplicate listed with the sides reversed — "Rostov v CSKA" against
    "CSKA v Rostov". Measured on the tick store this is a large share of the
    real ones and the most bettable, since backing the same team on both
    listings is one bet at two prices. An earlier version gated the cover
    behind `not flipped` and reported every one of them as "prices not
    comparable" with no edge figure at all."""

    def test_swapped_duplicate_is_flagged(self):
        odds = (_ev("A", "FC Rostov", "CSKA Moscow", {"home": 2.0, "away": 3.0})
                + _ev("B", "CSKA Moscow", "FC Rostov", {"home": 3.2, "away": 1.9}))
        f = _dups(odds)
        assert len(f) == 1 and "sides flipped" in f[0].detail

    def test_swapped_duplicate_prices_its_cover_after_realigning(self):
        """B is realigned onto A before comparing. Best of both is then
        home 2.00 (from A) and away 3.20 (B's home leg) = 0.8125 outlay."""
        odds = (_ev("A", "FC Rostov", "CSKA Moscow", {"home": 2.0, "away": 3.0})
                + _ev("B", "CSKA Moscow", "FC Rostov", {"home": 3.2, "away": 1.9}))
        d = _dups(odds)[0].detail
        assert "cover 0.8125" in d and "LOCKED 18.75%" in d
        assert "not comparable" not in d

    def test_swapped_three_way_keeps_the_draw_in_place(self):
        odds = (_ev("A", "Rostov", "CSKA", {"home": 2.0, "draw": 3.4, "away": 3.0})
                + _ev("B", "CSKA", "Rostov", {"home": 3.2, "draw": 3.5, "away": 1.9}))
        d = _dups(odds)[0].detail
        assert "not comparable" not in d and "cover" in d


class TestLeagueIsIrrelevant:
    """League plays no part in the test. The same match listed under two
    different league names is exactly the case worth catching, so it must not
    be treated as evidence of two different fixtures."""

    def test_wildly_different_leagues_still_pair(self):
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.1, "away": 1.7},
                    league="UEFA, Champions League")
                + _ev("B", "SSC Napoli", "Arsenal FC", {"home": 1.7, "away": 2.1},
                      league="Italy, Serie A"))
        assert len(_dups(odds)) == 1

    def test_a_womens_match_split_across_league_labels_is_still_caught(self):
        """The regression the league-blind guard exists for. An earlier version
        fed `league` into the women guard, so a women's fixture listed once
        under a league labelled "Women" and once under one that is not scored
        as two different matches and was silently dropped."""
        odds = (_ev("A", "Santiago", "Colo Colo", {"home": 2.1, "away": 1.7},
                    league="Chile, Primera Division, Women")
                + _ev("B", "Santiago", "Colo Colo", {"home": 1.7, "away": 2.1},
                      league="Chile, Primera Division"))
        assert len(_dups(odds)) == 1


class TestGuards:
    def test_a_youth_tag_on_one_side_only_still_pairs(self):
        """No youth guard, by measurement. Over 3 months of the tick store a
        youth/senior guard blocked 11 pairs, 3 of them on CrystalBet and
        Lider-Bet, and those three were the mislabelling this check exists for —
        "FCI Tallinn II v Tabasalu Ulasabat" against "Fci Levadia Tallinn U19 v
        Tabasalu", one club tagged II and the other U19. Two genuinely different
        fixtures between the same teams do not share a kickoff minute, so the
        tag adds nothing the kickoff has not already settled."""
        odds = (_ev("A", "FCI Tallinn II", "Tabasalu", {"home": 2.0, "away": 3.0})
                + _ev("B", "FCI Tallinn U19", "Tabasalu", {"home": 2.0, "away": 3.0}))
        assert len(_dups(odds)) == 1

    def test_a_women_tag_on_one_side_only_still_pairs(self):
        """Same reasoning; the women guard blocked 2 pairs in the same scan."""
        odds = (_ev("A", "Chelsea Women", "Arsenal Women", {"home": 2.0, "away": 3.0},
                    league="FA WSL Women")
                + _ev("B", "Chelsea", "Arsenal", {"home": 2.0, "away": 3.0}))
        assert len(_dups(odds)) == 1

    def test_different_kickoff_is_not_a_duplicate(self):
        """Identical kickoff is the whole rule. A wider window immediately
        starts reporting genuinely different fixtures."""
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8})
                + _ev("B", "SSC Napoli", "Arsenal FC", {"home": 2.5, "away": 2.8},
                      start=T0 + timedelta(minutes=30)))
        assert _dups(odds) == []

    def test_different_teams_at_the_same_kickoff_are_untouched(self):
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8})
                + _ev("B", "Liverpool", "Atletico Madrid", {"home": 2.0, "away": 3.5}))
        assert _dups(odds) == []

    def test_different_sports_never_pair(self):
        odds = (_ev("A", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8})
                + _ev("B", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8},
                      sport="basketball"))
        assert _dups(odds) == []

    def test_a_single_listing_flags_nothing(self):
        assert _dups(_ev("A", "Napoli", "Arsenal", {"home": 2.5, "away": 2.8})) == []
