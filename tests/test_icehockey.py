"""Ice hockey — the sport where regulation and full time are different bets.

Everything here pins a decision that was MEASURED against the live board
(see docs/icehockey.md), not a preference. The tests are grouped by the thing
they protect:

  1. the classifier, against the whole-board title census
  2. REG vs FT, the distinction the whole sport hangs on
  3. the period prefix, which CrystalBet spells three ways on one page
  4. the two new consistency checks — including proof they can FIRE, since
     both measured zero violations on the real board and a check that cannot
     fire is worse than no check at all
  5. the cross-book mappings, which are id tables that silently rot
"""
from datetime import datetime, timezone

import pytest

from src import consistency as C
from src.models import Odds
from src.scrapers import betlive as BL
from src.scrapers import crocobet as CR
from src.scrapers import liderbet as LB
from src.scrapers import pinnacle as PIN
from src.scrapers import setanta as ST
from src.scrapers import xbet as XB
from src.scrapers.sports import icehockey as IH

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _odds(market_type, period, selections, line=None, team_side=None,
          event_id="e1", sport="icehockey"):
    return Odds(source="crystalbet", sport=sport, home="Toronto", away="Montreal",
                market_type=market_type, period=period, selections=selections,
                fetched_at=NOW, line=line, team_side=team_side,
                raw_event_id=event_id)


# ── 1. the classifier ────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,market_type,period,n_way,team_side", [
    ("Main result",                                  "moneyline", "REG", 3, None),
    ("Winner (incl. overtime and penalties)",        "moneyline", "FT",  2, None),
    ("Total Goals*",                                 "total",     "REG", 2, None),
    ("Total Goals(incl. overtime and penalties)",    "total",     "FT",  2, None),
    ("Handicap",                                     "spread",    "REG", 2, None),
    ("Handicap (incl. overtime and penalties)*",     "spread",    "REG", 2, None),
    ("Home Team Total",                              "team_total", "REG", 2, "home"),
    ("Away Team total*",                             "team_total", "REG", 2, "away"),
    ("Home Team total (incl. overtime and penalties)", "team_total", "FT", 2, "home"),
    ("1 Period - Draw No Bet*",                      "moneyline", "P1",  2, None),
    ("2 Period - Draw No Bet*",                      "moneyline", "P2",  2, None),
    ("3 Period - Draw No Bet*",                      "moneyline", "P3",  2, None),
    ("1st Period - Handicap*",                       "spread",    "P1",  2, None),
    ("2nd Period - Total Goals*",                    "total",     "P2",  2, None),
    ("3 period - Home Team total",                   "team_total", "P3", 2, "home"),
])
def test_classifier_maps_the_board(title, market_type, period, n_way, team_side):
    c = IH.classify_market_title(title)
    assert c is not None, title
    assert (c.market_type, c.period, c.n_way, c.team_side) == (
        market_type, period, n_way, team_side)


@pytest.mark.parametrize("title", [
    "Double chance", "Correct Score", "Winning margin", "Odd/Even*",
    "Both Teams to Score*", "Will there be overtime*", "Highest Scoring Period*",
    "Matchbet and Total goals*", "Last Goal*", "1 goal*",
    "Home Team clean sheet (incl. overtime and penalties)",
    "1st Period - Both Teams To Score*", "2nd Period - Double Chance*",
    "Miss Playoffs", "Winnipeg Jets",       # NHL season outrights on the board
])
def test_unrepresentable_markets_are_skipped(title):
    """Real markets with no shape in `Odds` — skipped, never guessed at."""
    assert IH.classify_market_title(title) is None


def test_three_way_handicap_never_becomes_the_matched_spread():
    """"Handicap(1X2)*" is 3-way and Pinnacle prices no counterpart.

    The plain-handicap rule would match it on prefix, so the 1x2 guard has to
    come first. Permissively it is still a ladder worth checking internally.
    """
    assert IH.classify_market_title("Handicap(1X2)*") is None
    perm = IH.classify_market_title_permissive("Handicap(1X2)*")
    assert perm is not None and perm.n_way == 3


