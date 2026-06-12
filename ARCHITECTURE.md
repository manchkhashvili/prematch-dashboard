# Architecture

Component-level walkthrough of the prematch odds dashboard. For *why* a piece
is shaped the way it is, see the dated entries in `notes/build_log.md` — this
document describes the *what*.

---

## Data flow

```
                  ┌─────────────────────────────────────────────────┐
                  │              uvicorn process (one)              │
                  │                                                 │
   CrystalBet ◄───┤  ┌───────────────┐    ┌──────────────────────┐  │
   (Playwright,   │  │ CB pollers    │    │ FastAPI HTTP server  │──┼──► /matches.html
   per-sport      │  │ (per sport,   │    │ (/api/* + static/)   │  │   /arbs.html
   contexts)      │  │  ~180s each)  │    │                      │  │   /bets.html
                  │  └──────┬────────┘    │ Reads _state for     │  │   /calc.html
                  │         │             │ list endpoints       │  │   /unmatched.html
                  │   ┌─────▼─────────────▼──┐                   │  │
                  │   │ _state (in-memory)   │   /api/bets        │  │
                  │   │ per-sport per-source │   reads bets.db    │  │
                  │   │ {odds, fetched_at,   │   directly         │  │
                  │   │  count, error}       │                    │  │
                  │   └─────▲─────────────▲──┘                   │  │
                  │         │             │                       │  │
   Pinnacle  ◄────┤  ┌──────┴────────┐    │   ┌──────────────┐   │  │
   (httpx,        │  │ Pin pollers   │    └───┤ Bets module  │   │  │
   guest API,     │  │ (per sport,   │        │  bets.db     │   │  │
   60s each)      │  │  ~60s each)   │────────► (SQLite +    │   │  │
                  │  │  → also       │        │  history)    │   │  │
                  │  │  snapshots    │        └──────────────┘   │  │
                  │  │  open bets    │                            │  │
                  │  └───────────────┘                            │  │
                  └─────────────────────────────────────────────────┘
```

Three asyncio task pairs (Pinnacle + CrystalBet) per enabled sport, all
running inside one uvicorn process. State is a per-sport namespaced dict in
memory. HTTP handlers read without locking — Python's GIL makes the dict
load atomic and stale-by-one-update reads are fine for a dashboard.

---

## Scrapers

### CrystalBet (`src/scrapers/crystalbet.py`)

CrystalBet's `Sports.aspx` is an ASP.NET WebForms page that requires a real
browser to drive its `__doPostBack` calls. We use Playwright with a single
Chromium instance shared across sports, plus **one `BrowserContext` per
sport** so session cookies don't collide (the May 24 / Phase 2.5 #6 fix —
ASP.NET's session-bound `currentSport` would otherwise stomp itself when
two sports' pages call `DoSportTypePostBack` near-simultaneously).

Per-sport cycle:
1. `_ensure_page_for_sport(sport_id)` → ensure browser, ensure per-sport
   context, ensure page navigated + sport selected.
2. `_load_all_leagues_for_sport(page, sport_id)` → call
   `DoChampionatPostBack("SelectAllChampionats:<sport_id>")`, wait for
   `div.GContainerList[data-id]` to render.
3. Read full HTML via `page.content()`. Per-game metadata extracted via the
   list-view parser (`src/scrapers/sports/<sport>.py`).
4. (full mode only) For each game whose `loadinfo` hash has changed since
   the last cycle, expand the detail panel and read alt-line ladders.
   Change cache (`src/scrapers/change_cache.py`) avoids re-expanding stable
   games — saves enormous time on cold restart.
5. Combine detail-page odds with list-view fallback, emit `Odds` rows.

The per-game expansion is the slowest path (~3s/game for soccer with all
alt-lines). `SPORTS=basketball:list` short-circuits this entire step — list
view only, no alt-lines, no H1 markets, but ~30s/cycle instead of minutes.

#### Browser-free transport (`src/scrapers/cb_http.py`, `CB_TRANSPORT=http`)

The Playwright layer above is only a TRANSPORT — it produces two HTML
artifacts (the list-view page, the per-game detail table) that the parsers
consume. `cb_http.py` produces the same two artifacts over plain HTTP
ASP.NET postbacks (curl_cffi, no Chromium), ported from the prematch_v2
research (`../prematch_v2/docs/crystalbet.md`):

