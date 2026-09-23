# LSport — the feed the mistakes come from, and the cycle built around it

Added 2026-09-21. Two things live here: how a CrystalBet game's odds provider
is read off the board (`src/scrapers/cb_provider.py`), and the **New
inconsistencies** tab that runs the Anomalies checks over only that provider's
games on a session and a clock of its own (`src/lsport_scan.py`).

---

## 1. The owner's tip, and why the code could not see it

CrystalBet prices its board from two providers. CB exposes no provider field
anywhere the scraper reads — the `Provider` strings in its HTML are casino
banners and the SportRadar references are the stats widget (this was checked
in the 2026-08-19 staleness study and recorded as a dead end). What the owner
knows from **bet tickets** — a settled ticket names the feed — is that one of
the two is **LSport**, that LSport's prices are the ones with mistakes, that
essentially every anomaly and consistency opportunity he has placed came from
an LSport match, and that some sports have no LSport at all (table tennis: all
noise, always).

Two earlier findings in this repo were this split, seen without the label:

- tennis ships under "two naming schemes" (`docs/anomalies-catalog.md` B8:
  `Winner` / `1st Period Winner Home/Away` on 71 % of matches vs `Which
  player will win the match` / `1st Set - Winner` on 29 %);
- flagged CB soccer matches "carry a median 34 raw markets vs 114 board-wide"
  and cluster in "reserves / U22 / cups / ITF" (2026-09-08).

## 2. The tell: `data-game-code`

Every game row on the list panel carries the provider's own fixture id:

```html
<div class="game-row x_loop_game_title_block" id="G3235754054"
     data-game-code="20264337">                        <!-- LSport -->
<div class="game-row x_loop_game_title_block" id="G3181702400"
     data-game-code="68932806" fs-gameid="hAfMDh4C">   <!-- the other feed -->
```

Verified by expanding 400+ games across 16 sports and scoring each detail
page's market vocabulary independently of the code:

| | `data-game-code` | detail-page template |
|---|---|---|
| **LSport** | **19.5 – 20.3 M** | labels `1(-6.5)`, `Under 53.5` / `22.5Under`, `Odd`/`No`/`Neither`; titles `1st Period Winner Home/Away`, `Under/Over Including Overtime`, `Asian Handicap Halftime`, `Race To 20 Points`, `12 Halftime`, `HT/FT Including Overtime`, `Goals Even/Odd` — LSports' catalogue verbatim |
| other feed | **63 – 75 M** (+ a Flashscore `fs-gameid`) | labels `1 (-2.50)`, `Und 5.5` / `over 5.5`, `odd`/`no`; `*` / `***` suffixes, `Full Time Result(1X2)*`, `Halftime/Fulltime`, `Which player will win the match`, `1st Set - Winner` |

Zero overlap. `LSPORT_CODE_MAX = 40 000 000` sits in the empty band between
the two id spaces; either would have to grow 2× to cross it. A row with no
code (measured: 33 of 1 292 raw soccer rows, all outrights) is reported as
unknown, not guessed.

## 3. How much of the board is LSport

Kept (non-outright) board, 2026-09-21. LSport also supplies every outright
market, which is why a raw row count reads far higher — those rows are
skipped by `_skip_league` before the provider is read.

| sport | LSport share | what they are | flags that night |
|---|---|---|---|
| soccer | 93 / 1 008 (9 %) | Northern League D1, Finland Nelonen, Estonia Liiga II, NI Development League, Honduras Reserve, China U20, Argentina Primera C reserves — median +117 markets vs 815; listed ~22 h before kickoff vs ~144 h | **13 of 19** |
| basketball | 7 / 91 (8 %) | Brazil Paulista U20, Korea Student League, VBA | **2 of 3** |
| tennis | 201 / 434 (46 %) | ITF | **2 of 2** |
| volleyball | 15 / 20 | club and national leagues | — |
| futsal | 4 / 4 | | — |
| handball | 4 / 35 | Brazil women, Luxembourg | — |
| American football, ice hockey, rugby, baseball, cricket, table tennis, MMA, boxing, eSoccer | **0 %** | | — |

