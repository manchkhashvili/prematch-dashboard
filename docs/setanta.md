<!-- Probed and written 2026-07-26 (lives alongside crystalbet.md / liderbet.md /
     betlive.md / pinnacle.md as a definitive scraping reference). Companion
     live probe: scripts/probe_setanta.py. Scraper: src/scrapers/setanta.py. -->

# Setanta (setanta.bet) scraping — reference

How to pull the full prematch odds catalog from **setanta.bet** without a
browser, and how it is wired into the dashboard as the sixth book. Everything
here was probed live on **2026-07-26** against the running site.

> **Headline finding:** Setanta is the only book in the stack with **no REST
> odds endpoint at all**. Its sportsbook is a white-label "apg" SPA iframed
> from `sport-iframe.ukyuku.xyz`, and every price rides a **SignalR hub
> speaking the MessagePack protocol**. That sounds worse than it is: the hub
> takes **no auth, no cookie and no negotiate step**, the handshake is 0.08 s,
> and the book **publishes its own wire schema**, so there is no market-code
> archaeology. It is the cheapest per-outcome feed we have —
> ~7.5 KB and ~6 ms per event, whole soccer board in 4–7 s on one socket.
> Two catches: **no SportRadar id** (name+time matching, like CrystalBet), and
> a `subPeriod` field that will silently produce phantom edges if ignored (§6).

---

## 1. What Setanta is

`setanta.bet` is a Next.js casino/sport shell with its own account and wallet
API at `api.setanta.bet`. It contains **no sportsbook**. The sportsbook is
iframed from a separate host:

```
setanta.bet (Next.js shell, DataDome-protected)
  └─ GET api.setanta.bet/webApi/api/v1/games/sportsbook/url?currency=GEL&language=ka
       → {"url":"https://sport-iframe.ukyuku.xyz/ka?sessionId=<uuid>"}
          └─ sport-iframe.ukyuku.xyz         ← everything we want
               ├─ /apg/v0/…                  REST: navigation, translations
               ├─ /api/v0/sport/feed/schema  the wire schema
               └─ /direct-feed/feed          SignalR hub — ALL odds
```

**The `sessionId` is irrelevant to scraping.** `sport-iframe.ukyuku.xyz/en/`
loads standalone and the hub answers without it. We never touch `setanta.bet`
itself, so we never meet the DataDome bot wall on the shell — the iframe host
served 30 rapid REST calls with no challenge and no cookie.

**Access:** probed from a Georgian IP. No auth, no login, no CAPTCHA;
`curl_cffi` for the two REST bootstrap calls, `websockets` for the hub.
**Geo-blocking is UNVERIFIED** — same caveat as liderbet.md; run from GE.

---

## 2. Bootstrap — everything comes off the wire

`GET https://sport-iframe.ukyuku.xyz/en/` (225 KB, server-rendered) carries the
widget config inline:

```json
"widgets":{"defaultVariation":{"apiKeySdk":"17e856be-…","feedURL":"/direct-feed", …}}
```

plus the brand code (`CL53B1`) in its asset paths. Those, plus the schema, are
all the credentials that exist:

| Value | Where | Used for |
|---|---|---|
| `apiKeySdk` | iframe HTML | `x-api-key` header (REST) and `X-Api-Key` query param (WS) |
| brand `CL53B1` | iframe HTML | `brand` query param + element 3 of the feed context tuple |
| wire schema | `/api/v0/sport/feed/schema` | field order for every model |

`src/scrapers/setanta.py` re-reads all three at start-up (cached 1 h) rather
than pinning them — they are per-brand config and rotating the key is the
cheapest thing the operator could do to break us.

**Gotcha:** the schema lives under **`/api/v0/…`**, not the `/apg/v0/…` prefix
every other feed call uses — `/apg/v0/sport/feed/schema` returns `400`.

---

## 3. The hub

```
wss://sport-iframe.ukyuku.xyz/direct-feed/feed?brand=CL53B1&X-Api-Key=<apiKeySdk>
```

The SPA passes its headers **as query params** because it sets
`skipNegotiation: true` + `transport: WebSockets` — so there is no `/negotiate`
round-trip and no connection token. Handshake is one text frame:

```
→ {"protocol":"messagepack","version":1}\x1e
← {}\x1e                                       # 0.08 s, no auth challenge
```

