"""American football — CB parsing, cross-book wiring, and the OT-vs-regulation
consistency check (added 2026-08-12, Phase 3.2).

Everything asserted here was measured against the WHOLE live board on
2026-08-12, not a sample: all 177 in-scope CB games and every one of their
detail pages (39 distinct market titles), plus a full sport-id/market-code
census on Pinnacle, 1xbet, Lider-Bet, Betlive, Crocobet and Setanta.

The two findings that would have been missed by a sample-sized survey, and
that these tests lock in:

  * CB serves AF quarters under BOTH an ordinal and a bare-digit spelling on
    the SAME detail page ("1st Quarter - Total Points" next to "2 quarter -
    total"). Basketball's rules only know the ordinal form, so 2/3/4-quarter
    ladders fell through the strict path entirely and — worse — its permissive
    period deriver sent them to FT, where they would interleave into the
    full-time ladder and manufacture monotonicity anomalies.

  * Every book's AF market codes are a NEAR copy of another sport's, with one
    substitution each. Copying wholesale silently drops a market: Crocobet's
    AF total is -30172, not basketball's -2966; Setanta's AF moneyline is mt 1
    (tennis's code), not basketball's 145 — and Setanta's mt 2 DOES exist on
    AF, as the 3-way regulation result, so mistaking it for the moneyline
    would have paired a regulation price against Pinnacle's incl-OT one.
"""
from datetime import datetime, timezone

import pytest

from src.consistency import (
    OT_BOX_PP, OT_COINFLIP_PP, CONSISTENCY_SPORTS, find_consistency_flags,
)
from src.models import Odds
from src.scrapers import betlive, crocobet, liderbet, setanta, xbet
from src.scrapers.sports import americanfootball as af

NOW = datetime.now(tz=timezone.utc)


# ── the detail-page classifier ───────────────────────────────────────────────

@pytest.mark.parametrize("title,mt,per", [
    # the three markets that carry the board (95 %, 95 %, 80 % of games)
    ("Winner (incl. overtime)", "moneyline", "FT"),
    ("Handicap (incl. overtime)*", "spread", "FT"),
    # NOTE the missing space before the paren — CB really ships it this way,
    # and a pattern written as "total points \\(" would drop 141 events.
    ("Total Points(incl. overtime)*", "total", "FT"),
    # halves
    ("1st Half - Draw No Bet", "moneyline", "H1"),
    ("1st Half - Handicap", "spread", "H1"),
    ("1st Half - Total Points*", "total", "H1"),
    ("2nd Half - Draw No Bet (OT)", "moneyline", "H2"),
    ("2nd Half - Handicap (incl. overtime)*", "spread", "H2"),
    ("2nd Half - Total (incl. overtime)*", "total", "H2"),
    # quarters, ORDINAL spelling
    ("1st Quarter - Draw No Bet", "moneyline", "Q1"),
    ("2nd Quarter - Draw No Bet", "moneyline", "Q2"),
    ("3rd Quarter - Draw No Bet", "moneyline", "Q3"),
    ("4th. Quarter - Draw No Bet", "moneyline", "Q4"),
    ("1st Quarter - Total Points", "total", "Q1"),
    ("1st. Quarter - Handicap", "spread", "Q1"),
    # quarters, BARE-DIGIT spelling — same page, different wording
    ("2 quarter - total", "total", "Q2"),
    ("3 quarter - total", "total", "Q3"),
    ("4 quarter - total", "total", "Q4"),
    ("2 quarter - handicap", "spread", "Q2"),
    ("3 quarter - handicap", "spread", "Q3"),
    ("4 quarter - handicap", "spread", "Q4"),
])
def test_strict_classifier_covers_the_live_board(title, mt, per):
    c = af.classify_market_title(title)
    assert c is not None, f"{title!r} should classify"
    assert (c.market_type, c.period) == (mt, per)


@pytest.mark.parametrize("title,side", [
    ("HomeTeam Total (incl. overtime)*", "home"),
    ("AwayTeam Total (incl. overtime)*", "away"),
])
def test_team_totals_carry_a_side(title, side):
    """Unlike basketball (where CB's team totals have no Pinnacle counterpart
    in scope), AF team totals are matchable: Pinnacle ships 136 team_total
    entries with side=home/away across the AF board."""
    c = af.classify_market_title(title)
    assert c is not None
    assert c.market_type == "team_total"
    assert c.team_side == side


