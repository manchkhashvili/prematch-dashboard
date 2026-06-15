<!-- Probed and written 2026-06-15 (lives alongside crystalbet.md / liderbet.md /
     pinnacle.md as a definitive scraping reference). Companion live probe:
     scripts/probe_betlive.py. -->

# Betlive scraping — reference

How to pull the full prematch odds catalog from **betlive.com** **without a
browser**. Everything here was probed live on **2026-06-15** against the running
site.

> **Headline finding:** Betlive's sportsbook is a third distinct stack — an
> **Angular SPA behind Cloudflare**, backed by a **plain REST/JSON API** at
> `sportnew.betlive.com`. Like Lider-Bet (and unlike CrystalBet) there is no
> ASP.NET postback dance: every odds read is a simple `GET` returning JSON.
> Cloudflare fronts it, but `curl_cffi` Chrome impersonation passes with **no
> challenge** — browser-free works. The drill-down is the classic shape:
> `getSportCategories` → `getCountryCategories?sportId=` (countries with nested
> league children) → `getLeagueEvents?leagueIds=` (curated markets, odds inline)
> → `getPrematchEvent?id=` (the full ladder). See §2.

Companion probe script that reproduces every claim below:
`../scripts/probe_betlive.py` (11/11 checks passing on 2026-06-15).

---

## 1. What Betlive is

The public site `www.betlive.com` is an **Angular** SPA (`<app-website-root>`,
Angular-CLI `runtime`/`polyfills`/`main` bundles) — but that one is the
**casino** (its bundle only exposes `/api/game/*`). The **sportsbook is a
separate Angular app at `sport.betlive.com`** (`<app-root>`, title "Sport"),
reached from the casino nav via the route `/{lang}/sport/prematch`. That sub-app
is what we scrape.

Both sites sit **behind Cloudflare** (`server: cloudflare`, `cf-ray` headers).
This is the one thing CrystalBet explicitly *didn't* have. In practice it was a
non-issue here: plain `curl_cffi` Chrome impersonation got JSON 200s with no JS
challenge, no Turnstile, no `cf_clearance` cookie needed. If that changes, the
fallback is a real browser (Playwright) to mint a clearance cookie — but it was
not required on 2026-06-15.

**The API base is `https://sportnew.betlive.com`** (hard-coded in the sport
bundle as `base_url`). Other hosts seen: `cdn.betlive.com` (assets),
`s5.sir.sportradar.com` (SportRadar stat widgets), `socket.io` transport (live
odds push — out of scope for prematch).

**Access:** probed from a Georgian IP (Silknet). No auth, no cookie, no token on
any prematch endpoint. **Geo-blocking is UNVERIFIED** — every request went out
over a GE IP, so whether Betlive (or its Cloudflare config) refuses non-GE
clients was not tested. Run from GE to be safe, same caveat as the other two.

---

## 2. The prematch API (browser-free)

All GETs on `https://sportnew.betlive.com`:

| Step | Endpoint | Returns |
|---|---|---|
| sports | `/api/category/getSportCategories?time=` | list of sports (+ a few featured comps) |
| tree | `/api/category/getCountryCategories?sportId={id}&time=` | countries, each with nested `children` = leagues |
| list | `/api/event/getLeagueEvents?leagueIds={id[,id…]}&page=0&take={n}` | events in those leagues + a **curated** market set, odds inline |
| detail | `/api/event/getPrematchEvent?id={eventId}` | the **full** market ladder for one event |