Soccer: 9 % of the board, 68 % of the flags. The other-feed soccer flags were
all `htft_combo` on 800+-market boards — the stale-grid mechanism the catalog
documents on 2026-08-19, a different animal from LSport's mispricing.

This also closes the 2026-09-21 sweep of the uncovered sports (table tennis,
rugby, baseball, AFL, MMA…): "everything priced from one distribution, zero
locks" was the expected result on boards with no LSport in them.

## 4. The cycle — `src/lsport_scan.py`

The Anomalies scans spend their budget on the other 91 %: a soccer ladder
pass is ~900 games at a median of 815 markets and never completes inside its
240 s allowance. The whole LSport soccer board is ~90 games at ~120 markets.
So the New inconsistencies cycle:

- runs the **same detectors** — `find_ladder_anomalies` + `find_consistency_flags`,
  permissive classifier, per-section ladders, plus the `soft_scan` flags under
  the same switch — over **only** the games whose provider reads `lsport`;
- on **its own `CbHttpSession` per sport**. The shared per-sport sessions are
  guarded by crystalbet's sport lock, and ASP.NET keeps the expand/collapse
  view state per session, so reusing one without the lock would interleave
  with the price poll's postbacks. A second session is a second ASP.NET
  session on CB's side, and the two never touch;
- with **no sport lock** — it never queues behind a 47-minute soccer sweep and
  never makes one queue behind it — and **no change cache**: the board is
  small enough to expand in full every pass, and re-reading every LSport
  price each pass is the point;
- parsing through `cb_parse_pool` like every CB path, so html5lib stays off
  the event loop.

Measured, first live pass:

| sport | board (7-day horizon) | LSport in 48 h | expanded | list | expand | found |
|---|---|---|---|---|---|---|
| soccer | 455 | 70 | 70 | 4.8 s | **12.8 s** | 3 ladder (one at 14.5 %), 9 flags |
| basketball | 63 | 6 | 6 | 1.2 s | 0.7 s | 2 ladder, 3 flags (`htft_fair` 19.6) |
| tennis | 433 | 200 | 180 (+20 list-only) | 2.8 s | 41.2 s | 2 flags |

The 20 tennis fallbacks are `+2`-market games with no detail table; they keep
their list-view rows.

Results live in their own stores (`_lsport_*` in `app.py`) and are served on
`/api/new_inconsistencies` (+ `/status`, `/alerts`), never merged into the
Anomalies lists — the whole point is a board that holds only LSport findings.
Pinnacle context is attached against the cycle's own odds snapshot.

## 4b. Covering ALL LSport matches — discovery (2026-09-22)

"Cover all LSport matches" cannot be a list of sports. CB's nav moved by four
sports between two days (Bandy, Floorball, CS2, Sumo appeared; Sumo was gone
again within the hour), and LSport's coverage moves with the calendar. A
full census over every sport id on the nav, kept board, 7-day horizon:

| sport | LSport matches | covered by |
|---|---|---|
| tennis | 295 | dedicated |
| soccer | 76 | dedicated |
| volleyball | 8 (22 by the morning) | dedicated |
| futsal | 8 | dedicated |
| basketball | 5 | dedicated |
| handball | 1 | dedicated |
| sumo | 8 | **nothing** — one 2-way price per bout, no detail markets |
| the other 26 sports on the nav | **0** | — |