@pytest.mark.parametrize("title", [
    "Odd/even",
    "Odd/Even (incl. overtime)*",
    "Will there be overtime*",
    "Home Team odd/even",
    "Away Team odd/even",
    "Winning margin",
    "Winner (including OT) & Total (including OT)",
    "Handicap (including OT) & Total (including OT) -4.5/63.5",
    "",
])
def test_unsupported_titles_are_skipped(title):
    assert af.classify_market_title(title) is None


@pytest.mark.parametrize("title", ["Main result", "1st Half Result*"])
def test_regulation_three_way_never_reaches_the_ev_path(title):
    """Pinnacle's AF moneyline is 2-way incl-OT — 217 entries on the live
    board, not one carrying a "draw" designation. CB's regulation 1X2 must
    therefore never become the matched moneyline, or a regulation price would
    be scored against an incl-OT one. It is captured on the permissive path
    only, where ot_vs_regulation consumes it."""
    assert af.classify_market_title(title) is None
    perm = af.classify_market_title_permissive(title)
    assert perm is not None and perm.market_type == "moneyline"
    assert perm.n_way == 3


def test_htft_survives_the_permissive_path():
    c = af.classify_market_title_permissive("Halftime/fulltime")
    assert c is not None and c.market_type == "htft" and c.period == "FT"


@pytest.mark.parametrize("title,period", [
    ("2 quarter - total", "Q2"),
    ("3 quarter - handicap", "Q3"),
    ("4 quarter - total", "Q4"),
    ("1st Quarter - Total Points", "Q1"),
    ("2nd half - total", "H2"),
    ("1st Half - Handicap", "H1"),
])
def test_permissive_never_dumps_a_sub_period_into_ft(title, period):
    """The bug this guards against is silent and expensive: a bare-digit
    quarter deriving period "FT" puts a ~17-point quarter total on the same
    ladder as a ~45-point full-game total, and the monotonicity detector then
    reports an anomaly on every rung."""
    c = af.classify_market_title_permissive(title)
    assert c is not None
    assert c.period == period


# ── list view: identical to basketball, delegated ────────────────────────────

def test_list_view_delegates_to_basketball_and_stamps_the_sport():
    """All 177 live containers shipped basketball's 8-entry layout. The one
    cosmetic difference — AF's ML-away entry is a bare "2" where basketball
    ships "\\t2" — is harmless because _identify_loadinfo_roles resolves
    ML-vs-AH away by POSITION relative to the handicap landmark."""
    raw = (
        '[{"name":"1","bet":"1.45","handicap":""},'
        '{"name":"2","bet":"2.40","handicap":""},'
        '{"name":"1","bet":"1.80","handicap":""},'
        '{"name":"Handicap","bet":"-4.0 +4.0","handicap":"handicap"},'
        '{"name":"2","bet":"1.75","handicap":""},'
        '{"name":"Und","bet":"1.80","handicap":""},'
        '{"name":"Tot","bet":"44.0","handicap":"total"},'
        '{"name":"Over","bet":"1.75","handicap":""}]'
    )
    rows = af.parse_loadinfo(raw, "3010979501", "Seattle Seahawks",
                             "New England Patriots", "USA, NFL", None, NOW)
    got = {(o.market_type, o.line): o.selections for o in rows}
    assert all(o.sport == "americanfootball" for o in rows)
    assert got[("moneyline", None)] == {"home": 1.45, "away": 2.40}
    assert got[("spread", -4.0)] == {"home": 1.80, "away": 1.75}
    assert got[("total", 44.0)] == {"over": 1.75, "under": 1.80}


# ── cross-book wiring ────────────────────────────────────────────────────────

def test_every_book_knows_the_sport():
    """A missing entry here means that book silently contributes nothing —
    the failure mode is zero rows, not an exception."""
    assert xbet.SPORT_ID["americanfootball"] == 13
    assert liderbet.SECTION["americanfootball"] == "s:34"
    assert betlive.SPORT_ID["americanfootball"] == 15
    assert crocobet.SPORT_ID["americanfootball"] == 16
    assert setanta.SPORT_CODE["americanfootball"] == "AF"
    from src.scrapers import pinnacle
    assert pinnacle.SPORT_ID_AMERICANFOOTBALL == 15


