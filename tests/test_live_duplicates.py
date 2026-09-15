"""
duplicate_live — the same in-play match listed twice by one book, on the
prematch Anomalies tab. Parsers are pinned against the markup the boards
actually served on 2026-09-15 (the live/docs description was stale).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live_duplicates import (  # noqa: E402
    LiveMatch, _period_label, find_live_duplicates, parse_cb_live_board,
    parse_lider_live_board,
)


# ── the CB board, as served ───────────────────────────────────────────────────
def _cb_row(gid, h, a, per, hs, as_):
    return (f"<div class='d_row ' onclick=\"doGameOpenPost({gid});\"><div class='d_row1'>"
            f"<div class='d_row1_1'><div class='team1-title'>{h}</div>"
            f"<div class='team2-title'>{a}</div></div><div class='d_row1_2'>"
            f"<span class='started-game-image'></span><span>{per} </span></div></div>"
            f"<div class='d_row2 '><div class='result-group'><div class='team1-result'>{hs}</div>"
            f" <div class='team2-result'>{as_}</div></div></div></div>")


def _cb_league(name, rows):
    return (f"<div class='league-head' onclick=DoLiveCEPostBack('StartedChampionatCollapse:1')>"
            f"<div class='league-title'>{name}</div></div>" + "".join(rows))


def _cb_board(*sports):
    return "".join(f"<div class='started-games-by-country ' sportTypeId='{sid}' >{body}</div>"
                   for sid, body in sports)


class TestCbParser:
    def test_reads_teams_score_period_league_and_sport(self):
        html = _cb_board((16, _cb_league("UEFA Youth League",
                                         [_cb_row(1, "OFK Mladost U19", "Kayrat Almaty U19",
                                                  "2nd half 58'", 0, 3)])))
        m, = parse_cb_live_board(html)
        assert (m.book, m.sport, m.event_id) == ("cb", "soccer", "1")
        assert (m.home, m.away, m.league) == ("OFK Mladost U19", "Kayrat Almaty U19", "UEFA Youth League")
        assert (m.home_score, m.away_score, m.period) == (0, 3, "2nd half")

    def test_several_leagues_inside_one_sport_container(self):
        html = _cb_board((22, _cb_league("ATP A", [_cb_row(1, "A", "B", "1st set", 3, 2)])
                              + _cb_league("ATP B", [_cb_row(2, "C", "D", "2nd set", 6, 4)])))
        ms = parse_cb_live_board(html)
        assert [(m.league, m.event_id) for m in ms] == [("ATP A", "1"), ("ATP B", "2")]

    def test_simulated_and_esports_sport_ids_are_skipped(self):
        html = _cb_board((134, _cb_league("SRL", [_cb_row(1, "X", "Y", "1st half 10'", 0, 0)])),
                         (216, _cb_league("CS2", [_cb_row(2, "X", "Y", "map 1", 0, 0)])),
                         (16, _cb_league("Real", [_cb_row(3, "X", "Y", "1st half 10'", 0, 0)])))
        assert [m.event_id for m in parse_cb_live_board(html)] == ["3"]


# ── the Lider board, as served ───────────────────────────────────────────────
def _lider(matches):
    anc = {"s1": {"name": "Football"}, "s2": {"name": "eSoccer"}, "s3": {"name": "Tennis"},
           "t1": {"name": "Premier League"}, "t2": {"name": "Esports Battle"},
           "h1": {"name": "Rostov"}, "a1": {"name": "CSKA Moscow"},
           "h2": {"name": "Alcaraz C."}, "a2": {"name": "Sinner J."}}
    return {"ancestors": anc, "matches": {m["id"]: m for m in matches}}


def _lm(mid, sport, tour, home, away, hs, as_, status):
    return {"id": mid, "sportId": sport, "tourId": tour, "homeId": home, "awayId": away,
            "props": {"scores": {"main": {"home": hs, "away": as_}},
                      "status": {"matchStatus": status, "eventStatus": "LIVE"}}}


class TestLiderParser:
    def test_reads_names_via_ancestors(self):
        m, = parse_lider_live_board(_lider([_lm("sr:match:1", "s1", "t1", "h1", "a1", 1, 0, "2nd half")]))
        assert (m.book, m.sport, m.home, m.away) == ("liderbet", "soccer", "Rostov", "CSKA Moscow")
        assert (m.home_score, m.away_score, m.period, m.league) == (1, 0, "2nd half", "Premier League")

    def test_esports_sport_is_dropped(self):
        assert parse_lider_live_board(_lider([_lm("sr:match:1", "s2", "t2", "h1", "a1", 1, 0, "2nd half")])) == []

    def test_tennis_maps(self):
        m, = parse_lider_live_board(_lider([_lm("bg:match:9", "s3", "t1", "h2", "a2", 1, 1, "2nd set")]))
        assert m.sport == "tennis" and m.event_id == "bg:match:9"


class TestPeriodLabel:
    def test_strips_only_the_trailing_clock(self):
        assert _period_label("2nd half 58'") == "2nd half"
        assert _period_label("1st set 12:34") == "1st set"
        assert _period_label("4th quarter") == "4th quarter"
        assert _period_label("Game 5") == "game 5"

    def test_ordinals_survive(self):
        """A first cut stripped every digit and collided '4th set' with '5th set'."""
        assert _period_label("4th set") != _period_label("5th set")


# ── the detector ─────────────────────────────────────────────────────────────
def _m(eid, home, away, hs, as_, per="2nd half", book="cb", sport="soccer", league="L"):
    return LiveMatch(book, sport, eid, home, away, league, hs, as_, per)


class TestDetector:
    def test_same_teams_score_period_fires_at_100(self):
        rows = find_live_duplicates([_m("1", "Rostov", "CSKA Moscow", 1, 0),
                                     _m("2", "FC Rostov", "CSKA", 1, 0, league="Other")])
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "duplicate_live" and r["severity"] == 100.0 and r["book"] == "cb"
        assert "1 (L)" in r["detail"] and "2 (Other)" in r["detail"] and "1-0" in r["detail"]

    def test_swapped_sides_with_swapped_score(self):
        rows = find_live_duplicates([_m("1", "Rostov", "CSKA Moscow", 1, 0),
                                     _m("2", "CSKA Moscow", "Rostov", 0, 1)])
        assert len(rows) == 1 and "sides flipped" in rows[0]["detail"]

    def test_different_score_is_two_games(self):
        assert find_live_duplicates([_m("1", "Rostov", "CSKA", 1, 0), _m("2", "Rostov", "CSKA", 2, 0)]) == []

    def test_different_period_is_two_games(self):
        assert find_live_duplicates([_m("1", "Rostov", "CSKA", 1, 0), _m("2", "Rostov", "CSKA", 1, 0, per="1st half")]) == []

    def test_books_never_cross(self):
        """CB and Lider listing the same live match is not a duplicate — it is
        two books. Only two listings on ONE book count."""
        assert find_live_duplicates([_m("1", "Rostov", "CSKA", 1, 0, book="cb"),
                                     _m("2", "Rostov", "CSKA", 1, 0, book="liderbet")]) == []

    def test_sports_never_cross(self):
        assert find_live_duplicates([_m("1", "A", "B", 1, 0, sport="soccer"),
                                     _m("2", "A", "B", 1, 0, sport="futsal")]) == []

    def test_no_score_no_anchor(self):
        assert find_live_duplicates([_m("1", "A", "B", None, None), _m("2", "A", "B", None, None)]) == []

    def test_one_row_per_pair_not_two(self):
        rows = find_live_duplicates([_m("1", "A", "B", 1, 0), _m("2", "A", "B", 1, 0)])
        assert len(rows) == 1 and rows[0]["cb_event_id"] == "1"
