"""Fixes from the 2026-07-28 unmatched_log.csv audit.

The log had grown to 13 803 338 rows / 2.1 GB describing only 49 520 distinct
fixtures (279x duplication). Reading it surfaced four defects, each covered
here:

  1. CrystalBet was the only book not filtering SIMULATED (SRL) leagues —
     427 839 rows, and SRL reuses real team names so it is phantom-match bait.
  2. CB league names carried stray tabs ('Finland\\t, Nelonen') — 1 378 462 rows.
  3. 8 026 fixtures matched on NAME at the strong tier but were dropped by the
     +/-1 h kickoff gate; 91 % were tennis, which is scheduled "not before".
  4. Initialisms ('Seinajoen JK' vs 'SJK' = 26.7) could never match at all.

Plus the log's own hygiene: dedupe, 10-day retention, and the two columns that
would have made the audit a grep instead of an experiment.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from src import matcher
from src.matcher import (
    SCORE_LOOSE,
    SCORE_TIGHT,
    TIME_LOOSE_SECONDS,
    UnmatchedEvent,
    _accept,
    _initialism_bonus,
    _time_compatible,
    _time_loose_for,
    log_unmatched,
    match_with_diagnostics,
    prune_unmatched_log,
)
from src.models import Odds
from src.normalize import normalize_team
from src.scrapers.crystalbet import _clean_league, _skip_league

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _ml(source, home, away, start, sport="soccer"):
    return Odds(source=source, sport=sport, home=home, away=away,
                market_type="moneyline", period="FT",
                selections={"home": 1.9, "away": 1.95},
                fetched_at=NOW, start_time=start)


# ── 1 + 2. CB league hygiene ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "TOP-SRL, SRL Club Friendlies",
    "TOP-SRL, World Cup SRL",
    "SRL Spring Invitational Wagga Wagga, AUS",
    "TOP-SRL, World Cup",
])
def test_cb_skips_simulated_leagues(raw):
    """CB was the last book still letting SRL through into matching."""
    assert _skip_league(_clean_league(raw))


@pytest.mark.parametrize("raw", [
    "England, Premier League", "Germany, DFB Cup", "Finland\t, Nelonen",
])
def test_cb_keeps_real_leagues(raw):
    assert not _skip_league(_clean_league(raw))


def test_cb_still_skips_outrights():
    assert _skip_league(_clean_league("Outrights"))


@pytest.mark.parametrize("raw,want", [
    ("Finland\t, Nelonen", "Finland, Nelonen"),
    ("Italy\t, Serie A", "Italy, Serie A"),
    ("CONMEBOL Libertadores\t, Clubs", "CONMEBOL Libertadores, Clubs"),
    ("England, Premier League", "England, Premier League"),   # already clean
    ("  spaced   out  ", "spaced out"),
    ("", ""),
])
def test_cb_league_whitespace_normalized(raw, want):
    assert _clean_league(raw) == want


# ── 3. Tennis kickoff tolerance ──────────────────────────────────────────────

def test_tennis_gets_wider_loose_window_others_do_not():
    assert _time_loose_for("tennis") == 6 * 3600
    assert _time_loose_for("soccer") == TIME_LOOSE_SECONDS
    assert _time_loose_for("basketball") == TIME_LOOSE_SECONDS
    assert _time_loose_for(None) == TIME_LOOSE_SECONDS


def test_strong_tennis_pair_accepted_hours_apart():
    """'landaluce m' vs 'martin landaluce' scored 90 and was dropped at +3 h."""
    assert _accept(90.0, NOW, NOW + timedelta(hours=3), "tennis")
    assert not _accept(90.0, NOW, NOW + timedelta(hours=3), "soccer")


def test_tennis_widening_stops_at_six_hours():
    assert _accept(90.0, NOW, NOW + timedelta(hours=6), "tennis")
    assert not _accept(90.0, NOW, NOW + timedelta(hours=6, seconds=1), "tennis")


def test_medium_tier_tennis_keeps_the_tight_clock():
    """Surname collisions live here — a 65-79 score must NOT get +/-6 h."""
    assert _accept(70.0, NOW, NOW + timedelta(minutes=30), "tennis")
    assert not _accept(70.0, NOW, NOW + timedelta(hours=3), "tennis")


def test_below_tight_never_accepted_however_close():
    assert not _accept(SCORE_TIGHT - 0.1, NOW, NOW, "tennis")


def test_prefilter_never_prunes_what_accept_would_take():
    """_time_compatible gates scoring; if it were tighter than _accept for a
    sport, real matches would vanish before ever being scored."""
    for dt in (timedelta(0), timedelta(hours=3), timedelta(hours=6)):
        assert _time_compatible(NOW, NOW + dt, "tennis")
    assert not _time_compatible(NOW, NOW + timedelta(hours=6, seconds=1), "tennis")
    assert not _time_compatible(NOW, NOW + timedelta(hours=3), "soccer")


def test_tennis_widening_matches_end_to_end():
    cb = [_ml("crystalbet", "Landaluce M.", "Fritz T.", NOW, sport="tennis")]
    pin = [_ml("pinnacle", "Martin Landaluce", "Taylor Fritz",
               NOW + timedelta(hours=3), sport="tennis")]
    assert len(match_with_diagnostics(cb, pin).matched) == 1


def test_soccer_three_hours_apart_still_unmatched():
    """The widening must not leak into other sports."""
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", NOW + timedelta(hours=3))]
    r = match_with_diagnostics(cb, pin)
    assert r.matched == []
    assert r.unmatched[0].reject_reason == "time"


# ── 4. Initialism bridge ─────────────────────────────────────────────────────

def test_initialism_bridges_real_abbreviation():
    assert _initialism_bonus(normalize_team("Seinajoen JK"),
                             normalize_team("SJK")) == SCORE_TIGHT


@pytest.mark.parametrize("a,b", [
    ("Manchester United", "MCI"),      # different club entirely
    ("Sporting Lisbon", "SLB"),        # cross-town rival
    ("Boca Juniors", "RIV"),           # cross-town rival
    ("Gold Coast United", "GCK"),      # the same-city trap already guarded
    ("Barcelona", "B"),                # too short to be an initialism
    ("Real Madrid", "RMA2"),           # not alphabetic
    ("Lahti", "FC Lahti"),             # both multi/single word, no acronym
])
def test_initialism_refuses_false_expansions(a, b):
    assert _initialism_bonus(normalize_team(a), normalize_team(b)) == 0.0


def test_initialism_only_lifts_to_the_medium_tier():
    """It must never grant the strong tier — the pair still owes a tight clock."""
    assert _initialism_bonus(normalize_team("Seinajoen JK"),
                             normalize_team("SJK")) < SCORE_LOOSE


def test_initialism_needs_a_strong_partner_side():
    """Anchored: a bridge on one side cannot carry a fixture whose other side
    is also weak, or a bad expansion would invent a match outright."""
    cb = [_ml("crystalbet", "Someone Else", "Seinajoen JK", NOW)]
    pin = [_ml("pinnacle", "Unrelated Town", "SJK", NOW)]
    assert match_with_diagnostics(cb, pin).matched == []


def test_initialism_matches_when_partner_is_strong():
    cb = [_ml("crystalbet", "Lahti", "Seinajoen JK", NOW)]
    pin = [_ml("pinnacle", "FC Lahti", "SJK", NOW)]
    assert len(match_with_diagnostics(cb, pin).matched) == 1


def test_crb_alias_resolves():
    """Handled by team_aliases.yaml, not the bridge — dropping the 'AL' state
    suffix generically is the loosening that invents fixtures."""
    assert normalize_team("CR Brasil AL") == normalize_team("CRB")


# ── 5. Unmatched log: diagnostics, dedupe, retention ─────────────────────────

@pytest.fixture(autouse=True)
def _clear_dedupe_state():
    matcher._unmatched_seen.clear()
    matcher._last_prune = 9e18        # never prune during these tests
    yield
    matcher._unmatched_seen.clear()
    matcher._last_prune = 0.0


def _rows(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _unmatched(**kw):
    base = dict(cb_home="A", cb_away="B", cb_league="L", cb_start_time=NOW,
                best_pin_home="A2", best_pin_away="B2", best_pin_league="L2",
                best_score=70.0, best_pin_start_time=NOW, reject_reason="time")
    base.update(kw)
    return matcher.MatchResults(matched=[], unmatched=[UnmatchedEvent(**base)])


def test_log_writes_the_new_diagnostic_columns(tmp_path):
    p = tmp_path / "u.csv"
    log_unmatched(_unmatched(), path=p)
    row = _rows(p)[0]
    assert row["reject_reason"] == "time"
    assert row["best_pin_start_time"] == NOW.isoformat()


def test_log_does_not_repeat_an_unchanged_fixture(tmp_path):
    """279x duplication came from re-logging every fixture every poll."""
    p = tmp_path / "u.csv"
    for _ in range(50):
        log_unmatched(_unmatched(), path=p)
    assert len(_rows(p)) == 1


def test_log_records_a_fixture_again_when_its_state_changes(tmp_path):
    p = tmp_path / "u.csv"
    log_unmatched(_unmatched(best_score=70.0), path=p)
    log_unmatched(_unmatched(best_score=88.0), path=p)          # score moved
    log_unmatched(_unmatched(best_score=88.0, reject_reason="taken"), path=p)
    assert len(_rows(p)) == 3


def test_log_treats_different_fixtures_separately(tmp_path):
    p = tmp_path / "u.csv"
    log_unmatched(_unmatched(cb_home="A"), path=p)
    log_unmatched(_unmatched(cb_home="C"), path=p)
    assert len(_rows(p)) == 2


def test_prune_drops_only_rows_older_than_the_window(tmp_path):
    p = tmp_path / "u.csv"
    old = (NOW - timedelta(days=11)).isoformat()
    new = (NOW - timedelta(days=1)).isoformat()
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(matcher.UNMATCHED_HEADER)
        for ts in (old, old, new):
            w.writerow([ts] + [""] * (len(matcher.UNMATCHED_HEADER) - 1))
    assert prune_unmatched_log(p, days=10) == 2
    rows = _rows(p)
    assert len(rows) == 1 and rows[0]["ts"] == new


def test_prune_keeps_rows_with_an_unparseable_timestamp(tmp_path):
    """Housekeeping must never be the thing that eats data."""
    p = tmp_path / "u.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(matcher.UNMATCHED_HEADER)
        w.writerow(["not-a-date"] + [""] * (len(matcher.UNMATCHED_HEADER) - 1))
    assert prune_unmatched_log(p, days=10) == 0
    assert len(_rows(p)) == 1


def test_prune_on_missing_or_empty_file_is_a_noop(tmp_path):
    assert prune_unmatched_log(tmp_path / "nope.csv", days=10) == 0
    empty = tmp_path / "empty.csv"
    empty.touch()
    assert prune_unmatched_log(empty, days=10) == 0


def test_prune_leaves_no_temp_file_behind(tmp_path):
    p = tmp_path / "u.csv"
    log_unmatched(_unmatched(), path=p)
    prune_unmatched_log(p, days=10)
    assert list(tmp_path.iterdir()) == [p]


# ── reject_reason is honest about why ────────────────────────────────────────

def test_reject_reason_no_candidate_when_pin_board_is_empty():
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    r = match_with_diagnostics(cb, [])
    assert r.unmatched[0].reject_reason == "no_candidate"


def test_reject_reason_name_for_unrelated_fixtures():
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    pin = [_ml("pinnacle", "Zeta Rovers", "Omega Town", NOW)]
    assert match_with_diagnostics(cb, pin).unmatched[0].reject_reason == "name"


def test_reject_reason_records_the_pin_kickoff():
    """The column whose absence made the audit an experiment."""
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    pin_t = NOW + timedelta(hours=5)
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", pin_t)]
    u = match_with_diagnostics(cb, pin).unmatched[0]
    assert u.reject_reason == "time"
    assert u.best_pin_start_time == pin_t


def test_prune_migrates_the_legacy_nine_column_layout(tmp_path):
    """An existing log predates best_pin_start_time / reject_reason. Pruning
    must rewrite it to the current header rather than leave the writer
    appending 11-wide rows under a 9-wide header."""
    p = tmp_path / "u.csv"
    legacy = ["ts", "cb_home", "cb_away", "cb_league", "cb_start_time",
              "best_pin_home", "best_pin_away", "best_pin_league", "best_score"]
    recent = (NOW - timedelta(days=1)).isoformat()
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(legacy)
        w.writerow([recent, "A", "B", "L", "", "A2", "B2", "L2", "70.0"])
    prune_unmatched_log(p, days=10)
    with p.open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == matcher.UNMATCHED_HEADER
    row = _rows(p)[0]
    assert row["cb_home"] == "A" and row["best_score"] == "70.0"
    assert row["best_pin_start_time"] == "" and row["reject_reason"] == ""


def test_dedupe_map_evicts_stale_fixtures(tmp_path, monkeypatch):
    """The dedupe map must not grow for the life of the process — fixtures
    churn as events kick off and nothing else removes them."""
    p = tmp_path / "u.csv"
    monkeypatch.setattr(matcher, "_SEEN_MAX", 2)
    stale_ts = matcher.time.time() - 3 * matcher.UNMATCHED_REFRESH_SEC
    for i in range(5):                       # 5 old fixtures, last seen long ago
        matcher._unmatched_seen[(f"old{i}", "B", "")] = (stale_ts, ())
    log_unmatched(_unmatched(cb_home="New"), path=p)
    assert not [k for k in matcher._unmatched_seen if k[0].startswith("old")]
    assert ("New", "B", NOW.isoformat()) in matcher._unmatched_seen


def test_dedupe_map_keeps_recent_fixtures_when_evicting(tmp_path, monkeypatch):
    p = tmp_path / "u.csv"
    monkeypatch.setattr(matcher, "_SEEN_MAX", 1)
    recent = matcher.time.time()
    matcher._unmatched_seen[("recent", "B", "")] = (recent, ())
    log_unmatched(_unmatched(cb_home="New"), path=p)
    assert ("recent", "B", "") in matcher._unmatched_seen
