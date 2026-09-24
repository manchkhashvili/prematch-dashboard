/* Cross-page alert poller (Phase 3.12, 2026-05-27).
 *
 * Runs on every dashboard page. Polls /api/opportunities, plays a buzzy
 * 2.5-second alarm whenever a NEW opportunity at-or-above the user's
 * configured threshold appears.
 *
 * Behavior contract:
 *   - The alert ONLY fires for NEW opportunities, never for ones we've
 *     already seen in any prior poll on any prior page. Seen-keys persist
 *     in localStorage (arbs_seen_alerts_v1) so navigating between pages
 *     does NOT re-trigger sound.
 *   - On the very first dashboard visit (no seed marker in localStorage)
 *     we SEED the seen-set silently — every current opportunity is marked
 *     seen WITHOUT playing. Subsequent polls fire on newly-emerged opps.
 *   - The "Alert enabled" checkbox and threshold input still live on
 *     /arbs.html — they write `alert_enabled` and `alert_threshold` to
 *     localStorage. This poller reads from there.
 *
 * Why a shared file rather than per-page: the user wants the bip to fire
 * regardless of which tab they're on. The fastest path is one script
 * included by every page; the alternative (a Service Worker) is overkill
 * for a single-user local dashboard.
 *
 * To enable alerts on a new page: include /alerts.js in <head>.
 */
