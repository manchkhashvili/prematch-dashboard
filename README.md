# Prematch Odds Dashboard

A local web dashboard that scrapes [CrystalBet](https://crystalbet.com) prematch
odds and compares them against [Pinnacle](https://www.pinnacle.com) as a sharp
reference, surfacing **+EV** and **arbitrage** opportunities across basketball,
soccer, and tennis.

Status: **research preview, single-user.** Tested daily against live books;
not production-hardened.

---

## What it does

- Scrapes CrystalBet's `Sports.aspx` prematch pages via headless Playwright
  (basketball / soccer / tennis), parsing moneylines, spreads, totals,
  team-totals, and corners markets.
- Fetches Pinnacle's guest API for the same sports (sport ids 4 / 29 / 33).
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
- Cross-page sound alert when a new opportunity above your threshold appears,
  with the seen-set persisted in `localStorage` so navigation never replays.

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
# All three sports in list-only mode — lightest config, ~30 s/cycle
SPORTS=basketball:list,soccer:list,tennis:list python main.py

# Basketball with full detail expansion, soccer + tennis list-only
SPORTS=basketball:full,soccer:list,tennis:list python main.py

# Basketball only, full
SPORTS=basketball python main.py

# With tee'd log for later debugging
SPORTS=basketball:list,soccer:list,tennis:list python main.py 2>&1 | tee dashboard.log
```

---

## Environment variables

| Var                        | Default       | What it does |
|----------------------------|---------------|--------------|
| `SPORTS`                   | all-full      | Per-sport mode: `sport:mode` comma-separated. Modes: `full` (CB+Pin+detail), `list` (CB list-view only, no alt-lines), `off`. Example: `SPORTS=basketball:full,soccer:list`. |
| `PINNACLE_POLL_SEC`        | 60            | Pinnacle poll cadence per sport. |
| `CRYSTALBET_POLL_SEC`      | 180           | CrystalBet poll cadence per sport. |
| `CB_TRANSPORT`             | playwright    | CB byte-mover: `playwright` (browser) or `http` (browser-free ASP.NET postbacks via curl_cffi — same data, ~10× faster detail, no Chromium; parity-verified, see `scripts/cb_parity_check.py`). |
| `CB_HEADLESS`              | 1             | `1` = headless Chromium, `0` = headed (useful for debugging selectors). Playwright transport only. |
| `CB_USE_SAVED`             | 0             | `1` = parse saved HTML instead of scraping. Dev mode. |
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
  Right-side action buttons: `Log` (prefill the bet form), `★` (manually
  highlight in amber), `−` (mute and dim).
- **`/bets.html`** — placed bets table with live CB-now / Pin-fair-now / edge
  evolution. Inline sparkline of Pin fair over time per open bet. Settle
  buttons (Won / Lost / Pushed / Void / Delete).
- **`/calc.html`** — devig calculator (Shin or proportional toggle) + +EV
  checker with quarter-Kelly stake suggestion. Inputs persist via
  `localStorage`.
- **`/unmatched.html`** — CB events that didn't match any Pin event +
  their best below-threshold candidate. Useful for curating `team_aliases.yaml`.

---

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
│       └── sports/              # Per-sport parsers (basketball/soccer/tennis)
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
