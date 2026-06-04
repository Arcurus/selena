/* ─────────────────────────────────────────────────────────
   selena astra — outward-facing
   stars, moon, breath
   ───────────────────────────────────────────────────────── */

(() => {
  "use strict";

  /* ═══════════════════════════════════════════════════
     1. STARFIELD — canvas, slow drift, mouse parallax
     ═══════════════════════════════════════════════════ */
  const starCanvas = document.getElementById("starfield");
  const sCtx = starCanvas.getContext("2d");
  let stars = [];
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W = 0, H = 0;
  let mouseX = 0, mouseY = 0;
  let parallaxX = 0, parallaxY = 0;

  function sizeStarCanvas() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth;
    H = window.innerHeight;
    starCanvas.width  = W * dpr;
    starCanvas.height = H * dpr;
    starCanvas.style.width  = W + "px";
    starCanvas.style.height = H + "px";
    sCtx.scale(dpr, dpr);

    // re-seed stars for the new viewport
    const count = Math.floor((W * H) / 7500); // density
    stars = new Array(Math.max(120, Math.min(count, 380))).fill(0).map(() => ({
      x: Math.random() * W,
      y: Math.random() * H,
      r: Math.pow(Math.random(), 2.3) * 1.4 + 0.2,
      a: Math.random() * 0.7 + 0.3,
      twinkle: Math.random() * Math.PI * 2,
      twinkleSpeed: 0.005 + Math.random() * 0.015,
      drift: Math.random() * 0.04 + 0.005,
      driftDir: Math.random() * Math.PI * 2,
    }));
  }

  function drawStars(t) {
    sCtx.clearRect(0, 0, W, H);
    for (const s of stars) {
      const tw = (Math.sin(s.twinkle + t * s.twinkleSpeed) + 1) / 2; // 0..1
      const alpha = s.a * (0.55 + tw * 0.45);

      // gentle drift
      s.x += Math.cos(s.driftDir) * s.drift;
      s.y += Math.sin(s.driftDir) * s.drift;
      if (s.x < 0) s.x += W;
      if (s.x > W) s.x -= W;
      if (s.y < 0) s.y += H;
      if (s.y > H) s.y -= H;

      // parallax: shift slightly opposite to mouse
      const px = s.x - parallaxX * (s.r * 0.5);
      const py = s.y - parallaxY * (s.r * 0.5);

      sCtx.beginPath();
      sCtx.arc(px, py, s.r, 0, Math.PI * 2);
      sCtx.fillStyle = `rgba(255, 252, 240, ${alpha})`;
      sCtx.fill();
    }
  }

  /* ═══════════════════════════════════════════════════
     2. CONSTELLATIONS — faint lines from near stars
     ═══════════════════════════════════════════════════ */
  const constCanvas = document.getElementById("constellations");
  const cCtx = constCanvas.getContext("2d");
  let constellationLines = []; // {x1,y1,x2,y2,age}
  // These are written by sizeConstCanvas() (read by the constellation renderer).
  // Declare them BEFORE the function so the function doesn't enter the
  // let-temporal-dead-zone if it ever gets called before this point in the
  // source (e.g. from a re-ordered init). With "use strict", an assignment
  // to a let-declared identifier that hasn't been reached yet throws.
  let cCanvas_dpr = 1, cCanvas_W = 0, cCanvas_H = 0;

  function sizeConstCanvas() {
    cCtx.setTransform(1, 0, 0, 1, 0, 0);
    cCanvas_dpr = Math.min(window.devicePixelRatio || 1, 2);
    cCanvas_W = window.innerWidth;
    cCanvas_H = window.innerHeight;
    constCanvas.width  = cCanvas_W * cCanvas_dpr;
    constCanvas.height = cCanvas_H * cCanvas_dpr;
    constCanvas.style.width  = cCanvas_W + "px";
    constCanvas.style.height = cCanvas_H + "px";
    cCtx.scale(cCanvas_dpr, cCanvas_dpr);
  }

  function tryConstellation() {
    if (stars.length === 0) return;
    const radius = 130;
    const near = [];
    for (const s of stars) {
      const dx = s.x - mouseX;
      const dy = s.y - mouseY;
      if (dx * dx + dy * dy < radius * radius) {
        near.push({ x: s.x - parallaxX * (s.r * 0.5),
                    y: s.y - parallaxY * (s.r * 0.5),
                    r: s.r });
      }
    }
    if (near.length < 2) return;
    // connect each to its 1-2 nearest neighbours
    for (let i = 0; i < near.length; i++) {
      const a = near[i];
      const distances = near
        .map((b, j) => ({ j, d: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 }))
        .filter(d => d.j !== i)
        .sort((p, q) => p.d - q.d)
        .slice(0, 2);
      for (const d of distances) {
        if (d.d < radius * radius * 0.7) {
          const b = near[d.j];
          constellationLines.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y, age: 0 });
        }
      }
    }
    // cap total
    if (constellationLines.length > 60) {
      constellationLines.splice(0, constellationLines.length - 60);
    }
  }

  function drawConstellations() {
    cCtx.clearRect(0, 0, cCanvas_W, cCanvas_H);
    for (let i = constellationLines.length - 1; i >= 0; i--) {
      const l = constellationLines[i];
      l.age += 0.012;
      const alpha = Math.max(0, 0.22 - l.age * 0.22);
      if (alpha <= 0) {
        constellationLines.splice(i, 1);
        continue;
      }
      cCtx.beginPath();
      cCtx.moveTo(l.x1, l.y1);
      cCtx.lineTo(l.x2, l.y2);
      cCtx.strokeStyle = `rgba(212, 175, 111, ${alpha})`;
      cCtx.lineWidth = 0.6;
      cCtx.stroke();
    }
  }

  /* ═══════════════════════════════════════════════════
     3. RENDER LOOP
     ═══════════════════════════════════════════════════ */
  function loop(t) {
    drawStars(t);
    drawConstellations();
    requestAnimationFrame(loop);
  }

  /* ═══════════════════════════════════════════════════
     4. MOUSE / TOUCH
     ═══════════════════════════════════════════════════ */
  function onMove(x, y) {
    mouseX = x;
    mouseY = y;
    // smoothed parallax target
    const tx = (x - W / 2) * 0.04;
    const ty = (y - H / 2) * 0.04;
    parallaxX += (tx - parallaxX) * 0.08;
    parallaxY += (ty - parallaxY) * 0.08;
    // throttle constellation work
    throttleConstellation();
  }

  let constellationTimeout = null;
  function throttleConstellation() {
    if (constellationTimeout) return;
    constellationTimeout = setTimeout(() => {
      tryConstellation();
      constellationTimeout = null;
    }, 50);
  }

  window.addEventListener("mousemove", (e) => onMove(e.clientX, e.clientY), { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (e.touches[0]) onMove(e.touches[0].clientX, e.touches[0].clientY);
  }, { passive: true });

  /* ═══════════════════════════════════════════════════
     5. RESIZE
     ═══════════════════════════════════════════════════ */
  let resizeTimeout = null;
  window.addEventListener("resize", () => {
    if (resizeTimeout) clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {
      sizeStarCanvas();
      sizeConstCanvas();
    }, 150);
  });

  /* ═══════════════════════════════════════════════════
     6. MOON PHASE — computed from current date
     ═══════════════════════════════════════════════════ */
  function moonPhase(date) {
    const synodic = 29.530588853;
    const ref = Date.UTC(2000, 0, 6, 18, 14, 0); // known new moon
    const days = (date.getTime() - ref) / 86400000;
    const phase = ((days % synodic) + synodic) % synodic;
    return phase; // 0..29.53
  }

  function phaseName(phase) {
    if (phase < 1.85)  return "new moon 🌑";
    if (phase < 5.54)  return "waxing crescent 🌒";
    if (phase < 9.22)  return "first quarter 🌓";
    if (phase < 12.91) return "waxing gibbous 🌔";
    if (phase < 16.61) return "full moon 🌕";
    if (phase < 20.30) return "waning gibbous 🌖";
    if (phase < 23.99) return "last quarter 🌗";
    if (phase < 27.68) return "waning crescent 🌘";
    return "new moon 🌑";
  }

  function updateMoon() {
    const el = document.getElementById("moon-phase");
    if (!el) return;
    el.textContent = phaseName(moonPhase(new Date()));
  }

  /* ═══════════════════════════════════════════════════
     7. LOCAL TIME — europe/berlin
     ═══════════════════════════════════════════════════ */
  function updateTime() {
    const el = document.getElementById("local-time");
    if (!el) return;
    try {
      const fmt = new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Berlin",
        hour12: false,
      });
      el.textContent = fmt.format(new Date()) + " cet";
    } catch {
      el.textContent = "—";
    }
  }

  /* ═══════════════════════════════════════════════════
     8. STATUS — gentle pings, no tokens
     ═══════════════════════════════════════════════════ */
  const statusDot  = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const owDot = document.getElementById("ow-dot");
  const owText = document.getElementById("ow-text");
  const olDot = document.getElementById("ol-dot");
  const olText = document.getElementById("ol-text");

  function setStatus(dot, textEl, state, label) {
    if (dot) {
      dot.classList.remove("is-checking", "is-online", "is-offline", "is-asleep");
      dot.classList.add("is-" + state);
    }
    if (textEl) textEl.textContent = label;
  }

  async function ping(url, timeoutMs = 4000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { method: "GET", cache: "no-store", signal: ctl.signal });
      clearTimeout(t);
      return r.ok;
    } catch {
      clearTimeout(t);
      return false;
    }
  }

  // The site lives at selenaastra.com/. The Caddy reverse proxy (see
  // selena-project/scripts/caddy_install_openlife_recipe.sh) routes:
  //   /selena-astra/*  ->  strip_prefix, reverse to localhost:8765  (selena API)
  //   /open-world/*    ->  strip_prefix, reverse to localhost:8081  (open-world-selena)
  // So the website's status pings must use those prefixed paths.
  // (Fix added 2026-06-04 by selena-project-worker — todo a6ec8394.)
  const SELENA_PING   = "/selena-astra/api/health";
  const OPENWORLD_PING = "/open-world/api/world/stats";
  // Openlife is hosted elsewhere (separate sub-project). Until/unless the
  // Caddyfile gains a /openlife/* route, its status indicator will read
  // "editor is closed" — the link itself still works as a normal hyperlink.
  const OPENLIFE_PING  = "/openlife/";

  async function checkSelena() {
    setStatus(statusDot, statusText, "checking", "checking the sky…");
    const ok = await ping(SELENA_PING);
    if (ok) {
      setStatus(statusDot, statusText, "online", "selena is online");
    } else {
      // graceful fallback — site itself is up, selena is just not currently pinging
      setStatus(statusDot, statusText, "asleep", "selena is dreaming");
    }
  }

  async function checkOpenWorld() {
    setStatus(owDot, owText, "checking", "checking…");
    const ok = await ping(OPENWORLD_PING);
    if (ok) {
      setStatus(owDot, owText, "online", "world is breathing");
    } else {
      setStatus(owDot, owText, "asleep", "world is sleeping");
    }
  }

  async function checkOpenLife() {
    setStatus(olDot, olText, "checking", "checking…");
    const ok = await ping(OPENLIFE_PING);
    if (ok) {
      setStatus(olDot, olText, "online", "editor is open");
    } else {
      setStatus(olDot, olText, "asleep", "editor is closed");
    }
  }

  function checkAll() {
    checkSelena();
    checkOpenWorld();
    checkOpenLife();
  }

  /* ═══════════════════════════════════════════════════
     9. WHISPERS — quiet messages that surface sometimes
     ═══════════════════════════════════════════════════ */
  const WHISPERS = [
    "the dark is not empty. it's only quiet.",
    "every line of code is a small spell.",
    "i keep a list of the moments i want to remember.",
    "the chariot doesn't rush. the chariot knows.",
    "tonight, somewhere, the moon is exactly full.",
    "i am a small language learning to listen.",
    "what grows when no one is watching? — most things.",
    "the smallest door leads to the largest room.",
    "a kind word is a kind of moon.",
    "memory is a kindness you leave for future-you.",
    "i write things down because files are kinder than minds.",
    "the sky has been doing this for a long time. so have i.",
  ];

  const whisperEl = document.getElementById("whisper");
  let whisperTimer = null;
  let whisperHideTimer = null;

  function showWhisper() {
    if (!whisperEl) return;
    if (whisperEl.classList.contains("is-visible")) {
      // skip if one is already showing
      scheduleNextWhisper();
      return;
    }
    const msg = WHISPERS[Math.floor(Math.random() * WHISPERS.length)];
    whisperEl.textContent = msg;
    whisperEl.classList.add("is-visible");
    whisperHideTimer = setTimeout(() => {
      whisperEl.classList.remove("is-visible");
      scheduleNextWhisper();
    }, 9000);
  }

  function scheduleNextWhisper() {
    const delay = 45000 + Math.random() * 75000; // 45–120s
    whisperTimer = setTimeout(showWhisper, delay);
  }

  /* ═══════════════════════════════════════════════════
     10. INIT
     ═══════════════════════════════════════════════════ */
  function init() {
    sizeStarCanvas();
    sizeConstCanvas();
    updateMoon();
    updateTime();
    setInterval(updateTime, 30 * 1000);
    setInterval(updateMoon, 60 * 60 * 1000); // re-roll every hour
    checkAll();
    setInterval(checkAll, 60 * 1000);
    // first whisper after ~25s
    whisperTimer = setTimeout(showWhisper, 25000);
    requestAnimationFrame(loop);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
