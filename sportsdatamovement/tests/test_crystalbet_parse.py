"""CrystalBet's detail markup, and the four things about it that are not
obvious. Every fixture below reproduces markup captured live on 2026-08-26."""
from __future__ import annotations

import pytest

from sportsdatamovement.crystalbet import (
    parse_detail, parse_list, slice_detail,
)


def _cell(sel_id: str, label: str, odds: str, locked: bool = False) -> str:
    css = ("sport_more_bt EmptyDetailSnatch Snatch_Locked" if locked
           else "sport_more_bt DetailSnatch ")
    return (f"<div id='S{sel_id}' class='{css}' onclick='AS({sel_id})'>\n"
            f"    <div class='sport_more_bt1'>{label}</div>\n"
            f"    <div class='sport_more_bt2'>{odds}</div>\n"
            f"</div>\n")


def _row(row_id: str, title: str, cells: str, col: int = 1) -> str:
    """CrystalBet does NOT close its <tr> tags — that is the whole point of the
    fixture, and the reason the parser splits on `<tr id=` instead of walking a
    DOM."""
    a, b = (1, 2) if col == 1 else (3, 4)
    return (f"<tr id={row_id}>\n"
            f"    <td class='sport_more_td{a}'>{title}   "
            f"<span class='detail-favorite ' onclick='SelectFavDetail(\"1\",\"{row_id}\")'>"
            f"</span></td>\n"
            f"    <td class='sport_more_td{b}'>\n"
            f"        <div class='sport_more_td_div SnatchCount2'>\n{cells}"
            f"<div class='clear' ></div>\n")


DETAIL = (
    "<table class='game-details'><tbody>\n"
    + _row("3449368", "To qualify",
           _cell("101", "1", "1.75") + _cell("102", "2", "1.90"), col=1)
    + _row("3561966", "Main result",
           _cell("103", "1", "1.42") + _cell("104", "X", "3.90")
           + _cell("105", "2", "6.60"), col=3)
    + _row("3449270", "1st Half - Corner Handicap hcp=-2.5",
           _cell("106", "1", "1.55") + _cell("107", "2", "2.30"), col=1)
    + _row("3449270", "1st Half - Corner Handicap hcp=-1.5",
           _cell("108", "1", "2.10") + _cell("109", "2", "1.68"), col=3)
    + _row("3449999", "Handicap(1X2)",
           _cell("110", "1 (2:0)", "1.01", locked=True)
           + _cell("111", "2 (0:5)", "1.01", locked=True), col=1)
)


def _by_side(rows):
    return {(m[1], m[2]): m for m in rows}


def test_every_market_is_read_including_the_right_hand_column():
    """A td1/td2-then-td3/td4 walk drops half the board, because the unclosed
    <tr> between them defeats the pairing. Measured on a real game: 263 of 526
    markets found."""
    rows = list(parse_detail(DETAIL))
    titles = {r[1] for r in rows}
    assert "To qualify" in titles          # left column
    assert "Main result" in titles         # right column
    assert len(rows) == 11


def test_a_repeated_tr_id_is_not_one_market():
    """526 rows on a real game carried 304 distinct tr ids: the corner-handicap
    rungs share one. The title distinguishes them, so the title is the market."""
    rows = list(parse_detail(DETAIL))
    corner = {r[1] for r in rows if "Corner Handicap" in r[1]}
    assert corner == {"1st Half - Corner Handicap hcp=-2.5",
                      "1st Half - Corner Handicap hcp=-1.5"}


def test_the_market_key_is_the_name_not_the_per_game_row_id():
    """`tr id` is allocated per game. Keying on it makes `markets` one row per
    (game, market) — 56,460 rows and 16 % of the database after one pass — and
    makes "how does this market behave across all games" unanswerable."""
    rows = list(parse_detail(DETAIL))
    assert all(r[0] == r[1] for r in rows)
    assert {r[0] for r in rows if r[1] == "Main result"} == {"Main result"}


def test_one_title_on_two_rows_of_a_game_stays_separable():
    doc = (_row("11", "Total goals", _cell("1", "over 2.5", "1.90"), col=1)
           + _row("22", "Total goals", _cell("2", "over 2.5", "2.40"), col=3))
    keys = [r[0] for r in parse_detail(doc)]
    assert keys == ["Total goals", "Total goals (2)"]


def test_a_locked_cell_is_unpriced_not_101():
    rows = _by_side(parse_detail(DETAIL))
    assert rows[("Handicap(1X2)", "1 (2:0)")][4] is None
    assert rows[("Main result", "1")][4] == 1.42


def test_the_selection_id_is_kept_as_a_breadcrumb():
    rows = _by_side(parse_detail(DETAIL))
    assert rows[("Main result", "X")][3] == "104"


def test_the_favourite_star_is_not_part_of_the_market_name():
    assert {r[1] for r in parse_detail(DETAIL)} >= {"To qualify", "Main result"}


# ── cell groups ───────────────────────────────────────────────────────────────