So the cycle now **discovers**: every `lsport_discover_sec` (30 min) it fetches
the live nav in English (a second GET on a warmed session — the raw page
answers in Georgian and the flip response carries no nav), lists every sport
it is not already scanning, counts LSport matches in horizon, and activates
any sport that has some. A sport with a dedicated module gets that module
(a process started with only basketball still ends up scanning tennis,
soccer, futsal and handball if LSport is there); anything else gets the
**generic LSport sport** (`sports/lsport_generic.py`): a title-only classifier
for LSport's shared vocabulary — handicap → spread, under/over → total,
winner/result/1X2 → moneyline with `n_way=0` (auto 3-way when an `X` label
is present), draw no bet, HT/FT, real halves and quarters — that refuses any
"period"/set/frame/leg title, since `1st Period` is a set in volleyball, a
third in hockey and a half in handball, and filing it under H1 would feed
`total_additivity` the wrong sum. That buys family-A ladders on every
classified ladder plus the sport-agnostic checks (`ml_vs_spread`,
`favourite_flip`, period totals on real halves, the pick'em trio and
`htft_combo` where the markets exist) on a sport nobody wired.

Cost: one list postback per sport, ~23 s for 25 sports, sessions dropped
again where nothing was found. Badge-less games (a sumo bout) keep their list
rows without an ExpandDetail (`list_only` in the stats). The tab's status
chip carries the census — "LSport elsewhere: none (0 on 26 other sports,
checked 12m ago)" is the sentence that makes "all covered" checkable.

## 5. Config

| knob | where | default | what |
|---|---|---|---|
| `lsport_scan` | Config → Scans; `LSPORT_SCAN` | **on** | the cycle |
| `lsport_scan_sec` | Config → Cadence; `LSPORT_SCAN_SEC` | 180 s (floor 30) | one pass per sport per tick |
| `lsport_horizon_h` | Config → Limits; `LSPORT_HORIZON_H` | 48 h | LSport fixtures land ~22 h out at the median, so this is effectively the whole LSport board |
| `lsport_max_sec` | Config → Limits; `LSPORT_MAX_SEC` | 120 s | expansion budget per sport per pass; soonest kickoff first, tail keeps list-view rows |
| `LSPORT_SPORTS` | env | every sport the process runs | narrow the cycle |
| `LSPORT_EXTRA_SPORTS` | env | `volleyball,futsal,handball` | LSport-only sports with dedicated modules |
| `lsport_discover_sec` | Config → Cadence; `LSPORT_DISCOVER_SEC` | 1800 s (floor 300) | the nav sweep (§4b) |

American football and ice hockey have no LSport games, so on those a pass is
one list postback that selects nothing — left in on purpose, so the day CB
moves a sport onto LSport the tab picks it up unasked.

`Odds.provider` and `_GameOnList.provider` / `.game_code` carry the reading
everywhere CB rows go (list-view rows included), so the same filter can be
applied to the Arbs tab later. Not persisted by `cache_persistence`.

## 6. Next

- The +EV-vs-Pinnacle side was measured on one row at the dead hour (other
  feed). With `Odds.provider` on every CB row a daytime census of the Arbs tab
  by provider is a one-liner; if it lands the same way, a provider chip on the
  Arbs tab and an LSport-only alert layer follow.
- LSport-only sports on the cycle (`LSPORT_EXTRA_SPORTS`, no price poll, no
  Pinnacle): **volleyball** (`docs/volleyball.md`, 2026-09-21 — derived from the
  match price like tennis, one independent seam, rows gated on fair) and, from
  2026-09-22, **futsal** and **handball** (`sports/soccerlike.py`): the LSport
  soccer template on a different ball, read by the same identity checks —
  `pickem_*`, `htft_combo`, ladders, halves vs match — with the soccer goal
  model kept out. First live pass: futsal 1 `htft_combo` (+3 %), handball 2
  ladder crossings on one game (Criciuma W v Athb, `AH away −13.5 @1.85 →
  −12.5 @1.75`, 5.7 %). Double Chance on LSport soccer, measured on 74 games:
  derived, ±2pp of its legs — not built.
- The New inconsistencies tab hides rows that name no bet by default ("bets
  only"): a diagnostic like `ml_vs_spread` should not sit above a priced leg.
- Alerts for this tab: `/api/new_inconsistencies/alerts` already serves the
  compact feed; `alerts.js` polls only the Anomalies one.