After that every message is a binary frame carrying one or more
**varint-length-prefixed MessagePack messages**. Types are stock SignalR:
`4` StreamInvocation (ours), `2` StreamItem, `3` Completion, `5`
CancelInvocation, `6` Ping.

A subscription:

```jsonc
[4, {}, "0", "GetMarketsByTournamentIdAndStage",
    ["<tournamentId>", 1, null, ["en", "MOBILE_WEB", "CL53B1", "", "GEL"]]]
//                     ▲ stage   ▲ layout        ▲ context tuple (last arg of EVERY method)
```

`stage` is `0 Default / 1 Prematch / 2 Live`.

The hub reports its own arity in error strings — invoke anything with one
argument and read the `Completion` (`"provides 2 argument(s) but target
expects 3"`). Methods the scraper uses:

| Method | Args |
|---|---|
| `GetTournamentsBySport` | sport, stage, ctx |
| `GetRichEventsByTournamentIdAndStage` | tournamentId, stage, ctx |
| `GetMarketsByEventIds` | [eventIds], layout\|null, ctx |
| `GetMainMarketsByProfileAndEventIds` | profile, [ids], n, n, ctx |

Payload framing:

```
StreamItem = [2, {}, invocationId, [isInitialBatch, feedData[]]]
feedData   = [isRemoved, key, value]
```

The **first** StreamItem carries the whole snapshot (`isInitialBatch=true`);
everything after it is a delta on the same stream.

---

## 4. Decoding — the schema endpoint is the whole trick

Every model is a **positional array with no field names** — the same problem
1xbet's `G`/`T` codes pose, except the book publishes the field-order
dictionary itself. `/api/v0/sport/feed/schema` returns 21 models (`richEvent`,
`market`, `outcome`, `tournament`, `batch`, `feedData`, …), each
`{name, keySchema, valueSchema}` where a schema is
`{field: {index, type, enum?, optional?}}`. The decoder is ~40 lines
(`_Wire` in the scraper), needs no hand-built table, and self-heals when the
book adds fields. It also ships the enums:

- `tradingStatus`: `1 Opened / 2 Suspended / 3 Removed`
- `stage`: `0 Default / 1 Prematch / 2 Live`

---

## 5. Data model

### `richEvent` (key = eventId string)

```json
{"sport":"F","tournamentId":"…","startTime":1785250800,"stage":1,
 "name":"KuPS - Sabah FK","tradingStatus":1,
 "competitors":[{"id":"91617","name":"KuPS",…},{"id":"91015","name":"Sabah FK",…}],
 "categoryName":"UEFA Champions League","tournamentName":"Qualification",
 "hasBetradarMapping":false,"outcomesCount":460, …}
```

- `startTime` is **unix epoch seconds, UTC**. No CB-style `−4 h` correction.
- `competitors[0]` is **home**, `[1]` away — verified against Lider-Bet on 219
  shared soccer fixtures: **0 reversed**, favourite side agreed on 217/219.
- `categoryName`/`tournamentName` are denormalised onto the event — no
  ancestors lookup, unlike Lider.
- `outcomesCount` is an exact pre-count of the ladder — a free cost estimate.

### `market` — key is a compound tuple, not an id

```json
key   = {"eventId":"…","resultKind":1,"marketType":2,"period":0,
         "subPeriod":null,"layout":null}
value = {"marketItems":[{"key":{"marketParameters":["2.5"]},
                         "outcomes":[{"key":{"type":4},"odd":193,
                                      "isFrozen":false,"isRemoved":false,
                                      "originalOdd":190}, …]}]}
```

- **`odd` is decimal odds × 100** (`193` → 1.93). Integer maths.
- An **alt-line ladder is one market with many `marketItems`**, the line in
  `marketParameters` — structured, no regex on a label.
- Ladders can be **one-sided at the extremes** (a −5 handicap carried only the
  home outcome). Never assume 2 outcomes per line.
- `isFrozen` / `isRemoved` are per-outcome suspension flags.
- `originalOdd` is the provider's price before Setanta's markup; `odd` is what
  you bet. Displayed 1X2 margin is a flat ~5.3 % on real tiers vs ~8.2 % raw —
  Setanta hands back ~3 % of margin. Price against `odd`.

---

## 6. Market mapping — and the two traps