GROUPED = _row(
    "3485587", "Exact number of goals",
    _cell("201", "0", "9.90") + _cell("202", "1", "4.20")
    + "<div class='clear' ></div>\n"
    + _cell("203", "0", "12.0") + _cell("204", "1", "5.50")
    + "<div class='clear' ></div>\n"
    + _cell("205", "0", "14.0") + _cell("206", "1", "6.00"))


def test_repeated_labels_in_later_groups_are_separate_markets():
    """CrystalBet lays a market's cells out in visual groups separated by
    `<div class='clear'>`, and "Exact number of goals" is three of them —
    the match's, then each team's — with identical labels."""
    rows = list(parse_detail(GROUPED))
    assert len(rows) == 6
    assert len({(r[0], r[2]) for r in rows}) == 6


def test_the_group_shows_up_in_the_name_so_it_can_be_found_by_hand():
    names = [r[1] for r in parse_detail(GROUPED)]
    assert names[0] == "Exact number of goals"
    assert names[2] == "Exact number of goals #2"
    assert names[4] == "Exact number of goals #3"


def test_prices_do_not_leak_across_groups():
    by_key = {(r[0], r[2]): r[4] for r in parse_detail(GROUPED)}
    assert by_key[("Exact number of goals", "0")] == 9.90
    assert by_key[("Exact number of goals #2", "0")] == 12.0
    assert by_key[("Exact number of goals #3", "0")] == 14.0


# ── slicing ───────────────────────────────────────────────────────────────────

PANEL = (
    "<div class='GContainerList G1' data-id='111'>\n"
    + DETAIL
    + "</div><div class='GContainerList G2' data-id='222'>\n"
    + DETAIL.replace("Main result", "OTHER GAME MARKET")
    + "</div>"
)


def test_slice_takes_only_the_requested_games_markets():
    frag = slice_detail(PANEL, "111")
    assert "Main result" in frag
    assert "OTHER GAME MARKET" not in frag


def test_a_game_that_did_not_expand_yields_nothing_not_its_neighbour():
    """An expand re-renders the whole panel. Unbounded, a failed expand would
    harvest the NEXT game's markets and file them under this one."""
    panel = ("<div class='GContainerList G1' data-id='111'>\n"
             "  <div class='teams_name'>A - B</div>\n"
             "</div>"
             "<div class='GContainerList G2' data-id='222'>\n" + DETAIL + "</div>")
    assert slice_detail(panel, "111") == ""
    assert "Main result" in slice_detail(panel, "222")


def test_slice_of_an_absent_game_is_empty():
    assert slice_detail(PANEL, "999") == ""


# ── list view ─────────────────────────────────────────────────────────────────

LIST = (
    "<div class='x_loop_title_block '><div class='x_loop_date'>"
    "<span class='date'>Wednesday</span><span class='teams'> - 26/08/2026</span>"
    "</div></div>\n"
    "<div class='GContainerList GContainerG3171919770 ' data-id='3171919770'>\n"
    "  <div class='game-row' id='G3171919770'>\n"
    "    <div class='game-date'><span><font></font><span class='time'>23:00</span></span></div>\n"
    "    <div class='teams_name'> AEK Athens - Levski   </div>\n"
    "    <div class='game_hint'><label>UEFA, Champions League. Qualification </label></div>\n"
    "  </div></div>\n"
    "<div class='GContainerList GContainerG3171919732 ' data-id='3171919732'>\n"
    "    <div class='game-date'><span class='time'>19:30</span></div>\n"
    "    <div class='teams_name'>Basel - Copenhagen</div>\n"
    "    <div class='game_hint'><label>UEFA, Champions League</label></div>\n"
    "</div>\n"
)


def test_list_reads_both_games_with_their_metadata():
    games = parse_list(LIST)
    assert [g["event_key"] for g in games] == ["3171919770", "3171919732"]
    assert games[0]["home"] == "AEK Athens" and games[0]["away"] == "Levski"
    assert games[0]["league"] == "UEFA, Champions League. Qualification"


def test_the_date_header_carries_forward_to_later_games():
    """CrystalBet groups games under one date header; a per-game date lookup
    would leave every game but the first without a kickoff."""
    games = parse_list(LIST)
    assert games[0]["start_time"].isoformat() == "2026-08-26T19:00:00+00:00"
    assert games[1]["start_time"].isoformat() == "2026-08-26T15:30:00+00:00"


def test_a_game_without_a_date_header_still_lists():
    games = parse_list(LIST.split("</div>\n", 1)[1])
    assert games and games[0]["start_time"] is None


# ── the real capture, when it is around ───────────────────────────────────────

@pytest.mark.parametrize("path", ["cb_expand_3171919770.html"])
def test_against_a_live_capture_if_present(path):
    """Skipped in CI; run locally after dropping a capture next to the tests."""
    import pathlib
    f = pathlib.Path(__file__).parent / "fixtures" / path
    if not f.exists():
        pytest.skip(f"no capture at {f}")
    html = f.read_text(errors="replace")
    rows = list(parse_detail(slice_detail(html, "3171919770")))
    assert len(rows) > 4000
    assert any(r[1] == "Main result" for r in rows)
    assert any(r[4] is None for r in rows)          # locked cells present
