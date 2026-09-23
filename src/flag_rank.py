"""One ranking currency for the Anomalies tab: money, and how sure it is.

THE BUG THIS FIXES
------------------
Every consistency check reports a `severity`, and the tab sorted 33 checks by
it with a single `cons.sort(key=severity)`. But severity is not one quantity.
`static/anomalies.html` has said so for months — its `KIND_UNIT` table maps
each kind to "pp", "pts" or "%" — and the sort ignored that table completely.
So the board compared a probability gap in percentage points against a locked
return on outlay against a spread in goals, on one axis, as if they were the
same number.

Measured on the live board 2026-09-23 (60 flags), top of the tab was:

    13.62  htft_fair        "+14% over model fair"     model EV, not locked
    12.78  htft_combo       "12.78% too generous"      model EV, not locked
     9.50  combo_dominance  "the superset pays 9.5% more"   NOT money at all
     8.50  combo_duplicate  "+8.5% EV vs model fair"   model EV
     7.99  ml_vs_spread     "gap 8pp"                  NOT money, names no leg

and every arithmetic-certain row sat below them. Owner: "they were hiding in
the bottom when its locked ... so I think everythings should be calculated by
+ev instead of some locked and beeing on bottom."

WHAT REPLACES IT
----------------
Each kind is classified by what its severity actually is — verified by reading
the code that builds the flag, not by its name — into one of four bases:

  "fault"   The book's board is broken (one match listed twice). Severity is a
            flat 100 by design: this always alerts rather than competing on
            size, and the real numbers are in the detail. Keeps its pin above
            everything, which is what the flat 100 already did.
  "locked"  The profit follows by ARITHMETIC from prices the book is showing
            right now. No model. `severity` IS the return on outlay, in %.
  "ev"      One named leg, at a known price, against a reference fair price.
            `severity` IS the expected value of backing that leg, in %, and it
            is only as true as the model that produced the fair.
  "gap"     A contradiction with no money attached: two markets disagree (pp),
            a parent total misses its children (pts), or one price is longer
            than another it dominates (a free upgrade, not a standalone bet).
            These get NO ev_pct. A 22pp gap is not 22% of anything.

Sort: fault, then locked, then ev, then gap; by EV inside the paying tiers and
by severity inside the others. A locked +2% now outranks a model +13%, which
is this project's own repeated lesson — the measured zeros and the artifacts
have all been on the model-relative side of the line, and the money that
actually paid has been arithmetic on two prices on one screen.

SPLIT KINDS
-----------
Two kinds emit two different quantities under one name, so the table cannot be
the last word and emitters may override by setting `basis` on the flag:

  * `combo_dominance` — the totals branch gates on model fair and reports EV
    (src/lider_combos.py); the HT/FT branch has no model and reports `gain`,
    the raw "superset pays X% more". Only the first is money.
  * `htft_fair` — the EDGE branch reports `posted x p_fair - 1`, an EV; the
    SHAPE branch reports |ratio - 1| between two probabilities. Only the first
    is money.

Both were ranking as if they were EV. The HT/FT `combo_dominance` at 9.50 in
the census above is the clearest case: it means "this parlay pays 9.5% more
than a smaller parlay inside it", on a bet that can still be EV -40%.
"""
from __future__ import annotations