def test_period_1x2_is_permissive_only():
    """Pinnacle's period moneyline is 2-way; CB's "Nth Period - 1X2" is 3-way.

    Emitting the 3-way as the matched moneyline would score a three-outcome
    price against a two-outcome one. Draw No Bet is the strict 2-way instead.
    """
    assert IH.classify_market_title("1st Period - 1X2*") is None
    perm = IH.classify_market_title_permissive("1st Period - 1X2*")
    assert (perm.market_type, perm.period, perm.n_way) == ("moneyline", "P1", 3)
    strict = IH.classify_market_title("1 Period - Draw No Bet*")
    assert (strict.market_type, strict.period, strict.n_way) == ("moneyline", "P1", 2)


# ── 2. REG vs FT ─────────────────────────────────────────────────────────────

def test_regulation_and_full_time_are_different_markets():
    """The distinction the whole sport hangs on.

    CrystalBet's incl-OT goal ladder ran +0.024 devigged P(over) above the
    regulation one at the same line across 366 rungs (+0.05 mid-ladder), and
    team totals +0.017 on 868 of 888. Collapsing them into one period would
    score a 60-minute price against a full-game one on every event.
    """
    reg = IH.classify_market_title("Total Goals*")
    ot = IH.classify_market_title("Total Goals(incl. overtime and penalties)")
    assert reg.period == "REG" and ot.period == "FT"
    assert reg.period != ot.period


def test_both_puck_lines_land_on_regulation():
    """And that is arithmetic, not a shortcut.

    Overtime is sudden death and only ever reached from a tie, so the overtime
    goal always makes a ONE-goal win: winning by 2+ including overtime IS
    winning by 2+ in regulation. CrystalBet prices the two ladders identically
    on 300 of 300 rungs, at every |line| it offers (1.5 and up — it ships no
    half-goal handicap, which is the only line where they would diverge).
    """
    plain = IH.classify_market_title("Handicap")
    incl = IH.classify_market_title("Handicap (incl. overtime and penalties)*")
    assert plain.period == incl.period == "REG"


def test_pinnacle_period_map_is_sport_specific():
    """Period 6 is regulation and carries most of Pinnacle's hockey board.

    The shared map would read period 1 as a first half — a thing hockey does
    not have — and drop period 6 entirely, discarding 63 % of the board
    (129 spreads, 115 totals, 39 three-way moneylines, 26 team totals).
    """
    hockey = PIN.period_map_for("icehockey")
    assert hockey[0] == "FT" and hockey[6] == "REG"
    assert hockey[1] == "P1" and hockey[2] == "P2" and hockey[3] == "P3"
    # every other sport keeps the shared map
    assert PIN.period_map_for("soccer") is PIN.PERIOD_MAP
    assert PIN.period_map_for("basketball")[1] == "H1"


# ── 3. the period prefix ─────────────────────────────────────────────────────

def test_all_three_period_spellings_are_read():
    """CB uses all three on ONE detail page."""
    assert IH.classify_market_title("1st Period - Handicap*").period == "P1"
    assert IH.classify_market_title("1 Period - Draw No Bet*").period == "P1"
    assert IH.classify_market_title("1 period - Home Team total").period == "P1"


def test_the_books_own_typo_is_refused_not_guessed():
    """CB really ships "2rd Period - Last Team To Score*", inside its THIRD-period
    block. The digit and the ordinal disagree, so neither is trusted."""
    assert IH._split("2rd period - last team to score") == (
        None, "2rd period - last team to score")
    assert IH._split("3rd period - handicap")[0] == "P3"
    assert IH._split("2nd period - handicap")[0] == "P2"


# ── 4. the two new consistency checks, and proof they can fire ───────────────

def _hockey_event(reg_ml, ft_ml, reg_total=None, ot_total=None):
    rows = [_odds("moneyline", "REG", reg_ml), _odds("moneyline", "FT", ft_ml)]
    if reg_total:
        for line, sels in reg_total.items():
            rows.append(_odds("total", "REG", sels, line=line))
    if ot_total:
        for line, sels in ot_total.items():
            rows.append(_odds("total", "FT", sels, line=line))
    return rows


