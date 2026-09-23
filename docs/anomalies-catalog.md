# Anomalies catalog — everything we flag

A single reference for every anomaly detector in the dashboard: what each one
flags, the math behind it, its thresholds, the book/sport it covers, whether
it's *bettable* or merely *diagnostic*, and open research questions. Written to
be re-read months from now. Terse; cites the code that owns each check.

Legend:
- **Bettable** = a posted price we think is beatable (an EV claim).
- **Diagnostic** = "this game's pricing contradicts itself → go look" (no EV claim).
- `kind` = the string shown in the Anomalies tab (`static/anomalies.html` `KIND_LABEL`).

There are **four families**. A–B run off CrystalBet only; C is Betlive-only; D
sweeps all three books.

---

## Family A — CB ladder monotonicity (`src/anomalies.py`) · BETTABLE

The oldest detector. Structural, no model, no reference book.

- **Idea:** as a **home handicap line rises** (home gets more points) the home
  side is more likely to cover, so its **odds must get shorter**, never longer;
  the away side mirrors. Totals: as the **line rises**, OVER odds must be
  non-decreasing, UNDER non-increasing.
- **Flag:** any adjacent-rung pair on one `(event, period, market_type,
  submarket)` ladder that moves the wrong way. Compared only to the *next
  present* rung, so gaps in CB's line sequence don't create false positives.
- **Direction map** (`_DIRECTION`): spread/home→down, spread/away→up,
  total/over→up, total/under→down.
- **Scope:** CrystalBet, any sport with spread/total ladders. `kind` surfaced
  via the CB ladder scan (`ANOMALY_SCAN=1`).
- **Why bettable:** a ladder that crosses itself means one of the two rungs is
  literally mispriced — you take the better side. Highest-confidence family we
  have.
- **Research:** the size of the crossing (`.delta` / `.pct`) is the edge proxy;
  worth logging realised outcomes to see how often a crossing is a stale rung vs
  a genuine soft price.

---

## Family B — book-internal consistency (`src/consistency.py`) · DIAGNOSTIC (one sub-check bettable)

> **Not CB-only any more (2026-08-13).** The engine always ran on Lider-Bet,
> Crocobet and Setanta (`_LADDER_BOOKS`), but those books were emitting FT
> headline markets only — so nothing could fire. Their half markets and 9-cell
> HT/FT grids were already in payloads we were fetching and parsing, and were
> being dropped at the classifier. Now mapped and price-verified. `htft_combo`
> evaluates on all four books and **Setanta fires at threshold**; measured
> looseness of each book's HT/FT grid against its own 1X2 legs runs
> CrystalBet (median 2.29 %, max 15.4 %) > Setanta (1.55 / 4.8) >
> Crocobet (0.76 / 1.6) > Lider-Bet (0.00 / 0.00).

Contradictions between CB's **own** markets for the same game — across market
types (ML vs handicap) and across periods (halves/quarters vs full time).
Everything derivable from CB alone. `CONSISTENCY_SPORTS = (basketball, soccer,
tennis, americanfootball)`.
Thresholds deliberately sit **well above** normal period-to-period variation
(calibrated on a clean NBA game 2026-05-31: ML-vs-spread agreed <0.5pp, period
totals summed within 0.5pt).

### B1. `ml_vs_spread` — ML win-prob vs handicap-ladder win-prob
- Within one period, `|P_home(ML) − P_home(spread @ line 0)|`. Flag ≥ **5.0pp**
  (`ML_SPREAD_GAP_PP`). P(win) read **only** from a true pick'em (line 0.0)
  rung — no extrapolation (extrapolating to 0 outside a favourite's ladder
  fabricated fake gaps; interpolating across ±0.5 mixes tie conventions).
- **Dead for four months, revived by American football (2026-08-12).** This
  check had never fired once in 4606 historical flags, and the reason was
  structural, not threshold-related: it needs a 2-way ML *and* a line-0 rung in
  the same period, and neither sport had both. Soccer has the handicap side but
  no 2-way ML (its 1X2 is 3-way and Draw No Bet is parsed then `(SKIP)`ped);
  basketball has the ML but CB never posts a pick'em rung on it. AF has both —
  measured on the whole live board, **169 of 177 events carry a 2-way ML and 71
  carry a 0.0 spread rung** — so the check runs on 71 events and produced its
  first real flag.
- **Demoted to a diagnostic 2026-08-29 (`ALERT_DEFAULT_OFF`).** It names no bet
  by construction — it says the 1X2 and the pick'em rung disagree, not which is
  wrong or how to take it. Conversion measured over the whole collected board,
  2 147 events with 1 709 pricing both markets: **2 flags, 0 locks.** Still
  listed, still on the tab; it no longer chimes unless switched on, because its
  bettable siblings (B10/B11) fire on the same games.
- Its soccer coverage was also a lie until 2026-08-29: `Draw no bet` was
  hard-skipped and the 0.0 rung was missing on 196 events (see B10). With those
  closed it went from 2 flags to 15, and 3 of 11 on the live board had a
  bettable sibling — a **27 %** conversion, not 0.

### B10. `pickem_duplicate` — the same bet quoted twice · **BETTABLE, no losing branch**
A Draw No Bet and an Asian Handicap **0.0** rung are the *same bet*: both void
on the draw and settle on "who wins, given it is not drawn". A book quoting
both is quoting one market at two prices, and the better side of each covers
the game between them. When that costs under 1.0 it is locked with **no losing
branch at all** — the draw voids both legs and returns the stake.

Found by the owner on the live board (Resovia Rzeszów v KKP Bydgoszcz W,
2026-08-29):

```
Draw no bet         1 @ 1.55   2 @ 2.10   ->  P(home | no draw) 57.5 %
Asian Handicap 0.0  1 @ 1.20   2 @ 3.50   ->  P(home | no draw) 74.5 %

best of each: 1/1.55 + 1/3.50 = 0.9309  ->  +7.4 % locked
```

**Calibration.** 1 414 events price both, and they normally agree to a median
**0.1pp** (p90 0.4) — as two quotes of one bet should. Ten sat 5pp+ apart, one
10pp+, and exactly one locked, at +0.40 %. That rate is a **floor, not the
rate**: the sample is built from each event's LAST prices, which skew late and
settled, and cannot contain the case above — a live mid-morning board in an
obscure women's league, which is where a book's automation is loosest.

**Two holes had to be closed before it could fire at all**, and neither was an
anomaly bug:
1. `^draw no bet` was hard-skipped for soccer, correctly for the +EV pipeline
   (Pinnacle ships DNB as an unstructured `type=special`, unmatchable) and
   wrongly inherited here — the consistency engine never touches Pinnacle.
2. CrystalBet ships the same Asian ladder under **two titles**: `Handicap` on
   1 666 events and `Asian Handicap` on **196**, and only the first was
   classified. That was 10.5 % of the soccer board with no handicap coverage
   at all — in the anomaly scan *and* the Pinnacle matching, which never saw a
   spread row for those events. Closing it recovered 924 rows.

### B11. `pickem_dominance` — the better bet at the longer price · **BETTABLE**
Every market that **voids** the draw — the 0.0 rung and the Draw No Bet alike —
beats the 1X2 on the same side: same win condition, and one hands the stake
back where the other keeps it. So its price must be strictly **shorter**, and
on fair prices the ratio is exact:

```
voider(side) / 1X2(side)  ==  1 − P(draw)
```

A ratio at or above 1.0 is the book offering the better bet at the longer
price. There is nothing to model and nothing to devig — it compares **raw
prices you can take**, which makes it the only check in this file immune to the
devig choice everything else depends on.

**Calibration.**

| pair | events / sides | ratio p10 · median · p90 · max | violations |
|---|---|---|---|
| 0.0 rung vs 1X2 | 2 993 / 5 986 | 0.558 · 0.688 · 0.771 · **0.978** | **0** |
| Draw No Bet vs 1X2 | 109 / 218 | 0.712 · **0.774** · 0.840 · 1.127 | **1** |

The DNB median lands on the theoretical `1 − P(draw)` almost exactly. The bound
needs no slack: it is exact and measured never to be approached.

Two live cases, each invisible to a check that looked at only one voider:

```
Resovia Rzeszów v KKP Bydgoszcz W   0.0 rung away @3.50 vs 1X2 @3.10  ratio 1.129
GKS Wikielec v Concordia Elbląg     DNB      away @1.95 vs 1X2 @1.90  ratio 1.026
                                    ...while its 0.0 rung was fine at 0.793
```

### B12. `fts_vs_ml` — First Team To Score vs the 1X2 · **the only CORRELATION here**
Everything else in Family B is an identity. This one is not, and the trigger is
chosen to match what the relationship can actually support.

Measured over the 90 collected events pricing both, comparing
`P(home first | someone scores)` against `P(home | no draw)`:

```
median −0.050   p10 −0.131   p90 +0.104   max |0.174|
```

The median is the shape working: scoring first is a **shrunk** version of
winning, because a team can score first and lose. But the deciles are ±0.12
wide, so the **size** of the gap is useless as a trigger — a threshold clearing
that noise sits near 0.17 and fires on nothing.

What is interpretable is a favourite **flip**:

| rule | fires | rate |
|---|---|---|
| bare flip (opposite sides of 0.500) | 3 | 3.3 % |
| **flip + 1X2 decisive by 0.06** | **1** | **1.1 %** ← shipped |
| flip + BOTH decisive by 0.06 | 0 | 0.0 % |
| \|gap\| ≥ 0.15 (magnitude only) | 4 | 4.4 % |

A bare flip is mostly two near-coin-flips landing either side of 0.500.
Requiring both sides decisive kills it — the FTS side is usually near even,
which is the point: a clear underdog has no business being favoured to open the
scoring at all. So the 1X2 side must be decisive by `DECISIVE_PROB = 0.06`, the
bar `favourite_flip` already uses.

The one **exact** bound the pair has is checked too: nobody-scores *is* 0-0,
one way to draw, so it cannot exceed P(draw). Violated 0 times in 90 events.

FTS needed its own `market_type`. Its middle leg is "nobody scores", not a
draw, so filing it as a 3-way moneyline would feed a 0-0 price into every check
that reads a 1X2.