# kind -> basis. EVERY kind the tab can render must appear here, or
# tests/test_flag_rank.py fails: an unclassified check would silently fall into
# the unpriced tier and its money would stop being ranked as money.
#
# The comment on each line is the expression the emitter passes as `severity`,
# so this table can be re-verified against the source without re-deriving it.
BASIS: dict[str, str] = {
    # ── the board is broken ──────────────────────────────────────────────────
    "duplicate_fixture":    "fault",   # flat 100.0 (consistency.py)
    "duplicate_live":       "fault",   # flat 100.0 (live_duplicates.py)

    # ── arithmetic on displayed prices; no model ─────────────────────────────
    "pickem_arb":           "locked",  # (1/outlay - 1)*100, push-aware
    "pickem_duplicate":     "locked",  # (1/cost - 1)*100 across two quotes
    "combo_cover":          "locked",  # (1/outlay - 1)*100 from min_cover
    "vb_sets_cover":        "locked",  # (1/cost - 1)*100 over a set partition

    # ── one leg, one price, one fair ─────────────────────────────────────────
    "soccer_fair":          "ev",      # h.ev*100 vs the book's own 1X2
    "combo_fair":           "ev",      # (p*v - 1)*100 vs the atom model
    "combo_duplicate":      "ev",      # (p*hi - 1)*100, long side vs fair
    "combo_dominance":      "ev",      # (p_sup*ob - 1)*100 — TOTALS branch only;
                                       # the HT/FT branch overrides to "gap"
    "vb_sets_duplicate":    "ev",      # edge*100 vs fair from the match price
    "vb_sets_dominance":    "ev",      # edge*100, same
    "tennis_correct_score": "ev",      # (price*fair - 1)*100
    "vb_correct_score":     "ev",      # (price*fair - 1)*100
    "htft_fair":            "ev",      # (cb/fair_odds - 1)*100 — EDGE branch only;
                                       # the SHAPE branch overrides to "gap"
    "htft_combo":           "ev",      # (c - fair)/fair*100 vs correlation-fair

    # ── a contradiction, with no price on it ─────────────────────────────────
    "ml_vs_spread":         "gap",     # |P_ml - P_pickem| in pp
    "favourite_flip":       "gap",     # (|ft_edge| + |edge|)*100 in pp
    "quarter_ml_extreme":   "gap",     # q_dev - ft_dev in pp
    "total_additivity":     "gap",     # |sum(children) - parent| in POINTS
    "fts_vs_ml":            "gap",     # pp between FTS and the 1X2
    "betlive_flip":         "gap",     # a.gap_pp
    "betlive_ot_fold":      "gap",     # a.gap_pp
    "basketball_fav":       "gap",     # res["gap_pp"]
    "soccer_identity":      "gap",     # idf.severity, pp
    "soccer_curve":         "gap",     # cf.severity, pp off the curve
    "soccer_htft":          "gap",     # (ratio - 1)*100 — combo vs its OWN leg,
                                       # a relative price, not a fair
    "tennis_set_match":     "gap",     # pp
    "vb_set_match":         "gap",     # pp
    "half_result_vs_ft":    "gap",     # pp
    "ot_vs_regulation":     "gap",     # pp outside the overtime box
    "ot_monotone":          "gap",     # pp the regulation ladder sits above incl-OT
    # A free upgrade, not a bet: the voider at a LONGER price than the 1X2 it
    # strictly beats. Take it instead of the 1X2 — but only if you wanted the
    # 1X2, and it can still be a bad price. `excess` is the size of the
    # impossibility, not the size of a profit.
    "pickem_dominance":     "gap",     # (ratio - 1)*100 over the 1X2
}

# Lower sorts first. The gap between "ev" and "gap" is the whole point: no pp
# number, however large, outranks a row with money on it.
TIER: dict[str, int] = {"fault": 0, "locked": 1, "ev": 2, "gap": 3}

# The two bases whose severity is an expected value in % of stake.
PAYS = ("locked", "ev")


def basis_of(flag) -> str:
    """The basis for one flag — the emitter's override, else the kind table.

    Unknown kinds fall to "gap" rather than raising: a detector that ships
    without a table entry must not take the tab down, and the guard test is
    what keeps it from going unnoticed.
    """
    over = flag.get("basis") if isinstance(flag, dict) else getattr(flag, "basis", None)
    if over in TIER:
        return over
    kind = flag.get("kind") if isinstance(flag, dict) else getattr(flag, "kind", None)
    return BASIS.get(kind, "gap")


def annotate(flag: dict) -> dict:
    """Set `basis` and `ev_pct` on one flag row, in place, and return it.

    `ev_pct` is the row's severity where severity IS an expected value, and
    None everywhere else. It is deliberately NOT derived for the gap tier: a
    probability gap could be turned into an EV by multiplying by a price, but
    only after choosing which of the two disagreeing markets is the wrong one,
    and that choice is a model. Inventing the number here would put the
    project's least trustworthy rows back at the top of the board wearing a
    money sign.
    """
    b = basis_of(flag)
    flag["basis"] = b
    sev = flag.get("severity")
    flag["ev_pct"] = float(sev) if (b in PAYS and sev is not None) else None
    return flag


def sort_key(flag: dict) -> tuple:
    """Rank one row. Sorted ASCENDING — tier first, then size descending."""
    b = flag.get("basis") or basis_of(flag)
    ev = flag.get("ev_pct")
    if ev is None and b in PAYS:
        ev = flag.get("severity")
    size = ev if ev is not None else (flag.get("severity") or 0.0)
    return (TIER.get(b, 3), -float(size))


def sort_flags(flags: list[dict]) -> list[dict]:
    """Annotate every row and return them in board order."""
    for f in flags:
        annotate(f)
    return sorted(flags, key=sort_key)
