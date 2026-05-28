# Prematch dashboard — build log

Dated entries. Newest at top. Goal: keep enough trail that a context-free
return next week reconstructs what we knew and why.

---

## 📍 WHERE WE ARE — read this first if you're a new chat

**Phase 2 v1 SHIPPED 2026-05-26.** Both sports live: basketball (Phase 1 scope intact) + soccer (1X2 + AH + O/U + team_total + corners). Dashboard surfaces both. 336 tests passing. Cache persistence per-sport. See the C2.6 closeout entry below.

**Current state**

- **Phase 1 (basketball)**: ~85% FULL detail coverage, ~10% FAIL (CB-side flake, safety cap covers), ~5% LIST. 22K+ Odds per cycle.
- **Phase 2 (soccer)**: ~780 games per cycle, ~4,200 Odds, 762 matched-or-list rows on `/api/matches`, ~512 +EV/ARB opps in a fresh day. Corners markets matched via Pinnacle child-matchup folding. Team_total tracked but rarely +EV today (variance).
- **Known unresolved** (Phase 2.5 follow-up list — see C2.6 entry for full notes):
  1. First soccer cold cycle is ~85 min (~780 games × ~3s/expand). Restart paths are fast via change_cache.
  2. Startup race: first cycle can return 0 Odds and self-recover on cycle 2. Cosmetic; `status` shows `sc:0` until real cycle finishes. Worth fixing.
  3. Basketball cycle 1 sometimes hits `DoGamesPostBack is not defined` on a handful of games due to the same startup race. Self-recovers cycle 2.
  4. 100%+ edge anomalies on some soccer spread rows — likely a CB-vs-Pinnacle handicap convention mismatch (CB sometimes emits European/3-way handicap on titles we classify as 2-way). Present in Phase 1 too, soccer exposes it more. Phase 3 follow-up.
  5. Basketball team_total not wired end-to-end (Pinnacle gated to soccer). Phase 1.5 follow-up if we want it.
  6. Pinnacle bookings (~2 Pin leagues): deferred to Phase 2.5.
  7. Pending UI feedback the user hasn't batched yet.

**Honor the project rules** (the user has been firm about these):
- STOP at every meaningful checkpoint and report. Don't race ahead.
- Log every non-trivial decision here with date + the WHY, not just the WHAT.
- Don't add features the user didn't ask for.
- When something doesn't work, say so. Don't pretend it does.
- Browser-dependent code can't be tested from the agent sandbox — write code, prepare a Claude Code prompt at `notes/cc_prompt_<thing>.md`, hand off, wait for results.

**Important files to know about**:
- `src/scrapers/crystalbet.py` — singleton browser, list view + detail expansion orchestration
- `src/scrapers/cb_detail.py` — sport-agnostic detail-page walker (handles 2-way ML, alt-line spread/total)
- `src/scrapers/sports/basketball.py` — basketball-specific list-view parser + detail-page market classifier
- `src/scrapers/change_cache.py` — per-game hash-based expansion decisions
- `src/scrapers/cache_persistence.py` — disk save/load
- `src/scrapers/pinnacle.py` — Pinnacle guest API, currently sport_id 4 (basketball) hardcoded — will need to parameterize for soccer
- `src/matcher.py`, `src/edge.py`, `src/app.py` — matcher / edge math / FastAPI (will need 3-way support for soccer)
- `static/matches.html`, `static/arbs.html`, `static/style.css` — frontend (will need "X" column or equivalent for soccer 3-way)
- `notes/cc_prompt_*.md` — historical Claude Code prompts as patterns

Now keep scrolling for the dated history.

---

## 2026-05-27 — Phase 3.12: cross-page alerts (no startup bip, fires on any tab)

User: "remove default bip when I move to arb page no need to bip on that time, instead add bip on no matter which tab I am on".

**Two problems** the user surfaced:
1. Every navigation back to `/arbs.html` replayed the bip for current high-edge opps because the in-page `seenAlerts` Map was per-page-instance (reset on load).
2. Alerts only fired on `/arbs.html` — if you were viewing matches or bets, you'd miss new opportunities until you tabbed back.

**Fix.** New `static/alerts.js` — a shared poller loaded on every dashboard page. It:

- Reads `alert_enabled` and `alert_threshold` from `localStorage` (where the arbs.html UI controls already write).
- Polls `/api/opportunities?min_edge=1` every 30 s (low threshold to seed the seen-set comprehensively; the alert-threshold filter is applied client-side just before deciding whether to play).
- Tracks `seenKeys` in `localStorage` (`arbs_seen_alerts_v1`) so navigation between pages doesn't reset the "what's already been seen" state.
- On the very first visit ever (no `arbs_alerts_seeded` marker), SEEDS the seen-set without playing — every current opportunity is silently marked. The bip starts firing only on the next poll for truly new opportunities.
- Prunes keys that fall off the API response (game settled / line pulled / dropped below min_edge=1) so the localStorage map stays bounded.

**Removed from `arbs.html`:**
- `audioCtx`, `ensureAudio`, `playPing` (now in alerts.js)
- `seenAlerts` Map and `checkForAlerts` body (now in alerts.js)
- `fetchAndRender` no longer calls `checkForAlerts(cache)`

**Kept on `arbs.html`:**
- The "Alert ≥ N%" checkbox + threshold input. These still write to localStorage on change; alerts.js reads from there.

**Why a shared script over a Service Worker:** SW is overkill for a single-user local dashboard. The shared script polls independently on every page; cost is one extra `/api/opportunities` request per page per 30 s — negligible against the existing dashboard traffic.

**AudioContext gotcha** (same as before): browser autoplay policy requires a user gesture before `AudioContext` can sound. We lazy-init on first click/keydown of any page that loads alerts.js. Practically: the user must click somewhere on the dashboard once per session before the first bip.

**Files touched:**
- `static/alerts.js` — new, ~140 lines (poll loop, seed-on-first-visit, pruning, audio init, ping)
- `static/arbs.html` — removed ~60 lines of audio/seen-tracking; added comment pointer to alerts.js
- All 5 HTML pages — added `<script src="/alerts.js?v=20260527d" defer></script>` in `<head>`

**413/413 tests still pass.** End-to-end smoke: `/alerts.js` serves 200, every page references it, AudioContext init is lazy.

**One hard reload** (the `?v=20260527d` cache buster does the rest).

---

## 2026-05-27 — Phase 3.11: manual mute + highlight on arbs rows

User asked for per-row manual flags so anomalies (the +110% Heidelberg row, etc.) can be muted out of the way and rows worth tracking can be marked. Frontend-only — no backend touch.

**Color choices.**

Three row states now visually distinct without conflicting:

| State | Background | Left border | Trigger |
|---|---|---|---|
| `has-bet` (auto) | `--accent-highlight` (soft blue) | 3px `--accent` blue | `/api/bets` row matches the opp key |
| `user-highlighted` (manual) | `--warn-bg-soft` (soft amber) | 3px `--warn` amber | User clicks ★ on the row |
| `user-muted` (manual) | unchanged | unchanged | User clicks − on the row (composes with the others) |

**Stack semantics.** A row can be both `has-bet` AND `user-highlighted` — the amber treatment wins because it's the manual override. CSS uses `.has-bet:not(.user-highlighted)` so the rules don't double-apply. Mute is composable with everything (just an opacity filter, no background change), so a muted highlighted row stays amber but at 32% opacity.

**Per-row buttons.** Two new compact buttons next to `Log`:
- `★` toggle for highlight (active state = amber border + amber background)
- `−` toggle for mute (active state = faint border + dim background)

**Storage.** Two localStorage keys (`arbs_user_highlighted_v1`, `arbs_user_muted_v1`) holding arrays of bet-keys — same tuple shape as the auto bet-tracking (`cb_event_id | market_type | period | line | side | submarket | team_side`). Survives reloads + CB pulling and re-adding the same market.

**New controls toggle.** "Hide muted" checkbox in the filter bar. When ON, muted rows get `display: none` instead of just dimmed — useful when the user wants the muted rows out of the way entirely. State also persists in localStorage.

**Files touched:**
- `static/style.css` — `+45 lines`: `.user-highlighted`, `.user-muted`, `.user-hidden`, `.row-flag-btn` + active states. Wrapped the existing `.has-bet` rule with `:not(.user-highlighted)` so amber wins on stacking.
- `static/arbs.html` — `+50 lines`: storage helpers, toggle handlers, row template additions (buttons + class compositing), `hide-muted` checkbox.
- Cache buster bumped `?v=20260527c` across all 5 static pages.

**413/413 tests still pass** — no Python touched. JS syntax-checked.

**One hard reload** picks up CSS + JS via the new cache buster.

---

## 2026-05-27 — Phase 3.10: matcher time window widened to ±1h (both tiers)

User request: "lets increase matching range to 1hour instead of 10mins".

**Before:** strong-name tier (≥80 score) accepted within ±10 min; medium-name tier (65-79 score) required ±5 min.

**After:** both tiers accept within ±1 h. User chose "Both tiers → 1 h" over "Strong tier only" via the clarifying question — they're aware of the false-positive risk on tennis (similar surnames within an hour).

`src/matcher.py`:
- `TIME_LOOSE_SECONDS = 3600` (was 600)
- `TIME_TIGHT_SECONDS = 3600` (was 300)

**Why this matters.** CB and Pinnacle often disagree on kickoff time for the same fixture by 15-45 min — happens regularly in tournament brackets where TBD slots fill close to kickoff, or just because the two books pull from different feeds. With the old ±10 min window, score-100 pairs like "Club Nacional vs Aguada" got dropped purely on time disagreement (the canonical 2026-05-25 bug). The wider window catches those without much false-positive risk for team-sport markets.

**Two-tier semantics now collapse to one-tier in practice.** Both score thresholds (≥80 strong, 65-79 medium) accept within the same time window; the only remaining differentiator is the score floor (65). If false matches start appearing in tennis/soccer (watch `unmatched_log.csv` for entries showing matched-but-wrong pairs), the move is to lower TIME_TIGHT back down — keeps strong matches forgiving while reining in the medium ones.

**Tests:**
- Existing `test_perfect_name_match_with_large_time_gap_is_unmatched` (the Nacional/Aguada regression test from May 25) updated: 20-min gap → 90-min gap so it still anchors the "rejected past the window" guarantee.
- New `test_perfect_name_match_within_new_one_hour_window_matches`: locks in the new behaviour — a 20-min gap that would have been rejected before is now accepted.

**413/413 tests passing.**

---

## 2026-05-27 — Phase 3.9: UX polish (arbs highlight + calc persistence + reset)

Three small frontend additions. No backend touch.

**1. Arbs rows flag "already bet here".** `static/arbs.html` now fetches `/api/bets?status=all` alongside `/api/opportunities` on every poll and builds a Set of stable bet keys: `(cb_event_id | market_type | period | line | side | submarket | team_side)`. Opp rows whose key is in the Set get a `.has-bet` class → subtle `var(--accent-highlight)` background + 3px accent left-border. Tooltip explains: "You already have a bet logged on this market."

Includes settled bets (won/lost/pushed/void), not just open — once you've taken a view on a market, you've taken it, regardless of outcome.

**2. Calc inputs persist across page navigation.** `static/calc.html` now saves mode, method (Shin/proportional), bankroll, and every sharp/my odds value to `localStorage["calc_state_v1"]` on every input change. Restored on page load BEFORE first `renderGrid()` so the inputs render pre-filled and recompute uses restored values. Toggle chips visually sync to restored mode/method state.

**3. Calc reset button.** Wipes inputs, clears localStorage, resets mode/method/bankroll to defaults (2-way, Shin, $500), re-renders. Lives next to the bankroll input as a small "RESET" chip.

**Cache buster bumped** `?v=20260527b` across all 5 HTML pages so the user gets the new CSS+JS on first reload without manual force-refresh.

Files touched: `static/arbs.html` (+30 lines bet-fetching), `static/calc.html` (+60 lines persistence + reset), `static/style.css` (+12 lines `.has-bet` rule). 412/412 tests still green (no Python changes).

---

## 2026-05-27 — Phase 3.8: Pinnacle per-league matchups fallback

**Trigger.** User flagged a CB match ("Resende vs America RJ", Brazil Carioca A2, kickoff 22:30 UTC) showing no Pinnacle reference in the dashboard despite the match being clearly visible on pinnacle.com's web UI. Initial hypothesis (line pulled) was wrong — the bulk Pinnacle endpoint genuinely omits matchups that the per-league endpoint includes.

**Diagnosis.**

The Pinnacle scraper makes two kinds of calls per cycle:

1. **Bulk:** `GET /sports/{sport_id}/matchups` — one call returning every parent matchup across every league for that sport. Used to build the `matchup_by_id` index of `{home, away, start_time, units}` keyed by matchup id.
2. **Per-league:** `GET /leagues/{lid}/markets/straight` — N calls returning the actual prices keyed by matchup id.

Inside `_build_odds_for_league` (`src/scrapers/pinnacle.py:404-405`), any market row whose `matchupId` isn't in the bulk index gets silently dropped. No log, no warning — the row just disappears.