### B2. `favourite_flip` — periods disagree on who's favoured
- FT favours one side but a sub-period favours the other, **both decisively**
  (`|P−0.5| ≥ DECISIVE_PROB = 0.06`, ≈ 1.8/2.05). Severity = summed edges.

### B3. `total_additivity` — period totals don't sum to their parent
- Parent vs children centers: FT vs H1+H2; H1 vs Q1+Q2; H2 vs Q3+Q4; FT vs
  Q1+Q2+Q3+Q4. Flag when off by ≥ **5.0 pts** (`TOTAL_ADD_PTS`). NB **handicap**
  additivity is intentionally NOT checked — favourites pull away late, so
  H1+H2 handicap ran ~0.75 short of FT on a clean game (too noisy).

### B4. `quarter_ml_extreme` — a quarter is more lopsided than the full game
- A short period should compress toward 50%. Flag when a quarter's `|P−0.5|`
  exceeds FT's by ≥ **6.0pp** (`EXTREME_PP`).

### B5. `ht_vs_ft_divergence` — **REMOVED 2026-08-03**
Flagged `|P_home(H1) − P_home(FT)| ≥ 13pp`. Deleted: it tested **no relation**, so
it could not detect a contradiction. A large HT→FT gap is what a goal model
*requires* — half time carries far more draw mass than full time, so a
favourite's win-prob is always compressed at the break.

Evidence, measured over its own **1151 historical flags**:
- **0 of 1151** fired on a balanced match (FT 35–65%). The FT win-prob
  distribution was perfectly bimodal — every flag sat at FT ≤ 29% or ≥ 70%. It
  was a "does this game have a big favourite?" detector.
- Severity was the raw gap, so it sorted the dashboard by **favourite strength**,
  not by wrongness.
- It was **26% of all consistency flags** (1151/4452) — the second-largest
  source, crowding out the real ones.
- The genuine HT↔FT relationship is already modelled correctly by **B7
  `htft_fair`** (bivariate normal on the two margins, ρ ≈ 0.70, μ from CB's own
  devigged handicap ladder). A raw percentage-point gap is the crude, wrong
  version of that.

Not to be confused with **B6 `htft_combo`**, which is a real bound check and is
kept.

### B8. `tennis_set_match` — the first set priced richer than the whole match · **BETTABLE direction exists**
Added 2026-08-11. Tennis was previously excluded from the consistency engine
(`CONSISTENCY_SPORTS` was basketball + soccer) and ran **list-only**, so the only
tennis markets that existed were match ML, games spread and games total. A CB
tennis **detail** page actually carries **15 markets on every match** — all
functions of the same four best-of-3 outcomes, so they must agree arithmetically.

Two rules, both from the structure of a best-of-3:

- **HARD (order).** With per-set win probability `p`, the match probability is
  `p²(3−2p)`, which is strictly **above** `p` once `p > 0.5` — you can lose the
  opening set and still win. So a first-set favourite priced **below** that on the
  match is impossible, not merely aggressive.
- **SOFT (magnitude).** Invert the match price back to a per-set probability and
  compare it with the posted first-set probability. Both describe the same
  quantity; a gap of ≥ **10.0pp** (`SET_MATCH_PP`) is a contradiction.

The hard rule is gated on the player actually being a favourite
(`SET_MATCH_HARD_MIN_FAV = 0.60`, `SET_MATCH_HARD_MIN_PP = 3.0`). At p ≈ 0.5 the
match and set probabilities coincide, so a one-tick pricing difference flips
which player looks favoured — ungated, that produced 15 bogus "impossible" flags
on the full board, all near-even matches like match `1.70/1.80` against set
`1.80/1.70` (a 2.4pp "impossibility").

The case that prompted it (CB, ITF Campos Do Jordao women):

| market | odds | devigged P(win) |
|---|---|---|
| Which player will win the match | 1.40 / 2.35 | **0.63** |
| 1st Set - Winner | 1.11 / 4.30 | **0.79** |

The match was priced **below its own first set** by ~17pp, and the two implied
per-set numbers were 0.585 vs 0.795 — **21pp apart** on the same quantity.

**Calibration — the whole board, not a sample.** An early 130-match sample
covered only scheme B (below) and suggested a much tighter spread; measuring ALL
**525 checkable matches** gave median **2.07pp**, p90 **6.13**, p99 **8.48**,
**max 9.32**. The 10.0pp threshold sits above every observation on a full board
and roughly 2x below the flagged case. A full-board scan produces **0 flags** —
a rare-event detector, not a firehose.

**CB serves tennis under TWO naming schemes** (surveyed across all 546 matches):

| scheme | share | match winner | first set |
|---|---|---|---|
| A | **71%** | `Winner` | `1st Period Winner Home/Away` |
| B | 29% | `Which player will win the match` | `1st Set - Winner` |

Covering only B left match-winner coverage at 43 events against 218 with a
first-set winner. Covering both took **checkable coverage to 95% (525/548)**.
Scheme A also carries `To win 1st set & win the match` — that is P(1/1), the
combo leg, and is the obvious next thing to wire.

**Markets now classified for tennis** (`src/scrapers/sports/tennis.py`):
`Which player will win the match` → moneyline FT · `1st Set - Winner` → moneyline
H1 · `2nd Set - Winner` → moneyline H2 · `1st Set / Match*` → **htft** FT (same
4-cell shape as soccer HT/FT, so **B6 `htft_combo` now applies to tennis too**) ·
`Total sets` → total FT.

**Still on the table, deliberately not wired** — every one is a linear function
of the four correct-score probabilities, so each is an exact identity waiting to
be checked, but `Odds` has no representation for a yes/no or 4-way correct-score
market: `Correct score` (2:0/2:1/1:2/0:2), `Home|Away Team To Win a Set`,
`Home|Away to win exactly 1 set`, `Exact Sets` (a duplicate of `Total sets` in
different words — they must agree), `Match to end 2:0` / `0:2`, `Set Handicap`
(±1.5 **sets**, which would collide with the list-view **games** spread),
`Odd/even games`.

### B9. `ot_vs_regulation` — the incl-OT winner vs the regulation 1X2 · **AMERICAN FOOTBALL + ICE HOCKEY** · BETTABLE direction exists
Added 2026-08-12 with the sport. CB posts the **same period twice**: a 3-way
**regulation** result (`Main result` at FT, `1st Half Result` at H1 — American
football can be tied at the end of regulation and CB prices that leg) and a
2-way **including-overtime** winner (`Winner (incl. overtime)`). They are tied
by an identity with no model in it:

```
P(win incl OT) = P(win in regulation) + P(tie) · P(win the overtime | tie)
```

The conditional lives in [0, 1], so the incl-OT probability is **boxed**:

```
P(win reg)  ≤  P(win incl OT)  ≤  P(win reg) + P(tie)
```

Stepping outside that box is not an aggressive price, it is an impossible one.
Below the floor says overtime *removes* a regulation win; above the ceiling says
a team wins more often than "win in regulation, or tie and then win" allows.

- **HARD (the box).** Flag when a price sits ≥ **3.0pp** (`OT_BOX_PP`) outside
  either bound.
- **SOFT (the point estimate).** Inside the box, overtime is close to a coin
  flip, so `P(incl OT) ≈ P(reg) + P(tie)/2`. Flag a gap ≥ **8.0pp**
  (`OT_COINFLIP_PP`).

Applied **per period**, not just FT.

The case that prompted it (CB, USA NFL, Jacksonville–Cleveland):

| market | odds | devigged |
|---|---|---|
| Main result (regulation 1X2) | 1.35 / 12.8 / 3.15 | home **0.683**, tie **0.046** |
| Winner (incl. overtime) | 1.19 / 3.55 | home **0.779** |

The ceiling is 0.683 + 0.046 = **0.729**. The incl-OT price says 0.779 — **5pp
above** what winning-or-tying-then-winning-every-overtime can produce.

**Calibration — the whole board.** All 177 CB games; 50 (event, period) pairs
post both markets. Residuals, positive = outside the box:

| | p50 | p90 | max |
|---|---|---|---|
| floor `P(reg) − P(incl OT)` | −2.39 | +0.24 | **+4.26** |
| ceiling `P(incl OT) − P(reg) − P(tie)` | −1.23 | +0.95 | **+5.08** |
| \|coin-flip gap\| | 0.57 | 3.29 | 7.36 |

The ordinary case sits comfortably inside a box only ~3.6pp wide, so 3.0pp puts
the trigger past p90 in both directions: **3 of 50 pairs flagged**, all three
inspected by hand and real.

Sanity check on the tie leg: it devigs to ≈5% on NFL, which matches the ~6–7% of
NFL games that actually reach overtime — confirming `Main result` really is the
regulation market and not a mislabelled final result.

**Extended to ice hockey, 2026-08-28 — and it flags nothing there.** Hockey is
the sport this identity was made for: its tie leg is ~23 % rather than the NFL's
5 %, and CB posts both markets on 85 events. But the two live in DIFFERENT
periods here — the 3-way regulation result is `REG` (60 minutes) and the 2-way
winner is `FT` (incl. overtime and the shootout) — so the AF code path, which
reads both shapes out of one period view, finds nothing. Hockey gets its own
cross-period comparison.

Measured over the whole collected hockey history: **0 violations in 85 events**,
in either direction, and the coin-flip gap maxed at **1.81pp** against the 8.0pp
threshold. The reason is worth recording: **CrystalBet does not price its
incl-OT hockey moneyline independently at all.** Implied
`P(win in OT | tied after 60)` came out p10 0.477, **median 0.500**, p90 0.524 —
a flat coin flip applied to the regulation 1X2. Two markets computed from one
number cannot contradict each other.

That is not a reason to drop the check (it is exact, it costs a comparison, and
the board multiplies when the NHL season opens) but it *is* a reason not to
count it as coverage. See `docs/icehockey.md` §6.