Also useful: `/api/event/getLeagueEventsBySport?sportId=&time=&page=&take=`
(skip the per-league loop — pull a whole sport's events paged),
`/api/event/getSchedule`, `/api/event/getEventByName?key=`,
`/api/outcome/refreshOdds` (lightweight odds-only refresh),
`/api/Market/GetMarketDisplayTypes` (market display metadata).

Query params:

- `time` — a date-window selector. **Omit it for "everything"** (the default
  window). It is *not* "epoch 0": `?time=0` returns `[]`. Treat it as opaque;
  leave it off unless you reverse-engineer the encoding.
- `leagueIds` — comma-separated league ids; batch many leagues in one call.
- `page` / `take` — pagination on the event lists.
- `id` — a single event id for the detail call.
- Language: set via an `Accept-Language`/language header the SPA sends; pass
  `Accept-Language: en` for English market/sport names (works on the responses
  seen). There is no `?lang=` query param like Lider.

Headers that matter: `User-Agent` (Chrome, via `curl_cffi impersonate`),
`Referer: https://sport.betlive.com/`, `Origin: https://sport.betlive.com`.
No request body, no CSRF token, no session — every GET is independent.

### Cycle

```
1. GET /api/category/getSportCategories
     → [ {id:1,name:"Soccer",eventCount:395,…}, … ]   (id 1 = Soccer)

2. GET /api/category/getCountryCategories?sportId=1
     → [ {id,name:"UEFA",children:[ {id:222840,name:"Super Cup",eventCount,…}, …]}, … ]
       a NESTED tree; walk children to the leaf leagues (eventCount>0).

3. GET /api/event/getLeagueEvents?leagueIds=<leafLeagueId>&page=0&take=50
     → [ {id,name,events:[ <event>, … ]} ]
       each event already carries odds for the curated market set.

4. (only for deep/alt-line markets)
   GET /api/event/getPrematchEvent?id=<eventId>
     → the same event with markets = the FULL ladder.
```

Stateless and idempotent throughout — no ViewState, no session affinity, no
accumulation order to respect (the three things that make CrystalBet fragile).

### Measured timing (2026-06-15, single GE connection)

| Step | Notes | Time |
|---|---|---|
| getSportCategories | ~7 KB, 34 entries | ~0.3 s |
| getCountryCategories (soccer) | ~20 KB tree, 93 leaf leagues | ~0.3 s |
| getLeagueEvents (1 league, 19 events) | curated markets | ~0.4 s |
| getPrematchEvent (1 event, 105 markets) | full ladder | ~0.3 s |

---

## 3. Sport ids (`getSportCategories`)

Small integer ids (not SportRadar-style). Live snapshot 2026-06-15 — `id:1` =
Soccer is the one to verify first. The same call also returns a few **featured
competitions** mixed in at top level (e.g. `id:911 "World Cup 2026"`,
`eventCount:393`) — these are shortcut shelves, not real sports; identify a real
sport by it actually having a country tree under `getCountryCategories?sportId=`.
`eventCount` on each entry tells you what's live; don't hardcode it.

---

## 4. The category tree — `getCountryCategories?sportId={id}`

Returns a **nested** array (unlike Lider's flat adjacency map):

```json
[ { "id": 1552, "name": "UEFA", "eventCount": 2, "sportId": 1,
    "children": [
      { "id": 222840, "name": "Super Cup", "eventCount": 1, "sportId": 1,
        "children": [], "isHeadToHead": false, "leagueId": … } ] }, … ]
```

- Country/category nodes carry `children`; the **leaf** nodes (empty/no
  `children`, `eventCount > 0`) are the **leagues** whose `id` you feed to
  `getLeagueEvents?leagueIds=`.
- Recurse `children` to collect leaf league ids. Some leagues are esports /
  virtual (names like `"e-Sports Battle"`, `"Volta Champions League"`) and some
  are `"Special Bets"` outright shelves — filter by name if you only want real
  fixtures.

---

## 5. List-view odds — `getLeagueEvents`

Returns `[{ id, name, events:[ <event> ], eventCount, … }]` — a league wrapper
with an `events` list. **Markets are flattened onto each event**, fully
self-describing (no separate id→name dictionaries like Lider's `ancestors` /
`marketTypes` — every field is inlined on the outcome).

### Event shape

```json
{ "id": 68632355, "name": "Gimnasia Jujuy - Nueva Chicago",
  "homeTeamName": "Gimnasia Jujuy", "awayTeamName": "Nueva Chicago",
  "sportId": 1, "sportName": "Soccer", "leagueId": 1746439,
  "leagueName": "Primera Nacional", "sportCountryName": "Argentina",
  "startDate": 639171486000000000, "statusId": 1,
  "providerId": 19, "providerEventId": 10112284,
  "eventMarketCount": 747, "markets": [ … 11 curated … ] }
```

| Field | Meaning |
|---|---|
| `id` | primary event key (plain int) |
| `homeTeamName` / `awayTeamName` | inline, already resolved — no lookup table |
| `startDate` | **.NET `DateTime.Ticks`** (100 ns since 0001-01-01), **UTC** — see below |
| `leagueId` / `leagueName` / `sportCountryName` | inline context |
| `providerEventId` / `providerId` | upstream feed id + provider — the cross-book hint (provider-specific, not a clean `sr:match:` like Lider) |
| `eventMarketCount` | total markets the book *offers* (e.g. 747); the list returns ~11, the rest come from `getPrematchEvent` |

**Timezone — `startDate` is .NET ticks in UTC.** Convert with
`datetime(1,1,1) + timedelta(microseconds=ticks//10)`. Verified UTC on
2026-06-15: the value decodes to a kickoff a couple hours *ahead* of now when
read as UTC; reading it as Tbilisi (UTC+4) would put it in the past, impossible
for prematch. So: decode ticks → UTC, **no −4h** (CrystalBet needed `−4h`;
Betlive, like Lider, does not — but note the .NET-ticks encoding, a fingerprint
of a WebForms-era backend behind the Angular skin).

### Market + outcome shape (flattened to outcomes)

```json
"markets": [
  { "outcomes": [
    { "marketName":"Fulltime Result", "marketId":1, "eventMarketId":2937527438,
      "id":9684350158, "outcomeId":1, "name":"1", "odd":2.8,
      "argumentString":"", "argumentX":0, "columnIndex":1, "rowIndex":1,
      "marketStatusId":1, "statusId":1, "combinationGroups":[1] }, … ] }, … ]
```

- **`odd` = the decimal odds** (a number, not a string). Treat `odd ≤ 1.0` or
  `statusId`/`marketStatusId != 1` as not bettable.
- `marketName` = market label, `name` = selection label, `marketId` = stable
  market-type id, `outcomeId` = stable selection-type id within the market.
- **The line lives in `argumentString` / `argumentX`** (e.g. a Total at `2.5`,
  an Asian Handicap at `-0.5`). An alt-line ladder is the same `marketId`
  repeated with different `argumentX`. No regex on a label like CrystalBet — the
  line is a numeric field.
- `columnIndex` / `rowIndex` are display-grid hints (how the SPA lays the
  selections out), occasionally handy to pair Over/Under or 1/X/2.

### Key market ids (Soccer)

| marketId | Market | selection labels |
|---|---|---|
| `1` | Fulltime Result (1X2) | `1` / `X` / `2` |
| `7` | Total Goals (O/U) | `Over` / `Under`, line in `argumentX` |
| `44` | Goals (O/U variant) | `Over` / `Under` |
| `39` | Asian Handicap | line in `argumentX` |
| `5` | Halftime/Fulltime | |
| `2` | 1st half result | |
| `50` | Double Chance 1st half | |
| `4345` / `4339` | Total Goals Home / Away team | |

(Pull the live `marketId → marketName` map straight off the outcomes, or from
`/api/Market/GetMarketDisplayTypes`; don't hardcode beyond 1/7/39.)

---

## 6. Detail-view odds — `getPrematchEvent?id={eventId}`

Same event object, but `markets` is the **full ladder**. Verified on a real
match (Primera Nacional): **105 markets** from `getPrematchEvent` vs **11** in
the `getLeagueEvents` list (and `eventMarketCount: 747` counting every
line-variant), in ~0.3 s, browser-free. This is the structural parallel to
CrystalBet's `ExpandDetail` and Lider's `…/details` — but a trivial GET keyed by
event id, idempotent and parallelisable. Same scrape-cost decision as the
others: the list already carries 1X2 + main totals/handicaps, so only deep-fetch
events/markets you actually price.

---

## 7. ID stability

| ID | Form | Use |
|---|---|---|
| Event | `68632355` (int) | primary event key |
| League | `1746439` (int) | `getLeagueEvents?leagueIds=` input |
| Sport | `1` (int) | `getCountryCategories?sportId=` input |
| Market type | `marketId:1` | stable market dictionary key |
| Outcome type | `outcomeId:1` (within market) | stable selection key |
| Outcome instance | `id:9684350158` | a single priced selection — odds tracking |
| `eventMarketId` | per (event × market) | groups a market's outcomes |
| `providerEventId` / `providerId` | upstream feed | cross-book hint (provider-specific) |

`marketId` / `outcomeId` are the stable schema; the long `id` is the per-offer
instance. Cross-session stability not stress-tested.

---

## 8. Open questions / notes

- **Cloudflare** — passed with plain `curl_cffi` on 2026-06-15, but CF policy can
  change without notice. If you start getting `403`/`503` HTML challenge pages,
  warm a `cf_clearance` cookie with Playwright and reuse it. Back off on 429.
- **`time` param encoding** — opaque date-window selector; omitted = full window.
  Worth decoding if you want server-side date filtering instead of pulling all
  and filtering client-side.
- **Cross-book matching is weaker than Lider** — Betlive gives `providerEventId`
  / `providerId` (a feed id), not a clean SportRadar `sr:match:` id, so matching
  against Pinnacle is fuzzy (name + UTC kickoff) unless `providerId` turns out to
  map to a known SR/BetGenius feed. The SportRadar widget host
  (`s5.sir.sportradar.com`) hints an SR mapping exists somewhere — not located
  here.
- **Live is a different system** — live odds come over a `socket.io` push channel,
  not these `/api/event/getPrematch*` REST calls. Out of scope for prematch.
- **Rate / stability under sustained polling** — not stress-tested. Stateless
  GETs make it far safer than CrystalBet's per-sport ViewState sessions.
