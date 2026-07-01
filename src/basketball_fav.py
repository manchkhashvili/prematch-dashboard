"""
Basketball favourite disagreement — book-agnostic.

The 2-way (incl-OT winner), 3-way (regulation result) and HT (half-time result)
moneylines must all agree on WHO is favoured: the stronger team is more likely
in every one of them. So the devigged home-vs-away win probability (draw
dropped) should be close across the three. When it FLIPS (favourite changes
side) or spreads a lot, one of the markets is mispriced.

This generalises the betlive OT-fold check (2-way vs 3-way) to also include the
HT moneyline, and applies to all three books (CrystalBet, Betlive, Lider-Bet).
Feed it the two win prices per market; the caller drops the draw and extracts
per book.
"""
from __future__ import annotations

from typing import Optional

FAV_GAP_PP = 15.0    # flag when the home-win-prob spread across markets ≥ this


def _p_home(home: Optional[float], away: Optional[float]) -> Optional[float]:
    """Devigged P(home beats away), draw ignored (two-way renormalise)."""
    if not home or not away or home <= 1.0 or away <= 1.0:
        return None
    ih, ia = 1.0 / home, 1.0 / away
    return ih / (ih + ia)


def fav_disagreement(
    ml2: Optional[tuple] = None,   # (home, away) — 2-way incl-OT winner
    ml3: Optional[tuple] = None,   # (home, away) — 3-way regulation result (drop draw)
    ht: Optional[tuple] = None,    # (home, away) — HT result (drop draw)
    *, min_gap_pp: float = FAV_GAP_PP,
) -> Optional[dict]:
    """Return a flag dict if the favourite flips or the home-win-prob spread is
    >= min_gap_pp across the provided markets, else None. Needs >= 2 markets."""
    ps: dict[str, float] = {}
    for name, pair in (("ml2", ml2), ("ml3", ml3), ("ht", ht)):
        if pair:
            p = _p_home(pair[0], pair[1])
            if p is not None:
                ps[name] = p
    if len(ps) < 2:
        return None
    lo, hi = min(ps.values()), max(ps.values())
    flip = lo < 0.5 < hi                 # favourite changes side between markets
    gap = (hi - lo) * 100.0
    if flip or gap >= min_gap_pp:
        return {
            "probs": {k: round(v, 3) for k, v in ps.items()},
            "gap_pp": round(gap, 1),
            "flip": flip,
        }
    return None
