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
  const ENABLED_KEY   = "alert_enabled";
  const THRESHOLD_KEY = "alert_threshold";
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
    let threshold;
    try {
      threshold = parseFloat(localStorage.getItem(THRESHOLD_KEY) || "15");
      if (isNaN(threshold)) threshold = 15;
    } catch (e) { threshold = 15; }

    let opps;
    try {
      // Always query at min_edge=1 (catch-all) so the seen-set tracks every
      // opp regardless of current threshold setting. Filter to threshold
      // client-side just before deciding whether to play.
      const r = await fetch("/api/opportunities?min_edge=1");
      if (!r.ok) return;
      opps = await r.json();
    } catch (e) { return; }

    const isFirstEverPoll = !localStorage.getItem(SEEDED_KEY);
    // Reload mutes every poll — picks up toggles from arbs.html immediately.
    const mutedKeys = loadMutedSet();
    const currentKeys = new Set();
    let newAtThreshold = 0;

    for (const o of opps) {
      const k = alertKey(o);
      currentKeys.add(k);
      const prevEdge = seenKeys.get(k);
      const isNew = (prevEdge === undefined) || (o.edge_pct - prevEdge >= 5);
      if (isNew) seenKeys.set(k, o.edge_pct);
      // Phase 3.13: skip sound for opps the user muted on /arbs.html.
      // Still track them in seenKeys (so they don't fire when un-muted on
      // the same edge) but never count them toward newAtThreshold.
      const isMuted = mutedKeys.has(betKey(o));
      // Only count for sound-play if at-or-above threshold AND this isn't
      // the first-ever seed pass AND alerts are enabled AND not muted.
      if (isNew && o.edge_pct >= threshold && !isFirstEverPoll && enabled && !isMuted) {
        newAtThreshold++;
      }
    }

    // Prune keys that no longer appear (game settled, line pulled, dropped
    // below min_edge=1). Keeps localStorage bounded — the dashboard runs
    // for hours; without pruning the map would grow indefinitely.
    for (const k of [...seenKeys.keys()]) {
      if (!currentKeys.has(k)) seenKeys.delete(k);
    }
    saveSeen(seenKeys);

    if (isFirstEverPoll) {
      // Mark seeded — current opps are now "known" without playing sound.
      // Next poll will fire on truly new opportunities only.
      try { localStorage.setItem(SEEDED_KEY, "1"); } catch (e) {}
      return;
    }

    if (newAtThreshold > 0) {
      playPing();
      // Console hint for the dev console-watcher case.
      try {
        console.log(`[alert] ${newAtThreshold} new opportunity(ies) at ≥ ${threshold}%`);
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
      playMoveChime();
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

    playScanChime();
    try {
      console.log(`[scan-alert] anomaly scan refreshed: ${status.anomalies} anomalies, `
                  + `${status.consistency} consistency flags`);
    } catch (e) {}
  }

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
})();
