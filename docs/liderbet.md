<!-- Probed and written 2026-06-15 (lives alongside crystalbet.md / pinnacle.md
     as a definitive scraping reference). Companion live probe:
     scripts/probe_liderbet.py. -->

# Lider-Bet scraping — reference

How to pull the full prematch odds catalog from **lider-bet.com** (the real
domain is `www.lider-bet.com`; `liderbet.com` 301-redirects to it) **without a
browser**. Everything here was probed live on **2026-06-15** against the
running site.

> **Headline finding:** Lider-Bet is the *opposite* of CrystalBet. Where CB is
> server-rendered ASP.NET WebForms with odds buried in HTML behind a
> `__VIEWSTATE` postback dance, Lider-Bet's sportsbook is a **React SPA backed
> by a clean, public, read-only JSON API**. Two GETs get you everything:
> `/services/pre/m1/api/sport/menu` (the whole sport→country→tournament tree)
> and `/services/pre/m4/api/sport/matchData?tourIds=…` (every match in a
> tournament with odds already inline). No session, no ViewState, no postbacks,
> no HTML parsing. The full alt-line ladder is one more GET
> (`…/matchData/details`). See §2.

Companion probe script that reproduces every claim below:
`../scripts/probe_liderbet.py` (14/14 checks passing on 2026-06-15).

---

## 1. What Lider-Bet is

A **single-page React app** (Create-React-App build, `Sports v3.61.70`). The
public site is a thin shell at `www.lider-bet.com` that mounts shared header
JS and, for the sportsbook, hands off to a **separate sub-app at
`sports.lider-bet.com`** (`window.location.href = "https://sports.lider-bet.com/…"`).
That sub-app is what we scrape.

The whole thing sits behind an **Istio / Envoy** gateway (`server: istio-envoy`)
that fronts a set of microservices. The React bundle talks to them over plain
JSON; odds are never in HTML.

**Backend hosts seen in the bundles** (only `sports`/`pre` matter for prematch):

| Host | Role |
|---|---|
| `sports.lider-bet.com/services/pre/…` | **prematch** catalog + odds (what we use) |
| `sports.lider-bet.com/services/br/…` | **live** odds — a BetRadar (`br`) feed, out of scope |
| `sports.lider-bet.com/services/meta/…` | i18n strings, market display config |
| `staticdata.lider-bet.com` | images/icons (market & competitor logos) |
| `sportcache.lider-bet.com` | static JSON caches (news, jackpots) — not odds |
| `reactive.lider-bet.com`, `/eventsource` | real-time push channel for live odds deltas |

**Access:** probed from a Georgian IP (Silknet, `91.151.x`). The API answered
with no auth, no cookie, no CAPTCHA, no bot headers — `curl_cffi` Chrome
impersonation works, and even a plain UA is probably enough. **Geo-blocking is
UNVERIFIED**: every request here went out over a GE IP, so whether Lider blocks
non-GE clients the way CrystalBet does was not tested. Treat "needs a GE IP" as
likely-but-unconfirmed and run from GE to be safe.

### The one non-obvious bit: the `/services/` prefix

The JS bundle stores the endpoints as **`/pre/m1/api/sport/menu`** etc.
(relative, no host). In the browser the gateway mounts them under
**`/services/`**, so the real URL is
`https://sports.lider-bet.com/services/pre/m1/api/sport/menu`. Hitting the
bundle's literal `/pre/…` path (without `/services/`) silently returns the SPA's
`index.html` (HTTP 200, `text/html`) instead of JSON — Envoy's history-fallback,
the exact dead-end the first probes hit. **Always prefix `/services/`.** This
was discovered by capturing the SPA's real XHR traffic with Playwright; once you
know the prefix, no browser is needed again.

---

## 2. The prematch API (browser-free)

Three GETs, all on `https://sports.lider-bet.com/services`:

| # | Endpoint | Returns |
|---|---|---|
| menu | `/pre/m1/api/sport/menu?lang=en&marketFilter=true` | the whole sport→country→tournament tree (~150 KB) |
| list | `/pre/m4/api/sport/matchData?tourIds={t:ID[,t:ID…]}&lang=en` | every match in those tournaments + a **curated** market set, odds inline |
| detail | `/pre/m4/api/sport/matchData/details?matchIds={pr:m:ID}&lang=en` | the **full** market ladder for one (or several, comma-joined) match |

Query params:

- `lang` — `en` / `ka` / `ru`. `en` gives English sport, league, team and
  market names (§6) — use it.
- `marketFilter=true` — what the `sports.` host sends. On the **menu** it trims
  the tree to bettable nodes. On **matchData** it made **no difference** in
  testing (same 33-market curated set either way) — the list is always the
  curated subset; the full ladder only comes from `…/details`.
- `tourIds` — comma-separated tournament ids (`t:9008,t:65761`). You can batch
  many tournaments in one matchData call.
- Optional date window on the menu: `fromDate` / `toDate` (epoch ms), `cFrom` /
  `cTo` (coefficient/odds range filter). Omit for "everything".

Headers that matter: `User-Agent`, and `Referer: https://sports.lider-bet.com/`
(belt-and-suspenders). No `__VIEWSTATE`, no `__ASYNCPOST`, no per-request token.

### Cycle

```
1. GET /pre/m1/api/sport/menu?lang=en&marketFilter=true
     → {"menu": { "<nodeId>": [ <childNode>, … ], … }}
       a flat adjacency map (see §4). Walk it to collect tournament ids
       (t:…) and their match counts (cnt).

2. For each sport (or all at once), batch the tournament ids and:
   GET /pre/m4/api/sport/matchData?tourIds=t:…,t:…&lang=en
     → {"data": {"ancestors":{…}, "matches":{…}, "marketTypes":{…}, "blocks":…}}
       matches already carry odds for the curated market set.

3. (only if you need every alt-line / exotic market)
   GET /pre/m4/api/sport/matchData/details?matchIds=pr:m:…&lang=en
     → same shape, but match.markets is the FULL ladder.
```

No state is carried between calls — each GET is independent and idempotent.
That removes every fragile thing about CB (ViewState refresh, session death /
`pageRedirect`, accumulation order, hidden-field harvesting).

### Measured timing (2026-06-15, single GE connection)

| Step | Size | Time |
|---|---|---|
| GET sport menu | ~150 KB | ~0.3 s |
| matchData (1 tournament, 56 matches) | ~1.5 MB | ~0.4 s |
| matchData (25 tournaments batched, 165 matches) | ~4 MB | ~1 s |
| matchData/details (1 match, 370 markets) | ~300 KB | ~0.3 s |

The whole soccer catalog is ~50 tournaments / ~400 matches; batched matchData
pulls it in a handful of calls. Because the list call already carries the main
markets + the popular alt-line ladders inline, **you only need the per-match
`details` call for deep/exotic markets** — the inverse of CB, where the list was
nearly useless and ExpandDetail was mandatory.

---

## 3. Sport sections (the `s:` ids)

Lider uses **SportRadar-style ids**. In the menu, a sport is a `s:<n>` node and
every tournament/country under it carries `sectionId: "s:<n>"`. Resolve the
human name from the node whose `id == sectionId`, in `lang=en`.

Live snapshot 2026-06-15 (by match count):

```
s:16  Soccer (427)      s:28  Table Tennis (379)   s:13  Tennis (300)
s:242 (101)             s:2   Basketball (40)      s:34  (38)
s:33  (33)              s:32  (29)                 s:11  Rugby (21)
s:3   (18)              s:6   (17)                 s:14  (15)
s:84  (11)  s:21 (11)   s:5   Futsal (6)           s:61  (6)
s:18  (5)   s:102 Australian Football (7)          s:282 Formula 1
```

`s:16` = Soccer is the one to verify first. Counts swing with the schedule;
don't hardcode them. **Pseudo-sections** start with `f:` — `f:wc::s:16`
(World Cup feature group), `f:top-leagues::s:16`, `f:top-bets::s:13`,
`f:top-leagues:category::s:2` — these are UI feature shelves that re-list games
already under a real `s:` sport. Skip anything whose `sectionId` starts with
`f:` (the CB analogue is its negative pseudo-ids `-169`/`-1111`/`-666`).

---

## 4. The menu tree — `/pre/m1/api/sport/menu`

```json
{"menu": {
  "s:16":   [ {"id":"c:3287","name":"Australia","sectionId":"s:16","leaf":false,"cnt":25,"sort":6.0}, … ],
  "c:1534": [ {"id":"t:9008","name":"Championship","sectionId":"s:16","leaf":true,"cnt":1,"sort":114.0}, … ],
  "t:9008": [ … matches or empty … ]
}}
```

It is a **flattened adjacency map**, not a nested tree: each key is a node id and
its value is the list of that node's direct children. Walk it by id prefix:

