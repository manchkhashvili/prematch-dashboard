"""The 0.0 handicap rung against the markets that settle identically to it.

Owner found this on the live board (Resovia Rzeszow v KKP Bydgoszcz W,
2026-08-29) after three rounds of checks that produced nothing bettable.
CrystalBet posted, on one page:

    Main result         1 @ 1.85   X @ 3.95   2 @ 3.10
    Draw no bet         1 @ 1.55              2 @ 2.10
    Asian Handicap 0.0  1 @ 1.20              2 @ 3.50

Two provable things there, neither needing a model:

  * The Draw No Bet and the 0.0 rung are THE SAME BET — both void on the draw.
    Best of each side covers the game for 1/1.55 + 1/3.50 = 0.9309, a +7.4 %
    lock with no losing branch at all.
  * The 0.0 rung voids the draw where the 1X2 loses to it, so on the same side
    it must be the SHORTER price. Away @3.50 against the 1X2's @3.10 is a ratio
    of 1.129 — the better bet at the longer price, which cannot happen.

Neither was detected, for two separate reasons that are both fixed here and
both tested below: Draw No Bet was hard-skipped for soccer, and CrystalBet
titles this ladder "Asian Handicap" on 196 events where the classifier only
knew "Handicap".
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import consistency as C
from src.models import Odds
from src.scrapers.sports import soccer as SOC

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)

# The screenshot, exactly.
BOARD = {
    "1X2": {"home": 1.85, "draw": 3.95, "away": 3.10},
    "DNB": {"home": 1.55, "away": 2.10},
    "AH0": {"home": 1.20, "away": 3.50},
}


def _o(mt, per, sels, line=None, section=None, at=NOW):
    return Odds(source="crystalbet", sport="soccer", home="Resovia Rzeszow",
                away="KKP Bydgoszcz W", market_type=mt, period=per,
                selections=dict(sels), fetched_at=at, line=line,
                league="Poland, Ekstraliga W", raw_event_id="e1", section=section)


def _rows(**over):
    b = {**BOARD, **over}
    return [
        _o("moneyline", "FT", b["1X2"], section="Main result"),
        _o("moneyline", "FT", b["DNB"], section="Draw no bet"),
        _o("spread", "FT", b["AH0"], line=0.0, section="Asian Handicap"),
    ]


def _kinds(rows):
    return {f.kind: f for f in C.find_consistency_flags(rows)}


# ── the classifier holes that made it invisible ──────────────────────────────

def test_draw_no_bet_is_readable_on_the_permissive_path():
    """Skipped on the strict path because Pinnacle ships DNB as an
    unstructured type=special and it cannot be MATCHED. That is right for the
    +EV pipeline and wrong to inherit here: the consistency engine never
    touches Pinnacle, so matchability is beside the point."""
    ft = SOC.classify_market_title_permissive("Draw no bet")
    assert (ft.market_type, ft.period, ft.n_way) == ("moneyline", "FT", 2)
    h1 = SOC.classify_market_title_permissive("1st Half - Draw No Bet")
    assert (h1.market_type, h1.period, h1.n_way) == ("moneyline", "H1", 2)
    # ...and the strict path still skips it, so nothing reaches the matcher.
    assert SOC.classify_market_title("Draw no bet") is None


def test_the_three_way_second_half_variant_is_not_mistaken_for_it():
    """CB's `2nd Half - Draw No Bet***` is a 3-way scoreline derivative and a
    different bet. The pattern is anchored at both ends so the asterisks
    cannot carry it through."""
    assert SOC.classify_market_title_permissive("2nd Half - Draw No Bet***") is None


def test_both_spellings_of_the_asian_ladder_are_read():
    """The bigger hole. Whole-board census: CB titles this ladder "Handicap" on
    1 666 events and "Asian Handicap" on 196 — 10.5 % of the board that had no
    handicap coverage at all, in the anomaly scan OR the Pinnacle matching."""
    for title in ("Handicap", "Asian Handicap"):
        c = SOC.classify_market_title(title)
        assert c is not None, f"{title} is invisible"
        assert (c.market_type, c.period) == ("spread", "FT")


# ── pickem_duplicate: the same bet quoted twice ──────────────────────────────

def test_the_duplicate_lock_fires_and_prices_it():
    f = _kinds(_rows()).get("pickem_duplicate")
    assert f is not None, "the +7.4% lock was not detected"
    assert f.severity == pytest.approx(7.43, abs=0.01)
    assert "1.55" in f.detail and "3.5" in f.detail
    assert "void" in f.detail                       # says why there is no risk


def test_no_lock_when_the_two_quotes_agree():
    """Normal is agreement: over 1 414 events pricing both, the two sat a
    median 0.1pp apart. A duplicate that agrees is not an opportunity."""
    agree = {"home": 1.21, "away": 3.45}
    assert "pickem_duplicate" not in _kinds(_rows(DNB=agree))


def test_no_lock_when_one_market_is_better_on_both_sides():
    """Then there is no cross to take — you would simply always use that one."""
    assert "pickem_duplicate" not in _kinds(
        _rows(DNB={"home": 1.10, "away": 2.00}))


def test_legs_from_different_moments_are_not_a_position():
    """Same guard as pickem_arb, for the same reason: two prices captured
    minutes apart are not something you can actually take."""
    rows = _rows()
    rows[2] = _o("spread", "FT", BOARD["AH0"], line=0.0, section="Asian Handicap",
                 at=NOW + timedelta(seconds=C._PICKEM_MAX_SKEW_SEC + 1))
    assert "pickem_duplicate" not in _kinds(rows)


def test_a_plain_two_way_moneyline_is_not_treated_as_a_draw_no_bet():
    """The pairing is only exact where BOTH legs void on the draw. Basketball
    has no draw to void and American football's 2-way winner includes overtime
    while its 0.0 spread pushes on a regulation tie — so the section title has
    to be what identifies a DNB, not the two-way shape."""
    rows = _rows()
    rows[1] = _o("moneyline", "FT", BOARD["DNB"], section="Winner (incl. overtime)")
    assert "pickem_duplicate" not in _kinds(rows)


# ── pickem_dominance: the better bet at the longer price ─────────────────────

def test_dominance_fires_when_the_zero_rung_is_the_longer_price():
    f = _kinds(_rows()).get("pickem_dominance")
    assert f is not None, "the impossible ratio was not detected"
    assert f.outcome == "away"
    assert f.severity == pytest.approx(12.90, abs=0.01)
    assert "1.129" in f.detail


def test_dominance_is_silent_on_a_coherent_board():
    """The relation is exact: AH0/1X2 == 1 - P(draw). Measured over 5 986 sides
    the ratio ran median 0.688 and never once reached 1.0, so a coherent board
    must produce nothing."""
    coherent = {"home": 1.43, "away": 2.40}          # both well under the 1X2
    assert "pickem_dominance" not in _kinds(_rows(AH0=coherent))


def test_dominance_reads_raw_prices_so_no_devig_choice_can_move_it():
    """The point of this check: it compares prices you can actually take.
    Scaling both sides of the 1X2 by a constant changes its vig entirely and
    must not change the verdict."""
    base = _kinds(_rows()).get("pickem_dominance")
    vigged = {k: v * 0.97 for k, v in BOARD["1X2"].items()}
    still = _kinds(_rows(**{"1X2": vigged})).get("pickem_dominance")
    assert base is not None and still is not None
    assert still.severity > base.severity            # 1X2 shorter -> ratio worse


def test_dominance_covers_the_draw_no_bet_too_not_just_the_handicap():
    """GKS Wikielec v Concordia Elblag, 2026-08-29.

    Its 0.0 rung was perfectly coherent (2.30/1.45 against a 2.90/4.05/1.90
    1X2, ratio 0.793) while the DRAW NO BET was not: away @1.95 against the
    1X2's @1.90, ratio 1.026. Both markets void the draw, so both carry the
    same bound — checking only the handicap missed this board entirely.

    Calibration for the DNB arm, 218 sides: ratio p10 0.712, median 0.774
    (the theoretical 1 - P(draw)), p90 0.840, and one violation.
    """
    rows = [
        _o("moneyline", "FT", {"home": 2.90, "draw": 4.05, "away": 1.90},
           section="Main result"),
        _o("moneyline", "FT", {"home": 1.65, "away": 1.95}, section="Draw no bet"),
        _o("spread", "FT", {"home": 2.30, "away": 1.45}, line=0.0,
           section="Asian Handicap"),
    ]
    f = _kinds(rows).get("pickem_dominance")
    assert f is not None, "the DNB violation was not detected"
    assert f.outcome == "away"
    assert "draw-no-bet" in f.detail
    assert "1.026" in f.detail


def test_a_coherent_draw_no_bet_stays_quiet():
    """The normal case: DNB comfortably shorter than the 1X2 on both sides."""
    rows = [
        _o("moneyline", "FT", {"home": 2.90, "draw": 4.05, "away": 1.90},
           section="Main result"),
        _o("moneyline", "FT", {"home": 2.25, "away": 1.48}, section="Draw no bet"),
    ]
    assert "pickem_dominance" not in _kinds(rows)


def test_dominance_needs_all_three_legs_of_the_moneyline():
    """Without the draw price there is no P(draw) to quote, and the check would
    be asserting a bound it cannot explain."""
    rows = _rows()
    rows[0] = _o("moneyline", "FT", {"home": 1.85, "away": 3.10}, section="Main result")
    assert "pickem_dominance" not in _kinds(rows)


# ── both must be able to reach the alert list ────────────────────────────────

def test_the_new_kinds_alert_by_default():
    """These name a bet, unlike ml_vs_spread which was demoted for never
    converting. They must not inherit that silence."""
    from pathlib import Path
    import re
    page = (Path(C.__file__).resolve().parent.parent
            / "static" / "anomalies.html").read_text()
    labels = re.search(r"const KIND_LABEL\s*=\s*\{(.*?)\n\};", page, re.S).group(1)
    off = re.search(r"ALERT_DEFAULT_OFF\s*=\s*new Set\(\[(.*?)\]\)", page, re.S).group(1)
    for kind in ("pickem_duplicate", "pickem_dominance"):
        assert f"{kind}:" in labels, f"{kind} is not in the alert grid"
        assert f'"{kind}"' not in off, f"{kind} would never chime"


# ── fts_vs_ml, and the odds band ─────────────────────────────────────────────

FTS_BOARD = [
    _o("moneyline", "FT", {"home": 2.90, "draw": 4.05, "away": 1.90},
       section="Main result"),
    _o("fts", "FT", {"home": 1.80, "none": 17.1, "away": 1.95},
       section="First Team To Score"),
]


def test_first_team_to_score_flags_a_favourite_flip():
    """GKS Wikielec v Concordia Elblag: the 1X2 makes home a 2.90 underdog and
    First Team To Score makes it the 1.80 favourite to open the scoring."""
    f = _kinds(FTS_BOARD).get("fts_vs_ml")
    assert f is not None, "the flip was not detected"
    assert f.outcome == "home"
    assert f.odds == 1.8
    assert "underdog" in f.detail and "open the scoring" in f.detail


def test_it_needs_the_moneyline_side_to_be_decisive():
    """A bare flip is usually two near-coin-flips landing either side of 0.500.
    Measured over 90 events: bare flip fires on 3.3 %, flip-plus-decisive on
    1.1 %. Two of those three bare flips had the 1X2 within 3pp of even."""
    rows = [
        _o("moneyline", "FT", {"home": 2.55, "draw": 3.40, "away": 2.60},
           section="Main result"),          # home 49.5 % — not decisive
        _o("fts", "FT", {"home": 1.90, "none": 17.0, "away": 1.95},
           section="First Team To Score"),  # home 50.6 % — the other side of even
    ]
    assert "fts_vs_ml" not in _kinds(rows)


def test_it_stays_quiet_when_the_two_markets_agree():
    """The normal shape, and the measured one: FTS is a SHRUNK version of the
    1X2 (median -0.050), so agreement is the default."""
    rows = [
        _o("moneyline", "FT", {"home": 2.90, "draw": 4.05, "away": 1.90},
           section="Main result"),
        _o("fts", "FT", {"home": 2.30, "none": 17.1, "away": 1.62},
           section="First Team To Score"),
    ]
    assert "fts_vs_ml" not in _kinds(rows)


def test_nobody_scores_cannot_beat_the_whole_draw():
    """The one EXACT bound the pair has: 0-0 is one way to draw. Violated 0
    times in 90 events, so it is there for the day the book slips."""
    rows = [
        _o("moneyline", "FT", {"home": 2.90, "draw": 12.0, "away": 1.90},
           section="Main result"),          # draw only ~7 %
        _o("fts", "FT", {"home": 2.30, "none": 3.5, "away": 1.62},
           section="First Team To Score"),  # nobody-scores ~26 %
    ]
    f = _kinds(rows).get("fts_vs_ml")
    assert f is not None and "one way to draw" in f.detail


def test_every_flag_that_names_a_bet_carries_its_price():
    """The odds band on the tab reads `odds`, and ConsistencyFlag had no such
    field — so every consistency row was exempt from a filter the panel showed
    as applying to it. Checks that name no single leg stay None on purpose."""
    flags = {f.kind: f for f in C.find_consistency_flags(_rows() + FTS_BOARD[1:])}
    for kind in ("pickem_duplicate", "pickem_dominance"):
        assert flags[kind].odds is not None, f"{kind} carries no price"
        assert flags[kind].odds > 1.0
    assert flags["ml_vs_spread"].odds is None, (
        "ml_vs_spread describes a relationship between markets, not one leg — "
        "giving it a price would make the band hide it for the wrong reason")


# ── the HT/FT bettable-range cap ─────────────────────────────────────────────

HTFT_LEGS = [
    _o("moneyline", "FT", {"home": 2.00, "draw": 3.95, "away": 2.70},
       section="Main result"),
    _o("moneyline", "H1", {"home": 3.50, "draw": 2.70, "away": 2.15},
       section="1st Half Result"),
]
HTFT_GRID = {"1/1": 5.90, "1/X": 16.0, "1/2": 20.3, "X/1": 9.60, "X/X": 9.10,
             "X/2": 5.90, "2/1": 27.8, "2/X": 16.0, "2/2": 2.80}


def test_a_long_htft_price_is_no_longer_gated_out():
    """Lech Poznan Uam II v Staszkowka: 1/1 @5.90 against a correlation-fair
    5.25 is 12.4 % too generous, and the old 4.5 cap made the flag not exist.

    The cap earned its 15.0 by measurement: the model's p10-p90 dispersion is
    flat at ~10-11 % all the way to 15 and only widens past it (19.2). What
    drifts with price is the MEDIAN, downwards — so the +2 % bar gets harder to
    clear as the price lengthens, and a long-price flag is a bigger outlier
    than a short one.
    """
    assert C.HTFT_ODDS_MAX >= 5.90, "the cap would gate this out again"
    f = _kinds(HTFT_LEGS + [_o("htft", "FT", HTFT_GRID, section="HT/FT")])
    combo = f.get("htft_combo")
    assert combo is not None, "the 12.4% overprice was gated out"
    assert combo.outcome == "1/1"
    assert combo.odds == 5.9, "the flag must carry its price for the odds band"


def test_the_two_htft_caps_are_independent():
    """The regression guard for a mistake already made once.

    `HTFT_ODDS_MAX` had TWO consumers and only one was calibrated. htft_combo
    is soccer, two cells, a correlation-fair approximation — that is what the
    residual measurement covers. htft_fair is BASKETBALL, nine cells, a
    bivariate-normal model, with no calibration behind widening it.

    Raising the shared constant put 17 htft_fair flags on the live board with
    severities to 123.3, every one a "shape off vs model" on a reversal cell —
    the lowest-probability corner of the grid, where the model is least
    trustworthy and a small absolute error is a huge ratio.
    """
    assert C.HTFT_FAIR_ODDS_MAX == 4.5, (
        "htft_fair must keep the cap it was measured under")
    assert C.HTFT_ODDS_MAX > C.HTFT_FAIR_ODDS_MAX, (
        "the two checks are calibrated separately and must not share a cap")
    import inspect
    src = inspect.getsource(C._htft_fair_signals)
    assert "HTFT_FAIR_ODDS_MAX" in src and "HTFT_ODDS_MAX" not in src, (
        "htft_fair is reading the combo check's cap again")


def test_the_cap_still_stops_where_the_model_breaks_down():
    """Past 15 the residual spread nearly doubles (19.2 against ~10), so the
    fair value is no longer trustworthy enough to call anything an outlier."""
    assert C.HTFT_ODDS_MAX <= 15.0
    grid = dict(HTFT_GRID, **{"1/1": 60.0})
    assert "htft_combo" not in _kinds(
        HTFT_LEGS + [_o("htft", "FT", grid, section="HT/FT")])


# ── corners are a second board, and must never mix with goals ────────────────

def _corner(mt, per, sels, line=None, section=None):
    return Odds(source="crystalbet", sport="soccer", home="A", away="B",
                market_type=mt, period=per, selections=dict(sels),
                fetched_at=NOW, line=line, raw_event_id="e1",
                submarket="corners", section=section)


def test_a_corners_market_never_pairs_with_a_goals_market():
    """The bug this partition exists to prevent, and it would have been silent.

    A corners 1X2 and a goals 1X2 are both "moneyline FT"; a corners 0.0 rung
    and a goals one are both "spread FT line 0". Grouped on the event alone
    they land in one bucket, and every check then reads a goals price against a
    corners price — pickem_duplicate would "lock" a cover across two different
    things being counted.

    The invariant, stated so it does not depend on the fixture happening to be
    flag-free: scanning the two boards TOGETHER must produce exactly what
    scanning each one ALONE produces. Any extra flag is a cross-board pairing.
    """
    goals = [
        _o("moneyline", "FT", {"home": 2.50, "draw": 3.40, "away": 2.90},
           section="Main result"),
        _o("spread", "FT", {"home": 1.85, "away": 1.95}, line=0.0,
           section="Asian Handicap"),
    ]
    # A corners board with a very different balance — the cross pairing would
    # be wildly inconsistent, which is exactly what must not be looked at.
    corners = [
        _corner("moneyline", "FT", {"home": 1.40, "draw": 9.00, "away": 6.50},
                section="Corner Matchbet"),
        _corner("spread", "FT", {"home": 1.22, "away": 4.10}, line=0.0,
                section="Handicap of corner"),
    ]

    def sig(fs):
        return sorted((f.kind, f.submarket, f.severity) for f in fs)

    together = sig(C.find_consistency_flags(goals + corners))
    apart = sig(C.find_consistency_flags(goals) + C.find_consistency_flags(corners))
    assert together == apart, (
        "combining the boards produced a flag neither board produces alone — "
        "a goals market was compared against a corners market")


def test_a_corners_contradiction_is_still_found_and_labelled():
    """Partitioning must not mean ignoring — the checks run on the corners
    board on its own terms, and the flag says which board it is on."""
    rows = [
        _corner("moneyline", "FT", {"home": 2.90, "draw": 4.05, "away": 1.90},
                section="Corner Matchbet"),
        _corner("spread", "FT", {"home": 2.30, "away": 1.45}, line=0.0,
                section="Handicap of corner"),
        _corner("moneyline", "FT", {"home": 1.65, "away": 1.95},
                section="Draw no bet of corner"),
    ]
    flags = C.find_consistency_flags(rows)
    assert flags, "the corners board produced nothing"
    assert all(f.submarket == "corners" for f in flags)
    assert all(f.detail.startswith("[corners]") for f in flags), (
        "a corners flag must say so — otherwise it reads as a goals finding")


def test_the_corners_board_is_classified_at_all():
    """CB serves a full second board per match. The 3-way was skipped as
    "different shape from Pinnacle" — right for matching, wrong to inherit in a
    check that never touches Pinnacle."""
    for title, mt, per in (("Corner Matchbet", "moneyline", "FT"),
                           ("1st Half - CornerBet", "moneyline", "H1"),
                           ("Handicap of corner", "spread", "FT"),
                           ("Total Corners", "total", "FT")):
        c = SOC.classify_market_title_permissive(title)
        assert c is not None, f"{title} is invisible"
        assert (c.market_type, c.period, c.submarket) == (mt, per, "corners")
        assert SOC.classify_market_title(title) is None, (
            f"{title} must stay off the strict path — Pinnacle cannot pair it")


# ── Lider-Bet corners ────────────────────────────────────────────────────────

def test_liderbet_classifies_its_corner_board():
    """Lider ships a fuller corner board than CB: Corner Matchbet on 319
    events, Corners Handicap on 322, Total corners on 344, plus first-half
    variants on 335-344."""
    from src.scrapers import liderbet as LB
    for name, want in (("Corner Matchbet", ("moneyline", "FT", 3)),
                       ("Corners Handicap", ("spread", "FT", 2)),
                       ("Total corners", ("total", "FT", 2)),
                       ("1st Half - Corner Matchbet", ("moneyline", "H1", 3)),
                       ("1st Half - Corner Handicap", ("spread", "H1", 2))):
        assert LB._classify_corner_market(name) == want, name
    assert LB._classify_corner_market("Total") is None
    assert LB._classify_corner_market("Handicap") is None


def test_liderbet_corner_rows_are_labelled_and_do_not_leak():
    """`submarket` is set only on the corner branch, so it must be reset per
    market or a corners row leaves "corners" behind for the NEXT market in the
    loop — mislabelling a goals market, which the consistency engine would then
    check against the real corner board."""
    import inspect
    from src.scrapers import liderbet as LB
    src = inspect.getsource(LB._parse_match)
    assert "submarket: str | None = None" in src, "submarket is not reset per market"
    assert src.count("submarket=submarket") == src.count("_build(sport_name"), (
        "a _build call is missing submarket, so those rows would land unlabelled")
    # ...and the scraper can actually carry it
    assert "submarket" in inspect.signature(LB._build).parameters
