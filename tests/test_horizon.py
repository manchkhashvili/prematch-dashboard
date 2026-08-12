"""Global data horizon — no book pulls an event starting more than N days out.

Owner call 2026-08-12: *"I don't need any data later than 7 days to be pulled,
from any book."* Default 7 days, live-adjustable from the Config tab as
`limits.max_start_days`, 0 disables.

The thing these tests actually protect is that the cap is applied in SEVEN
different scrapers with seven different shapes, so it is easy for one to drift
back to unfiltered without anything failing loudly — the symptom of a missing
filter is extra data, not an error.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import horizon, runtime_config
from src.models import Odds

NOW = datetime.now(tz=timezone.utc)


@pytest.fixture(autouse=True)
def _default_cap(monkeypatch):
    """Pin the cap at the shipped default so a saved runtime_config.json on the
    dev machine can't silently change what these tests measure."""
    monkeypatch.setattr(horizon, "max_start_days", lambda: 7.0)


def _in(days):
    return NOW + timedelta(days=days)


# ── the primitive ────────────────────────────────────────────────────────────

def test_inside_the_window_is_kept_and_beyond_it_is_not():
    assert horizon.keeps(_in(1))
    assert horizon.keeps(_in(6.9))
    assert not horizon.keeps(_in(7.1))
    assert not horizon.keeps(_in(30))


def test_unknown_start_time_is_kept():
    """A missing kickoff is not evidence the event is far away. Dropping it
    would lose real fixtures, and the matcher takes the same view — an unknown
    time falls back to name confidence rather than being rejected."""
    assert horizon.keeps(None)


def test_naive_datetimes_are_treated_as_utc():
    naive_now = NOW.replace(tzinfo=None)
    assert horizon.keeps(naive_now + timedelta(days=1))
    assert not horizon.keeps(naive_now + timedelta(days=20))


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setattr(horizon, "max_start_days", lambda: 0.0)
    assert horizon.cutoff() is None
    assert horizon.keeps(_in(3650))


def test_capped_hours_takes_the_tighter_of_the_two():
    """A book's number is an energy budget; the cap is a data policy. The
    tighter wins, and neither overwrites the other."""
    assert horizon.capped_hours(36) == 36        # book tighter than the cap
    assert horizon.capped_hours(240) == 168      # cap tighter than the book
    assert horizon.capped_hours(0) == 168        # 0 means "unlimited" to a book


def test_capped_hours_is_a_noop_when_the_cap_is_off(monkeypatch):
    monkeypatch.setattr(horizon, "max_start_days", lambda: 0.0)
    assert horizon.capped_hours(240) == 240
    assert horizon.capped_hours(0) == 0


def test_filter_items_keeps_order_and_unknowns():
    items = [("a", _in(1)), ("b", _in(99)), ("c", None), ("d", _in(3))]
    kept = horizon.filter_items(items, lambda t: t[1])
    assert [k[0] for k in kept] == ["a", "c", "d"]


def test_a_broken_extractor_never_loses_data():
    """Defensive: if a scraper's start-time accessor throws, the row is kept.
    Silently dropping fixtures because of an attribute error is far worse than
    pulling one extra event."""
    def boom(_):
        raise KeyError("startTime")
    items = [1, 2, 3]
    assert horizon.filter_items(items, boom) == [1, 2, 3]


# ── the config knob ──────────────────────────────────────────────────────────

def test_the_knob_exists_with_the_right_default_and_bounds():
    factory, lo, hi = runtime_config.LIMITS["max_start_days"]
    assert factory() == 7.0
    assert lo == 0.0            # 0 must be reachable — it disables the cap
    assert hi >= 365.0