| Prefix | Node | Notes |
|---|---|---|
| `s:<n>` | Sport / section | top of a branch; name via the self-referential node |
| `c:<n>` | Country / category | e.g. `c:22319` = "World Cup 2026" |
| `t:<n>` | Tournament / league | **the unit you feed to `matchData?tourIds=`** |
| `f:…` | Feature shelf (pseudo) | skip — duplicates real nodes |
| `future:wmg:…` | Outright / futures group | long-term markets, usually empty in the menu |

Per-node fields: `cnt` = number of matches under it, `leaf` = no further tree
children, `sort` = display order, `hidden`, `drawStyle`. To enumerate a sport:
collect every `t:` node with `sectionId == "s:16"` and `cnt > 0`, then batch
their ids into `matchData`.

---

## 5. List-view odds — `matchData`

`{"data": {"ancestors":{…}, "matches":{…}, "marketTypes":{…}, "blocks":{…}}}`

- **`matches`** — `{ "pr:m:<id>": <match> }`
- **`ancestors`** — id→`{name,…}` lookup for **everything referenced** by id:
  competitors (`cm:`), countries (`c:`), tournaments (`t:`). This is how you
  resolve team / league names — they are NOT inlined on the match.
- **`marketTypes`** — id→definition for every `mt:` referenced (name +
  ordered `outcomeTypes`); this is the market dictionary.

### Match shape

```json
{"id":"pr:m:4826654","startTime":"2026-06-26T02:00:00","sportId":"s:16",
 "catId":"c:22319","tourId":"t:65761","homeId":"cm:262716","awayId":"cm:18500",
 "providerId":"pre:1","markets":{ … },
 "meta":{"marketsCount":"1159","matchProvider":{"providerId":"BR","matchId":"sr:match:66456948"},
         "pre2live":{"provider":"BR","providerId":"sr:match:66456948"},
         "additionalInfo":{"venue":{…},"extraInfo":{…}}}}
```

| Field | Meaning |
|---|---|
| `id` (`pr:m:4826654`) | primary event key, prefix `pr:m:` |
| `startTime` | **ISO-8601, UTC, no offset suffix** — see timezone note below |
| `homeId` / `awayId` | competitor ids → names via `ancestors` |
| `tourId` / `catId` / `sportId` | tree context → names via `ancestors` |
| `meta.matchProvider.matchId` | **SportRadar id** `sr:match:…` — the cross-book join key (CB's analogue was the flaky `data-game-code`) |
| `meta.marketsCount` | total markets the book *offers* (e.g. 1159); the list returns a curated slice, `details` returns the rest |

**Timezone:** `startTime` carries no offset. Verified UTC on 2026-06-15: the
soonest upcoming matches sit ~10 min ahead of `keepAlive`'s `server_time` when
read as UTC; reading them as Tbilisi (UTC+4) would place them 4 h in the *past*,
impossible for prematch. So: parse as **UTC**, no `−4h` correction (CB needed
`HH:MM − 4h`; Lider does not).

### Market + outcome shape

```json
"pr:mr:390664608": {
  "id":"pr:mr:390664608","matchId":"pr:m:4826654","typeId":"mt:16:502",
  "specifier":{"special":"1.5","total":"1.5"},
  "outcomes":{
    "ot:16:6":{"id":"pr:oc:4617925337","typeId":"ot:16:6","value":3.55,"probability":0.2745,"specifier":{"special":"1.5"}},
    "ot:16:7":{"id":"pr:oc:4617925353","typeId":"ot:16:7","value":1.23,"probability":0.7255,"specifier":{"special":"1.5"}}
  }}
```

- **`value` = the decimal odds.** (No string parsing, no "odds ≤ 1.0 = suspended"
  HTML quirk — though still treat `value ≤ 1.0` / missing as not bettable.)
- `typeId` → look up `marketTypes[typeId]` for the market name and the ordered
  `outcomeTypes` (which name each `ot:` — `1`/`X`/`2`, `Over`/`Under`, …).
- `specifier.total` / `.special` / `.handicap` = the **line** (e.g. total `1.5`,
  handicap `-1.5`). An alt-line ladder is the **same `typeId` repeated** with a
  different `specifier` — e.g. Total (`mt:16:502`) appears 11× for lines
  `0.5 … 5.5`, each its own market object. No regex on a label string like CB;
  the line is a structured field.
- `probability` — book's normalized implied probability (the two sides of a
  2-way sum to ~1.0). Useful but devig at edge-compute time, not from this.

