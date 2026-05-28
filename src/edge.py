"""
+EV and ARB opportunity detection (Mode A: same-line comparison).

For each matched CB↔Pinnacle event we pair each CB Odds row with the closest
Pinnacle row of the same market_type, period, and line (AH/OU within
LINE_MATCH_TOLERANCE half-points). Then two passes:

  +EV pass — devig Pinnacle to get fair probabilities. Edge per side:
      edge_pct = (cb_decimal / pin_fair_decimal - 1) * 100
  ARB pass — using Pinnacle's *vigged* posted prices on the opposing side:
      edge_pct = (1 - (1/cb_decimal + 1/pin_other_decimal)) * 100
    If positive, betting CB side X plus Pinnacle side Y locks in profit
    regardless of outcome.

Emits one Opportunity per qualifying side / kind, both kinds in the same list.

`pin_no_vig` carries the SAME meaning on both kinds: the devigged Pinnacle
fair price for the side the CB row represents. This is what makes the column
visually consistent on the arbs page. For ARB rows the actual partner-leg
vigged price (the one that drives the arb math) lives in `arb_partner_odds`,
so the UI can render it as a separate inline chip.

Kelly stake (quarter-Kelly) is computed only for +EV rows; ARB rows have
their own staking math (proportional to the inverse-odds split) which is
beyond v1's scope, so kelly_stake stays at 0 for ARB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.matcher import MatchedEvent
from src.models import Odds, Opportunity
from src.vig import devig_2way, devig_3way

log = logging.getLogger(__name__)

LINE_MATCH_TOLERANCE = 0.01  # near-exact match (float-safety only) — Phase 3.1.2
# Previously 0.5, which falsely paired CB +1.0 with Pin +1.5 (different
# markets, wrong edge math). Lines like +1 / +1.5 / +2 are distinct bets;
# pairing them produces misleading edges. With 0.01, only float-noise
# differences count as "the same line" — actually-different lines don't
# pair and the row simply shows "no Pin reference" rather than a phantom edge.
BANKROLL = 1000.0             # reference bankroll in local currency
KELLY_FRACTION = 0.25         # quarter-Kelly
MIN_EDGE_PCT = 1.0            # default minimum edge threshold


def compute_opportunities(
    matched: list[MatchedEvent],
    min_edge_pct: float = MIN_EDGE_PCT,
) -> list[Opportunity]:
    """Return all +EV opportunities, sorted by edge% descending."""
    opps: list[Opportunity] = []
    for event in matched:
        opps.extend(_process_event(event, min_edge_pct))
    return sorted(opps, key=lambda o: -o.edge_pct)


def _process_event(event: MatchedEvent, min_edge_pct: float) -> list[Opportunity]:
    opps: list[Opportunity] = []

    for cb in event.cb:
        pin = _find_pin_match(cb, event.pin)
        if pin is None:
            log.debug(
                "no Pin match for %s %s %s line=%s",
                cb.market_type, cb.period, f"{event.home} vs {event.away}", cb.line,
            )
            continue

        market_label = _market_label(cb)
        match_label = f"{event.home} — {event.away}"
        start_time = cb.start_time or datetime.now(tz=timezone.utc)
        cb_event_id = cb.raw_event_id

        # Devigged same-side fair prices for THIS market. Used as the
        # `pin_no_vig` value on both +EV and ARB rows so the column has one
        # consistent meaning across the dashboard.
        pairs = _fair_pairs(cb, pin)
        fair_by_side: dict[str, float] = {}
        if pairs is not None:
            for side, _cb_dec, fair_prob in pairs:
                fair_by_side[side] = 1.0 / fair_prob

        # ── +EV pass ────────────────────────────────────────────────────────
        if pairs is not None:
            for side, cb_dec, fair_prob in pairs:
                if cb_dec is None or cb_dec <= 1.0:
                    continue
                pin_fair_dec = fair_by_side[side]
                edge_pct = (cb_dec / pin_fair_dec - 1.0) * 100.0
                if edge_pct < min_edge_pct:
                    continue
                kelly_f = (fair_prob * cb_dec - 1.0) / (cb_dec - 1.0)
                kelly_stake = max(0.0, kelly_f * KELLY_FRACTION * BANKROLL)
                opps.append(Opportunity(
                    start_time=start_time, match_label=match_label,
                    market=market_label, side=side,
                    cb_odds=cb_dec, pin_no_vig=pin_fair_dec,
                    edge_pct=edge_pct, kind="+EV",
                    kelly_stake=kelly_stake,
                    cb_event_id=cb_event_id,
                    market_type=cb.market_type, period=cb.period, line=cb.line,
                    submarket=cb.submarket, team_side=cb.team_side,
                ))

        # ── ARB pass ────────────────────────────────────────────────────────
        # For each CB side, check whether CB's price + Pinnacle's opposite-side
        # vig'd price lock in a guaranteed profit. The partner-leg vigged
        # price drives the math; we surface it via arb_partner_* so the
        # `pin_no_vig` column stays semantically consistent across kinds.
        # 3-way moneyline (soccer 1X2) returns [] here — no ARB-3 in v1
        # per user 2026-05-26 (vanishingly rare within a single book; the
        # +EV pass above still emits per-side opportunities). When book #2
        # lands we can revisit.
        for cb_side, pin_other_side in _opposing_pairs(cb):
            cb_dec = cb.selections.get(cb_side)
            pin_other_dec = pin.selections.get(pin_other_side)
            if cb_dec is None or pin_other_dec is None:
                continue
            if cb_dec <= 1.0 or pin_other_dec <= 1.0:
                continue
            arb_edge_pct = (1.0 - (1.0 / cb_dec + 1.0 / pin_other_dec)) * 100.0
            if arb_edge_pct < min_edge_pct:
                continue
            # pin_no_vig = same-side fair price (consistent with +EV rows).
            # Falls back to the vigged same-side price if devig failed
            # (e.g. one Pinnacle side missing — shouldn't happen given the
            # filtering above, but stay defensive).
            same_side_fair = fair_by_side.get(cb_side) or pin.selections.get(cb_side, 0.0)
            opps.append(Opportunity(
                start_time=start_time, match_label=match_label,
                market=market_label, side=cb_side,
                cb_odds=cb_dec,
                pin_no_vig=same_side_fair,
                edge_pct=arb_edge_pct, kind="ARB",
                kelly_stake=0.0,
                cb_event_id=cb_event_id,
                arb_partner_side=pin_other_side,
                arb_partner_odds=pin_other_dec,
                market_type=cb.market_type, period=cb.period, line=cb.line,
                submarket=cb.submarket, team_side=cb.team_side,
            ))

    return opps


def _opposing_pairs(cb: Odds) -> list[tuple[str, str]]:
    """
    For ARB, return [(cb_side, pin_other_side), ...] for this CB market.

    Takes the full CB Odds (not just market_type) so we can detect 3-way
    moneyline via `"draw" in cb.selections` — soccer 1X2 has no 2-way
    "opposite" pair and we skip ARB for it in v1.

      - 2-way moneyline / spread: home↔away
      - total / team_total      : over↔under
      - 3-way moneyline (1X2)   : [] (no single-book ARB-3 in v1)
    """
    if cb.market_type == "moneyline":
        if "draw" in cb.selections:
            # 3-way ML — soccer 1X2. ARB-3 within one book needs
            # 1/d_home + 1/d_draw + 1/d_away < 1, which is vanishingly
            # rare on Pinnacle's tight pricing. Skip in v1; revisit when
            # book #2 lands.
            return []
        return [("home", "away"), ("away", "home")]
    if cb.market_type == "spread":
        return [("home", "away"), ("away", "home")]
    if cb.market_type in ("total", "team_total"):
        return [("over", "under"), ("under", "over")]
    return []


def _market_label(cb: Odds) -> str:
    """
    Human-readable market label for the arbs table.

    Examples:
      basketball spread H1 -2.5      → "Spread H1 -2.5"
      basketball moneyline FT        → "Moneyline FT"
      soccer 3-way moneyline FT      → "Moneyline FT"
      soccer team_total FT 1.5 home  → "Team Total FT +1.5 (home)"
      soccer corners total FT 9.5    → "Total FT +9.5 (corners)"
      soccer corners spread H1 -0.5  → "Spread H1 -0.5 (corners)"
    """
    line_str = f" {cb.line:+g}" if cb.line is not None else ""
    name = cb.market_type.replace("_", " ").title()
    parts = [f"{name} {cb.period}{line_str}"]
    if cb.team_side:
        parts.append(f"({cb.team_side})")
    if cb.submarket:
        parts.append(f"({cb.submarket})")
    return " ".join(parts)


def _fair_pairs(
    cb: Odds, pin: Odds
) -> list[tuple[str, float | None, float]] | None:
    """
    Return [(side, cb_decimal, fair_prob), ...] for all sides in this market.
    Returns None if de-vigging fails.

    Soccer additions:
      - 3-way moneyline: detected via `"draw" in pin.selections`. Uses
        `devig_3way` to fair-price home/draw/away.
      - team_total: same over/under shape as total — handled by the else
        branch below since both market types have {over, under} selections.
    """
    ps = pin.selections
    try:
        if cb.market_type == "moneyline":
            if "draw" in ps:
                # 3-way soccer 1X2
                fair_home, fair_draw, fair_away = devig_3way(
                    ps["home"], ps["draw"], ps["away"],
                )
                return [
                    ("home", cb.selections.get("home"), fair_home),
                    ("draw", cb.selections.get("draw"), fair_draw),
                    ("away", cb.selections.get("away"), fair_away),
                ]
            fair_home, fair_away = devig_2way(ps["home"], ps["away"])
            return [
                ("home", cb.selections.get("home"), fair_home),
                ("away", cb.selections.get("away"), fair_away),
            ]
        if cb.market_type == "spread":
            fair_home, fair_away = devig_2way(ps["home"], ps["away"])
            return [
                ("home", cb.selections.get("home"), fair_home),
                ("away", cb.selections.get("away"), fair_away),
            ]
        # total OR team_total — both ship {over, under}
        fair_over, fair_under = devig_2way(ps["over"], ps["under"])
        return [
            ("over",  cb.selections.get("over"),  fair_over),
            ("under", cb.selections.get("under"), fair_under),
        ]
    except (KeyError, ValueError) as exc:
        log.debug("devig failed for %s %s: %s", cb.market_type, cb.period, exc)
        return None


def _find_pin_match(cb: Odds, pin_list: list[Odds]) -> Odds | None:
    """
    Find the Pinnacle Odds row matching a CB row by type, period, line,
    submarket, AND team_side. For AH/OU/team_total, picks the closest line
    within LINE_MATCH_TOLERANCE.

    Phase 2 (soccer): submarket and team_side are part of the join key so:
      - corners-total-9.5 doesn't accidentally match goals-total-9.5
      - home-team-total-1.5 doesn't accidentally match away-team-total-1.5

    Basketball Odds have submarket=None and team_side=None on both sides,
    so the new equality checks are no-ops for Phase 1 data (None == None).
    """
    candidates = [
        p for p in pin_list
        if p.market_type == cb.market_type
        and p.period == cb.period
        and p.submarket == cb.submarket
        and p.team_side == cb.team_side
        and _lines_match(cb.line, p.line, cb.market_type)
    ]
    if not candidates:
        return None
    if cb.line is not None and len(candidates) > 1:
        candidates.sort(key=lambda p: abs((p.line or 0.0) - cb.line))
    return candidates[0]


def _lines_match(cb_line, pin_line, market_type: str) -> bool:
    if market_type == "moneyline":
        return True
    if cb_line is None or pin_line is None:
        return False
    return abs(cb_line - pin_line) <= LINE_MATCH_TOLERANCE