- One warmed session per sport (GET → English full-postback →
  `DoSportTypePostBack`), mirroring the one-context-per-sport rule.
- List refresh = ONE `SelectAllChampionats:<sport_id>` postback (~1 s for
  the full board vs ~30-50 s of browser waits). Re-selects all
  championships every cycle, so new leagues appear automatically.
- Detail = `ExpandDetail:<game_id>` postback (~0.3-0.5 s vs ~3-15 s); the
  table rides in the RepeaterChampionat updatePanel. CollapseDetail is
  posted after parsing to keep the server-side view small.
- Two ASP.NET quirks matter: hidden inputs rendered inside panels must be
  re-scanned and merged into the next POST's field set (a browser
  form-serializes the live DOM; the hiddenField delta segments alone are
  NOT enough), and __VIEWSTATE must be refreshed from each delta.
- **normalize_html()** re-serializes every returned panel through html5lib
  before parsers see it. CB's raw markup is heavily unclosed (515 `<td>`
  opens vs 259 closes measured live); browsers repair it during HTML5 tree
  construction and `page.content()` returned the repaired form. html5lib
  applies the same WHATWG algorithm, so parsers receive browser-equivalent
  HTML. (lxml's recovery diverges — do not substitute it.)

Parsing, change cache, detail cache, and fallbacks are shared between the
two transports; the dispatch lives in `_refresh_list_html_for_sport` and
`_expand_game` in crystalbet.py. Parity was verified live with
`scripts/cb_parity_check.py` (A/B/A: playwright → http → playwright per
game; structural = http disagrees with both pw captures).

### Pinnacle (`src/scrapers/pinnacle.py`)

Pinnacle exposes a `guest.api.arcadia.pinnacle.com/0.1` JSON API. No
Playwright needed; we use `httpx` with the required headers (`x-api-key`,
`Origin`, `Referer`).

Per-sport cycle:
1. `GET /sports/{sport_id}/leagues?all=false` → list of leagues
   (after filtering out blacklisted suffixes like "Bookings").
2. `GET /sports/{sport_id}/matchups` → bulk: every parent matchup across
   every league, indexed by id.
3. `GET /leagues/{lid}/markets/straight` per league, with concurrency 10.
4. **Phase 3.8 fallback:** when per-league markets reference a `matchup_id`
   missing from step 2's bulk response, call `GET /leagues/{lid}/matchups`
   for that one league and merge into a per-league-scoped index view. This
   handles the case where Pinnacle's bulk endpoint omits matchups their UI
   shows (verified with the Resende / Brazil Carioca A2 debug session).
5. Devig is *not* done at scrape time — only at edge-compute time. Odds rows
   carry the vigged prices; matcher and edge module devig downstream.

Per-league failure tracking: 3 consecutive 4xx/5xx → 1-hour cooldown to
avoid hammering Pinnacle for leagues with no upcoming markets.

#### Tennis matchup split (Phase 3.1.4)

Pinnacle splits each tennis match into TWO matchups: a parent with
`units="Sets"` carrying moneyline + set-handicap markets, and a child with
`parentId=<parent>` and `units="Games"` carrying the games-handicap
versions (the markets visible on pinnacle.com under "Handicap (Games)"). We
fold the child under the parent for ML and skip the parent's set-spread /
set-total markets to avoid double-counting.

---

## Matching (`src/matcher.py`)

Two-tier fuzzy + time window. Both tiers were widened to ±1h in Phase 3.10
because CB and Pinnacle often disagree on kickoff time for the same fixture.

```
For each (cb_event, pin_event) pair:
  score = mean of rapidfuzz.token_set_ratio over (home, away) — both sides
          normalized via src/normalize.py first (team_aliases.yaml + sport-
          specific rules).

If score >= 80 (SCORE_LOOSE)  and  |Δt| <= 3600s : accept (strong tier).
If score >= 65 (SCORE_TIGHT)  and  |Δt| <= 3600s : accept (medium tier).
Otherwise: reject.

Final pass: sort all accepted pairs by score desc, greedy-assign each cb
event to at most one pin event. Robust against same-team-twice cases.

Unmatched CBs still get their best-candidate Pin recorded into
data/unmatched_log.csv for the curation loop.
```

Sport-aware normalization:
- **Team sports** (basketball, soccer): NFD → ASCII → lowercase → drop
  noise tokens (FC, BC, U18, etc.) + optional `team_aliases.yaml` lookup.
- **Tennis** (Phase 3.2): same ASCII cleanup, then strip trailing /
  leading single-char tokens (the CB "F" / "M.B." initials), keep all
  remaining surname tokens. `token_set_ratio` handles Pinnacle's extra
  firstname token by returning 100 on strict-subset matches. Doubles
  recurse on `/`-separated partners and join sorted so partner order
  doesn't matter.

---

## Edge math (`src/edge.py` + `src/vig.py`)

### Devig

`devig_2way` / `devig_3way` default to **Shin's method** (Phase 3.7). Shin
solves numerically (bisection over `z ∈ [0, 0.5]`) for the insider
proportion `z` such that:

```
For each side i:
  fair_i = (sqrt(z² + 4(1-z) · π_i² / Π) − z) / (2(1-z))
  where π_i = 1/d_i  and  Π = sum(π_i)
```

The constraint is `sum(fair_i) = 1`. On balanced markets (1.91 / 1.91)
Shin and proportional agree to 6+ decimals; on skewed markets the
divergence is large and Shin matches Pinnacle's empirical structure better.

Proportional method is preserved as `devig_2way_proportional` and
`devig_3way_proportional` for comparison.

### Opportunity computation

For each `MatchedEvent`, for each (cb_market, pin_market) pair joined on
(market_type, period, line, submarket, team_side):

- **+EV pass:** for each side, `edge = cb_dec × pin_fair_prob − 1`. If
  edge ≥ `min_edge_pct`, emit `Opportunity(kind="+EV")` with quarter-Kelly
  stake `((p·d − 1) / (d − 1)) / 4 × BANKROLL`.
- **ARB pass:** for each opposing pair (e.g. CB home + Pin away), if
  `1 − (1/cb + 1/pin_other) ≥ min_edge_pct`, emit
  `Opportunity(kind="ARB")` with the partner-side as `arb_partner_*`.
  Only 2-way markets emit ARB.

`Opportunity` carries both the human-readable `market` label and the
structured fields (`market_type`, `period`, `line`, `submarket`,
`team_side`) — the structured fields drive the bet-tracker prefill on
the arbs page.

---

## Bet tracker (`src/bets.py`)

SQLite, two tables:

```sql
bets (
  id INTEGER PK,
  placed_at, sport, cb_event_id (nullable), match_label,
  period, market_type, line, side, submarket, team_side,
  book ('cb'|'pin'|'other'), odds_taken, stake, bankroll_at_time,
  pin_fair_at_placement, cb_fair_at_placement, edge_at_placement_pct,
  status ('open'|'won'|'lost'|'pushed'|'void'),
  settled_at, payout,
  pin_fair_closing,         -- stamped automatically when kickoff passes
  note, start_time
)

bet_odds_history (
  id INTEGER PK,
  bet_id INTEGER FK ON DELETE CASCADE,
  recorded_at, cb_decimal, pin_fair_decimal,
  UNIQUE(bet_id, recorded_at)
)
```

The Pinnacle poll loop (`_pinnacle_loop_for_sport`) calls
`_snapshot_open_bets_for_sport` after every successful fetch — iterates
open bets for the polled sport, looks up the current CB row + matching
Pin row via `_current_odds_for_bet`, devigs Pin via Shin, inserts a
history row. Once kickoff passes, `pin_fair_closing` is stamped once and
never overwritten. After a bet is settled, the recorder skips it.

CLV displayed in the UI is computed on-the-fly:
`clv_pct = (pin_fair_at_placement / pin_fair_now − 1) × 100`. Positive =
our side's Pin fair shortened since placement, meaning sharp money agrees
with our pick.

---

## Frontend (`static/`)

Five vanilla HTML pages, served by FastAPI's `StaticFiles` mount. No
build step, no framework.

### Shared scripts

- **`theme.js`** — light/dark theme controller. Loaded synchronously in
  every page's `<head>` so `data-theme` is set on `<html>` before paint
  (no flash). Reads/writes `localStorage["theme"]`. Injects the toggle
  button into the `<header>` on DOMContentLoaded.
- **`alerts.js`** — cross-page sound alert poller (Phase 3.12). Polls
  `/api/opportunities?min_edge=1` every 30 s, tracks seen-keys in
  `localStorage` so navigation never replays. Plays a buzzy 2.5-second
  alarm only on opps newly-at-threshold AND only after the first-ever
  seed pass.

### CSS

`style.css` uses CSS variables in `:root` (dark default) +
`:root[data-theme="light"]` overrides. Variables cover backgrounds, text
levels, accent / good / warn / bad, and soft tinted backgrounds for
status chips. The bet-row state-stack:
`.has-bet` (auto-blue) → `.user-highlighted` (manual amber, wins) →
`.user-muted` (opacity 0.32, composes with anything).

### Page composition

Every page includes `style.css`, `theme.js`, and `alerts.js`. The
`<nav>` block is duplicated across pages (no template engine — the trade-off
for not having a build step). Cache-buster query params (`?v=YYYYMMDDx`) get
bumped on each release so users don't have to hard-reload.

---

## Configuration knobs that matter

These are the dials worth knowing about, lifted from `src/app.py` defaults
and the constants files. See `README.md` for the env-var table.

- `PINNACLE_POLL_SEC` (default 60) — also dictates the bet-history snapshot
  resolution.
- `CRYSTALBET_POLL_SEC` (default 60 since the browser-free transport;
  was 180) — full-mode PLAYWRIGHT soccer is sensitive to
  this; reducing it below 120s puts noticeable load on the per-game
  detail expansion.
- `LINE_MATCH_TOLERANCE` (`src/edge.py`, 0.01) — for matching CB and Pin
  spread/total lines. Phase 3.1.2 tightened from 0.5; was falsely pairing
  +1.0 with +1.5.
- `SCORE_LOOSE` / `SCORE_TIGHT` (`src/matcher.py`, 80 / 65) — fuzzy
  threshold for accepting matches.
- `TIME_LOOSE_SECONDS` / `TIME_TIGHT_SECONDS` (`src/matcher.py`, both
  3600) — Phase 3.10 widened from 600/300.
- `BANKROLL` / `KELLY_FRACTION` (`src/edge.py`, $500 / 0.25) — used by the
  `kelly_stake` field on Opportunity rows.
- `_STALE_CACHE_MAX_AGE_SEC` (`src/scrapers/change_cache.py`, 6h) — Phase
  2.5 #4 bumped from 30min; was dropping all disk-loaded cache on restart.

---

## Tests

413 tests, all run offline (Playwright + Pinnacle HTTP are mocked). Suite
organized by component:

- `test_vig.py` — devig (Shin + proportional), edge math primitives
- `test_edge.py` / `test_edge_soccer.py` — opportunity emission
- `test_matcher.py` / `test_normalize_tennis.py` — fuzzy matching + tennis
  name normalization
- `test_basketball_parser.py` / `test_crystalbet_parser.py` / `test_cb_detail.py`
  — CB list + detail parsers, fed by captured HTML in `data/raw/`
- `test_pinnacle_parser.py` / `test_pinnacle_soccer.py` /
  `test_pinnacle_tennis.py` / `test_pinnacle_fallback.py` — Pinnacle
  parser + Phase 3.8 fallback
- `test_change_cache.py` / `test_cache_persistence.py` /
  `test_stale_cache_safety.py` — change cache durability + safety
- `test_bets.py` — SQLite DAO + state machine
- `test_app.py` / `test_app_soccer.py` — FastAPI endpoint shape

---

## Known limits

- **Pinnacle 403 on some per-league `/matchups` calls.** Phase 3.8 fallback
  works for most leagues but ~8 boutique soccer leagues (Norway Eliteserien,
  Ireland Premier, Brazil Copa Sul-Sudeste, etc.) return 403 from the
  per-league endpoint. Those matches stay unmatchable.
- **CB pulls lines aggressively close to kickoff.** Within ~30-90 min of
  start for minor leagues, both CB and Pinnacle may remove markets.
- **Doubles tennis** is partially supported. Same-pair detection via the
  surname-sorted normalizer; mixed partner orderings work but Pinnacle
  often doesn't list doubles at all.
- **No multi-book.** Only CB vs Pin. Adding a second book would require a
  scraper module + extending the matcher's pair logic.
- **3-way ARB not emitted.** Vanishingly rare within a single book; the
  +EV pass still surfaces per-side opportunities.
