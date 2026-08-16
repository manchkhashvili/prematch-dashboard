<!-- Migrated from prematch_v2/docs/ on 2026-06-12 (v2 paused; these are the
     definitive scraping references, valid for v1 — the browser-free CB
     transport in src/scrapers/cb_http.py implements crystalbet.md, and
     src/scrapers/pinnacle.py implements pinnacle.md. Live probes:
     v1 scripts/probe_cb_http.py, v2 research/probe_*.py. -->

# CrystalBet scraping — performance & throughput

Measured live on **2026-06-11** from a single Georgian IP on one modest
machine, using the browser-free protocol in [crystalbet.md](crystalbet.md).
Numbers answer: *how fresh can snapshots be, and how many matches can we cover?*

All measurements use `curl_cffi` threaded sessions. Reproduce via the
`/tmp/cb_bench*.py` benchmark family (kept out of the repo; methodology below).

---

## The one fact that governs everything

**An `ExpandDetail` response re-renders the entire current games panel, not
just the one game.** So expand cost scales with **how many games are loaded
in the view**, not with the game you're expanding:

| games in view | expand response size | expand latency |
|---|---|---|
| ~5–11 (1–2 champs) | ~350 KB | ~0.25–0.4 s |
| ~86 (12 champs) | up to ~2 MB | ~0.5–0.7 s |

**Rule:** keep each session's view small (1–2 championships) and
`CollapseDetail` after each expand. This keeps responses at ~350 KB and is
what makes parallelism scale.

---

## Measured throughput

### List-only (top lines for every match — no alt-lines)

| Mode | Football catalog (191 champs / 952 games) |
|---|---|
| Sequential, 1 session | **~201 s** (~0.9 champ-posts/s, panel grows to 6.8 MB) |
| **8 parallel sessions** (split the champ tree) | **~11 s** → **18× faster** |

So a top-line snapshot of the **entire** catalog is an ~11–30 s job. You can
refresh every match's main markets every 30–60 s without breaking a sweat.

### Extended (full alt-line detail via ExpandDetail)

Small views + collapse, sustained (not a burst):

| Sessions | Throughput | Per-minute | Mean latency |
|---|---|---|---|
| 1 | ~2 games/s | ~120/min | 0.40 s |
| **8** | **~14.6 games/s** | **~880/min** | 0.25 s |
| 12 (threads) | ~4–8 games/s | ~240–490/min | 0.9–1.6 s ⚠ |
| 16 (threads) | ~8 games/s | ~490/min | 0.91 s ⚠ |

⚠ Past 8 threaded sessions, throughput **degrades** — Python's GIL serializes
the HTML parse/harvest step, and per-request latency climbs. **8 is the
threaded sweet spot.** An `asyncio` client (single thread, no GIL contention
on the network wait) should push higher; not yet measured.

---

## What this means for "extended snapshots on 1–2k matches every minute"

The verified single-IP, threaded ceiling is **~850 extended snapshots/min**.

| Target | One IP, threaded | Verdict |
|---|---|---|
| 500 extended/min | comfortable (8 sessions, ~35 s of work) | ✅ |
| 1,000 extended/min | at the edge — needs ~9–10 sessions or asyncio | 🟡 feasible |
| 2,000 extended/min | beyond single-IP threaded reach (~33 games/s) | 🔴 needs reframe |

### Two hard limits to know

1. **Bandwidth.** At ~350 KB/expand: 1,000/min ≈ **47 Mbps** sustained,
   2,000/min ≈ **93 Mbps**. Check your connection before assuming CPU is the
   only constraint — past Playwright, bandwidth becomes the wall.
2. **CPU is no longer the wall.** Dropping Playwright removes Chromium
   entirely. Parsing a 350 KB HTML delta with regex/bs4 is ~milliseconds vs
   rendering a browser page. Your current "CPU can't handle full views"
   constraint **disappears** with the browser-free path — that's the headline.

### The reframe that makes 1–2k/min trivial

You almost certainly don't need to re-expand **every** match **every** minute.
Alt-line ladders are stable minute-to-minute for most games.

- **List poll** (~11–30 s, whole catalog) every 30–60 s gives top lines for
  all matches **plus a change signal** (the `data-loadInfo` hash).
- **Expand only what changed** (or a priority subset: leagues you bet,
  near-kickoff games, games already showing an edge). Realistically 10–20 %
  of matches move per minute → **200–400 expands/min** → well inside the
  ~850/min budget at 8 sessions.

This is v1's change-cache idea, but now cheap enough that the whole pipeline
fits one IP and one modest machine. **Full-ladder coverage of 1–2k matches
with fresh data every minute is feasible — by expanding the ~300 that
changed, not all 2,000.**

If you genuinely need every ladder every minute with no change-filtering:
1k/min is borderline (asyncio, ~50 Mbps); 2k/min needs 2–3 IPs/proxies and
~90 Mbps.

---

## Recommended v2 architecture (from these numbers)

1. **No browser anywhere.** Pure `curl_cffi`/`httpx`. (Removes the CPU wall.)
2. **List tier:** 8 parallel sessions split the champ tree → whole-catalog top
   lines every 30–60 s. Hash each game's `loadInfo` to detect changes.
3. **Detail tier:** a worker pool (8 threaded sessions, or async) that expands
   only changed/priority games, small-view + collapse pattern.
4. **Prefer asyncio over threads** for the detail pool if you want to push past
   ~850/min on one IP — avoids the GIL ceiling we hit at 12–16 threads.
5. Add proxies/IPs only if you truly need >1k *unconditional* expands/min.

---

## Odds change rate (drives storage design)

Measured 2026-06-11 ~13:00 Tbilisi, one 60 s window:

- **CB:** 0 / 1,469 selections changed across 113 football games (0.0 %).
- **Pinnacle:** 0 / 6,529 market entries changed (price *and* `version`)
  across the 5 busiest soccer leagues.

Midday prematch lines are essentially static minute-to-minute; movement
concentrates near kickoff and around news. Consequence: **store change events,
not per-minute snapshots** — a 1-minute poll cadence then costs almost nothing
in storage. (Single midday window — treat as lower bound; near-kickoff windows
will run hotter.)

Caveat: Pinnacle guest responses are Cloudflare-cached (`max-age≈900`,
`must-revalidate`). v1's 60 s polling does observe line moves cycle-to-cycle,
so the cache appears to revalidate/purge on origin change — but effective
freshness per league may be minutes, not seconds. Don't promise sub-minute
Pinnacle granularity.

## Methodology notes

- "session" = one `curl_cffi` Session with its own ASP.NET cookies + ViewState,
  warmed through GET → English switch → `DoSportTypePostBack(16)`.
- Concurrency via Python threads (`curl_cffi` releases the GIL on network I/O;
  the parse/harvest step does not — hence the threaded ceiling).
- Same-session concurrent expands **serialize** server-side (ASP.NET session
  lock) — parallelism requires *separate* sessions, one per worker.
- Sustained runs (≥5 s, ≥80 samples) trusted over sub-2 s bursts.
- All from one residential GE IP; no proxies. No rate-limiting or blocks hit
  across several hundred requests, but we did not probe abuse thresholds.

---

## Multi-book: extended vs list (CrystalBet, Lider-Bet, Betlive)

How much MORE "extended" polling (every event's full market ladder) costs than
"list" (curated top markets), per full 3-sport (soccer+basketball+tennis) catalog
cycle. Measured live 2026-06-15 from a GE IP: time the list poll, sample a few
detail fetches per book for the per-event cost, then project across the real
event count. Reproducer (gitignored, local): `scripts/bench_extended_vs_list.py`.

| Book | events | LIST reqs / MB / s | EXTENDED reqs / MB / s(serial) / s(conc10) | × reqs / MB / time |
|---|---|---|---|---|
| Lider-Bet  | 747 |  8 / 10 / 2.2 |  84 /  65 /   15 /   3.5 | 10× /   6× /   **7×** |
| Betlive    | 698 | 11 / 21 / 5.9 | 709 / 269 /   94 /  14.7 | 64× /  13× /  **16×** |
| CrystalBet | 730 |  3 /  4 / 8.2 | 733 / 669 / 1098 / 117.2 | 244× / 154× / **133×** |

Detail-fetch shape decides the cost: **Lider-Bet batches 10 matches/request**
(`…/matchData/details?matchIds=`) → extended is cheap (~7× time, ~3.5 s/cycle at
concurrency 10). **Betlive is 1 request/event** (`getPrematchEvent?id=`, not
batchable) but the JSON is tiny, so concurrency tames it (~15 s/cycle).
**CrystalBet is the energy hog** — its list is one cheap request but every game
is a separate heavy `ExpandDetail` postback (~670 MB, ~18 min serial / ~2 min at
concurrency 10 for all three sports), ~90% of the total cost.

Caveats: this is **cold/full** and **serial** is worst-case. The CB scraper's
production change-cache only re-expands games whose odds moved, so steady-state
CB extended is far cheaper than the projection; Lider/Betlive have no such cache
yet. `s(conc10)` is the realistic poller figure (these books are stateless GETs;
concurrency scales near-linearly — watch for rate limits). Practical read:
extended for Lider (and Betlive) is ~free at sane concurrency; doing it for CB is
what actually costs — so the cache or a longer CB extended cadence is the lever.

---

## The per-sport lock: why a wide scan horizon stops a sport dead

Measured on the live dashboard **2026-08-13**, after `anomaly_extra_horizon_h`
was raised 12 → 48 in the Config tab.

Every CrystalBet task serialises on a **per-sport `asyncio.Lock`**
(`crystalbet._sport_locks`). That is correct — the ASP.NET page carries per-
session viewstate and two interleaved `ExpandDetail` postbacks on the same page
corrupt each other. The consequence is that lock hold time is not a scraper
detail, it is the **sport's whole scheduling budget**:

| holder | what queues behind it |
|---|---|
| ladder/anomaly sweep (`bypass_cache`) | the price poll, the opportunity re-verify loop, the next scan |

A 48 h horizon on soccer is **476 games** in horizon against 135 at the 12 h
default (counted from the live CB board). The sweep held the soccer lock for
**47 minutes** and had not finished. In that window:

- CB soccer ran **one** price cycle in four hours (`poll_cycles`: 07:29, then
  nothing) while basketball/tennis/AF cycled normally — every soccer row on the
  Arbs tab was a **43-minute-old** price scored against a 10-minute-old Pinnacle
  fair, which is drift reported as edge;
- the opportunity re-verify loop — whose entire job is to re-pull exactly those
  rows — blocked on the same lock and reported `at: null` after an hour of
  uptime, having never completed a pass;
- `_anomaly_extra_loop` walks its sports **in order**, so tennis and american
  football were never reached: `extra.sports == []`, and the Anomalies tab was
  empty for two sports that were working fine.

One knob, three symptoms, none of them pointing at the knob.

### What made it unbounded

`_fetch_for_sport`'s `bypass_cache` branch **accepted a `should_continue`
callable and never called it** (the normal branch had checked it for months).
The sweep could not be aborted by the Config-tab switch, by a global pause, or
by anything else. The horizon was the only bound, and a horizon bounds *how many
games*, not *how long the rest of the board waits*.

### The per-game cost is ~16 s, not 0.85 s

The first cut of the fix put the deadline in the **caller**, and the next pass
proved that wrong in a way worth keeping:

| sport | started | published | total | expand | in horizon | expanded |
|---|---|---|---|---|---|---|
| soccer | 08:41:02 | 08:55:51 | 889 s | **0.0 s** | 500 | **0** |
| tennis | 08:55:51 | 09:05:38 | 586 s | **0.0 s** | 266 | **0** |
| americanfootball | 09:05:38 | 09:09:44 | 247 s | 229.4 s | 20 | 13 |

A caller cannot time this call. Two unbounded phases run before the expansion
loop — waiting for the sport lock behind another CB task, then refreshing the
whole league tree — and soccer spent **889 s** in them. The 240 s budget was
gone before the first postback, so the loop broke at game 1 and expanded
nothing. `max_expand_sec` is now a scraper-side parameter whose clock starts at
the expansion loop, and the three phases are reported separately (`wait_sec`,
`list_sec`, `expand_sec`) because they have completely different fixes.

The AF row is the useful one: **229.4 s for 14 games = ~16 s per game.** The
`~0.85 s/game` in the old code comments is off by a factor of ~19, and the
reason is the fact at the top of this document — an `ExpandDetail` re-renders
the **entire loaded panel**, and the ladder scan loads every league. So the
scan pays a whole-board render plus an html5lib parse per game.

At 16 s/game a 500-game soccer horizon is a **2.2-hour** pass. No wall-clock
budget makes that fit; a budget only decides how small a prefix you see. The
main dashboard path survives the same arithmetic for exactly one reason: it
expands only games whose list-view hash **moved**. The scan now gets the same
deal — its own change/detail cache under a `sport:ladder` namespace (it cannot
share the dashboard's, since the two run different classifiers over the same
games), with a **2 h** freshness bound rather than the dashboard's 6 h, because
a ladder anomaly is an alt-line claim and an unmoved main only makes an unmoved
rung *likely*.

Consequences:

- the cold pass is expensive and partial; later passes re-expand only what
  moved, so **coverage accumulates across passes** instead of resetting to the
  nearest N every time;
- a truncated pass narrows what got **refreshed**, not what is on screen — the
  tail keeps its cached ladder, or its list-view Odds if it has no cache yet.
  Dropping the tail is what made flagged games disappear from the tab.

### The rule

**Horizon says how much is worth scanning; a wall-clock budget says how long
everything else may be made to wait for it.** `ANOMALY_EXTRA_MAX_SEC` (default
240 s, runtime-tunable as `limits.anomaly_extra_max_sec`) now bounds each sport's
pass. Games are expanded soonest-kickoff first, so a truncated pass is the useful
prefix, and games past the cut keep their already-parsed list-view Odds rather
than vanishing from the snapshot.

Two consequences worth knowing:

- **A horizon wider than the budget can reach is a no-op**, not a hazard. Both
  select the same soonest-N games; the horizon just stops being the thing that
  decides N. Leaving it at 48 h is fine.
- **An aborted sweep must not publish.** Now that the sweep *can* stop early,
  hitting Pause mid-sweep would otherwise overwrite a good snapshot with the
  stump it got as far as — emptying the tab as a side effect of pausing. Both
  scan loops keep the previous snapshot when they were switched off mid-flight;
  only budget truncation publishes.

`/api/anomalies` → `extra.cost` reports per sport what the horizon actually
bought: `in_horizon`, `expanded`, `truncated_at`, `sec`. Anything that costs
minutes of a shared lock should have to say so.

### Not persisted, deliberately

`cache_persistence` saves named sports, so the `:ladder` namespaces are not
written to disk. A restart therefore costs one cold pass per sport — which is
the honest behaviour: the 2 h freshness bound would drop most of a restored
cache anyway, and a restored ladder that looked fresh would be the exact failure
this whole entry is about.

---

## The "+N" badge: the cheapest filter in the scraper

Owner's idea, 2026-08-14: *"can we somehow skip big games in anomalies that have
like 300+ positions on expanded versions? anyway its obscure games that have some
anomalies and nothing is in big ones"*. Both halves check out.

CB renders the market count on the **same div that triggers the expansion**:

```html
<div class="x_loop_game_active_add"
     onclick='DoGamesPostBack("ExpandDetail:2996090402")'>+4489</div>
```

So the cost of an expand is knowable **before** paying for it, from HTML we
already fetch and parse. Live soccer board: 1831 of 1877 games carry a badge;
min 2, p25 60, median 777, p75 839, p90 3407, max 6661.

### Cost per band, measured

Expanding a sample from each band and counting the **ladder rungs a check can
actually use** (lined spread/total rows):

| band | games | sec/game | MB | rungs | rungs/sec |
|---|---|---|---|---|---|
| 0–50 | 99 | 0.70 | 0.03 | **0** | **0.0** |
| 50–300 | 292 | 0.70 | 0.07 | 10 | 14.3 |
| **300–900** | **931** | **0.75** | **0.36** | **29** | **38.8** |
| 900–2000 | 236 | 1.46 | 0.62 | 29 | 19.9 |
| 2000+ | 271 | 3.12 | 2.07 | 43 | 13.8 |

### Correction: that measured ladders, not consistency

The first cut of this filter read the table above and set a **floor** at 50. That
was the wrong measurement — a consistency check needs no ladder at all, and the
owner had seen a `+2` game raise a flag. Re-measured on what the consistency
checks actually consume:

| band | games | rungs | htft | periods w/ 1X2 | markets |
|---|---|---|---|---|---|
| 0–20 | 83 | 0 | 0/6 | 1 | 1 |
| 20–50 | 14 | 6 | 0/6 | 1 | 8 |
| **50–300** | **284** | 9 | **2/6** | 2 | 11 |
| 300–900 | 911 | 29 | 6/6 | 2 | 44 |

The 50–300 band carries an **HT/FT grid in a third of games**, so a floor at 50
was quietly cutting `htft_combo` and `htft_fair`. And it was never worth much:
64s of a 2232s soccer sweep, **2.9%**. Tennis and AF save more of their own
(25% / 37%) but both already finish inside the 240s budget, so it buys nothing
there either. **The floor ships at 0 (off).** The knob remains for anyone who
wants it.

A banded-out game also keeps its **list-view Odds**. Not expanded is not the
same as erased — those rows are already parsed and carry the FT 1X2 and main
total that several consistency checks read.

### Yield: the big games are also the ones that are never wrong

From 7421 historical ladder anomalies across 67 leagues, the top 10 leagues are
**75%** of all of them, and every one is a minor competition — New Zealand NBL
(2462), Brazil LDB U22 (865), Paulista FPB U20 (552), Lebanon, Rwanda, Argentina
La Liga Federal, Indonesia IBL, Vietnam VBA, Mali Women, Iraq. Genuine top-tier
fixtures account for **9 rows, 0.12%**. Consistency flags agree: 3.3% by a
generous league-name match, and most of those are a minor Argentine league whose
name contains "La Liga".

Big games are the most expensive to expand and the least likely to be wrong.
That is what makes a **band** better than a bigger budget.

### Effect

`anomaly_min_markets` / `anomaly_max_markets` (defaults 50 / 2000, `0` disables
either side, live on the Config tab). Applied after the horizon and before the
budget — it is the filter that makes the budget go further rather than cutting
it off sooner. A game with **no badge is kept**: an unreadable count must not
silently drop a fixture.

Exact soccer savings by ceiling (1827 games with a badge, full sweep ~2232s):

| ceiling | games skipped | saved | resulting sweep |
|---|---|---|---|
| >2000 | 282 | 880s (39%) | 1353s |
| >1500 | 285 | 884s (40%) | 1348s |
| **>1000** | **509** | **1211s (54%)** | **1021s** |
| >800 | 941 | 1553s (70%) | 679s |

**1000 is the shipped default** — the knee of the curve, and it touches nothing
else: basketball's largest game is under 1000 markets, and tennis and american
football have nothing above 500. A global ceiling is therefore a soccer-only
filter in practice, with no per-sport override to keep in sync.

Against the 240s budget soccer goes from ~203 games per pass to ~320, and with
the ladder cache accumulating across passes the remaining ~1300 games are
covered in about four.

The band applies to the ladder scan **only**. The dashboard price path expands on
`cb_expand_within_hours` and keeps every game — dropping a big fixture there
would remove it from the Arbs tab.

---

## Parsing off the event loop: threads don't work, processes do

CB's detail parse is `BeautifulSoup(html, "html5lib")` + `cb_detail`. html5lib is
**pure Python**, so the GIL serialises it:

| | 4 pages |
|---|---|
| 1 thread | 3.56s |
| 4 threads | **4.03s** (0.88×, *slower*) |
| 1 process | 8.35s / 8 pages |
| 6 processes | 3.38s / 8 pages (2.47×) |

`asyncio.to_thread` — which the transport already used — moves work off the
loop's *stack* but not off its *thread of execution*. The event-loop impact is
the real prize, not the throughput:

| soccer list panel, 16 MB | parse | loop ticks | max stall |
|---|---|---|---|
| in-process | 9.48s | **1** | **9,477 ms** |
| pool (3 workers) | 9.06s | 177 | **21 ms** |

Same wall time; with the pool off, *nothing else in the process runs* for nine
and a half seconds, every cycle, per sport.

**Design constraint:** a BeautifulSoup tree cannot cross a process boundary, so
the split has to be bytes-in / Odds-out. `cb_http` grew `expand_detail_raw` and
`fetch_list_raw` (unnormalised strings, same English-flip check); the worker owns
normalise + parse and returns `Odds` / `_GameOnList` dataclasses. The classifier
travels as a **name**, since functions don't pickle, and `_classify_mode` refuses
an unknown one rather than guessing which markets get parsed.

`CB_PARSE_PROCS=3`, `0` disables, **off under pytest** (a worker spawns a fresh
interpreter and re-imports the scraper stack — left on, a 4 s suite timed out).
Any pool failure degrades to in-process parsing.

### Negative result: lxml is not a substitute

It would have been simpler and it does not work.

| | saved fixtures | **live pages** |
|---|---|---|
| lxml vs html5lib | identical Odds, 3.1–4.7× faster | **0/15 soccer, 0/10 basketball match** |

lxml over-collects on CB's unclosed `<td>`s — one game returned 59 markets under
html5lib and 108 under lxml. **The saved fixtures are a trap**: they're
`page.content()` captures, i.e. already browser-repaired, so they cannot test raw
panel markup at all. Any future parser swap must be verified against *live*
deltas.

### Negative result: the other books were never the problem

Heartbeating the loop during a book fetch:

| book | fetch | loop ticks | max stall |
|---|---|---|---|
| xbet | 53.7s | **1050** | 55 ms |
| liderbet | 57.9s | 1047 | 309 ms |

They're **network-bound**, and curl_cffi releases the GIL during I/O — so
`to_thread` genuinely works for them. Moving them into a pool made liderbet 25%
*slower* (57.9s → 72.4s) from pickling 33k Odds back. Only CB's parse is
CPU-bound. The rule: `to_thread` is fine for I/O-bound work and useless for
CPU-bound work, and you have to measure which one you have.

---

## The dashboard was starving the scanner

The finding that explained a full day of symptoms, and the one that was invisible
from inside the app.

`/api/opportunities` costs **~25 s** of pure Python — `match_events` is 2.6 s per
book/sport pair and `_compute_opportunities_now` runs every enabled soft book ×
every sport. `static/alerts.js` runs on **every dashboard page** and polls it
every 30 s, **per tab, independently**. Two or three tabs is an effective
10-second poll of a 25-second computation, and the loop never surfaces.

Same process, nothing changed but closing the browser tabs:

| | UI open | UI closed |
|---|---|---|
| `/api/config` (a dict read) | 8–17 s | **0.004 s** |
| `/api/anomalies` | 66 s | **0.035 s** |
| app CPU | 97% | **0.6%** |

~2000× on loop latency. The soccer ladder scan was never slow — 23–37 s per game
against 1.1 s standalone — it was **starved by the dashboard watching it**. This
also explains "it used to work": nothing broke, the matcher grew past the poll
interval as the board grew.

**Fix:** `limits.api_cache_sec` — a 10 s TTL on the expensive read endpoints so
N pollers and N tabs share one computation. Verified with 4 simulated tabs
polling like `alerts.js`: `/api/config` p50 **0.007 s**, p90 0.460 s, against
8–17 s constant before. Bounded to 64 keys (keys carry query params); `0`
disables; off under pytest, because a cache makes "mutate, re-query, assert"
non-deterministic and several existing tests do exactly that.

**Monitoring is load.** My own watcher polled `/api/anomalies` — the same
matching — every two minutes for hours while "measuring". Any probe against a
single-threaded app is part of the experiment.