def test_config_tab_labels_the_knob():
    """runtime_config.LIMITS keys render into the Config tab automatically, but
    an unlabelled one shows its raw key. Every limit deserves a description."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "static" / "config.html").read_text()
    assert "max_start_days:" in html


# ── applied per scraper ──────────────────────────────────────────────────────

def _bl_event(days, **kw):
    from src.scrapers.betlive import _DOTNET_EPOCH
    start = NOW + timedelta(days=days)
    ticks = int((start.replace(tzinfo=None) - _DOTNET_EPOCH).total_seconds()) * 10_000_000
    ev = {"id": 1, "homeTeamName": "A", "awayTeamName": "B", "startDate": ticks,
          "leagueName": "NFL", "eventType": 1,
          "markets": [{"outcomes": [
              {"name": "1", "odd": 1.9, "marketName": "Match Winner (12)"},
              {"name": "2", "odd": 1.9, "marketName": "Match Winner (12)"}]}]}
    ev.update(kw)
    return ev


def test_betlive_drops_far_events():
    from src.scrapers.betlive import _parse_event
    assert _parse_event(_bl_event(2), "americanfootball", NOW)
    assert not _parse_event(_bl_event(30), "americanfootball", NOW)


def _lb_match(days):
    start = (NOW + timedelta(days=days)).replace(tzinfo=None).isoformat()
    return {
        "id": "pr:m:1", "startTime": start, "homeId": "h", "awayId": "a",
        "tourId": "t",
        "markets": {"m1": {"typeId": "mt:1", "outcomes": {
            "o1": {"value": 1.9}, "o2": {"value": 1.9}}}},
    }


def test_liderbet_drops_far_matches():
    from src.scrapers.liderbet import _parse_match
    anc = {"h": {"name": "A"}, "a": {"name": "B"}, "t": {"name": "NFL"}}
    mts = {"mt:1": {"name": "Winner (OT)", "outcomeTypes": [
        {"id": "o1", "name": "1"}, {"id": "o2", "name": "2"}]}}
    assert _parse_match(_lb_match(2), anc, mts, "americanfootball", NOW)
    assert not _parse_match(_lb_match(30), anc, mts, "americanfootball", NOW)


def test_pinnacle_index_drops_far_matchups():
    from src.scrapers.pinnacle import _index_matchups

    def mu(mid, days):
        return {"id": mid, "type": "matchup", "parent": None,
                "startTime": (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
                "participants": [{"alignment": "home", "name": "A"},
                                 {"alignment": "away", "name": "B"}]}

    idx = _index_matchups([mu(1, 2), mu(2, 30)], NOW)
    assert 1 in idx and 2 not in idx


def test_pinnacle_dropping_a_matchup_drops_its_whole_market_block():
    """The reason the filter lives in _index_matchups: the market loop skips any
    row whose matchupId it cannot resolve, so one drop removes the alt-line
    ladder too — not just the event header."""
    from src.scrapers.pinnacle import _build_odds_for_league
    markets = [{"matchupId": 999, "period": 0, "type": "total",
                "prices": [{"designation": "over", "points": 44.5, "price": -110},
                           {"designation": "under", "points": 44.5, "price": -110}]}]
    assert _build_odds_for_league(markets, {}, "NFL",
                                  sport_name="americanfootball") == []


def test_xbet_horizon_is_capped_by_the_global_one():
    """1xbet's AF override is 240 h (10 days) on purpose — it exists so a weekly
    sport gets priced at all. With a 7-day cap in force the effective window is
    168 h, and the override still does its job (the nearest AF fixture was
    ~48 h out when it was added)."""
    from src.scrapers import xbet
    assert xbet.HORIZON_HOURS_BY_SPORT["americanfootball"] == 240.0
    assert horizon.capped_hours(
        xbet.HORIZON_HOURS_BY_SPORT["americanfootball"]) == 168.0


def test_crystalbet_filters_the_game_list(monkeypatch):
    """CB's filter runs after the one list postback that has to happen anyway,
    but before anything per-game — so a far fixture costs neither an
    ExpandDetail round trip nor a list-view Odds row."""
    from src.scrapers.crystalbet import _GameOnList

    def g(eid, days):
        return _GameOnList(event_id=eid, home="A", away="B", league="NFL",
                           start_time=_in(days), loadinfo="", list_odds=[])

    games = [g("near", 2), g("far", 30), g("unknown", 0)]
    games[2] = _GameOnList(event_id="unknown", home="A", away="B", league="NFL",
                           start_time=None, loadinfo="", list_odds=[])
    kept = horizon.filter_items(games, lambda x: x.start_time, label="CB test")
    assert [x.event_id for x in kept] == ["near", "unknown"]


def test_every_scraper_module_imports_the_horizon():
    """A book that forgets the import is a book with no cap, and the symptom is
    silent: more data, no error."""
    import importlib
    for mod in ("crystalbet", "pinnacle", "xbet", "liderbet", "betlive",
                "crocobet", "setanta"):
        m = importlib.import_module(f"src.scrapers.{mod}")
        src = open(m.__file__).read()
        assert "horizon" in src, f"{mod} does not reference the global horizon"


def test_the_secondary_sweeps_are_capped_too():
    """The seven book scrapers are not the only things that open per-event
    requests. betlive_watch runs its own discover loop before a refreshOdds per
    candidate, and soft_scan enumerates CB / Betlive / Lider itself before a
    detail call per surviving event — all four are fetch paths the cap has to
    reach, or "no book pulls past 7 days" is only true of the main loops."""
    import importlib
    for mod in ("src.betlive_watch", "src.soft_scan"):
        m = importlib.import_module(mod)
        src = open(m.__file__).read()
        assert "horizon.keeps" in src or "horizon.filter_items" in src, (
            f"{mod} enumerates events but never applies the horizon")
