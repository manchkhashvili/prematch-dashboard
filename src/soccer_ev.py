"""
Soccer EV flagging off the calibrated goal model — own-book anchor.

Anchor = the BOOK'S OWN 1X2. Fit lambdas to the book's devigged 1X2, price the
full fair sheet (src.soccer_model), then flag every posted derivative whose price
is LONGER than model fair (positive EV). Because a posted derivative carries its
own vig, positive EV means the line is genuinely generous versus what the book's
own headline 1X2 implies — a soft spot. Model-free identity checks
(src.soccer_identities) and the ladder curve residuals (src.soccer_curves) ride
along on whatever markets are present.

Own-book caveat: the model can't know the book's 1X2 is itself correct, so these
EV flags are an INTERNAL-CONSISTENCY signal (generous derivative vs the book's own
headline), not a sharp-anchored edge. Pure & testable; soft_scan wraps the hits
into Anomalies-tab rows. Thresholds are env-tunable (SOCCER_EV_MIN); set it to 0
to surface literally everything above fair.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from src import soccer_model as sm
from src import soccer_curves as scv
from src.soccer_identities import identity_flags, IdentityFlag

EV_MIN = float(os.environ.get("SOCCER_EV_MIN", "0.03"))       # generic derivative floor
TAIL_EV_MIN = float(os.environ.get("SOCCER_EV_TAIL_MIN", "0.08"))  # tails: model error grows
# Anchor devig. Default "power" — it matches the owner's hand-validated edges and
# surfaces more (the whole intent: everything above fair pops up). "shin" is the
# conservative alternative; they differ materially at extreme prices (fair 2/2
# ~1.67 power vs ~1.76 shin at a 1.18 fav → the Nepean 2/2 reads +7.8% vs +2.3%).
# "auto" = shin for 3-way, power for 2-way. Override with SOCCER_DEVIG.
DEVIG_METHOD = os.environ.get("SOCCER_DEVIG", "power")
FAIR_ODDS_CAP = 15.0     # ignore outcomes the model prices longer than this (tail blow-up)
POSTED_MIN = 1.10        # nothing shorter is worth flagging
ROBUST_SPLITS = (0.42, 0.48)   # recompute EV at these H1 splits → robustness hint


@dataclass(frozen=True)
class EvHit:
    key: str
    posted: float
    fair_odds: float
    fair_prob: float
    ev: float
    robust: bool         # EV survives at the alternate H1 splits too


@dataclass
class SoccerAnalysis:
    model: Optional[sm.SoccerModel]
    ev_hits: list = field(default_factory=list)
    identities: list = field(default_factory=list)
    curves: list = field(default_factory=list)


def _is_tail(key: str, fair_odds: float) -> bool:
    return fair_odds > 8.0 or key.startswith("cs:")


def _anchor_excludes(total_line: Optional[float]) -> set:
    ex = {f"ft_1x2:{s}" for s in ("1", "X", "2")}
    if total_line is not None:
        ex |= {f"ft_total:over_{total_line:g}", f"ft_total:under_{total_line:g}"}
    return ex


def analyze(
    posted: dict, *,
    ah_raw: Optional[dict] = None, totals_raw: Optional[dict] = None,
    total_line: Optional[float] = None, ou_odds=None,
    devig_method: str = DEVIG_METHOD, split: float = sm.DEFAULT_SPLIT,
    league: Optional[str] = None,
    ev_min: float = EV_MIN, tail_ev_min: float = TAIL_EV_MIN,
) -> SoccerAnalysis:
    """Fit the model to the posted 1X2, then EV-flag every generous derivative,
    plus model-free identities and ladder curve residuals."""
    ids = identity_flags(posted)
    o1x2 = [posted.get("ft_1x2:1"), posted.get("ft_1x2:X"), posted.get("ft_1x2:2")]
    if not all(isinstance(o, (int, float)) and o > 1.0 for o in o1x2):
        # no anchor → still return the model-free identities
        return SoccerAnalysis(None, [], ids, _curve_flags(None, ah_raw, totals_raw))

    model = sm.model_from_market(
        o1x2, total_line=total_line, odds_over_under=ou_odds,
        devig_method=devig_method, split=split, league=league)
    import math
    if not (math.isfinite(model.lh) and math.isfinite(model.la)
            and model.lh > 0 and model.la > 0):
        return SoccerAnalysis(None, [], ids, _curve_flags(None, ah_raw, totals_raw))
    alts = [sm.build_model(model.lh, model.la, split=s) for s in ROBUST_SPLITS]
    excl = _anchor_excludes(total_line)

    hits: list[EvHit] = []
    for key, po in posted.items():
        if key in excl:
            continue
        try:
            po = float(po)
        except (TypeError, ValueError):
            continue
        if po < POSTED_MIN:
            continue
        fe = model.prices.get(key)
        if not fe or fe["prob"] <= 0 or fe["odds"] > FAIR_ODDS_CAP:
            continue
        ev = po * fe["prob"] - 1.0
        thr = tail_ev_min if _is_tail(key, fe["odds"]) else ev_min
        if ev < thr:
            continue
        robust = all(po * a.prices[key]["prob"] - 1.0 >= thr
                     for a in alts if key in a.prices)
        hits.append(EvHit(key, po, fe["odds"], fe["prob"], round(ev, 4), robust))
    hits.sort(key=lambda h: h.ev, reverse=True)

    return SoccerAnalysis(model, hits, ids, _curve_flags(model, ah_raw, totals_raw))


def _curve_flags(model, ah_raw, totals_raw) -> list:
    out: list = []
    if totals_raw:
        out += scv.total_curve_flags(totals_raw)
    if ah_raw:
        out += scv.ah_curve_flags(ah_raw)
    if model is not None and ah_raw and totals_raw:
        cf = scv.cross_fit_divergence(model.lh - model.la, model.lh + model.la,
                                      ah_raw, totals_raw)
        if cf:
            out.append(cf)
    return out


# ── human-readable market labels ──────────────────────────────────────────────
_SIDE = {"1": "home", "X": "draw", "2": "away"}


def pretty_key(key: str) -> str:
    fam, _, rest = key.partition(":")
    if fam == "htft":
        return f"HT/FT {rest}"
    if fam == "ft_1x2":
        return f"FT {_SIDE.get(rest, rest)}"
    if fam == "ht_1x2":
        return f"HT {_SIDE.get(rest, rest)}"
    if fam == "h2_1x2":
        return f"2H {_SIDE.get(rest, rest)}"
    if fam == "dc":
        return f"double chance {rest}"
    if fam == "dnb":
        return f"DNB {_SIDE.get(rest, rest)}"
    if fam in ("ft_total", "h1_total", "h2_total"):
        side, _, line = rest.partition("_")
        tag = {"ft_total": "", "h1_total": " (1H)", "h2_total": " (2H)"}[fam]
        return f"{side} {line}{tag}"
    if fam.startswith("tt_"):
        who = "home" if "home" in fam else "away"
        side, _, line = rest.partition("_")
        return f"{who} team {side} {line}"
    if fam == "ah":
        who, _, line = rest.partition("_")
        return f"AH {who} {line}"
    if fam == "btts":
        return f"BTTS {rest}"
    if fam == "cs":
        return f"correct score {rest}"
    if fam == "multigoal":
        return f"multigoals {rest}"
    if fam == "res_tot":
        r, _, t = rest.partition("&")
        return f"{_SIDE.get(r, r)} & {t}"
    if fam == "wtn":
        return f"win to nil {rest}"
    if fam == "cleansheet":
        return f"clean sheet {rest}"
    return key
