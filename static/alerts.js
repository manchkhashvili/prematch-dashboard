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

  // ── Key for de-duping alerts across polls ──────────────────────────────
  function alertKey(o) {
    // Prefer event id; fall back to sport+label so rows without an event id
    // (rare; off-platform synthetic) don't all collide into the empty bucket.
    if (o.cb_event_id) {
      return `eid|${o.cb_event_id}|${o.market}|${o.side}|${o.kind}`;
    }
    return `lbl|${o.sport || ""}|${o.match_label}|${o.market}|${o.side}|${o.kind}`;
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
    const currentKeys = new Set();
    let newAtThreshold = 0;

    for (const o of opps) {
      const k = alertKey(o);
      currentKeys.add(k);
      const prevEdge = seenKeys.get(k);
      const isNew = (prevEdge === undefined) || (o.edge_pct - prevEdge >= 5);
      if (isNew) seenKeys.set(k, o.edge_pct);
      // Only count for sound-play if at-or-above threshold AND this isn't
      // the first-ever seed pass AND alerts are enabled.
      if (isNew && o.edge_pct >= threshold && !isFirstEverPoll && enabled) {
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

  // Initial poll + interval. Stagger by 2s so we don't slam the API on
  // page-load alongside the page's own /api/opportunities fetch.
  setTimeout(poll, 2_000);
  setInterval(poll, POLL_MS);
})();