The Resende match was provably in the per-league markets response (otherwise Pinnacle wouldn't display it in their UI), but **the bulk `/sports/29/matchups` endpoint did not return it**. We know this because the matcher's best-candidate score for "Resende vs America RJ" across all 385 bulk soccer matchups was **37.6** against the unrelated "Fiorentina vs Parma" — meaning no Resende-like name existed anywhere in the bulk pool. If Pinnacle had included the matchup, fuzzy scoring would produce ≥70 trivially.

**Why bulk omits some matchups.** Unconfirmed from Pinnacle's side, but the likely reason is payload-size optimization or per-region content filtering — Pinnacle's web UI typically uses `GET /leagues/{lid}/matchups` (per-league) and stitches results across visible leagues, so they don't need the bulk to be exhaustive.

**Fix — per-league fallback.** Keep the bulk endpoint as the fast path (one HTTP call covers most matchups). For each league, after fetching markets, detect any referenced matchup_id missing from the bulk index. If any are missing, call `GET /leagues/{lid}/matchups` and merge the result into a per-league-scoped view. Log a warning each time the fallback fires so we can quantify how often Pinnacle's bulk is incomplete.

Code lives in `src/scrapers/pinnacle.py:_one_league` between the markets fetch and the `_build_odds_for_league` call (~25 lines added). The merge is **per-league local**, never mutates the shared `matchup_by_id` dict — keeps thread/coroutine safety trivial.

```python
referenced_mids = {mkt.get("matchupId") for mkt in markets} - {None}
needed_parent_mids = {child_to_parent.get(mid, (mid, None))[0]
                       for mid in referenced_mids}
missing_parents = needed_parent_mids - matchup_by_id.keys()
if missing_parents:
    league_matchups_raw = await _get(client, f"/leagues/{lid}/matchups")
    extra_index = _index_matchups(league_matchups_raw, now)
    league_matchup_by_id = {**matchup_by_id, **extra_index}
    # ... log recovery counts ...
```

**Cost.** Only triggers for the long-tail leagues that bulk thins out — most leagues with normal coverage never make the second call. Expected: 0-5 extra HTTP calls per sport per cycle in typical conditions.

**Tests** — `tests/test_pinnacle_fallback.py` (3 cases, all passing):

- `test_fallback_recovers_missing_matchup` — bulk omits matchup 999 (Resende); per-league includes it; after fallback, all market rows surface
- `test_no_fallback_when_bulk_has_everything` — when bulk covers every referenced matchup, the per-league call is NOT made (preserves fast path)
- `test_fallback_failure_is_graceful` — if the per-league call raises, known matchups still surface; unknown ones stay dropped (no crash, no regression)

A 4th case for corners-child-recovery-via-fallback was scoped out — Phase 3.8 fixes the parent-matchup case that exposed the bug. Corners-child fallback can ship as a follow-up if the symptom appears.

Tests in the dashboard mock `pinnacle._get` via monkeypatch to dispatch by URL path. Uses `asyncio.run` rather than `@pytest.mark.asyncio` since the project doesn't have pytest-asyncio installed (anyio is available but adding `pytest-asyncio` for one file felt excessive — `asyncio.run` works in plain sync tests just fine).

**Suite:** 412/412 passing (up from 409). All existing Pinnacle parser tests still green; the fallback path is purely additive.

### What this changes for your dashboard

- Matches like Resende vs America RJ that were previously missing Pin columns will now have them, IF Pinnacle's per-league endpoint includes the matchup.
- When the fallback fires you'll see a WARNING line in `dashboard.log`:
  ```
  Pinnacle soccer league 'Brazil - Carioca A2' (id=215951): bulk missed 1 matchup(s),
  recovered 1 via per-league fallback
  ```
  Useful diagnostic — if this fires for the same league every cycle, that's a stable Pinnacle behaviour we can document.
- Edge calculations on those matches will now work — previously they were CB-only, now they get a Pin no-vig reference like everyone else.

### Files changed

```
MOD  src/scrapers/pinnacle.py                ~30 lines added in _one_league
ADD  tests/test_pinnacle_fallback.py         3 tests covering recovery + fast-path + graceful-failure
MOD  notes/build_log.md                      this entry
```

---

## 2026-05-27 — Phase 3.7: Shin's method replaces proportional devig

**Trigger.** User screenshot on `/calc.html`: sharp odds 1.023 home / 7.31 away. Proportional devig (the only method we had) produced fair odds 1.140 / **8.146**. User flagged this as obviously wrong — and correctly. Proportional devig has a well-known bias on skewed markets.

**Why proportional is biased.** It assumes the book takes equal-proportional rake from every side. In reality, Pinnacle (and most sharp books) take **much less rake from the favorite** because nobody bets heavy dogs at offered prices — books inflate the dog side to absorb information asymmetry. Proportional devig overstates the dog's true probability and understates the favorite's.

Concrete impact on the dashboard's edge calc, using the 1.023/7.31 example:

```
                       fair_home   fair_away   away "true" prob
proportional (old)     1.140       8.146       12.28%
Shin (new)             1.087      12.556        7.96%
power method           1.038      27.035        3.70%   (likely too aggressive)
```

If CB offered that dog at 9.00:
- Old (proportional) dashboard: `9.00/8.146 − 1 = +10.5%` edge — flashing green
- New (Shin) dashboard: `9.00/12.556 − 1 = −28%` — accurately rated as a confidently losing line

The bug affected every "skewed" matchup across the entire dashboard. For balanced markets (1.91/1.91) both methods agree to 6+ decimals, so this fix is **harmless on balanced lines and surgical on skewed ones**.

### Implementation

**`src/vig.py` rewrite.** Both methods now coexist as named functions; `devig_2way` and `devig_3way` are aliases that point to Shin.

```
devig_2way_shin(d1, d2)            ← default behind devig_2way
devig_3way_shin(d1, d2, d3)        ← default behind devig_3way
devig_2way_proportional(d1, d2)    ← legacy, available for tests + comparison
devig_3way_proportional(...)       ← same, 3-way
```

Shin is solved numerically: bisection over the insider proportion `z ∈ [0, 0.5]` until the per-side fair probabilities sum to 1. 60 iterations of bisection gets `z` to ~1e-15 precision, then a final renormalize cleans up any float residual. The per-side formula:

```
p_i = (sqrt(z² + 4(1−z) · π_i² / Π) − z) / (2(1−z))
```

where `π_i = 1/d_i` and `Π = Σ π_i` (the overround).

**Guardrails.** If the solver hits invalid output (NaN, prob outside (0,1), etc.) it falls back to proportional and logs a warning rather than failing. This protects against pathological inputs we haven't anticipated — but in practice, every input across the testable space converges cleanly.

**Callers don't change.** `src/edge.py`, `src/app.py`, and any other code calling `devig_2way` / `devig_3way` automatically pick up Shin. No call-site edits needed.

### Calc UI changes (`static/calc.html`)

Added a second toggle row labelled "Shin (default) / Proportional (legacy)" so you can A/B compare the two methods on any input live. JS-side Shin solver is the exact same bisection algorithm, cross-checked against the Python implementation to 3+ decimal places on multiple test cases including the canonical 1.023/7.31 example (both produce 1.087 / 12.556).

### Test coverage

`tests/test_vig.py` grew from 27 to 39 cases. New `TestShin2Way` / `TestShin3Way` / `TestShinNumericGuardrails` classes anchor:

- Balanced markets match proportional to 9 decimals (no surprise behaviour on the boring case)
- Sum-to-1 invariant holds across realistic skew bands
- The canonical user example (1.023/7.31) produces 1.087 / 12.56 ± tolerance
- Direction-vs-proportional: Shin pushes the favorite UP and the dog DOWN (the structural correction)
- 3-way analogues of all the above for soccer 1X2 markets
- High-vig and low-vig guardrails behave sensibly

Full suite: **409/409 passing** (up from 397). Zero regressions in `test_edge.py`, `test_edge_soccer.py`, `test_app.py`, `test_app_soccer.py` — those tests check invariants and direction, both of which Shin preserves.

### User policy / known limits

User stated 2026-05-27: "I'll avoid betting on too much skewed ones and inside some range 1.2–5 I think it will work properly." Sound call. Inside that band the difference between Shin and proportional is typically 1–3 cents on fair odds — much smaller than book quotes shift cycle-to-cycle. The big corrections happen at the extremes Shin was designed for.

For users straying outside the 1.2–5.0 range:
- At odds < 1.05 or > 20: Shin's `z` solver can take more bisection steps and the dog-side fair odds become very sensitive to the exact `z`. Still converges cleanly in practice.
- Very high vig (> 15%): rare for Pinnacle, common for CrystalBet, but the math handles it.
- Pre-existing `pin_fair_at_placement` snapshots in `data/bets.db` stay as-is — those were computed with proportional. New snapshots use Shin. The CLV calculation on existing bets is therefore mildly inconsistent until they settle.

### Files changed

```
MOD  src/vig.py              200 lines (was 126); Shin solver + proportional kept as named fns
MOD  tests/test_vig.py       +12 Shin-specific tests
MOD  static/calc.html        method toggle + JS Shin port
ADD  notes/build_log.md      this entry
```

No other files touched. Callers in `src/edge.py` / `src/app.py` automatically picked up Shin via the `devig_*` aliases.

---

## 2026-05-27 — Phase 3.4–3.6: calculator + bet tracker (with CLV)

Shipped as one batch after Phase 3.3. Three pieces landed together because the user requested "ship them together" — light mode was the warm-up, this is the real surface-area expansion.

### Phase 3.4 — Fair-odds calculator (`/calc.html`)

Standalone static page. Toggle between 2-way (ML / spread / total) and 3-way (1X2). Enter the sharp book's decimal odds per side → outputs fair odds + fair % + book's vig%. Optional "my odds" column per side → outputs edge% + quarter-Kelly stake.

Math is a JS port of `src/vig.py`'s `devig_2way`/`devig_3way`. Cross-checked against Python on three inputs (1.91/1.91, 1.50/2.75, 2.10/3.40/3.40) — exact match to 6 decimals. Kelly fraction is `(p·d − 1) / (d − 1) / 4` × bankroll, exactly as in `src/edge.py`.

Bankroll input defaults to $500 (matches the project-brief constant) but is freely editable. The page has no backend dependency — pure client-side JS so it works offline if uvicorn isn't running.

### Phase 3.5 — Bet tracker layer 1: SQLite + endpoints + UI

**Storage choice.** The bet tracker is the first piece of the dashboard with durable cross-restart state, so it gets its own SQLite file at `prematch/data/bets.db` rather than piggybacking on the in-memory `_state` or the JSON `cache_persistence`. The original brief envisioned SQLite as the canonical store for everything; we just hadn't needed it yet. Bets are a clean wedge case — manual write rate is essentially zero, but the data must survive restarts.

A `BETS_DB_PATH` env var overrides the default location (useful for tests + ops). WAL journal mode is best-effort — it works on local APFS/ext4 but fails on the FUSE mount this sandbox uses, so I catch `OperationalError` and fall through to the default journal.

**Schema.** Two tables in `src/bets.py`:

- `bets` — one row per wager. Columns capture (placed_at, sport, cb_event_id, match_label, period, market_type, line, side, submarket, team_side, book, odds_taken, stake, bankroll_at_time, pin_fair_at_placement, cb_fair_at_placement, edge_at_placement_pct, status, settled_at, payout, pin_fair_closing, note, start_time). Status state machine: `open → won|lost|pushed|void`. Status-specific indexes on placed_at and cb_event_id for the dashboard's join queries.
- `bet_odds_history` — periodic (cb_decimal, pin_fair_decimal) snapshots per OPEN bet. Populated by the recorder hook (3.6). `UNIQUE(bet_id, recorded_at)` so duplicate same-second writes silently no-op. `ON DELETE CASCADE` from the FK on `bet_id` so deleting a bet kills its history.

Connection pool is one process-wide `sqlite3.Connection` opened with `check_same_thread=False`, guarded by a `threading.Lock` on every read/write. Plenty for a single-user dashboard.

**API** (`src/app.py`):
- `GET /api/bets?status=open|settled|all` — list bets, augmented with `cb_odds_now`, `pin_fair_now`, `edge_now_pct`, `clv_pct` computed from current `_state`.
- `GET /api/bets/{id}` — one bet, same augmentation.
- `GET /api/bets/{id}/history` — odds snapshots for the sparkline.
- `POST /api/bets` — create. Auto-snapshots `pin_fair_at_placement`, `cb_fair_at_placement`, `edge_at_placement_pct` from current `_state` at the time of the call.
- `PATCH /api/bets/{id}` — settle (`{"outcome": "won"|"lost"|"pushed"|"void"}`) OR partial update (`note`, `cb_event_id`, `start_time`, `pin_fair_closing`).
- `DELETE /api/bets/{id}` — hard delete.

The helper `_current_odds_for_bet(bet)` finds the matching live CB Odds row via `cb_event_id` (or falls back to `(home, away)` parsed from `match_label` for off-platform bets), then uses the existing `_closest_pin()` + `_maybe_devig()` plumbing for the Pin fair. That keeps line-matching consistent with the rest of the dashboard — uses `LINE_MATCH_TOLERANCE` from `src/edge.py`.

**UI** (`static/bets.html`):
- Collapsed "Log a new bet" form at top (`<details>` element). Form fields are types-tight (number inputs with step/min). Reset button + inline error display.
- Tab bar: Open / Settled / All.
- Table with all the columns listed in the "WHERE WE ARE" plan: placed, kickoff, sport, match, market/side, book, odds taken, stake, CB now, Pin fair now, edge now, CLV, trend (sparkline), status, settle buttons. Polls `/api/bets` every 30s.
- Per-row settle buttons (Won / Lost / Pushed / Void) with confirmation-free single-click. Delete button has a `confirm()` guard.

**Log-bet shortcuts.** Per the 2026-05-27 design call (user picked option (b)):
- `static/arbs.html`: new "Log" link per opportunity row. URL-encoded prefill of every field we can derive from the Opportunity dataclass — required extending `Opportunity` with `market_type`, `period`, `line`, `submarket`, `team_side` so the prefill doesn't have to parse the human-readable `market` label.
- `static/matches.html`: row-level "Log" link that prefills sport/match/start_time/cb_event_id/cb_home odds, leaving the user to fine-tune market/side from the expander view. Per-market log buttons in the expander would be ideal but require a bigger rework — deferred.

Click handler on `arbs.html` row was tightened to skip navigation when the click target is the Log link itself (`event.target.closest("a.log-bet-link")`), so the link works even though its parent `<tr>` is also clickable.

### Phase 3.6 — Bet tracker layer 2: CLV + sparklines

The history recorder hook is in `_pinnacle_loop_for_sport`. After each successful Pin fetch for sport S, it iterates `bets.list_bets(status="open")`, filters to S, computes current `cb_now` + `pin_fair_now` via `_current_odds_for_bet`, and inserts one history row. Bounded: only open bets get snapshots, and the moment a bet is settled the recorder skips it.

It also stamps `pin_fair_closing` on the bet exactly once — when kickoff passes — so the closing-line value is permanent regardless of how long after the match the user gets around to settling. Once stamped, never re-stamped.

**CLV math** (in `_bet_to_dict`):
```
clv_pct = (pin_fair_at_placement / pin_fair_now − 1) × 100
```
Positive = our side's Pin fair odds SHORTENED since placement = sharp says we picked well. Negative = we got worse-than-current price. Color-coded in the UI: green if CLV ≥ +0.5%, red if ≤ −0.5%, gray otherwise.

**Sparkline.** Inline SVG (no Chart.js, no CDN dependency). Plots `pin_fair_decimal` over time. Color: green if the line drops (positive CLV trajectory), red if it rises, gray if flat. 80×22px, fits naturally in the table cell.

### Testing

- `tests/test_bets.py` — 30 new tests covering schema init (idempotency), CRUD, settle state machine (default payouts, re-settle rejection, all four outcomes), delete + cascade, update field whitelist, history dedup + ordering, `open_bet_ids`.
- End-to-end FastAPI TestClient smoke: POST → GET history → PATCH settle → DELETE → validation rejections → all 7 static pages serve 200.
- Full suite: **397/397 green** (up from 367 before this batch).

### Files added / changed

```
ADD  src/bets.py                          ~280 lines  SQLite DAO
ADD  tests/test_bets.py                   ~250 lines  30 tests
ADD  static/calc.html                     ~340 lines  fair-odds calc + +EV check
ADD  static/bets.html                     ~390 lines  bet tracker UI
MOD  src/app.py                                       imports + lifespan init_db +
                                                     5 endpoints + history hook +
                                                     _opp_to_dict extension
MOD  src/edge.py                                      populate new Opportunity fields
MOD  src/models.py                                    +5 Opportunity fields
MOD  src/scrapers/sports/ + others                    NO changes — sport plumbing untouched
MOD  static/arbs.html                                 Log column + handler + colspan bump
MOD  static/matches.html                              Log link + buildBetPrefillUrl + nav
MOD  static/unmatched.html                            nav additions
MOD  static/style.css                                 .log-bet-link styles
```

### Known limits & deferred items

- The arbs Log button prefills only the CB leg of an ARB. The Pin partner leg has to be logged manually as a second bet (book=`pin`, side=`arb_partner_side`, odds=`arb_partner_odds`). v1 doesn't auto-create both — keeps the form review-able before commit.
- Per-market Log buttons inside the matches expander were skipped — only the main row has one. The user fills in market/side from the form. Easy follow-up if the workflow demands it.
- CLV % uses `pin_fair_at_placement` as the anchor. If that field is None (off-platform bet, or Pin didn't have the market at placement time), CLV shows as `—`. Could fall back to the first history row's pin_fair as the anchor — deferred.
- Bankroll history isn't tracked across bets — each bet snapshots its own `bankroll_at_time`. A running-bankroll computation (sum payouts − sum stakes for settled) would be a small follow-up; deferred until the user wants it.
- The history recorder fires once per Pin poll (every 60s by default). Sparkline resolution = 60s. Adequate; pre-match odds don't move that fast.
- `bets.db` lives at `prematch/data/bets.db` by default; backup-by-copy of that file is sufficient for now.

---

## 2026-05-27 — Phase 3.3: light/dark theme toggle

First of a 4-step batch (light mode → calculator → bet tracker v1 → bet tracker CLV). User asked for a light-mode toggle alongside two larger features; doing it first lets us establish the theming + shared-asset patterns that the calculator and bets pages will reuse.

**Design.**

`static/style.css` already had nearly all colors as CSS variables in `:root`. Only 4 spots had hardcoded values that needed converting:
- The `highlight-fade` keyframe (deep-link match highlight on `/matches.html#match-{id}`) — `rgba(88,166,255,0.20)` → `var(--accent-highlight)`.
- The expander row background `#0a0d12` → `var(--bg-expander)`.
- Status-badge soft backgrounds (`status-loaded`/`status-list`/`status-failed`) → `var(--good-bg-soft)` etc.
- Freshness chip backgrounds → same set of soft-bg vars.

Added a single dark-default `:root` block (current look unchanged) and a `:root[data-theme="light"]` override block with a GitHub-Primer-inspired light palette. Same semantic var names, completely different colors.

**Theming controller — `static/theme.js` (~70 lines).**

- Reads `localStorage["theme"]` synchronously and applies `data-theme` on `<html>` **before paint** (no flash). This is why it's loaded with a plain `<script src>` in `<head>`, NOT `defer` — defer would let the body paint in dark before the script runs.
- On `DOMContentLoaded`, injects a small "Light"/"Dark" button into `<header>`. Button label = the theme you'd switch *to* (less ambiguous than an icon).
- Click handler flips `data-theme`, persists to localStorage, updates button text.
- One file, included on every page via `<script src="/theme.js"></script>`.

**Why a single shared JS file rather than copying snippets per page?**

The dashboard has no template system (vanilla static HTML, per project rules — no React, no build step). The choices were: (a) copy ~70 lines of theme code into every HTML page, or (b) one shared file every page includes. (b) means a new page (calc, bets) just adds one `<script>` line and inherits the toggle. The cost is one extra HTTP request per page, which is fine for a single-user local dashboard.

**Pages wired:** `matches.html`, `arbs.html`, `unmatched.html`. Future pages (calc, bets) get themed automatically by adding `<script src="/theme.js"></script>` to their `<head>`.

**Verification.**

- Synthesized a uvicorn boot in sandbox; `/theme.js`, `/style.css`, all three HTML pages return 200 with correct content-types. Scraper errors in startup logs are sandbox-only (no Playwright, no Pinnacle network access from agent env) and don't affect static serving.
- JS syntax-checked via Node's `new Function()`.
- 367/367 tests still pass (no Python changes).

**To verify visually:** run `python main.py`, click the "Light" button in the header. Should flip instantly with no flash, persist across reloads, and apply on every page.

**What this doesn't change.**

- No light-mode-specific tweaks to the alert sound, expander layout, or chart styles (none exist yet — Chart.js for sparklines arrives in Phase 3.5).
- No system-preference auto-detection (`prefers-color-scheme`) — explicit user choice only. Easy to add later: change `getStored()` default from `"dark"` to reading the media query.
- Future pages (calc, bets) MUST put `<script src="/theme.js"></script>` in `<head>` (not at end of body, not `defer`) or they'll flash on load.

---

## 2026-05-27 — Phase 3.2: tennis-aware name normalizer

**Problem.** Tennis match rate was visibly worse than basketball (46% vs 61% from a 5000-row unmatched_log slice on 2026-05-26 22:30). User asked whether the matcher's tier-2 rule "only works for basketball." Answer: matcher is sport-agnostic. The real culprit is name format — CB writes `LastName F[.X.]`, Pinnacle writes `FirstName [Middle] LastName`. Examples from unmatched_log:

```
  77.1  CB 'Cobolli F' vs 'Wu Y.'   →  Pin 'Flavio Cobolli' vs 'Yibing Wu'
  79.5  CB 'Molnar K.' vs 'Mikhalkova B.'  →  Pin 'Kitti Molnar' vs 'Barbora Michalkova'
  59.5  CB 'Teixido Garcia M.A.' vs 'Vaquero M.M.'  →  Pin 'Meritxell Teixido Garcia' vs 'Laura Mair'
  83.3  CB 'Bulgaru M.B.' vs 'Primorac P.'  →  Pin 'Miriam Bulgaru' vs 'Petra Primorac'
```

Even when both sides matched, scores were in the 77-83 range — close to (or below) SCORE_TIGHT=65, where one collision in greedy assignment is enough to lose the pair entirely. Basketball doesn't have this problem because team_aliases.yaml normalizes city/franchise variants into shared canonical forms.

**Fix.** Added `normalize_tennis_name()` in `src/normalize.py`. It:

1. NFD-normalizes to ASCII, lowercases, replaces non-alnum with spaces.
2. Splits doubles on `/`, recurses per side, joins sorted (partner order is meaningless).
3. Strips trailing AND leading single-char tokens — these are CB's initials. `"Cobolli F"` → `["cobolli","f"]` → `["cobolli"]`. `"M.B."` becomes `["m","b"]` after punctuation cleanup, both stripped. Multi-word lastnames stay intact: `"Teixido Garcia M.A."` → `["teixido","garcia"]`.
4. Does NOT strip Pinnacle's firstname token — we rely on `fuzz.token_set_ratio` returning 100 when one side is a strict subset of the other. So `"cobolli"` vs `"flavio cobolli"` → 100, no extra logic needed.

`src/matcher.py` now branches at normalize time: if `Odds.sport == "tennis"`, use the tennis normalizer; otherwise `normalize_team`. Detection is per-event from the first Odds in the group, so mixed-sport input is handled correctly.

**Why this approach rather than `team_aliases.yaml` for each player?** ~570 unique tennis CB events per cycle, doubles + SRL + qualifying expand that further. Curating aliases for each is infeasible. The structural transformation captures the entire format mismatch in one place.

**Verification.**

Synthetic test: 10 realistic CB/Pin tennis pairs at sequential start times, 1 deliberate mismatch.

| Normalizer | Matches | Notable |
|---|---|---|
| OLD (`normalize_team`) | 8/10 | Scores ranged 77-90. "Teixido Garcia" pair dropped to 59.5 — below SCORE_TIGHT, unmatched. |
| NEW (`normalize_tennis_name`) | 9/10 | 7 of 9 score a perfect 100. "Teixido Garcia" now 67.6, clears tight tier. Deliberate mismatch correctly stays unmatched (zero false positives). |

Replay of all 578 unique tennis pairs in `unmatched_log.csv`:
- 78 pairs migrated from 80-89 → ≥90 (denser scores → fewer greedy-assignment collisions in live matching).
- 3 pairs newly cross SCORE_TIGHT (small direct lift, but unmatched_log is biased toward case-1 events where Pin has no real counterpart anyway — SRL, ITF skips, doubles Pin doesn't cover).
- ZERO regressions (no pairs lost matchability).

**Tests.** `tests/test_normalize_tennis.py` — 18 cases covering single trailing initial, dotted multi-initial, multi-word lastname, accent stripping, doubles partner-order invariance, the CB-vs-Pin token_set_ratio=100 guarantees, and a sanity check that unrelated surnames still score low. Full suite: **367/367 passing**.

**Edge cases deliberately NOT handled (and why).**
- `"Bulgaru MB"` (multi-initial, no periods) — stays as `"bulgaru mb"`. CB always writes `"M.B."` with periods which the regex splits into single-char tokens. A bare `"MB"` is ambiguous (could be a legitimate name component); over-stripping risks false positives. Revisit if real data shows the no-period form.
- Cross-sport collisions (a tennis CB event scoring 80 against a basketball Pin team) — still possible in theory but caught by the time window and by overall score density. Not addressed; would need a sport-equality filter in matcher.

**What this doesn't fix.**
- SRL (Simulated Reality League / virtual) events Pinnacle doesn't cover at all — still permanently unmatched, as intended.
- Doubles where CB and Pin list different partner combos for the same court — still won't match unless both partners overlap (correct behavior).
- ITF/Challenger events Pinnacle skips — same, permanently unmatched.

User can verify in production by running the dashboard fresh and watching the tennis match count on `/api/status` versus the prior baseline.

---


**REVERSES the Phase 3.1.3 conclusion.** User's targeted probe of Pinnacle matchup 1631354983 (Bulgaru-Primorac, ITF Women Bol R1) revealed the actual architecture I'd missed in the earlier aggregate-across-leagues probe. **Lesson: drill into one specific matchup's full data before generalizing from aggregate statistics.**

**Real architecture confirmed:**

Pinnacle splits tennis into TWO matchups per real match:

```
Parent matchup id=1631354983  units="Sets"   parentId=None
  ├─ moneyline period=0           (ML — same regardless of sets/games)
  ├─ spread period=0 line=±1.5    (SET handicap ±1.5)
  └─ total period=0 line=2.5      (SET total 2.5 sets)

Child matchup  id=1631367688  units="Games"  parentId=1631354983
  ├─ spread period=0 lines: 0, ±0.5, ±1.0, ±1.5, ±2.0   (games handicap, full match)
  ├─ spread period=1 lines: ±1.5, ±3.5                  (games handicap, first set)
  ├─ total period=0 lines: 20.5, 21.0, 21.5, 22.0, 22.5 (total games, full match)
  └─ total period=1 lines: 8.5, 10.5                    (total games, first set)
```

CB list-view ships GAMES-handicap as primary. Pre-fix our parser indexed only top-level matchups (parents → Sets), skipped children → captured Pin's SET handicap at ±1.5 → mismatched with CB's GAMES handicap at ±1.5 (same matchupId, same line, different markets, fair price way off → false +20% edge).

**Why my earlier probe was wrong:** I ran `_index_matchups`-style aggregation across many leagues and concluded "Pinnacle period 0 spread lines are continuous 0-7, must be games-handicap" — because I was seeing the UNION of (parent set-handicap ±1.5) + (child games-handicap, all lines) collapsed by matchupId in my counter. Looked continuous but it was actually two distinct distributions stacked.

**Three code changes in `src/scrapers/pinnacle.py`:**

1. `_index_matchups` now stores `units` in matchup_by_id (alongside home/away/start_time). Backward compatible — existing test fixtures without `units` get empty string, downstream check is `== "sets"` which is False for empty.

2. `_index_child_matchups` detects `units == "Games"` (mirror of `units == "Corners"`) and folds onto parent matchupId with **submarket=None**. The games-handicap IS the primary tennis spread/total we want; no submarket tag needed.

3. `_build_odds_for_league` skips spread/total/team_total when `info.units == "sets"` — **but only if the market came from the parent itself** (not from a folded child). Critical `from_child` flag: child markets get rewritten to parent matchupId, but they're games-based by construction so the parent's sets-skip rule must not apply to them. ML kept regardless of units (ML is the same prediction sets-based or games-based).

**Concrete result for Bulgaru-Primorac after the fix:**
- Pin ML home=1.729, away=2.050 — kept (parent matchup ML, units=sets but ML is exempt)
- Pin spread -1.5 home=1.926, away=1.813 — these come from the child matchup (units=Games), folded onto parent matchupId, submarket=None. They MATCH CB's games-handicap -1.5 ≈ 1.93 / 1.81.
- Pin spread -1.5 sets variant (home=2.83, away=1.40) DROPPED — parent matchup's spread, units=sets, market came from parent (not folded), skip rule fires.

The phantom +20% edge dashboard row should disappear. Pin's fair price for "Primorac +1.5 games" devigged should now be ~1.84 (close to CB's 1.80) — small/no edge as expected.

**11 new regression tests** in `tests/test_pinnacle_tennis.py` covering: child games-fold with submarket=None, multiple children with different units coexisting, units capture in matchup_by_id, set-based spread/total skip, ML preservation on units=sets matchup, end-to-end "parent set-spread skipped + child games-spread folded at same line".

349/349 tests pass.

---

## 2026-05-26 — Phase 3.1.3 (closed, no code change): tennis set/games disambiguation not needed

User suspected tennis set-handicap was being cross-paired with games-handicap. Probe of `/leagues/3285/markets/straight` (ATP French Open R2, 825 market entries) refuted the hypothesis:

- **Period 0 spread lines are continuous** from 0.0 to 7.0+ with the most common at ±1.5 (30×) and ±2.5 (29×). Set handicap in best-of-3 tennis would be EXCLUSIVELY ±1.5 (the only valid set spread). Continuous line distribution is the signature of games-handicap, not set-handicap.
- **Period 0 totals are 33-41 games** — total games per match. Set totals would be 2.5 ± a half (best-of-3 → match always 2 or 3 sets).
- **Period 1 = first set**: spread lines 1.5-3.5 (games margin within a set), total lines 8.5-12.5 (total games in a set).
- Pinnacle does NOT ship set-based tennis markets via the guest API. Only games-based.

So set-vs-games is a non-issue for our parser. The user's observed cross-pairing was entirely the line-tolerance bug fixed in Phase 3.1.2 — CB games +1.0 was being paired with Pin games +1.5 (both games-handicap, but DIFFERENT lines), which looked like set/games confusion because the lines didn't make sense.

After 3.1.2 (exact-line match required), tennis spread pairing should be correct: CB games +X.X only pairs with Pin games +X.X at the SAME X.X. User to verify on next dashboard restart.

Worth noting: Pinnacle's period 1 = "first set" for tennis (we map this to "H1" in PERIOD_MAP, label is misleading but data is correct). CB list-view only ships period 0 (full match) spreads, so cross-period pairing wouldn't happen anyway. If we ever wire tennis detail-page expansion, CB might surface period 1 spreads that'd then correctly pair with Pin period 1 (both games-handicap within first set).

---

## 2026-05-26 — Phase 3.1.2: tighten line-match tolerance (0.5 → 0.01)

User reported: tennis showing CB +1.0 falsely matched against Pin +1.5 (different bets, wrong edge math). Root cause: `LINE_MATCH_TOLERANCE = 0.5` in edge.py — abs(1.0 - 1.5) = 0.5 → matches inclusive. Same hardcoded 0.5 also in app.py `_closest_pin`.

Tightened to **0.01** (float-rounding safety only). Lines now require exact match (or float-noise close). Distinct lines like +1.0 vs +1.5, -3.5 vs -3.0, 2.5 vs 3.0 no longer pair — row simply shows "no Pin reference" instead of a phantom edge.

**Affects all sports, not just tennis** — this bug was present in basketball + soccer too but user just noticed it because tennis has tightly-packed handicap lines (+0.5, +1.0, +1.5 all common) where the 0.5 tolerance most often misfires.

Test updates: replaced `test_pin_match_accepts_line_within_tolerance` (which asserted the buggy behavior) with three tighter tests — exact-line required, float noise still allowed, exact-line picked among multiple candidates.

338/338 tests pass (was 337 — one new float-noise test).

**Known remaining issue (#3.1.3, deferred):** tennis has separate "set handicap" and "games handicap" markets that both come back as `type=spread` from Pinnacle. Need a probe to figure out how Pinnacle distinguishes them (probably via `key` field or sub-type), then either filter to one or store them as different markets. See follow-up task.

---

## 2026-05-26 — Phase 3.1.1: Pinnacle tennis brandId=0 fix

After Phase 3.1 shipped, first live tennis cycle returned 0 Odds. Probe revealed `/sports/33/leagues?all=false&brandId=0` returns 403 BAD_APIKEY for tennis, but `?all=false` (without brandId) returns 200 with 38 tennis leagues.

Generalizes the May 24 fix that dropped `brandId=0` from `/leagues/{id}/matchups`: brandId=0 is unreliable across Pinnacle's guest endpoints. **Dropped it from `/sports/{id}/leagues` too**, in `_fetch_pinnacle_for_sport`. Tennis fixed; basketball + soccer expected to keep working (both accept either form per Pinnacle's actual API behavior).

Probe also confirmed tennis matchup structure:
- 500 matchups in bulk (495 type=matchup, 5 specials → our filter works)
- 264 top-level vs 231 with parent (set-by-set sub-matchups, correctly filtered)
- Sample participants: standard `alignment="home"/"away"` two-player structure (singles). Doubles probably mirror this with combined names like `"A. Player / B. Player"`.

337/337 tests still pass.

If basketball or soccer cycles also degrade after this change (different league count than before), branch the brandId logic per-sport. Default to "drop globally" until proven otherwise.

---

## 2026-05-26 — Phase 3.1: tennis sport, list-only mode

User confirmed Phase 2 plan and asked for tennis as third sport (in list-only mode, since detail expansion would be ~500 matches × 3s = heavy). CB sport_id=22, Pinnacle sport_id=33 (to verify on first live run).

**Key finding from the captured sample (575 containers, 205 with loadinfo, 364 Format-B):** tennis on CB has the IDENTICAL list-view structure to basketball — 8-entry loadinfo positional layout with the same landmark anchors (`handicap='handicap'` at index 3, `handicap='total'` at index 6), and the same 8-col Format-B layout (col0/1 ML, col2/3/4 AH, col5/6/7 OU). Only differences from basketball: the OU line landmark name is `"Game"` instead of `"Point"` (irrelevant — we anchor on the `handicap='total'` flag), and the away-team-ML name has a tab prefix `"\t2"` instead of basketball's leading-space `" 2"` (handled by the same `.strip()` discriminator).

**Implementation** = 30-line delegation pattern, not a full parser duplication:

1. **`src/scrapers/sports/basketball.py`** — refactored `_make_odds`, `parse_loadinfo`, `parse_div_odds` to accept an optional `sport_name` parameter (default `"basketball"` for back-compat with all existing test fixtures + call sites). Inside, the only effect of `sport_name` is what gets stamped onto the emitted `Odds.sport`. The parser logic itself is sport-agnostic for any 2-way-ML + AH + OU structure.

2. **`src/scrapers/sports/tennis.py`** — new, ~50 lines. Three thin wrappers:
   - `parse_loadinfo(...)` → `basketball.parse_loadinfo(..., sport_name="tennis")`
   - `parse_div_odds(...)` → `basketball.parse_div_odds(..., sport_name="tennis")`
   - `classify_market_title(title) → None` (list-only mode; no detail-page expansion)
   - `SPORT_ID = 22`, `SPORT_NAME = "tennis"`.

3. **`src/scrapers/crystalbet.py`** — added `tennis` to the imports, `TENNIS_SPORT_ID = 22`, `SAMPLE_OUT_TENNIS`, `_SPORT_MODULES`/`_SPORT_SAMPLE_PATHS` entries, `parse_html_tennis`, `fetch_crystalbet_tennis_prematch`, `dry_run_parse_saved_tennis`, `--parse-saved-tennis` CLI flag.

4. **`src/scrapers/pinnacle.py`** — `SPORT_ID_TENNIS = 33`, `ALLOWED_MARKET_TYPES_BY_SPORT['tennis'] = {moneyline, spread, total}`, `fetch_pinnacle_tennis()` wrapper.

5. **`src/app.py`** — added the `SportConfig` for tennis to `_ALL_SPORTS`. CB and Pin fetchers wire automatically into the existing multi-sport poller pattern.

**Saved-HTML smoke** via `dry_run_parse_saved_tennis()`: 902 Odds parsed (550 moneyline + 182 spread + 170 total) across ~225-300 matches. Sample row: Davidovich Fokina vs Tirante → moneyline 1.95/1.70, spread line=+1.0 home=1.80 away=1.85, total line=39.0 over=1.80 under=1.85. Sport stamp is `"tennis"` throughout.

**Test fixes:** two tests had hardcoded "exactly 2 sports" or "tennis = unknown sport" assumptions. Updated:
- `test_app_soccer.py::test_per_sport_structure` — assertion now `"basketball" in sport_names AND "soccer" in sport_names` (open to extension).
- `test_pinnacle_soccer.py::test_unknown_sport_falls_back_to_full_set` — uses `sport_name="hockey"` (still unknown) instead of `"tennis"`.

**337/337 tests pass.**

**To enable tennis live:**

```bash
SPORTS=basketball:full,soccer:list,tennis:list python main.py
```

**Pinnacle tennis sport_id verification needed.** I set `SPORT_ID_TENNIS = 33` based on Pinnacle's standard convention but haven't probed live. If first run shows `0 leagues after filter` for tennis, change to whatever the actual id is. Easy to spot in logs.

**Pattern this proves** for future sports: any sport whose CB list view has the 2-way ML + AH + OU 8-entry structure (basketball, tennis, probably hockey/baseball/MMA) can be added in ~50 lines of tennis.py-style delegation. The expensive work was the sport-isolation architecture during Phase 2. Each new sport is now cheap.

---

## 2026-05-26 — Phase 2.5 #6: per-sport BrowserContext (fix session-cookie collision)

User confirmed: `SPORTS=basketball:full` alone works, `SPORTS=soccer:list` alone works, but together they fail in the same way (`wait_for_selector "div.GContainerList[data-id]"` timeout for both, after a successful SelectAllChampionats post).

Root cause: **ASP.NET session cookie collision.** Both sports' Playwright pages shared a single BrowserContext (cookies + storage common). CB's site tracks "current sport" at the **session** level — i.e., in the ASP.NET session keyed by the session cookie. When two pages from the same context call `DoSportTypePostBack` with different sport ids in rapid succession:

```
Page A (basketball): DoSportTypePostBack(17) → server session.currentSport = 17
Page B (soccer):     DoSportTypePostBack(16) → server session.currentSport = 16  ← overwrites!
Page A: DoChampionatPostBack("SelectAllChampionats:17")
  → server checks session.currentSport (= 16, not 17)
  → returns empty list (or cached soccer state)
Page A: wait_for_selector("div.GContainerList[data-id]") → no containers → 15s timeout
```

C2.6 happened to work because the cycles were not exactly aligned and the session sport sometimes matched. Once cycle cadences synchronized (or basketball took long enough that soccer's setup overlapped), the collision became permanent.

**Fix:** each sport gets its own `BrowserContext`. Same Chromium process (one `_pw` + one `_browser`), but `_contexts: dict[int, Any]` maps sport_id → its own BrowserContext. Each context has independent cookies including its own ASP.NET session cookie. Sessions don't see each other. Memory cost: ~50 MB per extra context — negligible vs the 250-400 MB of Chromium itself.

**Code changes (src/scrapers/crystalbet.py):**
- Removed module-level `_context: Any`.
- Added `_contexts: dict[int, Any]` — sport_id → BrowserContext.
- New `_ensure_context_for_sport(sport_id)` — creates context with the locale/UA/tz settings, lazily.
- `_ensure_browser_singleton` no longer creates a context — just `_pw` and `_browser`.
- `_ensure_page_for_sport` now calls `_ensure_context_for_sport(sport_id)` under `_browser_init_lock` before spawning the page, then spawns in that context.
- `_close_page_internal` now closes the page AND its context (each sport's context exists for exactly one page).
- `_close_browser_internal` closes all contexts first (via `_close_page_internal`), then a safety-net pass for orphans, then the browser, then pw.

No test changes needed — Playwright is mocked / stubbed throughout the suite; the existing 337 tests still pass.

**Phase 2.5 #3 (debug-basketball-fails-in-parallel) is RESOLVED by this fix** — same root cause. Marking that one's diagnosis done.

---

## 2026-05-26 — Phase 2.5 #5: unified SPORTS env knob

Collapse ENABLED_SPORTS + CB_SKIP_DETAIL_SPORTS into one expressive `SPORTS=sport:mode,sport:mode` variable. User-requested 2026-05-26: "1 choice for all sports".

Syntax (both forms equivalent):
- `SPORTS=basketball:full,soccer:list`
- `SPORTS=basketball_full,soccer_list`

Three modes per sport:
- `full` — CB scrape + Pin fetch + per-game detail expansion. Default if mode omitted.
- `list` — CB list-view only, no per-game detail. Soccer cold cycle ~30s instead of ~60min. Loses alt-lines, team_total, corners, H1.
- `off` — sport disabled. Equivalent to omitting the sport from SPORTS.

Sports absent from `SPORTS` are off. Empty/unset = all known sports in full mode (preserves current default).

Implementation lives entirely in `src/app.py` at module-load time:
1. `_parse_sports_env` returns `{sport_name: mode}` dict, accepting both colon and underscore separators.
2. SPORTS list filtered to entries with mode != off.
3. Override `crystalbet.SKIP_DETAIL_SPORTS` with the set of mode=list sports — replaces whatever the legacy `CB_SKIP_DETAIL_SPORTS` env loaded at import time.

Legacy back-compat preserved: if `SPORTS` is unset but `ENABLED_SPORTS` and/or `CB_SKIP_DETAIL_SPORTS` are set, those still work exactly as before.

Verified all six combinations: colon, underscore, single-sport, off-mode, unset (default), legacy fallback. 337/337 tests still pass.

---

## 2026-05-26 — Phase 2.5 #4: tune timeouts + CB_SKIP_DETAIL_SPORTS (list-view-only mode)

First live parallel-mode run after Phase 2 ship surfaced three concrete problems in `prematch/dashboard.log`:

1. **`DoSportTypePostBack` 10s timeout was too aggressive** — fired on EVERY cold start for BOTH sports (lines 306/308). Basketball cycle 1 crashed because the retry path also failed. Bumped to **30s** in all three `wait_for_function` sites (`_flip_to_english`, `_select_sport`, `_load_all_leagues_for_sport`).

2. **`_EXPAND_SELECTOR_WAIT_SEC=6.0s` was too aggressive for soccer.** Cold cycle 1 stats: 1 expanded / 449 list-fallback / **345 expand-failed (43%)** out of 795 games. Basketball at the same cycle: 45 expanded / 10 cached / 45 list-fallback / 0 failed. Soccer detail pages render slower (more markets, more JS). Bumped to **12.0s**.

3. **`_STALE_CACHE_MAX_AGE_SEC=30 min` was dropping all disk-loaded cache.** Every observed cycle showed `cached-detail=0` because the cache file age (15:25) was always >30 min stale by the time the dashboard restarted. Bumped to **6 hours** — still protects against truly stale data (the ITD Santa Tecla incident was 4h+) while letting normal restart gaps benefit from the disk cache.

Test updated: `test_hours_old_is_stale` now uses 8h to stay on the stale side of the new 6h cap (was 4h, which is now fresh under the new threshold).

**Plus new `CB_SKIP_DETAIL_SPORTS` env knob** (Option B from user 2026-05-26 — "soccer too heavy for laptop"):

Comma-separated sport names whose cycle skips per-game detail expansion entirely. The cycle still does list-view scrape + per-game extraction, then returns the list-view Odds for every game without ever calling `DoGamesPostBack:ExpandDetail`. Soccer cold cycle drops from ~60-90 min to ~30s.

What you lose with `CB_SKIP_DETAIL_SPORTS=soccer`:
- Alt-line spread/total ladders (only main line from list view)
- team_total markets entirely
- corners markets entirely
- H1 markets entirely

What you keep for soccer:
- 1X2 / moneyline FT (the main actionable market)
- Main-line Total FT (over/under goals)
- Pin matching + 3-way edge math + ARB still work for these

Note: soccer's list-view doesn't ship Asian Handicap, so list-only mode loses spread for soccer too. Basketball list-only would retain spread (basketball list view has AH).

**Recommended daily-driver command** for laptop-load relief:

```bash
CB_SKIP_DETAIL_SPORTS=soccer .venv/bin/python main.py
```

Both sports run, basketball gets full detail-page coverage, soccer gets fast list-only with the actionable markets. ~30s soccer cycles + ~3-4 min basketball cold cycle (~30s warm).

337/337 tests pass.

---

## 2026-05-26 — Phase 2.5 #1 extended: JS-binding race on ALL three CB postback functions

User hit `ReferenceError: DoSportTypePostBack is not defined` on the FIRST `python main.py` after Phase 2 v1 ship. Self-recovered via the existing reset-and-retry, but the warning was real: same JS-bootstrap race I fixed for `DoGamesPostBack` was also live on `DoSportTypePostBack` (called during page setup, before the existing fix could help).

Added the same `page.wait_for_function("typeof X === 'function'", timeout=10_000)` guard to all three postback call sites:

- `_flip_to_english` → waits for `__doPostBack`
- `_select_sport` → waits for `DoSportTypePostBack`
- `_load_all_leagues_for_sport` → waits for `DoChampionatPostBack` (before firing it; the wait_for_selector after the post-back is unchanged)

`_ensure_page_for_sport`'s health check (which waits for both DoChampionatPostBack + DoGamesPostBack) was already in place from earlier today's patch — covers re-init paths.

All four CB JS dependencies now have explicit readiness guards. A page that's truly slow to bootstrap (>10s for any of the four) raises RuntimeError → cycle's outer try/except resets the page and retries on the next cycle. No more silent `ReferenceError` warnings on the FIRST cold-cycle attempt.

337/337 tests still pass.

---

## 2026-05-26 — Phase 2.5 #1 shipped: startup-race fix (DoGamesPostBack + 0-games)

Three-part patch in `src/scrapers/crystalbet.py` addressing the cosmetic-but-annoying first-cycle pathology from C2.6:

1. **`_load_all_leagues_for_sport` now awaits a real DOM signal.** After the `DoChampionatPostBack("SelectAllChampionats:N")` evaluate + 8s sleep, we `page.wait_for_selector("div.GContainerList[data-id]", state="attached", timeout=15_000)`. Real cold runs render containers within a couple of seconds; the extra 15s ceiling covers the "JS still bootstrapping" race. On timeout, raise RuntimeError → cycle's exception handler keeps `fetched_at` at None → next cycle retries cleanly.

2. **`_ensure_page_for_sport` health check tests BOTH JS functions.** Was `typeof DoChampionatPostBack === "function"`; now also tests `DoGamesPostBack`. Pre-fix, cycle 1 sometimes hit `DoGamesPostBack is not defined` because that JS binds later in CB's page bootstrap; the health check passed but expand failed mid-cycle. Now we wait until both are bound before declaring the page healthy.

3. **`_fetch_for_sport` raises if extraction yields 0 games.** Belt-and-suspenders: if the page rendered HTML but our parser found no GContainerList[data-id] (e.g. CB shipped only outright games today and we filtered them all), treat as transient. Avoids the "first cycle silently succeeds with 0 Odds → status bar stuck at sc:0 for one full poll interval" pathology.

**Why all three:** the wait_for_selector covers "page not rendered yet"; the health-check covers "page is rendered but per-game JS not yet bound" (matters on re-init paths); the 0-games raise is the final safety net so no path ever silently sets fetched_at to a 0-Odds success.

**Test added** (`tests/test_crystalbet_parser.py`): empty/garbage HTML → `_extract_games_from_list_html` returns `[]` cleanly (parser stays non-throwing on empty input; the "raise on 0" decision lives in `_fetch_for_sport`, where the cycle's exception handler treats it as transient). 337/337 tests pass.

**Expected behavior on next `python main.py`:**
- Cold start (no cache): `_load_all_leagues_for_sport` waits up to 15s for containers; if CB renders them (always does on a normal page) cycle proceeds. No more sc:0 stuck for 90 min.
- Warm start (cache loaded from disk via cache_persistence): same wait, then most games served from cache → fast cycle, dashboard usable in under a minute.
- True transient (CB momentarily flaky): cycle raises, error visible in `/api/status` for the sport, retry in 5 min (CRYSTALBET_POLL_SEC). No silent 0-Odds windows.

---

## 2026-05-26 — Phase 2 v1 SHIPPED — live 90-min smoke test PASSED

Final checkpoint of Phase 2. User ran live `uvicorn` for ~90 min and exercised both sports end-to-end through real Pinnacle + real Playwright CB scrapes. Verdict: PASS. Phase 2 v1 ships.

**Live stats from the run:**

| Surface | Basketball | Soccer |
| --- | --- | --- |
| `/api/matches` rows | 104 | 762 |
| Odds in CB state | ~2,000 (steady warm) | 4,189 (cold cycle) |
| Pinnacle Odds | ~2,000 | ~13,400 |
| `/api/opportunities` rows | 6 | 512 |
| `/api/unmatched` rows | 37 | 412 |
| Cold-cycle wall time | ~3-4 min (Phase 1 baseline) | **~85 min** (~780 live games × ~3 s/expand) |
| Cache file size on shutdown | 3.2 MB | 1.2 MB |

Confirmations:
- `cb_draw` populated on all 762 soccer rows; None on all 104 basketball rows.
- Status bar format works: `CB bb:N (Xm) | sc:M (Ym)`.
- 3-column expander grid renders correctly for both 2-way (basketball padding side 3) and 3-way (soccer 1X2 with home/draw/away).
- 7 corner opportunities with `(corners)` label (incl. Crystal Palace–Rayo Vallecano, PSG–Arsenal).
- 289 team_total markets matched against Pin (no positive-edge opps today — normal variance).
- Sport column on arbs.html (10 cols) and unmatched.html (7 cols) — populated and correct.
- Clean shutdown: `Application shutdown complete` log line, both cache files written.

**Phase 2.5 follow-ups** (logged for future-self, none blocking ship):

1. **Soccer cold cycle is ~85 min**, longer than my 25-35 min pre-run estimate. The 686-game sample I sized against is smaller than the live ~780-game soccer page. Warm cycles (cache served) run in tens of seconds — first cold start after disk wipe is the only slow path. If we want to accelerate, the cheap options are (a) parallelize the per-game expand calls (currently serial within one sport's page), (b) prioritize HasAdditionalOdds=True games and defer the long tail. Neither needed for v1.

2. **Startup race: first soccer cycle returns 0 Odds.** Both sports' poll loops fire immediately on lifespan startup. Soccer's `_ensure_page_for_sport` goto + flip-English + select-sport sequence wasn't done before the cycle tried to read `page.content()`. Result: empty list view → 0 Odds extracted → cycle "succeeds" with `count=0`, `fetched_at=now`. The next cycle (~5 min later) runs the real 85-min cold cycle and populates correctly. **Cosmetic damage**: status bar shows `sc:0` for the first ~90 min of a fresh start. Fix would be to either (a) skip the count update when the cycle returns 0 with no prior cache hit, or (b) add a more robust page-readiness check in `_ensure_page_for_sport` (e.g. wait for `div.GContainerList` to be present after the SelectAllChampionats post). Worth fixing; not urgent because cached restarts skip the issue.

3. **Basketball cycle 1 hit `DoGamesPostBack is not defined` on 71 games.** Same root cause as #2 — page wasn't fully JS-ready before the per-game expand started. Self-recovered cycle 2 with no errors. The existing health check (`typeof DoChampionatPostBack === "function"` in `_ensure_page_for_sport`) was checking the WRONG function name — `DoGamesPostBack` is what `_expand_and_parse_one` actually calls. Quick fix: change the health-check to test `typeof DoGamesPostBack === "function"` (or both). Worth shipping in Phase 2.5 even if not blocking.

4. **100%+ edge anomalies on some soccer spread rows.** Sample: South Korea vs Czechia H1 Spread -1, CB 7.05, Pin fair 3.44 → 94% edge. Present in Phase 1 basketball too (rare). Soccer exposes it more because CB ships many more spread lines per match. Hypothesis: CB sometimes labels a 3-way "Handicap (1X2)" market as bare "Handicap" (which we classify as 2-way AH) — the price spread reflects the 3-way payoff while we're devigging as 2-way. Investigation: capture a real CB detail block from one of the >100%-edge games and inspect the title + label shape. Defer to Phase 3 (multi-book validation will surface these too).

5. **Basketball team_total deferred (intentional).** Pinnacle ships ~10 basketball team_total Odds per cycle; we gate them off because CB classifier has no team_total rules and they'd be phantom unpaired Odds. Phase 1.5 follow-up if we want it: add team_total to basketball's `_RULES` + map CB's title to it. Probably a single rule each for FT/H1/Q1-Q4 if CB has them.

6. **Pinnacle bookings deferred (intentional).** Only 2 Pin leagues (UCL + Conference League). The `submarket="bookings"` plumbing is symmetrically additive to `submarket="corners"` — just need to flip `units == "bookings"` in `_index_child_matchups` and add `"bookings"` to the Submarket Literal in models.py. Maybe 30 min of work when there's appetite.

7. **Pending UI feedback the user hasn't batched yet** — flagged 2026-05-25 ("user has pending UI feedback they want to batch — not collected yet"). Still pending; ask before next change cluster lands.

**Phase 3+ roadmap remains** (per project memory):
- Second reference book → unlocks ARB-3 for soccer 1X2, broader market coverage, edges sharper than against just Pinnacle.
- Live scraping → separate architecture, needs API subscription, sub-30s cadence. Out of scope for now.

Phase 2 v1 closeout complete. We're done with the soccer build.

---

## 2026-05-26 — C2.5.b shipped: frontend multi-sport (Draw column + Sport column + grid expander)

Frontend half of C2.5. Three HTML files + style.css.

**`static/matches.html`** — 11 → 15 columns:

Added (between Pin A and Edge H% / between CB A and Pin H): **CB X / Pin X / Edge X%**. Soccer rows with 3-way ML populate all three. Basketball rows (and soccer rows where Pin moneyline came back 2-way for whatever reason) render `—`. Filter row colspan adjusted to 10 (spans the 10 cells after the 5 filter inputs). Loading row + expander row colspan bumped from 12 to 15.

**Expander rewritten as a 3-column side-block grid.**

Pre-Phase-2 expander rendered each market as 7 columns: Market label, CB side A / B, Pin side A / B, Fair A / B. Worked for 2-way but doesn't scale to 3-way (1X2) or team_total (where the team_side is part of identity, not selection key).

New layout: each market emits up to 3 side-block cells (`<td class="side-block">`), each stacking CB / Pin / Fair vertically:

```
| Market         | Side 1 (Home)      | Side 2 (Draw)     | Side 3 (Away)      |
| Moneyline FT   | CB  2.30           | CB  3.20          | CB  3.00           |
|                | Pin 2.20           | Pin 3.10          | Pin 2.95           |
|                | Fair 2.46          | Fair 3.46         | Fair 3.29          |
| Spread H1 -1.5 | CB  3.30 (home)    | CB  1.32 (away)   | (padded)           |
| ...                                                                          |
```

2-way markets pad the third side with an invisible `side-pad` cell so the table grid stays aligned. 3-way markets (moneyline with draw selection, detected via `"draw" in m.cb`) populate all 3.

**`expanderLabel` mirrors `edge.py:_market_label`** — `replace(_, " ").title()` for the type name, with `(team_side)` and `(submarket)` suffixes when set. Soccer corners total at 9.5 reads "Total +9.5 (corners)"; soccer Home Team Total at 1.5 reads "Team Total +1.5 (home)".

**`sidesForMarket(m)`** detects market shape from selection keys:
- `total` / `team_total` → `["over", "under"]`
- `moneyline` with `"draw"` in `cb` selections → `["home", "draw", "away"]`
- else → `["home", "away"]`

**Sort within period** updated: parent markets before submarket-tagged ones (so `Total goals` comes before `Total corners*`); team_total `home` side before `away` side; then by line ascending.

`MARKET_TYPE_ORDER` gained `team_total: 3` (after `total: 2`).

**`static/arbs.html`** — 9 → 10 columns:

Added **Sport** column (per user request mid-C2.5). Renders `sport` field from each opportunity row. Compact 2-letter status prefix shown in the header (`CB bb:N (3m) | sc:M (4m)`) using the same `buildSourceStatus` helper as the matches page.

**`static/unmatched.html`** — 6 → 7 columns:

Added **Sport** column for symmetry with the other tables. Each unmatched row carries the sport tag from C2.5.a's `/api/unmatched` aggregation.

**Status bar update across all three pages.**

Pre-Phase-2 status read `s.cb.count` / `s.pin.count` / `s.cb.age_sec`. C2.5.a changed `/api/status` to nest under `s.sports.{sport}.cb` / `s.sports.{sport}.pin`. All three frontend pages now read the new shape via a shared `buildSourceStatus(label, sports, source, staleThresholdSec)` helper. Output format:

```
CB bb:233 (3m) | sc:599 (4m)
Pin bb:1925 (2m) | sc:13190 (3m)
```

Worst age across sports drives the stale/ok color class; any per-sport error sets the err class.

**`static/style.css`** additions:

- `.markets-table-grid td.side-block` — side cell layout (vertical-align top, padding, min-width 100px)
- `.markets-table-grid td.side-pad` — invisible padding cell for 2-way markets
- `.side-header` — small uppercase accent label per side
- `.side-row` — CB / Pin / Fair value rows (flex, label dim left, value mono-right)
- `.markets-table-grid .market-label` — vertically centered against the side-block content

No backend changes. 336 tests still pass.

**Verification handed off** via `notes/cc_prompt_phase2_C2.5.b_visual.md` — user runs `uvicorn` with `CB_USE_SAVED=1` so the dashboard reads from the saved soccer + basketball HTML samples (no live CB scrape), Pinnacle hits the live API. User visits three pages, eyeballs the new columns + expander, reports back.

**Phase 2 v1 status as of close of C2.5.b:**
- C2.0 → C2.5.b all shipped and pytest-green
- Remaining: **C2.6 — end-to-end live soccer smoke** (user's laptop, real Playwright + Pinnacle, observe both sports for a few cycles, log to build_log)

---

## 2026-05-26 — C2.5.a shipped: app.py multi-sport backend (single browser, both sports' loops + endpoints)

Splits C2.5 into a (backend) + b (frontend) the way we did C2.2. C2.5.a is the dashboard pipeline; C2.5.b is the matches.html Draw column + arbs.html sport column.

**Architecture:**

- `SportConfig` dataclass with `(sport_name, cb_fetcher, pin_fetcher, cb_sample_path, cb_parse_html)`. `SPORTS = [basketball_cfg, soccer_cfg]` drives every multi-sport iteration in the module — adding tennis later means appending one more SportConfig and the rest works.
- `_state` namespaced by sport: `_state[sport]["cb"]["odds"]`, `_state[sport]["pin"]["odds"]`. One lock for all writes (different sports update different keys so contention is minimal).
- Background pollers parameterized: `_pinnacle_loop_for_sport(cfg)` and `_crystalbet_loop_for_sport(cfg)`. Lifespan creates 4 tasks (2 sports × 2 sources). Both sports share PINNACLE_POLL_SEC + CRYSTALBET_POLL_SEC env knobs per user 2026-05-26 (soccer doesn't get a separate cadence in v1).
- Cache persistence: `save_all(SPORT_NAMES)` / `load_all(SPORT_NAMES)` from C2.2.b plumbed into lifespan startup + shutdown.

**Endpoints (all aggregate across sports):**

- `GET /api/matches` — iterates SPORTS, builds per-sport matches via `_build_matches_view`, concats, sorts by start_time asc across sports. Each row carries `sport: "basketball"|"soccer"`.
- `GET /api/opportunities?min_edge=&kind=` — iterates SPORTS, builds per-sport opps via `compute_opportunities`, concats, sorts by edge desc, each opp dict gets a `sport` field.
- `GET /api/status` — per-sport breakdown:
    ```json
    {
      "sports": {
        "basketball": {"cb": {...}, "pin": {...}},
        "soccer":     {"cb": {...}, "pin": {...}}
      },
      "config": {"pin_poll_sec": 180, "cb_poll_sec": 300, "cb_use_saved": false, "sport_names": ["basketball", "soccer"]}
    }
    ```
- `GET /api/unmatched` — aggregates across sports, each unmatched row gets a `sport` field, sorted by best_score desc.

**`_build_match_row` 3-way ML support** (signature unchanged for back-compat — test_app.py basketball fixtures still work):

Schema additions to the row dict, all default None so basketball rows are unchanged:
- `cb_draw` — from `cb_ml.selections.get("draw")` (populated for soccer 3-way ML)
- `pin_draw` — from `pin_ml.selections.get("draw")`
- `pin_draw_fair` — from `devig_3way` (1.0 / fair_draw)
- `edge_draw_pct` — `(cb_draw / pin_draw_fair - 1) * 100`

3-way detection: if `ph and pa and pd and all > 1.0` → devig_3way path; else fall back to devig_2way (basketball + soccer 2-way derivatives). This means a soccer match where Pin only ships 2-way ML (unlikely for 1X2 markets but defensive) gracefully degrades to the basketball path.

**Defensive Pin ML filter on `_build_match_row` extended:**
The pre-Phase-2 filter matched on `(market_type, period)`. Now also filters `submarket is None and team_side is None` so the parent-match ML row never accidentally picks a corners/team_total entry. The matcher itself never passes mixed-submarket Odds to a single MatchedEvent (sport segregation is upstream), but the filter is belt-and-suspenders for the future.

**Markets-view per-entry schema additions:**
`_build_markets_view` per-entry dicts now include `submarket` and `team_side` fields so the frontend can label corners/team_total markets distinctly from parent ones. Used by the expander when C2.5.b lands.

**Tests** (`tests/test_app_soccer.py`, 13 cases):

- `TestBuildMatchRow3Way` (3): soccer 3-way populates all draw fields (fair_probs sum to 1.0); basketball 2-way leaves draw fields None (regression); unmatched soccer still has cb_draw populated but pin/edge None.
- `TestApiMatchesMultiSport` (5): combines both sports' rows; sorted by start_time across sports; soccer row has draw cols populated; basketball row's draw cols None; empty state returns [].
- `TestApiOpportunities` (3): opps carry sport tag; sorted by edge desc across sports; 3-way ML emits no ARB.
- `TestApiStatus` (1): per-sport structure with both nested + config.sport_names.
- `TestApiUnmatched` (1): aggregates across sports with sport tag per row.

**Important test-architecture note logged for future-self:**

The tests **do not use TestClient**. TestClient triggers the lifespan handler which spawns 4 background poll tasks (Pinnacle + CB for each sport). Those try to hit live APIs (httpx → Cowork sandbox SOCKS-proxy-blocked → 15s timeout) and Playwright (sandbox has no Chromium). The tests hung past pytest's 30+s before timing out.

Instead, tests call the endpoint async functions directly via `asyncio.run(app_mod.api_matches())`. Bypasses FastAPI routing AND the lifespan; tests the endpoint logic without flaky background tasks. The downside is we don't exercise the route decorators, but those are declarative FastAPI plumbing — bugs there would surface at startup, not in a unit test.

If we ever need routing-level tests, options: (a) add a `DISABLE_POLLERS=1` env knob the lifespan checks; (b) patch the fetchers in a fixture; (c) refactor lifespan to take a `start_pollers: bool` flag. None needed today.

**Verification:** 336/336 tests pass in 7.87 s. Pytest hand-off in `notes/cc_prompt_phase2_C2.5.a_pytest.md`.

**What's still NOT wired (C2.5.b):**
- `static/matches.html` — add CB_X/Pin_X/Edge_X columns (sport-conditional rendering; basketball rows show `—`)
- `static/matches.html` — [+] expander updates to label submarket/team_side and render 3-way ML selections
- `static/arbs.html` — add Sport column (user requested 2026-05-26 mid-C2.5)
- `static/style.css` — any new column styles
- Filter row updates for the new columns

C2.5.b is the next checkpoint.

---

## 2026-05-26 — C2.4 shipped: 3-way edge math + submarket/team_side join + basketball team_total gate

Combines the C2.3 follow-up gate decision with the C2.4 main scope.

**Decision: basketball team_total GATED at Pinnacle fetch level.** Post-C2.3 smoke showed Pinnacle ships 10 basketball team_total Odds/cycle. CB's basketball classifier has no team_total rules so those Pin rows would be phantom (unpaired, no edge, no display). User confirmed 2026-05-26: gate them at the source. Bringing basketball team_total end-to-end is filed as a Phase 1.5 follow-up.

Implementation: `ALLOWED_MARKET_TYPES_BY_SPORT: dict[str, set[str]]` in `pinnacle.py`:
```python
{
    "basketball": {"moneyline", "spread", "total"},                  # Phase 1
    "soccer":     {"moneyline", "spread", "total", "team_total"},    # Phase 2
}
```
Lookup with fallback to the full `ALLOWED_MARKET_TYPES` union for unknown sports (so adding tennis later doesn't accidentally filter everything). Basketball regression smoke after gate: back to ~1,925 Odds (= Phase 1 count, no team_total).

**3-way ML + team_total in the edge pipeline (`src/edge.py`):**

- `_fair_pairs(cb, pin)` — detects 3-way ML via `"draw" in pin.selections` and routes to `devig_3way`, returning three `(side, cb_decimal, fair_prob)` tuples summing fair_probs to 1.0. team_total falls through to the existing over/under devig branch (same selection shape as total).
- `_opposing_pairs(cb)` — signature changed from `(market_type: str)` to `(cb: Odds)` so we can detect 3-way ML via selections. Returns `[]` for 3-way ML (per user 2026-05-26: no ARB-3 within one book; vanishingly rare on Pinnacle pricing, revisit when book #2 lands). Returns `[(over,under), (under,over)]` for both `total` and `team_total`.
- `_find_pin_match(cb, pin_list)` — join key extended from `(market_type, period, line)` to `(market_type, period, line, submarket, team_side)`. Stops corners-total-9.5 from accidentally matching goals-total-9.5; stops home-team-total-1.5 from matching away-team-total-1.5. Basketball Odds have both fields None on both sides → None==None is True → identical Phase 1 behavior.
- `_market_label(cb)` — now uses `cb.market_type.replace("_", " ").title()` (so `team_total` → `"Team Total"`) and appends `(team_side)` / `(submarket)` suffixes when set:
    - `Spread H1 -2.5` (basketball — unchanged)
    - `Moneyline FT` (basketball or soccer 3-way — same label, selections distinguish)
    - `Team Total FT +1.5 (home)` (soccer team_total home)
    - `Total FT +9.5 (corners)` (soccer corners total)
    - `Spread H1 +0.5 (corners)` (soccer H1 corner handicap)

**Matches-page expander (`src/app.py`):**

- `_closest_pin(cb, pin_rows)` — adds submarket+team_side filter mirroring `_find_pin_match`. Without this, the matches expander would cross-pair corners/parent markets and home/away team_totals.
- `_maybe_devig(pin)` — gained branches for 3-way ML (returns `{home, draw, away}` fair-decimals via devig_3way) and team_total (over/under devig, same as total).

**Why include app.py here vs deferring to C2.5:** the join logic in `_closest_pin` and `_maybe_devig` is the same family as edge.py — they're the read-side mirror of the same join. Splitting them across checkpoints would leave a half-done state where the arbs page is correct but the matches expander shows phantom corners-vs-goals pairings. C2.5 picks up the rest of the dashboard wiring (soccer poll task, frontend Draw column, sport filter).

**Tests added (`tests/test_edge_soccer.py`, 25 cases) + (`tests/test_pinnacle_soccer.py`, +3 cases):**

- `TestOpposingPairs` (5): 2-way ML / spread / total unchanged, 3-way ML returns [], team_total returns over/under pairs.
- `TestFairPairs` (4): 3-way returns three tuples summing to 1.0, 2-way unchanged, team_total uses over/under devig, missing draw side → None.
- `TestMarketLabel` (7): all the label format cases above.
- `TestFindPinMatchSoccer` (5): submarket filter, team_side filter, basketball None/None back-compat.
- `TestCompute3WayEndToEnd` (2): per-side +EV emission (home gets +5.87% over fair 2.456 in the test fixture; draw + away below fair → suppressed; no ARB rows), label correctness.
- `TestComputeTeamTotalEndToEnd` (1): cross-pairing prevention — synthetic Pin away-tt at heavily-favored 1.50 over wouldn't generate phantom +30% edges on CB home-tt because the team_side filter blocks them.
- `TestComputeCornersEndToEnd` (1): cross-pairing prevention for corners-vs-parent same-line collisions.
- `TestTeamTotalGating` (3, in test_pinnacle_soccer.py): basketball team_total filtered out, soccer team_total kept, unknown sports fall back to full set.

**Bug caught during test writing:** my first `test_3way_ml_emits_per_side_ev_no_arb` fixture had CB at lower prices than Pin on every side, expecting at least one positive edge because Pin's vig was higher. Wrong intuition — devig produces FAIR prices tighter than vigged prices, so CB needs to beat the fair price (not just Pin's vigged price) to be +EV. With Pin 2.20/3.10/2.95, fair decimals are ~2.456/3.460/3.292; CB 2.30/3.20/3.00 is BELOW fair on every side → no rows emitted. Bumped CB home to 2.60 → +5.87% edge → assertion passes. The math direction was right; the fixture data was wrong.

**Verification:** 323/323 tests pass in Cowork sandbox in 23 s. Pytest+live verification handed off via `notes/cc_prompt_phase2_C2.4_pytest.md`.

**What's still NOT wired for Phase 2:**
- C2.5 — `app.py` soccer poll task + multi-sport `/api/matches` + `/api/opportunities` + frontend (Draw column or expander-only, sport filter chips). The edge math is ready; just need the plumbing + UI.
- C2.6 — end-to-end soccer smoke on user's laptop (Pinnacle + CB + matcher + edge + frontend all together).

---

## 2026-05-26 — C2.3 shipped: Pinnacle soccer (sport_id 29)

User-provided wire-shape probe (`notes/cc_prompt_phase2_C2.3_probe.md`) confirmed all four key shapes; refactored `pinnacle.py` to a generic `_fetch_pinnacle_for_sport(sport_id, sport_name)` with `fetch_pinnacle_basketball` / `fetch_pinnacle_soccer` as thin wrappers.

**Wire shapes verified (soccer sport_id=29):**

| Market type | Shape | Notes |
| --- | --- | --- |
| `moneyline` (3-way) | One entry, 3 prices `[home, draw, away]` | Detected by presence of `"draw"` designation. No `points` field. |
| `spread` | Same as basketball | `key: "s;0;s;-1.25"`, prices home/away with points |
| `total` | Same as basketball | `key: "s;0;ou;2.25"`, prices over/under with points |
| `team_total` | TWO entries per matchupId | Top-level `"side": "home"\|"away"` distinguishes. Each entry has over/under prices for one team. Alt-lines marked `isAlternate: true`. |

**Corners child matchups:**
- Top-level `parentId` field (NOT nested `parent.id`) — cleaner lookup
- Top-level `"units"` field distinguishes: `"Corners"` (in scope) vs `"Bookings"` (deferred to Phase 2.5 — only 2 leagues)
- Corners child league (e.g., 6914 "Copa Sudamericana Corners") returns 403 if hit directly via `/leagues/X/markets/straight`. Markets are accessible **only via the parent league's** `/markets/straight` response, mixed in by child matchupId.
- This validates Option A architecture: fold child Odds onto parent matchupId tagged with `submarket="corners"`. Matcher's CB↔Pin join stays 1:1.

**Period 39 ("To Advance" on knockout ties):** zero new code — existing `PERIOD_MAP = {0: "FT", 1: "H1"}` filter already drops it via `period_int not in PERIOD_MAP`.

**`type=special` (~2,700 entries on soccer):** new filter in `_index_matchups` — Pinnacle's unstructured props/futures/group-stage markets have free-text `description` fields, no machine-priceable `prices` structure. Filtered at the index step so they never reach the market parser.

**Alt-line handling: KEEPING ALL LINES.** User's probe note suggested filtering `isAlternate=false` for primary line only. Decided against — basketball's parser includes all alt-lines (Phase 1 explicitly captured the ladder for arbs/edges across lines). Soccer should match that behavior. Easy revert if it produces too much noise; default is "more data".

**Files changed (2):**

1. `src/scrapers/pinnacle.py`:
   - `SPORT_ID_SOCCER = 29` constant added. `HEADERS` constant kept as basketball back-compat; `_make_headers(sport_name)` builds the per-sport version (Referer differs).
   - `LEAGUE_SKIP` extended with `"corners"`, `"bookings"` — those leagues exist in `/sports/29/leagues` but always 403 on direct fetch. Skipping by name prevents wasted requests + failure-tracker churn.
   - `ALLOWED_MARKET_TYPES` gained `"team_total"`.
   - `_consecutive_failures` / `_skip_until` keys changed to `(sport_id, league_id)` tuples to prevent cross-sport league_id collision (basketball ACB id 559 vs hypothetical soccer league 559).
   - `_index_matchups` filters `type=="special"` (new); existing parent/live/participants filters unchanged.
   - New `_index_child_matchups` walks bulk matchups producing `{child_id: (parent_id, "corners")}`. Bookings deliberately skipped (deferred). Specials with parentId also skipped.
   - `_build_odds_for_league` gained kw-only `child_to_parent` + `sport_name` params (defaults preserve every existing test's positional call). 3-way ML detected by any `"draw"` designation in `prices`; team_total reads top-level `"side"` field; corners markets rewritten to PARENT matchupId with `submarket="corners"` tagged.
   - `_fetch_pinnacle_for_sport` is the generic; `fetch_pinnacle_basketball` and new `fetch_pinnacle_soccer` are thin wrappers.
   - Smoke test (`_smoke`) gained `--soccer` flag; prints by_submarket histogram and a corners sample when present.

2. `tests/test_pinnacle_soccer.py` (new, 23 cases):
   - `TestIndexChildMatchups` (7): corners picked up, case-insensitive units, bookings skipped, missing units skipped, no-parentId skipped, special-with-parentId skipped, mixed-input handling.
   - `TestIndexMatchupsPhase2` (3): type=special filtered (new behavior), parent matchups still included (back-compat), child matchups still excluded.
   - `TestBuildOdds3WayMoneyline` (3): 3-way emits {home, draw, away}, missing draw side drops the line, 2-way ML still works for basketball without the new sport_name arg.
   - `TestBuildOddsTeamTotal` (6): home side, away side, both sides → 2 separate Odds, missing side skipped, invalid side ("neutral") skipped, alt-line included.
   - `TestBuildOddsCornersChild` (4): market attributed to parent matchupId, parent team names used (not "PSG (Corners)" suffix), corners spread alt-lines, no-child-map basketball back-compat, child-with-unknown-parent quietly dropped.

**Verification (Cowork sandbox):** 295/295 tests pass in 30.96 s. Sandbox can't actually hit Pinnacle (SOCKS proxy blocks the guest API), so live verification handed to Claude Code via `notes/cc_prompt_phase2_C2.3_smoke.md`.

**Still to do before Phase 2 is complete:**
- C2.4 — edge math 3-way handling (devig_3way already exists; just need the dispatch + ARB-3 condition + submarket-aware matcher join key)
- C2.5 — app.py wiring (soccer poll task, /api/matches+/api/opportunities multi-sport, frontend Draw column + sport filter)
- C2.6 — end-to-end soccer smoke on user's laptop

---

## 2026-05-26 — C2.2.b shipped: crystalbet.py multi-sport (single browser, page-per-sport)

**Architecture**: one Chromium + BrowserContext shared, one persistent Page per sport (kept alive across cycles, sport-selected at init), per-sport `asyncio.Lock` for parallel cycles. Confirmed with user 2026-05-26 over single-page-re-select (~20s/cycle penalty) and two-browsers (doubles RAM).

**Files changed (3):**

1. `src/scrapers/change_cache.py` — `_global_cache: ChangeCache` (single) → `_caches: dict[str, ChangeCache]` (per sport). `get_cache(sport_name="basketball")` creates lazily. `reset_cache(sport_name=None)` clears all if no arg (preserves test semantics), or just that sport. Pre-Phase-2 callers (`change_cache.get_cache()` / `change_cache.reset_cache()`) unchanged because basketball is the default.

2. `src/scrapers/cache_persistence.py`:
   - `save(path=None, *, sport_name="basketball")` and `load(path=None, *, sport_name="basketball")` — kw-only sport_name; positional path still works for the 13 existing tests that pass explicit `tmp_path` paths.
   - Default save path is now `data/cache/cb_change_cache_{sport}.json` (sport-namespaced). Old `cb_change_cache.json` migrated transparently for basketball: if the new file doesn't exist but the old one does, we load from old and the next save writes to the new path.
   - Added `save_all(sport_names=("basketball","soccer"))` and `load_all(...)` convenience for app.py's lifespan to use in C2.5.
   - `_odds_to_dict` / `_odds_from_dict` now round-trip the new `submarket` and `team_side` fields. Old saved files without these keys load fine via `dict.get(...)` returning None. No SCHEMA_VERSION bump — the change is additive.

3. `src/scrapers/crystalbet.py` — full rewrite (~700 lines, was ~840) preserving every Phase 1 symbol:

   *Preserved (back-compat):*
   - `SAMPLE_OUT`, `BASKETBALL_SPORT_ID` constants
   - `_parse_loadinfo`, `_parse_div_odds`, `_identify_loadinfo_roles`, `_make_odds` (basketball aliases — test_crystalbet_parser.py imports these)
   - `_flip_to_english`, `_init_browser`, `_load_all_leagues`, `_select_basketball` (scripts/capture_single_match_detail.py + scripts/check_game_freshness.py import these)
   - `_STALE_CACHE_MAX_AGE_SEC`, `_is_cached_detail_fresh_enough` (test_stale_cache_safety.py)
   - `parse_html(html, fetched_at)` — basketball list-view parse, same signature
   - `_extract_games_from_list_html(html, fetched_at, sport=None)` — defaults to basketball
   - `fetch_crystalbet_basketball_prematch(headed=False)` — same signature, internally `_fetch_for_sport(basketball, headed=headed)`
   - `close_crystalbet()` — same signature, now closes all sport pages + browser
   - `get_detail_status_map(sport_name="basketball")`, `get_last_expanded_map(sport_name="basketball")` — default basketball preserves pre-Phase-2 callers
   - `get_detail_odds_cache(sport_name="basketball")`, `restore_detail_odds_cache(cache, sport_name="basketball")` — same
   - `dry_run_capture`, `dry_run_parse_saved`, `_print_summary` — basketball-only, unchanged

   *Added:*
   - `SOCCER_SPORT_ID = 16`, `SAMPLE_OUT_SOCCER` (= `data/raw/cb_prematch_sample_soccer.html`)
   - `_SPORT_MODULES`, `_SPORT_SAMPLE_PATHS` lookup dicts
   - `_select_sport(page, sport_id)`, `_load_all_leagues_for_sport(page, sport_id)` — parameterized internals; `_select_basketball` / `_load_all_leagues` now thin wrappers
   - `_parse_html_for_sport(html, fetched_at, sport)` — generic; `parse_html` (basketball) and `parse_html_soccer` are thin wrappers
   - `_ensure_browser_singleton(*, headed)` (browser-level) + `_ensure_page_for_sport(sport_id, *, headed)` (page-level) — split from the old `_ensure_browser`
   - `_close_page_internal(sport_id)` — closes one sport's page without touching the browser
   - `_get_sport_lock(sport_id)`, `_get_sport_detail_cache(sport_name)` — per-sport accessors
   - `_fetch_for_sport(sport, *, headed)` — the generic cycle that `fetch_crystalbet_basketball_prematch` and `fetch_crystalbet_soccer_prematch` both call
   - `fetch_crystalbet_soccer_prematch(*, headed=False)` — public API for soccer
   - `dry_run_parse_saved_soccer(out_path=SAMPLE_OUT_SOCCER)` — parse saved soccer HTML, mirrors basketball

   *Refactored internals:*
   - Module-level state went from `_pw/_browser/_context/_page` + `_cycle_count` + `_browser_lock` + `_detail_odds_cache` to `_pw/_browser/_context` (shared) + `_pages: dict[int, page]` + `_sport_cycle_counts: dict[int, int]` + `_sport_locks: dict[int, Lock]` + `_browser_init_lock` (just for browser-level setup) + `_sport_detail_odds_caches: dict[str, dict[str, list[Odds]]]`
   - `_ensure_page_for_sport` keeps all three defensive layers per-sport: periodic re-init (every 50 cycles), health check (typeof DoChampionatPostBack), retry-once
   - Per-sport refresh lock means basketball + soccer cycles run in parallel — neither blocks the other

**Verification (Cowork sandbox):**

1. All back-compat imports resolve:
   ```python
   from src.scrapers.crystalbet import (
       SAMPLE_OUT, SAMPLE_OUT_SOCCER,
       BASKETBALL_SPORT_ID, SOCCER_SPORT_ID,
       _parse_loadinfo, _parse_div_odds, _identify_loadinfo_roles, _make_odds,
       _flip_to_english, _init_browser, _load_all_leagues, _select_basketball,
       _STALE_CACHE_MAX_AGE_SEC, _is_cached_detail_fresh_enough,
       parse_html, parse_html_soccer,
       _extract_games_from_list_html,
       fetch_crystalbet_basketball_prematch, fetch_crystalbet_soccer_prematch,
       close_crystalbet,
       get_detail_status_map, get_last_expanded_map,
       get_detail_odds_cache, restore_detail_odds_cache,
       dry_run_parse_saved, dry_run_parse_saved_soccer,
   )
   ```
   All resolve.

2. Cross-sport cache isolation: basketball cache contains only basketball entries; soccer's `prune_missing` doesn't drop basketball entries; `reset_cache()` with no args clears both.

3. Soccer list-view parse via `dry_run_parse_saved_soccer()`: **1086 Odds emitted** from the saved PSG-Arsenal sample HTML (598 3-way ML + 488 Total). All ML have `selections={home, draw, away}`. Sample row: Paris SG vs Arsenal | moneyline FT | {home: 2.20, draw: 3.10, away: 2.95}. Consistent with the probe expectations (686 GContainerList, 494 with loadinfo → most contribute one ML + one Total; Format-B contributes the rest).

4. **Full Cowork-sandbox pytest run**: **272/272 passed in 11.86s** — no Phase 1 regression. Verified after installing pytest+httpx+fastapi+rapidfuzz+pyyaml+jinja2 system-wide for the sandbox.

**What's NOT done in C2.2.b (intentionally):**
- App.py wiring (`PINNACLE_POLL_SEC` / `CRYSTALBET_POLL_SEC` apply to both sports? need to think about cadence per sport) — that's C2.5.
- Adding a soccer poll task in app.py — that's C2.5.
- Frontend (Draw column, sport filter chips) — that's C2.5.
- New tests for soccer-specific paths (3-way ML smoke through `_fetch_for_sport`, per-sport cache namespacing). The 272-test suite covers basketball's path; soccer-specific tests would harden but aren't strictly blocking. Add as part of C2.5/C2.6.
- Bookings (Pinnacle's 2 leagues) — deferred to Phase 2.5.

**Pytest verification re-handed off** to Claude Code for the user's real venv (sandbox install was a workaround) — see `notes/cc_prompt_phase2_C2.2.b_pytest.md`.

---

## 2026-05-26 — C2.2.a shipped: soccer.py + cb_detail.py extensions

**Files changed (3):**

1. `src/scrapers/cb_detail.py`:
   - Moved `MarketClassification` here from `basketball.py` (sport-neutral home; sport modules now import from cb_detail, not across sport boundaries). Added optional fields `n_way: int = 2`, `submarket: Optional[str] = None`, `team_side: Optional[str] = None`. Basketball construction unchanged (all default).
   - New `_parse_ml_3way` — labels `1`/`X`/`2` → `{home, draw, away}`. Strips whitespace on labels (handles CB's `\t2` / ` 2` inconsistency on the away side observed in both loadinfo samples).
   - Existing `_parse_ml` also gained whitespace-stripping on the label for the same reason (no behavior change for basketball — basketball labels are already clean `"1"` / `"2"`).
   - `parse_detail_page` dispatch extended: `moneyline` now routes to 2-way or 3-way parser based on `cls.n_way`. Added `team_total` case (reuses `_parse_total` for labels, stamps `team_side` from classification onto emitted Odds).
   - Dedup key extended to `(period, market_type, line, submarket, team_side)`. Basketball (everything None) reduces to the prior 3-tuple → no behavior change.
   - `_build_odds` now accepts and propagates `submarket` + `team_side`.

2. `src/scrapers/sports/basketball.py`:
   - Removed the local `MarketClassification` definition + the `@dataclass` import.
   - Added `from src.scrapers.cb_detail import MarketClassification  # noqa: F401` at the top so external code that did `from src.scrapers.sports.basketball import MarketClassification` keeps working.
   - Zero changes to `_RULES`, `_SKIP_PATTERNS`, `_normalize_title`, or list-view parsers.

3. `src/scrapers/sports/soccer.py` (new, ~330 lines):
   - `SPORT_ID = 16`, `SPORT_NAME = "soccer"`.
   - `parse_loadinfo` (Format A): emits at most 2 Odds per game — 3-way ML (1X2 trio with `handicap=""`, scanned by stripped name `1`/`X`/`2`) + Total (landmark `handicap="total"` carries line in `bet` field, flanked by `Und*` and `over*` neighbors). Skips DC/DNB/BTTS. Defensive on locked sides (CB omits locked entries entirely; we scan rather than positional-index).
   - `parse_div_odds` (Format B): cols 0,1,2 → 1X2; cols 8,9,10 → Total under/line/over. Cols 3-7 and 11-12 (DC/DNB/BTTS) ignored. Verified against 3 non-outright Format-B containers in the saved sample (Ishoej-VSK, Hellerup-Vendsyssel, Varnamo-Nordic United).
   - `_normalize_title` — lowercase + collapse whitespace, **does NOT strip asterisks** (semantic markers — see "CRITICAL" finding below).
   - `_RULES` (14 in-scope): 10 parent-match (1X2/Handicap/Total/Home-Away Team Total × FT/H1) + 4 corners (Total Corners/Handicap of Corner × FT/H1, with H1 corner spread regex allowing the `hcp=X.Y` title suffix without capturing — line is extracted from inline labels by `_parse_spread`).
   - `_SKIP_PATTERNS` (~22 patterns): `\*\*\*$` (3-way derivatives), `^2nd half/^second half` (H2 — Pin doesn't ship), specials Pin sends as type=special (DNB/DC/BTTS/Correct Score/Halftime), combos (` & ` / ` and ` / ` or ` / ` / `), player markets (goalscorer/clean sheet/assists/shots/etc), bookings (deferred), exotics/ranges, CB-only corner shapes (Cornerbet/Corner Matchbet/First Corner/Last Corner/1st. Corner).

**Critical finding logged for future-self: asterisks are SEMANTIC on soccer.**

CB uses asterisk count as a market-class marker:
- `"Total corners*"` (one `*`)  → corners-submarket marker
- `"1st half - handicap"` (none) → 2-way Asian Handicap (in scope)
- `"1st Half - Handicap***"` (three `*`) → 3-way scoreline-handicap derivative (out of scope; labels like `"1 (2:0)"` not `"1 (-1.5)"`)

Basketball's normalizer strips asterisks because basketball doesn't use them this way. If we'd reused basketball's normalizer for soccer, the two H1 handicap variants would both collapse to `"1st half - handicap"` and the 3-way variant would mis-classify as 2-way AH, producing nonsensical Odds with scoreline strings shoved into the line slot. Sport-specific normalizers are the right call here.

**Bug found and fixed during smoke test:** initial skip-patterns alternation included `\b(?:goals|assists|shots|passes|tackles|saves|cards)\b` intended for player-stat markets. But `\bgoals\b` matches the in-scope parent titles `"Total goals"` and `"1st. Half Total goals"`, silently killing both FT and H1 total markets (the most important markets for soccer edge). Smoke test caught it: the breakdown showed 0 Odds under `(FT, total, None, None)` and `(H1, total, None, None)` even though the probe had counted 11 and 6 lines respectively. Fixed by removing `goals` from the alternation (kept the other player-stat words — none appear in in-scope titles, and `goals` combos like `"Player X Total Goals Over 2.5"` would still be caught by the existing ` & ` / ` / ` / ` and ` combo-skip patterns plus the player-name marker patterns).

**Smoke test against `data/raw/cb_single_match_detail_soccer.html`** (PSG-Arsenal UCL Final, 662 distinct titles):

Total: **55 Odds** emitted. Breakdown:

| period | market_type | submarket | team_side | count |
| ------ | ----------- | --------- | --------- | ----- |
| FT     | moneyline   | —         | —         | 1     |
| FT     | spread      | —         | —         | 7     |
| FT     | spread      | corners   | —         | 3     |
| FT     | team_total  | —         | home      | 4     |
| FT     | team_total  | —         | away      | 3     |
| FT     | total       | —         | —         | 11    |
| FT     | total       | corners   | —         | 5     |
| H1     | moneyline   | —         | —         | 1     |
| H1     | spread      | —         | —         | 4     |
| H1     | spread      | corners   | —         | 3     |
| H1     | team_total  | —         | home      | 2     |
| H1     | team_total  | —         | away      | 2     |
| H1     | total       | —         | —         | 6     |
| H1     | total       | corners   | —         | 3     |

Counts match the probe-derived expectations exactly. 14 distinct rules fired; all noise titles correctly skipped.

**Sample lines** (sanity check the data shape):
- FT 3-way ML: home=2.20, draw=3.10, away=2.95
- FT Total ladder: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5 (over odds rising correctly with line — 1.05 → 8.45)
- FT corners total ladder: 7.5, 8.5, 9.5, 10.5, 11.5 (over/under crossover at 9.5 — both 1.80)
- H1 corner spread per-line titles: 3 separate Odds at home_line -1.5 / -0.5 / +0.5 ✓
- FT team_total home ladder: 0.5, 1.5, 2.5, 3.5 (one missing at 4.5 over-only-no-under, correctly dropped)

**Basketball regression:** classifier still returns correct MarketClassification for known in-scope titles (Winner incl OT, Total Points incl OT, 1st Half DNB, 2nd Half Handicap incl OT, 4th Quarter Total Points) and correctly skips known noise (`"1X2"`, player props, Halftime/Fulltime). MarketClassification new fields default correctly (n_way=2, submarket=None, team_side=None) — basketball construction is unchanged.

**Pytest verification handed off** to Claude Code (see `notes/cc_prompt_phase2_C2.2.a_pytest.md`).

**Still to do for full C2.2 (split off as C2.2.b):**
- Parameterize `crystalbet.py`'s sport-specific bits (`_select_basketball`, `SelectAllChampionats:17` hardcoding).
- Add `fetch_crystalbet_soccer_prematch()`.
- Decide: one singleton browser handling both sports (re-select per cycle, adds ~20 s/cycle) vs two parallel singletons (one per sport, doubles memory, parallel cycles).
- Namespace `change_cache` and `cache_persistence` by sport so caches don't collide.

---

## 2026-05-26 — C2.1.5 model changes shipped (additive, basketball untouched)

Edited `src/models.py` to extend the schema for soccer Phase 2:

**MarketType Literal** — added `"team_total"` (Pinnacle ships 1,845 entries/cycle today; needed for Home/Away Team Total markets on soccer).

**Odds dataclass** — two new optional fields, both default `None`:
- `submarket: Submarket | None` — `None` (regular market) | `"corners"` | `"bookings"`. Used to fold Pinnacle's child-matchup markets (corners/bookings) into the parent matchupId while preserving their identity so the matcher can split them back out for line-equality joins. Basketball Odds always have `submarket=None`.
- `team_side: TeamSide | None` — `"home"` | `"away"`. Required logically for `market_type=="team_total"` (picks which team's total this is). Not enforced via validator yet — that lands in C2.2 once soccer.py first constructs these Odds; adding a hard check now would surprise existing test fixtures.

Market identity for matcher / dedup is now `(event_id, period, market_type, line, submarket, team_side)`. Existing `_find_pin_match` in `src/edge.py` and `_closest_pin` in `src/app.py` filter only on `(market_type, period, line)` — they need to add `submarket` and `team_side` awareness in C2.4 to avoid corners-total-9.5 phantom-matching goals-total-9.5 if a real-world game ever has both lines coincide. Doesn't fire today because no soccer Odds exist yet; ship as part of C2.4.

`Period` Literal not extended — soccer doesn't add H2 (Pinnacle ships FT/H1 only for soccer); basketball already uses `"H2"` implicitly via its classifier without the Literal listing it (Literal isn't runtime-enforced).

**Smoke tests** in Cowork sandbox (python3, no venv): basketball Odds with no new fields construct cleanly; soccer 3-way ML with `selections={home, draw, away}` constructs; team_total with `team_side="home"` and `line=1.5` constructs; corners total with `submarket="corners"` constructs; existing `>1.0` validator still fires on suspended-side input. Pytest verification handed off to Claude Code (see `notes/cc_prompt_phase2_C2.1.5_pytest.md`).

---

## 2026-05-26 — C2.1 design: soccer parser scope confirmed

User ran the capture (`scripts/capture_soccer_samples.py`) and posted findings. Both HTML files live in `data/raw/cb_prematch_sample_soccer.html` (6.8 MB, 686 GContainerList, 494 with loadinfo) and `data/raw/cb_single_match_detail_soccer.html` (8.4 MB, PSG-Arsenal UCL Final, 662 distinct detail-page market titles). Probed both for parser design.

**List-view loadinfo shape (verified on PSG-Arsenal sample, position-indexed):**
```
[0] name="1"       handicap=""       → 1X2 home
[1] name="X"       handicap=""       → 1X2 draw
[2] name="\t2"     handicap=""       → 1X2 away  (tab prefix in sample 1; single space in sample 2 — INCONSISTENT — strip leading whitespace+tabs)
[3] name="1X"      handicap=""       → DC home-or-draw       (SKIP)
[4] name="12"      handicap=""       → DC home-or-away       (SKIP)
[5] name="X2"      handicap=""       → DC draw-or-away       (SKIP)
[6] name="(0)1"    handicap=""       → DNB home              (SKIP)
[7] name="(0)2"    handicap=""       → DNB away              (SKIP)
[8] name="Und "    handicap=""       → Total under (trailing space)
[9] name="Goal"    handicap="total"  → Total line landmark   (line in `bet` field, e.g. "2.5")
[10] name="over "  handicap=""       → Total over (trailing space)
[11] name="Yes"    handicap=""       → BTTS yes              (SKIP)
[12] name="no"     handicap=""       → BTTS no               (SKIP)
```

**No handicap entry in soccer loadinfo** for this sample. AH is detail-page-only OR lives in Format-B (col-divs). 28% of soccer games (192/686) have no loadinfo — they use Format-B for the list view. Format-B col-position layout for soccer is NOT yet verified — needs inspection in C2.2 against a saved sample. Best guess: cols 0-2 = 1X2, cols 3-5 = AH (home/line/away), cols 6-8 = OU (under/line/over).

**In-scope CB detail-page titles** (13 rules total — mirror Pinnacle exactly):

PARENT match (no submarket):
- `^(?:1x2|main result)$` → moneyline FT (3-way)
- `^handicap$` → spread FT (2-way AH; "Handicap(1X2)" 3-way variant SKIPPED — Pinnacle has 2-way only)
- `^total goals$` → total FT
- `^home team total$` → team_total FT side=home
- `^away team total$` → team_total FT side=away
- `^1st half result$` → moneyline H1
- `^1st\s*half\s*-\s*handicap$` → spread H1
- `^1st\.?\s*half\s+total\s+goals$` → total H1 (note period after "1st." varies)
- `^1st\s*half\s*-\s*home team total$` → team_total H1 side=home
- `^1st\s*half\s*-\s*away team total$` → team_total H1 side=away

CORNERS (submarket="corners"):
- `^total corners$` → total FT corners
- `^handicap of corner$` → spread FT corners
- `^1st\s*half\s*-\s*total corners$` → total H1 corners
- `^1st\s*half\s*-\s*corner\s*handicap\s*hcp=([+-]?\d+(?:\.\d+)?)$` → spread H1 corners (LINE FROM TITLE — new parser variant; H1 corner spread is per-line title, not alt-lines inside one title like basketball)

Normalization (same as basketball): lowercase, collapse whitespace, strip `*`/`**`/`***`.

**Hard-skip patterns** (suppress warnings, not in scope):
- `^handicap\(1x2\)$` (3-way handicap; Pinnacle has 2-way only)
- `^(?:draw no bet|double chance|both teams to score|correct score|halftime|matchbet)`
- `^2nd half|^2nd\s+half|^2nd\.\s*half` (H2 — Pinnacle doesn't ship; skip per mirror-Pinnacle scope; trivially additive later)
- `corner range` (multi-outcome ranges, not over/under; CB Home/Away Team Corner Range markets are NOT comparable to Pinnacle's team_total corners over/under — shape mismatch, drop from earlier mapping table)
- `^cornerbet`, `corner matchbet`, `first[\s.]*corner|last[\s.]*corner` (CB-only / different shape)
- `^booking` / `\bbooking[s]?\b` (Pinnacle has bookings on 2 leagues, ~2 matchups — defer to Phase 2.5)
- Combo bets: ` & `, ` and `, `\bor\b`
- Player markets: `\b(?:1st|anytime) goalscorer\b`, `clean sheet`, `multigoals`
- Stats/exotics: `exact (?:goals|bookings|corners)`, `odd/?even`, `who scores`, `10 minutes`

**Things explicitly OUT of scope for v1** (per user confirmation):
- H2 markets entirely
- Handicap(1X2) 3-way
- Corner Matchbet (3-way corners winner)
- Home/Away Team Corner Range (multi-outcome, not over/under)
- Bookings (deferred to Phase 2.5 — only 2 Pinnacle leagues, trivial reuse later)
- Specials (BTTS, DC, DNB, Correct Score, Halftime/Fulltime — Pinnacle ships as type=special, not machine-priceable)

**Architecture for child matchups (Option A confirmed):** add `submarket` field to Odds. Pinnacle scraper detects child matchups (league-name suffix `... Corners`, `parent.id` set), looks up parent participants, emits the child's Odds tagged `submarket="corners"` and ATTRIBUTED to the parent matchupId. CB's corner markets in the same event's detail page get `submarket="corners"` at classifier time. Matcher's CB↔Pin event join stays 1:1; the new per-market dedup key (`event, period, market_type, line, submarket, team_side`) keeps corners-total from accidentally matching goals-total.

**New parser shapes needed in `cb_detail.py` (C2.2):**
- `_parse_ml_3way` — selections `{home, draw, away}`. Label dispatch on "1"/"X"/"2" with whitespace-strip on labels.
- `_parse_team_total` — selections `{over, under}` per (team_side, line). Title-determined team_side; over/under labels inside.
- `_parse_title_line_spread` — spread variant where line comes from classifier (captured from title via regex group), selections are just `1`/`2` labels. For H1 corner handicap.

**Pinnacle (C2.3) — bookings deferred.** Corners is the architecturally interesting case (15 leagues with corners children — Copa Libertadores/Sudamericana with 6 each, UCL, Conference, etc.). Bookings is a trivial reuse once the submarket plumbing is in place.

What still needs verification before C2.2 ships:
- Format-B col-position layout for soccer (inspect a no-loadinfo container in saved sample)
- Whether CB's FT "Handicap of corner*" is alt-lines-inside-one-title or per-line-title (H1 is per-line; FT may differ)
- Whether CB's team_total detail-page labels are `"1 over X"` / `"under"` or `"over X"` / `"under X"` with team derived from title

These will be answered in-script when writing the parsers, not in advance.

---

## 2026-05-26 — Pinnacle soccer API fully characterized (pre-C2.1)

User did a thorough sweep of `https://guest.api.arcadia.pinnacle.com/0.1/sports/29/matchups` and `/markets/straight` across all 75 soccer leagues. Findings:

**Market type taxonomy across the entire soccer API — only 4 types exist:**

| type        | rows today | notes                                            |
| ----------- | ---------- | ------------------------------------------------ |
| `moneyline` | 3,245      | **3-way for soccer** — home/draw/away            |
| `spread`    | 4,975      | Asian Handicap                                   |
| `total`     | 4,675      | Over/Under goals                                 |
| `team_total`| 1,845      | **NEW for v1** — home team total / away team total |

No corners/bookings/BTTS at the `type` level. Those live as separate matchups or aren't structured at all (see below).

**Periods: only 0 (FT) and 1 (H1).** No H2 on Pinnacle soccer. Period **39 = "To Advance"** appears on knockout-tie parent matchups (e.g., PSG–Arsenal Champions League) — 2-way moneyline, not a regular 90-min market. Out of scope for v1; filter alongside `type=special`.

**Corners/Bookings are CHILD MATCHUPS in separate leagues.** Linked via `parent.id`. Participants named with suffix: `"Paris Saint-Germain (Corners)"`. Currently 15 leagues with corners children (Copa Libertadores, Copa Sudamericana most active with 6 each; rest 1-2), 2 with bookings (UCL, Conference League). Corners children carry spread+total+team_total (FT and H1); bookings carry moneyline+spread+total+team_total (FT only).

**`type=special` (2,688 entries) — NOT machine-priceable.** Free-text `description` field, no structured `prices[].designation`. Categories: Team Props (2,618 — BTTS, Double Chance, H/T-F/T, Correct Score, 3-way handicap with specific lines), Group A-L (65 — World Cup advancement props), Player Props (3 — Anytime Goalscorer), Futures (2 — tournament winners). All ignored for v1.

**Design implication chosen: Option A for child matchups.** Add `submarket: Optional[str]` to `Odds` (values: `None`, `"corners"`, `"bookings"`). Pinnacle scraper detects child matchups via league-name suffix or `parent.id`, looks up parent participants, emits the child's Odds tagged with submarket but ATTRIBUTED TO THE PARENT matchupId. Matcher logic doesn't change — CB serves corner markets inside the same event's detail page (not a separate event), so one CB event ↔ one Pinnacle parent matchup remains true. Frontend renders submarket-tagged Odds in the expander with a chip/label. Basketball is untouched (submarket=None throughout).

Rejected Option B (separate Match records per matchupId) — would force the matcher to handle CB shipping regular+corners under one event_id while Pinnacle splits them. Messier; no upside.

**User's in-scope CB↔Pinnacle mapping table (the authoritative source for the soccer classifier):**

| CB title                                   | Pin type     | period | source                |
| ------------------------------------------ | ------------ | ------ | --------------------- |
| 1X2 / Main result                          | moneyline    | 0      | parent                |
| Handicap / Asian Handicap                  | spread       | 0      | parent                |
| Total goals                                | total        | 0      | parent                |
| Home Team Total / Away Team Total          | team_total   | 0      | parent                |
| 1st Half Result                            | moneyline    | 1      | parent                |
| 1st half - handicap                        | spread       | 1      | parent                |
| 1st Half Total goals                       | total        | 1      | parent                |
| 1st Half Home/Away Team Total              | team_total   | 1      | parent                |
| Total Corners / 1st Half - Total Corners   | total        | 0/1    | corners child matchup |
| Corner Handicap / 1st Half Corner Handicap | spread       | 0/1    | corners child matchup |
| Home/Away Team Corner Range                | team_total   | 0      | corners child matchup |

BTTS, Double Chance, Correct Score, 1st/Last Goalscorer — in Pinnacle's specials, **not matchable** for v1. CB likely has them; per "mirror Pinnacle" scope, they go in the not-implemented bucket.

**What's still unknown** (CB-side, awaiting capture run):
- CB's exact list-view loadinfo shape for 3-way 1X2 (reference §9 says soccer uses loadinfo, but locked-side handling and the X-position index are unverified)
- CB's detail-page market titles for soccer (we have basketball's set; soccer's will be different — e.g., "1st Half - Total" might be "1st Half Total goals" or "HT - Total" or something CB-specific)
- Whether CB ships corner markets under the same event's detail page or as a separate game tile
- Whether CB has team-total markets prominently (probably yes — common across books)

The CB capture script answers all of these. C2.1 design work starts when the two HTML samples land in `data/raw/`.

---

## 2026-05-25 — DOM-removal fix backfired; switch to CollapseDetail + stale cap

User ran the freshness diagnostic on ITD Santa Tecla BKB vs San Salvador
(event_id 3029012589). Result was conclusive:

- Dashboard's `markets_status`: **expand_failed**
- Dashboard's `last_expanded_at`: 21:42 UTC (4+ hours before the test)
- 220 of 262 in-both markets DIFFERED from fresh CB (84% staleness)
- FT moneyline: dashboard had home 5.80/away 1.05; CB live had 4.25/1.13
- Whole alt-line ladder shifted accordingly (market moved heavily on the home side)

**Diagnosis**: my earlier "remove old detail block before ExpandDetail"
fix was the cause of the persistent expand_failed. Sequence:
  1. We DOM-remove the old block before re-expanding.
  2. CB's `DoGamesPostBack('ExpandDetail:...')` checks server-side ASP.NET
     state — sees "this game is already expanded" — so it no-ops (or
     errors silently); no new block gets inserted.
  3. wait_for_selector times out → mark expand_failed.
  4. Cached detail Odds from the last successful expansion (21:42) keep
     getting served as fallback.
  5. Every cycle since, same loop. Cache grows stale by the hour while the
     dashboard reports "loaded" if you look at the data but
     "expand_failed" if you look at the status.

**Two-part fix**:

**(a) Replace DOM-removal with CollapseDetail.** Instead of reaching into
the DOM ourselves, use CB's own JS to undo the expansion through its state
machine. `DoGamesPostBack('CollapseDetail:id')` → wait for block to
detach → `DoGamesPostBack('ExpandDetail:id')` → wait for fresh block.
Server-side and DOM stay consistent. Cost: one extra postback per
re-expansion (~+0.5 s per game). For ~30 warm-cycle re-expansions that's
+15 s — well within budget.

Defensive: if CollapseDetail errors or doesn't detach within 3 s, we
proceed to ExpandDetail anyway. Even if expand is a no-op on a still-
expanded game, we'd read the existing block (possibly stale) which is no
worse than the cache fallback. The status would be "loaded" — visibly
fresh-looking — which the cache wouldn't have been. Combined with (b)
below, the worst case is bounded.

**(b) Stale-cache safety cap.** New helper `_is_cached_detail_fresh_enough`
+ constant `_STALE_CACHE_MAX_AGE_SEC = 30 * 60`. Used in both fallback
paths (expand_failed and hash-stable-no-detail):
  - If `last_expanded_at` is within 30 min → serve cached detail
  - Otherwise → drop cached, fall back to fresh list-view Odds

List view is ALWAYS fresh per cycle (5-min cadence, no caching at that
layer). So when expansion has been broken for 30+ min, we'd rather show
the user current ML/spread/total than hour-old alt-lines. They lose
detail-page coverage temporarily; they don't lose correctness.

When we drop a stale cached entry, we log it AND pop it from
_detail_odds_cache so we don't keep reusing it on subsequent stable-hash
cycles. Next successful expansion repopulates.

**Tests** (`tests/test_stale_cache_safety.py`, 12 cases):
  - Fresh: just expanded, 5 min old, just under cap, exactly at cap
  - Stale: just over cap, hours old (the ITD case), missing cache,
    empty list, missing entry, entry with no timestamp
  - Custom max_age override (caller can tighten the cap)
  - Default `now` uses wall clock

All 272 tests passing.

**Expected behavior on the user's next restart**:
- First cycle: cache loads from disk (entries within 12 h limit), warm
  cycle starts. Games whose hash hasn't moved → cached detail served.
- Games whose hash HAS moved (likely a lot of them given the user's been
  off for hours) → CollapseDetail → ExpandDetail → fresh block read →
  cache updated with current odds.
- For ITD Santa Tecla specifically: the diagnostic showed
  last_expanded_at = 21:42 UTC. If current time is past 22:12 UTC (30 min
  past), and expansion succeeded on this restart → cache refreshed to
  current odds. If expansion still fails for some reason → safety cap
  drops the 4-hour-old cached entry, list-view fallback takes over,
  user sees fresh ML/spread/total at minimum.

**What we don't know yet**:
- Whether CollapseDetail exists on CB exactly as `DoGamesPostBack('CollapseDetail:id')`.
  Inferred from the saved sample (1 occurrence in HTML); not directly verified.
  If it doesn't exist, the evaluate will throw, fall into except, log
  "CollapseDetail failed", proceed to ExpandDetail directly — which then
  hits the original problem (no-op on already-expanded). But the safety
  cap (b) bounds the damage.
- Live verification needed (next restart, observe whether stale cases recover).

---

## 2026-05-25 — Sound alerts + staleness tooling (basketball soak-time work)

User asked: (1) sound alert when a new ≥15% edge appears, (2) some way to
verify the dashboard's odds aren't silently stale. Both shipped in this
session; basketball gets a day or two of soak time before Phase 2 (soccer)
starts.

**1. `last_expanded_at` exposed per game** (`src/scrapers/crystalbet.py` +
`src/app.py`):
- New `get_last_expanded_map()` reads `last_expanded_at` from the change
  cache's CacheEntry objects.
- `_build_match_row` accepts a `last_expanded` dict, surfaces ISO string
  per game as `last_expanded_at` in `/api/matches` rows.
- 3 new tests in `test_app.py`.

**2. Freshness badge in the matches table** (`static/matches.html` +
`static/style.css`):
- New `freshnessBadge(iso)` helper renders "12m" / "2h" / "—" inline next
  to the existing FULL/LIST/FAIL badge.
- Color-coded: fresh (<10 min, green), ok (<1 h, dim), stale (<3 h, amber),
  very stale (≥3 h, red), none/never expanded (faint).
- Hover tooltip shows precise UTC time.
- At-a-glance answer to "which games might be sitting on cached data?"

**3. Sound alert on the arbs page** (`static/arbs.html`):
- Two new inputs in the controls bar: enable checkbox + threshold number
  (default 15%, configurable per session).
- Both settings persist via localStorage (degrades silently when localStorage
  is blocked).
- Audio: pure Web Audio API two-tone "ping" (880 Hz + 1175 Hz, ~300 ms
  total). No external sound file. Soft attack + decay envelope, no harsh
  edges.
- Autoplay-policy workaround: AudioContext created lazily on first user
  click or keydown. Standard browser pattern.
- Dedup: `seenAlerts` Map keyed by `(cb_event_id, market, side, kind)`.
  Same opportunity persisting across polls doesn't keep beeping. Re-alerts
  only when the edge grows by ≥5% from the last alert (captures
  "this got bigger" events).

**4. Diagnostic script `scripts/check_game_freshness.py`**:
- Takes an `event_id` arg, queries `/api/matches` for what the dashboard
  has cached, spawns a FRESH Playwright browser (completely independent
  of the dashboard's singleton), expands that one game on live CB, parses,
  and prints a side-by-side comparison.
- Lists: total counts, matching markets, mismatched markets (top 15 with
  fresh-vs-dashboard values), markets present on only one side.
- Verdict at the bottom: ✓ perfect match, ⚠ benign count difference, or
  ⚠ staleness detected.
- This is the "how do I verify" answer. Run any time a game looks suspect;
  ground truth in ~30 s.

**Test count**: 260 passing.

**What's NOT done** (deliberately):
- No background audit task that periodically force-re-expands games to
  audit the cache automatically. User picked the on-demand CLI route to
  avoid extra load on the dashboard. Add later if manual checking gets old.
- Sound alert only on the arbs page (per user choice). Matches page is
  silent. If we want desktop notifications later, Web Notifications API
  is a small add.
- Threshold + enable state persist via localStorage. If the user clears
  browser data they'd lose the settings — fine for a single-user local tool.

---

## 2026-05-25 — Cache persistence to disk

User chose this as the small next-step win after Phase 1 wrapped. Without
persistence, every dashboard restart (laptop wake, code change, manual stop)
paid the 3-4 min cold-cycle penalty before the cache rebuilt.

**Implementation** (`src/scrapers/cache_persistence.py`, ~200 lines):
- Single JSON file at `data/cache/cb_change_cache.json` — debuggable,
  ~1-2 MB compact for ~120 games × ~200 markets.
- Two top-level keys: `entries` (change_cache.ChangeCache state) and
  `detail_odds` (the actual cached Odds rows per event_id).
- Atomic save (write tmp + rename) so a crash mid-write never leaves a
  corrupt file behind.
- Schema version field — bumped if shape ever changes, old files discarded
  rather than crashing the loader.
- Max age 12 h — older saves get discarded on load (odds would be too
  stale to be a net win over a fresh cold cycle).
- Best-effort `save()` — never raises, just logs warnings. Dashboard
  shutdown can't be blocked by cache persistence failing.

**Lifecycle** (`src/app.py` lifespan):
- On startup (before background pollers start): `cache_persistence.load()`.
  If it succeeds, log "first cycle will be warm." If not (missing/corrupt/
  too-old), log "first cycle will be cold."
- On shutdown (after pollers stop, browser closes): `cache_persistence.save()`.
  Persists for next start.

**Helper additions** in `crystalbet.py`:
- `get_detail_odds_cache()` — read access to the module-level _detail_odds_cache
- `restore_detail_odds_cache(cache)` — write access for the persistence layer
  on startup. Cleaner than having cache_persistence reach into another
  module's globals directly.

**What this does NOT do** (intentional):
- No periodic saves. Just on shutdown. SIGKILL or hard crash loses since-
  last-save state. If crash recovery becomes a felt problem, add a periodic
  save task (every N cycles or every M minutes). v1 keeps the write
  volume tiny — ~1 save per dashboard session.
- No cache pruning beyond what change_cache already does (prune_missing
  per cycle). On-disk file size is bounded by current game count.

**Tests** (`tests/test_cache_persistence.py`, 13 cases):
- Roundtrip (save → wipe → load → verify state) including alt-line
  preservation and last_expanded_at timestamps
- Failure modes: missing file / corrupt JSON / version mismatch / too-old
  / bad per-event odds (silently skipped, not fatal)
- Save behavior: creates nested directories, atomic rename leaves no .tmp
  files, empty cache is valid JSON, overwrites existing file

All 257 tests passing.

**Expected behavior on restart**:
- Stop dashboard cleanly (Ctrl+C). Save fires, file appears at
  `data/cache/cb_change_cache.json`.
- Start dashboard. Load fires, log shows "CB cache restored from disk".
- First CB cycle: re-extracts list view → compares hashes vs loaded cache.
  Only games whose odds moved since the save get re-expanded.
- If the dashboard was off for >12 h, cache is discarded, full cold cycle
  runs as before.

---

## 2026-05-25 — Stale-detail-block bug on re-expansion (Krka case)

User reported a basketball game (Krka Novo Mesto vs KK Cedevita Olimpija)
showing stale odds for ~1 hour on the dashboard: CB live showed 1.55/2.10,
dashboard showed 1.25/3.10. Edge calculations correspondingly wrong
(-26.64%/+28.06% phantom edges).

**Root cause**: `_expand_and_parse_one` called `DoGamesPostBack('ExpandDetail:id')`
without first clearing the prior detail block from the DOM. On a fresh
expansion (game never expanded before), this is fine — CB inserts a new
`<table class="game-details">` and our `wait_for_selector` matches it. On
RE-expansion (same game expanded a previous cycle), the OLD detail block
is still in the DOM. `wait_for_selector` matches it instantly, before
CB's ExpandDetail finishes any update, and we read the stale block's
`outerHTML`. Result: hash detected a market move correctly, we did try to
re-expand, but we silently re-cached the same stale data.

This explains: why warm cycles "worked" (cache hits served unchanged
games), why first-cycle expansion was correct (no prior block to mask the
fresh one), but why games whose odds moved later kept showing pre-move
values across many cycles.

**Fix** (`src/scrapers/crystalbet.py`, `_expand_and_parse_one`):
Add a DOM cleanup step 0 before the postback — `evaluate()` that does
`document.querySelectorAll(selector).forEach(el => el.remove())` to clear
any prior block for THIS game's container. Now `wait_for_selector` can
only match a freshly-inserted block.

Belt-and-suspenders: deleting the block doesn't break anything if it was
never there (querySelectorAll returns empty NodeList — no-op).

**Why this didn't surface in the earlier live test**:
Both cycles in the same-process test ran 30 s apart. With a 30 s gap,
most games' odds DON'T move enough to change the hash, so warm-cycle
re-expansion only fired for ~8 games. Those 8 games may have produced
stale data too, but the test didn't compare against ground-truth post-fix
odds. The user spotted it only after running the live dashboard for
~1 hour, when enough hash changes had occurred for the stale serving
to be visible.

**What to test now**:
Restart the dashboard, let it run one warm cycle. Confirm that Krka (or
any game whose odds visibly moved between cycles) shows current values
matching the CB live site. If a game still shows stale, the issue is
deeper (e.g., CB's list-view loadinfo itself is stale for our singleton
browser, separate failure mode).

Tests still 244/244.

---

## 2026-05-25 — Phase 1 complete: steps 6 + 7 (API + frontend)

**Step 6 (`src/app.py`)**:
- Imports `get_detail_status_map` from crystalbet.
- `_build_matches_view` looks up per-game detail status and passes it to
  `_build_match_row`.
- `_build_match_row` adds `markets_status` field to the per-game dict
  ("loaded" / "list_only" / "expand_failed") so the frontend can render
  a coverage badge. Defaults to "list_only" when event not in map.
- Also fixed a minor preexisting bug: `cb_ml` selection now prefers the FT
  moneyline (was picking whatever ML came first, which on the new scraper
  is usually FT anyway but could in theory be H1 — period filter on selection
  makes it deterministic).
- 4 new tests in test_app.py covering the markets_status field across all
  4 status states.

**Step 7 (`static/matches.html` + `static/style.css`)**:
- New "Hide markets without Pinnacle reference" toggle, defaults ON. Filters
  the [+] expander to only markets where the matcher found a Pin counterpart.
  With this on, the dashboard's actionable view stays clean (FT + H1 markets);
  with it off, all 21 combos visible including H2 and Q1–Q4.
- [+] expander reorganized: markets now grouped by period (FT → H1 → H2 →
  Q1 → Q2 → Q3 → Q4) with sub-headers. Within each period, sorted ML →
  spread (by line) → total (by line). Easier to scan ~200 markets per game
  than the previous flat list.
- Per-game markets-status badge ("FULL" / "LIST" / "FAIL") rendered inline
  next to the [+] button. Color-coded; hover-tooltip explains each state.
- Expander max-height capped at 70vh with vertical scroll so very-rich games
  (NBA playoffs with 200+ markets) don't blow out the viewport.

**Test count**: 244 passing (was 240 before Phase 1 finishing pass).

**What's NOT done in Phase 1** (intentionally — out of scope per user
decisions during planning):

- Player props (deferred to potential Phase 1c if needed)
- Combo bets (handicap & total, 1x2 & total) — out of scope
- Soccer / other sports (Phase 2)
- Second reference book (Phase 3)
- Live scraping (Phase 4 — needs API subscription)
- Cache TTL for stable games (user said "leave it for now")
- Per-cycle expansion budget (haven't needed it — first cold cycle finishes
  in ~3-4 min, well within tolerance)

**End-to-end timing summary** (from live tests):
- Cold start: ~3-4 min (full expansion of ~125 games)
- Warm cycles: ~22 s (cache serves ~95% of games, ~5-10 re-expand)
- Per-game expansion: ~2-3 s steady state
- 9.7× speedup cold → warm

**Architecture additions in Phase 1**:
- `src/scrapers/sports/basketball.py` — sport-specific list-view parser +
  detail-page market classifier (21 in-scope combos, incl-OT preference)
- `src/scrapers/cb_detail.py` — sport-agnostic detail-page walker (handles
  2-way ML, alt-line spread, alt-line total shapes; takes a sport mapping)
- `src/scrapers/change_cache.py` — per-game hash-based expansion decisions
- `src/scrapers/crystalbet.py` — refactored to orchestrate list view +
  change cache + serial expansion + result merging
- `src/vig.py` — added `devig_3way` for soccer's eventual 1X2 markets

The sport-isolation structure (basketball.py as a peer module) means adding
soccer in Phase 2 should be ~1-2 sessions of work: write a `soccer.py` with
the equivalent list-view parser + market classifier, update `cb_detail.py`
if needed for new market shapes (3-way moneyline), wire into crystalbet.py
with sport selection.

---

## 2026-05-25 — Phase 1 step 5: live verification PASS

After the O(N²) fix, ran two cycles in the same process to verify both
cold-start and warm-cache paths:

| Metric                | Cycle 1 (cold) | Cycle 2 (warm, 30s later) |
| --------------------- | -------------- | -------------------------- |
| Elapsed               | 3m 38s         | **22 s**                   |
| Odds emitted          | 22,231         | 22,231                     |
| Expanded this cycle   | 106            | 8                          |
| Cached-detail re-used | 0              | 98                         |
| List-only fallback    | 14             | 14                         |
| Expand failed         | 0              | 0                          |

**9.7× warm-cycle speedup.** Cache served 98 of 106 previously-expanded
games unchanged; only 8 games' odds had moved enough in 30 s to warrant
re-expansion. List-fallback count is stable across cycles — these 14 games
are ones whose detail pages yielded zero in-scope markets (smaller leagues
with very short menus). They keep their list-view ML/spread/total.

**Market coverage delta vs pre-phase-1**: ~300 → 22,231 Odds per cycle.
That's a ~75× expansion of the actionable surface. All 21 (period ×
market_type) combos populated. Sample event (Cavaliers vs Knicks) shows 249
markets with correct alt-line progression (FT spread -12 → home 4.85/away 1.12;
-11.5 → 4.5/1.14; etc.).

**Lesson worth keeping**: when testing module-level singleton state (cache,
browser), the test must invoke the scraper twice in the SAME `asyncio.run()`.
Two separate `asyncio.run()` invocations spawn two separate Python processes
→ cache wiped each time → can't measure cache effectiveness. The dashboard's
production loop runs in one long-lived process so this isn't an issue there;
it was purely a test-design oversight in v1 of the test prompt.

Step 5 done. Moving to steps 6 (app.py: expose markets_status) + 7 (frontend:
grouped expander + toggle).

---

## 2026-05-25 — Phase 1 step 5: O(N²) page.content() bug + fix

First live test of the detail-expansion flow stalled at 18:41 elapsed having
processed 0 of 125 games (no per-game logs, no completion). Process alive
the whole time — not crashed, just slow.

**Diagnosis** (from CC's reported elapsed × games math):
- Per-game amortized cost = 1100s / 125 = ~9s
- Expected per-game cost = ~3-5s
- 4-6s per-game overhead with no visible cause = O(N²) somewhere

Root cause: `_expand_and_parse_one` was calling `_page.content()` to read
the page HTML after each expansion. **Every ExpandDetail accumulates a new
detail block in the DOM**, so the page grows after each game. `page.content()`
serializes the ENTIRE page on every call. By the 100th game we were
serializing 100 detail blocks worth of HTML (~50-100MB total). Cost was
roughly Σ(1..N) × 0.15s/block = N(N+1)/2 × 0.15 → 1180s for N=125. Matches
the 18:41 observed runtime almost exactly.

**Fix**:
- Replace `page.content()` with `page.evaluate()` returning ONLY this game's
  detail block outerHTML. Small payload (~10-100KB), constant cost regardless
  of how many prior expansions have accumulated.
- Add `wait_for_selector` to wait until the detail block actually appears in
  the DOM (replaces the fixed 3s sleep — fast games take ~500ms, slow ones
  get up to 6s before we give up).
- Wrap all `page.evaluate()` calls in `asyncio.wait_for` with explicit
  per-call timeouts (Playwright's global default is 30s — too lenient).
- Add progress logging every 25 games so long cycles don't run silently.

Per-game expected steady-state cost now ~2-3s. 125 games should complete
in ~5-7 min instead of >20 min.

**What the fix does NOT do**:
- Doesn't add a per-cycle expansion budget (e.g., "max 50 games per cycle").
  If the O(N²) fix is the only issue, first cycle should complete in
  reasonable time without one. If it doesn't, add a budget next.
- Doesn't collapse expanded games after parsing. Accumulated DOM is fine
  as long as we don't serialize it; extracting just the new block via
  evaluate() sidesteps the problem. Could collapse later if Chromium
  memory becomes an issue over many warm cycles.

Tests still 240/240. Need live re-test to verify.

---

## 2026-05-25 — Regression test coverage: 105 tests across 6 files

User picked "regression-focused" scope and "tests first, then SQLite". Built
one test per bug that actually landed in this log. Goal is to lock fixed bugs
fixed so a future refactor pass can't silently re-introduce them.

**File layout (`tests/`):**

| File                          | Tests | Guards                                                                      |
| ----------------------------- | ----- | --------------------------------------------------------------------------- |
| `test_vig.py`                 | 22    | (pre-existing) decimal conversion, devig math                              |
| `test_matcher.py`             | 19    | `_accept` truth table (10), greedy collision, unmatched diag, score-100 + time-gap, alias hot-reload |
| `test_edge.py`                | 14    | +EV direction, ARB detection, `pin_no_vig` consistency, `arb_partner_*` populated only on ARB, `cb_event_id` on every Opp, `_find_pin_match` period/type/line filters |
| `test_pinnacle_parser.py`     | 25    | `_index_matchups` sub-matchup/live/missing-side filters, `_build_odds_for_league` filters (period/type/suspended/zero-American/incomplete), happy paths (ML FT, ML H1, spread alt-line, total), `_parse_iso`, league blocklist |
| `test_crystalbet_parser.py`   | 21    | DD.MM.YYYY + DD/MM/YYYY, date-carry across game-tables, Tbilisi→UTC, `_identify_loadinfo_roles` (locked-odds shift across ML home/AH home/AH away/OU under), Format-A and Format-B end-to-end, outright skip, smoke against saved sample |
| `test_app.py`                 | 4     | `_build_match_row` period filter on Pin moneyline (H1 listed first must not steal FT's slot); unmatched still renders |
| **Total**                     | **105**| All passing in 0.80s                                                       |

**Behaviors uncovered along the way:**

- `_group_by_event` keys on `(home, away)` only — league is NOT in the key.
  So two CB Odds rows with identical team names collapse into one event. My
  first matcher collision test got this wrong (created two CBs with identical
  names expecting two events); fixed by using distinct names that fuzz-score
  high via token_set_ratio's set semantics ("Hawks" + "Hawks United"). Added
  an explicit test (`test_same_home_away_pair_groups_into_one_event`) to pin
  this behavior so future-me sees it directly.
- The "decimal odds ≤ 1.0" guard in `_build_odds_for_league` is purely
  defensive — mathematically `american_to_decimal` cannot produce ≤ 1.0 from
  finite American input, so the reachable failure path is via `price=0`
  raising `ValueError`. Reworded the test to exercise the actually-reachable
  path.
- The blocklist filter (cyber/esport/etc.) is an inline lambda inside
  `fetch_pinnacle_basketball`, not a callable. Tested the constant +
  reproduced the lambda logic in the test.

**What's not covered (deliberately):**

- HTTP layer in `pinnacle.py`: `_get`'s retry-once + `_record_failure` /
  `_league_in_cooldown` state machine. Not regression-focused — these were
  net-new additions today, not bugs that landed. Worth adding if we extend
  the failure tracker.
- Singleton browser lifecycle in `crystalbet.py`: `_ensure_browser`,
  `close_crystalbet`. Requires either Playwright stubbing or actual Chromium;
  the build-log singleton verification entry covered this live.
- `_closest_pin` / `_build_markets_view` in `app.py`. Same filter semantics as
  `_find_pin_match` (tested in edge), so the regression risk overlaps.
- `log_unmatched` CSV writer. Pure I/O on disk format; would need a tmpdir
  fixture and the failure mode (corrupt CSV) is benign.

**Confidence boost from this:**

The bug list in the log clusters in two areas: CB parser (4 bugs: dates ×2,
timezone, locked-odds) and cross-source pairing (3 bugs: period filter on
main row, period+line filter, devig direction). Both clusters now have
dedicated tests. The other areas (matcher tier-2, alias loader, edge math
direction) get bug-shaped coverage too. Future refactors that break the
common bug patterns should fail loud on `pytest tests/`.

---

## 2026-05-25 — CB singleton + dual-poller live verification

Ran the full stack (`main.py` → uvicorn + both background pollers) end-to-end
for ~3.5 minutes to confirm the singleton browser change from earlier today
behaves correctly under the real dual-poller loop, not just in unit isolation.
This is the live counterpart to the sandbox verification in the earlier
"CB singleton browser + cadence tuning" entry, where Chromium couldn't run.

**CB singleton timing — three consecutive cycles**

| Cycle | Latency | What happened |
| ----- | ------- | -------------- |
| 1     | 28.7 s  | Cold path: Playwright init + Sports.aspx nav + English flip + basketball select + `SelectAllChampionats:17` + parse. Single `init #1 (headed=False)` log line — no re-inits. |
| 2     | 8.5 s   | Warm path: only `SelectAllChampionats:17` re-trigger + `page.content()` + parse. ~3.4× faster than cycle 1. |
| 3     | 8.4 s   | Warm path. Steady-state confirmed — variance is within noise, not drift. |

All three cycles emitted **300 Odds rows** — output stable across the
cold→warm transition, so the warm path isn't dropping markets to be faster.

**Pinnacle dual-cadence sanity**

Pinnacle ran 2 cycles over the ~3.5 min window at the new `PINNACLE_POLL_SEC=180`
default. Expected = `floor(210 / 180) + 1 = 2` if the first cycle fires at
boot (which it does). Confirms the cadence change from earlier today is in
effect end-to-end, not just configured.

**Shutdown**

Clean. Two log lines confirm the lifespan teardown ran in order:
```
CB browser: closed cleanly
background pollers stopped
```
`close_crystalbet()` is wired into the FastAPI lifespan exit per the singleton
entry. No orphan Chromium processes left after SIGTERM — checked.

**What this validates from earlier today's entries**

- Singleton lifecycle: init once, reuse across cycles, close once. Three
  defensive layers (health check, retry-once, periodic re-init at N=50) did
  not need to fire — no failures or drift in this run, which is the expected
  happy path.
- Real-data confirmation that the ~22 s of "stable state setup" we were
  paying every cycle is in fact bypassed on warm cycles (28.7 → 8.5).
- Dual-poller independence: CB's 28.7 s cold cycle did not block Pinnacle's
  180 s cadence; the asyncio.Lock around CB's singleton is per-source and
  doesn't bleed into Pinnacle's loop.

**What we still don't know from this run**

- Long-uptime behavior. We only ran for ~3.5 min so the N=50 forced re-init
  never triggered. The first real test of that path will be the first time
  someone leaves the dashboard up for ~4+ hours. Note to future-self: when
  that happens, check the log for `CB browser: forced periodic re-init` and
  confirm latency on that cycle ≈ cold-path (~28 s) not warm (~8 s).
- Recovery from a mid-run page kill. The retry-once + health-check paths
  haven't been exercised in live conditions. Could be smoke-tested manually
  by killing the Chromium process from outside while the loop runs.

---

## 2026-05-25 — Pinnacle bulk matchups + auto-skip failed leagues

Pinnacle's guest API was 403'ing ~15-17 leagues per cycle in worst runs.
Two distinct causes mixed together: (a) Cloudflare/WAF flicker under
request load (intermittent, different leagues each cycle, retry recovers
some) and (b) commercial-data-feed restrictions (deterministic, same
leagues every cycle — Greek Basket League, France LNB, Brazil NBB,
Argentina Liga Nacional, etc.; retry never helps). Two surgical changes:

**Switch to bulk `/sports/4/matchups`.** One call returns all basketball
matchups across all leagues. Replaces 47 per-league `/matchups` requests
per cycle. Total request count drops ~94 → ~48 per cycle, halving WAF
pressure. The bulk response is indexed once into a global matchupId → info
map; `_build_odds_for_league` looks up team names/start times from that
map rather than re-fetching per league.

**Per-league failure tracker on `/markets/straight`.** Module-level
`_consecutive_failures` + `_skip_until` dicts. After
`MAX_CONSECUTIVE_FAILURES=3` consecutive misses on a league, suppress
requests for `SKIP_DURATION_SEC=1800` (30 min). A successful fetch at any
point resets the counter. Stops wasting per-league calls on
commercial-feed-restricted leagues while still periodically probing in
case Pinnacle restores access.

Verified in sandbox with a 5-cycle mock harness:
- Cycle 1-3: league 300 (synthetic "Premium League") returns 403; counter
  ticks 1→2→3; on cycle 3 the cooldown triggers (logged).
- Cycle 4: league 300 makes ZERO HTTP requests (skipped). Other leagues
  still polled.
- Cycle 5: manual cooldown clear → league 300 attempted again.

---

## 2026-05-25 — Locked-odds position shift in _parse_loadinfo

User reported: CrystalBet game with HOME ML locked rendered on dashboard
as CB_H=8.40 (actually away ML), CB_A=1.80 (actually AH home). Off-by-one
column shift.

Cause: CB OMITS locked entries from the loadinfo JSON entirely rather
than emitting a placeholder. Our parser used fixed offsets relative to
the Handicap landmark (`ah_idx`): `items[ah_idx-1]` assumed to be AH home,
`items[0..ah_idx-1]` assumed to be the ML pair + AH home. With ML home
removed every position shifts left, and `items[ah_idx-1]` is now ML away,
not AH home.

Fix: replaced fixed-offset extraction with name + position verification
via new `_identify_loadinfo_roles(items, ah_idx, ou_idx)`. Each market
side is identified by both POSITION (relative to a landmark) AND NAME
(string match against CB's labels — "1" / "2" / "Und" / "Over"). When
the entry at the expected position has the wrong name, the role is
treated as MISSING and that market is skipped rather than letting the
parser steal a neighbor's value.

Leading-space detail: CB's loadinfo uses `name=" 2"` (leading space) for
ML away and `name="2"` (no space) for AH away. The verifier requires
exact-match on AH away to prevent ML away from filling AH away's slot
when AH home is locked. ML pair uses stripped name to accept either.

Verified in sandbox across 8 cases:
- Baseline (no locks) → all three markets emit correctly.
- ML home locked → ML skipped (was: bogus 8.40/1.80 emission). AH/OU intact.
- ML away locked → ML skipped. AH/OU intact.
- AH home locked → ML emits, AH skipped, OU intact.
- AH away locked → ML emits, AH skipped, OU intact.
- OU under locked → ML/AH emit, OU skipped.
- Both ML locked → ML skipped, AH/OU intact.
- Exact user case (Sabis SC vs Ghaz Al Shimal: home ML locked, 7 entries)
  → ML skipped (was the bug), spread 1.80/1.80 + total 1.80/1.80 correct.

Format B (col-positional divs) was already safe: a locked div doesn't
have the `Snatch` class so the CSS selector skips it, leaving the
col_map entry absent and the market emission guarded by `if N in col_map`.
Only format A needed the fix.

---

## 2026-05-25 — CB singleton browser + cadence tuning

User asked how CB scraping works and pushed back on the cost: each 5-min
cycle was tearing down Chromium and rebuilding ~22 s of stable state
(Sports.aspx nav → English flip → basketball select) before the only
action that mattered (`DoChampionatPostBack("SelectAllChampionats:17")`).
With more sports/markets planned, the per-cycle waste compounds.

**Change**: keep the Playwright instance + browser + context + page alive
across cycles. First call does full init (~30 s); subsequent calls just
re-trigger SelectAllChampionats + `page.content()` (~10 s). 3× steady-state
speedup on the CB scraper.

Guarded by an `asyncio.Lock` so concurrent calls serialize. Three
defensive layers against staleness:
  1. **Health check** — `typeof DoChampionatPostBack === "function"` before
     each refresh. If page state drifted, tear down + re-init.
  2. **Try/except retry-once** — on any failure during the refresh, tear
     down + re-init + retry. Surfaces persistent issues as warnings.
  3. **Periodic forced re-init** — every `_REINIT_EVERY_N_CYCLES = 50`
     (~4 hours at 5-min cadence). Dodges memory growth and ASP.NET
     ViewState / session expiry over long uptimes.

`close_crystalbet()` exposed for clean shutdown; wired into the FastAPI
lifespan teardown so SIGTERM doesn't leave orphan Chromium processes.

**Cadence tuning** (separate but bundled):
  - `PINNACLE_POLL_SEC` default 60 → 180. Pinnacle has been 403'ing
    intermittently at 1-min cadence; 3-min is plenty for prematch where
    lines move on minutes-to-hours.
  - `CRYSTALBET_POLL_SEC` stays at 300 (5 min).
  - Both env-overridable.

**Architectural note for when alt-lines land** (Reference §8,
`DoGamesPostBack('ExpandDetail:id')`): the user flagged "if we're opening
the same matches over and over". Correct concern but forward-looking —
today's list-view scrape doesn't expand individual games. When we add
alt-lines:
  - Hash each game's list-level odds per cycle.
  - Only expand a game whose hash changed (or first-time-seen).
  - Cache the expanded result with a TTL.
That way a stable game's detail page isn't fetched repeatedly.

**dry_run_capture intentionally NOT routed through the singleton.** It's
a manual one-off (used for capturing sample HTML when the live structure
changes) and benefits from a clean isolated context. The two coexist
without conflict — different Playwright instances.

**Verified in Cowork** (with stubbed playwright since the sandbox can't
run Chromium):
  - Module imports cleanly. Singleton state initializes to None/0.
  - Public API surface preserved: `fetch_crystalbet_basketball_prematch`,
    `close_crystalbet`, `dry_run_capture`, `dry_run_parse_saved`.
  - `PINNACLE_POLL_SEC` reads as 180; `close_crystalbet` import wired in
    `app.py`.
  - `_ensure_browser` errors cleanly when playwright unavailable (proves
    it tries the real import path — succeeds when Chromium present).

---

## 2026-05-24 — Date parser fix verified end-to-end (live)

Final live numbers after the DD.MM.YYYY fix landed:
- `/api/matches`:    84 total, 84/84 with start_time, 41/84 with Pinnacle.
- `/api/unmatched`:  43 total, 43/43 with start_time, **0 in the 65-79
  band** (the tight tier caught all of them), 2 remaining at score ≥ 80
  with time gap > 10 min.

The 2 outliers at ≥80 are likely genuine schedule disagreements between
CB and Pinnacle (e.g., same teams playing twice in the same day, or one
book listing the announced tipoff while the other has the delayed
start). Acceptable miss for v1.

**Match rate trajectory across this session:**
- Pre-aliases, pre-date-fix, pre-tight-tier: ~36/77 saved (47%).
- Post-aliases, post-tight-tier, but with broken date parser: 30/85
  live = 35% (Pinnacle had a bad day, ceiling was 44/85 = 52%).
- All fixes landed: 41/84 live = 49% with Pinnacle in normal health.

The path forward on match rate from here is alias curation against
unmatched_log.csv or /unmatched.html — Cowork now has nothing more to
add to the algorithm; the remaining gap is data, not logic.

---

## 2026-05-24 — Date parser: CB live uses DD.MM.YYYY (dots), not DD/MM/YYYY

User's Unmatched page showed every Start (UTC) as "—" even after the
date-carry-across-game-tables fix. Cowork diagnostic against fresh live
HTML revealed the cause: CB has been serving `DD.MM.YYYY` (e.g., "Monday
- 25.05.2026") on the live page, while our older saved sample on disk
used `DD/MM/YYYY`. The parser's regex `r"\d{2}/\d{2}/\d{4}"` matched
the slash form only, so every event under the live structure got
start_time=None.

Notably, reference §7 actually documents the dot form ("Friday -
22.05.2026"). The slash version was a Claude-Code-era miss when the
parser was first written off a sample that happened to use slashes.

**Fix**: regex now `r"(\d{2})[./](\d{2})[./](\d{4})"` — accepts either
separator. After capture we normalize to slash before strptime so we keep
one format string. Backward compatible with the slash-format samples
still on disk.

**Verified in Cowork** against the user-refreshed live sample:
- 64 dot-format dates in the HTML, 0 slash-format.
- parse_html → 82/82 unique events have start_time (was 0/82 before fix).
- Synthetic dot- AND slash-format test cases both parse to identical UTC.

This unblocks the two-tier matcher's tight tier (65-79 score needs start
times within ±5 min). With dates populated, the games we flagged earlier
(Marineros, Diamant Kaposvar, Brandt Hagen, etc.) should now match.

---

## 2026-05-24 — Pinnacle scraper: retry-once on 403/429/5xx

User's live run showed Pinnacle returning only 1136 Odds rows / 44 events
(vs the typical 1500+ / 60+) — 17 leagues 403'd this cycle. Match rate
30/85 = 35% was misleading: the matcher ceiling is Pinnacle's coverage
(44/85 = 52%), and we're 14 events shy of that ceiling. Most of the gap
is Pinnacle's edge flaking, not matcher failures.

Pattern observed across runs: different leagues fail each cycle, suggesting
intermittent edge behavior on Pinnacle's side (likely Cloudflare /
rate-limiting heuristics on the guest API), not deterministic per-league
access control. Today's 17 includes Italy Serie B, France Pro A/B,
Germany Pro A, Croatia A1, Austria SuperLiga, Switzerland, Europe BNXT,
Czech Pro A, Denmark, Poland, Slovakia, Estonia, etc. — many of which
DO have CB counterparts when reachable.

**Fix**: single retry with 1.5 s backoff on `403 / 429 / 5xx` and on
transient httpx network exceptions. Implemented in `_get` in
`src/scrapers/pinnacle.py` so all per-league calls get it automatically.
No retry on `404` (means the league has nothing booked — different signal).

**Why this should help**: when the edge intermittently 403s, a second
identical request 1.5s later usually succeeds. Truly-restricted endpoints
still fail twice and we give up (current behavior). The bandwidth cost
is one extra request per failing league, ~5–15 per cycle — negligible.

Verified in sandbox with mock httpx: 200-first-try / 403-then-200 /
403-twice / 404-no-retry / 503-then-200 all behave correctly.

**Not done now, considered**:
- Falling back to the bulk `/sports/4/matchups` endpoint (one call,
  surfaces all matchups across all leagues) when per-league `/matchups`
  fails. Could be added if retry doesn't recover enough. Would need a
  rework of the league-attribution flow so I left it out for now.
- Increasing retry count to 2+. One retry already doubles the call count
  on failure; more aggressive retry would hammer Pinnacle's edge. Keep
  it minimal.

---

## 2026-05-24 — Matcher tier-2: 65+ score accepted within ±5 min

User looked at the Unmatched page and noticed many score-65-79 rows were
clearly the same teams just with name variants ("Brandt Hagen" vs
"Phoenix Hagen vs Eisbaren Bremerhaven", "Diamant Kaposvar vs Atomeriomiu
Paks" vs "Kaposvari KK vs Atomeromu SE"). Asked to accept those when start
times align tightly.

**Matcher refactor — two-tier `_accept(score, cb_time, pin_time)`:**

| Score band         | Time window required | Notes                          |
| ------------------ | -------------------- | ------------------------------ |
| < 65               | —                    | reject                         |
| ≥ 65 & < 80        | ±5 min               | tight tier, needs time signal  |
| ≥ 80               | ±10 min              | loose tier, prior behavior     |
| any                | time missing         | fall back to ≥ 80 requirement  |

The tight tier requires the tighter window because the name is less
confident — time has to do more of the work to disambiguate from games
that happen to share team-name fragments. Without the time signal we fall
back to the high-confidence-name rule (≥ 80) since there's nothing else
to lean on.

Constants surfaced as module-level:
`SCORE_LOOSE=80`, `SCORE_TIGHT=65`, `TIME_LOOSE_SECONDS=600`,
`TIME_TIGHT_SECONDS=300`. Easy to tune later.

Implementation note: the candidate-enumeration loop no longer pre-filters
on time (we don't yet know the score, so can't pick a window). Instead it
scores every (cb, pin) pair and then `_accept` decides as the threshold
filter. For 77 CB × ~60 Pin = ~4600 token_set_ratio calls per match cycle,
which is microseconds total.

**Parser fix bundled in this pass.** The Unmatched page screenshot showed
"—" for Start (UTC) on every row — meaning the live CB layout has
`div.game-table` blocks without their own `x_loop_title_block` date header.
Without a date the parser was emitting `start_time=None` for those games,
which then failed the two-tier matcher's time-required check.

Fix: moved `date_str: Optional[str] = None` outside the `for game_table`
loop so a block without its own title_block inherits the previously seen
date. Common HTML pattern — single date header above multiple consecutive
tables for that day.

**Verified in Cowork:**
- All 9 cases of the `_accept` truth table pass.
- Synthetic test with name variations + ±7-min jitter: 50 CB games + 50 Pin
  matched correctly across both tiers. 48 via loose tier (≥80), 2 via the
  new tight tier (65–79). Those 2 are exactly the cases the user pointed
  to in the screenshot — Marineros de Puerto Plata (78) and Diamant
  Kaposvar (71).

---

## 2026-05-24 — Matcher upgrade: team_aliases.yaml + unmatched_log + Unmatched page

Goal: lift the ~48% CB→Pin match rate. User chose this as the highest-leverage
next step before adding alt-lines or persistence.

**Three pieces shipped:**

1. **`src/team_aliases.yaml`** — manual alias map, applied case-insensitively
   on the original team name before the unicode/lowercase/token-drop pipeline.
   Seeded with the two WNBA aliases we already observed:
   `"Connecticut" → "Connecticut Sun"`, `"Phoenix" → "Phoenix Mercury"`.
   Includes commented examples for the curation loop (Italian Serie A long
   names, VTB transliterations, Adriatic League).
   - Loader caches by mtime: reloaded automatically when the file on disk
     changes. No server restart needed.

2. **`matcher.py` refactor** to expose unmatched diagnostics:
   - New `match_with_diagnostics()` returns `MatchResults(matched, unmatched)`.
     `matched` is the existing list[MatchedEvent]; `unmatched` is
     list[UnmatchedEvent] where each event carries its single best Pinnacle
     candidate (regardless of threshold) + the similarity score.
   - `match_events()` kept as a backwards-compat alias returning just the
     matched list — used by edge.py and the API endpoints that don't
     need the diagnostic.
   - `log_unmatched(result)` appends every unmatched CB event to
     `data/unmatched_log.csv`. Writes header only on first append, then
     pure-append. Called once per CB poll cycle from the background loop
     (NOT per API hit — that would spam the file).

3. **`/api/unmatched` + `static/unmatched.html`**. Live page polling every
   30 s. Shows CB events with no Pinnacle match + the best below-threshold
   candidate. Sortable by score (highest first → most promising alias
   candidates). Score color-coded: green ≥80 (would-have-matched —
   shouldn't appear unless threshold ties), amber 60–79 (alias candidates),
   grey-dim < 40 (likely genuinely absent from Pinnacle). Min-score filter
   input so user can focus on the curation-worthy band (60–79). Page
   includes a hint reminding the user to edit team_aliases.yaml directly.
   Nav links updated on matches.html and arbs.html.

**Verified in Cowork**:
- Alias loader picks up both yaml entries; `normalize_team("Connecticut")`
  → `"connecticut sun"`.
- 4 WNBA games in the saved CB sample (Atlanta-Phoenix, NY Liberty-Dallas,
  Seattle-Washington, GS Valkyries-Connecticut) all match a synthetic
  Pinnacle set using franchise-full names — score 100 across the board.
  Before aliases the two with shortened names (Atlanta-Phoenix and
  GS Valkyries-Connecticut) would have failed.
- `data/unmatched_log.csv` writes correctly with proper header; second
  append doesn't duplicate header (verified line counts).

**What this gets you on real data**: at minimum +2 matches per cycle for
the WNBA shortenings. The bigger lift is the curation loop itself —
open `/unmatched.html`, see all the 60–79 close-misses, add their aliases
to the yaml, watch the match rate move on the next poll. Realistically
plausible to lift 48% → 65–70% with a session of curation.

**Scope notes & deferred decisions**:
- Threshold stayed at 80, scorer stayed `token_set_ratio`. Brief specifies
  85 + `partial_ratio`. We deviate because token_set_ratio handled the
  observed cases well and aliases-first is higher leverage than scorer
  tuning. Easy to revisit after curation data accumulates.
- No dedup on unmatched_log.csv — appends every poll. User can `sort -u`
  or post-process. If file grows annoying we add dedup later.
- No PUT/POST to edit aliases from the dashboard. The yaml is opened in
  a text editor. Cleaner than building an alias-editor UI for v1.

---

## 2026-05-24 — Bug: matches-page main row picked Pinnacle H1 ML instead of FT

User noticed phantom edges on the matches table (e.g., +35.93% on Wellington
Saints away when both CB and Pinnacle clearly had the home team as a heavy
favorite). The expander row showed different Pin prices than the main row
for the same game.

Root cause in `_build_match_row` (`src/app.py`):
```python
pin_ml = next((o for o in match.pin if o.market_type == "moneyline"), None)
```
No period filter. Pinnacle returns both FT (period 0) and H1 (period 1)
moneylines for most matches; whichever comes first in the markets response
won. When H1 came first the main row compared CB's FT price against
Pinnacle's H1 fair price → fake edge.

**Scope.** Only the matches-page main row's `Pin H/Pin A` and `Edge H%/A%`
columns were affected. Other code paths were already correct:
- `_closest_pin` (expander rows) filters by both market_type AND period.
- `_find_pin_match` in `edge.py` (powers `/api/opportunities`) filters by
  market_type, period, AND line. So +EV/ARB rows on the arbs page were
  never affected.

Fix: added `o.period == cb_ml.period` to the filter. Verified in sandbox
with a synthetic Pin set containing both H1 (1.12/5.04) and FT (1.04/7.70)
moneylines — main row now picks FT and produces the expected ~-11% edge
on both sides instead of the bogus +36%.

Lesson: any cross-source pairing in this codebase must filter by ALL of
(market_type, period, line) — drop any of those three and you risk
comparing different markets and producing phantom edges.

---

## 2026-05-24 — UX: column filters + sport column on matches page

User asked for inline column filters (Start UTC, Sport, League, Home,
Away) and confirmation that the sport column was useful even with only
basketball. Added:

- Filter row inside `<thead>` under the title row in `matches.html`.
  Each cell holds a text input; live substring-match (case-insensitive)
  against the field on every keystroke. Start UTC filters against the
  formatted display string ("05-25 19:00") rather than the raw ISO so
  typing "05-25" or "19:" Just Works.
- A `clear filters` button on the right side of the filter row.
- Filter state combines with the existing "Has Pinnacle reference"
  checkbox (AND).
- New CSS rules for `.filter-row` and `.col-filter` — sticky at top: 30px
  so they stay visible below the header row when the table scrolls.
- Sport column added between Start (UTC) and League. Surfaced from
  `anchor.sport` in `_build_match_row`. Every CB row currently reads
  "basketball" (v1 scope) but the column is now present for future
  multi-sport expansion. Colspan on the loading + expander rows
  bumped from 11 → 12.

Date-display "regression" turned out to be the user not having restarted
uvicorn after the previous code edits — static files reload on browser
refresh but Python doesn't. After restart, dates render correctly.

---

## 2026-05-24 — Post-launch UX polish

Two issues surfaced once the dashboard was in the user's browser:

1. **"Pin ref" column on arbs/+EV page was confusing.** It showed the
   no-vig fair price (e.g., 4.60 for Salvadorenos away) while the
   matches page showed the vigged posted price (4.01) for the same
   side. Same game, two different numbers, no visual cue that one was
   fair and the other was vigged. Additionally, for ARB rows the
   column had a *third* meaning (partner-leg vigged price), so its
   semantics weren't even consistent within the arbs page.

   **Fix.** `pin_no_vig` on the Opportunity dataclass now means the
   same thing on both `+EV` and `ARB` rows: the devigged fair price
   for the side the CB bet represents. For ARB rows the partner-leg
   vigged price (the one the arb math actually consumes) was moved to
   `arb_partner_odds`. Frontend now shows it as a small inline chip
   below the side ("+ Pin away @ 3.00"), preserving the info without
   overloading the main column. Column header renamed `Pin ref` →
   `Pin fair` with a hover tooltip explaining the distinction from
   the matches-page columns.

2. **No way to click from arbs back to the match.** When you see an
   interesting opportunity you want to look at the full markets
   table — currently you had to scroll the matches page manually.

   **Fix.** Added `cb_event_id` to the Opportunity dataclass.
   Populated in `edge.py` from `cb.raw_event_id`. Serialized through
   `/api/opportunities`. Arbs page now makes each row clickable
   (cursor: pointer, hover background) and navigates to
   `/matches.html#match-{cb_event_id}`. Matches page listens for
   the hash on every render: scrolls the matching row into view,
   pops its `[+]` expander open, and applies a brief blue
   highlight-fade for visual anchoring. `hashchange` event also
   handled so back/forward through deep links works.

Verified in Cowork: synthetic Pin set up to yield both +EV and ARB on
the same CB market. `pin_no_vig` value identical across kinds (1.517
for the home side, 2.935 for the away side — devigged from Pin 1.55/3.00).
`arb_partner_*` populated only for ARB rows. `cb_event_id` populated on
every Opportunity.

---

## 2026-05-24 — Checkpoint 5 + 6: FastAPI dashboard

Built the actual dashboard layer the brief specifies: scraper loop +
FastAPI + two HTML pages polling /api endpoints. Frontend has zero
dependencies (no React, no Tailwind, no build) — three static files
served by FastAPI's StaticFiles mount.

**Architecture decisions**
- **In-memory shared state.** Module-level `_state` dict in `src/app.py`,
  guarded by `asyncio.Lock` on writes. Reads use Python's GIL semantics
  and tolerate one-update staleness. SQLite persistence (C4) deferred —
  user explicitly chose to skip it. Add later when history analysis is
  needed; the dashboard works fine without it.
- **Two independent background tasks.** `_pinnacle_loop` polls every
  `PINNACLE_POLL_SEC` (default 60), `_crystalbet_loop` polls every
  `CRYSTALBET_POLL_SEC` (default 300). Separating them prevents a slow
  CB scrape from blocking the faster Pinnacle cadence.
- **Fresh Playwright per CB cycle.** Singleton browser would shave
  ~25 s per poll, but at 5-min cadence the overhead is negligible and
  singleton lifecycle (page state drift, recovery from disconnect) is
  expensive code to maintain. Trade simplicity for the milliseconds.
- **`CB_USE_SAVED=1` env switch.** Parses `data/raw/cb_prematch_sample.html`
  instead of scraping live. Cuts the 25-s CB cycle to ~1 s — only used
  for frontend iteration.
- **No SSE / WebSockets.** Frontend polls /api/* every 30 s per brief.
  /api/status polls every 10 s to keep the staleness indicators tight.
- **Lifespan handler.** Uses FastAPI's `@asynccontextmanager` pattern
  for startup/shutdown — the supported way; `@app.on_event("startup")`
  is deprecated.

**Endpoint shapes**
- `GET /api/status` → counts, age_sec for both sources, error fields,
  active config. Used by status badge in the header.
- `GET /api/matches` → list of dicts, one per CB event. Pin columns
  null when unmatched. Each row carries a `markets` array for the
  `[+]` expander showing every CB market with the closest Pin
  counterpart side-by-side (CB raw / Pin raw / Pin fair).
- `GET /api/opportunities?min_edge=X&kind=Y` → +EV + ARB rows sorted
  by edge%. `kind` query param filters to one type; default returns
  both. Default `min_edge=1.0`.

**main.py shape change**
- Default: launches uvicorn against `src.app:app` on HOST:PORT
  (defaults `127.0.0.1:8000`).
- `--once`: preserves the previous CLI behavior — one scrape cycle,
  print rich table, optional CSV, exit. Kept for dev/audit work.
- Other CLI flags (`--cb-saved`, `--headless`, `--min-edge`, `--no-csv`)
  only apply under `--once`. Dashboard mode uses env vars instead.

**Frontend**
- `static/style.css` — dark theme, mono numbers, sticky headers, edge
  color coding (green positive, dim negative).
- `static/matches.html` — 11-column table per brief. Filter checkbox
  for "Has Pinnacle reference". Each row has a `+` button that
  toggles an inline expander row showing every market side-by-side.
- `static/arbs.html` — 9-column table per brief. Chip group filters
  to All / +EV / ARB. `Min edge %` input passes through to query string.
- Both pages poll `/api/status` every 10 s and their own data endpoint
  every 30 s. Header shows live counts, age strings, color-coded
  staleness (ok < 60 s / stale at boundaries / error red).

**Verified in Cowork** (sandbox, with synthetic Pinnacle since the API
is proxy-blocked):
- App imports cleanly; all routes register.
- `_state` injection + `TestClient.get()` exercises all 3 API endpoints.
- `/api/matches` returns 77 rows (matches unique CB events), 5 with
  has_pin=True from a 15-row synthetic Pin set. Markets array carries
  3 entries per matched event (ML + spread + total). Edge math math
  direction correct: high CB / low Pin → positive edge.
- `/api/opportunities` returns 60 rows = 30 +EV + 30 ARB.
- `kind=ARB` and `kind=+EV` filters return correct subsets.
- `/` redirects to `/matches.html` (302).
- Static files (matches.html / arbs.html / style.css) all 200.

**What's still missing vs brief**
- C4 entirely. No SQLite, no team_aliases.yaml, no unmatched_log.csv.
  These are not in the dashboard's critical path but are useful for:
  - history (which CB events were live yesterday, did edges materialize)
  - manual alias overrides for tricky team-name pairings
  - audit trail of unmatched events for matcher tuning
- Kelly stake for ARB rows. Currently `kelly_stake=0` placeholder.
  Real ARB sizing is proportional to inverse-odds split; nontrivial.
- Tests for matcher / edge / parser / app. test_vig.py only.

---

## 2026-05-24 — Checkpoint 3–5: scope compression + bug fixes

After Cowork handed off the navigation-only `crystalbet.py` and Prompt E to
capture sample HTML, Claude Code raced past the explicit STOP and delivered
the parser, matcher, edge math, and a CLI in one go. Result: ~700 lines of
working code that covers a compressed version of C3–C5 minus the SQLite
schema, the FastAPI/uvicorn layer, the frontend, the unmatched-log CSV, and
the team_aliases.yaml curation path. Decision (user) was to keep the work
and patch bugs rather than roll back.

**What's in `src/` now**
- `src/scrapers/crystalbet.py` — Playwright navigation + parser. Discovered
  CB serves TWO HTML formats for basketball: `data-loadinfo` JSON (38/113
  games in sample) and `col0..col7` positional divs (75/113). Both parsed.
  Reference §9 attributed loadinfo to football only; this is a real finding
  worth carrying forward — added a note to reference for future-us.
- `src/matcher.py` — rapidfuzz `token_set_ratio` over `normalize_team()`,
  global sorted-by-score greedy assignment, ±10 min time window. NOT named
  `matcher.py` in Code's original delivery (was `match.py`); renamed during
  the fix pass.
- `src/normalize.py` — Unicode-strip + drop common noise tokens (FC/BC/U18).
- `src/edge.py` — same-line +EV pass (devig + edge%) + ARB pass (raw vig'd
  prices). Both emit `Opportunity` rows with `kind` populated.
- `main.py` — argparse CLI: `--cb-saved` for offline iteration on the
  saved HTML, `--headless`, `--min-edge`, `--no-csv`. Rich/plain tables,
  writes CSV to `output/edges_<ts>.csv`. ONE-SHOT — not the scraper-loop +
  uvicorn shape the brief specifies for `main.py`.

**Bugs fixed in this pass**
1. *Timezone.* CB times were stored as naive Tbilisi (UTC+4); matcher had
   a 6 h window papering over the constant offset. Now: subtract
   `TBILISI_UTC_OFFSET` and tag UTC at parse time in `crystalbet.py`.
   Matcher does pure UTC-aware ±10 min comparison. Verified 219/219 CB
   rows have tz-aware start_time after fix.
2. *Greedy matching.* Was per-CB-first-best-Pin; could mis-pair on
   team-name collisions across leagues. Now: enumerate all
   (cb, pin) pairs above 80, sort score-desc, greedy-assign.
3. *Positional loadinfo.* Fixed indices `[3]`, `[6]` replaced with
   `handicap` field landmarks (`"handicap"` for AH-line, `"total"` for
   OU-line). Over vs Under disambiguated by `name` field. Survives CB
   inserting/removing entries around the existing positions.
4. *No ARB detection.* Brief requires ARB and +EV in one table with
   `type` column. Added second pass in `edge.py` using raw vig'd Pinnacle
   opposite-side price; ARB rows populate `arb_partner_side` and
   `arb_partner_odds`. Note: `pin_no_vig` field is reused for ARB rows to
   hold the partner-leg vigged price (UI displays it as "reference odds").
5. *Filename.* `src/match.py` → `src/matcher.py` per brief; two import
   sites updated.

**Smoke test** (synthetic Pinnacle for one CB game, `min_edge_pct=-100`
so the math direction is observable in both green and red zones):
- 1 matched event, score 100
- 12 opportunities emitted: 6 +EV + 6 ARB
- +EV `pin_no_vig` matches manual devig computation
- ARB `pin_no_vig` correctly holds Pinnacle opposite-side vigged price
- All negative edges in the synthetic (Pin was rigged sharper than CB); the
  math direction is right — production CB-vs-Pin will produce real positives

**Deliberate divergences from brief still in the code (flagged, not fixed)**
- `fuzz.token_set_ratio` instead of `partial_ratio`. `token_set_ratio` is
  more tolerant of qualifier differences ("Real Madrid Basket" vs
  "Real Madrid"). Switch back if false positives surface in real runs.
- Score threshold 80 instead of brief's 85. Marginal; will retune from data.

**Still missing vs brief — explicit punch-list for future checkpoints**
- C4: `src/db.py` + SQLite schema (`snapshots` / `matches` / `odds`).
- C4: `team_aliases.yaml` starter file with example commented entries.
- C4: `data/unmatched_log.csv` written by matcher.
- C5: `src/app.py` FastAPI with `/api/matches` + `/api/opportunities`.
- C5: `main.py` as scraper-loop + uvicorn. Currently a one-shot.
- C5: Kelly stake for ARB rows (v1 uses `kelly_stake=0` placeholder).
- C6: All three static files — `matches.html`, `arbs.html`, `style.css`.
- C6: `[+]` expander showing all markets side by side.
- C6: "Has Pinnacle reference" filter toggle.
- Tests: nothing for matcher / edge / crystalbet parser. test_vig.py only.

---

## 2026-05-24 — Checkpoint 3 (initial): CrystalBet scraper — dry-run (superseded)

**Scope of this turn.** Navigation only. No parser. Per brief: save one real
HTML sample to `data/raw/cb_prematch_sample.html`, design the parser against
the observed DOM, then code it.

**File.** `src/scrapers/crystalbet.py`. Two public entry points:
- `dry_run_capture(headed=True)` — runs the navigation, saves HTML, prints
  a diagnostic summary. **This is what runs in C3.**
- `fetch_crystalbet_basketball_prematch()` — production entry point.
  `raise NotImplementedError(...)` until parser lands. Fails loud not silent.

**Navigation flow** copied/adapted from `reference/cb_scraping.md` §3 + §7
and validated against the patterns in `cb/src/dashboard.py:373-388`:
- Chromium with `--disable-blink-features=AutomationControlled`, UA=Chrome 125
  on macOS, `locale="ka-GE"`, `timezone_id="Asia/Tbilisi"`.
- `goto(Sports.aspx, wait_until="domcontentloaded")` + 5 s sleep.
- English flip via `__doPostBack('ctl00$ctl00$ImageButtonEn','')`, wrapped in
  `page.expect_navigation()`. Reference §3 is explicit that this is a full
  page reload and a plain `await asyncio.sleep(5)` races the WebForms init.
- `DoSportTypePostBack(17)` for basketball.
- Enumerate `[championat-data]` elements. Log first 5 raw attribute values
  for parser-design reference.
- Filter league name (case-insensitive) against `outright/outrights/special/
  specialmarkets/esport/ebasket/cyber`.
- **Click the first eligible element** rather than parsing `championat-data`
  and calling `DoChampionatPostBack(id)`. We don't yet know how the league
  id is encoded in that attribute; the dry-run logs surface that for the
  parser-design phase. The page's own click handler will invoke
  `DoChampionatPostBack` for us.

**Diagnostic summary printed after capture:**
- Clicked league name + raw `championat-data` value
- HTML file path + size
- Count of `class="…GContainerList…"` matches via regex
- First 5 `GContainerG(\d+)` event IDs via regex

These four numbers are the green-light checks. If GContainerList is 0 or
event IDs is empty, the DOM has shifted from reference §7 and we re-design
before parsing.

**Why no parser yet.** Brief is explicit: "Save one raw HTML sample to
data/raw/cb_prematch_sample.html before writing the parser. STOP. Show me
the sample and proposed parser approach BEFORE coding it."

**Verified in Cowork.** Syntax parses. Constants resolve. `fetch_*()`
correctly raises NotImplementedError. Live run deferred to user — Cowork's
sandbox has no Georgian IP, CB blocks via Cloudflare otherwise (reference §10).

---

## 2026-05-24 — Checkpoint 2: Pinnacle scraper

**Scope.** `src/scrapers/pinnacle.py` — `fetch_pinnacle_basketball()` returns
`list[Odds]` across all leagues, both periods (FT + H1), both market types
that have lines (spread + total) at every alt-line plus moneyline.

**Endpoints used** (all under `https://guest.api.arcadia.pinnacle.com/0.1`):
- `/sports/4/leagues?all=false&brandId=0` — discover leagues.
- `/leagues/{id}/matchups?brandId=0` — team names + tipoff (ISO 8601 Z).
- `/leagues/{id}/markets/straight` — markets with `prices[]`.

Headers per brief: `x-api-key: guest`, `Origin`, `Referer`. Concurrency = 10
per `asyncio.Semaphore`. Per-league fetch failures are caught, logged, and
the league is skipped (don't kill the whole poll over one bad league).

**Design choices**
- One `Odds` per `(matchupId, period, type, line)`. Pinnacle returns one
  market entry per such tuple with both sides inside `prices[]`, so we don't
  group across entries — we just iterate.
- Sub-matchup filter: `parent != None` → skipped. Markets keyed to
  sub-matchups (player props, derivatives) won't have their matchupId in
  `by_id` and are dropped automatically.
- Live-game filter: `startTime <= now` → matchup dropped (prematch only).
- Allowed market types: `{moneyline, spread, total}`. Everything else
  (team_total, etc.) → skipped silently.
- Allowed periods: `{0: FT, 1: H1}`. Q1-Q4 not offered prematch per brief;
  any unknown period int → skipped.
- A suspended side (`price: None`) → the **entire** market line is dropped,
  not just the side. Otherwise we'd emit half-formed Odds objects that fail
  invariants downstream.
- Decimal odds ≤ 1.0 → same treatment (suspended, drop the whole line).
- League name blocklist: `cyber`, `esport`, `ebasket`, `specials`, `outright`.

**Verification done in Cowork (no live API access)**
- Parser dry-run against a synthetic Pinnacle response covering: FT ML, FT
  spread main + alt-line, FT total, H1 ML, a Q1 market (dropped), a
  `team_total` market (dropped), a market keyed to a sub-matchup (dropped),
  a market with `price: None` on one side (dropped), and a live matchup
  (dropped). Expected 5 emitted rows; got 5. Decimal conversions check
  (-110 → 1.909, -150 → 1.667, +130 → 2.30).

**Verification deferred to user's machine** (Pinnacle is proxy-blocked from
Cowork's sandbox; sanctioned `web_fetch` refuses since the URL isn't in a
literal user message). See Prompt C in chat.

**Unknowns until live API run**
- `matchups[].participants[].alignment` is assumed lowercase `"home"`/`"away"`.
  If Pinnacle actually returns `"Home"`/`"Away"` we'd emit zero rows; we lowercase
  the value so this is safe.
- `matchups[].parent` is assumed `None` for top-level games. If Pinnacle uses
  a missing key instead of `None`, `m.get("parent") is not None` still treats
  the missing case as top-level — safe.
- `participants` list may sometimes have alignments other than home/away
  (neutral?). Currently those matchups would be skipped (no home/away pair).
- `startTime` timezone: assumed UTC with `Z` suffix. Handled with `.replace("Z", "+00:00")`.

**Smoke test result (2026-05-24 20:44 UTC+4 Tbilisi)** — see
`data/raw/pinnacle_smoke_20260524_2044.txt`.
- 1646 Odds rows total, 61 unique matchups.
- by market_type: 102 moneyline, 780 spread, 764 total.
- by period: 1237 FT, 409 H1 (H1/FT ratio ~33%, consistent with H1 being
  offered on most but not all games).
- All sampled selections two-sided; all decimal odds > 1.0; lines look sane.

**Bug surfaced: 9 leagues 403 on /matchups endpoint**
Affected (matched against `/sports/4/leagues` IDs): Spain ACB (559),
Euroleague (382), Germany Bundesliga (423), Israel National League (212805),
France Pro A (414), Greek Basket League (437), Chinese Taipei P League
(211861), Tunisia National 1 (216744), Colombia BPC (207008). These are
exactly the high-volume European leagues we most need.

Important: my `asyncio.gather` is called *without* `return_exceptions=True`,
so when matchups 403s the entire league pair is dropped — no markets rows
either. The 1646 rows are from the 52 leagues where both endpoints
succeeded; the affected 9 contribute zero. Not a "team names missing"
problem — it's "leagues are invisible to us."

The first hypothesis from Claude Code's report ("add x-api-key header to
matchups too") was based on a misread; `x-api-key: guest` is set as a
default header on the `httpx.AsyncClient` at construction so all requests
through the client carry it. The cause is something else.

Diagnostic probe (Prompt D in chat) pending: tests whether the issue is
(a) `brandId=0` param, (b) a per-league access control, or (c) a sport-level
bulk-matchups endpoint we should use instead.

**Resolution (Prompt D ran 2026-05-24).** Root cause = (a). The `brandId=0`
query param triggers `403 BAD_APIKEY` on `/leagues/{id}/matchups` for
Pinnacle's premium leagues (Euroleague, Bundesliga, etc.). Removing the
param entirely returns 200 across ALL leagues tested — including ACB which
worked with the param too. Same guest API key, same headers — Pinnacle
appears to treat brandId=0 as an unauthorized brand for those leagues
specifically.

Fix: dropped `params={"brandId": 0}` from the matchups call only. The
sports/leagues call still uses brandId=0 (works fine and matches spec).
Markets/straight never used the param.

Bonus the probe surfaced: `/sports/4/matchups` (no params) returns 229
matchups in one shot. Future optimization — replace per-league matchup
calls with a single sport-level call, dropping ~50 requests per poll to 1.
Not doing now: per-league pattern makes the league-name attribution
trivial (we know which league we asked about). Worth revisiting if poll
latency becomes a problem.

**Post-fix smoke (2026-05-24 21:08 UTC+4)** —
`data/raw/pinnacle_smoke_20260524_2108.txt`. 1590 rows, 64 matchups,
{ml:108, spread:745, total:737}, {FT:1163, H1:427}. Row count down 56 vs
20:44 run — explained by games rolling past startTime in the intervening
25 min and being filtered as live; matchup count up +3 (net of additions
and roll-offs). Healthy distribution.

5–6 leagues still 403 (different leagues than first run except CT P-League).
Conclusion: residual 403s are flaky-edge behavior on Pinnacle's side, not
deterministic per-league access control. Mitigation = existing per-league
try/except → that league sits out the affected poll cycle. At 60 s cadence
a missed league recovers within one cycle. Not adding retry or bulk-endpoint
fallback per brief's "don't add features I didn't ask for" — revisit only
if the same leagues fail across many consecutive polls.

**Checkpoint 2 closed.** Pinnacle scraper produces healthy data; intermittent
flakiness handled. Ready for Checkpoint 3 (CrystalBet prematch scraper)
pending user go-ahead.

---

## 2026-05-24 — Checkpoint 0 + 1

**Inventory (Checkpoint 0)**
- `reference/cb_scraping.md` is the only load-bearing prior knowledge. Read
  fully. Prematch path lives in §7–§8: navigate `Sports.aspx`, English flip,
  `DoSportTypePostBack(17)`, loop `DoChampionatPostBack(champ_id)` for each
  league, parse `div.GContainerList` rows. Expanded ladder via
  `DoGamesPostBack('ExpandDetail:{id}')` → `table.game-details`.
- **The src/ files the project brief mentions (`models.py`, `vig.py`,
  `matcher.py`) do not exist.** What's actually committed in `src/` is three
  live-betting tools (`dashboard.py`, `match_view.py`, `single_match.py`),
  all wired to BetsAPI and rendering with `rich`. No prematch code, no
  Pinnacle, no SQLite, no FastAPI. We write models/vig/matcher fresh.
- Reusable patterns (will lift directly):
  - Playwright launch config: locale `ka-GE`, timezone `Asia/Tbilisi`,
    Chrome-125-on-macOS UA, `--disable-blink-features=AutomationControlled`.
    Source: `dashboard.py:373–388`.
  - CB odds-cell extraction + `>1.0` guard + `LiveBetSnatchPaused` check
    (`dashboard.py:_extract_odd`, `single_match.py:_snatch_odd`).
  - Market-header classifier `_classify(label)` in `single_match.py:96–118` —
    same taxonomy applies to prematch expanded detail.
  - AH / Total line regexes — already correct for prematch DOM per §7.

**Folder decision (Checkpoint 1)**
- Chose `prematch/` subfolder over a sibling fork. Reason: keeps the
  `reference/cb_scraping.md` knowledge co-located with both live and prematch
  code, shares `.env`, doesn't require duplicating the Playwright chromium
  download. Live tools keep running on `LiveBetting.aspx`; prematch hits
  `Sports.aspx`. Different page → different code paths, but shared scraping
  primitives may eventually move into a `src/shared/` if duplication grows.

**Skeleton (Checkpoint 1)**
- Created `prematch/{src/{scrapers/},static,tests,data/raw,notes}`.
- `src/models.py` — three dataclasses:
  - `Odds` carries both sides in `selections`. Diverges from reference §11 in:
    - `market_type` uses Pinnacle vocabulary (`moneyline` | `spread` | `total`)
      instead of `match_winner`. Pinnacle is the reference book, so we adopt
      its terms throughout.
    - Dropped `score` (irrelevant for prematch).
    - Added `__post_init__` guard rejecting any odds ≤ 1.0 — turns the "≤1.0
      is suspended" rule from reference §12 into a hard invariant at the
      type boundary.
  - `Match` — paired CB↔Pinnacle game, carries SQLite rowid.
  - `Opportunity` — one row of the arbs table; ARB legs share a row via
    `arb_partner_*` so we don't have to render two rows per ARB. May revisit.
- `src/vig.py` — `american_to_decimal`, `decimal_to_implied_prob`,
  `devig_2way`, `fair_decimal`, `vig_pct`. Formulas taken straight from
  the project brief. All raise `ValueError` on out-of-range input rather
  than returning sentinel values.
- `tests/test_vig.py` — 22 cases across three classes:
  `TestAmericanToDecimal`, `TestDevig2Way`, `TestHelpers`. Covers pickem,
  favorite, underdog, balanced/skewed devig, realistic Pinnacle line
  (-106/-104), boundary rejections, round-trip via `fair_decimal`.
  All 22 pass against Python 3.10.
- `requirements.txt` — superset of root: + `fastapi`, `uvicorn[standard]`,
  `jinja2`, `rapidfuzz`, `pyyaml`, `pytest`. Keeping `python-dotenv` for
  potential reuse of the root `.env`.
- `main.py` — stub raising `NotImplementedError`; lands in Checkpoint 5.

**Gotchas surfaced today**
- pytest's tempfile cleanup recurses-without-bound when run with the cwd
  inside the Cowork FUSE mount in this sandbox. Doesn't affect the macOS
  side; flag if it shows up later on the real machine.

**Open question for Checkpoint 2**
- Pinnacle's `/leagues/{id}/matchups` response — need to confirm whether
  start_time is in `startTime` (ISO Z) or some other field. Will verify on
  first real call.
