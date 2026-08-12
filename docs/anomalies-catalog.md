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

### B9. `ot_vs_regulation` — the incl-OT winner vs the regulation 1X2 · **AMERICAN FOOTBALL** · BETTABLE direction exists
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
legs. Only fires inside the bettable range `1.15 ≤ odds ≤ 4.5`. Two bounds:
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
