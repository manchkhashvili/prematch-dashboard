"""
Tennis correct-score: the CB parser, the classifier guard, and the
latent-strength model that prices the four legs off the other two markets.

The model tests are the ones that matter. Its whole claim is that CB's
1st-set price and match price DETERMINE the correct-score partition with no
free parameter, so the properties worth pinning are the structural ones —
it sums to 1, it degenerates to the existing IID function, and it round-trips
the price it was solved from. A calibration that drifts off any of those is
measuring something other than what it says.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.consistency import (  # noqa: E402
    _cs_match_prob, _cs_moments, _cs_partition, _cs_solve_nu,
    _match_prob_from_set, _set_prob_from_match,
)
from src.scrapers.cb_detail import _parse_correct_score  # noqa: E402
from src.scrapers.sports import tennis  # noqa: E402


def _snatches(pairs):
    """Build DetailSnatch cells in CB's shape: bt1 = label, bt2 = odds."""
    cells = "".join(
        f'<div class="sport_more_bt DetailSnatch">'
        f'<div class="sport_more_bt1">{lab}</div>'
        f'<div class="sport_more_bt2">{od}</div></div>'
        for lab, od in pairs
    )
    return BeautifulSoup(f"<div>{cells}</div>", "html.parser").select(".DetailSnatch")


# ── classifier ───────────────────────────────────────────────────────────────
class TestClassifier:
    @pytest.mark.parametrize("title", ["Correct Score", "Correct score",
                                       "  CORRECT SCORE  "])
    def test_both_naming_schemes_classify(self, title):
        cls = tennis.classify_market_title(title)
        assert cls is not None
        assert (cls.market_type, cls.period) == ("correct_score", "FT")

    @pytest.mark.parametrize("title", [
        "Correct score 1st set",       # games inside ONE set — a different partition
        "Correct Score 2nd Set",
        "Correct score - sets handicap",
    ])
    def test_per_set_variants_are_not_swallowed(self, title):
        """The reason the match is exact rather than a substring test.

        A per-set correct score partitions the GAMES in one set. Devigging it
        together with the match-level partition would mix two different sample
        spaces and manufacture edge out of the difference.
        """
        assert tennis.classify_market_title(title) is None


# ── parser ───────────────────────────────────────────────────────────────────
class TestParser:
    def test_hyphen_and_colon_normalise_to_the_same_keys(self):
        a = _parse_correct_score(_snatches([("2-0", "7.30"), ("2-1", "7.20"),
                                            ("1-2", "3.80"), ("0-2", "1.70")]))
        b = _parse_correct_score(_snatches([("2:0", "7.30"), ("2:1", "7.20"),
                                            ("1:2", "3.80"), ("0:2", "1.70")]))
        assert a == b == {"2-0": 7.30, "2-1": 7.20, "1-2": 3.80, "0-2": 1.70}

    def test_incomplete_partition_returns_none(self):
        """A missing leg must kill the row, not shrink it.

        Proportional devig over three legs of a four-leg partition inflates
        every survivor, and the check would read that inflation as edge.
        """
        assert _parse_correct_score(_snatches([
            ("2-0", "7.30"), ("2-1", "7.20"), ("1-2", "3.80")])) is None

    def test_suspended_leg_is_an_incomplete_partition(self):
        """_safe_float drops <= 1.0, which must take the whole market with it."""
        assert _parse_correct_score(_snatches([
            ("2-0", "7.30"), ("2-1", "7.20"), ("1-2", "3.80"),
            ("0-2", "1.00")])) is None

    def test_best_of_five_parses(self):
        got = _parse_correct_score(_snatches([
            ("3-0", "4.0"), ("3-1", "4.5"), ("3-2", "6.0"),
            ("2-3", "5.0"), ("1-3", "4.2"), ("0-3", "3.9")]))
        assert got is not None and len(got) == 6

    def test_best_of_five_missing_a_leg_returns_none(self):
        assert _parse_correct_score(_snatches([
            ("3-0", "4.0"), ("3-1", "4.5"), ("3-2", "6.0"),
            ("2-3", "5.0"), ("1-3", "4.2")])) is None

    def test_non_score_labels_ignored(self):
        assert _parse_correct_score(_snatches([("1", "1.5"), ("2", "2.5")])) is None