### Key market types (Soccer, `mt:16:*`)

| typeId | Market | outcomeTypes |
|---|---|---|
| `mt:16:500` | Full Time Result (1X2) | `ot:16:2`=1(home), `ot:16:1`=X, `ot:16:3`=2(away) |
| `mt:16:502` | Total (O/U) | `ot:16:6`=Over, `ot:16:7`=Under; line in `specifier.total` |
| `mt:16:503` | Double Chance | 1X / 12 / X2 |
| `mt:16:538` | Both Teams To Score | Yes / No |
| `mt:16:6572` | Draw No Bet | 1 / 2 |
| `mt:16:1079` | Handicap | line in `specifier` |

The `16` in `mt:16:…` / `ot:16:…` is the sport id, so market/outcome ids are
namespaced per sport — don't assume `mt:2:500` (basketball) means the same thing.

---

## 6. Detail-view odds — `matchData/details`

`GET /pre/m4/api/sport/matchData/details?matchIds=pr:m:4826654&lang=en` — same
envelope as §5, but `matches[id].markets` is the **full ladder**. For the sample
match: **370 markets across 267 distinct types** (vs 33 in the curated list), in
~0.3 s, browser-free. `matchIds` accepts a comma-separated list to batch.

This is the structural parallel to CB's `ExpandDetail`, but trivial: a plain GET
keyed by match id, no hidden-field harvesting, no session affinity, idempotent
and parallelisable. Decision for the scraper mirrors CB's open question — at
~0.3 s/match you can either (a) pull `details` for every match every cycle, or
(b) rely on the curated list and only deep-fetch matches whose markets you trade.
Given the list already carries 1X2 + totals + the main ladders, (b) is cheap.

---

## 7. Language (`lang=` param, no "switch")

There is no stateful English toggle like CB's `ImageButtonEn` postback — every
endpoint just takes `?lang=en|ka|ru` and returns that language's names inline
(sport "Soccer", market "Full Time Result", teams "Turkey"/"USA"). The full
i18n string table is also fetchable at
`/services/meta/api/v1/i18n?lang=en` and market display ordering at
`/services/meta/api/v1/meta/viewConfig?lang=en`, but for raw odds you don't need
them — pass `lang=en` and read the names off the response.

---

## 8. ID stability

| ID | Form | Use |
|---|---|---|
| Match | `pr:m:4826654` | primary event key |
| Market | `pr:mr:390664608` | per (match × type × line) instance |
| Outcome | `pr:oc:4617925337` | a single priced selection — odds tracking |
| Market **type** | `mt:16:502` | stable market dictionary key (per sport) |
| Outcome **type** | `ot:16:6` | stable selection-name key (per sport) |
| Tournament | `t:65761` | league filter / `matchData` input |
| **SportRadar match** | `sr:match:66456948` | **cross-book join** (Pinnacle etc.) |

The `pr:*` instance ids are per-offer; the `mt:*`/`ot:*` *type* ids are the
stable schema you build the market map against. Treated `mt:`/`ot:` as stable
within a session; cross-session stability not stress-tested.

---

## 9. Open questions / notes for v2

- **Curated list vs full details** — same trade-off as CB §8, but cheaper on
  both sides. Probably: use the batched `matchData` list as the spine, deep-fetch
  `details` only for matches/markets we actually price.
- **Geo-block unverified** — all probing was over a GE IP. Confirm whether a
  non-GE IP is refused (CB hard-blocks non-GE). Run from GE regardless.
- **Live is a different system** — live odds come from the BetRadar feed at
  `/services/br/…` plus a push channel (`reactive.lider-bet.com` / `/eventsource`),
  not from `/pre/`. Out of scope for prematch, but note the `pre2live` /
  `matchProvider` mapping lets you follow a match across the prematch→live cut.
- **Rate / stability under sustained polling** — not stress-tested. Stateless
  GETs make this far less risky than CB's per-sport ViewState sessions, but the
  Envoy gateway may rate-limit; back off on 429/5xx.
- **`sr:match:` cross-ref is gold for matching** — Lider hands you the SportRadar
  match id directly on every match, so fixture-matching against any other
  SportRadar-backed book (incl. Pinnacle's SR ids) can be exact rather than
  fuzzy name+time. This is a meaningful upgrade over the CB side.
