"""The Anomalies tab ranks by money, and says which kind of money it is.

The bug these pin: `cons.sort(key=severity)` ordered 33 checks whose severity
is variously a locked return on outlay (%), a model EV (%), a probability gap
(pp) and a difference in goals (points). Measured on the 60-row board of
2026-09-23, a +4.5% LOCKED cover would have sorted 17th and a +2.0% one 44th,
below rows like "the superset pays 9.5% more" — which is not money at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import flag_rank

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "static" / "anomalies.html").read_text()
MIRROR = (ROOT / "static" / "new_inconsistencies.html").read_text()
APP = (ROOT / "src" / "app.py").read_text()


def _block(src: str, head: str) -> str:
    i = src.index(head)
    return src[i:src.index("\n};", i)]


# ── the classification must be total ─────────────────────────────────────────

def test_every_rendered_check_is_classified():
    """A detector that ships without a BASIS entry falls silently into the
    unpriced tier — its money stops being ranked as money and nothing says so.
    KIND_LABEL is the tab's own list of everything it can render."""
    labels = set(re.findall(r'^\s{2}(\w+):\s*"', _block(PAGE, "const KIND_LABEL"), re.M))
    assert labels, "could not parse KIND_LABEL"
    missing = labels - set(flag_rank.BASIS)
    assert not missing, f"checks with no basis in flag_rank.BASIS: {sorted(missing)}"


def test_no_stale_entries():
    labels = set(re.findall(r'^\s{2}(\w+):\s*"', _block(PAGE, "const KIND_LABEL"), re.M))
    stale = set(flag_rank.BASIS) - labels
    assert not stale, f"flag_rank.BASIS classifies checks the tab cannot render: {sorted(stale)}"


def test_every_basis_is_a_known_tier():
    assert set(flag_rank.BASIS.values()) <= set(flag_rank.TIER)


# ── the ordering ─────────────────────────────────────────────────────────────

def _row(kind, severity, **kw):
    return dict(kind=kind, severity=severity, **kw)


def test_a_small_lock_outranks_a_huge_gap():
    """The owner's report, reduced to two rows: a certain +0.5% must not sit
    below a 99pp disagreement that names no bet."""
    rows = flag_rank.sort_flags([
        _row("ot_monotone", 99.0),
        _row("combo_cover", 0.5),
    ])
    assert rows[0]["kind"] == "combo_cover"
    assert rows[0]["ev_pct"] == 0.5
    assert rows[1]["ev_pct"] is None


def test_locked_outranks_model_ev_of_any_size():
    rows = flag_rank.sort_flags([
        _row("soccer_fair", 40.0),
        _row("pickem_arb", 1.0),
    ])
    assert [r["kind"] for r in rows] == ["pickem_arb", "soccer_fair"]


def test_faults_stay_pinned_above_everything():
    """duplicate_fixture carries a flat 100 by design — a book listing one
    match twice always alerts rather than competing on size."""
    rows = flag_rank.sort_flags([
        _row("combo_cover", 12.0),
        _row("duplicate_fixture", 100.0),
    ])
    assert rows[0]["kind"] == "duplicate_fixture"


def test_within_a_tier_the_bigger_ev_wins():
    rows = flag_rank.sort_flags([
        _row("combo_fair", 3.0), _row("soccer_fair", 9.0), _row("htft_combo", 6.0),
    ])
    assert [r["severity"] for r in rows] == [9.0, 6.0, 3.0]


def test_gap_rows_keep_their_own_order():
    rows = flag_rank.sort_flags([
        _row("ml_vs_spread", 4.0), _row("ot_monotone", 21.0), _row("fts_vs_ml", 9.0),
    ])
    assert [r["severity"] for r in rows] == [21.0, 9.0, 4.0]


# ── ev_pct is money or it is nothing ─────────────────────────────────────────

