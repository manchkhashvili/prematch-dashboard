# Findings — 2026-06/07 (multi-book, anomalies, capital)

Operational discoveries worth keeping. Terse on purpose; see the cited code for
detail.

## CrystalBet — HTTP transport English flip is flaky at boot
- The browser-free transport (`cb_http.py`) flips the site to English via an
  `ImageButtonEn` full postback in `warm()`, but never **verified** it took.
  When several sport sessions warm **concurrently at boot**, CB (under load)
  silently drops the flip for some sports → that session serves **Georgian**
  team/league names for hours (soccer flipped, basketball+tennis stuck). Each
  session warms once, so it never self-heals.
- Fix: `fetch_list_html` now checks the games panel for Mkhedruli chars
  (`georgian_chars()`; a flipped panel has ~0, an unflipped one hundreds) and
  **re-warms once** if Georgian. The full warm page keeps ~3450 Georgian glyphs
  (left tree) even when flipped, so verify the **panel**, not the whole page.
- `CB_ANOMALY_TRANSPORT` (default `http`): the hourly anomaly scan runs
  browser-free even when the main app is on Playwright — ExpandDetail ≈ 355 KB /
  0.3 s per game vs seconds of browser nav.

## CrystalBet basketball — HT/FT market title conventions vary by league
Real titles from Argentina La Liga Federal (`River Plate vs Urquiza`), which
broke the assumptions baked for FIBA/NBA:
- The half result is titled **`1X2 Halftime`** (not `1st half - 1x2`).
  `_derive_period` didn't know "halftime" → it (and `Asian Handicap Halftime`,
  etc.) silently defaulted to **FT**, corrupting the FT view and leaving H1
  empty. Fixed: added `\bhalf ?time\b → H1` to `_PERIOD_PATTERNS`.
- The combo is **`HT/FT Including Overtime`** (abbreviated + incl-OT), not
  `Halftime/Fulltime`. Broadened `_RE_HTFT_TITLE` to match `ht/ft…` / `half time
  / full time…` as a **prefix**, excluding combo-of-combos (`… and Total`,
  `… Correct Score`) via `_RE_HTFT_EXCLUDE`.

## HT/FT combo fair pricing (consistency.py)
- The naive independent product `odds(HT) × odds(FT)` **over-states** the fair
  1/1 combo: conditioning on the HT lead makes the FT leg shorter than its
  unconditional price, so the true fair sits between `max(leg)` and the product.
