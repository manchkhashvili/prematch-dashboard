# Prematch Odds Dashboard

A local web dashboard that scrapes [CrystalBet](https://crystalbet.com) prematch
odds and compares them against [Pinnacle](https://www.pinnacle.com) as a sharp
reference, surfacing **+EV** and **arbitrage** opportunities across basketball,
soccer, tennis, and American football.

Status: **research preview, single-user.** Tested daily against live books;
not production-hardened.

---

## What it does

- Scrapes CrystalBet's `Sports.aspx` prematch pages via headless Playwright
  (basketball / soccer / tennis / American football), parsing moneylines,
  spreads, totals, team-totals, and corners markets.
- Fetches Pinnacle's guest API for the same sports (sport ids 4 / 29 / 33 / 15
  — Pinnacle calls American football "Football" and soccer "Soccer").
- Matches CB events to Pin events by fuzzy team name similarity + start-time
  proximity (Phase 3 fuzzy normalization handles tennis player-name format,
  Georgian-to-Latin transliteration, GitHub Primer light/dark theming).
- Devigs Pinnacle's posted prices using **Shin's method** (Phase 3.7 — replaced
  the proportional default after we caught it overstating dog probabilities
  on skewed lines).
- Computes per-side edge%, quarter-Kelly stake suggestions, and ARB
  opportunities (1/d1 + 1/d2 &lt; 1).
- Serves a vanilla-HTML dashboard at `http://localhost:8000` with five pages:
  Matches / Arbs / Bets / Calc / Unmatched.
- Tracks placed bets in SQLite, snapshotting CB and Pinnacle fair odds every
  poll so you can see CLV (Closing Line Value) per-bet via inline sparklines.
- Cross-page sound alert when a new opportunity clears the gates you set —
  edge, probability-point edge, odds range, Kelly range, Pinnacle limit,
  kickoff window, plus sport/book/market/period/confidence filters — with the
  seen-set persisted in `localStorage` so navigation never replays.

---

## Stack

- **Python 3.11+** (3.10 also works)
- **httpx** for Pinnacle's guest API
- **playwright** + Chromium for CrystalBet's ASP.NET WebForms pages
- **FastAPI** + **uvicorn** for the local server
- **sqlite3** (stdlib) for the bet tracker
- **rapidfuzz** for team-name matching
- **pyyaml** for the manual team alias overrides
- Vanilla HTML / CSS / JavaScript on the frontend — no React, no build step

---

## Quick start

```bash
# 1. Clone + create venv
git clone <your-repo-url> prematch
cd prematch
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install Python deps + the Chromium browser Playwright needs
pip install -r requirements.txt
playwright install chromium

# 3. Run the dashboard
python main.py
# → http://localhost:8000
```

The first cycle is a cold-start (~30-60 s per sport). Subsequent cycles use
the on-disk change cache and are typically &lt;30 s.

### One-shot CLI mode (dev / iteration)

```bash
python main.py --once             # one cycle, print table to terminal, exit
python main.py --once --cb-saved  # parse the saved HTML sample instead of scraping
python main.py --once --min-edge 5
```

### Common runtime flavors

```bash
# All sports in list-only mode — lightest config, ~30 s/cycle
SPORTS=basketball:list,soccer:list,tennis:list,americanfootball:list python main.py

# Basketball + American football with full detail expansion, the rest list-only.
# AF is affordable in full mode: ~180 games, ~80 s for a whole-board detail sweep
# (tennis is 500+ games, hence list).
SPORTS=basketball:full,soccer:list,tennis:list,americanfootball:full python main.py

# Basketball only, full
SPORTS=basketball python main.py

# With tee'd log for later debugging
SPORTS=basketball:list,soccer:list,tennis:list python main.py 2>&1 | tee dashboard.log
```

---

## Runtime config (Config tab)

Everything that used to need a restart is switchable at `/config.html`:

| Section | What |
|---|---|
| Everything | **Pause / Resume** — idles every poll loop (all books, Pinnacle, every scan) while leaving the server up, so you can resume from the page instead of killing the terminal. See below. |
| Books | `crystalbet`, `liderbet`, `betlive`, `crocobet`, `setanta`, `xbet` (Pinnacle is the reference — always on) |
| Scans | `anomaly`, `anomaly_extra`, `anomaly_watch`, `betlive_anomaly`, `soft_scan` |
| Cadence | per-loop poll intervals, clamped server-side to a safe range |
| Limits | CB anomaly horizon, Setanta / Crocobet full-ladder horizons |

API: `GET /api/config`, `POST /api/config` (partial, e.g.
`{"books":{"setanta":false}}` or `{"paused":true}`), `POST /api/config/reset`.

**Why it saves real money:** the scans are the expensive part — a cold
CrystalBet full-detail soccer sweep is ~278 s of CPU (html5lib + bs4). Books
each cost 0.3–1.4 s CPU per cycle plus their bytes. Switching off what you
aren't watching stops that work at the source rather than throttling it.

Precedence: `data/runtime_config.json` (written by the UI) wins over env vars;
env only seeds keys that have never been set. `POST /api/config/reset` drops
back to the env-seeded defaults.

---

## Environment variables

| Var                        | Default       | What it does |
|----------------------------|---------------|--------------|
| `SPORTS`                   | all-full      | Per-sport mode: `sport:mode` comma-separated. Modes: `full` (CB+Pin+detail), `list` (CB list-view only, no alt-lines), `off`. Example: `SPORTS=basketball:full,soccer:list`. |
| `MAX_START_DAYS`           | 7             | **Global data horizon.** No book fetches, parses or emits an event starting more than this many days out — applied in all seven scrapers and bounding every per-book horizon (the tighter wins). `0` disables the cap. Live-adjustable on the Config tab as `limits.max_start_days`; see `src/horizon.py`. |
| `OPP_REVERIFY_SEC`         | 120           | Re-pull CB detail for the games currently showing an opportunity, so an edge is confirmed on a fresh price. `0` disables. See "Stale prices" below. |
| `OPP_REVERIFY_MAX_GAMES`   | 25            | Cap on the re-verify shortlist (edge-sorted, so the biggest claims are kept). |
| `OPP_REVERIFY_MIN_EDGE`    | 3.0           | Only re-pull games whose edge is worth acting on. |
| `ANOMALY_EXTRA_MAX_SEC`    | 240           | Wall-clock budget per sport per ladder-scan pass. The scan holds the CB **per-sport lock**, so this is what stops a wide `anomaly_extra_horizon_h` from wedging the sport's price poll and re-verify loop (measured: 47 min, see `docs/performance.md`). Games are expanded soonest-kickoff first; the rest keep their list-view Odds. Live as `limits.anomaly_extra_max_sec`. |
| `PINNACLE_POLL_SEC`        | 60            | Pinnacle poll cadence per sport. |
| `CRYSTALBET_POLL_SEC`      | 60            | CrystalBet poll cadence per sport (was 180 in the Playwright era; a browser-free list cycle is ~1-2s). |
| `CB_TRANSPORT`             | http          | CB byte-mover: `http` (browser-free ASP.NET postbacks via curl_cffi — ~10× faster detail, no Chromium) or `playwright` (browser, escape hatch if CB changes the postback protocol). Same data either way — parity-verified 2026-06-12 and re-verified 2026-07-28 (soccer 1017/1017 games + 311/311 markets, basketball 515/515, zero structural diffs): `scripts/cb_parity_check.py`. Note the browser path resolves DNS itself and so bypasses the `dns_pin` fix in `cb_http.py`. |
| `CB_HEADLESS`              | 1             | `1` = headless Chromium, `0` = headed (useful for debugging selectors). Playwright transport only. |
| `CB_USE_SAVED`             | 0             | `1` = parse saved HTML instead of scraping. Dev mode. |
| `LIDERBET`                 | 0 (off)       | `1` = add Lider-Bet as a soft book (browser-free JSON; see `docs/liderbet.md`). Matched vs Pinnacle like CB (filter by book on Arbs) and cross-book on the Cross-book tab. |
| `BETLIVE`                  | 0 (off)       | `1` = add Betlive as a soft book (browser-free JSON behind Cloudflare; see `docs/betlive.md`). List view is moneyline-only for now. |
| `EXTRA_BOOK_POLL_SEC`      | 120           | Poll cadence for Lider-Bet / Betlive (cheap JSON books). |
| `HOST`                     | 127.0.0.1     | uvicorn bind host. |
| `PORT`                     | 8000          | uvicorn bind port. |
| `BETS_DB_PATH`             | `data/bets.db`| Override the SQLite path for the bet tracker. |

Legacy: `ENABLED_SPORTS` and `CB_SKIP_DETAIL_SPORTS` still work; `SPORTS`
supersedes them when set.

---

## Dashboard pages

- **`/matches.html`** — one row per CB match. Columns: start time, sport,
  league, home, away, CB odds, Pin odds, edge% per side, plus a `[+]`
  expander showing every market for that match (ML, spreads, totals, H1
  versions, team totals, corners) with CB and Pin side-by-side.
- **`/arbs.html`** — opportunities sorted by edge%. `+EV` rows are bets where
  CB's price beats Pinnacle's no-vig fair; `ARB` rows lock in a guaranteed
  profit across both sides. Click any row to deep-link into the matches page.
  Right-side action buttons: `Log` (prefill the bet form), `Mark` (see below),
  `★` (manually highlight in amber), `−` (mute and dim). A book filter (CB /
  Lider / Betlive / All) appears when extra books are enabled.
- **`/cross-book.html`** — best price per market across your soft books, with
  Pinnacle's no-vig fair as the reference: `Edge%` = best book vs Pinnacle fair
  (**+EV**), and an `Arb` badge when the best opposing prices across books lock a
  profit. Only populated when `LIDERBET=1` / `BETLIVE=1` (and/or CB) are on.
- **`/bets.html`** — placed bets table with live CB-now / Pin-fair-now / edge
  evolution. Inline sparkline of Pin fair over time per open bet. Settle
  buttons (Won / Lost / Pushed / Void / Delete). A **date filter** ("Placed
  between", with Today / This month / All time presets) scopes the table *and*
  the Capital & PnL block to the same window, so picking a fresh start date
  reads zero across both. Two modes sit beside the dates:
  **PnL** windows bets only (money columns stay all-time and current — "how did
  my betting do in July"), while **Start fresh** windows bets *and* ledger
  transactions so an empty period reads zero everywhere, per-account rows
  included. Both are views: nothing is deleted, and clearing the filter returns
  the full picture.
- **`/anomalies.html`** — two tables of single-book findings that need no
  reference price. **Ladder anomalies**: rungs where an alt-line ladder crosses
  itself (home gets more points but its odds get *longer*) — structurally
  impossible, so one of the two rungs is mispriced. **Consistency flags**: a
  book contradicting itself across markets or periods — basketball, soccer,
  **tennis** (a first set priced richer than the whole match is structurally
  impossible in a best-of-3) and, since 2026-08-12, **American football**, where
  the incl-overtime winner must sit between the regulation win probability and
  regulation-win-plus-tie. See
  `docs/anomalies-catalog.md` for every detector, its math and its thresholds,
  and "Anomaly alerts" below for chiming on them.
- **`/calc.html`** — two calculators. **Devig + +EV**: Shin or proportional
  toggle, edge% and quarter-Kelly stake. **Half-time & HT/FT from the full-time
  market**: enter only the FT 1X2 and one total line and it derives fair prices
  for the half-time result, half-time totals and the whole 3×3 HT/FT grid —
  the half-time price is deliberately *not* an input, because it is usually the
  market the book does not post or posts lazily. Book prices are optional per
  market; supply the ones you have and it shows the edge. Inputs persist via
  `localStorage`. See "Half-time from full-time" below.
- **`/unmatched.html`** — CB events that didn't match any Pin event +
  their best below-threshold candidate. Useful for curating `team_aliases.yaml`.
- **`/config.html`** — **live** on/off switches for every soft book and every
  scan, plus poll cadences and work-horizon limits. Changes apply within ~5 s
  without a restart: a book you switch off does not fetch, parse, write ticks
  or run its ladder-anomaly pass, and its odds are cleared so nothing
  downstream prices off a frozen snapshot. Settings persist to
  `data/runtime_config.json`; env vars only seed the *initial* values, so an
  existing deployment behaves exactly as before until you change something.
  See "Runtime config" below.

---

### Mark a game (`Mark`, next to `Log`)

`Log` records **one position** as a bet. `Mark` records that you have money on
the **fixture** — optionally a total across however many positions and books —
and highlights that game's row on every board page. It carries no odds and no
settlement, and never enters PnL, capital or CLV.

Click `Mark` → optional amount → the row gets a green stripe everywhere the game
appears, and the button reads `✓ 450`. Click again to change the amount; enter
`0` to remove it. Stored in `game_marks` in `data/bets.db`; marks for games that
kicked off more than 2 days ago are pruned automatically.

Marks are keyed on `pin_event_id` where a row has one — that is the real
cross-book identity, since every book is matched against Pinnacle, and one
fixture can appear under three different spellings from three books. Rows with
no Pinnacle counterpart (consistency flags, CB-only games) fall back to a
normalized name key, which is also stored as an alias so a mark set from one
page is found from another. Known gap: two rows that *both* lack
`pin_event_id` **and** spell the teams differently (`Seinajoen JK` vs `SJK`)
still key apart.

API: `GET /api/marks`, `POST /api/marks`, `DELETE /api/marks/{game_key}`.

### Pause everything

Config tab → **Pause everything**. Every poll loop idles; the web server keeps
serving so you unpause from the same page. Measured: CPU ~100 % → ~0.2 %.

Pause is a top-level override and never touches the book/scan switches, so
resuming continues exactly as before — there is no prior state to restore. It
also does not clear any odds: the board stays as you left it and goes stale
(the per-page status ages keep counting up), rather than emptying.

Two things to know:

- **Cooperative.** Work already in flight finishes; long CB detail sweeps abort
  at their next progress checkpoint rather than running all 400+ games.
- **Persists across restarts.** Pausing then closing the laptop will not
  silently resume — a fresh start stays paused, with the Config section red,
  until you press Resume.

### Opportunity alerts (Arbs tab → **Alert settings**)

This used to be one box: `Alert ≥ N%`. The problem with an edge **percentage**
on its own is that the biggest percentages sit on the longest prices, which is
exactly where Pinnacle's fair number is least certain and where the Kelly stake
is smallest — so the alert was loudest where it was least useful. A real
example from the live board: a Lider-Bet row at **+3.79 % on odds 4.80** is
worth **0.79** probability points and a Kelly stake of **2.5**, while a
**+5.30 % on odds 2.00** is worth **2.63pp** and a Kelly of **40**.

Every criterion is a **gate**: a row must clear *all* the ones you fill in, and
a blank box is ignored. (Deliberately the opposite of the anomaly alerts below,
which fire if *any* criterion is met — there you are casting a net, here you
are narrowing one.)

| Criterion | What it reads | Why you'd use it |
|---|---|---|
| `Edge ≥` | the `Edge%` column | the original threshold |
| `Prob. edge ≥` | `(1/fair − 1/book) × 100` | longshot-proof: ranks a 2.00 over a 10.00 correctly |
| `Re-alert after +` | growth since the last chime | was hardcoded at 5pp |
| `Odds` min→max | the book's decimal price | skip prices you would never take |
| `Kelly` min→max | quarter-Kelly stake | "only if it is worth real money" |
| `Pin limit ≥` | Pinnacle's `maxRiskStake` | how much the sharp book stands behind its own price — the best single quality signal in the row |
| `Kickoff in` min→max | minutes / hours to start | a game six days out will move many times; two minutes out may be unplaceable |
| Kind / Confidence / Sport / Book / Market / Period | chip groups | scope |

Two behaviours worth knowing:

- **Nothing selected in a chip group means all of it.** Unticking the last chip
  widens the alert rather than silencing it (silencing is what the master
  switch is for), and a sport or book added to the backend later keeps alerting
  instead of quietly dropping out.
- **ARB rows ignore the Kelly gate.** `edge.py` leaves arb staking to the
  bettor, so `kelly_stake` is 0 on every ARB row — a Kelly floor that applied
  to them would silence arbs entirely.
- A **missing** Pinnacle limit passes the limit gate: absent means unknown, not
  zero, and some books/markets ship no limits at all.

`weak` pairings stay excluded by default (the 2026-07-26 audit found big edges
are overwhelmingly weak), but that is now the default of the Confidence chips
rather than a hard-coded law, so you can inspect and override it.

### Anomaly alerts (Anomalies tab → **Alert settings**)

`Chime on refresh` fires every time the scan completes, whatever it found.
**Alert settings** is the content-based version: chime only on findings that
clear a bar you set. Both are cross-page — the sounds follow you to any tab.

**Ladder alerts** fire on a new violation, or on an existing one that got
materially worse. Three criteria, matching the three columns of the table, and
a row fires if **any** filled one is met:

| Criterion | Column it reads | Unit |
|---|---|---|
| `% off ≥` | `% off` — wrong-direction move as a share of the smaller price | % |
| `Odds change ≥` | the jump in `Odds → odds` | decimal |
| `Ladder step ≥` | the gap in `Ladder (line → line)` — how far apart the two rungs sit | points |

Leave a box **blank to ignore that criterion**; with all three blank nothing
fires (an empty box means "off", never "zero").

**Consistency alerts** are per-check, because severity is not one quantity:
it is percentage points for the moneyline checks, points for `period totals`,
and % for the HT/FT ones. Set a default, override any check that needs its own
bar, and untick a check to silence it entirely. Blank inherits the default.

Ladder and consistency findings have **distinct sounds** — a ladder violation
is bettable (Family A, the detector with the best track record), a consistency
flag is diagnostic — so you can tell them apart without looking.

Both seed silently the first time, so switching an alert on never chimes once
per finding already on screen, and both re-alert only when a finding grows by
≥1.5× (relative, so it means the same thing whatever the severity unit is).

Backed by `GET /api/anomalies/alerts` — a deliberately cheap sibling of
`/api/anomalies` that skips the Pinnacle re-matching, since the poller runs on
every page every 30 s.

### Half-time from full-time (Calc tab)

`src/ht_from_ft.py`, served by `POST /api/ht_from_ft`. Fits Poisson goal rates
**through** a two-half construction, so whatever the nuisance parameters are set
to the model still reproduces the full-time prices you fed it — the FT anchor is
the one thing actually known and it must not drift when a knob is nudged.

It is a **screening tool, not a pricing engine**. The dominant error is not
Poisson misspecification, it is (a) the devig assumption and (b) the fact that
FT 1X2 plus one total is 3 constraints against 2 goal-rate parameters. So rather
than emit a point estimate that hides both, it sweeps the assumption space —
devig × fit mode × H1 goal share × H2 score-dependence, 16 corners fast or 108
full — and reports a **band**.

**The decision rule is the worst corner.** The `min bet odds` column is the
longest fair price across every corner; below it you are betting model noise,
not edge. The `<< BET` marker uses the worst-corner edge, never the central one.

Two safeguards worth knowing:

- If the refit cannot reproduce the FT prices to within 3pp, the page says so in
  a banner: those prices do not sit on a clean Poisson surface and everything
  derived off them is shaky.
- A sweep is ~1.5 s (fast) or ~5 s (full), so it is button-triggered and the
  endpoint memoises identical inputs for 5 minutes. It is never called per
  keystroke.

The module keeps the project's **numpy-only** dependency set: the original used
`scipy.optimize.brentq`, `scipy.optimize.least_squares` and `scipy.stats.poisson`,
all three replaced by equivalents already living in `src/soccer_model.py` (plus
a local damped Gauss-Newton). `tests/test_ht_from_ft.py` pins the *original
scipy outputs* across six fixtures — worst drift measured 2.9e-7.

CLI, same model:

```bash
python3 -m src.ht_from_ft --ft 1.55 3.60 5.25 --total 2.5 2.15 1.55 \
                          --book ht_1=3.05 htft_1/1=4.30 --min-edge 0.05
```

### Stale prices, and the re-verify loop

`edge.py` compares a soft-book price to a reference price without checking how
old either is. That is fine for the fast books and not fine for CrystalBet.
Measured from `ticks.db`:

| | cycles | avg sweep | worst | events |
|---|---|---|---|---|
| **cb / soccer** | 233 | **191 s** | **3127 s** | 1103 |
| pin / soccer | 537 | 25 s | 1141 s | 647 |

CrystalBet is **cycle-bound, not interval-bound** — `crystalbet_poll_sec` is
irrelevant when one sweep of 1103 games takes longer than the interval. Gap
between consecutive CB soccer cycles: median 171 s, p90 724 s, **max 3300 s**.
Pinnacle re-prices the same board 2.3× more often. The persisted change cache
shows the same thing from the other side: a single `cb_change_cache_soccer.json`
holds a **52-minute spread** of `last_expanded_at`.

What that costs, over 8 460 CB soccer tick series:

| lag | p90 drift | moved >3% | moved >6% |
|---|---|---|---|
| 3 min | 0.00% | 0.5% | 0.1% |
| 10 min | 0.84% | 2.8% | 0.7% |
| 30 min | 2.94% | 9.8% | 3.5% |
| **50 min** | 3.88% | **14.5%** | **5.5%** |

So a large edge on an old CB price is substantially a measurement of drift.

Two things address it, and neither hides a row:

- **An `Age` column** on the Arbs tab — the age of the *soft-book* leg, dimmed
  under 5 min and red over 15. Cached CB detail rows carry the `fetched_at` of
  the cycle they were **expanded** in, not the one that re-emitted them, so this
  is a real staleness signal rather than a cycle counter.
- **The re-verify loop** (`OPP_REVERIFY_SEC`): every 2 min it re-pulls CB detail
  for *only* the games currently showing an edge ≥ `OPP_REVERIFY_MIN_EDGE` — a
  handful of `ExpandDetail` postbacks rather than a board sweep. An edge that
  survives on a freshly-pulled price is real; one that evaporates was drift.
  CB only: lider 9.9 s, betlive 11.9 s, crocobet 16.1 s and xbet 20.8 s per
  cycle are already fresh, so re-verifying them would spend requests for
  nothing.

`GET /api/status` carries an `opp_reverify` block with `rows_before` /
`rows_after` — the share of re-checked edges that survived. A low survival rate
is the loop working, not failing.

The re-verify merge replaces a re-pulled event **wholesale** rather than merging
row by row, so a line the book has since pulled disappears instead of surviving
as the stalest row on the page. An event whose re-pull returns nothing keeps its
old rows — an expansion failure is not evidence the markets are gone.

**If `Age` is stuck in the tens of minutes on CB rows, the loop is being
starved, not misconfigured.** Every CB task serialises on a per-sport lock, and
the ladder/anomaly sweep holds it for its whole pass — measured 2026-08-13, a
48 h `anomaly_extra_horizon_h` held the soccer lock for 47 minutes, during which
CB soccer ran one price cycle in four hours and this loop never completed a
single pass (`opp_reverify.at: null`). The loop now **probes** `sport_busy()` and
skips a busy sport for that tick (`skipped_busy` in the same status block)
instead of queueing behind it, and the sweep itself is bounded by
`ANOMALY_EXTRA_MAX_SEC`. Full account in `docs/performance.md`.

## Edge math

All math runs on **decimal odds**.

- **Convert American → decimal** at Pinnacle ingest (`src/vig.py`).
- **Devig** with Shin's method by default — solves for the insider
  proportion `z` such that per-side fair probabilities sum to 1. Falls back
  to proportional if numerically degenerate. Proportional kept as
  `devig_2way_proportional` / `devig_3way_proportional` for comparison.
- **+EV edge:** `edge = cb_decimal × pin_fair_prob − 1`.
- **ARB edge:** `1 − (1/cb + 1/pin_other_side)`. We only emit ARB rows for
  2-way markets; 3-way (soccer 1X2) doesn't surface ARB in v1.
- **Quarter-Kelly stake:** `f* = (p·d − 1) / (d − 1) / 4 × bankroll`.

See `notes/build_log.md` Phase 3.7 entry for the rationale on Shin vs
proportional.

---

## Project layout

```
prematch/
├── main.py                      # entry point: --once or dashboard mode
├── requirements.txt
├── src/
│   ├── app.py                   # FastAPI app + background poller loops
│   ├── models.py                # Odds, Match, Opportunity dataclasses
│   ├── vig.py                   # Devig (Shin + proportional), Kelly, Vig%
│   ├── edge.py                  # compute_opportunities()
│   ├── matcher.py               # Two-tier fuzzy + time matcher
│   ├── normalize.py             # Team name normalization (team + tennis)
│   ├── team_aliases.yaml        # Manual CB→Pin name aliases
│   ├── bets.py                  # SQLite bet tracker DAO
│   └── scrapers/
│       ├── crystalbet.py        # Playwright singleton, per-sport contexts
│       ├── cb_detail.py         # Detail-page expansion
│       ├── change_cache.py      # Per-game hash-based expansion cache
│       ├── cache_persistence.py # Disk save/load of change cache
│       ├── pinnacle.py          # Pinnacle guest API
│       └── sports/              # Per-sport parsers (basketball/soccer/tennis/americanfootball)
├── static/                      # Vanilla HTML dashboard
│   ├── matches.html
│   ├── arbs.html
│   ├── bets.html
│   ├── calc.html
│   ├── unmatched.html
│   ├── style.css                # CSS vars + light/dark theme
│   ├── theme.js                 # Theme toggle (loaded on every page)
│   └── alerts.js                # Cross-page sound alert poller
├── tests/                       # 413 tests, all passing
├── data/                        # Local-only — see .gitignore
│   ├── bets.db                  # SQLite (your placed bets — never pushed)
│   ├── cache/                   # Scraper warm-restart state
│   ├── unmatched_log.csv        # Matcher diagnostics, grows each cycle
│   └── raw/                     # Captured HTML samples (used by tests)
└── notes/
    └── build_log.md             # Dev journal — every phase + the WHY
```

---

## Testing

```bash
python -m pytest                  # full suite
python -m pytest tests/test_vig.py -v   # just the math
```

The suite mocks Playwright + Pinnacle HTTP, so the tests don't hit any live
service. Sample HTML for parser tests lives in `data/raw/`.

---

## Disclaimers

- **Books may have ToS against scraping.** This project is for personal,
  research use. Use at your own risk.
- **No edge is guaranteed.** Pinnacle is the sharpest reference but it's not
  infallible. Backtest before committing real money.
- **Bet responsibly.** If gambling is causing you harm, the
  [National Council on Problem Gambling helpline (US) at 1-800-GAMBLER](https://www.ncpgambling.org/help-treatment/national-helpline-1-800-522-4700/),
  or your country's equivalent, is one source of support.

---

## Further reading

- `ARCHITECTURE.md` — component-level walkthrough of scrapers, matcher, edge
  math, and the bet-tracker storage model.
- `notes/build_log.md` — dated journal entries documenting every non-trivial
  decision since Phase 1. Read this if you want context on *why* something
  is the way it is.
