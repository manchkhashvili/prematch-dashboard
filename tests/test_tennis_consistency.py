"""Tennis set-vs-match consistency (added 2026-08-11).

Tennis was excluded from the consistency engine and ran list-only, so the only
tennis markets that existed were match ML / games spread / games total. A CB
tennis DETAIL page actually carries 15 markets on every match, all functions of
the same four best-of-3 outcomes — so they must agree arithmetically.

The check that prompted this, seen on CB (ITF Campos Do Jordao, women):
    match winner   1.40 / 2.35  -> P(win) 0.63
    1st set winner 1.11 / 4.30  -> P(win) 0.79
A player cannot be less likely to win the MATCH than to win its first set: in a
best-of-3 you can drop the opener and still win. This one was 21-26pp out.

Calibrated over ALL 525 checkable live matches, not a sample — an early
130-match sample only covered CB's tighter naming scheme and badly understated
the spread. Full board: p50 2.07pp, p90 6.13, p99 8.48, max 9.32, and 0 flags at
the shipped thresholds.

CB serves tennis under TWO naming schemes: 71% of matches use plain "Winner" +
"1st Period Winner Home/Away", 29% use "Which player will win the match" +
"1st Set - Winner". Covering only the latter left match-winner coverage at 43
events against 218 with a first-set winner; covering both took it to 95%.
"""
from datetime import datetime, timezone

import pytest

from src.consistency import (
    SET_MATCH_HARD_MIN_FAV, SET_MATCH_HARD_MIN_PP, SET_MATCH_PP,
    _match_prob_from_set, _set_prob_from_match, find_consistency_flags,
)
from src.models import Odds
from src.scrapers.sports import tennis

NOW = datetime.now(tz=timezone.utc)


def _t(mt, per, sel, event="T1", line=None):
    return Odds(source="crystalbet", sport="tennis", home="Teodoro Souza D.",
                away="Cerqueira Carvalho Farias Lima J.", market_type=mt,
                period=per, selections=sel, fetched_at=NOW, line=line,
                league="ITF. Campos Do Jordao, Women", raw_event_id=event,
                section=mt + per)


def _flags(rows):
    return [f for f in find_consistency_flags(rows) if f.kind == "tennis_set_match"]


# ── the model ────────────────────────────────────────────────────────────────

def test_match_prob_exceeds_set_prob_for_a_favourite():
    """The whole basis of the check: winning the match is EASIER than winning
    the first set once you are better than even, because you get three chances
    to take two."""
    for p in (0.55, 0.6, 0.7, 0.8, 0.9):
        assert _match_prob_from_set(p) > p


def test_set_and_match_probability_round_trip():
    for p in (0.35, 0.5, 0.62, 0.79, 0.95):
        assert _set_prob_from_match(_match_prob_from_set(p)) == pytest.approx(p, abs=1e-3)


def test_inverse_rejects_degenerate_input():
    assert _set_prob_from_match(0.0) is None
    assert _set_prob_from_match(1.0) is None


# ── the detector ─────────────────────────────────────────────────────────────

def test_the_real_screenshot_case_is_flagged():
    rows = [_t("moneyline", "FT", {"home": 1.40, "away": 2.35}),
            _t("moneyline", "H1", {"home": 1.11, "away": 4.30})]
    f = _flags(rows)
    assert f, "the match was priced BELOW its own first set — must flag"
    assert f[0].periods == "H1 vs FT"
    assert f[0].severity > 10


def test_flag_is_orientation_independent():
    """Same fixture with the players swapped must produce the same flag — the
    rule is about the favourite, not about which name is 'home'."""
    rows = [_t("moneyline", "FT", {"home": 2.35, "away": 1.40}, event="T2"),
            _t("moneyline", "H1", {"home": 4.30, "away": 1.11}, event="T2")]
    assert _flags(rows)


def test_coherently_priced_match_is_silent():
    """p_set ~0.62 implies p_match ~0.68 — priced consistently, no flag."""
    rows = [_t("moneyline", "FT", {"home": 1.42, "away": 3.05}, event="OK"),
            _t("moneyline", "H1", {"home": 1.55, "away": 2.45}, event="OK")]
    assert not _flags(rows)


def test_normal_book_noise_stays_under_threshold():
    """Calibrated over ALL 525 checkable live matches (not a sample): p90 6.13pp,
    p99 8.48pp, max 9.32pp. The soft threshold must sit above the observed max so
    ordinary pricing never fires — the full board produced 0 flags."""
    assert SET_MATCH_PP > 9.32


def test_near_even_match_is_not_a_hard_violation():
    """The order rule only carries information for a CLEAR favourite. At p ~ 0.5
    the match and set probabilities coincide, so a one-tick pricing difference
    flips which player looks favoured. Real board example that used to produce a
    bogus 'impossible' flag: match 1.70/1.80 against set 1.80/1.70."""
    rows = [_t("moneyline", "FT", {"home": 1.70, "away": 1.80}, event="EVEN"),
            _t("moneyline", "H1", {"home": 1.80, "away": 1.70}, event="EVEN")]
    assert not _flags(rows)


def test_hard_rule_needs_a_real_favourite():
    assert SET_MATCH_HARD_MIN_FAV >= 0.55
    assert SET_MATCH_HARD_MIN_PP >= 2.0


def test_match_without_a_first_set_market_is_silent():
    assert not _flags([_t("moneyline", "FT", {"home": 1.40, "away": 2.35})])


def test_other_sports_are_untouched():
    b = Odds(source="crystalbet", sport="basketball", home="A", away="B",
             market_type="moneyline", period="FT",
             selections={"home": 1.9, "away": 1.9}, fetched_at=NOW,
             raw_event_id="B1", section="mlFT")
    assert not [f for f in find_consistency_flags([b]) if f.kind == "tennis_set_match"]


# ── the classifier that makes the markets exist at all ───────────────────────

@pytest.mark.parametrize("title,mt,per", [
    ("Which player will win the match", "moneyline", "FT"),
    ("Winner", "moneyline", "FT"),                        # scheme A, 71% of matches
    ("2nd Period Winner Home/Away", "moneyline", "H2"),
    ("Under/Over Sets", "total", "FT"),
    ("1st Set - Winner", "moneyline", "H1"),
    ("2nd Set - Winner", "moneyline", "H2"),
    ("1st Period Winner Home/Away", "moneyline", "H1"),   # live board's wording
    ("1st Set / Match*", "htft", "FT"),
    ("Total sets", "total", "FT"),
])
def test_tennis_titles_classify(title, mt, per):
    c = tennis.classify_market_title(title)
    assert c is not None, f"{title!r} should classify"
    assert (c.market_type, c.period) == (mt, per)


@pytest.mark.parametrize("title", [
    "Correct score",            # 4-way; Odds has no representation yet
    "Home Team To Win a Set",   # yes/no
    "Odd/even games",
    "Set Handicap",             # would collide with the list-view GAMES spread
    "Winner & total",           # exact-match guard: must NOT be the match winner
    "1 set - winner & total",
    "To win 1st set & win the match",   # this is P(1/1), not the match winner
    "",
])
def test_unsupported_tennis_titles_are_skipped(title):
    assert tennis.classify_market_title(title) is None