def test_icehockey_is_in_the_consistency_sport_set():
    """Without this every check below is parsed and then never run."""
    assert "icehockey" in C.CONSISTENCY_SPORTS


def test_ot_box_fires_below_the_floor():
    """Overtime cannot take away a regulation win.

    Regulation says home wins ~62 %; the incl-OT price says ~45 %. That is not
    an aggressive price, it is an impossible one.
    """
    flags = C.find_consistency_flags(_hockey_event(
        reg_ml={"home": 1.55, "draw": 4.40, "away": 4.60},
        ft_ml={"home": 2.20, "away": 1.72},
    ))
    kinds = [f for f in flags if f.kind == "ot_vs_regulation"]
    assert kinds, "the floor violation did not fire"
    assert kinds[0].periods == "REG vs FT"
    assert "below the floor" in kinds[0].detail


def test_ot_box_fires_above_the_ceiling():
    """Nor can it win more often than winning-or-drawing in regulation."""
    flags = C.find_consistency_flags(_hockey_event(
        reg_ml={"home": 3.30, "draw": 4.20, "away": 2.05},
        ft_ml={"home": 1.15, "away": 5.50},
    ))
    kinds = [f for f in flags if f.kind == "ot_vs_regulation"]
    assert kinds, "the ceiling violation did not fire"
    assert "above the ceiling" in kinds[0].detail


def test_ot_box_stays_quiet_on_a_coherent_pair():
    """The shape CrystalBet actually ships: regulation plus an even overtime.

    Measured across 170 (event, side) pairs, CB's implied P(win in OT | tied)
    is 0.500 median with p10 0.477 — so this is the normal case and it must not
    flag.
    """
    flags = C.find_consistency_flags(_hockey_event(
        reg_ml={"home": 2.30, "draw": 3.85, "away": 2.35},
        ft_ml={"home": 1.80, "away": 1.85},
    ))
    assert not [f for f in flags if f.kind in ("ot_vs_regulation", "ot_monotone")]


def test_ot_monotone_fires_when_the_ladders_cross():
    """Overtime only ADDS goals, so the incl-OT ladder can never be the shorter
    of the two at the same line. Here regulation is priced above it."""
    flags = C.find_consistency_flags(_hockey_event(
        reg_ml={"home": 2.30, "draw": 3.85, "away": 2.35},
        ft_ml={"home": 1.80, "away": 1.85},
        reg_total={5.5: {"over": 1.55, "under": 2.45}},   # P(over) ~ 61 %
        ot_total={5.5: {"over": 2.45, "under": 1.55}},    # P(over) ~ 39 %
    ))
    mono = [f for f in flags if f.kind == "ot_monotone"]
    assert mono, "the ladder inversion did not fire"
    assert "can only ADD goals" in mono[0].detail
    assert mono[0].severity > C.OT_MONOTONE_PP


def test_ot_monotone_stays_quiet_when_incl_ot_sits_above():
    """The correct ordering, which is what 924 of 924 real rungs looked like."""
    flags = C.find_consistency_flags(_hockey_event(
        reg_ml={"home": 2.30, "draw": 3.85, "away": 2.35},
        ft_ml={"home": 1.80, "away": 1.85},
        reg_total={5.5: {"over": 2.00, "under": 1.80}},
        ot_total={5.5: {"over": 1.85, "under": 1.95}},
    ))
    assert not [f for f in flags if f.kind == "ot_monotone"]


def _total_rungs(period, lines):
    """A ladder whose devigged P(over) crosses 50 % between the two rungs."""
    return [_odds("total", period, {"over": 2.10, "under": 1.75}, line=lines[0]),
            _odds("total", period, {"over": 1.30, "under": 3.40}, line=lines[1])]


