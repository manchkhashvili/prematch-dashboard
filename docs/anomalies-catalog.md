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

## Family B — CB internal consistency (`src/consistency.py`) · DIAGNOSTIC (one sub-check bettable)

Contradictions between CB's **own** markets for the same game — across market
types (ML vs handicap) and across periods (halves/quarters vs full time).
Everything derivable from CB alone. `CONSISTENCY_SPORTS = (basketball, soccer)`.
Thresholds deliberately sit **well above** normal period-to-period variation
(calibrated on a clean NBA game 2026-05-31: ML-vs-spread agreed <0.5pp, period
totals summed within 0.5pt).

### B1. `ml_vs_spread` — ML win-prob vs handicap-ladder win-prob
- Within one period, `|P_home(ML) − P_home(spread @ line 0)|`. Flag ≥ **5.0pp**
  (`ML_SPREAD_GAP_PP`). P(win) read **only** from a true pick'em (line 0.0)
  rung — no extrapolation (extrapolating to 0 outside a favourite's ladder
  fabricated fake gaps; interpolating across ±0.5 mixes tie conventions).

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

### B5. `ht_vs_ft_divergence` — half vs full priced very differently
- Home win-prob swings ≥ **13.0pp** (`HT_FT_DIV_PP`) between H1 and FT. Uses the
  2-way ML where present, else the 3-way 1X2 home prob (sport-agnostic). This is
  the **HT/FT value zone** — e.g. River Plate ~80% FT favourite but ~65% at the
  break (~14pp). Added this session; the entry point into HT/FT research.

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
  `basketball_fav`→"basketball fav disagreement".

## Env-var quick map
`ANOMALY_SCAN` (CB ladder + consistency) · `BETLIVE_ANOMALY` (OT-fold watch) ·
`SOFT_SCAN` + `SOFT_SCAN_SEC` (soft sweep) · `SOFT_HTFT_ML_RATIO` (D1 vs-ML
threshold, default 1.35) · `CB_TRANSPORT` / `CB_ANOMALY_TRANSPORT` (http vs
Playwright).

## Confidence ranking (for research triage)
1. **A (ladder monotonicity)** — hard, bettable, single-book. Trust most.
2. **C (Betlive OT-fold)** — hard structural bound, single-book, but short-lived.
3. **B6/B7 (HT/FT combo & model)** — model-light→model-heavy EV claims; the
   research core.
4. **B1–B5 (consistency diagnostics)** — "go look", not EV.
5. **D2 (basketball fav)** — structural disagreement, promising, needs outcome data.
6. **D1 vs-ML (soccer HT/FT)** — exploratory screen; likely normal pricing.
