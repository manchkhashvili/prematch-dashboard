<!-- Migrated from prematch_v2/docs/ on 2026-06-12 (v2 paused; these are the
     definitive scraping references, valid for v1 — the browser-free CB
     transport in src/scrapers/cb_http.py implements crystalbet.md, and
     src/scrapers/pinnacle.py implements pinnacle.md. Live probes:
     v1 scripts/probe_cb_http.py, v2 research/probe_*.py. -->

# Pinnacle guest API — reference

How to pull prematch odds from Pinnacle's **guest Arcadia API** — free,
unmetered, JSON, no browser. Re-verified live on **2026-06-11**. Curated
response samples: `../../prematch_v2/research/pinnacle_samples.json`. Probe script that
reproduces every claim: `../../prematch_v2/research/probe_pinnacle.py`.

> Context: Pinnacle shut down their public retail API on 2025-07-23. The guest
> API is the same feed pinnacle.com's own web UI uses. Third-party Pinnacle
> resellers (~$99/mo) mostly resell this exact data; what you'd buy from them
> is WebSocket push (~1 s latency) + uptime, **not** more accurate lines. For
> prematch this free polling feed is all we need.

---

## 1. Base + auth

```
https://guest.api.arcadia.pinnacle.com/0.1
```

Headers (v1 sends all three; only some are enforced):

```
x-api-key: CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R   # from pinnacle.com /config/app.json
Origin:    https://www.pinnacle.com
Referer:   https://www.pinnacle.com/en/<sport>/
```

Verified 2026-06-11:
- `x-api-key` is the public guest key embedded in pinnacle.com's own JS
  (`/config/app.json`). **Rotate if you start getting 403 `BAD_APIKEY`.**
- It is **not** uniformly required — `/sports/{id}/leagues` returned 200
  with no key at all. But other endpoints do enforce it, so always send it.
- **Never send `brandId`.** v1 (Phase 3.1.1) found that `brandId=0` made
  tennis (`sport_id=33`) and per-league `/matchups` return `403 BAD_APIKEY`,
  while omitting it returned 200. As of **2026-06-11 that 403 no longer
  reproduces** (returns 200 with `brandId=0` too) — so it may have been a
  temporary WAF rule. Either way the safe form is to omit `brandId` entirely;
  there's never a reason to send it. (Flagged as drift to watch.)

**Caching:** responses are Cloudflare-cached (`cf-cache-status: HIT`,
`cache-control: max-age≈900, must-revalidate`). Fine for prematch — lines
don't move on a sub-15-min scale that matters here. (This same cache makes the
guest API unusable as a *live* feed — see `live/FINDINGS_live_guestapi.md`.)

---

## 2. Endpoints (the 3-call cycle + 1 fallback)

| # | Call | Returns |
|---|---|---|
| 1 | `GET /sports/{sport_id}/leagues?all=false` | leagues for the sport |
| 2 | `GET /sports/{sport_id}/matchups` | **bulk**: every matchup across every league |
| 3 | `GET /leagues/{lid}/markets/straight` | all market entries for one league (run per league, concurrency ~10) |
| 3b | `GET /leagues/{lid}/matchups` | per-league matchups — **fallback** when bulk omits a matchup (§6) |