def test_period_totals_add_up_to_regulation_not_to_full_time():
    """The three periods make the 60 minutes, so the parent has to be REG.

    Also pins the threshold scale. The shared TOTAL_ADD_PTS is 5.0, which is
    five POINTS on basketball's ~220 — on hockey's ~5.5-goal total that would
    need the three periods to sum to nearly double the regulation line before
    anything fired. Here the periods centre near 1.67 each (5.0 total) against
    a regulation ladder centred near 8.67: a 3.7-goal contradiction that the
    basketball constant would have swallowed whole.
    """
    rows = [_odds("moneyline", "REG", {"home": 2.3, "draw": 3.85, "away": 2.35})]
    for per in ("P1", "P2", "P3"):
        rows += _total_rungs(per, (1.5, 2.5))
    rows += _total_rungs("REG", (8.5, 9.5))
    flags = C.find_consistency_flags(rows)
    add = [f for f in flags if f.kind == "total_additivity"]
    assert add, "P1+P2+P3 vs REG never ran"
    assert add[0].periods == "P1+P2+P3 vs REG"
    assert C.TOTAL_ADD_PTS_BY_SPORT["icehockey"] < C.TOTAL_ADD_PTS


def test_a_coherent_hockey_game_does_not_trip_additivity():
    """Three ~1.67 periods against a ~5.0 regulation total is the clean case."""
    rows = [_odds("moneyline", "REG", {"home": 2.3, "draw": 3.85, "away": 2.35})]
    for per in ("P1", "P2", "P3"):
        rows += _total_rungs(per, (1.5, 2.5))
    rows += _total_rungs("REG", (4.5, 5.5))
    flags = C.find_consistency_flags(rows)
    assert not [f for f in flags if f.kind == "total_additivity"]


# ── 5. the cross-book mappings ───────────────────────────────────────────────

def test_every_book_knows_the_sport():
    """An id table that quietly loses an entry costs a whole book, silently."""
    assert PIN.SPORT_ID_ICEHOCKEY == 19
    assert LB.SECTION["icehockey"] == "s:3"
    assert BL.SPORT_ID["icehockey"] == 2
    assert CR.SPORT_ID["icehockey"] == 4
    assert XB.SPORT_ID["icehockey"] == 2
    assert ST.SPORT_CODE["icehockey"] == "H"


def test_books_file_their_full_game_markets_as_regulation():
    """Every soft book's hockey main board is the 60 minutes, not the full game.

    Verified by price against Pinnacle's period 6 rather than by label: median
    devigged gap 0.27-1.79pp across all seven books (docs/icehockey.md §4).
    Filing them at FT would pair them against Pinnacle's incl-OT moneyline.
    """
    assert BL._FULL_GAME_PERIOD["icehockey"] == "REG"
    assert CR._GAMETYPE["icehockey"][1] == ("moneyline", "REG", 3, None)
    assert CR._GAMETYPE["icehockey"][8] == ("total", "REG", 2, None)
    assert LB._DETAIL_TYPES["mt:3:502"] == ("total", "REG", 2, None)
    assert ST._PERIOD["H"][0] == "REG"
    assert ST._PERIOD["H"][1] == "P1"


def test_setanta_keeps_its_two_moneylines_apart():
    """Setanta files both under period 0 and only the market type separates them.

    mt 2 is the 3-way regulation result, mt 1 the 2-way incl-OT winner —
    confirmed by the overtime box over 86 (event, side) pairs, median implied
    P(win in OT | tied) 0.500.
    """
    assert ST._MARKET["H"][2] == ("moneyline", 3)
    assert ST._MARKET["H"][1] == ("moneyline", 2)
    assert ST._FULL_GAME_PERIOD["H"] == {1: "FT"}


def test_xbet_hockey_uses_three_periods_not_halves():
    assert XB.SUBGAME_PERIODS["icehockey"] == {
        "1st period": "P1", "2nd period": "P2", "3rd period": "P3"}


def test_crocobet_outright_and_three_way_period_are_left_out():
    """gameType 190 is the tournament winner (its outcomes are team names) and
    -273 is a 3-WAY period result with no 2-way Pinnacle counterpart."""
    table = CR._GAMETYPE["icehockey"]
    assert 190 not in table
    assert -273 not in table