(function () {
  "use strict";

  const POLL_MS = 30_000;
  // Collapse guard (2026-08-03). If a poll returns fewer than COLLAPSE_RATIO of
  // the keys we were tracking, treat it as a feed glitch rather than genuine
  // expiry and do NOT prune the seen-set — otherwise the recovery poll re-alerts
  // on everything. MIN_KEYS_FOR_PRUNE keeps the guard from tripping on tiny
  // boards where 2 → 1 rows is a normal change.
  const COLLAPSE_RATIO = 0.5;
  const MIN_KEYS_FOR_PRUNE = 4;
  const ENABLED_KEY   = "alert_enabled";
  const THRESHOLD_KEY = "alert_threshold";
  // 2026-08-12: the opportunity alert grew from "enabled + edge%" to a full
  // gate set, configured by the panel on /arbs.html. Every key below is written
  // there and read here, and nothing at runtime complains if the two drift —
  // tests/test_arb_alerts_wiring.py pins them in both directions.
  //
  // Semantics, deliberately different from the anomaly alerts above: those fire
  // when ANY filled criterion is met (casting a net), these require ALL of them
  // (narrowing one). A blank box is ignored; an empty chip selection means
  // "all", so a sport or book added to the backend later keeps alerting instead
  // of silently dropping out.
  // Layers: a JSON list of gate-sets, ORed. Present since 2026-08-16; when
  // absent the flat keys below are read as a single layer instead.
  const A_LAYERS    = "arb_alert_layers";
  const A_PP        = "arb_alert_pp";              // min edge in probability points
  const A_STEP      = "arb_alert_step";            // re-alert only after +N pp
  const A_ODDS_MIN  = "arb_alert_odds_min";
  const A_ODDS_MAX  = "arb_alert_odds_max";
  const A_KELLY_MIN = "arb_alert_kelly_min";
  const A_KELLY_MAX = "arb_alert_kelly_max";
  const A_PINMAX    = "arb_alert_pin_max_stake";
  const A_LEAD_MIN  = "arb_alert_lead_min";        // minutes to kickoff, min
  const A_LEAD_MAX  = "arb_alert_lead_max_h";      // hours to kickoff, max
  const A_KINDS     = "arb_alert_kinds";
  const A_CONF      = "arb_alert_conf";
  const A_SPORTS    = "arb_alert_sports";
  const A_BOOKS     = "arb_alert_books";
  const A_MARKETS   = "arb_alert_markets";
  const A_PERIODS   = "arb_alert_periods";
  // Was hardcoded; now the default when the box is blank.
  const DEFAULT_RE_ALERT_PP = 5;
  const SEEN_KEY      = "arbs_seen_alerts_v1";
  const SEEDED_KEY    = "arbs_alerts_seeded";
  // Phase 3.13: user-muted bet-keys live in localStorage under this name.
  // Written by arbs.html when the user clicks the − button on a row. We read
  // it fresh every poll so toggling a mute takes effect by the next cycle.
  const MUTED_KEY     = "arbs_user_muted_v1";

  // Phase 5.2: separate alert path for Pinnacle line moves (Top Moves page).
  // Distinct sound + own enable/threshold so the user can tell a steam move
  // apart from a +EV opportunity by ear.
  const MOVE_ENABLED_KEY   = "move_alert_enabled";
  const MOVE_THRESHOLD_KEY = "move_alert_threshold";   // min Δpp to alert
  const MOVE_SEEN_KEY      = "moves_seen_alerts_v1";
  const MOVE_SEEDED_KEY    = "moves_alerts_seeded";

  // Phase 6.6: third alert — a distinct chime each time the anomaly scan
  // refreshes (a new full-detail basketball scan completed). Heads-up that the
  // Anomalies tab now has fresh data. Toggle lives on /anomalies.html; default
  // ON since it's an opt-in feature the user requested. We track the last-seen
  // scan timestamp so we chime once per new scan (every ~30 min), not per poll.
  const SCAN_ENABLED_KEY = "anomaly_scan_alert_enabled";
  const SCAN_SEEN_TS_KEY = "anomaly_scan_seen_ts";

  // 2026-08-10: CONTENT-based anomaly alerts, as opposed to SCAN_ENABLED_KEY
  // above which chimes on every refresh regardless of what was found. Two
  // independent paths with their own sounds, because a ladder violation is
  // bettable (family A, the detector we trust most) while a consistency flag is
  // diagnostic — worth telling apart by ear without looking at the screen.
  // Config is written by the panel on /anomalies.html; these key names MUST
  // mirror the constants there (tests/test_anomaly_alerts_wiring.py pins that).
  //
  // TWO BOARDS, one implementation (2026-09-24). The Anomalies tab and the New
  // inconsistencies tab each get their own thresholds and their own seen-map,
  // keyed by a prefix:
  //
  //   "anom_"  /api/anomalies/alerts             every book, every sport
  //   "lsp_"   /api/new_inconsistencies/alerts   CrystalBet's LSport feed
  //
  // Separate seen-maps are not a nicety. The two boards share event ids and
  // check kinds, so ladderKey/consKey collide across them — one shared map
  // would let whichever board polled first mark a row seen and mute the
  // other's chime for good.
  //
  // Every name is the prefix plus a fixed suffix, and /alert-panel.js builds
  // the same names from the same suffixes, so "anom_" reproduces exactly what
  // shipped in 2026-08 and no saved setting is orphaned.
  function anomKeys(p) {
    return {
      LAD_ON:      p + "ladder_alert_enabled",
      LAD_PCT:     p + "ladder_alert_pct",
      LAD_DELTA:   p + "ladder_alert_delta",
      LAD_STEP:    p + "ladder_alert_step",
      CONS_ON:     p + "cons_alert_enabled",
      CONS_DEF:    p + "cons_alert_default",
      CONS_KINDS:  p + "cons_alert_kinds",
      // Odds vetoes (2026-08-27). Written by the same panel; see the note
      // there. These are NOT another way to fire — they gate everything else,
      // because a longshot flag is noise no matter how large its severity is.
      LAD_MAXODDS:  p + "ladder_alert_max_odds",
      LAD_MINODDS:  p + "ladder_alert_min_odds",
      CONS_MAXODDS: p + "cons_alert_max_odds",
      CONS_MINODDS: p + "cons_alert_min_odds",
      LAD_SEEN_KEY:    p + "ladder_seen_v1",
      LAD_SEEDED_KEY:  p + "ladder_seeded",
      CONS_SEEN_KEY:   p + "cons_seen_v1",
      CONS_SEEDED_KEY: p + "cons_seeded",
      SOUND_LAD:  p + "ladder",
      SOUND_CONS: p + "cons",
    };
  }
  const ANOM = anomKeys("anom_");
  const LSP  = anomKeys("lsp_");
  // The original board's names, unchanged, so the rest of this file and the
  // wiring tests keep reading the same identifiers.
  const LAD_ON = ANOM.LAD_ON, LAD_PCT = ANOM.LAD_PCT, LAD_DELTA = ANOM.LAD_DELTA,
        LAD_STEP = ANOM.LAD_STEP, CONS_ON = ANOM.CONS_ON, CONS_DEF = ANOM.CONS_DEF,
        CONS_KINDS = ANOM.CONS_KINDS, LAD_MAXODDS = ANOM.LAD_MAXODDS,
        LAD_MINODDS = ANOM.LAD_MINODDS, CONS_MAXODDS = ANOM.CONS_MAXODDS,
        CONS_MINODDS = ANOM.CONS_MINODDS, LAD_SEEN_KEY = ANOM.LAD_SEEN_KEY,
        LAD_SEEDED_KEY = ANOM.LAD_SEEDED_KEY, CONS_SEEN_KEY = ANOM.CONS_SEEN_KEY,
        CONS_SEEDED_KEY = ANOM.CONS_SEEDED_KEY;
  // Re-alert when a finding gets materially worse, not on every poll it stays
  // above the bar. Deliberately RELATIVE: consistency severity is pp for some
  // checks, points for others and % for the HT/FT ones, so a fixed "+5" step
  // would mean wildly different things per check. 1.5x is unit-free.
  const RE_ALERT_FACTOR = 1.5;

  // ── Persistence ────────────────────────────────────────────────────────
  function loadSeen() {
    try {
      const raw = localStorage.getItem(SEEN_KEY);
      if (!raw) return new Map();
      return new Map(JSON.parse(raw));
    } catch (e) { return new Map(); }
  }
  function saveSeen(map) {
    try { localStorage.setItem(SEEN_KEY, JSON.stringify([...map])); }
    catch (e) {}
  }
  let seenKeys = loadSeen();

  // ── Cross-tab sound claim ──────────────────────────────────────────────
  // Every open tab runs this poller with its OWN in-memory seen-set, so when a
  // new opportunity appears each tab independently decides it is new and each
  // plays the 2.5 s alarm — N tabs, N overlapping alarms. Persisting the
  // seen-set does not fix it: the tabs poll at their own offsets and both read
  // "not seen" before either writes.
  //
  // localStorage has no compare-and-swap, but writes ARE synchronous and
  // ordered within an origin, so write-then-read-back is an effective claim:
  // if two tabs race, the last writer wins the key and the other reads back a
  // different tab id and stays silent. The residual race is the microseconds
  // between our own write and read-back, versus the seconds-wide window before.
  //
  // The claim is scoped per sound kind and expires quickly, so a tab closing
  // mid-claim cannot mute future alerts.
  const TAB_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const CLAIM_KEY_PREFIX = "alert_sound_claim_v1:";
  const CLAIM_WINDOW_MS = 6000;

  function claimSound(kind) {
    const key = CLAIM_KEY_PREFIX + kind;
    const now = Date.now();
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const prev = JSON.parse(raw);
        // A fresh claim by ANOTHER tab means it is already sounding for this
        // poll round — stay quiet. Our own claim does not block us (a genuinely
        // new alert in the next round should still fire).
        if (prev && prev.tab !== TAB_ID && now - prev.ts < CLAIM_WINDOW_MS) {
          return false;
        }
      }
      localStorage.setItem(key, JSON.stringify({ tab: TAB_ID, ts: now }));
      const after = JSON.parse(localStorage.getItem(key) || "null");
      return !after || after.tab === TAB_ID;    // lost the write race → silent
    } catch (e) {
      return true;      // localStorage unavailable → behave as before
    }
  }

  // ── Audio (lazy init on first user gesture) ────────────────────────────
  let audioCtx = null;
  function ensureAudio() {
    if (audioCtx) return audioCtx;
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      return audioCtx;
    } catch (e) { return null; }
  }
  // Browser autoplay policy requires a user gesture before AudioContext
  // can sound. Lazy-init on first click/keydown.
  document.addEventListener("click",   ensureAudio, { once: true });
  document.addEventListener("keydown", ensureAudio, { once: true });

  function playPing() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // Buzzy 2.5-second alarm — alternating two-tone square wave. User-requested
    // 2026-05-26 (was a gentle 300ms ping). Cuts through other audio.
    const now = ctx.currentTime;
    const duration = 2.5;
    const slotDur = 0.14;
    const tones = [740, 620];
    const master = ctx.createGain();
    master.gain.value = 0.10;
    master.connect(ctx.destination);
    let t = 0, i = 0;
    while (t < duration) {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = tones[i % 2];
      env.gain.setValueAtTime(0.0001, now + t);
      env.gain.exponentialRampToValueAtTime(1.0,    now + t + 0.005);
      env.gain.exponentialRampToValueAtTime(0.0001, now + t + slotDur - 0.005);
      osc.connect(env).connect(master);
      osc.start(now + t);
      osc.stop(now + t + slotDur);
      t += slotDur;
      i++;
    }
  }

  function playMoveChime() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // DISTINCT from the +EV/ARB buzzer: a clean ascending 3-note arpeggio
    // (C5–E5–G5, a major triad) on a sine wave. Bright + pleasant rather than
    // urgent — "a line moved" is informational, not act-now-or-lose-it. ~0.6s.
    const now = ctx.currentTime;
    const notes = [523.25, 659.25, 783.99];  // C5, E5, G5
    const noteDur = 0.16;
    const master = ctx.createGain();
    master.gain.value = 0.16;
    master.connect(ctx.destination);
    notes.forEach((freq, i) => {
      const t = now + i * noteDur;
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0.0001, t);
      env.gain.exponentialRampToValueAtTime(1.0, t + 0.01);
      env.gain.exponentialRampToValueAtTime(0.0001, t + noteDur - 0.01);
      osc.connect(env).connect(master);
      osc.start(t);
      osc.stop(t + noteDur);
    });
  }

  function playScanChime() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // DISTINCT from the other two: a calm DESCENDING two-note "done" tone
    // (G5→C5) on a triangle wave, ~0.4s. Neither the urgent square buzzer nor
    // the bright ascending move-triad — reads as "a refresh landed".
    const now = ctx.currentTime;
    const notes = [783.99, 523.25];  // G5 → C5
    const noteDur = 0.20;
    const master = ctx.createGain();
    master.gain.value = 0.16;
    master.connect(ctx.destination);
    notes.forEach((freq, i) => {
      const t = now + i * noteDur;
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "triangle";
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0.0001, t);
      env.gain.exponentialRampToValueAtTime(1.0, t + 0.01);
      env.gain.exponentialRampToValueAtTime(0.0001, t + noteDur - 0.01);
      osc.connect(env).connect(master);
      osc.start(t);
      osc.stop(t + noteDur);
    });
  }

  function playLadderAlert() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // DISTINCT from the other three: a fast DESCENDING sawtooth run — reads as
    // "warning", and nothing else here uses a sawtooth. A ladder violation is
    // the most trustworthy thing we flag, so it gets a sound with some bite,
    // but far shorter than the 2.5s +EV buzzer.
    //
    // 2026-09-08: it was inaudible in practice, and raw gain was the least of
    // the reasons. Every note was a pure pluck — instant attack, then an
    // exponential decay across the whole 0.10s slot — so almost no energy
    // survived the transient; the entire run was over in 0.4s; and 0.13 master
    // put a sound that already had no sustain another ~8 dB down. Now each
    // note HOLDS at full level for most of its slot before a short release,
    // the slot is slightly longer, and the run repeats once: ~1.1s total.
    // Still under half the +EV buzzer's length, still unmistakably not it.
    const now = ctx.currentTime;
    const notes = [880, 740, 622, 523, 880, 740, 622, 523];
    const noteDur = 0.135;
    const rel = 0.035;                  // release tail inside each slot
    const master = ctx.createGain();
    master.gain.value = 0.30;
    master.connect(ctx.destination);
    notes.forEach((freq, i) => {
      const t = now + i * noteDur;
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "sawtooth";
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0.0001, t);
      env.gain.exponentialRampToValueAtTime(1.0, t + 0.008);
      // Anchor the held level: without this the release below would ramp from
      // the attack's end and decay across the entire slot, i.e. a pluck again.
      env.gain.setValueAtTime(1.0, t + noteDur - rel);
      env.gain.exponentialRampToValueAtTime(0.0001, t + noteDur);
      osc.connect(env).connect(master);
      osc.start(t);
      osc.stop(t + noteDur);
    });
  }

  function playConsistencyAlert() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // Deliberately the SOFTEST of the four: a sine with a downward pitch bend.
    // A consistency flag is diagnostic — "go look" — so it should register
    // without demanding attention the way the ladder run or the +EV buzzer do.
    //
    // 2026-09-08: soft had become inaudible. The bend ENDED at 294 Hz, which
    // laptop speakers barely reproduce, on a pure sine with no harmonics to
    // carry it, at 0.11 master, decaying from the moment it started. Four
    // changes, none of which touch its character: the bend moved up a fifth to
    // 660→440 Hz where small speakers actually work; a quiet octave partial
    // gives the sine some body; the note holds before releasing instead of
    // decaying throughout; and the master came up. Same soft two-tone fall,
    // now audible from across a room — and still the gentlest thing here.
    const now = ctx.currentTime;
    const dur = 0.45;
    const rel = 0.12;
    const master = ctx.createGain();
    master.gain.value = 0.26;
    master.connect(ctx.destination);
    const env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, now);
    env.gain.exponentialRampToValueAtTime(1.0, now + 0.02);
    env.gain.setValueAtTime(1.0, now + dur - rel);   // hold, then release
    env.gain.exponentialRampToValueAtTime(0.0001, now + dur);
    env.connect(master);
    // Fundamental plus a quiet octave. The partial is what makes a sine
    // carry on a small speaker; at 0.28 it adds presence, not harshness.
    [[1, 1.0], [2, 0.28]].forEach(function (pair) {
      const mult = pair[0], level = pair[1];
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      g.gain.value = level;
      osc.type = "sine";
      osc.frequency.setValueAtTime(660 * mult, now);
      osc.frequency.exponentialRampToValueAtTime(440 * mult, now + dur);
      osc.connect(g).connect(env);
      osc.start(now);
      osc.stop(now + dur);
    });
  }

  function loadMoveSeen() {
    try {
      const raw = localStorage.getItem(MOVE_SEEN_KEY);
      if (!raw) return new Set();
      return new Set(JSON.parse(raw));
    } catch (e) { return new Set(); }
  }
  function saveMoveSeen(set) {
    try { localStorage.setItem(MOVE_SEEN_KEY, JSON.stringify([...set])); }
    catch (e) {}
  }
  let moveSeen = loadMoveSeen();

  // Move identity for dedup. recorded_at is stamped once per Pinnacle cycle,
  // so all moves from one cycle share it — two 30s alert-polls within one
  // 60s Pin cycle see identical keys (no double-beep). A genuinely new move
  // next cycle gets a fresh recorded_at → new key → can alert.
  function moveKey(m) {
    return `${m.sport}|${m.match_label}|${m.market}|${m.side}|${m.recorded_at}`;
  }

  // ── Key for de-duping alerts across polls ──────────────────────────────
  function alertKey(o) {
    // Prefer event id; fall back to sport+label so rows without an event id
    // (rare; off-platform synthetic) don't all collide into the empty bucket.
    if (o.cb_event_id) {
      return `eid|${o.cb_event_id}|${o.market}|${o.side}|${o.kind}`;
    }
    return `lbl|${o.sport || ""}|${o.match_label}|${o.market}|${o.side}|${o.kind}`;
  }

  // ── Bet-key (matches arbs.html's mute/highlight storage shape) ─────────
  // Phase 3.13: must EXACTLY mirror betKey() in arbs.html so a row the user
  // muted there gets matched here. Different from alertKey() above, which
  // dedupes alerts; betKey identifies the underlying market for user flags.
  function betKey(o) {
    return [
      o.cb_event_id || "",
      o.market_type || "",
      o.period || "",
      o.line == null ? "" : o.line,
      o.side || "",
      o.submarket || "",
      o.team_side || "",
    ].join("|");
  }

  // ── Opportunity gates ───────────────────────────────────────────────────
  // Read fresh every poll so a change in the panel takes effect next cycle.
  function cfgSet(key) {
    // null (absent / "" / empty array) means "all" — see the note on the key
    // constants. Anything else is the allowed set.
    let raw;
    try { raw = localStorage.getItem(key); } catch (e) { return null; }
    if (raw === null || String(raw).trim() === "") return null;
    try {
      const arr = JSON.parse(raw);
      return (Array.isArray(arr) && arr.length) ? new Set(arr) : null;
    } catch (e) { return null; }
  }

  function readOppGates() {
    return {
      edge:     cfgNum(THRESHOLD_KEY),
      pp:       cfgNum(A_PP),
      step:     cfgNum(A_STEP),
      oddsMin:  cfgNum(A_ODDS_MIN),
      oddsMax:  cfgNum(A_ODDS_MAX),
      kellyMin: cfgNum(A_KELLY_MIN),
      kellyMax: cfgNum(A_KELLY_MAX),
      pinMax:   cfgNum(A_PINMAX),
      leadMin:  cfgNum(A_LEAD_MIN),
      leadMaxH: cfgNum(A_LEAD_MAX),
      kinds:    cfgSet(A_KINDS),
      conf:     cfgSet(A_CONF),
      sports:   cfgSet(A_SPORTS),
      books:    cfgSet(A_BOOKS),
      markets:  cfgSet(A_MARKETS),
      periods:  cfgSet(A_PERIODS),
    };
  }

  /* ── Alert LAYERS (2026-08-16) ──────────────────────────────────────────
   *
   * A single gate-set cannot express "10 %+ at odds 1–3 but 15 %+ at odds 4–5":
   * within a gate-set every criterion is ANDed, and those two rules contradict
   * each other. So the panel stores a LIST of gate-sets and a row alerts if it
   * clears ANY of them — AND within a layer, OR across layers.
   *
   * Stored shape: [{name, on, g:{edge, pp, step, oddsMin, …, kinds:[…] | null}}]
   * with null meaning "off" for a number and "all" for a selection, matching
   * what readOppGates() produces.
   */
  function normaliseGates(g) {
    const n = v => {
      if (v === null || v === undefined || String(v).trim() === "") return null;
      const x = Number(v);
      return isNaN(x) ? null : x;
    };
    const s = v => (Array.isArray(v) && v.length) ? new Set(v) : null;
    return {
      edge: n(g.edge), pp: n(g.pp), step: n(g.step),
      oddsMin: n(g.oddsMin), oddsMax: n(g.oddsMax),
      kellyMin: n(g.kellyMin), kellyMax: n(g.kellyMax),
      pinMax: n(g.pinMax), leadMin: n(g.leadMin), leadMaxH: n(g.leadMaxH),
      kinds: s(g.kinds), conf: s(g.conf), sports: s(g.sports),
      books: s(g.books), markets: s(g.markets), periods: s(g.periods),
    };
  }

  // Active layers, newest format first. Falls back to the pre-layers flat keys
  // as a SINGLE layer — that is what runs in a browser which has not opened
  // arbs.html since the upgrade, so an existing configuration is never silently
  // downgraded to "fires on everything".
  function readLayers() {
    let raw = null;
    try { raw = localStorage.getItem(A_LAYERS); } catch (e) {}
    if (raw) {
      try {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr) && arr.length) {
          return arr
            .filter(l => l && l.on !== false)
            .map((l, i) => ({ name: String(l.name || `Layer ${i + 1}`),
                              g: normaliseGates(l.g || {}) }));
        }
      } catch (e) { /* fall through */ }
    }
    return [{ name: "Layer 1", g: readOppGates() }];
  }

  // /api/opportunities defaults to min_edge=1, so a layer asking about edges
  // BELOW 1 % has to widen the query or the rows it wants never reach us — the
  // panel would look configured and simply never fire. With layers the floor is
  // the most generous one any active layer needs.
  function oppQueryFloor(layers) {
    let lowest = null;
    for (const L of layers) {
      if (L.g.edge === null) return 1;      // no edge gate → 1 is already as wide as we go
      lowest = (lowest === null) ? L.g.edge : Math.min(lowest, L.g.edge);
    }
    if (lowest === null) return 1;
    return lowest < 1 ? Math.max(0, lowest) : 1;
  }

  // Which layers does this row clear? Confidence lives here rather than in
  // passesGates because it owns the "unset means exclude weak" default, and it
  // is per-layer: a longshot band may want weak rows excluded while a
  // short-price band does not care.
  function matchingLayers(o, layers) {
    const conf = o.confidence || "medium";
    return layers.filter(L => {
      const confOk = L.g.conf ? L.g.conf.has(conf) : conf !== "weak";
      return confOk && passesGates(o, L.g);
    });
  }

  // How much better must an already-alerted edge get before it speaks again?
  // The MINIMUM across the layers that matched: under OR semantics, if any
  // layer wants to hear about a +2pp improvement, you hear about it.
  function effectiveStep(matched) {
    let best = null;
    for (const L of matched) {
      const s = L.g.step === null ? DEFAULT_RE_ALERT_PP : L.g.step;
      best = (best === null) ? s : Math.min(best, s);
    }
    return best === null ? DEFAULT_RE_ALERT_PP : best;
  }

  // Edge in PROBABILITY POINTS rather than percent. edge_pct flatters long
  // prices — a 10.0 against an 8.0 fair reads "+25%" but is only 2.5pp, while a
  // 2.0 against a 1.9 fair reads "+5.3%" and is worth MORE at 2.6pp. Without
  // this the alert is loudest exactly where the fair price is least certain.
  function ppEdge(o) {
    const book = Number(o.cb_odds), fair = Number(o.pin_no_vig);
    if (!isFinite(book) || !isFinite(fair) || book <= 1 || fair <= 1) return null;
    return (1 / fair - 1 / book) * 100;
  }

  function minutesToKickoff(o) {
    if (!o.start_time) return null;
    const t = Date.parse(o.start_time);
    if (isNaN(t)) return null;
    return (t - Date.now()) / 60000;
  }

  // Does this row clear every configured gate? Blank/absent gates pass.
  function passesGates(o, g) {
    if (g.edge !== null && !(o.edge_pct >= g.edge)) return false;
    if (g.pp !== null) {
      const pp = ppEdge(o);
      if (pp === null || pp < g.pp) return false;
    }
    const odds = Number(o.cb_odds);
    if (g.oddsMin !== null && !(odds >= g.oddsMin)) return false;
    if (g.oddsMax !== null && !(odds <= g.oddsMax)) return false;
    // Kelly is 0 on ARB rows by design (edge.py leaves the split to the
    // bettor), so a Kelly floor would silence every arb. Only gate +EV.
    if (o.kind !== "ARB") {
      const kelly = Number(o.kelly_stake);
      if (g.kellyMin !== null && !(kelly >= g.kellyMin)) return false;
      if (g.kellyMax !== null && !(kelly <= g.kellyMax)) return false;
    }
    // A missing Pinnacle limit is unknown, not zero — do not fail the row on it.
    if (g.pinMax !== null && o.pin_max_stake != null
        && !(Number(o.pin_max_stake) >= g.pinMax)) return false;
    const mins = minutesToKickoff(o);
    if (mins !== null) {
      if (g.leadMin !== null && mins < g.leadMin) return false;
      if (g.leadMaxH !== null && mins > g.leadMaxH * 60) return false;
    }
    if (g.kinds && !g.kinds.has(o.kind)) return false;
    // Confidence is handled by the caller (it also owns the "unset means
    // exclude weak" default) — encoding it twice would let the two drift.
    if (g.sports && o.sport && !g.sports.has(o.sport)) return false;
    if (g.books && !g.books.has(o.book || "cb")) return false;
    if (g.markets && o.market_type && !g.markets.has(o.market_type)) return false;
    if (g.periods && o.period && !g.periods.has(o.period)) return false;
    return true;
  }

  function loadMutedSet() {
    try {
      const raw = localStorage.getItem(MUTED_KEY);
      if (!raw) return new Set();
      return new Set(JSON.parse(raw));
    } catch (e) { return new Set(); }
  }

  // ── Poll ────────────────────────────────────────────────────────────────
  async function poll() {
    // Alerts off — nothing to do this cycle. (But we do still seed the
    // seen-set silently the first time so toggling alerts on later doesn't
    // bip on opps that were already on screen.)
    const enabled = (localStorage.getItem(ENABLED_KEY) === "1");
    // One or many gate-sets; a row alerts if it clears ANY of them. The
    // re-alert step is per-layer now, so it is resolved per row below rather
    // than once here.
    const layers = readLayers();

    let opps;
    try {
      // Query at min_edge=1 (catch-all) so the seen-set tracks every opp
      // regardless of the current settings, and gate client-side just before
      // deciding whether to play.
      //
      // The one exception: an edge gate BELOW 1 % has to widen the query too,
      // or the row it is asking about never reaches us. That is easy to miss —
      // the panel would look configured and simply never fire, which is the
      // failure mode this whole file is careful about.
      const floor = oppQueryFloor(layers);
      const r = await fetch("/api/opportunities?min_edge=" + floor);
      if (!r.ok) return;
      opps = await r.json();
    } catch (e) { return; }

    const isFirstEverPoll = !localStorage.getItem(SEEDED_KEY);
    // Reload mutes every poll — picks up toggles from arbs.html immediately.
    const mutedKeys = loadMutedSet();
    const currentKeys = new Set();
    let newAtThreshold = 0;

    const firedBy = new Set();      // layer names, for the console line
    for (const o of opps) {
      const k = alertKey(o);
      currentKeys.add(k);
      // Which layers this row clears decides BOTH whether it may sound and how
      // much improvement re-arms it, so resolve them before the seen-set check.
      const matched = matchingLayers(o, layers);
      const reAlertPp = effectiveStep(matched);
      const prevEdge = seenKeys.get(k);
      const isNew = (prevEdge === undefined) || (o.edge_pct - prevEdge >= reAlertPp);
      if (isNew) seenKeys.set(k, o.edge_pct);
      // Phase 3.13: skip sound for opps the user muted on /arbs.html.
      // Still track them in seenKeys (so they don't fire when un-muted on
      // the same edge) but never count them toward newAtThreshold.
      const isMuted = mutedKeys.has(betKey(o));
      // `weak` pairings used to be excluded unconditionally (2026-07-26 audit:
      // big edges are overwhelmingly weak — every kickoff-misaligned row and 7
      // of the 8 edges above 20 % were weak, so the alert was loudest where it
      // was least reliable). That rule is now the DEFAULT of the confidence
      // chip group rather than a hard-coded law, so it can be inspected and
      // overridden; with no confidence selection saved we keep the old
      // behaviour exactly. It is applied per-layer inside matchingLayers().
      //
      // Rows still enter seenKeys even when no layer matches, so one that later
      // starts passing at the same edge doesn't fire retroactively.
      if (isNew && !isFirstEverPoll && enabled && !isMuted && matched.length) {
        newAtThreshold++;
        firedBy.add(matched[0].name);
      }
    }

    // Prune keys that no longer appear (game settled, line pulled, dropped
    // below min_edge=1). Keeps localStorage bounded — the dashboard runs
    // for hours; without pruning the map would grow indefinitely.
    //
    // BUT never prune on a COLLAPSE. The feed can briefly go empty or near-empty
    // (a reference-book fetch returning 0 rows blanks every opportunity for that
    // sport). Pruning then forgets every key, so when the list returns one poll
    // later EVERY row looks brand new and the alarm re-fires on opportunities
    // the user already dismissed — the exact "list disappears, fake alerts fire
    // again" bug. A real list never loses most of its rows in one 30 s poll, so
    // treat that as a feed glitch and keep the seen-set intact.
    const collapsed = seenKeys.size >= MIN_KEYS_FOR_PRUNE
      && currentKeys.size < seenKeys.size * COLLAPSE_RATIO;
    if (!collapsed) {
      for (const k of [...seenKeys.keys()]) {
        if (!currentKeys.has(k)) seenKeys.delete(k);
      }
    } else {
      try {
        console.warn(`[alert] feed collapsed ${seenKeys.size} → ${currentKeys.size} `
                     + `opportunities; keeping seen-set (no re-alert on recovery)`);
      } catch (e) {}
    }
    saveSeen(seenKeys);

    if (isFirstEverPoll) {
      // Mark seeded — current opps are now "known" without playing sound.
      // Next poll will fire on truly new opportunities only.
      try { localStorage.setItem(SEEDED_KEY, "1"); } catch (e) {}
      return;
    }

    if (newAtThreshold > 0) {
      if (claimSound("opps")) playPing();
      // Console hint for the dev console-watcher case.
      try {
        // Name the layer(s) — with several bands configured, "which rule fired"
        // is the first thing you want to know when the chime goes off.
        console.log(`[alert] ${newAtThreshold} new opportunity(ies) via `
                    + `${[...firedBy].join(", ") || "?"}`);
      } catch (e) {}
    }
  }

  // ── Move poll (Phase 5.2) ───────────────────────────────────────────────
  async function pollMoves() {
    const enabled = (localStorage.getItem(MOVE_ENABLED_KEY) === "1");
    // When move-alerts are off we skip the request entirely AND skip seeding,
    // so the first time the user turns them on we seed silently (no blast of
    // every current mover). The seed happens on the first poll-while-enabled.
    if (!enabled) return;

    let threshold;
    try {
      threshold = parseFloat(localStorage.getItem(MOVE_THRESHOLD_KEY) || "5");
      if (isNaN(threshold)) threshold = 5;
    } catch (e) { threshold = 5; }

    let moves;
    try {
      const r = await fetch("/api/moves?min_move=" + threshold);
      if (!r.ok) return;
      moves = await r.json();
    } catch (e) { return; }

    const isFirstEnabledPoll = !localStorage.getItem(MOVE_SEEDED_KEY);
    const currentKeys = new Set();
    let newMoves = 0;

    for (const m of moves) {
      const k = moveKey(m);
      currentKeys.add(k);
      if (!moveSeen.has(k)) {
        moveSeen.add(k);
        if (!isFirstEnabledPoll) newMoves++;
      }
    }

    // Prune keys no longer present (old cycles' moves drop off /api/moves).
    for (const k of [...moveSeen]) {
      if (!currentKeys.has(k)) moveSeen.delete(k);
    }
    saveMoveSeen(moveSeen);

    if (isFirstEnabledPoll) {
      try { localStorage.setItem(MOVE_SEEDED_KEY, "1"); } catch (e) {}
      return;
    }
    if (newMoves > 0) {
      if (claimSound("moves")) playMoveChime();
      try {
        console.log(`[move-alert] ${newMoves} new move(s) at ≥ ${threshold}pp`);
      } catch (e) {}
    }
  }

  // ── Scan-refresh poll (Phase 6.6) ───────────────────────────────────────
  // Chime once when the anomaly scan's computed_at changes. Uses the cheap
  // /api/anomalies/status heartbeat (no Pinnacle re-matching). Default ON; the
  // toggle on /anomalies.html writes SCAN_ENABLED_KEY. Seeds silently on the
  // first observation so a page-load doesn't chime on the existing scan.
  async function pollScan() {
    const raw = localStorage.getItem(SCAN_ENABLED_KEY);
    const enabled = (raw === null) ? true : (raw === "1");  // default ON
    if (!enabled) return;

    let status;
    try {
      const r = await fetch("/api/anomalies/status");
      if (!r.ok) return;
      status = await r.json();
    } catch (e) { return; }
    if (!status.enabled || !status.computed_at) return;

    let lastSeen = null;
    try { lastSeen = localStorage.getItem(SCAN_SEEN_TS_KEY); } catch (e) {}
    if (status.computed_at === lastSeen) return;          // no new scan

    try { localStorage.setItem(SCAN_SEEN_TS_KEY, status.computed_at); } catch (e) {}
    if (lastSeen === null) return;                        // seed silently first time

    if (claimSound("scan")) playScanChime();
    try {
      console.log(`[scan-alert] anomaly scan refreshed: ${status.anomalies} anomalies, `
                  + `${status.consistency} consistency flags`);
    } catch (e) {}
  }

  // ── Anomaly CONTENT alerts (2026-08-10) ─────────────────────────────────
  // Ladder violations and consistency flags, each gated on its own thresholds
  // from the Alert settings panel on /anomalies.html. Feeds off
  // /api/anomalies/alerts, which is the cheap sibling of /api/anomalies — no
  // Pinnacle re-matching, so this is safe to poll every 30s from every tab.

  function cfgNum(key) {
    // Blank/absent/garbage → null, meaning "this criterion is off". Explicitly
    // NOT 0: a 0 threshold would fire on every row, which is the opposite of
    // what leaving a box empty should mean.
    let raw;
    try { raw = localStorage.getItem(key); } catch (e) { return null; }
    if (raw === null || String(raw).trim() === "") return null;
    const n = Number(raw);
    return isNaN(n) ? null : n;
  }

  function loadSeenMap(key) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return new Map();
      return new Map(JSON.parse(raw));
    } catch (e) { return new Map(); }
  }
  function saveSeenMap(key, map) {
    try { localStorage.setItem(key, JSON.stringify([...map])); } catch (e) {}
  }

  /* Games the user muted from either findings board (2026-09-24).
   *
   * Written by AlertPanel.Mutes in /alert-panel.js; the key name MUST match
   * there — tests/test_anomaly_alerts_wiring.py pins it, because a typo would
   * leave the button looking like it worked while every chime kept firing.
   *
   * Keyed by GAME, as `sport|match_label` lowercased. That is the only
   * identity both sides can build: these compact feeds carry no event id the
   * tables share, and the checks re-fire under a different kind, period or
   * line as prices move, so a row-level mute would not hold on a fixture that
   * had already been dismissed.
   *
   * Reloaded every poll so a mute takes effect on the next cycle, exactly like
   * the Arbs mute it is modelled on.
   */
  const MUTED_GAMES_KEY = "findings_muted_games_v1";

  function loadMutedGames() {
    try {
      const raw = localStorage.getItem(MUTED_GAMES_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) { return new Set(); }
  }

  function gameMuteKey(row) {
    return (row.sport || "") + "|" +
           String(row.match_label || "").trim().toLowerCase();
  }

  function ladderKey(r) {
    return ["lad", r.book || "", r.event_id || "", r.market || "", r.period || "",
            r.side || "", r.line_lo, r.line_hi].join("|");
  }
  function consKey(f) {
    return ["cons", f.book || "", f.event_id || "", f.kind || "",
            f.periods || "", f.outcome || ""].join("|");
  }

  // OR across the filled criteria, exactly as the panel describes it. Every
  // criterion blank → nothing fires (rather than everything).
  /* The odds veto, shared by both feeds.
   *
   * Returns false only when the row names a price ABOVE the cap. A row with no
   * price is never vetoed: several consistency checks describe a relationship
   * rather than one bettable leg, and silently muting those would turn a noise
   * filter into a coverage hole.
   */
  function withinOddsCap(odds, key) {
    const cap = cfgNum(key);
    if (cap === null || cap <= 0) return true;
    if (odds === null || odds === undefined) return true;
    return odds <= cap;
  }

  /* The floor, and the other half of the band. Noise is at BOTH ends: a 40.0
   * longshot sits where the book's margin is 238 %, and a 1.02 clears any
   * percentage threshold trivially while being worth nothing to stake. A row
   * that names no price is exempt from both, same as the cap. */
  function aboveOddsFloor(odds, key) {
    const floor = cfgNum(key);
    if (floor === null || floor <= 0) return true;
    if (odds === null || odds === undefined) return true;
    return odds >= floor;
  }

  function ladderPasses(r, K) {
    K = K || ANOM;
    // Veto first: it overrides every criterion below, so an expensive rung
    // cannot chime by clearing one of them.
    const o = [r.odds_lo, r.odds_hi].filter(x => x != null);
    if (o.length && !withinOddsCap(Math.max.apply(null, o), K.LAD_MAXODDS)) return false;
    if (o.length && !aboveOddsFloor(Math.max.apply(null, o), K.LAD_MINODDS)) return false;
    const pct = cfgNum(K.LAD_PCT), delta = cfgNum(K.LAD_DELTA), step = cfgNum(K.LAD_STEP);
    if (pct   !== null && r.pct   != null && r.pct   >= pct)   return true;
    if (delta !== null && r.delta != null && r.delta >= delta) return true;
    if (step  !== null && r.step  != null && r.step  >= step)  return true;
    return false;
  }

  // Must stay identical to ALERT_DEFAULT_OFF in anomalies.html — the panel
  // draws the checkbox from its copy and this decides whether the chime fires,
  // so a kind in one set and not the other either alerts on something the grid
  // shows as off, or shows as on and never sounds.
  const ALERT_DEFAULT_OFF = new Set(["ml_vs_spread"]);

  function consPasses(f, kindCfg, dflt, K) {
    K = K || ANOM;
    if (!withinOddsCap(f.odds, K.CONS_MAXODDS)) return false;
    if (!aboveOddsFloor(f.odds, K.CONS_MINODDS)) return false;
    const k = kindCfg[f.kind] || {};
    if (k.on === false) return false;               // check silenced
    // Diagnostics stay silent until switched on deliberately. `k.on === true`
    // (an explicit tick in the panel) beats the default, same as the grid.
    if (k.on === undefined && ALERT_DEFAULT_OFF.has(f.kind)) return false;
    const bar = (k.sev === null || k.sev === undefined) ? dflt : k.sev;
    if (bar === null || bar === undefined || isNaN(bar)) return false;
    return f.severity != null && f.severity >= bar;
  }

  /* Shared "new or materially worse" pass over one feed.
   *
   * Mirrors the opportunity path: a row fires when we have not seen its key, or
   * when its magnitude has grown by RE_ALERT_FACTOR since we last fired on it.
   * Rows BELOW threshold are still tracked (magnitude recorded) so that a row
   * which drifts up into range later reads as new rather than being suppressed.
   * Returns the number of rows that should sound.
   */
  function evaluateFeed(rows, seen, passes, magnitudeOf, seededKey) {
    const isFirstPass = !localStorage.getItem(seededKey);
    const currentKeys = new Set();
    let fires = 0;
    for (const r of rows) {
      const k = r.__key;
      currentKeys.add(k);
      const mag = magnitudeOf(r);
      const ok = passes(r);
      const prev = seen.get(k);
      if (!ok) {
        // Track it at its current magnitude WITHOUT arming a re-alert: if it
        // later clears the bar we want that to read as new.
        if (prev === undefined) seen.set(k, null);
        continue;
      }
      const isNew = prev === undefined || prev === null
                    || (mag != null && mag >= prev * RE_ALERT_FACTOR);
      if (isNew) {
        seen.set(k, mag);
        if (!isFirstPass) fires++;
      }
    }
    // Prune vanished keys, with the same collapse guard the opportunity path
    // uses: a feed that briefly empties (scan mid-write, book unreachable) must
    // not wipe the seen-set, or the recovery poll re-alerts on everything.
    const collapsed = seen.size >= MIN_KEYS_FOR_PRUNE
      && currentKeys.size < seen.size * COLLAPSE_RATIO;
    if (!collapsed) {
      for (const k of [...seen.keys()]) if (!currentKeys.has(k)) seen.delete(k);
    }
    if (isFirstPass) {
      try { localStorage.setItem(seededKey, "1"); } catch (e) {}
      return 0;
    }
    return fires;
  }

  // One poller per board. The seen-maps are per board for the reason spelled
  // out at anomKeys(): the two feeds share event ids and check kinds, so a
  // shared map would have one board silencing the other.
  function makeFindingsPoller(K, url, label) {
    let ladderSeen = loadSeenMap(K.LAD_SEEN_KEY);
    let consSeen   = loadSeenMap(K.CONS_SEEN_KEY);

    return async function pollFindings() {
      const ladOn  = (localStorage.getItem(K.LAD_ON) === "1");
      const consOn = (localStorage.getItem(K.CONS_ON) === "1");
      if (!ladOn && !consOn) return;    // nothing enabled → don't even fetch

      let feed;
      try {
        const r = await fetch(url);
        if (!r.ok) return;
        feed = await r.json();
      } catch (e) { return; }
      if (!feed.enabled) return;        // scanner off — nothing to say

      // Reload every poll so a mute clicked on either tab applies next cycle.
      const muted = loadMutedGames();
      // A muted row still goes through evaluateFeed — it must land in the
      // seen-map — but can never pass, so it never sounds. Dropping the rows
      // instead would make un-muting retro-fire every finding that arrived
      // while the game was quiet.
      const unmuted = (passes) => (row) => !muted.has(gameMuteKey(row)) && passes(row);

      if (ladOn) {
        const rows = (feed.ladders || []).map(r => { r.__key = ladderKey(r); return r; });
        const n = evaluateFeed(rows, ladderSeen, unmuted(r => ladderPasses(r, K)),
                               r => r.pct, K.LAD_SEEDED_KEY);
        saveSeenMap(K.LAD_SEEN_KEY, ladderSeen);
        if (n > 0) {
          if (claimSound(K.SOUND_LAD)) playLadderAlert();
          try { console.log(`[ladder-alert] ${label}: ${n} new/worsened ladder violation(s)`); }
          catch (e) {}
        }
      }

      if (consOn) {
        let kindCfg = {};
        try { kindCfg = JSON.parse(localStorage.getItem(K.CONS_KINDS) || "{}") || {}; }
        catch (e) { kindCfg = {}; }
        const dflt = cfgNum(K.CONS_DEF);
        const rows = (feed.consistency || []).map(f => { f.__key = consKey(f); return f; });
        const n = evaluateFeed(rows, consSeen,
                               unmuted(f => consPasses(f, kindCfg, dflt, K)),
                               f => f.severity, K.CONS_SEEDED_KEY);
        saveSeenMap(K.CONS_SEEN_KEY, consSeen);
        if (n > 0) {
          if (claimSound(K.SOUND_CONS)) playConsistencyAlert();
          try { console.log(`[consistency-alert] ${label}: ${n} new/worsened flag(s)`); }
          catch (e) {}
        }
      }
    };
  }

  const pollAnomalyFindings = makeFindingsPoller(
    ANOM, "/api/anomalies/alerts", "anomalies");
  const pollLsportFindings = makeFindingsPoller(
    LSP, "/api/new_inconsistencies/alerts", "new inconsistencies");

  // Initial poll + interval. Stagger by 2s so we don't slam the API on
  // page-load alongside the page's own /api/opportunities fetch.
  setTimeout(poll, 2_000);
  setInterval(poll, POLL_MS);
  // Move poll offset by 1s from the opp poll so the two never fire their
  // fetches in the exact same tick.
  setTimeout(pollMoves, 3_000);
  setInterval(pollMoves, POLL_MS);
  // Scan-refresh poll offset another 1s.
  setTimeout(pollScan, 4_000);
  setInterval(pollScan, POLL_MS);
  // Anomaly content alerts, offset another 1s again.
  setTimeout(pollAnomalyFindings, 5_000);
  setInterval(pollAnomalyFindings, POLL_MS);
  // The LSport board, offset another 1s so the two never fetch in one tick.
  setTimeout(pollLsportFindings, 6_000);
  setInterval(pollLsportFindings, POLL_MS);

  // Test seam. `module` does not exist in a browser, so this is inert there and
  // changes nothing about how the page behaves. It exists because the alert
  // DECISION rules — OR across filled criteria, blank-means-off, the
  // new-or-worsened re-alert, silent seeding, the collapse guard — are exactly
  // the kind of logic that has broken here before while the page still looked
  // fine. tests/test_anomaly_alerts_logic.py drives these under node.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { ladderPasses, consPasses, evaluateFeed, cfgNum,
                       withinOddsCap, aboveOddsFloor,
                       LAD_MAXODDS, CONS_MAXODDS, LAD_MINODDS, CONS_MINODDS,
                       ladderKey, consKey, RE_ALERT_FACTOR,
                       readLayers, normaliseGates, matchingLayers,
                       effectiveStep, oppQueryFloor, passesGates,
                       readOppGates, DEFAULT_RE_ALERT_PP,
                       // The game mute (2026-09-24): whether a muted row is
                       // still TRACKED is the part that breaks silently, so
                       // it is driven under node rather than only grepped.
                       loadMutedGames, gameMuteKey, MUTED_GAMES_KEY,
                       anomKeys };
  }
})();