# ── the model ────────────────────────────────────────────────────────────────
class TestLatentStrengthModel:
    @pytest.mark.parametrize("mu", [0.35, 0.50, 0.62, 0.7110, 0.88])
    @pytest.mark.parametrize("nu", [5.0, 50.0, 271.3, 1e9])
    def test_partition_sums_to_one(self, mu, nu):
        assert sum(_cs_partition(mu, nu).values()) == pytest.approx(1.0, abs=1e-12)

    @pytest.mark.parametrize("mu", [0.35, 0.55, 0.7110, 0.88])
    def test_infinite_concentration_is_the_iid_model(self, mu):
        """nu -> inf must reproduce _match_prob_from_set exactly, or the new
        model is not a generalisation of the one already in production."""
        assert _cs_match_prob(mu, 1e12) == pytest.approx(_match_prob_from_set(mu),
                                                         abs=1e-9)
        iid = _cs_partition(mu, 1e12)
        assert iid["0-2"] == pytest.approx(mu * mu, abs=1e-8)
        assert iid["2-0"] == pytest.approx((1 - mu) ** 2, abs=1e-8)

    @pytest.mark.parametrize("mu,pm", [(0.7110, 0.7967), (0.62, 0.66), (0.55, 0.57)])
    def test_solve_nu_round_trips(self, mu, pm):
        nu = _cs_solve_nu(mu, pm)
        assert nu is not None
        assert _cs_match_prob(mu, nu) == pytest.approx(pm, abs=1e-9)

    @pytest.mark.parametrize("mu", [0.15, 0.29, 0.38, 0.45])
    def test_underdog_orientation_solves(self, mu):
        """The regression that a live board caught.

        P(match) is monotone in nu, but the direction REVERSES at mu = 0.5.
        Solving without orienting onto the favourite rejected every event whose
        away player was the underdog — 55% of the CB tennis board on
        2026-09-08. nu is symmetric under P <-> 1-P, so both sides must solve
        to the SAME concentration.
        """
        fav = 1.0 - mu
        pm_fav = _match_prob_from_set(fav) * 0.98
        nu_fav = _cs_solve_nu(fav, pm_fav)
        nu_dog = _cs_solve_nu(mu, 1.0 - pm_fav)
        assert nu_fav is not None and nu_dog is not None
        assert nu_dog == pytest.approx(nu_fav, rel=1e-6)
        # and the partition still closes from the dog's side
        assert sum(_cs_partition(mu, nu_dog).values()) == pytest.approx(1.0, abs=1e-12)

    @pytest.mark.parametrize("mu", [0.55, 0.62, 0.7110, 0.85])
    def test_reachable_match_prices_are_one_sided(self, mu):
        """IID is the CEILING, not the middle — and this bounds the check.

        Variance only ever costs a favourite match equity, so every nu lands at
        or below _match_prob_from_set(mu). A book whose match price is ABOVE
        the one its own 1st-set price implies is asserting NEGATIVE set-to-set
        correlation, which no latent-strength distribution can express: solve_nu
        returns None and the event is skipped rather than fitted with a clamped
        nu that would report edge on every leg.

        How much of a live board that costs is a coverage question, not a
        correctness one — scripts/calibrate_tennis_correct_score.py counts it
        as the "no-nu" skip bucket.
        """
        ceiling = _match_prob_from_set(mu)
        assert _cs_solve_nu(mu, ceiling * 0.98) is not None
        assert _cs_solve_nu(mu, min(ceiling * 1.02, 0.999)) is None

    def test_variance_is_the_correlation(self):
        """P(2-0) = mu^2 + sigma^2 — the straight-set lift IS the variance,
        which is what makes 'winning set 1 shortens set 2' arithmetic rather
        than a fudge factor."""
        mu, nu = 0.7110, 271.3
        sig2 = mu * (1 - mu) / (nu + 1)
        assert _cs_partition(mu, nu)["0-2"] == pytest.approx(mu * mu + sig2, abs=1e-9)

    def test_variance_costs_a_favourite_match_equity(self):
        """Monotonicity the bisection in _cs_solve_nu depends on: for a
        favourite, more uncertainty means a lower match probability."""
        mu = 0.75
        probs = [_cs_match_prob(mu, nu) for nu in (5, 20, 100, 1000, 1e9)]
        assert probs == sorted(probs)

    def test_unreachable_match_price_returns_none(self):
        """A match price no nu can produce is a contradiction the set/match
        check already owns — solve_nu must decline it, not clamp."""
        assert _cs_solve_nu(0.71, 0.30) is None
        assert _cs_solve_nu(0.71, 0.99) is None

    def test_model_agrees_with_the_hand_worked_case(self):
        """Balazs-Parizzia, 2026-09-08 — the match that prompted all of this.
        Set-1 3.05/1.24 and match 4.35/1.11 devig to these, and the solved
        model says the board's 1-2 @ 3.80 is the generous leg."""
        mu, pm = 0.71095, 0.79670
        nu = _cs_solve_nu(mu, pm)
        legs = _cs_partition(mu, nu)
        assert legs["0-2"] == pytest.approx(0.5062, abs=5e-4)
        assert legs["1-2"] == pytest.approx(0.2905, abs=5e-4)
        assert 3.80 * legs["1-2"] - 1 == pytest.approx(0.104, abs=5e-3)
        # and the market it was solved from is reproduced
        assert _set_prob_from_match(pm) == pytest.approx(mu, abs=2e-3)


# ── the check, end to end ────────────────────────────────────────────────────
from datetime import datetime, timezone  # noqa: E402

from src.consistency import (  # noqa: E402
    _cs_fair_from_match, _devig_home, find_consistency_flags,
)
from src.models import Odds  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _event(ml_ft, ml_h1, cs, home="A. Player", away="B. Player"):
    def row(mt, per, sels):
        return Odds(source="crystalbet", sport="tennis", home=home, away=away,
                    market_type=mt, period=per, selections=sels, fetched_at=NOW,
                    start_time=NOW, league="ITF", raw_event_id="evt1")
    return [row("moneyline", "FT", {"home": ml_ft[0], "away": ml_ft[1]}),
            row("moneyline", "H1", {"home": ml_h1[0], "away": ml_h1[1]}),
            row("correct_score", "FT", dict(cs))]