### B9b. `ot_monotone` — the incl-OT goal ladder must dominate the regulation one · **ICE HOCKEY** · diagnostic
Added 2026-08-28. CrystalBet publishes two goal ladders per hockey event —
`Total Goals*` (regulation) and `Total Goals(incl. overtime and penalties)` —
plus both team-total variants. Overtime can only ever **add** goals, so at every
line

```
P(over N incl OT)  ≥  P(over N regulation)
```

exactly, with no model and no tolerance. A regulation price sitting above the
incl-OT one at the same line is the book contradicting its own arithmetic.

`OT_MONOTONE_PP = 3.0` is pure devig slack — the true bound is zero, and the two
ladders carry independent vig.

**Calibration.** 924 shared rungs over 49 events: **not one** had the regulation
side higher, and the incl-OT side ran **+2.4pp** above it on median (+5–6pp
mid-ladder), which is what ~0.2 of an overtime goal looks like on a ladder.

**The puck line is deliberately NOT checked this way**, and that is arithmetic
rather than an omission: overtime is sudden death and is only reached from a
tie, so the overtime goal always makes a **one-goal** win. Winning by 2+
including overtime *is* winning by 2+ in regulation. CB prices those two ladders
identically on **300 of 300** rungs, correctly — a monotonicity check there would
be checking an equality.

**Caveat before retuning:** residual size depends on the devig model. `src.vig`
uses a **power** devig, which pushes more vig onto the longshot tie leg than a
proportional one would; proportional devigging shrinks the same three cases by
1–2pp. 3.0 is calibrated against the power devig the rest of the system uses —
it is not a model-free constant.

**A shared bug this check exposed.** `cb_detail` keyed variant dedup on
`(period, market_type, line, submarket, team_side)`, so a 2-way and a 3-way
moneyline on the same period collided and whichever the page rendered first
silently deleted the other. That starved this check of one of its two inputs by
construction — and it was **already** doing the same to **basketball**, where CB
posts `Full Time Result(1X2)*` alongside `Winner (incl. overtime)` and B6
`htft_combo` is written to consume the regulation legs. Selection count is now
part of the key; the strict (+EV) classifiers never emit both shapes for one
period, so only the permissive/anomaly path changes.

### B6. `htft_combo` — the HT/FT 1/1 (and 2/2) price vs its own legs · **BETTABLE direction exists**
The Halftime/Fulltime combo checked against the H1 and FT **regulation** 1X2
legs. Only fires inside the bettable range `1.15 ≤ odds ≤ 15.0`. Two bounds:

> **The cap was 4.5 until 2026-08-29**, and it was hiding well-founded flags
> while filtering almost no noise. Residual of the correlation-fair model,
> `(posted − fair)/fair`, over 2 936 (event, cell) pairs:
>
> | posted | n | median | p10 | p90 | **p10–p90 spread** |
> |---|---|---|---|---|---|
> | 1.15–2.5 | 441 | −12.0% | −15.1 | −3.9 | 11.2 |
> | 2.5–4.5 | 1264 | −21.1% | −25.9 | −16.0 | 9.9 |
> | 4.5–8 | 846 | −31.3% | −37.4 | −27.2 | 10.2 |
> | 8–15 | 297 | −44.9% | −51.3 | −40.0 | 11.3 |
> | >15 | 86 | −58.6% | −72.6 | −53.5 | **19.2** ← model degrades |
>
> The **dispersion is flat** to 15 and only widens past it — the model holds
> fine at 5.90. What drifts is the **median**, and it drifts *down*, so the
> +2 % bar gets **harder** to clear as the price lengthens. A long-price flag
> is a *bigger* outlier than a short-price one, which is the opposite of what a
> longshot gate assumes.
>
> Cells clearing +2 % across the whole collected board: cap 4.5 → **30**,
> cap 8 → 30, cap 15 → **31**, no cap → 31. The old cap blocked one historical
> flag, and on the live board a `1/1 @5.90` sitting **12.4 % above fair** while
> its own bucket's p90 is −27.2 % (Lech Poznań Uam II v Staszkówka).
>
> Filtering by PRICE now belongs to the per-table odds band on the tab and the
> alert vetoes — visible and adjustable, where a constant makes a flag never
> exist at all.
>
> **This applies to `htft_combo` ONLY.** `HTFT_ODDS_MAX` had two consumers and
> the table above measures one of them. B7 `htft_fair` keeps its own
> `HTFT_FAIR_ODDS_MAX = 4.5`: different sport (basketball), different model
> (bivariate normal), all nine cells, and no calibration behind widening it.
> Sharing the constant put 17 htft_fair flags on the live board at severities
> to 123.3 — all "shape off vs model" on reversal cells, the corner where that
> model is least trustworthy.
- **Dominance:** `P(1/1) ≤ min(P(H1=1), P(FT=1))` ⟹ `odds(1/1) ≥` each leg.
  A combo **shorter than its own leg** is logically impossible → flag
  (`short_pct ≥ HTFT_GAP_PCT = 2%`).
- **Correlation-fair (the +EV direction):** leading at half and winning are
  positively correlated, so the true combo is **shorter than the naive product**
  `odds(H1)×odds(FT)` (which also double-carries both legs' vig). Owner's model
  (2026-06-21): `fair = HT_leg × (1 + (FT_leg − 1)/2)`, clamped at the longest
  leg. A combo **longer than fair** by ≥ 2% is over-generous.
  - Worked case (River Plate): H1 1.35 × FT 1.07 → fair ≈ 1.44; a 1/1 @ 1.55
    reads ~7% generous, where the raw product 1.539 saw only 0.7% and missed it.
- 🟡 **SUPERSEDED (shadow, pending validation).** For soccer, the correlation
  haircut `HT × (1 + (FT−1)/2)` is a crude stand-in for the true joint. Family E
  (`soccer_model`) prices the same 1/1 & 2/2 off the calibrated goal matrix
  exactly. B6 stays **on in shadow** until E is graded on beat-the-close; it is
  the model-light fallback and still owns **basketball** HT/FT combos (E is
  soccer-only). Retire the soccer path only once E wins the grading.

### B7. `htft_fair` — HT/FT vs a bivariate-normal model (**basketball only**) · BETTABLE direction exists
Model-based fair for **every** HT/FT outcome via a bivariate normal on the
(halftime margin, full-game margin) — a joint probability, not a product of
marginals (`src/htft_model.py`). `mu` from CB's devigged handicap ladder (else
ML), `sigma` per league, `rho ≈ 0.70`. Two signals per outcome:
- **EDGE:** posted price beats model fair even before vig
  (`cb ≥ fair × 1.03`, `HTFT_FAIR_EDGE_PCT = 3%`) → +EV candidate. Soft books
  template a near-constant HT/FT multiplier while the true one varies (~1.27 for
  a favourite 1/1 … ~1.49 for a dog 2/2), so **lopsided lines are the
  structural sweet spots**.
- **SHAPE:** devigged CB prob disagrees with the model by ≥ **1.5×**
  (`HTFT_SHAPE_RATIO`) on a meaningful outcome (model ≥ 5%) → internal shape off.
- Guards: ignore model odds > 20 (X-row longshots explode), only inside
  1.15–4.5 posted, shape needs a near-complete ladder (≥7 of 9 / ≥5 of 6).

**Why B6/B7 matter for research:** these are the structural home of the whole
"soft books misprice HT/FT" thesis. B6 is model-light (just the legs + a
correlation haircut); B7 is the full joint model. Cross-checking the two against
each other on the same game is the cleanest way to validate the fair.

---

## Family C — Betlive OT-fold (`src/betlive_anomalies.py`, watched by `src/betlive_watch.py`) · DIAGNOSTIC, single-book

`kind`: `betlive_ot_fold` and `betlive_flip` (the favourite-flip subtype).
Gated by `BETLIVE_ANOMALY=1`. **Basketball / ice-hockey only** (sports that play
OT off a drawable regulation result).

- **Idea:** Betlive prices BOTH an **incl-overtime 2-way winner** and a
  **regulation 3-way result**. Folding OT in only moves probability OUT of the
  draw and INTO the two win outcomes — it can never take prob AWAY from a side.
  So after devigging each market on its own:
  `P(side, incl-OT) ≥ P(side, regulation)` for both sides.
- **Flag:** a violation ≥ `min_gap_pp = 1.5pp`. `favourite_flip` = the 2-way
  favourite differs from the 3-way favourite (the loud case). Root cause is
  almost always a **swapped home/away** on the incl-OT 2-way; the detector
  doesn't need the cause.
  - Canonical case (VBA basketball, Ho Chi Minh vs Nha Trang): reg devig
    home .381 / draw .072 / away **.548**; incl-OT home .556 / away **.444** —
    the .55 favourite "lost" 10.3pp by adding OT. Impossible.
- **Invisible without the detail call:** the list feed prices only one of the
  two markets; you must fetch `getPrematchEvent` ("extended mode") to see both.
- **Short-lived → latency matters.** These self-correct in ~15–30 min. Two-speed
  watch: slow `discover()` records each event's 5 ML `outcomeId`s; fast
  `/api/outcome/refreshOdds` (POST, ~50 KB for the whole board) recomputes the
  OT-fold check every few seconds.
- **Guards** (`_DISQUALIFY`, `_has_line`): exclude sub-period/prop markets and
  **lined** markets — a "Points Spread (OT)" carries `1`/`2` labels too and
  would masquerade as the moneyline.
- **Research:** log the flip→correction time distribution and whether the
  refreshOdds fast path actually catches them before they close.

---

## Family D — Soft-book sweep (`src/soft_scan.py`) · EXPLORATORY, all three books