- Model now used (owner's): `fair = HT_leg × (1 + (FT_leg − 1)/2)`, clamped at
  the longest leg. River Plate: 1.35 × 1.07 ≈ 1.44 → a 1/1 @ 1.55 reads ~7%
  generous where the product (1.539) saw only 0.7% and missed it.
- New `ht_vs_ft_divergence` flag: home win-prob swings ≥ `HT_FT_DIV_PP` (13pp)
  between HT and FT (the HT/FT value zone). Uses the 2-way ML or the 3-way 1X2.
- The consistency engine is no longer basketball-only — `CONSISTENCY_SPORTS =
  (basketball, soccer)`; soccer got a permissive HT/FT classifier. Quarter/
  bivariate-model checks stay basketball-only.

## Betlive favourite-flip anomaly (`betlive_anomalies.py` / `betlive_watch.py`)
- Basketball/hockey price a 2-way **incl-OT** winner AND a 3-way **regulation**
  result that can disagree on the favourite. Folding OT in can only ADD a side's
  win prob, so devigged `P(side, incl-OT) ≥ P(side, regulation)`; a violation is
  a model-free single-book anomaly (usually a swapped home/away).
- These errors are **short-lived** (VBA game self-corrected in ~15-30 min), so
  detection latency matters. Two-speed watch: slow `discover()` (full ladders,
  records the 5 ML `outcomeId`s/event) + fast `/api/outcome/refreshOdds` (POST
  `[{outcomeId,eventId}]`, ~50 KB for the whole board) recomputing the OT-fold
  check every few seconds. Opt-in `BETLIVE_ANOMALY=1`; flags land in the
  Anomalies tab consistency list.

## Capital / PnL model (capital.py)
- **Withdrawal commission is GROSS**: 6% grossed up = `6/(100−6)` = 6.38 on 100.
  `transfer(book→bank)` books the pair balanced + a separate `commission` ledger
  row; `withdrawal_rate()` also drives the cash-out valuation. Fee only when
  source has a `book_tag` and dest doesn't (bank).
- Book detection = `book_tag`. An untagged account (e.g. a "Liderbet" that
  defaulted to NULL) is treated as the bank → no fee. Tags are now **editable**
  (`set_book_tag`) and **free-form** (any book is first-class; `KNOWN_BOOKS` is
  advisory only).
- **Dividend** accounts (`is_dividend`) = profit pulled OUT; excluded from
  equity/Total, surfaced as `dividend_total`. Transfer Bank→Dividend to take one.
- **Manual bookkeeping** (bet a lot, don't log each bet): `set_balance()` books
  the delta as a `pnl` ledger row → flows into settled PnL / ROI / net-PnL /
  curve, but is **excluded from yield** (no turnover). `set_open_exposure()`
  sets `manual_open_stake` — moves cash from free balance into exposure (NOT
  PnL). UI: `＄ bal` / `⧗ exp` buttons per account.
- `pushed` (and `void`-like) bets are excluded from yield turnover; `Fees paid`
  card = commissions actually paid; PnL shown net of fees.

## Soft-book HT/FT + basketball-favourite sweep (soft_scan.py, SOFT_SCAN=1)
A slow 60-min filtered sweep across CB + Betlive + Lider-Bet, separate from the
CB basketball ladder scan.
- **Soccer HT/FT** (htft_favourite): for a heavy favourite the HT/FT combo (1/1,
  2/2) is ~1.0× its first-half leg (measured 0.97–1.03 across a live board), so a
  combo >= **1.2×** the leg is a soft/generous line. Favourite = side < 1.30 OR a
  side with NO price (an omitted favourite price = an extreme favourite). Skip top
  leagues (World Cup / UEFA / big-5). Real cases seen at H1 1.1 → HT/FT 1.4 (1.27×).
- **Basketball favourite disagreement** (basketball_fav): the devigged home-win-prob
  (draw dropped) must be close across the 2-way (incl-OT), 3-way (regulation) and HT
  moneylines; a flip or >=15pp spread flags. Extends the betlive OT-fold idea.
- **Per-book gotchas (validated live 2026-07):**
  - Betlive labels HT/FT outcomes **"1 / 1" with spaces** (strip spaces before
    matching "1/1"). Its 3-way basketball result is NOT marketId 1 — match by
    `{1,X,2}` labels. Exclude **lined markets** (a "Points Spread (OT)" also carries
    "1"/"2" labels and was masquerading as the moneyline → 7 false flags).
  - Lider-Bet full ladder is `matchData/details?matchIds=` (the plain `matchData`
    list is a curated 33-market subset with no HT/FT). It has HT/FT but **no
    standalone 1st-half result** (only combos: "1st Half Result *or* Match Result",
    "…/1X2"), so its soccer HT/FT no-ops gracefully; it still contributes basketball.
  - CB soccer HT/FT title varies — regex broadened to the HT/FT prefix (like bball).
- **Energy per sweep (filtered):** CB ~17 MB/30 s, Betlive ~7.6 MB/6 s,
  Lider-Bet ~4.5 MB/0.6 s (batched details). ~30 MB/40 s once an hour.
- The fuzzy market-name matchers are validated against live data; a book renaming a
  market would silently miss it — re-run the coverage probe periodically.

## Arbs/EV — match confidence
- Soft-book↔Pinnacle pairing is name+time fuzzy (Pinnacle has no SR id), so a
  high name score can still be the wrong game (same-named lower divisions →
  fake +145% edges). `edge.match_confidence()` combines name score + kickoff gap
  + price sanity (implausibly large edge ⇒ likely mismatch) → strong/medium/weak.
