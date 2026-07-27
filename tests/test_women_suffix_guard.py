"""Bare trailing-"W" women's suffix must not match a men's fixture.

Found live 2026-07-26: Betlive writes a women's game as
"Boca Juniors W — San Lorenzo de Almagro W" — no parentheses, no "Women" in the
league — which `_WOMEN_TAG` did not catch. token_set_ratio treats
"boca juniors" as a subset of "boca juniors w" and scores 100, so the WOMEN'S
fixture matched the MEN'S Pinnacle event (pin_event 1632820968) at
`confidence: strong`. Strong-confidence phantoms are the dangerous class —
nothing in the UI warns you.

A blanket trailing-W rule would be worse: English clubs abbreviate Wanderers
the same way. Women's fixtures suffix BOTH sides; "Bolton W" faces an
unsuffixed opponent.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.matcher import match_events
from src.models import Odds
from src.normalize import has_women_suffix_pair

NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)


def _ml(source, home, away, league=None, h=2.5, a=2.6):
    return Odds(source=source, sport="soccer", home=home, away=away,
                market_type="moneyline", period="FT",
                selections={"home": h, "away": a},
                fetched_at=NOW, start_time=NOW, league=league)


@pytest.mark.parametrize("home,away,expected", [
    ("Boca Juniors W", "San Lorenzo de Almagro W", True),
    ("Palmeiras W.", "Santos W.", True),
    ("Bolton W", "Barnsley", False),          # Wanderers, not women
    ("Wycombe W", "Ipswich", False),
    ("Boca Juniors", "San Lorenzo", False),
])
def test_suffix_pair_detection(home, away, expected):
    assert has_women_suffix_pair(home, away) is expected


def test_womens_fixture_does_not_match_mens_pinnacle_event():
    """The exact live failure."""
    women = [_ml("betlive", "Boca Juniors W", "San Lorenzo de Almagro W")]
    pin_men = [_ml("pinnacle", "Boca Juniors", "San Lorenzo de Almagro",
                   league="Argentina - Primera Division")]
    assert match_events(women, pin_men) == []


def test_mens_fixture_still_matches():
    men = [_ml("liderbet", "Boca Juniors", "San Lorenzo de Almagro")]
    pin_men = [_ml("pinnacle", "Boca Juniors", "San Lorenzo de Almagro")]
    assert len(match_events(men, pin_men)) == 1


def test_womens_matches_womens():
    women = [_ml("betlive", "Boca Juniors W", "San Lorenzo de Almagro W")]
    pin_women = [_ml("pinnacle", "Boca Juniors", "San Lorenzo de Almagro",
                     league="Argentina - Primera Division Women")]
    assert len(match_events(women, pin_women)) == 1


def test_wanderers_abbreviation_still_matches_men():
    """The false positive we deliberately avoid: one-sided 'W' is a club name."""
    cb = [_ml("crystalbet", "Bolton W", "Barnsley")]
    pin = [_ml("pinnacle", "Bolton Wanderers", "Barnsley")]
    assert len(match_events(cb, pin)) == 1
