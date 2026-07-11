"""Wrong-fixture guards — each test reproduces a REAL false positive found on
the 1xbet Arbs feed 2026-07-11 (rows at +37..+108% "edge", all wrong matches).
"""
from datetime import datetime, timezone

from src.matcher import _flip_odds, match_events
from src.models import Odds

T0 = datetime(2026, 7, 12, 4, 30, tzinfo=timezone.utc)


def _ml(source, home, away, h=1.9, a=1.9, sport="soccer", start=T0, draw=None):
    sel = {"home": h, "away": a}
    if draw:
        sel["draw"] = draw
    return Odds(source=source, sport=sport, home=home, away=away,
                market_type="moneyline", period="FT", selections=sel,
                fetched_at=T0, start_time=start, raw_event_id=f"{source}:{home}")


def test_same_city_different_clubs_rejected():
    """croco 'Gold Coast Knights FC v Brisbane City' scored mean 78 / min 76 vs
    1xbet 'Gold Coast United v Brisbane Olympic' — different clubs sharing
    city tokens. The distinctive tokens contradict → reject."""
    soft = _ml("crocobet", "Gold Coast Knights FC", "Brisbane City")
    ref = _ml("xbet", "Gold Coast United", "Brisbane Olympic")
    assert match_events([soft], [ref]) == []


def test_one_sided_similarity_rejected():
    """croco 'tps v oulu' vs 1xbet 'VJS Vantaa v OLS Oulu': away scored 100,
    home 31 — the mean passed the medium tier. Min-side floor rejects."""
    soft = _ml("crocobet", "tps", "oulu")
    ref = _ml("xbet", "VJS Vantaa", "OLS Oulu")
    assert match_events([soft], [ref]) == []


def test_reversed_fixture_matches_flipped():
    """Betlive 'FCI Levadia Tallinn U19 v Tallinna Kalev U21' vs 1xbet
    'Tallinna Kalev U21 v Levadia U19' — same fixture, reversed. Must match
    WITH the reference odds flipped (was cross-priced → phantom 88% edge)."""
    soft = _ml("betlive", "FCI Levadia Tallinn U19", "Tallinna Kalev U21",
               h=3.35, a=1.75, draw=4.2)
    ref = _ml("xbet", "Tallinna Kalev U21", "Levadia U19",
              h=3.62, a=1.68, draw=4.6)
    matched = match_events([soft], [ref])
    assert len(matched) == 1
    flipped_ref = matched[0].pin[0]
    # reference now oriented like the soft book: home=Levadia
    assert flipped_ref.home == "Levadia U19"
    assert flipped_ref.selections["home"] == 1.68   # was the away price
    assert flipped_ref.selections["away"] == 3.62
    assert flipped_ref.selections["draw"] == 4.6    # draw untouched


def test_flip_odds_spread_and_team_total():
    o = Odds(source="xbet", sport="basketball", home="A", away="B",
             market_type="spread", period="FT",
             selections={"home": 1.9, "away": 1.9}, fetched_at=T0, line=-4.5)
    f = _flip_odds(o)
    assert f.line == 4.5 and f.home == "B" and f.away == "A"
    tt = Odds(source="xbet", sport="basketball", home="A", away="B",
              market_type="team_total", period="FT",
              selections={"over": 1.9, "under": 1.9}, fetched_at=T0,
              line=110.5, team_side="home")
    ft = _flip_odds(tt)
    assert ft.team_side == "away" and ft.line == 110.5
    assert ft.selections == {"over": 1.9, "under": 1.9}


def test_good_matches_still_pass():
    """Guards must not kill legitimate abbreviation/transliteration matches."""
    pairs = [
        (("Miami Heat", "Milwaukee Bucks"), ("Miami Heat", "Milwaukee Bucks")),
        (("Man Utd", "Fulham"), ("Manchester United", "Fulham")),
    ]
    for (sh, sa), (rh, ra) in pairs:
        soft = _ml("crocobet", sh, sa, sport="basketball")
        ref = _ml("xbet", rh, ra, sport="basketball")
        assert match_events([soft], [ref]), f"{sh}/{sa} should match {rh}/{ra}"