@pytest.mark.parametrize("kind", [k for k, b in flag_rank.BASIS.items() if b in flag_rank.PAYS])
def test_paying_kinds_carry_their_ev(kind):
    assert flag_rank.annotate(_row(kind, 7.25))["ev_pct"] == 7.25


@pytest.mark.parametrize("kind", [k for k, b in flag_rank.BASIS.items() if b not in flag_rank.PAYS])
def test_unpriced_kinds_carry_no_ev(kind):
    """A 22pp gap is not 22% of anything. Deriving an EV here would need a
    decision about WHICH of the two disagreeing markets is wrong, and that is
    a model — one that would put the least trustworthy rows back on top
    wearing a money sign."""
    assert flag_rank.annotate(_row(kind, 22.0))["ev_pct"] is None


def test_an_unknown_kind_does_not_raise_and_does_not_claim_money():
    f = flag_rank.annotate(_row("some_new_detector", 50.0))
    assert f["basis"] == "gap" and f["ev_pct"] is None


# ── the split kinds ──────────────────────────────────────────────────────────

def test_a_row_may_override_its_kind():
    """htft_fair emits an EV on its EDGE branch and a distance between two
    probabilities on its SHAPE branch. Same kind, only one is money."""
    assert flag_rank.basis_of(_row("htft_fair", 13.6)) == "ev"
    assert flag_rank.basis_of(_row("htft_fair", 13.6, basis="gap")) == "gap"


def test_the_override_moves_the_row():
    rows = flag_rank.sort_flags([
        _row("htft_fair", 13.6, basis="gap"),        # SHAPE: not an EV
        _row("combo_fair", 1.0),
    ])
    assert [r["kind"] for r in rows] == ["combo_fair", "htft_fair"]


# ── the dominance tier ───────────────────────────────────────────────────────

def test_dominance_sits_between_locked_and_ev():
    """Owner, on a pickem_dominance row that the first cut put in `gap`:
    "they should be second before ev betts". A 0.0 handicap quoted LONGER
    than the 1X2 on the same side is arithmetic on raw prices, not a soft
    disagreement — the 0.0 voids the draw where the 1X2 loses, so it is the
    strictly better bet, and the better bet cannot be the longer price."""
    rows = flag_rank.sort_flags([
        _row("soccer_fair", 40.0),          # ev
        _row("pickem_dominance", 1.0),      # dom
        _row("combo_cover", 0.1),           # locked
        _row("ml_vs_spread", 30.0),         # gap
    ])
    assert [r["basis"] for r in rows] == ["locked", "dom", "ev", "gap"]


def test_every_dominance_check_is_in_the_tier():
    """All three are containments — outcome set A inside B, B quoted longer.
    None of them may sit in `gap` or be ranked as a plain model EV."""
    for kind in ("pickem_dominance", "combo_dominance", "vb_sets_dominance"):
        assert flag_rank.BASIS[kind] == "dom", kind


def test_the_tier_ranks_on_the_impossibility_not_on_severity():
    """The tier mixes rows whose severity is an EV with rows whose severity is
    a price improvement, so severity cannot order it — that is the same
    category error one tier down."""
    rows = flag_rank.sort_flags([
        _row("combo_dominance", 9.0, dom_pct=2.0, ev_pct=9.0),   # priced
        _row("pickem_dominance", 4.0),                            # dom_pct = 4.0
    ])
    assert [r["kind"] for r in rows] == ["pickem_dominance", "combo_dominance"]


def test_an_unpriced_dominance_row_defaults_its_size_to_severity():
    f = flag_rank.annotate(_row("pickem_dominance", 13.3))
    assert f["dom_pct"] == 13.3 and f["ev_pct"] is None


def test_a_priced_dominance_row_keeps_both_numbers():
    """combo_dominance's totals branch and vb_sets_dominance gate on a model
    fair before firing, so they carry an EV on top of the impossibility."""
    f = flag_rank.annotate(_row("vb_sets_dominance", 6.4, dom_pct=18.0, ev_pct=6.4))
    assert f["dom_pct"] == 18.0 and f["ev_pct"] == 6.4


