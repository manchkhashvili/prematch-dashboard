"""Same-named fixtures at different kickoffs must not merge into one event.

Pinnacle lists reserve and U20 games under the IDENTICAL senior team names.
Observed live 2026-07-26 on El Salvador's Primera Division:

    PINNACLE  Aguila v Luis Angel Firpo        -> {21:00, 18:00}
    PINNACLE  Fuerte San Francisco v Platense  -> {21:00, 18:00}

Lider showed what the 18:00 games were — "Cd Aguila U20", "agila (rez)". Because
`_group_by_event` keyed only on (home, away, is_women), both Pinnacle events
collapsed into ONE bucket and the senior soft-book fixture was priced against the
RESERVE ladder: 14 phantom rows, including 6 of the 9 ARBs on the board.

The youth guard cannot catch this — it reads names, and Pinnacle's names carry no
marker. Kickoff is the only distinguishing signal, so it is now part of the key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.matcher import _group_by_event, _time_bucket, match_events
from src.models import Odds

SENIOR = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
RESERVE = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)   # 3 h earlier


def _total(source, home, away, start, line, over, under):
    return Odds(source=source, sport="soccer", home=home, away=away,
                market_type="total", period="FT", line=line,
                selections={"over": over, "under": under},
                fetched_at=SENIOR, start_time=start)


def test_same_names_different_kickoff_are_separate_events():
    """The core fix: two Pinnacle events, identical names, 3 h apart."""
    pin = [
        _total("pinnacle", "Aguila", "Luis Angel Firpo", SENIOR, 2.5, 1.90, 1.95),
        # the untagged RESERVE game — a low-scoring line at the same nominal 2.5
        _total("pinnacle", "Aguila", "Luis Angel Firpo", RESERVE, 2.5, 3.40, 1.33),
    ]
    groups = _group_by_event(pin)
    assert len(groups) == 2, "reserve and senior fixtures must not merge"
    # and each group holds exactly its own row
    assert all(len(v) == 1 for v in groups.values())


def test_senior_soft_book_prices_against_the_senior_ladder():
    """End to end: the 21:00 soft fixture must pair with the 21:00 Pinnacle
    event, not the 18:00 reserve one (which is what produced the phantoms)."""
    soft = [_total("liderbet", "Aguila", "CD Luis Angel Firpo", SENIOR, 2.5, 1.92, 1.93)]
    pin = [
        _total("pinnacle", "Aguila", "Luis Angel Firpo", SENIOR, 2.5, 1.90, 1.95),
        _total("pinnacle", "Aguila", "Luis Angel Firpo", RESERVE, 2.5, 3.40, 1.33),
    ]
    matched = match_events(soft, pin)
    assert len(matched) == 1
    # the paired Pinnacle leg must be the senior 21:00 one
    assert all(o.start_time == SENIOR for o in matched[0].pin)


def test_one_fixture_stays_one_event():
    """All of a fixture's rows share a kickoff, so nothing legitimate splits.
    (Measured 2026-07-27: 0 of 2 820 name-pairs across Pinnacle/Lider/Setanta/
    Crocobet had more than one kickoff.)"""
    pin = [_total("pinnacle", "Aguila", "Luis Angel Firpo", SENIOR, ln, 1.9, 1.95)
           for ln in (1.5, 2.0, 2.5, 3.0, 3.5)]
    assert len(_group_by_event(pin)) == 1


def test_small_drift_does_not_split_an_event():
    """The bucket tolerates sub-bucket jitter, so a book emitting a fixture a few
    minutes apart across rows keeps its markets together."""
    a = _total("pinnacle", "A", "B", SENIOR, 2.5, 1.9, 1.95)
    b = _total("pinnacle", "A", "B", SENIOR + timedelta(minutes=2), 3.0, 1.9, 1.95)
    assert len(_group_by_event([a, b])) == 1


def test_missing_kickoff_groups_together():
    """Rows without a kickoff must not each become their own event."""
    rows = [_total("cb", "A", "B", None, ln, 1.9, 1.95) for ln in (2.0, 2.5)]
    assert len(_group_by_event(rows)) == 1
    assert _time_bucket(None) is None


def test_bucket_separates_three_hours_but_not_two_minutes():
    assert _time_bucket(SENIOR) != _time_bucket(RESERVE)
    assert _time_bucket(SENIOR) == _time_bucket(SENIOR + timedelta(minutes=2))


# ── The fix must remove phantoms WITHOUT hiding real edges ───────────────────
# "Did splitting the group also make real opportunities invisible?" — the answer
# has to be demonstrated, not asserted. Both checks below run the full
# match -> compute_opportunities path.

def _ml(source, home, away, start, h, d, a):
    return Odds(source=source, sport="soccer", home=home, away=away,
                market_type="moneyline", period="FT",
                selections={"home": h, "draw": d, "away": a},
                fetched_at=SENIOR, start_time=start)


def test_real_senior_edge_survives_the_split():
    """A genuine soft-book edge against the SENIOR Pinnacle line must still be
    found once the reserve fixture is split off."""
    from src.edge import compute_opportunities
    from src.matcher import match_events

    # senior Pinnacle: balanced-ish. soft book offers a clearly better home price.
    pin = [
        _ml("pinnacle", "Aguila", "Luis Angel Firpo", SENIOR, 2.00, 3.40, 3.80),
        # reserve game, wildly different prices — the contamination source
        _ml("pinnacle", "Aguila", "Luis Angel Firpo", RESERVE, 1.20, 6.50, 12.0),
    ]
    soft = [_ml("liderbet", "Aguila", "CD Luis Angel Firpo", SENIOR, 2.30, 3.40, 3.80)]

    opps = compute_opportunities(match_events(soft, pin), min_edge_pct=0.5)
    home = [o for o in opps if o.side == "home"]
    assert home, "the genuine senior home edge must still surface"
    # priced against the SENIOR fair (~2.00 vig'd), not the reserve's 1.20
    assert home[0].pin_no_vig < 2.6, f"paired against the wrong ladder: {home[0].pin_no_vig}"
    assert 5 < home[0].edge_pct < 40, f"implausible edge: {home[0].edge_pct}"


def test_reserve_contamination_no_longer_produces_a_phantom():
    """The mirror case: a soft SENIOR fixture must not be priced against the
    reserve ladder, which is what generated 68%+ phantoms."""
    from src.edge import compute_opportunities
    from src.matcher import match_events

    pin = [
        _ml("pinnacle", "Aguila", "Luis Angel Firpo", SENIOR, 2.00, 3.40, 3.80),
        _ml("pinnacle", "Aguila", "Luis Angel Firpo", RESERVE, 12.0, 6.50, 1.20),
    ]
    # soft price that is fair vs SENIOR but looks enormous vs the reserve away leg
    soft = [_ml("liderbet", "Aguila", "CD Luis Angel Firpo", SENIOR, 2.00, 3.40, 3.85)]

    opps = compute_opportunities(match_events(soft, pin), min_edge_pct=0.5)
    assert all(o.edge_pct < 20 for o in opps), \
        f"phantom survived: {[(o.side, round(o.edge_pct, 1)) for o in opps]}"