def test_crocobet_total_is_the_af_code_not_basketballs():
    """The single substitution that a copied table would have missed: AF's
    total is -30172 ('ქულების რაოდენობა … (OT)'), while basketball's -2966
    does not appear on the AF board at all. Copying basketball's table would
    have emitted moneyline + spread + team totals and NO totals — 628 of the
    1030 live rows."""
    table = crocobet._GAMETYPE["americanfootball"]
    assert table[-30172][0] == "total"
    assert -2966 not in table
    assert table[-2527][0] == "moneyline"
    assert table[-2950][0] == "spread"
    assert table[182] == ("team_total", "FT", 2, "home")
    assert table[183] == ("team_total", "FT", 2, "away")


def test_setanta_moneyline_is_mt1_and_mt2_is_excluded():
    """mt 2 is present on AF but is the 3-way REGULATION result on sub-periods.
    Taking it as the moneyline would pair regulation against Pinnacle's
    incl-OT price."""
    assert setanta._MARKET["AF"][1] == ("moneyline", 2)
    assert 2 not in setanta._MARKET["AF"]
    assert 145 not in setanta._MARKET["AF"]


def test_setanta_af_periods_follow_basketballs_convention():
    """The documented trap in this feed. Period 4010 is the first half and
    1..4 are quarters — confirmed by the lines themselves (period 4010 totals
    sit at 28.5, period 1 totals at 10.5). Soccer's map, where 1 means H1,
    would have priced a quarter as a half."""
    assert setanta._PERIOD["AF"][4010] == "H1"
    assert setanta._PERIOD["AF"][1] == "Q1"
    assert setanta._PERIOD["AF"][0] == "FT"


def test_xbet_gets_a_wider_horizon_than_the_default():
    """AF plays on weekly slots, so on most days the nearest fixture is beyond
    the 36 h default and 1xbet priced NOTHING: 130 events enumerated, 0 rows
    emitted. The board is small enough that a wide horizon is cheap."""
    assert xbet.HORIZON_HOURS_BY_SPORT["americanfootball"] > xbet.HORIZON_HOURS


def test_betlive_curated_moneyline_name_classifies():
    """Betlive's AF headline market is literally "Match Winner (12)" — the
    column pair is part of the name. Without it AF emitted 0 rows from 96
    events."""
    assert betlive._classify_market("Match Winner (12)", {"1", "2"}) == ("moneyline", 2)


def test_liderbet_af_market_names_need_no_new_rules():
    """Lider ships AF as 'Winner (OT)' / 'Handicap (OT)' / 'Total (OT)', all
    already in the incl-overtime aliases — so the section id was the whole
    change."""
    assert liderbet._classify_market("Winner (OT)") == ("moneyline", 2)
    assert liderbet._classify_market("Handicap (OT)") == ("spread", 2)
    assert liderbet._classify_market("Total (OT)") == ("total", 2)


# ── ot_vs_regulation ─────────────────────────────────────────────────────────

def _ml(sels, period="FT", event="AF1", section="s"):
    return Odds(source="crystalbet", sport="americanfootball",
                home="Jacksonville Jaguars", away="Cleveland Browns",
                market_type="moneyline", period=period, selections=sels,
                fetched_at=NOW, league="USA, NFL", raw_event_id=event,
                section=section)


def _ot_flags(rows):
    return [f for f in find_consistency_flags(rows) if f.kind == "ot_vs_regulation"]


def test_americanfootball_is_in_the_consistency_engine():
    assert "americanfootball" in CONSISTENCY_SPORTS


def test_above_the_ceiling_is_flagged():
    """The real board case (CB, Jacksonville-Cleveland, 2026-08-12): the
    regulation book says 68 % win + 5 % tie, so 73 % is the most the incl-OT
    winner can be — but it is priced at 78 %. No overtime record produces
    that, because winning including overtime requires winning in regulation
    or tying first."""
    rows = [_ml({"home": 1.35, "draw": 12.8, "away": 3.15}, section="Main result"),
            _ml({"home": 1.19, "away": 3.55}, section="Winner (incl. overtime)")]
    f = _ot_flags(rows)
    assert f, "incl-OT price above the win+tie ceiling must flag"
    assert f[0].periods == "FT"
    assert f[0].severity >= OT_BOX_PP


