<!-- Migrated from prematch_v2/docs/ on 2026-06-12 (v2 paused; these are the
     definitive scraping references, valid for v1 — the browser-free CB
     transport in src/scrapers/cb_http.py implements crystalbet.md, and
     src/scrapers/pinnacle.py implements pinnacle.md. Live probes:
     v1 scripts/probe_cb_http.py, v2 research/probe_*.py. -->

# CrystalBet scraping — reference

How to pull the full prematch odds catalog from crystalbet.com **without a
browser**. Everything here was re-verified live on **2026-06-11** against the
running site; where a claim is inherited from the v1 research and not
re-checked, it's marked _(v1, unverified)_.

> **Headline finding (new in v2):** the per-game detail expansion
> (`ExpandDetail`) — the full alt-line market table — works over plain HTTP
> with `curl_cffi`. v1 and all the old notes (`reference/cb_scraping.md`,
> `notes/cb_scraper.md`) assumed it required Playwright. It does not. This
> removes the single slowest, most fragile component of v1 (the per-game
> Playwright expansion at ~3 s/game). See §5.

Companion probe script that reproduces every claim below:
`../../prematch_v2/research/probe_crystalbet.py`.

---

## 1. What CrystalBet is

Classic **ASP.NET 4.x WebForms** on IIS. No SPA, no JSON API. Every page is
a server-rendered `.aspx`; the UI updates itself with **ASP.NET UpdatePanel**
partial-postbacks (`__doPostBack` + a handful of `DoXxxPostBack` JS helpers).
Odds are embedded directly in the returned HTML.

The whole prematch catalog is reachable from one page:

```
https://www.crystalbet.com/Pages/Sports.aspx
```

**Access:** CrystalBet blocks non-Georgian IPs (HTTP timeout / 000 behind a
non-GE VPN). Run from a Georgian IP or GE residential proxy. No Cloudflare,
no CAPTCHA, no bot-detection headers seen. `curl_cffi` Chrome impersonation
is sufficient; a User-Agent is the only header that truly matters.

---

## 2. The postback protocol (browser-free)

A scrape cycle is a sequence of form POSTs to `Sports.aspx`. The mechanics
of an ASP.NET UpdatePanel POST:

- The body is the **entire form** — every `<input type=hidden>` on the page,
  most importantly `__VIEWSTATE` (~6 KB), `__VIEWSTATEGENERATOR`,
  `__EVENTVALIDATION` — plus three control fields:
  - `__EVENTTARGET` — the control "raising" the postback
  - `__EVENTARGUMENT` — its argument
  - `ctl00$ctl00$MasteScriptManager` = `"<UpdatePanelID>|<TargetControlID>"`
    (note the typo `Maste` — it's really spelled that way in their markup)
  - `__ASYNCPOST = true`
- The response is **not HTML** — it's the UpdatePanel delta format:
  `length|type|id|content|length|type|id|content|...`, where `type` is
  `updatePanel`, `hiddenField`, `scriptBlock`, etc. You parse out the
  `updatePanel` segments for HTML and the `hiddenField` segments to refresh
  `__VIEWSTATE` for the next POST.

### The JS helpers (confirmed from `ts.crystalbet.sports.js`)

| JS call | `__EVENTTARGET` | `__EVENTARGUMENT` |
|---|---|---|
| `DoSportTypePostBack(p)` | `…ContentPlaceHolder2$UpdateSportTypes` | `p` |
| `DoChampionatPostBack(p)` | _(button click)_ — sets `HiddenFieldUpdateChampionatsParam=p`, clicks `…$ButtonUpdateChampionats` | — |
| `DoGamesPostBack(p)` | `…ContentPlaceHolder2$UpdateGames` | `p` |

`DoGamesPostBack` is the important one: `__doPostBack('…$UpdateGames', params)`
with `params` like `ExpandDetail:<gameId>` / `CollapseDetail:<gameId>`.

### Cycle

```
1. GET Sports.aspx
     → cookies: ASP.NET_SessionId, CL_Affinity (LB sticky), Device.Selected.Theme
     → scrape all hidden inputs (incl. __VIEWSTATE ~6 KB)
     → page already carries a handful of pinned games (73 on 2026-06-11)

2. (optional, recommended) Switch to English — see §6.

3. For each sport_type_id:
     DoSportTypePostBack(sport_id)
       SM       = …$UpdatePanelGames|…$UpdateSportTypes
       target   = …$UpdateSportTypes
       argument = str(sport_id)
       → clears the game view, returns the championship tree (all league IDs)

     For each championship_id in the tree:
       DoChampionatPostBack(champ_id)
         SM       = …$UpdatePanelsHolder|…$ButtonUpdateChampionats
         target   = …$ButtonUpdateChampionats   argument = ""
         HiddenFieldUpdateChampionatsParam = str(champ_id)
       → ADDS that championship's games to the accumulated view.
         Championships ACCUMULATE — they do not replace. The final response
         after loading all of them contains every game for the sport.
```

Refresh `__VIEWSTATE` from each response's `hiddenField` segments before the
next POST. `pageRedirect` in the first ~120 chars of a response = session
died; re-GET and restart.

### Measured timing (2026-06-11, single GE connection)

| Step | Time |
|---|---|
| GET Sports.aspx (1.3 MB) | ~1.1 s |
| `DoSportTypePostBack` (returns tree) | ~0.2 s |
| `DoChampionatPostBack` (per league) | ~0.2 s |
| `ExpandDetail` (per game, 355 KB) | ~0.3 s |

Football alone is **189 championships** → ~40 s just to load the football
tree at 0.2 s/champ + delay. v1's measured full-catalog run was ~4 min for
~1,980 events across 22 sports (vs the old Playwright list-scraper's ~85 min
for soccer alone).

---

## 3. Sport type IDs

Used as the argument to `DoSportTypePostBack`. Negative pseudo-IDs
(`-169`=TOP, `-1111`=LIVE, `-666`=FAV) are tabs, not sports — skip them.

```
16 Football   17 Basketball  18 Hockey      20 Handball    21 Volleyball
22 Tennis     23 Baseball    24 Rugby       26 TableTennis 29 WaterPolo
30 Boxing     33 Esports     61 Cricket     76 Snooker     104 GaelicFootball
105 GaelicHurling  112 Cycling  133 Golf    134 MMA/UFC    135 Cricket(alt)
226 Darts     227 Motorball
```

_(IDs from v1's POC sport list; football=16, basketball=17, tennis=22 re-verified live.)_

---

## 4. List-view odds — `data-loadInfo`

After loading championships, the games live in the `UpdatePanelGames` HTML.
Each match is a `div.GContainerList[data-id='<gameId>']`. Its top-line
markets are a JSON array in the `data-loadInfo` attribute on the child
`div.game_loading`.

### Match-level fields

| Field | Location | Example |
|---|---|---|
| Game ID | `data-id` on `.GContainerList` | `3047526043` |
| Teams | `div.teams_name` (live: `span.live_game`) | `Tukums 2000 - DFC Daugavpils` |
| Start time | `span.time` | `19:30` — **Tbilisi local, UTC+4**, bare HH:MM |
| Start date | per-day header inside the league block: `<div class='x_loop_date'><span class='date'>Friday</span><span class='teams'> - 12/06/2026</span></div>` | **dd/mm/yyyy with slashes** (v1 notes claimed dots — wrong on the live site). Assign each game the nearest date header above it; ~90 % of events have one. Combine with HH:MM − 4h → UTC |
| League | `div.game_hint > label` | |
| Championship | `span.champ_title` | |
| Has alt-lines | any loadInfo item with `HasAdditionalOdds == "True"` | → call ExpandDetail |

### `data-loadInfo` item shape

```json
{"name":"1","bet":"2.65","percent":"0.00","color":"#2b97fc",
 "handicap":"","id":"44867472847","HasAdditionalOdds":"False"}
```

- `name` — outcome label (see map below)
- `bet` — decimal odds as string; **odds ≤ 1.0 = suspended, discard**
- `id` — stable selection ID
- `handicap == "total"` — this item is a **total line value** (e.g. `bet:"2.5"`
  is the O/U line, not a price), not a bettable selection
- `HasAdditionalOdds` — `"True"` → full ladder available via ExpandDetail

### Two parsing gotchas (both verified live)

1. **Literal tab/control chars inside JSON strings.** The away "2" label
   often arrives as `"\t2"` (literal TAB). Python's `json.loads` rejects
   control chars in strings by default → **you must pass `strict=False`**,
   or pre-strip control chars. v1's POC (`_parse_loadinfo`) uses plain
   `json.loads` and silently drops these games. **Fix in v2: `strict=False`.**
2. **Trailing comma.** The array sometimes ends `...},]`. Strip with
   `re.sub(r",\s*]", "]", raw)` before parsing.

### Outcome label → market map

Labels are **language-dependent**. In English mode (recommended, §6):

| Label | Market | Selection |
|---|---|---|
| `1` / `X` / `2` (away often `\t2`) | 1X2 | Home / Draw / Away |
| `1X` / `12` / `X2` | Double Chance | 1X / 12 / X2 |
| `(0)1` / `(0)2` | Asian Handicap 0 | Home / Away |
| `Und ` / `over ` | Total Goals | Under / Over |
| `Goal` (`handicap:"total"`) | — | total **line** carrier |
| `Yes` / `no` | BTTS | Yes / No |

In Georgian mode the same positions read `ნაკლ.`/`მეტი ` (U/O), `კი`/`არა`
(BTTS), `გოლი` (total). Switching to English (§6) avoids needing the Georgian
table at all.

---

## 5. Detail-view odds — `ExpandDetail` (browser-free, NEW)

This is the full alt-line ladder + every secondary market for one game.

**Trigger** — `DoGamesPostBack('ExpandDetail:<gameId>')`, i.e. POST with:

```
ctl00$ctl00$MasteScriptManager = …$UpdatePanelsHolder|…$UpdateGames
__EVENTTARGET                  = …$ContentPlaceHolder2$UpdateGames
__EVENTARGUMENT                = ExpandDetail:<gameId>
__ASYNCPOST                    = true
+ all current hidden fields
```

### The one trick that makes it work without a browser

After `DoChampionatPostBack`, the returned `UpdatePanelGames` /
`UpdatePanelsHolder` HTML contains **new hidden `<input>`s that are not in the
`hiddenField` delta segments** — notably:

- `…$ContentPlaceHolder2$HiddenFieldExpandedGameId`
- `…$ContentPlaceHolder2$RepeaterChampionats$ctl00$HiddenFieldChampionatId`
- `…$ContentPlaceHolder2$HiddenFieldUpdateChampionatsParam`

A real browser form-serializes the live DOM, so these ride along
automatically. A raw POSTer must **re-scan hidden inputs out of the returned
panel HTML** (not just apply the `hiddenField` delta) and merge them into the
field set before the ExpandDetail POST. Without them the server returns a
near-empty delta (only the QuickBet panel) — which is exactly the dead-end
the first v2 probes hit. With them you get the full **355 KB** response.

Verified 2026-06-11: ExpandDetail on a Latvian Virslīga game returned a
`table.game-details` with **828 selection cells across 113 markets**, in
~0.3 s, no browser. A second ExpandDetail on the same session worked
identically (~0.23 s).

### Detail table DOM

```
table.game-details
  tr
    td.sport_more_td1  → MARKET TITLE A          (e.g. "Handicap", "Total goals")
    td.sport_more_td2  → selections for A
        div.sport_more_bt.DetailSnatch           (one per selection, paired s1/s2/…)
            div.sport_more_bt1 → label (line embedded, e.g. "1 (-1.5)", "Und 2.5")
            div.sport_more_bt2 → decimal odds
    td.sport_more_td3  → MARKET TITLE B          (2 markets per row)
    td.sport_more_td4  → selections for B
```

A representative excerpt is saved at `../../prematch_v2/research/cb_detail_table_excerpt.html`.

English market titles seen (first row of a football detail): `Main result`,
`Double chance`, `Both teams to score`, `Draw no bet`, `Total goals`,
`Handicap(1X2)`, `Halftime/Fulltime`, `Handicap`, `1st Half Result`,
`1st half - handicap`, `Total goals in 1st half`, `Multigoals`, … (113 total).

### Line / label parsing _(v1-proven)_

- Spread label `"1 (-1.5)"` / `"2 (+1.5)"` → `re.compile(r"^([12])\s*\(([+-]?\d+(?:\.\d+)?)\)$")`
- Total label `"Und 2.5"` / `"over 2.5"` → `re.compile(r"^(und(?:er)?|over|ov)\s+(\d+(?:\.\d+)?)$", re.I)`
- Score-handicap rows like `"1 (2:0)"` are a different market — detect the
  `:` and skip.
- Odds ≤ 1.0 = suspended → drop. Suspended markets may render the outer cell
  without the `bt2` odds div.

---

## 6. English language switch (browser-free)

POST a **full** (non-async) postback:

```
__EVENTTARGET = ctl00$ctl00$ImageButtonEn
__EVENTARGUMENT =
+ all hidden fields    (no MasteScriptManager / __ASYNCPOST)
Content-Type: application/x-www-form-urlencoded; charset=utf-8
```

Returns a full HTML page (starts `<!DOCTYPE`) in English; the language sticks
to the session cookies. After this, championship names ("Virslīga", "A Lyga"),
team names ("Tukums 2000 - DFC Daugavpils") and detail market titles all come
back English. Re-scrape hidden fields from this full page before continuing.

Verified 2026-06-11. Doing this first means the scraper never has to carry the
Georgian→market lookup table.

---

## 7. ID stability _(v1)_

| ID | Stability | Use |
|---|---|---|
| Game ID (`3047526043`) | stable while match exists, survives reloads | primary event key |
| Championship ID (`13882`) | stable across sessions | league filter |
| Selection ID (`44867472847`) | stable, tied to one outcome | odds tracking |
| `data-game-code` | external provider id (Flashscore?) | cross-ref only |

---

## 8. Open questions for v2

- **Do we even need the list view if ExpandDetail is cheap?** v1 used the
  change-cache to avoid re-expanding stable games because expansion cost
  ~3 s each via Playwright. At ~0.3 s/game over HTTP, expanding every game
  every cycle may be simpler than maintaining a change cache. Decide based
  on total game count × 0.3 s vs poll interval.
- **Session/ViewState lifetime under sustained polling** — not stress-tested
  here. v1 refreshed the session per sport to avoid ViewState bloat.
- **Live (`LiveBetting.aspx`) is out of scope** for prematch v2 — documented
  in `reference/cb_scraping.md` if we ever want it (timer-driven 5 s
  UpdatePanel POSTs; HTTP replay of `OpenGame` does NOT work cross-event
  because the server keys detail off ASP.NET session state).
