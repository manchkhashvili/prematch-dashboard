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
