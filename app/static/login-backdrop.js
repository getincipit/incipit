/* The sign-in page's backdrop, as the site's owner chose it: a colour, a photo album, a video, an
   animated SVG, or one of the built-in motions. Motion stops while the page is hidden, and under
   prefers-reduced-motion the album shows its first photograph and the motions draw one still.
   The appearance editor draws the same backdrop into its preview through window.drawBackdrop. */
(function () {
  "use strict";

  const stillOnly = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function dark() {
    const chosen = document.documentElement.getAttribute("data-theme");
    if (chosen) return chosen === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function colour(host, backdrop) {
    host.style.background = dark() ? backdrop.colour_dark : backdrop.colour_light;
  }

  function album(host, backdrop, photos, motion) {
    if (!photos.length) return;
    const settings = backdrop.album || {};
    const order = photos.slice();
    if (settings.shuffle) order.sort(() => Math.random() - 0.5);
    const layers = order.map((photo) => {
      const layer = document.createElement("div");
      layer.className = "layer" + (settings.transition === "pan" && !stillOnly() ? " pan" : "");
      // Loaded lazily: only the photograph about to show is fetched.
      layer.dataset.src = photo.url;
      host.appendChild(layer);
      return layer;
    });
    const caption = document.createElement("div");
    caption.className = "caption";
    caption.hidden = true;
    host.appendChild(caption);
    let index = 0;
    function show(i) {
      const layer = layers[i];
      if (layer.dataset.src) {
        layer.style.backgroundImage = `url("${layer.dataset.src}")`;
        delete layer.dataset.src;
      }
      layers.forEach((l, j) => l.classList.toggle("shown", j === i));
      const words = [order[i].caption, order[i].credit].filter(Boolean).join(" — ");
      caption.textContent = words;
      caption.hidden = !(settings.captions && words);
    }
    show(0);
    if (stillOnly() || layers.length < 2) return;
    let timer = null;
    motion.start.push(() => {
      if (timer === null) timer = setInterval(() => { index = (index + 1) % layers.length; show(index); },
                                              Math.max(4, settings.interval || 8) * 1000);
    });
    motion.stop.push(() => { clearInterval(timer); timer = null; });
  }

  function video(host, backdrop, motion) {
    const chosen = backdrop.video || {};
    if (!chosen.asset) return;
    if (stillOnly()) {
      if (chosen.poster) {
        const still = document.createElement("div");
        still.className = "layer shown";
        still.style.backgroundImage = `url("/site/assets/${chosen.poster}")`;
        host.appendChild(still);
      }
      return;
    }
    const player = document.createElement("video");
    Object.assign(player, { muted: true, loop: true, playsInline: true, autoplay: true });
    player.setAttribute("muted", "");
    if (chosen.poster) player.poster = `/site/assets/${chosen.poster}`;
    player.src = `/site/assets/${chosen.asset}`;
    host.appendChild(player);
    motion.start.push(() => player.play().catch(() => {}));
    motion.stop.push(() => player.pause());
  }

  function animation(host, backdrop) {
    const chosen = backdrop.animation || {};
    if (!chosen.asset || stillOnly()) return;
    // An <img> plays an SVG's own CSS and SMIL motion and never runs a script inside it.
    const image = document.createElement("img");
    image.alt = "";
    image.src = `/site/assets/${chosen.asset}`;
    host.appendChild(image);
  }

  // ---------- the built-in motions, drawn on a canvas ----------

  const PRESETS = {
    // Paper grain drifting slowly, as if a sheet were moving under a lamp.
    paper(ctx, w, h, t, ink) {
      ctx.clearRect(0, 0, w, h);
      for (let i = 0; i < 900; i++) {
        const x = ((Math.sin(i * 12.9898 + t * 0.00002) * 43758.5453) % 1 + 1) % 1 * w;
        const y = ((Math.sin(i * 78.233 + t * 0.000013) * 12345.6789) % 1 + 1) % 1 * h;
        ctx.fillStyle = ink(0.035 + (i % 7) * 0.006);
        ctx.fillRect(x, y, 1.5, 1.5);
      }
    },
    // A slow wash of ink, spreading and receding.
    ink(ctx, w, h, t, ink) {
      ctx.clearRect(0, 0, w, h);
      for (let i = 0; i < 5; i++) {
        const x = w * (0.55 + 0.3 * Math.sin(t * 0.00005 + i * 1.7));
        const y = h * (0.5 + 0.35 * Math.cos(t * 0.00004 + i * 2.3));
        const r = Math.max(1, Math.min(w, h) * (0.25 + 0.1 * Math.sin(t * 0.00007 + i)));
        const wash = ctx.createRadialGradient(x, y, 0, x, y, r);
        wash.addColorStop(0, ink(0.07));
        wash.addColorStop(1, ink(0));
        ctx.fillStyle = wash;
        ctx.fillRect(0, 0, w, h);
      }
    },
    // Ruled pages, line by line, drifting across the right of the page.
    lines(ctx, w, h, t, ink) {
      ctx.clearRect(0, 0, w, h);
      ctx.lineWidth = 1.2;
      for (let page = 0; page < 3; page++) {
        const px = w * (0.5 + page * 0.17) + Math.sin(t * 0.00003 + page) * 18;
        const py = h * (0.18 + page * 0.08) + Math.cos(t * 0.000025 + page) * 14;
        const pw = Math.min(260, w * 0.18), ph = pw * 1.3;
        ctx.strokeStyle = ink(0.14);
        ctx.strokeRect(px, py, pw, ph);
        ctx.strokeStyle = ink(0.09);
        for (let line = 1; line < 14; line++) {
          const length = pw * (0.55 + 0.35 * Math.abs(Math.sin(line * 3.1 + page)));
          ctx.beginPath();
          ctx.moveTo(px + 14, py + line * ph / 15);
          ctx.lineTo(px + 14 + length - 28, py + line * ph / 15);
          ctx.stroke();
        }
      }
    },
  };

  function preset(host, backdrop, motion) {
    const draw = PRESETS[backdrop.preset] || PRESETS.paper;
    const canvas = document.createElement("canvas");
    host.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const ink = (alpha) => (dark() ? `rgba(255, 255, 255, ${alpha})` : `rgba(20, 24, 32, ${alpha})`);
    function size() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, host.clientWidth * ratio);
      canvas.height = Math.max(1, host.clientHeight * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }
    size();
    window.addEventListener("resize", size);
    motion.dispose.push(() => window.removeEventListener("resize", size));
    const frame = (t) => draw(ctx, host.clientWidth, host.clientHeight, t, ink);
    frame(0);
    if (stillOnly()) return;
    let handle = null;
    const loop = (t) => { frame(t); handle = requestAnimationFrame(loop); };
    motion.start.push(() => { if (handle === null) handle = requestAnimationFrame(loop); });
    motion.stop.push(() => { cancelAnimationFrame(handle); handle = null; });
  }

  /** Draws `site`'s backdrop into `host`; returns a function that stops it and empties the host. */
  function drawBackdrop(site, host) {
    const backdrop = (site.appearance && site.appearance.backdrop) || { kind: "none" };
    // Everything that runs on a timer registers here, so hiding or disposing stops all of it.
    const motion = { start: [], stop: [], dispose: [] };
    host.replaceChildren();
    host.style.background = "";
    host.dataset.kind = backdrop.kind;
    if (backdrop.kind === "colour") colour(host, backdrop);
    else if (backdrop.kind === "album") album(host, backdrop, site.album || [], motion);
    else if (backdrop.kind === "video") video(host, backdrop, motion);
    else if (backdrop.kind === "animation") animation(host, backdrop);
    else if (backdrop.kind === "preset") preset(host, backdrop, motion);
    const onVisibility = () => (document.hidden ? motion.stop : motion.start).forEach((step) => step());
    if (!document.hidden) motion.start.forEach((go) => go());
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      motion.stop.forEach((step) => step());
      motion.dispose.forEach((step) => step());
      document.removeEventListener("visibilitychange", onVisibility);
      host.replaceChildren();
      host.style.background = "";
    };
  }

  window.drawBackdrop = drawBackdrop;
  const page = document.getElementById("site-backdrop");
  if (page) {
    fetch("/api/site").then((r) => (r.ok ? r.json() : null)).then((site) => site && drawBackdrop(site, page))
      .catch(() => { /* the card works without a backdrop */ });
  }
})();
