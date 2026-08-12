"""Opportunity re-verify loop + price-age plumbing (2026-08-12).

Why this exists, measured from the owner's own ticks.db rather than assumed:

  * a CB soccer sweep is 1103 events and takes 191 s on average, 3127 s at
    worst, while Pinnacle re-prices the same board in 25 s and 2.3x as often.
    Gap between consecutive CB soccer cycles: median 171 s, p90 724 s, max
    3300 s;
  * `src/edge.py` never referenced `fetched_at`, so a 50-minute-old CB price
    was scored against a 90-second-old Pinnacle fair and the drift between them
    was reported as edge;
  * measured over 8 460 CB soccer tick series, the share of prices that have
    moved more than 6 % is 0.1 % at a 3-minute lag and 5.5 % at 50 minutes.

The fix is not a faster full sweep (that is the same sweep) but a targeted
re-pull of the shortlist that currently shows an opportunity. An edge that
survives on a freshly-pulled price is real; one that evaporates was drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import app as A
from src.edge import compute_opportunities
from src.matcher import MatchedEvent
from src.models import Odds, Opportunity

NOW = datetime.now(tz=timezone.utc)


def _odds(source, sels, *, fetched_at=NOW, eid="E1", mt="moneyline", line=None):
    return Odds(source=source, sport="soccer", home="A", away="B",
                market_type=mt, period="FT", selections=sels,
                fetched_at=fetched_at, line=line, league="L", raw_event_id=eid)


# ── the age signal ───────────────────────────────────────────────────────────

def test_opportunity_carries_the_soft_legs_fetch_time():
    """Not the reference leg's, and not 'now' — the age that matters is how
    long ago the price you would actually bet was pulled."""
    old = NOW - timedelta(minutes=47)
    m = MatchedEvent(
        cb=[_odds("crystalbet", {"home": 3.25, "draw": 3.4, "away": 2.4},
                  fetched_at=old)],
        pin=[_odds("pinnacle", {"home": 2.99, "draw": 3.4, "away": 2.5})],
        home="A", away="B", score=100.0)
    opps = compute_opportunities([m], min_edge_pct=1.0)
    assert opps, "fixture should produce an edge"
    assert all(o.cb_fetched_at == old for o in opps)


def test_age_sec_is_rendered_from_it():
    old = NOW - timedelta(minutes=47)
    o = Opportunity(start_time=NOW, match_label="A — B", market="Moneyline FT",
                    side="home", cb_odds=3.25, pin_no_vig=2.99, edge_pct=8.6,
                    kind="+EV", kelly_stake=10.0, cb_fetched_at=old)
    d = A._opp_to_dict(o, now=NOW)
    assert d["age_sec"] == pytest.approx(47 * 60, abs=2)


def test_unknown_fetch_time_is_null_not_zero():
    """A missing timestamp must not render as 'fresh'. Zero would be the most
    reassuring possible lie."""
    o = Opportunity(start_time=NOW, match_label="A — B", market="Moneyline FT",
                    side="home", cb_odds=3.25, pin_no_vig=2.99, edge_pct=8.6,
                    kind="+EV", kelly_stake=10.0)
    assert A._opp_to_dict(o, now=NOW)["age_sec"] is None


def test_naive_timestamps_do_not_blow_up_the_row():
    o = Opportunity(start_time=NOW, match_label="A — B", market="Moneyline FT",
                    side="home", cb_odds=3.25, pin_no_vig=2.99, edge_pct=8.6,
                    kind="+EV", kelly_stake=10.0,
                    cb_fetched_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None))
    assert A._opp_to_dict(o, now=NOW)["age_sec"] == pytest.approx(300, abs=2)


def test_age_never_goes_negative_on_clock_skew():
    o = Opportunity(start_time=NOW, match_label="A — B", market="Moneyline FT",
                    side="home", cb_odds=3.25, pin_no_vig=2.99, edge_pct=8.6,
                    kind="+EV", kelly_stake=10.0,
                    cb_fetched_at=NOW + timedelta(seconds=30))
    assert A._opp_to_dict(o, now=NOW)["age_sec"] == 0


# ── the merge ────────────────────────────────────────────────────────────────

def test_reverified_events_are_replaced_wholesale():
    existing = [
        _odds("crystalbet", {"home": 3.25, "away": 2.4}, eid="E1"),
        _odds("crystalbet", {"over": 2.0, "under": 1.9}, eid="E1",
              mt="total", line=2.5),
        _odds("crystalbet", {"home": 1.8, "away": 2.1}, eid="E2"),
    ]
    fresh = [_odds("crystalbet", {"home": 3.0, "away": 2.5}, eid="E1")]
    out = A._merge_reverified(existing, fresh, {"E1"})
    e1 = [o for o in out if o.raw_event_id == "E1"]
    assert len(e1) == 1, "the stale E1 total must not survive the re-pull"
    assert e1[0].selections == {"home": 3.0, "away": 2.5}
    assert any(o.raw_event_id == "E2" for o in out), "other events untouched"


def test_a_market_that_vanished_does_not_linger():
    """The point of replacing the whole event rather than merging row by row: a
    line the book PULLED must disappear, not survive as the stalest row on the
    page — which is exactly the failure this loop exists to remove."""
    existing = [_odds("crystalbet", {"over": 2.0, "under": 1.9}, eid="E1",
                      mt="total", line=4.5)]
    fresh = [_odds("crystalbet", {"home": 3.0, "away": 2.5}, eid="E1")]
    out = A._merge_reverified(existing, fresh, {"E1"})
    assert not any(o.market_type == "total" for o in out)


def test_an_event_that_returned_nothing_keeps_its_old_rows():
    """An expansion failure is not evidence the markets are gone. Wiping on a
    failed re-pull would delete real rows every time CB flaked."""
    existing = [_odds("crystalbet", {"home": 3.25, "away": 2.4}, eid="E1"),
                _odds("crystalbet", {"home": 1.8, "away": 2.1}, eid="E2")]
    fresh = [_odds("crystalbet", {"home": 3.0, "away": 2.5}, eid="E2")]
    out = A._merge_reverified(existing, fresh, {"E1", "E2"})
    assert any(o.raw_event_id == "E1" for o in out), "E1 failed — keep it"
    e2 = [o for o in out if o.raw_event_id == "E2"]
    assert len(e2) == 1 and e2[0].selections == {"home": 3.0, "away": 2.5}


def test_merge_is_a_no_op_for_an_empty_shortlist():
    existing = [_odds("crystalbet", {"home": 3.25, "away": 2.4}, eid="E1")]
    assert A._merge_reverified(existing, [], set()) == existing


# ── the loop's configuration ────────────────────────────────────────────────

def test_the_loop_targets_crystalbet_only():
    """Measured cycle averages: lider 9.9 s, betlive 11.9 s, crocobet 16.1 s,
    xbet 20.8 s — against CB's 191 s. Re-verifying an already-fresh book would
    spend requests for nothing."""
    import inspect
    src = inspect.getsource(A._opportunity_reverify_loop)
    assert '"cb"' in src
    for book in ("liderbet", "betlive", "crocobet", "setanta"):
        assert f'"{book}"' not in src, f"loop should not re-verify {book}"


def test_the_loop_uses_the_strict_classifier():
    """The permissive classifier exists for the anomaly ladder scan. Feeding
    permissive rows into the +EV path would emit markets the Pinnacle matcher
    cannot pair, which is worse than a stale row."""
    import inspect
    assert "permissive=False" in inspect.getsource(A._opportunity_reverify_loop)


def test_the_loop_is_bounded_and_only_chases_actionable_edges():
    assert A.OPP_REVERIFY_MAX_GAMES > 0
    assert A.OPP_REVERIFY_MIN_EDGE > 0, (
        "re-pulling a game to confirm a 1% edge spends a request on something "
        "you would not bet")


def test_the_loop_is_registered_on_startup():
    import inspect
    src = inspect.getsource(A)
    assert "_opportunity_reverify_loop()" in src
    assert 'name="opportunity_reverify_loop"' in src


def test_the_targeted_rescrape_helper_is_sport_generic():
    """It was basketball-hardcoded for the anomaly watch loop; the opportunity
    loop needs it for whichever sport the edge is in."""
    from src.scrapers import crystalbet as cb
    import inspect
    sig = inspect.signature(cb.fetch_crystalbet_games)
    assert "sport_name" in sig.parameters
    assert "permissive" in sig.parameters
    # the old entry point must still work — the anomaly watch loop calls it
    assert callable(cb.fetch_crystalbet_basketball_games)


# ── the UI contract ─────────────────────────────────────────────────────────

def test_the_age_column_is_rendered_and_the_table_still_lines_up():
    import re
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "static" / "arbs.html").read_text()
    head = page[page.index("<thead>"):page.index("</thead>")]
    # (?=[\s>]) so the enclosing <thead> is not counted as a column
    n_th = len(re.findall(r"<th(?=[\s>])", head))
    i = page.index('<td class="num ${pctClass(o.edge_pct)}">')
    row = page[page.rindex("`", 0, i):page.index("`", i)]
    n_td = len(re.findall(r"<td[^>]*>", row))
    assert n_th == n_td, f"{n_th} headers vs {n_td} cells — the table is skewed"
    assert "o.age_sec" in page, "the age is never rendered"
    assert 'colspan="15"' not in page, "a colspan still spans the old width"
