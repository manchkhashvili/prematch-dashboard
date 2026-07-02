"""
Model-free soccer algebra checks — family-A confidence, no devig, no model.

These run on RAW posted odds and exploit relationships that hold by the axioms
of probability regardless of the book's true numbers:

1. PARTITION INEQUALITY. If a whole outcome is the disjoint union of some parts
   (HT/FT column → the FT result; HT/FT row → the HT result; double chance → its
   two 1X2 legs; totals → exact-goal cells; correct-score cells → 1X2 / totals),
   then on RAW implied probabilities `sum(parts) >= whole` normally holds with
   room to spare, because each part carries its own slice of vig. A book whose
   parts sum to LESS than the whole (beyond epsilon) has priced at least one part
   too generously — the +EV direction, model-free.

2. EXACT EQUIVALENCES. Some markets are literally the same bet: AH(0) == DNB,
   AH(-0.5) == 1X2 home, AH(+0.5) == double-chance 1X, and a quarter line == the
   average of its two neighbouring half-lines. A price gap beyond epsilon is a
   flag; opposing sides that cross (1/a + 1/b < 1) are an intra-book arbitrage.

Everything is keyed with the same scheme as src.soccer_model's price sheet
(`ft_1x2:1`, `htft:2/2`, `ah:home_-0.5`, `exact:2`, …) so a posted-odds dict and
the model sheet are directly comparable. Per-book extraction into that scheme is
a separate (integration) concern; this module is pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EPSILON = 0.005          # raw-prob tolerance; below this is odds-step / rounding noise

_LAB = ("1", "X", "2")


@dataclass(frozen=True)
class IdentityFlag:
    kind: str                    # "partition" | "equivalence" | "arb"
    name: str                    # human label
    detail: str
    severity: float              # percentage points (bigger = worse)
    keys: tuple                  # markets involved
    suspect: Optional[str] = None  # the leg to back (longest-priced / cheaper side)
    tier: str = "identity"       # family-A confidence — model-free & hard


def _ri(posted: dict, key: str) -> Optional[float]:
    """Raw implied probability 1/odds, or None if absent/invalid."""
    o = posted.get(key)
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 1.0 / o if o > 1.0 else None


# ── partition relationships (whole == disjoint union of parts) ─────────────────
def _static_partitions() -> list[tuple[str, str, list[str]]]:
    parts: list[tuple[str, str, list[str]]] = []
    # HT/FT columns → FT result (both directions of point 3 collapse to this)
    for lab in _LAB:
        parts.append((f"HT/FT column → FT {lab}", f"ft_1x2:{lab}",
                      [f"htft:{h}/{lab}" for h in _LAB]))
    # HT/FT rows → HT result
    for lab in _LAB:
        parts.append((f"HT/FT row → HT {lab}", f"ht_1x2:{lab}",
                      [f"htft:{lab}/{f}" for f in _LAB]))
    # double chance → its two 1X2 legs
    parts += [
        ("DC 1X → 1,X", "dc:1X", ["ft_1x2:1", "ft_1x2:X"]),
        ("DC 12 → 1,2", "dc:12", ["ft_1x2:1", "ft_1x2:2"]),
        ("DC X2 → X,2", "dc:X2", ["ft_1x2:X", "ft_1x2:2"]),
    ]
    # totals → exact-goal cells (grid-complete, exact partition)
    for line, upto in ((0.5, 0), (1.5, 1), (2.5, 2), (3.5, 3)):
        parts.append((f"Under {line:g} → exact 0..{upto}", f"ft_total:under_{line:g}",
                      [f"exact:{g}" for g in range(upto + 1)]))
    # multigoal bands → exact goals
    parts += [
        ("Multigoal 0-1 → exact 0,1", "multigoal:0-1", ["exact:0", "exact:1"]),
        ("Multigoal 1-2 → exact 1,2", "multigoal:1-2", ["exact:1", "exact:2"]),
        ("Multigoal 2-3 → exact 2,3", "multigoal:2-3", ["exact:2", "exact:3"]),
    ]
    return parts


def _cs_partitions(posted: dict) -> list[tuple[str, str, list[str]]]:
    """Correct-score partitions — only those grid-complete from the posted cells.
    CS is the 'mother market'; low-total unders and (with explicit any-other
    cells) the 1X2 are exactly attributable."""
    cs = {k for k in posted if k.startswith("cs:")}
    out: list[tuple[str, str, list[str]]] = []
    # Under N.5 == all correct scores with total <= N (small totals live in-grid)
    for line, tmax in ((0.5, 0), (1.5, 1), (2.5, 2)):
        cells = [f"cs:{i}-{j}" for i in range(6) for j in range(6) if i + j <= tmax]
        if all(c in cs for c in cells):
            out.append((f"CS → Under {line:g}", f"ft_total:under_{line:g}", cells))
    # 1X2 from CS, only if the book prices the 'any other' catch-alls (else the
    # 6×6 grid truncates and the partition is biased low → false flags).
    if {"cs:other:1", "cs:other:X", "cs:other:2"} <= cs:
        home = [f"cs:{i}-{j}" for i in range(6) for j in range(6) if i > j] + ["cs:other:1"]
        draw = [f"cs:{i}-{i}" for i in range(6)] + ["cs:other:X"]
        away = [f"cs:{i}-{j}" for i in range(6) for j in range(6) if i < j] + ["cs:other:2"]
        for lab, cells in (("1", home), ("X", draw), ("2", away)):
            if all(c in cs for c in cells):
                out.append((f"CS → FT {lab}", f"ft_1x2:{lab}", cells))
    return out


def _partition_flag(posted, name, whole_key, part_keys, epsilon) -> Optional[IdentityFlag]:
    rw = _ri(posted, whole_key)
    rps = [(k, _ri(posted, k)) for k in part_keys]
    if rw is None or any(v is None for _, v in rps):
        return None
    parts_sum = sum(v for _, v in rps)
    gap = rw - parts_sum                       # > 0 → parts too generous (violation)
    if gap <= epsilon:
        return None
    suspect = min(rps, key=lambda t: t[1])[0]  # longest-priced leg = prime candidate
    return IdentityFlag(
        kind="partition", name=name,
        detail=(f"{name}: parts sum to {parts_sum:.3f} but the whole "
                f"({whole_key} @{posted[whole_key]:g}) implies {rw:.3f} — parts "
                f"{gap*100:.1f}pp too generous; back {suspect} @{posted[suspect]:g}"),
        severity=round(gap * 100, 2),
        keys=(whole_key, *part_keys), suspect=suspect,
    )


# ── exact equivalences ────────────────────────────────────────────────────────
_EQUIVS = [
    ("AH(0) home == DNB home", "ah:home_0", "dnb:1"),
    ("AH(0) away == DNB away", "ah:away_0", "dnb:2"),
    ("AH(-0.5) home == 1X2 home", "ah:home_-0.5", "ft_1x2:1"),   # home -0.5 ⇔ home win
    ("AH(-0.5) away == 1X2 away", "ah:away_-0.5", "ft_1x2:2"),   # away -0.5 ⇔ away win
    ("AH(+0.5) home == DC 1X", "ah:home_0.5", "dc:1X"),          # home +0.5 ⇔ win or draw
    ("AH(+0.5) away == DC X2", "ah:away_0.5", "dc:X2"),          # away +0.5 ⇔ win or draw
]


def _equiv_flag(posted, name, a, b, epsilon) -> Optional[IdentityFlag]:
    ra, rb = _ri(posted, a), _ri(posted, b)
    if ra is None or rb is None:
        return None
    diff = abs(ra - rb)
    if diff <= epsilon:
        return None
    back = a if ra < rb else b                  # lower implied prob = longer odds = back it
    return IdentityFlag(
        kind="equivalence", name=name,
        detail=(f"{name}: {a} @{posted[a]:g} vs {b} @{posted[b]:g} price the same "
                f"bet {diff*100:.1f}pp apart — back {back} @{posted[back]:g}"),
        severity=round(diff * 100, 2), keys=(a, b), suspect=back,
    )


_QUARTER_NEIGHBOURS = [
    ("ah:home_-0.25", "ah:home_0", "ah:home_-0.5"),
    ("ah:home_0.25", "ah:home_0", "ah:home_0.5"),
    ("ah:home_-0.75", "ah:home_-0.5", "ah:home_-1"),
    ("ah:home_0.75", "ah:home_0.5", "ah:home_1"),
]


def _quarter_flag(posted, q, lo, hi, epsilon) -> Optional[IdentityFlag]:
    rq, rlo, rhi = _ri(posted, q), _ri(posted, lo), _ri(posted, hi)
    if rq is None or rlo is None or rhi is None:
        return None
    avg = 0.5 * (rlo + rhi)
    diff = abs(rq - avg)
    if diff <= epsilon:
        return None
    return IdentityFlag(
        kind="equivalence", name=f"quarter line {q}",
        detail=(f"{q} @{posted[q]:g} should equal the average of {lo} @{posted[lo]:g} "
                f"and {hi} @{posted[hi]:g} — off by {diff*100:.1f}pp"),
        severity=round(diff * 100, 2), keys=(q, lo, hi),
        suspect=(q if rq < avg else None),
    )


_ARB_PAIRS = [
    ("AH(0)/DNB", "ah:home_0", "dnb:2"),        # back home AH0 + away DNB
    ("AH(0)/DNB", "ah:away_0", "dnb:1"),
    ("AH(-0.5)/1X2", "ah:home_-0.5", "dc:X2"),  # home -0.5 wins ⇔ home wins; X2 covers rest
]


def _arb_flag(posted, name, a, b) -> Optional[IdentityFlag]:
    oa, ob = posted.get(a), posted.get(b)
    try:
        oa, ob = float(oa), float(ob)
    except (TypeError, ValueError):
        return None
    if oa <= 1.0 or ob <= 1.0:
        return None
    book = 1.0 / oa + 1.0 / ob
    if book >= 1.0:
        return None
    return IdentityFlag(
        kind="arb", name=f"{name} intra-book arb",
        detail=(f"back {a} @{oa:g} + {b} @{ob:g}: booksum {book:.3f} < 1 → "
                f"{(1.0/book - 1.0)*100:.1f}% locked"),
        severity=round((1.0 / book - 1.0) * 100, 2), keys=(a, b),
    )


def identity_flags(posted: dict, *, epsilon: float = EPSILON) -> list[IdentityFlag]:
    """Every model-free algebra violation in one match's posted-odds dict.

    posted: {market_key: decimal_odds} using src.soccer_model's key scheme.
    Sorted by severity (biggest first).
    """
    flags: list[IdentityFlag] = []
    for name, whole, parts in _static_partitions() + _cs_partitions(posted):
        f = _partition_flag(posted, name, whole, parts, epsilon)
        if f:
            flags.append(f)
    for name, a, b in _EQUIVS:
        f = _equiv_flag(posted, name, a, b, epsilon)
        if f:
            flags.append(f)
    for q, lo, hi in _QUARTER_NEIGHBOURS:
        f = _quarter_flag(posted, q, lo, hi, epsilon)
        if f:
            flags.append(f)
    for name, a, b in _ARB_PAIRS:
        f = _arb_flag(posted, name, a, b)
        if f:
            flags.append(f)
    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags
