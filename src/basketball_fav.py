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

FAV_GAP_PP = 10.0    # flag when the home-win-prob spread across markets ≥ this
                     # (measured: the 3-way often disagrees with the 2-way by ~12pp)


def _p_home(home: Optional[float], away: Optional[float]) -> Optional[float]:
    """Devigged P(home beats away), draw ignored (two-way renormalise)."""
    if not home or not away or home <= 1.0 or away <= 1.0:
        return None
    ih, ia = 1.0 / home, 1.0 / away
    return ih / (ih + ia)


def htft_winner(htft: Optional[dict]) -> Optional[tuple]:
    """(home, away) FT-winner odds implied by a 9-way HT/FT combo: home wins FT =
    */1 (1/1, X/1, 2/1); away = */2. Lets the HT/FT position join the comparison."""
    if not htft:
        return None
    ph = sum(1.0 / htft[k] for k in ("1/1", "X/1", "2/1") if htft.get(k))
    pa = sum(1.0 / htft[k] for k in ("1/2", "X/2", "2/2") if htft.get(k))
    return (1.0 / ph, 1.0 / pa) if ph and pa else None


def fav_disagreement(
    ml2: Optional[tuple] = None,   # (home, away) — 2-way incl-OT winner
    ml3: Optional[tuple] = None,   # (home, away) — 3-way regulation result (drop draw)
    ht: Optional[tuple] = None,    # (home, away) — HT result (drop draw)
    htft: Optional[tuple] = None,  # (home, away) — implied by the HT/FT combo
    *, min_gap_pp: float = FAV_GAP_PP,
) -> Optional[dict]:
    """Return a flag dict if the favourite flips or the home-win-prob spread is
    >= min_gap_pp across the provided markets, else None. Needs >= 2 markets."""
    ps: dict[str, float] = {}
    for name, pair in (("ml2", ml2), ("ml3", ml3), ("ht", ht), ("htft", htft)):
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