Devig is **not** done here — markets carry the vigged American prices; devig
happens at edge-compute time (Shin's method).

Also useful:
- `GET /sports` → full sport list with live `matchupCount` per sport (good for
  discovering which sports have inventory right now).

### Sport IDs (verified live 2026-06-11)

```
3 Baseball   4 Basketball   6 Boxing       8 Cricket      10 Darts
12 E Sports  15 Football(American)   17 Golf   18 Handball   19 Hockey
22 Mixed Martial Arts   26 Rugby League   27 Rugby Union   28 Snooker
29 Soccer    33 Tennis      34 Volleyball  37 Padel        39 Aussie Rules
44 Formula 1
```

⚠ **`Soccer = 29`, not 15.** `15` is American Football. Live matchup counts
on 2026-06-11: Soccer 567, Tennis 247, Basketball 92, Football 82, MMA 16.

---

## 3. League object

`/sports/{id}/leagues` returns a flat list. Relevant fields:

```json
{ "id": 1739, "name": "Argentina - Primera B Nacional",
  "group": "Argentina", "matchupCount": 18, "matchupCountSE": 18,
  "isHidden": false, "isPromoted": false,
  "sport": { "id": 29, "name": "Soccer", "primaryMarketType": "moneyline" } }
```

**Skip by name substring** (case-insensitive): `cyber`, `esport`, `ebasket`,
`specials`, `outright`, plus the soccer child-league names `corners`,
`bookings` (their `/markets/straight` always 403s — those markets come via
the *parent* league instead, §5).

---

## 4. Matchups (`/sports/{id}/matchups`)

One bulk call returns **everything** — parents, sub-matchups, and props —
indexed by `id`. On 2026-06-11 soccer returned **3,098** entries.

### Classifying an entry

- **Parent (real game):** `parent == null` AND `parentId == null` AND
  `type != "special"`. → 263 soccer parents on 2026-06-11.
- **Special (prop/future):** `type == "special"`. ~2,800 on soccer —
  unstructured, **filter out**.
- **Child sub-matchup:** `parentId` set. Distinguish by `units`:

  | `units` (child) | count (soccer 2026-06-11) | v1 handling |
  |---|---|---|
  | `Corners` | 26 | folded onto parent, `submarket="corners"` |
  | `Bookings` | 12 | deferred (skipped) |
  | `1st Goal` | 108 | not handled |
  | `CombinedResult` | 53 | not handled |
  | `Red Cards` | 8 | not handled |
  | `Penalty Kick` | 8 | not handled |
  | `Regular` | 2316 | period/derivative variants |

  > v2 decision point: v1 only folds Corners. The other child types
  > (1st Goal, Red Cards, Penalty Kick…) are real markets CB may also list —
  > worth revisiting whether to support any.

### Parent matchup shape (key fields)

Full sample in `../../prematch_v2/research/pinnacle_samples.json`. The fields that matter:

```json
{ "id": 1631759252,
  "startTime": "2026-06-14T18:30:00Z",     // ISO 8601 Z; live if <= now
  "isLive": false,
  "units": "Regular",                       // tennis parents = "Sets" (§7)
  "league": { "id": 1739, "name": "Argentina - Primera B Nacional" },
  "participants": [
    { "alignment": "home", "name": "Estudiantes de Caseros" },
    { "alignment": "away", "name": "All Boys" } ],
  "type": "matchup", "parent": null, "parentId": null }
```

Extract `home`/`away` from `participants[].alignment`. Skip if either is
missing, if `type=="special"`, if it has a parent, or if `startTime <= now`
(that's live, not prematch).

---

## 5. Market entries (`/leagues/{lid}/markets/straight`)

A flat list of market entries. **Keys (full union, verified):**
`cutoffAt, isAlternate, key, limits, matchupId, period, prices, side, status,
type, version`.

```json
{ "matchupId": 1631762810,
  "type": "total",            // moneyline | spread | total | team_total
  "period": 1,                // 0 = FT, 1 = H1  (soccer; no Q's prematch)
  "key": "s;1;ou;0.75",
  "isAlternate": false,        // alt-line flag — v1 keeps ALL lines
  "status": "open",            // suspended markets appear here too — filter
  "cutoffAt": "2026-06-14T18:30:00+00:00",
  "limits": [ {"amount": 100, "type": "maxRiskStake"} ],   // max Pinnacle will take
  "version": 3634295997,
  "prices": [
    {"designation": "over",  "points": 0.75, "price": -129},
    {"designation": "under", "points": 0.75, "price": 101} ] }
```

### Price fields

- `designation` — `home | away | draw | over | under`
- `price` — **American** odds (convert to decimal downstream)
- `points` — the line value (spread/total/team_total); absent for moneyline
- `side` (top-level, **team_total only**) — `home | away`, which team's total

### `key` format (Pinnacle-internal, handy for debugging)

```
s;0;m            full-game moneyline
s;1;m            1st-half moneyline
s;0;s;-0.5       full-game spread, home -0.5
s;1;ou;0.75      1st-half total 0.75
s;1;tt;0.5;home  1st-half home team-total 0.5
```

### Market-type specifics (verified)

- **3-way moneyline (soccer 1X2):** one entry whose `prices` contains a
  `draw` designation → emit {home, draw, away}. Basketball/tennis ML never
  has `draw`, so presence-of-draw is a safe 2-way/3-way discriminator.
- **team_total:** two entries per matchup, one `side:"home"` one `side:"away"`,
  each with over/under prices.
- **periods:** soccer ships `0` (FT) and `1` (H1) only. Other ints exist on
  other sports/markets (e.g. `39` = knockout "To Advance") — **whitelist**
  `{0: "FT", 1: "H1"}` and drop the rest.
- **suspended / bad lines:** drop a market line if `status != "open"`, any
  price is missing, or any converted decimal ≤ 1.0.

### Per-league failure handling _(v1-proven)_

`/markets/straight` 403s come in two flavours: transient WAF flicker (retry
once + 1.5 s backoff recovers it) and persistent data-feed restriction (same
leagues fail every cycle). v1: after 3 consecutive failures on a league,
cooldown 30 min; any success resets. A `404` = no markets booked, not a
failure.

---

## 6. The bulk-omission fallback (§3b) _(v1 Phase 3.8)_

The bulk `/sports/{id}/matchups` sometimes **omits** matchups that
pinnacle.com displays and that *do* appear in `/leagues/{lid}/markets/straight`
(payload-size / regional filtering on Pinnacle's side — their UI stitches
per-league `/matchups` so bulk needn't be exhaustive). Symptom: market rows
reference a `matchupId` not in the bulk index → silently dropped → a real
match shows no Pinnacle prices.

**Fix:** per league, after fetching markets, compute the referenced parent
matchup IDs; if any are missing from the bulk index, call
`GET /leagues/{lid}/matchups` for *that league only* and merge into a
league-local view (never mutate the shared bulk index). Log when it fires.
Verified case: Resende vs América RJ / Brazil Carioca A2.

**Known hard limit:** ~8 boutique soccer leagues (Norway Eliteserien, Ireland
Premier, Brazil Copa Sul-Sudeste, …) 403 even on the per-league endpoint —
those stay unmatchable.

---

## 7. Tennis Sets/Games split _(v1 Phase 3.1.4, re-verified)_

Pinnacle splits each tennis match into **two** matchups:

- **Parent**, `units="Sets"` — moneyline + set-handicap (±1.5 sets) + set-total
  (2.5 sets).
- **Child**, `parentId=<parent>`, `units="Games"` — games-handicap +
  games-total (what pinnacle.com shows under "Handicap (Games)").

Verified 2026-06-11: tennis had 201 parents (all `Sets`) + children
`{Games: 203, Sets: 16}`.

CB's tennis primary handicap is **games-based**, so:
- Fold the `Games` child onto the parent as the **primary** spread/total
  (`submarket=None`).
- **Skip** the parent's own `Sets` spread/total (they'd mis-pair against CB's
  games lines).
- Keep moneyline from the parent (same prediction either way).

---

## 8. v1 quirks worth keeping / revisiting

- v1 captures **none** of `limits` / `status` / `cutoffAt` into its `Odds`
  rows. `limits.maxRiskStake` is genuinely useful for "is this edge bet-sized
  worth it" — **candidate to capture in v2.**
- v1 gates basketball to `{moneyline, spread, total}` (no team_total) because
  the CB side had no team_total classifier — pulling them created phantom
  unpaired rows. Per-sport allow-lists, not a global one.
- MMA (`sport_id=22`): `{moneyline, total}` only — can't handicap a fight.
  (Protocol fact retained for reference; MMA was dropped from the dashboard
  2026-07-26 — only CB and Pinnacle ever served it, and no soft book did.)
  Method-of-victory + go-the-distance props exist as separate market types,
  unused.
