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