def test_htft_fair_splits_its_two_branches():
    """The EDGE branch reports posted x p_fair - 1 (money); the SHAPE branch
    reports |ratio - 1| between two probabilities (not money). Both ship as
    `htft_fair`, so the emitter has to say which."""
    from src import consistency
    src = Path(consistency.__file__).read_text()
    body = src[src.index("def _htft_fair_signals"):]
    body = body[:body.index("\ndef ", 10)]
    assert '"ev"' in body and '"gap"' in body, (
        "_htft_fair_signals no longer labels its two branches — the shape "
        "disagreement will be ranked as an expected value again"
    )


def test_lider_htft_dominance_carries_no_ev():
    """The HT/FT grid has no atom model, so 'the superset pays X% more' is a
    real impossibility with no fair behind it: dom_pct, and no ev_pct."""
    from src import lider_combos
    src = Path(lider_combos.__file__).read_text()
    i = src.index('f"HT/FT: \'{la}\'')
    tail = src[i:i + 700]
    assert "dom_pct=gain" in tail
    assert "ev_pct" not in tail, (
        "the HT/FT containment row claims an expected value, but nothing "
        "prices it — a superset can pay 9% more and still be EV -40%"
    )


def test_lider_totals_dominance_carries_both():
    from src import lider_combos
    src = Path(lider_combos.__file__).read_text()
    i = src.index("dom_pct=gain, ev_pct=ev_sup")
    assert i > 0, "the priced containment row lost one of its two numbers"


# ── the wiring ───────────────────────────────────────────────────────────────

def test_no_raw_severity_sort_survives_in_the_api():
    """Every place that orders consistency rows must go through flag_rank, or
    one of the tabs silently keeps the old category error."""
    assert 'cons.sort(key=lambda f: f["severity"]' not in APP
    assert 'out.sort(key=lambda r: (r["severity"] or 0.0), reverse=True)' not in APP
    assert APP.count("flag_rank.sort_flags(") >= 3


def test_the_alert_feed_carries_the_basis():
    """ALERT_FEED_CAP truncates the alert list, so without an ordering that
    puts money first a locked cover can be cut off by unpriced gap rows."""
    i = APP.index("def _consistency_alert_rows")
    body = APP[i:APP.index("\n@app.get", i)]
    assert '"basis": flag_rank.basis_of(f)' in body
    assert "flag_rank.sort_flags(out)" in body


@pytest.mark.parametrize("page,name", [(PAGE, "anomalies.html"),
                                       (MIRROR, "new_inconsistencies.html")])
def test_both_tabs_render_the_basis_and_the_ev(page, name):
    assert "BASIS_CLASS" in page, f"{name} does not render the basis badge"
    assert "f.ev_pct" in page, f"{name} does not render ev_pct"


@pytest.mark.parametrize("page,name", [(PAGE, "anomalies.html"),
                                       (MIRROR, "new_inconsistencies.html")])
def test_neither_tab_resorts_the_server_order(page, name):
    i = page.index("function renderConsistency")
    body = page[i:page.index("\nfunction ", i + 10)]
    assert ".sort(" not in body, (
        f"{name} re-sorts the consistency rows client-side, which would undo "
        f"the server's ranking"
    )


@pytest.mark.parametrize("page,name", [(PAGE, "anomalies.html"),
                                       (MIRROR, "new_inconsistencies.html")])
def test_the_consistency_colspans_span_the_table(page, name):
    i = page.index('<table id="consistency">')
    head = page[i:page.index("</thead>", i)]
    cols = len(re.findall(r"<th[ >]", head))
    body = page[page.index('<tbody id="cons-body">'):]
    for span in re.findall(r'colspan="(\d+)"', body[:4000]):
        assert int(span) >= cols, (
            f"{name}: an empty-state row spans {span} of {cols} columns"
        )