def test_below_the_floor_is_flagged():
    """Board case (Hamilton-Saskatchewan): 28 % to win in regulation but only
    23 % including overtime. Overtime cannot take a regulation win away."""
    rows = [_ml({"home": 3.10, "draw": 19.8, "away": 1.32}, section="Main result"),
            _ml({"home": 3.40, "away": 1.21}, section="Winner (incl. overtime)")]
    f = _ot_flags(rows)
    assert f
    assert "below the floor" in f[0].detail


def test_a_coherent_pair_is_silent():
    """Regulation 1.90/12.2/1.90 -> 47.5 % + 5.0 % tie, incl-OT 1.80/1.75 ->
    49.2 %. That sits inside the box and within a point of the coin-flip
    estimate — an ordinary, correctly priced game from the same board."""
    rows = [_ml({"home": 1.90, "draw": 12.2, "away": 1.90}, section="Main result"),
            _ml({"home": 1.80, "away": 1.75}, section="Winner (incl. overtime)")]
    assert not _ot_flags(rows)


def test_thresholds_sit_above_ordinary_board_noise():
    """Measured over ALL 50 (event, period) pairs on the live board that post
    both markets: the floor residual ran p90 +0.24 / max +4.26 and the ceiling
    residual p90 +0.95 / max +5.08, inside a box only ~3.6pp wide; the
    coin-flip gap ran p90 3.29 / max 7.36."""
    assert OT_BOX_PP >= 3.0
    assert OT_COINFLIP_PP > 7.36


def test_one_market_alone_is_silent():
    assert not _ot_flags([_ml({"home": 1.19, "away": 3.55})])
    assert not _ot_flags([_ml({"home": 1.35, "draw": 12.8, "away": 3.15})])


def test_other_sports_never_get_an_ot_flag():
    """Soccer posts a 3-way and (via Draw No Bet) a 2-way on the same period
    too, but a soccer draw is a RESULT, not a gateway to extra time — the
    identity does not hold there."""
    rows = [
        Odds(source="crystalbet", sport="soccer", home="A", away="B",
             market_type="moneyline", period="FT",
             selections={"home": 1.35, "draw": 4.5, "away": 9.0},
             fetched_at=NOW, raw_event_id="S1", section="1x2"),
        Odds(source="crystalbet", sport="soccer", home="A", away="B",
             market_type="moneyline", period="FT",
             selections={"home": 1.19, "away": 3.55},
             fetched_at=NOW, raw_event_id="S1", section="dnb"),
    ]
    assert not _ot_flags(rows)


# ── the dedup fix the check depends on ───────────────────────────────────────

def test_two_way_and_three_way_moneylines_both_survive_parsing():
    """cb_detail used to key variant dedup on (period, market_type, line,
    submarket, team_side), so a 2-way and a 3-way moneyline on the same period
    collided and whichever the page rendered first deleted the other. That
    starved ot_vs_regulation of one of its two inputs — and, on basketball,
    htft_combo of its regulation 1X2 legs. Selection count is now part of the
    key."""
    from src.scrapers.cb_detail import parse_detail_page

    html = """
    <table class="game-details"><tr>
      <td class="sport_more_td1">Winner (incl. overtime)</td>
      <td class="sport_more_td2"><div class="sport_more_td_div">
        <div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">1</div>
          <div class="sport_more_bt2">1.19</div></div>
        <div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">2</div>
          <div class="sport_more_bt2">3.55</div></div>
      </div></td>
      <td class="sport_more_td3">Main result</td>
      <td class="sport_more_td4"><div class="sport_more_td_div">
        <div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">1</div>
          <div class="sport_more_bt2">1.35</div></div>
        <div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">X</div>
          <div class="sport_more_bt2">12.8</div></div>
        <div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">2</div>
          <div class="sport_more_bt2">3.15</div></div>
      </div></td>
    </tr></table>
    """
    rows = parse_detail_page(
        html, event_id="AF1", home="Jacksonville Jaguars", away="Cleveland Browns",
        league="USA, NFL", start_time=None, fetched_at=NOW,
        sport_name="americanfootball",
        classify=af.classify_market_title_permissive)
    mls = [o for o in rows if o.market_type == "moneyline" and o.period == "FT"]
    shapes = sorted(len(o.selections) for o in mls)
    assert shapes == [2, 3], f"both moneyline shapes must survive, got {shapes}"
    assert _ot_flags(rows), "and together they must reproduce the board flag"