Identity is the tuple `(sport, resultKind, marketType, period,
marketParameters, outcomeType)`. **Never classify on a name**: the market
dictionary is condition-guarded (`sport:resultKind:period:…`) and resolving it
naively gives wrong labels — matching only on sport labelled a football Total
as "Total kicks", the penalty-shootout variant.

Verified codes (see the scraper docstring for how):

| Sport | Market | marketType | Outcome types |
|---|---|---|---|
| soccer `F` | moneyline (1X2) | 2 | 0=home, 1=draw, 3=away |
| soccer | total | 5 | 4=over, 5=under |
| soccer | spread (Asian) | 4 | 86=home, 87=away |
| soccer | team total | 7 | 37=over, 38=under; params `[team, line]` |
| basketball `B` | **moneyline incl OT** | **145** | 0=home, 3=away |
| basketball | total / spread / team total | 5 / 4 / 7 | as soccer |
| tennis `T` | moneyline | **1** | 0=home, 3=away |
| tennis | total / spread (games) | 5 / 4 | as soccer |

`marketParameters[0]` is the **signed HOME line** (verified against the
dictionary's sign-conditioned templates: `p1<0` renders `1 (-p1)`).

### Trap 1 — period codes are sport-specific

| Sport | Codes |
|---|---|
| soccer | 0=FT, 1=H1, 2=H2 *(no H2 in the v1 Period model → dropped)* |
| basketball | 0=FT, **4010=H1**, **1..4 = Q1..Q4**, 4011/4012=H2 (dropped) |
| tennis | 0=FT, 1..5 = sets (no Set period in the model → FT only) |

Mapping basketball period `1` to "H1" would pair a **quarter against a half**.

### Trap 2 — `subPeriod` is a minute window, not a sub-market

`(period=1, subPeriod=15)` is **"first 15 minutes"**, not the first half. On
CA Ferrocarril Midland – Patronato the 15-minute 3-way was 6.62/1.22/12.19
while the true H1 was 2.78/1.94/5.64. Emitting the former as H1 produced
**130 %+ phantom edges** against Pinnacle's real H1 on the first end-to-end
run — 271 "opportunities" collapsed to 53 once `subPeriod` was required to be
null. Only whole periods have a slot in the v1 model.

### resultKind

`resultKind` is the STATISTIC: 1=Goals/Points, 4=Corners, 8=Yellow cards,
32=Penalties, 66=Shots on target, … Corners and cards share marketType codes
with the main markets, so **only `resultKind == 1` is emitted**. Corners/cards
would need the `submarket` model and their own verification pass.

---

## 7. Energy

Measured 2026-07-26, one GE connection. Board: 25 sports, ~1 110 prematch
soccer events, horizon out to ~714 days.

| Step | Cost |
|---|---|
| Bootstrap (iframe HTML + schema) — once per hour | ~250 KB / ~0.5 s |
| Hub handshake | **0.08 s** |
| Tournament list (215 tournaments) | 24 KB / 0.11 s |
| **Whole-board enumeration**, pipelined | **362 KB / 0.3 s** |
| **Limited tier** (main markets, whole board, batch 200) | **415 KB / 0.3 s** |
| **Expanded tier** (full ladders, whole board) | **~8 MB / 4–7 s** |
| Per event, amortised | **7.5 KB / ~6 ms**, ~209 outcomes |

**Pipelining is the whole game.** Fire every subscription before reading any:
enumeration goes from 21.7 s to 0.3 s. Awaiting one stream at a time makes the
scraper ~50× slower than the feed can go.

Against the rest of the stack (full-board extended soccer sweep):

| Book | Sweep | Concurrency |
|---|---|---|
| **Setanta** | **4–7 s** | **1 socket** |
| Lider-Bet | 12 s | batches 50 |
| Crocobet | 20 s | 8-wide |
| Betlive | 24 s | 8-wide |
| CrystalBet | ~68 s + html5lib CPU | 8 sessions |

Two operational gotchas, both load-bearing in the scraper:

- **Cancel your streams** (`CancelInvocation`, type 5). A stream pushes deltas
  until told to stop, and leftovers steal decode time from the next sweep —
  back-to-back sweeps measured 4.2 s then 7.2 s purely from stale streams.
- **`ping_interval=None`** on the websocket client. Decoding an 8 MB initial
  batch starves the event loop long enough that `websockets` fails its own
  keepalive and drops the connection. The hub pings at the SignalR level.

To keep per-cycle decode CPU in line with Crocobet's budget, the scraper pulls
**full ladders only within `SETANTA_DETAIL_HOURS`** (default 24) and the cheap
main-market tier for everything farther out.

---

## 7b. Market-map verification (the `market-mapping-principles.md` gate)

Devigged (Shin) fair probabilities vs **Pinnacle** on live matched fixtures,
corners/bookings excluded from the reference side. Gate is a median ≤3pp:

| Sport | Markets compared | n | median | p90 |
|---|---|---|---|---|
| soccer | ML/total/spread/team_total, FT+H1 | 4 957 | **0.29–0.99pp** | ≤2.05pp |
| basketball | ML(incl OT)/total/spread, FT+H1 | 112 | **0.25–0.67pp** | ≤1.89pp |
| tennis | ML/total/spread FT | 566 | **0.40–0.77pp** | ≤2.04pp |

All PASS. Reproduce with the gate script pattern in `notes/build_log.md`
(2026-07-26).

**Watch out when re-running this:** Pinnacle folds corners/bookings into the
parent fixture tagged with `submarket`. Comparing a goals spread against a
corners spread at the same line gave a 27.8pp p90 that looked exactly like a
sign-convention bug and was purely a verification-script artifact — filter
`o.submarket` on the reference side.

---

## 8. Matching — no SportRadar id

`hasBetradarMapping` was **false on 383/383** events sampled across 10 sports,
no competitor carries `extraData`, and no `sr:match:` id appears anywhere. So
Setanta joins on **name+time fuzzy matching** like CrystalBet — not the exact
`sr_match_id` join we get free from Lider (`meta.matchProvider.matchId`),
Betlive (`providerEventId`) and Crocobet (`remoteId`).

Names are clean English at `lang=en` ("KuPS", "CA River Plate") and Setanta
tags youth sides ("Wollongong Wolves U-20"), which the existing
`has_youth_tag` guard (`\bu[- ]?(1[6-9]|2[0-3])\b`) handles.

**Known pre-existing hazard, not Setanta-specific:** where Pinnacle lists a
senior and a youth fixture under identical untagged names (Australian NPL
double-headers), Pinnacle's two events collapse into one matcher event group
and a soft-book senior line can be compared against the youth game's ladder.
Lider-Bet reproduces the same rows (Wollongong Wolves — Blacktown City, Total
FT +4, Δt 7200 s). `edge.match_confidence` already flags every such row
**weak**, which is how they were spotted.

---

## 9. Dashboard integration

Opt-in like the other extra books:

```bash
SETANTA=1 python main.py           # setanta=1 also accepted
```

| Where | What |
|---|---|
| `src/scrapers/setanta.py` | the scraper (`fetch_setanta_{soccer,basketball,tennis}`) |
| `src/models.py` | `"setanta"` added to `Source` |
| `src/app.py` | `_BOOK_FETCHERS["setanta"]`, `EXTRA_BOOKS`, `_LADDER_BOOKS` |
| `static/arbs.html` | book filter chip |
| `static/bets.html` | book tag option |
| `tests/test_setanta_parser.py` | 17 offline tests, fixtures in `tests/data/setanta/` |

`SETANTA_DETAIL_HOURS` (default 24) controls the full-ladder horizon. It is in
`_LADDER_BOOKS`, so its ladders feed the ladder-anomaly scan alongside Lider
and Crocobet.

---

## 10. Open questions

1. **Corners/cards** (`resultKind` 4/8/…) are on the wire and free to decode —
   they need the `submarket` model and a verification pass before shipping.
2. **Push mode.** The scraper does one-shot sweeps to fit the existing poller
   interface, which throws away Setanta's best property: subscriptions stay
   open and deltas arrive by themselves. Holding the whole soccer board open
   costs ~800–920 KB/min vs ~8 MB per re-sweep — ~10× cheaper *and*
   sub-second fresh. Wiring that in means a long-lived connection, a new
   shape for this codebase.
3. **Live stage** (`stage=2`) is the same hub and the same methods — the
   `live/` project could take Setanta almost for free.
4. **Geo unverified** — all probing was from a GE IP.
5. **Basketball totals/spreads incl-OT vs regulation** is not yet pinned;
   moneyline is unambiguous (mt 145 is explicitly "to win including
   overtime"), but confirm the total/spread convention before trusting
   basketball alt-lines the way we trust soccer's.