`SOFT_SCAN=1`, default `SOFT_SCAN_SEC=3600` (60 min). Separate from the CB
ladder scan. Per book: cheap list → keep only **heavy-favourite, non-top-league**
games → open the full ladder for just those → fuzzy-match markets → run two
detectors. Resilient (one book failing doesn't sink the rest).

**Gate** (`htft_favourite.should_open`): favourite = a side **< 1.30** OR a side
with **NO price** while the other is quoted (an omitted favourite price = an
extreme favourite — a case we explicitly don't want to miss). Skip top leagues
(`TOP_LEAGUE_TOKENS`: World Cup / UEFA / big-5) — sharp, no soft errors, and they
dominate the <1.3 set.

### D1. `soccer_htft` — HT/FT combo too generous for a heavy favourite (`src/htft_favourite.py`)
For a heavy FT favourite the HT/FT combo for that side (1/1 home, 2/2 away)
should be only marginally longer than the **first-half result** for the same
side: given a heavy favourite leads at the break it almost always wins, so
`HTFT ≈ H1` (ratio ~1.0, measured 0.97–1.03 on a live board). Two modes:
- **vs first-half leg** (`htft_flag`, `RATIO = 1.2`): flag when combo ≥ 1.2× its
  H1 leg. Real case: H1 1.1 → HT/FT 1.4 (1.27×). The *real* line — 1.5/1.7 never
  fire because for a heavy fav the ratio hugs 1.0. Applies to BOTH 1/1 and 2/2.
- **vs the FT moneyline** (`htft_vs_ml_flag`, `HTFT_ML_RATIO`, env
  `SOFT_HTFT_ML_RATIO`, default **1.35**): fallback for books/games with **no
  standalone first-half market** (this is how Lider-Bet participates at all).
  For a heavy favourite the combo runs ~1.3–1.4× the ML.
- ⚠️ **Honest caveat:** the vs-ML mode is **top-of-the-distribution, not a hard
  anomaly**. Live, the *same* games flag across all three books at ~1.4×
  (Boca, Palmeiras, Middlesbrough), which says it's **normal pricing**, not a
  per-book error. Treat as a screen to eyeball, not an EV claim. Tune
  `SOFT_HTFT_ML_RATIO` up (fewer, sharper) / down (more, noisier).
- 🟡 **SUPERSEDED (shadow, pending validation).** A *constant* vs-ML ratio has
  **zero discrimination**: the fair 2/2-to-FT ratio equals `1 / P(led at HT |
  won FT)`, which is a function of favourite strength and total — ~**1.36** for a
  1.18 favourite in a 4.2-total game, but ~**1.7–1.9** for a 1.60 favourite in a
  2.3-total game. A single 1.35 threshold therefore flags normal strong
  favourites and misses genuine soft lines on tighter games. Family E computes
  that conditional ratio per game from the goal model, so it replaces this. Keep
  D1's **vs-first-half mode** (1.2×) as a cheap pre-filter; the vs-ML mode stays
  on in shadow only until E is graded, then retires.

### D2. `basketball_fav` — favourite disagreement across markets (`src/basketball_fav.py`)
The 2-way (incl-OT winner), 3-way (regulation result), HT moneyline, and the
FT-winner **implied by the 9-way HT/FT combo** must all agree on WHO is
favoured. Devigged home-vs-away win prob (draw dropped, two-way renormalise)
should be close across all four.
- **Flag:** favourite **flips** (`lo < 0.5 < hi`) OR the spread across markets
  ≥ **10.0pp** (`FAV_GAP_PP`; measured: the 3-way often disagrees with the 2-way
  by ~12pp). Needs ≥ 2 of the 4 markets.
- `htft_winner(htft)` derives (home, away) FT-winner odds from the combo:
  home = `*/1` (1/1, X/1, 2/1), away = `*/2`. This is the "HT/FT position" the
  owner asked to add as a 4th market.
- **This is the more meaningful soft-scan signal** (the 2-way/3-way split is a
  real structural disagreement, closer to Family C than to D1). Live: 8 betlive
  flags e.g. `Luxembourg 2w=75% 3w=87% ht=78%`.

### Per-book gotchas (validated live 2026-07, also in `findings-2026-07.md`)
- **Betlive:** HT/FT outcome labels are **"1 / 1" (spaced)** → strip spaces
  before matching "1/1". The 3-way basketball result is **NOT marketId 1** →
  match by `{1,X,2}` labels. **Exclude lined markets** — a "Points Spread (OT)"
  carries 1/2 labels and masqueraded as the ML (was 7 false flags).
- **Lider-Bet:** full ladder is `matchData/details?matchIds=` (plain `matchData`
  is a curated 33-market subset with no HT/FT). It has HT/FT but **no standalone
  1st-half result** (only combos) → its soccer HT/FT works **only via the ML
  fallback**; it still contributes basketball.
- **CrystalBet:** soccer HT/FT title varies by league — regex broadened to the
  HT/FT prefix (like basketball). Title conventions seen: half result is
  `1X2 Halftime` (not `1st half - 1x2`); combo is `HT/FT Including Overtime`.
- **Fuzzy matchers are the fragile part:** a book renaming a market silently
  drops coverage — re-run the coverage probe periodically.

### Energy per sweep (filtered)
CB ~17 MB / 30 s · Betlive ~7.6 MB / 6 s · Lider-Bet ~4.5 MB / 0.6 s (batched
details). ≈ 30 MB / 40 s once an hour. Cheap enough to run alongside the CB
ladder scan.

---

## Family E — model-implied soccer fair pricing (`src/soccer_model.py` + `soccer_identities.py` + `soccer_curves.py`) · SOCCER ONLY

The principled replacement for the soccer heuristics (B6's combo formula, D1's
static ratio). One calibrated goal model fair-prices **every** soccer derivative;
a set of model-free algebra checks backstop it at family-A confidence. **numpy
only** — devig, Poisson/Dixon-Coles score matrices, and the fits are hand-rolled
(no scipy). Status: **library built + unit-tested (Modules 1–3); EV flagging,
staleness, and logging (Modules 4–5) are pending a wiring design** — not yet
connected to the refresh loops or the Anomalies tab.

### E-core — the goal model (`soccer_model.py`, Module 1)
- Everything reduces to `(λ_home, λ_away)` ≡ supremacy `S = λh−λa`, total
  `T = λh+λa`. `F[i,j] = P(home i, away j)` is an N×N (N=12) independent-Poisson
  matrix with a **Dixon-Coles** τ-correction on the 0-0/1-0/0-1/1-1 cells
  (`rho = −0.08`). With `dc_rho = 0` it's pure Poisson, so the two half matrices'
  convolution equals the FT matrix **exactly** — every cross-half identity then
  holds to numerical zero (the basis of the property tests).
- **Devig** (`devig`): `proportional` (baseline only), `power` (solve k with
  Σpᵢᵏ=1), `shin` (insider-fraction z). Default `auto` = shin for 3-way, power
  for 2-way. Power/shin shrink longshots harder than proportional — the whole
  point at lopsided prices (1.18 vs 8.90).
- **Fit** (`fit_lambdas`): recover `(λh, λa)` by matching devigged home-win + over
  (if a main total is present) or home-win + draw (1X2 only), via damped Newton
  in log-λ space.
- **Half split** (`half_matrices`): `split` of each λ into H1 (~0.44), the rest
  into H2; per-league configurable (`LEAGUE_SPLIT`).
- **Price sheet** (`build_model`): one calibration → 1X2, DC, DNB, all 9 HT/FT
  cells, HT & 2nd-half 1X2, FT/half totals ladders, Asian handicap incl. quarter
  lines, European 3-way handicap, team totals, BTTS (+ result combos), **result &
  total combos taken off the joint matrix, not leg products** (the correlation
  books throw away), correct-score grid, win-to-nil, clean sheet, multigoals,
  exact goals, odd/even, highest-scoring-half, goal-in-both-halves. All pure
  functionals of the matrices.
- **Validated** on the hand-checked Nepean–Mounties fixture: power devig
  (0.0708/0.1108/0.8184), fit λ≈(0.921, 3.239), T≈4.16, and HT/FT prices
  (2/2 fair 1.66 vs posted 1.80 = **+8% EV**) reproduce exactly.

### E-identities — model-free algebra (`soccer_identities.py`, Module 2, family-A tier)
On RAW posted odds, keyed with the model's scheme:
- **Partition inequality:** for any whole = disjoint union of parts (HT/FT column
  → FT side; HT/FT row → HT side; DC → its 1X2 legs; totals → exact-goal cells;
  correct-score cells → 1X2 / unders), `Σ raw(parts) ≥ raw(whole)` normally holds
  (each part carries vig). Parts summing to **less** ⇒ at least one part
  over-generous (+EV). Generalises the original HT/FT "/2 column" find; validated
  on Nepean (/2 column 0.8235 < FT2 0.8475 → fires).
- **Exact equivalences + intra-book arb:** AH(0) ≡ DNB, AH(∓0.5) ≡ 1X2 side,
  AH(±0.5) ≡ double chance, quarter ≡ mean of neighbours. A price gap flags; a
  crossed pair (1/a + 1/b < 1) is a locked arb.
- A perfectly consistent (vig-free) sheet raises **zero** identity flags — the
  property test that caught a real sign bug in the AH↔DC mapping.

### E-curves — curve residuals + cross-fit (`soccer_curves.py`, Module 3, upgrade of A)
- **AH ladder ⇒ Skellam(λh, λa)** (goal difference of two Poissons): fit the
  ladder, flag any rung ≥ 2pp off the fitted survival curve. Catches
  monotone-but-wrong-magnitude rungs family A misses.
- **Totals ladder ⇒ Poisson(T):** same, off-curve rungs (DC is second-order on
  the aggregate total).
- **Two-anchor cross-fit:** read `(S, T)` from the 1X2 + main total vs from the
  AH + totals ladders; if they diverge by ≥ 0.15 goals (S) or ≥ 0.25 goals (T),
  one family is stale — diagnostic, with a timestamp guess at which moved last.

### E-EV — model EV flagging, WIRED into the sweep (`soccer_ev.py`, Module 4, simplified)
Runs inside the existing `SOFT_SCAN` sweep (`soft_scan.py`) — no new
scheduler/DB. Per owner (2026-07-03):
- **Anchor = the book's OWN 1X2** (own-book, not Pinnacle). Fit λ to the book's
  devigged 1X2, price the sheet, flag every posted derivative priced LONGER than
  fair. `kind`s: `soccer_fair` (EV), `soccer_identity` (E-identities),
  `soccer_curve` (E-curves). The own-book anchor makes these an
  internal-consistency signal (generous vs the book's own headline).
- **Devig default = `power`** (env `SOCCER_DEVIG`) — matches the owner's hand
  validation and surfaces more; `shin` is the conservative alternative (they
  disagree at extreme prices: Nepean 2/2 reads +7.8% power vs +2.3% shin).
- **Game gate = a side < 1.5 (or one side priced, the other not), skip top
  leagues** (`SOCCER_FAV_MAX`, env). Looser than the legacy 1.30 htft gate;
  basketball keeps 1.30.
- **Thresholds:** `SOCCER_EV_MIN` (default 3%), tails 8% (`SOCCER_EV_TAIL_MIN`);
  set `SOCCER_EV_MIN=0` to surface everything above fair. A light **robustness
  hint** recomputes EV at H1 splits {0.42,0.48}; non-robust hits are tagged
  `[split-sensitive]` (not gated out).
- **Market scope (this cut): 1X2 + HT/FT + 1st-half result only**, across all
  three books. CB's `total`/`spread` market_types also carry corners/cards
  ladders (an "over 8.5" that devigs to 74% is corners), so **totals & Asian
  handicaps are deferred** until a goals-only filter + a coverage probe are in —
  E-curves therefore stay dormant for now. The fit is bounded so a mislabelled
  1X2 can't produce NaN EV.
- **Live-validated 2026-07-03:** betlive HT/FT is tight (≈0 EV, identities catch
  small draw-column inconsistencies); CB leaves real value on obscure leagues
  (HT/FT 2/2 @1.70 vs fair 1.47 = +15.5%); lider similar. Fixed a latent
  `_bl_markets` bug — it grabbed "1 - 10 min. Result" (draw @1.24) as the 1X2;
  now anchored on betlive's `marketId == 1` "Fulltime Result".

### E-dropped (owner 2026-07-03)
The **staleness modifier**, **Pinnacle/sharp anchor tier**, the full
split×devig **robustness band**, and the **SQLite flag log + beat-the-close
grading** (Module 5) were dropped — "just the new math in the anomalies flow."
B6 soccer path + D1 vs-ML remain in **shadow** (both still emit) rather than
being graded-then-retired.

---

## Why `htft_combo` fires: it is a STALENESS detector (measured 2026-08-19)

Owner's read: *"most likely I think when ml positions move they kind of stay
stale and that causes it"*. Correct, and the grid proves it about itself.

### The instrument: the grid's own identity

An HT/FT grid must reconcile with the book's own 1X2 legs, with no model and no
reference book involved:

    rows    {1/1,1/X,1/2} -> H1 home   {X/*} -> H1 draw   {2/*} -> H1 away
    columns {1/1,X/1,2/1} -> FT home   {*/X} -> FT draw   {*/2} -> FT away

(Verified on three other books earlier: median row/column error 0.32–0.99 pp.)

Comparing each CB grid against CB's *own* posted H1 and FT moneylines, live,
flagged games versus a control group expanded in the same pass:

| | row vs H1 | col vs FT |
|---|---|---|
| **flagged** (n=17) | median **2.78 pp**, max 4.66 | median **2.33 pp**, max 5.51 |
| control (n=59) | median 0.80 pp, **max 1.99** | median 0.42 pp, max 1.66 |

**Perfect separation**: every flagged game's row error (2.15–4.66 pp) exceeds
every control game's maximum (1.99 pp). The grid is out of step with the legs on
100 % of flags and on none of the controls — the signature of legs that moved
while the grid did not.

**Practical consequence:** a row error above ~2 pp identifies a stale grid
without running the correlation model at all. Cheap pre-filter, and a candidate
severity input.

### The error has a direction

    cell 1/1: 11    cell 2/2: 3
    too generous (book price longer than fair): 14 of 14

Always the favourite's coherent outcome, always too long, never the reverse.
Systematic, not noise — consistent with a grid derived from an older, less
confident price and never refreshed as the favourite shortened.

### Nothing else on those games is stale

Every consistency check and the ladder detector, run over all 17 flagged games:

    OTHER consistency kinds on flagged games: none
    ladder anomalies: 2 across all 17   (control: 0)

The grid is the *only* market out of step. Spreads, totals, team totals and
period markets stay coherent. There is no second market type to mine this way —
at least not on the games HT/FT flags.

For scale, board-wide CB soccer staleness (time since a market's last price
*change*, from `ticks.db`):

| market | median | p90 |
|---|---|---|
| **moneyline FT** | **930 min** | 2816 min |
| total FT (lined) | 213 min | 2489 min |
| spread FT (lined) | 95 min | 213 min |

The grid is not unusually stale in absolute terms; it is stale *relative to legs
that moved*.

### Provider: no tag exists, but the pattern is competition-shaped

CB exposes no odds-provider field — the `Provider` strings in its HTML are
casino banners and the SportRadar references are the stats widget, not a feed
tag. So there is nothing to join on.

What is visible: flags cluster in secondary competitions — **youth 6, cup 3,
women 2, reserves 1** of 17 — and **13 of 17 games do appear on other books**,
so this is not a CB-only obscure feed. The likeliest reading is that these
competitions get algorithmically-derived HT/FT refreshed on a slower schedule
than the mains, rather than a distinct provider.

### Known gap: the grid has no history

Only the four PRICE loops call `ticks.rows_from_odds`. The ladder scan — the
only thing that fetches grids — writes nothing, so **HT/FT has no tick history
at all** and staleness has to be inferred from a single snapshot. Writing the
ladder scan's odds to the tick store would make the lag directly measurable and
allow an alert on "ML moved > X, grid unchanged for > Y". Not done.

---

## How they reach the Anomalies tab
- CB ladder (A) + CB consistency (B) via the main CB scan (`ANOMALY_SCAN=1`).
- Betlive OT-fold (C) via `BETLIVE_ANOMALY=1` — flags land in the consistency list.
- Soft sweep (D) via `SOFT_SCAN=1` → merged into `/api/anomalies` `cons` list,
  `first_seen` carried over on `(book, book_event_id, kind, outcome)`.
- `enabled` when any of `ANOMALY_SCAN | BETLIVE_ANOMALY | SOFT_SCAN`.
- `KIND_LABEL` (`static/anomalies.html`): `ml_vs_spread`→"ML vs handicap",
  `favourite_flip`→"favourite flip", `total_additivity`→"period totals",
  `quarter_ml_extreme`→"quarter ML extreme", `htft_combo`→"HT/FT combo",
  `htft_fair`→"HT/FT fair (model)", `betlive_flip`→"betlive: favourite flip",
  `betlive_ot_fold`→"betlive: OT-fold", `soccer_htft`→"soccer HT/FT (soft)",
  `basketball_fav`→"basketball fav disagreement", `soccer_fair`→"soccer EV
  (model)", `soccer_identity`→"soccer identity", `soccer_curve`→"soccer curve"
  (Family E, in the `SOFT_SCAN` sweep).

## Env-var quick map
`ANOMALY_SCAN` (CB ladder + consistency) · `BETLIVE_ANOMALY` (OT-fold watch) ·
`SOFT_SCAN` + `SOFT_SCAN_SEC` (soft sweep) · `SOFT_HTFT_ML_RATIO` (D1 vs-ML
threshold, default 1.35) · `CB_TRANSPORT` / `CB_ANOMALY_TRANSPORT` (http vs
Playwright).
Family E (soccer, in the `SOFT_SCAN` sweep): `SOCCER_FAV_MAX` (game gate, default
1.5) · `SOCCER_DEVIG` (anchor devig, default `power`) · `SOCCER_EV_MIN` (default
0.03; set 0 to see everything above fair) · `SOCCER_EV_TAIL_MIN` (default 0.08).

## Confidence ranking / triage order (soccer, once Family E is live)
1. **E-identities & AH(0)≡DNB** (family-A tier) — model-free, hard. Trust most.
2. **E partition inequality** — model-free, points at the over-generous leg.
3. **Stale-derivative-tagged EV flags** (E-pending, Module 4) — the parent moved,
   the derivative didn't; strongest live signal.
4. **Clean EV flags off the sheet** (E), robustness-band-confirmed, sharp anchor.
5. **E-curve residuals** (upgrade of A) — off-curve rungs, magnitude errors.
6. **Cross-fit divergence** — diagnostic ("one family is stale"), not EV.

## Confidence ranking (all families, current)
1. **A (ladder monotonicity)** — hard, bettable, single-book. Trust most.
2. **C (Betlive OT-fold)** — hard structural bound, single-book, but short-lived.
3. **E (soccer model + identities + curves)** — the soccer research core;
   supersedes B6/D1 (both now shadow, pending grading).
4. **B7 (basketball HT/FT model)** — the basketball analogue of E; EV claims.
5. **B1–B5 (consistency diagnostics)** — "go look", not EV.
6. **D2 (basketball fav)** — structural disagreement, promising, needs outcome data.
7. 🟡 **B6 soccer path / D1 vs-ML** — SUPERSEDED by E; kept in shadow only.

---

## Lider combo bounds — `combo_cover` / `combo_dominance`

### The price ceiling — `CEILING_ODDS` (2026-09-23)

**Lider's 101.0 is a refusal to quote, not an offer**, and the board says so
without ambiguity. Over 25 743 priced outcomes on 40 live matches:

| price | count | share |
|---|---|---|
| **100.0** | 854 | **3.32 %** |
| **101.0** | 617 | **2.40 %** |
| 35.0 | 379 | 1.47 % |
| 50.0 | 367 | 1.43 % |

Above 50 there are only **seven distinct values** on the whole board
(50/60/70/80/90/100/101), and **101.0 is the maximum price anywhere**. A
distribution of beliefs does not have its mode at its own ceiling.

**Why it is load-bearing rather than cosmetic.** A leg at 101 adds
`1/101 = 0.0099` to a cover's outlay — almost nothing — while being the leg
that **completes** it. So the ceiling buys the arb its last corner for free,
and the position only exists if the book will actually take that bet at size.

Found from the owner's board, Barcelona v Paris FC (Champions League Women):
three of the four `combo_cover` flags on that match were completed by a
`Double chance / Total — Over/X2 @ 101` leg. Board-wide at a 48 h horizon,
**2 of 3** covers leaned on one.

Dropping `odds >= CEILING_ODDS` at `_dedup` — the single choke point both bet
lists exit through, so the cover DP, the containment test and the duplicate
scan are all covered by one filter — changes the board like this:

```
without   combo_cover 3   leaning on a >=100 leg: 2   severities [9.5, 7.1, 5.4]
with      combo_cover 3   leaning on a >=100 leg: 0   severities [8.0, 7.1, 4.5]
```

**Nothing is lost** — the same three covers survive, rebuilt from real legs at
their true value. The two inflated ones fall to what they are actually worth.

**Third time for this shape in this project**, which is why it now has a named
constant: CrystalBet's 100.0 on 88 552 positions (the old "grid
opportunities"), the pinned longshot rungs in the ice-hockey pass, and now
this. The tell is always the same — a price that is also the mode.


*Built 2026-08-19. Engine: `src/lider_combos.py`. Scan toggle: Config → Scans →
`lider_combo` (default OFF — it pays for its own detail fetch).*

Lider prices ~50 **combo** market types ("Team 2 win and Total Over 1.5",
"1st Half Result or Match Result", "I Team Not lose and Total Under {L}"). Each
is a bet on a *set* of outcomes, and so is every primitive — the 1X2, the double
chance, each rung of the totals ladder. Express both as bitmasks over a small
atom space and two exact, model-free tests fall out.

**Atom spaces.** `TOTAL` = (FT result) × (Under/Over @ line), 6 atoms, one space
per line. `HTFT` = (H1 result) × (FT result), 9 atoms.

**`combo_dominance`** — if set A sits inside set B, A can never pay more than B.
Odds compare directly: no devig, no margin assumption, no correlation model, no
second book. A violation is a logical impossibility, not an opinion.

**`combo_cover`** — buy a group whose sets union to everything. Every outcome
pays, so an outlay of `sum(1/odds) < 1` is locked profit. Exact 2ⁿ-state DP.

### Why only some families are worth scanning

Measured on the whole 1380-match board, 225 matches re-polled 16× at 3-minute
spacing. **Two completely different behaviours** when a leg moves:

| family | moves | given 1X2 moved | given neither moved |
|---|---|---|---|
| `mt:16:3396` Total/HT-FT | 4.0% | **100%** | 0.2% |
| `mt:16:2307` DC/Total | 3.8% | **100%** | 0.2% |
| `mt:16:1080` 1X2/Total | 3.8% | **98.5%** | 0.2% |
| `mt:16:1095` 1X2/BTTS | 3.6% | **98.5%** | 0.1% |
| `mt:16:1716-1719` not-lose & total | 13.6% | **22%** | 13.2% |
| `mt:16:3910/4793` H1 win & total | 13.9% | **12%** | 13.9% |
| `mt:16:3976/3977` win & BTTS | 8.3% | 19% | 7.8% |

The 6-way grids are **derived** — they reprice in the same tick as their legs,
so nothing accumulates (best cover across 30 000 grid cells: 1.0333, zero arbs).
The 2-way Yes/No families are **independently maintained**: their follow rate is
no higher than their baseline, so a leg moving carries *no information* about
whether they update. **Every arb found was in that second group.** Both are
parsed (the grids are free once the payload is read, and they make good hedge
legs) but the yield is in the 2-way families.

### What it found

Board-wide, 96 h horizon, ~50 s: **4 locked covers** and ~70 containment rows.
The covers were all one match — Iwata vs Tokushima, J2 League, whose whole
"I Team Not lose" block was mispriced at every line:

```
buy  I Team Not lose & Under 4.5  @ 1.85     0.5405
buy  I Team Not lose & Over  4.5  @ 24.00    0.0417
buy  FT 2 (away win)              @ 2.85     0.3509
                                             ------
                                             0.9331   →  +7.17%
```

It held unchanged for 11+ minutes across three full passes. Next best cover on
the entire board was 1.0333, so there is a clean gap between one broken match
and everything else — this fires almost never, which is what makes a flag worth
acting on.

### Three traps, all of which bit during the build

1. **Exactness.** A totals rung at M ≠ L is a valid *cover* leg but its mask
   understates what it wins ("Over 2.5" inside the 4.5 space also wins on 3 and
   4 goals). Understating a cover leg is safe; understating a *containment*
   operand invents subset relations. The first live sweep produced **98**
   dominance rows whose biggest were all this artefact. Only `M == L` is exact,
   and only exact bets take part in containment.
2. **Margin direction.** An early "too rich" test compared margin-inflated raw
   probability against a ceiling and produced **280** phantom violations. Each
   direction must use the raw odds of the side you would actually back, so the
   margin works *against* a flag rather than manufacturing one.
3. **Silent death.** An identity check requiring a matched line produced **zero**
   rows and looked clean — Lider offers `Draw & Under` at 2.5 but `Draw & Over`
   at 1.5, so the lines never paired. `tests/test_lider_combos.py` pins all
   three.

Mappings are read by **typeId + outcomeId only**, never by name: `mt:16:3982`
and `mt:16:3985` carry the identical name `'Team 1 win and number of goals: 3-5'`,
so one is mislabelled at source — the same trap as `mt:16:501`/`1079`. A mapping
was only trusted after its partition sum reproduced the posted leg across the
population (`1716/1718` → `1X` at **1.35pp** median absolute error over 1140
observations).


### Two more detectors (2026-08-20) — the exact tests were too weak

Containment and cover only catch what is logically *impossible*, and that bar is
very low. The market the owner was actually pointing at —
**"Team 1 Win and score more than 1.5 goals" @ 6.90** on Iwata — sits comfortably
inside its Frechet box `[0.062, 0.392]` and violates nothing. Its fair price is
about 3.45. Board-wide the exact tests found 4 covers and ~78 containment rows;
"almost nothing to actually bet on" was a fair verdict.

**`combo_duplicate`** — Lider prices one event in several places, and the prices
disagree. "Team 1 Win and score more than 1.5 goals" *is* the 1X2/Total grid cell
`Over / 1` at 1.5 (given A wins, `A>=2` ⟺ `total>=2`), and it was quoted at
**6.90 and 3.10 simultaneously**. `_dedup` collapsed every set to its best price
before anything looked at it, so the detector was deleting its own strongest
signal.

A duplicate says one of the two is wrong, **not which** — framing the gap as an
overlay produced **3240** board-wide rows, and the biggest were cases like
`X2&O2.5` @ 24 vs 7.8 on a 1.10 favourite, where 24 is the *correct* price and
7.8 the mistake. A bad short price is not bettable. So the long side must
independently clear model fair before it earns a row; then it carries more
evidence than either check alone, because two unrelated methods agree.

**`combo_fair`** — fit a Poisson score matrix to the book's own 1X2 + totals
ladder (`src/soccer_model.fit_lambdas`) and price every combo exactly off it.
**Half-lines only**: Lider posts integer rungs too, where a total of exactly L is
a *push*, so the two sides do not partition and a proportional devig of them is
meaningless. Measured on Iwata the integer rungs missed the fitted model by
**-19.9pp** and **-16.3pp** while every half-line landed within **2.4pp** —
including them killed the fit outright. The fit is rejected unless it reproduces
every posted half-line rung to 3pp, because a fair price is only as good as the
fit under it.

Result on the same board: **3331 → 177 flags across 116 distinct matches**, in
~34 s. The owner's market now reads

```
'1 win & score 2+' @ 6.90  vs  '1&O1.5' @ 3.10   (122.6% apart)
model fair 3.45            ->  EV +99.8%
```

with the two methods agreeing to within 0.35 on the fair price.


## Measured and NOT built — the side-market sweep (2026-09-02)

Owner asked to extend the checks to every statistic with handicaps and
over/unders: corners, cards, shots, throw-ins, tackles, anything. Corners were
built (both books). The rest was measured first and rejected, and the numbers
are here so the call can be revisited on evidence rather than re-derived.

### What the statistic boards hold, soccer

| statistic | CrystalBet | Lider-Bet |
|---|---|---|
| cards | 1085 ev | 932 |
| corners | 601 | 362 |
| shots | 241 | 236 |
| penalties | 34 | 106 |
| offsides / fouls | 19 / 16 | 28 / 26 |
| **throw-ins** | **15** | **21** |
| tackles / saves | 9 / 6 | 7 / 6 |

Past the top three it is a tail of under ~35 events. Throw-ins and tackles are
single digits to low twenties on both books.

### Three things measured, three zeros

**`N+` threshold series (shots).** One-sided Yes prices, so `find_ladder_anomalies`
cannot read them — but they carry their own exact bound, `P(≥4) ≥ P(≥5) ≥ …`.
Built the check: **1090 series over 219 CB events, 594 over 198 Lider events,
7444 adjacent comparisons, ZERO order violations.** A sample series reads
`{8: 1.5, 9: 1.8, 10: 2.3, 11: 2.95, 12: 3.75}` — generated from one
distribution, so it cannot disagree with itself.

**`Exact bookings` vs the bookings total ladder.** Two *separately priced*
markets, which is the shape that pays — and it showed a 4.45pp median
disagreement, huge against everything else. It is an artifact: the exact
market carries a **1.592 median overround** with **8.3 % of its devigged mass
in buckets pinned at the 30.0 ceiling**. Proportional devigging spreads that
evenly, deflating the short buckets, which is precisely the one-directional gap
observed. Same trap as the old "grid opportunities" (100.0 on 88 552 positions).

**The cards board.** It has exactly the right shape — CB posts TWO independent
3-way results (`Booking 1X2` and `Which team will have the most cards`) plus a
`Handicap cards**` ladder with a 0.00 rung. But they do not co-occur:

```
Booking 1X2                        40 events
Which team will have the most cards 38 events
Handicap cards** (0.00 rung)       39 events
        BOTH 3-way results:         3 events
        3-way + 0.00 rung:          1 event
```

On those 3 the two views disagree by up to **9.9pp** — real, but the
best-of-each cover costs 1.0077 at its cheapest, so **0 locks**.

### The pattern, and the actual constraint

Contradictions live only where a book prices two things **independently**.
Every zero above is a market generated from one number — a ladder, a threshold
series, an incl-OT derivative. Every check that fires is a cross between
separately priced markets: Draw No Bet vs the 0.0 rung, the 1X2 vs the 0.0
rung, the HT/FT grid vs its own legs.

But the binding constraint is not *which* markets get classified — it is how
rarely a book posts two independent views of one quantity **on the same
event**. Corners: 5 (event, period) pairs carry both a corners 1X2 and a
corners 0.0 rung. Cards: 3. That is the ceiling on this whole direction, and
no amount of extra classifiers raises it.

---

## 2026-09-21 — the provider split: LSport, and the New inconsistencies tab

The owner's tip, from bet tickets: CrystalBet runs two odds feeds, one of them
**LSport**, and the mistakes live there — every anomaly/consistency bet placed
came from an LSport match, and sports without LSport (table tennis) are all
noise. The feed is readable off the list view (`data-game-code`, LSport at
~20 M vs the other feed at 63–75 M, zero overlap across 400+ expanded games),
which explains two things this file already recorded without the label: the
tennis "two naming schemes" (B8) and flagged soccer matches carrying a
"median 34 raw markets vs 114" in "reserves / U22 / cups / ITF". Same night:
LSport is 9 % of the soccer board and **13 of 19** soccer flags, 8 % of
basketball and 2 of 3, 46 % of tennis and 2 of 2.

Every check in this file now also runs on `/new_inconsistencies.html`, over
LSport games only, from a separate CB cycle (soccer: 70 games in 12.8 s).
`docs/lsport.md` has the measurement, the cycle and the knobs.

### Measured and NOT built — the uncovered sports (2026-09-21)

Before the provider split was known: 16 sports snapshotted (list + detail),
the same model-free identities run generically (ladders, DNB vs 0.0, voider
vs 1X2, DC vs 1X2, AH ±0.5 twins, HT/FT vs legs, set-sport correct-score /
handicap / exact-sets covers, baseball's extra-innings box), plus an exact
min-cost cover DP over every priced set per game. **Zero locks** anywhere;
cheapest full covers 1.004 (AFL `X @71.5 + DC 12 @1.01`) — CB's 1.01 floor
caps a pinned leg's complement at 0.0099, so a lock needs odds > 101 there.

What did show up, all on the OTHER feed (0 % LSport in every one of these
sports), and none bettable without a reference: baseball prices the tie
twice (`Will there be an extra inning: Yes @~6` vs `1X2 X @~9–10.5`, 18/18
games, 50–63 % apart, the short side wrong); rugby's HT/FT grid is a template
(`X/1` at 13.1 on four different games, shorter than the H1 draw at 14–16.6);
rugby/AFL double-chance templates overprice the dog side against their own
1X2 legs by 20–44 %; AFL Carlton v Richmond W 2-way `Winner '2' @5.60` vs
3-way `'2' @4.60`. And the 1.01-floor artifact: when a favourite pins at
1.01 the other side lands at a fixed ~8.0 regardless of truth (`1/1.01 +
1/8.05 = 1.114`) — every volleyball `2(-2.5)` at 8.0 against a `0:3` at
23–33. Always the short side; never generous. Table tennis: 129 games, all
priced from one distribution to within vig.

Where LSport does have games and no detector yet: futsal (100 %, soccer-
shaped), handball (Brazil women / Luxembourg — 1X2, DNB, AH with a 0.0 rung,
H1, HT/FT: B10/B11/B15/htft verbatim). Volleyball (75 %) is built — see
below.

### B17. Volleyball — `vb_set_match`, `vb_correct_score`, `vb_sets_*` · LSport-only sport

The tennis set checks in best-of-5 plus the exact identities across the sets
markets, on the first sport that runs only in the New inconsistencies cycle.
Measured before building on 15 LSport games (`docs/volleyball.md`): the
1st-set winner is an IID inversion of the match price (median 0.0pp, max
1.3pp); the correct score is a correlated model of it (latent-strength fit
`VB_CS_SIGMA2 = 0.032`, RMS 1.14pp vs 4.07pp IID, every leg 9–21 % under
fair); the only independent seam is the sets handicap against the
correct-score cell it equals. `vb_sets_duplicate` (≥ 8 % apart, row carries
the model's view of the long side), `vb_sets_dominance` (subset shorter than
its superset) and `vb_sets_cover` (locked) are model-free over the six
outcomes, half lines only, 1.01-pinned rungs skipped — and, since
2026-09-22, gated on the leg clearing the model fair by 5 % with the edge as
severity: the first cut's three duplicate rows (8–22 % gaps) sat on top of the
tab with every long side 5–15 % under fair. A row is a bet. Model rows only
inside the fit's range (`VB_CS_MAX_FAV = 0.85`). `total_additivity` is exempt
on set-period sports (`SETS_AS_PERIODS`) — it read set 1 + set 2 as halves.

### Discovery — every LSport match, not a list of sports

2026-09-22. The New inconsistencies cycle sweeps CB's live nav every 30 min,
counts LSport matches on every sport it is not scanning, and activates any
that has some — the dedicated module if one exists, else a generic title-only
LSport classifier (`sports/lsport_generic.py`) that yields ladders and the
sport-agnostic checks. Full census on the day: LSport in seven sports, six
already dedicated, the seventh (sumo) a single 2-way price per bout; 0 on the
other 26. `docs/lsport.md` §4b.

### Futsal and handball — the LSport soccer template, same checks

2026-09-22, `sports/soccerlike.py`: LSport-only, soccer-shaped, read by the
identity checks as-is (`pickem_*`, `htft_combo`, ladders, `total_additivity`,
`favourite_flip`); `half_result_vs_ft` and the E family stay soccer-only. A
prelude classifies LSport's period titles the soccer classifier skips
(`1st Period Winner`, `Asian Handicap 1st Period`, `Under/Over - Home Team`,
`2nd Half 3 Way`). First pass: futsal 8 LSport games, one `htft_combo` at
+3 %; handball 3, one game with two ladder crossings (5.7 %).

## 2026-09-08 → 09-15 — four checks in, five candidates out, and when to watch

### B13. `tennis_correct_score` — the exact set score vs the match price · BETTABLE

CB's tennis correct score was skipped on the belief it "needs a schema change"
(tennis.py docstring). It never did — `selections` is a free-form dict and
`htft` has carried nine keys since soccer. Widening the `MarketType` Literal was
the whole change.

Fair price from the devigged **match price alone**, via a latent-strength
model: sets are independent GIVEN a player's true per-set strength, which is
unknown — `P ~ Beta(μν, (1−μ)ν)`, and every leg is a raw moment:
`P(2-0) = E[P²] = μ² + σ²`. The set-to-set correlation IS the variance; ν→∞
is the old IID `_match_prob_from_set`. Owner's phrasing of the mechanism:
*"when someone wins first it most likely lower odds to win next one too."*

**The first version was wrong** and the owner's doubt caught it. It solved ν
per match from the (1st-set, match) pair — one free parameter per observation,
and the gap's leverage is `3σ²(1−2μ)`, zero at an even match. Near μ = 0.5 the
solver turned ordinary pricing noise into whatever variance closed the gap:
the same −2pp gap implies σ² = 0.116 at μ = 0.55 and 0.012 at μ = 0.80. Filar
v Spierle (μ = 0.573) solved to σ² = 0.121 — 49% of the maximum possible — and
produced "+18.9%" on a leg the pooled model prices at −8.2%. Now **one σ² for
the board**, least-squares on P(match) over 363 events: `CS_SIGMA2 = 0.04026`,
residual RMS 1.60pp. It predicts the 1st-set market it never consumes to a
median |err| of 0.94pp.

Bars, all measured: edge ≥ 8% (p99 +7.49 over 1000 legs); leg odds ≤ 8.0 (the
raw top finding was 0-2 @ 20.60 at "+18.6%"); moneyline legs ≥ 1.05 (Goncalo
v Nunez at 1.01/9.00 is not a price, and vig.py's literal 1.20 floor would
have dropped 30% of the board). **5 flags on 457 events**; the screenshot
that started it (Balazs–Parizzia, 0-2 @ 1.70 vs fair 1.49) was the largest at
+14.1%. Capture: `data/raw/tennis_cs_calibration.json` (fit script lives in
gitignored `scripts/`).

### B14. `duplicate_fixture` — the same match listed twice by one book · BETTABLE · severity flat 100

Rule, and it is the whole rule: **identical kickoff and the same two teams**
(≥ 80 fuzzy, sides may be swapped). League plays no part — the same match under
two league names is exactly the case worth catching. **No youth or women
guard.** Measured on 238k tick-store events: a guard blocked 11 pairs
board-wide, 3 on CB/Lider, and those three were the mislabellings the check
exists for (`FCI Tallinn II` vs `Fci Levadia Tallinn U19`). Two different
fixtures between the same teams do not share a kickoff minute.

History says it is real: **CB 128, Lider 33 over ~3 months**, ≈ 1.8/day.
Shapes: two league names (`Club Friendly Games` / `Clubs`), sides swapped
(`Rostov v CSKA` / `CSKA v Rostov`), two spellings (`Dane Sweeny` / `Sweeny
D.`). Swapped listings must be **realigned before covering** — the first
version reported all of them "prices not comparable". Simulated/e-sports
leagues must be excluded: 16,631 events, and they are why xbet shows 10k and
betlive 3.4k against your books' 128 and 33.

Severity is a **flat 100** by owner instruction. Scoring the locked edge put an
identically-priced pair at −5.3 (the book's own overround); scoring the
disagreement put it at 0. Neither clears a bar; both silenced exactly the
duplicates worth hearing about.

### B15. `half_result_vs_ft` — a half's 1X2 vs the full-time 1X2 · tripwire, 15pp

Found by the owner: Belarus U19 v Gomel Region, full-time 1.10/6.90/14.6,
first half 1.45/2.85/11.8, second half **1.90/3.20/3.50** — away 25% to win
the half against 6% for the match. A goal model fitted only to the full-time
market reproduced H1 to 0.0pp and missed H2 by 18pp. It could not fire: **CB's
second half was never classified** ("Pinnacle ships no H2 soccer markets" —
the +EV pipeline's concern, not this one's; third time that reasoning hid a
CB-internal contradiction). Now on the PERMISSIVE path only; strict still
skips it. Period totals write the number first (`0.5 Und`) and needed a label
swap in `cb_detail`.

**The model had a systematic bias**, the same on every book: closing-line
posted − model, median pp — H1 Pinnacle (2763) draw **−1.32**, home +1.00; H2
Lider (2827) draw **−1.79**; Crocobet agrees to a tenth. Fixed-split Poisson
overstates half-draws. The owner proposed learning FT→half from well-priced
data instead. Tested head-to-head on held-out Pinnacle: the pure empirical
1-D curve removes the bias but has **wider tails than raw Poisson** (home p1
−4.26 vs −1.60) — a map on FT-home alone cannot see totals. **Poisson + a
learned bias curve** (`soccer_model.HALF_BIAS`: Pinnacle for H1, Lider+Croco
for H2) wins on every outcome, both halves — H1 |mean| 1.21 → 0.72, H2 0.92 →
0.36 — and transfers to CB unseen (p99 5.17; Setanta 4.81). "Top leagues only"
changed nothing (5.10 vs 5.17). Cross-book H2 bar needs no CB history:
Lider → Crocobet p99 2.22.

The bar is **15pp**, deliberately high. Owner's example "ml 1.50 / ht ml 3.50"
= 18.8pp; Belarus = 22.4pp; the largest gap in three months of normal pricing
was **12.6pp** (CB H1) / **6.1pp** (Lider H2). A 6pp bar (just past p99) was
correct as statistics and useless as a product: it fired the H1 draw of
lopsided national-team matches. Both halves on, alerting.

### B16. `duplicate_live` — the same LIVE match listed twice · severity flat 100

`src/live_duplicates.py`, loop `_live_duplicate_loop`, `live_dup_sec` 300 s
(floor 60 — a tripwire, not a feed). CB + Lider, every sport, **enumeration
boards only**: Lider is one GET (~4 MB, 0.3 s); CB is GET → English flip →
`ShowAllStarted` (~500 KB). No per-match calls. Anchor is the game state —
same teams, same score, same period (swap-aware); books never cross.
`live/docs/crystalbet-live.md` is **stale**: rows are `div.d_row` with
`doGameOpenPost(id)` under a `sportTypeId` container, not `div.game_info`.
Live: 94 CB + 108 Lider matches in 1.5 s, 0 duplicates on a normal board.

### Measured and NOT built — CB side markets (2026-09-08)

All on 851–1102 live CB soccer events, all zero:

| candidate | result |
|---|---|
| HT/FT 9-cell marginals vs the 1X2 legs | 844 events, 5064 comparisons, **max 3.81pp**, 0 covers < 1.0 — consistent with the 2026-08-19 grid-identity control (median 0.80pp). The grid is derived with correct marginals and mis-specified dependence; `htft_combo`'s correlation bound is already aimed at the only part that carries signal |
| 2-way combo family (`win or under 2.5` ×6) | 851 events, 0 arbs, best cover 1.0289 |
| Correct score × totals ladder | 851 events, 0 arbs, best 1.0054 — structural, `Over 0.5` alone costs 0.9901 |
| Double chance × 1X2 | 0 dominance violations, best cover 1.0289 |
| BTTS × totals | 0 violations; `BTTS Yes ⊆ Over 1.5` never binds |

The catalog's own rule, re-learned: CB prices its side markets from one
distribution. The exception (B15, the second half) was never in this test set
because it was never ingested. A first combo run reported three "locked arbs"
at 0.7619 — the total-line finder had grabbed *Under/Over Corners 2nd Half*;
a 31% risk-free return is the tell.

Flagged CB matches carry a **median 34 raw markets vs 114 board-wide** and are
reserves / U22 / cups / ITF. The 1,006 unexpanded soccer events are **policy**
(`cb_expand_within_hours` = 24 for the poll; detectors run at
`anomaly_extra_horizon_h` = 70), not a gap; far-out events are equally rich
(median 114). `cb_expand_max_sec` = 300 is **not binding** — `past_budget` = 0
on every sport, soccer uses 69 s. Tennis `list_fallback` = 38% (183/485) vs
soccer 2.4% is the thing worth looking at.

### When opportunities appear — CB soccer, 3 weeks, replayed from ticks

Soccer opportunities were never logged (`output/history/*` is basketball-only).
Replayed 11,639 CB events matched to Pinnacle, first crossing of ≥ 3% edge,
**per 100 CB polls** because uptime ran 12–24 h/day. Local time (+04).

- **09:00–21:00 = 142.6 per 100 polls; the owner's 15:00–03:00 = 88.5.**
  15:00–20:00 is excellent (145–177), then decays; **00:00–04:00 is the dead
  zone** (20–43). Hours 08–12 have < 40 polls each — direction solid, numbers soft.
- Days: Sat **140** > Sun 106 > Fri 102 > Wed 86 > Tue 71 > Thu 57 > Mon 48.
- **Median 4.3 h to kickoff** at first appearance, 40% inside 3 h, 7% beyond
  a day. The clock pattern is really "when matches are 1–5 h out".
- Morning leagues: Hong Kong (×3), New Zealand, Japan, Bulgaria, Norway U19,
  Sweden Div 2, Denmark. South America (Honduras reserve, Argentina Primera C
  reserves, Bolivia, Dominica) peaks late evening — already inside the window.
  Top league overall: England FA National League – Women.
- Markets: spread 704 > moneyline 554 > total 245.

### Noted, not built

- `htft_fair` emits EDGE and SHAPE under one `kind`. SHAPE severity is
  `|ratio − 1| × 100`, so a 2/2 at 28% against a model 16% shows as **80** — a
  diagnostic wearing a bet's number. The book had priced "away leads at half
  and wins" at ≈ P(away wins). Split into `htft_fair` (EDGE) / `htft_shape`
  (default-off), the way `ml_vs_spread` sits.
- Lider-Bet has no DNS pin; `xbet.py::_resolve_ip` is the precedent. A startup
  wedge on 2026-09-08 cost an 18-minute hole in the combo tab.
- `half_result_vs_ft` H2 bar could tighten toward ~2.5pp once CB second-half
  history accumulates (classified from 2026-09-14).
- Alert sounds: both anomaly chimes were pure plucks (instant attack, decay
  across the whole slot) and the consistency bend ended at 294 Hz. Now hold
  then release; bend moved to 660→440 Hz with an octave partial.


---

## 2026-09-23 — `pickem_dominance` was dead: the bound was 1.0, the identity is `1 − P(draw)`

Owner, on a `Pinheiros — Cascavel` row: *"scanner should detect something like
on the screenshot which it didnt, or did on very low level when there was like
very good arb/ev bet possible."*

Right on both counts, and it is the same defect twice.

### The check enforced a much weaker bound than the one it documents

The relation is exact on fair prices:

```
AH0(side) / X12(side)  ==  1 − P(draw)
```

which is about **0.75** on a normal board. The code triggered on
`ratio >= 1.0` and scored `(ratio − 1) × 100`. So a voider at 0.95 — already
**25% longer** than the 1X2 puts it — was silent, and one that did cross 1.0
was scored from the wrong origin.

Measured on `data/ticks.db`, 4 608 CrystalBet soccer sides whose 1X2 and 0.0
rung came from the **same fetch** (9 615 events dropped on >120 s skew):

| | p1 | p50 | p99 | max |
|---|---|---|---|---|
| `ratio` | 0.651 | 0.736 | 0.850 | **0.900** |
| `ratio / (1 − P(draw))` | 0.851 | 1.006 | 1.048 | 1.153 |

**The ratio never reached 1.0. The check fired zero times.** Meanwhile 28
sides sat 5%+ above their own identity and none was visible.

### The identity does not hold on raw prices, and the error runs with price

A 2-way voider and a 3-way 1X2 carry different margins, so the residual is
not 1.000 — and it is not constant either:

| voider price | n | median residual |
|---|---|---|
| 1.0–1.2 | 411 | **1.038** |
| 1.2–1.5 | 1044 | 1.027 |
| 1.5–2.0 | 1236 | 1.012 |
| 2.0–3.0 | 1162 | 0.989 |
| 3.0–5.0 | 701 | 0.940 |
| 5.0+ | 54 | **0.826** |

Monotone across the whole range — the book's margin is worst on longshots. A
flat bound cannot serve both ends: at 1.05 the short-favourite band
contributes 15 rows of pure noise, while a 6.30 voider has to be 27% out
before anyone notices. So the score is taken against `PICKEM_DOM_CURVE`, and
the banded residual is tight enough to threshold on — p50 0.996, p90 1.009,
p99 1.021.

### Result

`PICKEM_DOM_Z = 1.08`, roughly four times the p99 excess:

| | fires on 4 608 sides |
|---|---|
| before (`ratio >= 1.0`) | **0** |
| after (banded) | **14** (0.30%), all mid-to-long prices |

and the screenshot row goes from **13.3 → 46.7**, which is what
*"did on very low level"* meant. The hard case survives inside the new one —
a ratio at or above 1.0 scores far above the band wherever it lands — and the
detail text still says which of the two it is, because *"the better bet cannot
be the longer price"* needs no calibration and *"short of its band"* does.

The top of what was invisible:

```
sev 29.0   6.30 vs 7.00   Deportivo Espanol Res — Centro Espanol Res
sev 26.4   6.00 vs 6.80   Atletico Vega Real FC — Jarabacoa
sev 23.2   5.15 vs 6.30   Singida Black Stars — Polisi Tanzania FC
sev 20.8   3.55 vs 4.00   WFC Osijek W — Donat Zadar W
sev 20.5   4.75 vs 5.55   Thitsar Arman U20 — Hantharwady United U20
```

### The note that got it wrong

The original calibration measured the residual at a median of +0.003 and
concluded *"the bound needs no slack: it is exact."* That is an argument for
measuring **against** `1 − P(draw)`, not for keeping a bound at 1.0 that
nothing ever crosses. A tight residual is what makes a check sensitive; it was
read as a reason not to build one.
