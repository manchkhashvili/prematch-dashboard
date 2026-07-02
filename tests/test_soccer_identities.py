"""Tests for src.soccer_identities — model-free algebra checks on raw odds."""
import pytest

from src import soccer_model as sm
from src.soccer_identities import identity_flags


def test_nepean_column_partition_fires():
    # The hand-validated case: the /2 column (away FT via HT/FT) is priced more
    # generously than the FT-away leg itself.
    #   raw: 1/16.2 + 1/4.85 + 1/1.80 = 0.8235  <  1/1.18 = 0.8475
    posted = {
        "ft_1x2:2": 1.18,
        "htft:1/2": 16.2, "htft:X/2": 4.85, "htft:2/2": 1.80,
    }
    flags = identity_flags(posted)
    col = [f for f in flags if f.kind == "partition" and f.keys[0] == "ft_1x2:2"]
    assert col, "the /2 column partition must fire"
    f = col[0]
    assert f.severity == pytest.approx(2.4, abs=0.3)          # ~2.4pp gap
    assert f.suspect == "htft:1/2"                            # longest-priced leg


def test_clean_column_does_not_fire():
    # Parts carry their own vig, so a normal book has parts_sum > whole.
    posted = {
        "ft_1x2:2": 1.20,                                     # raw 0.833
        "htft:1/2": 13.0, "htft:X/2": 4.2, "htft:2/2": 1.70,  # raw ~0.899 > 0.833
    }
    assert not [f for f in identity_flags(posted) if f.keys[0] == "ft_1x2:2"]


def test_double_chance_partition():
    # DC 1X priced longer than backing 1 and X separately → violation.
    posted = {"dc:1X": 1.50, "ft_1x2:1": 2.10, "ft_1x2:X": 3.60}  # 0.667 vs 0.754
    # here parts_sum(0.754) > whole(0.667) → NO flag (normal). Flip to violate:
    posted = {"dc:1X": 1.25, "ft_1x2:1": 2.10, "ft_1x2:X": 4.20}  # whole 0.80 > parts 0.714
    f = [x for x in identity_flags(posted) if x.name.startswith("DC 1X")]
    assert f and f[0].severity > 0


def test_equivalence_gap_flagged():
    posted = {"ah:home_0": 2.10, "dnb:1": 1.90}               # 0.476 vs 0.526
    f = [x for x in identity_flags(posted) if x.kind == "equivalence"]
    assert f and f[0].suspect == "ah:home_0"                  # back the longer price


def test_intra_book_arb_flagged():
    posted = {"ah:home_0": 2.10, "dnb:2": 2.10}               # 0.476 + 0.476 < 1
    f = [x for x in identity_flags(posted) if x.kind == "arb"]
    assert f and f[0].severity == pytest.approx(5.0, abs=0.5)


def test_quarter_line_deviation():
    # -0.25 should be the average of 0 and -0.5; here it's priced far off.
    posted = {"ah:home_-0.25": 2.30, "ah:home_0": 2.00, "ah:home_-0.5": 2.05}
    f = [x for x in identity_flags(posted) if x.name.startswith("quarter")]
    assert f


def test_fair_sheet_produces_no_identity_flags():
    # A perfectly consistent (vig-free) sheet must violate nothing.
    m = sm.model_from_market([8.90, 6.15, 1.18], devig_method="power",
                             split=0.45, dc_rho=0.0)
    posted = {k: v["odds"] for k, v in m.prices.items() if v["odds"] < 1e6}
    assert identity_flags(posted) == []