def _cs_flags(odds):
    return [f for f in find_consistency_flags(odds)
            if f.kind == "tennis_correct_score"]


class TestCheckEndToEnd:
    # Rafael Kis Balazs vs Parizzia N., CB board 2026-09-08 — the screenshot
    # that started this. Match 4.35/1.11, and the board's 0-2 at 1.70 against a
    # fair 1.54.
    # Match 4.35/1.11; the board's 0-2 at 1.70 against a fair 1.49. On the
    # calibration board this was the single strongest finding of 250 events.
    BALAZS = dict(ml_ft=(4.35, 1.11), ml_h1=(3.05, 1.24),
                  cs={"2-0": 7.30, "2-1": 7.20, "1-2": 3.80, "0-2": 1.70})

    def test_fires_on_the_screenshot(self):
        flags = _cs_flags(_event(**self.BALAZS))
        assert len(flags) == 1
        f = flags[0]
        assert f.outcome == "0-2" and f.odds == pytest.approx(1.70)
        assert f.severity == pytest.approx(14.1, abs=1.0)     # severity is EDGE %
        assert "fair" in f.detail

    def test_first_set_price_is_not_required(self):
        """The model reads ONLY the match price, by design.

        The earlier version fitted its variance per match from the 1st-set
        price and was unidentified near an even match (see CS_SIGMA2). Holding
        that market out is what makes it available as an independent check —
        and it lifts coverage to every match with a correct-score board.
        """
        odds = [o for o in _event(**self.BALAZS) if o.period != "H1"]
        flags = _cs_flags(odds)
        assert len(flags) == 1 and flags[0].outcome == "0-2"

    def test_a_board_priced_at_fair_is_silent(self):
        """A correct-score market that agrees with the match price says nothing.

        Built by pricing the model's own partition with a flat 12% overround —
        what a book consistent with its own moneyline looks like.
        """
        pm = 1.0 - _devig_home(4.35, 1.11)
        cs = {k: 1.0 / (v * 1.12) for k, v in _cs_fair_from_match(pm).items()}
        assert _cs_flags(_event((4.35, 1.11), (3.05, 1.24), cs)) == []

    def test_the_book_charging_too_much_is_not_a_flag(self):
        """Only the generous direction is actionable. Shortening every leg is
        just more vig, and it is most of the board."""
        pm = 1.0 - _devig_home(4.35, 1.11)
        cs = {k: 1.0 / (v * 1.45) for k, v in _cs_fair_from_match(pm).items()}
        assert _cs_flags(_event((4.35, 1.11), (3.05, 1.24), cs)) == []

    def test_non_price_moneyline_is_refused(self):
        """Goncalo M. vs Nunez L., 2026-09-08: priced 1.01/9.00.

        A 1.01 leg is vig.py's '>0.99 implied' solver edge case, not a price,
        and the whole fair partition is derived from it.
        """
        odds = _event((1.01, 9.00), (1.01, 7.40),
                      {"2-0": 1.18, "2-1": 5.75, "1-2": 17.0, "0-2": 18.5})
        assert _cs_flags(odds) == []

    def test_longshot_legs_are_refused(self):
        """A small absolute error is a huge RELATIVE edge on a long price.

        The calibration board's top raw finding was 0-2 @ 20.60 at '+18.6%',
        where model and book differ by well under a point of probability.
        """
        pm = 1.0 - _devig_home(1.15, 6.50)
        fair = _cs_fair_from_match(pm)
        # price the longest leg far above fair, but beyond CS_MAX_LEG_ODDS
        longest = max(fair, key=lambda k: 1.0 / fair[k])
        cs = {k: 1.0 / (v * 1.10) for k, v in fair.items()}
        cs[longest] = (1.0 / fair[longest]) * 2.0
        assert cs[longest] > 8.0
        assert [f for f in _cs_flags(_event((1.15, 6.50), (1.19, 5.50), cs))
                if f.outcome == longest] == []

    def test_ordinary_favourite_is_processed(self):
        """The moneyline guard is on implied probability, not on favouritism.

        Figl M. vs Nagel A is 3.85/1.15 — a normal 87% favourite. vig.py's
        literal 1.20 floor would discard it along with 30% of the board, so a
        genuinely generous leg on such a match must still be reachable.
        """
        pm = 1.0 - _devig_home(3.85, 1.15)
        fair = _cs_fair_from_match(pm)
        cs = {k: 1.0 / (v * 1.10) for k, v in fair.items()}
        cs["1-2"] = (1.0 / fair["1-2"]) * 1.25          # 25% over fair
        assert cs["1-2"] <= 8.0
        flags = _cs_flags(_event((3.85, 1.15), (3.45, 1.19), cs))
        assert [f.outcome for f in flags] == ["1-2"]
