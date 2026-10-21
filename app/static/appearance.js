/* Site settings and appearance editors, mounted in Settings (Site, Appearance) and in the setup
   wizard. The server validates and owns every rule; this only gathers choices and shows the
   answer, with a live preview of the sign-in page drawn by login-backdrop.js. */
(function () {
  "use strict";

  const { esc, api } = window.pageKit;
  const FONTS = [["system", "System"], ["serif", "Serif"], ["humanist", "Humanist sans"], ["mono", "Monospace"]];
  const BACKDROPS = [["none", "None"], ["colour", "Colour"], ["album", "Photo album"], ["video", "Video"],
                     ["animation", "Animation"], ["preset", "Built-in motion"]];
  const PRESETS = [["paper", "Drifting paper"], ["ink", "Slow ink wash"], ["lines", "Ruled pages"]];
  // The same backgrounds appearance.py checks the accent against.
  const GROUNDS = { neutral: { light: "#fbfbfd", dark: "#141416" }, warm: { light: "#fbfaf6", dark: "#161512" },
                    cool: { light: "#f8fafc", dark: "#121418" } };
  const ON_ACCENT = { light: "#ffffff", dark: "#111318" };


  function luminance(hex) {
    const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  function contrast(a, b) {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  }

  function status(container, message, isError) {
    const box = container.querySelector(".appearance-status");
    if (!box) return;
    box.textContent = message || "";
    box.classList.toggle("is-error", !!isError);
    box.hidden = !message;
  }

  /** Re-fetches /site.css, so the page itself takes on what was just saved. */
  function refreshSiteCss() {
    document.querySelectorAll('link[href^="/site.css"]').forEach((link) => {
      link.href = `/site.css?v=${Date.now()}`;
    });
  }

  // ---------- Site: names, labels and joining ----------

  async function mountSite(container, options) {
    const opts = options || {};
    const data = await api("GET", "/api/site/settings");
    const owner = !!opts.owner;
    container.innerHTML = `
      <div class="appearance-status pipeline-status" hidden></div>
      <div class="setting-card">
        <div class="setting-card-head"><h4>The site</h4></div>
        <label class="field">Name of the site<input name="product_name" maxlength="60" value="${esc(data.product_name)}" /></label>
        <label class="field">Organisation<input name="company_name" maxlength="120" value="${esc(data.company_name)}" /></label>
        <label class="field">Time zone<input name="time_zone" maxlength="64" value="${esc(data.time_zone)}" /></label>
      </div>
      <div class="setting-card">
        <div class="setting-card-head"><h4>What people are called</h4></div>
        <p class="setting-note">These words appear wherever the site names a role.</p>
        <div class="field-row">
          <label class="field">Owner<input name="role_label_owner" maxlength="30" value="${esc(data.role_label_owner)}" /></label>
          <label class="field">Administrator<input name="role_label_admin" maxlength="30" value="${esc(data.role_label_admin)}" /></label>
          <label class="field">Reviewer<input name="role_label_reviewer" maxlength="30" value="${esc(data.role_label_reviewer)}" /></label>
        </div>
      </div>
      <div class="setting-card">
        <div class="setting-card-head"><h4>Joining</h4></div>
        <label class="check"><input type="checkbox" name="reviewer_self_signup" ${data.reviewer_self_signup ? "checked" : ""} />
          Anyone with the sign-in link may create a reviewer account without a join code</label>
        <label class="check"><input type="checkbox" name="join_enabled" ${data.join_enabled ? "checked" : ""} ${owner ? "" : "disabled"} />
          New accounts may be created from the sign-in page${owner ? "" : " (the owner's choice)"}</label>
        <div class="org-id">
          <span>Organisation id</span> <code class="org-id-value">${esc(data.org_id)}</code>
          ${owner ? '<button type="button" class="btn btn-secondary btn-small" data-org-id>New id</button>' : ""}
        </div>
        <p class="setting-note">People can reach this site's sign-in page by its link, or by typing this id on the
          Incipit sign-in page. A new id stops the old one working.</p>
      </div>
      <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-save-site>Save</button></div>`;

    container.querySelector("[data-org-id]")?.addEventListener("click", async () => {
      if (!window.confirm("Make a new organisation id? Anyone using the old one will need the new one.")) return;
      try {
        const { org_id: orgId } = await api("POST", "/api/site/org-id");
        container.querySelector(".org-id-value").textContent = orgId;
        status(container, "A new organisation id is in place.");
      } catch (error) { status(container, error.message, true); }
    });
    container.querySelector("[data-save-site]").addEventListener("click", async () => {
      const changes = {};
      container.querySelectorAll("input[name]").forEach((input) => {
        if (input.disabled) return;
        changes[input.name] = input.type === "checkbox" ? input.checked : input.value;
      });
      try {
        const saved = await api("PUT", "/api/site/settings", changes);
        if (window.setTermLabels) {
          window.setTermLabels({ owner: saved.role_label_owner, admin: saved.role_label_admin,
                                 reviewer: saved.role_label_reviewer });
        }
        status(container, "Saved.");
        if (opts.onSaved) opts.onSaved();
      } catch (error) { status(container, error.message, true); }
    });
  }

  // ---------- Appearance ----------

  async function mountAppearance(container, options) {
    const opts = options || {};
    const owner = !!opts.owner;
    let data = await api("GET", "/api/site/settings");
    let look = JSON.parse(JSON.stringify(data.appearance));
    let assets = data.assets;
    let stopPreview = null;

    const byKind = (kind) => assets.filter((a) => a.kind === kind);
    const assetUrl = (id) => (id ? `/site/assets/${id}` : "");
    const album = () => look.backdrop.album.assets.map((id) => assets.find((a) => a.id === id)).filter(Boolean);

    function fontOptions(chosen) {
      const uploaded = byKind("font").map((f) => [f.id, `Uploaded font ${f.id}`]);
      return FONTS.concat(uploaded).map(([value, label]) =>
        `<option value="${esc(value)}" ${String(value) === String(chosen) ? "selected" : ""}>${esc(label)}</option>`).join("");
    }

    function ratioNote(theme) {
      const accent = look[`accent_${theme}`];
      const ground = GROUNDS[look.surface][theme];
      const worst = Math.min(contrast(accent, ground), contrast(ON_ACCENT[theme], accent));
      return `${worst.toFixed(1)}:1 ${worst >= 3 ? "— fine" : "— needs 3:1"}`;
    }

    function albumGrid() {
      const photos = album();
      if (!photos.length) return '<p class="setting-note">No photographs yet.</p>';
      return `<ol class="album-grid">${photos.map((photo) => `
        <li class="album-item" draggable="true" data-asset="${photo.id}">
          <img src="${esc(photo.url)}" alt="" loading="lazy" />
          <input placeholder="Caption" data-caption="${photo.id}" value="${esc(photo.caption)}" maxlength="200" />
          <input placeholder="Credit" data-credit="${photo.id}" value="${esc(photo.credit)}" maxlength="200" />
          <button type="button" class="btn btn-ghost btn-small" data-remove-asset="${photo.id}" aria-label="Remove">&times;</button>
        </li>`).join("")}</ol>`;
    }

    function backdropControls() {
      const b = look.backdrop;
      if (b.kind === "colour") {
        return `<div class="field-row">
          <label class="field">Light<input type="color" data-backdrop="colour_light" value="${esc(b.colour_light)}" /></label>
          <label class="field">Dark<input type="color" data-backdrop="colour_dark" value="${esc(b.colour_dark)}" /></label></div>`;
      }
      if (b.kind === "album") {
        return `${albumGrid()}
          <label class="btn btn-secondary btn-small upload">Add photographs
            <input type="file" accept="image/png,image/jpeg,image/webp" multiple data-upload="album_image" hidden /></label>
          <div class="field-row">
            <label class="field">Moves by<select data-album="transition">
              <option value="crossfade" ${b.album.transition === "crossfade" ? "selected" : ""}>Crossfade</option>
              <option value="pan" ${b.album.transition === "pan" ? "selected" : ""}>Slow pan</option></select></label>
            <label class="field">Seconds each<input type="number" min="4" max="30" data-album="interval" value="${esc(b.album.interval)}" /></label>
          </div>
          <label class="check"><input type="checkbox" data-album="captions" ${b.album.captions ? "checked" : ""} /> Show captions</label>
          <label class="check"><input type="checkbox" data-album="shuffle" ${b.album.shuffle ? "checked" : ""} /> Shuffle</label>`;
      }
      if (b.kind === "video") {
        return `<p class="setting-note">${b.video.asset ? "A video is chosen." : "No video yet."} MP4 or WebM, up to 50 MB;
            it plays muted and loops. The poster shows while it loads, and instead of it for reduced motion.</p>
          <label class="btn btn-secondary btn-small upload">${b.video.asset ? "Replace video" : "Upload video"}
            <input type="file" accept="video/mp4,video/webm" data-upload="video" hidden /></label>
          <label class="btn btn-secondary btn-small upload">${b.video.poster ? "Replace poster" : "Upload poster"}
            <input type="file" accept="image/png,image/jpeg,image/webp" data-upload="poster" hidden /></label>`;
      }
      if (b.kind === "animation") {
        return `<p class="setting-note">${b.animation.asset ? "An animation is chosen." : "No animation yet."}
            An animated SVG, up to 2 MB. It plays as an image, so nothing in it can run a script.</p>
          <label class="btn btn-secondary btn-small upload">${b.animation.asset ? "Replace" : "Upload"} SVG
            <input type="file" accept="image/svg+xml,.svg" data-upload="animation" hidden /></label>`;
      }
      if (b.kind === "preset") {
        return `<label class="field">Motion<select data-backdrop="preset">${PRESETS.map(([v, l]) =>
          `<option value="${v}" ${b.preset === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
      }
      return '<p class="setting-note">The sign-in card on the site\'s own background.</p>';
    }

    function render() {
      container.innerHTML = `
        <div class="appearance-status pipeline-status" hidden></div>
        <div class="appearance-layout">
          <div class="appearance-form">
            <div class="setting-card">
              <div class="setting-card-head"><h4>Sign-in card</h4></div>
              <label class="field">Heading<input data-look="tagline" maxlength="80" value="${esc(look.tagline)}" /></label>
              <label class="field">Welcome<textarea data-look="welcome" rows="2" maxlength="280">${esc(look.welcome)}</textarea></label>
              <label class="field">Footer or credit line<input data-look="footer" maxlength="200" value="${esc(look.footer)}" /></label>
            </div>
            <div class="setting-card">
              <div class="setting-card-head"><h4>Logo and icon</h4></div>
              <div class="asset-pick">
                ${look.logo_asset ? `<img class="asset-thumb" src="${assetUrl(look.logo_asset)}" alt="Logo" />` : '<span class="setting-note">The Incipit mark</span>'}
                <label class="btn btn-secondary btn-small upload">Upload logo
                  <input type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml,.svg" data-upload="logo" hidden /></label>
                ${look.logo_asset ? '<button type="button" class="btn btn-ghost btn-small" data-clear="logo_asset">Use the default</button>' : ""}
              </div>
              <div class="asset-pick">
                ${look.favicon_asset ? `<img class="asset-thumb small" src="${assetUrl(look.favicon_asset)}" alt="Icon" />` : '<span class="setting-note">The default tab icon</span>'}
                <label class="btn btn-secondary btn-small upload">Upload tab icon
                  <input type="file" accept="image/png,image/svg+xml,.svg" data-upload="favicon" hidden /></label>
                ${look.favicon_asset ? '<button type="button" class="btn btn-ghost btn-small" data-clear="favicon_asset">Use the default</button>' : ""}
              </div>
            </div>
            <div class="setting-card">
              <div class="setting-card-head"><h4>Colour and shape</h4></div>
              <div class="field-row">
                <label class="field">Accent, light<input type="color" data-look="accent_light" value="${esc(look.accent_light)}" />
                  <small class="contrast" data-contrast="light">${esc(ratioNote("light"))}</small></label>
                <label class="field">Accent, dark<input type="color" data-look="accent_dark" value="${esc(look.accent_dark)}" />
                  <small class="contrast" data-contrast="dark">${esc(ratioNote("dark"))}</small></label>
                <label class="field">Surfaces<select data-look="surface">
                  ${["neutral", "warm", "cool"].map((s) => `<option value="${s}" ${look.surface === s ? "selected" : ""}>${s[0].toUpperCase() + s.slice(1)}</option>`).join("")}
                </select></label>
              </div>
              <label class="field">Corner radius <span data-radius-value>${esc(look.radius)}px</span>
                <input type="range" min="0" max="24" data-look="radius" value="${esc(look.radius)}" /></label>
              <div class="field-row">
                <label class="field">Headings<select data-look="heading_font">${fontOptions(look.heading_font)}</select></label>
                <label class="field">Text<select data-look="body_font">${fontOptions(look.body_font)}</select></label>
              </div>
              ${owner ? `<label class="btn btn-secondary btn-small upload">Upload a WOFF2 font
                <input type="file" accept="font/woff2,.woff2" data-upload="font" hidden /></label>` : ""}
            </div>
            <div class="setting-card">
              <div class="setting-card-head"><h4>Sign-in backdrop</h4></div>
              <div class="backdrop-kinds" role="radiogroup" aria-label="Backdrop">
                ${BACKDROPS.map(([value, label]) => `<label class="check"><input type="radio" name="backdrop-kind" value="${value}"
                  ${look.backdrop.kind === value ? "checked" : ""} /> ${label}</label>`).join("")}
              </div>
              <div class="backdrop-controls">${backdropControls()}</div>
            </div>
            ${owner ? `<div class="setting-card">
              <div class="setting-card-head"><h4>Custom CSS</h4><span class="setting-badge">Owner</span></div>
              <p class="setting-note">Added last to every page. Imports, scripts and files from other sites are removed.</p>
              <textarea data-look="custom_css" rows="6" spellcheck="false" class="code">${esc(look.custom_css)}</textarea>
            </div>` : ""}
            <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-save-look>Save appearance</button></div>
          </div>
          <aside class="appearance-preview" aria-label="Preview of the sign-in page">
            <div class="preview-stage">
              <div class="site-backdrop preview-backdrop"></div>
              <div class="preview-card" style="--accent:${esc(look.accent_light)}">
                ${look.logo_asset ? `<img src="${assetUrl(look.logo_asset)}" alt="" class="preview-logo" />` : '<img src="/logo.svg" alt="" class="preview-logo" />'}
                <div class="preview-eyebrow">${esc(data.product_name)}</div>
                <div class="preview-title">${esc(look.tagline)}</div>
                <div class="preview-sub">${esc(look.welcome)}</div>
                <div class="preview-field"></div><div class="preview-field"></div>
                <div class="preview-button">Sign in</div>
              </div>
            </div>
            <p class="setting-note">Unsaved changes show here first.</p>
          </aside>
        </div>`;
      drawPreview();
      wire();
    }

    function drawPreview() {
      if (stopPreview) stopPreview();
      const host = container.querySelector(".preview-backdrop");
      if (host && window.drawBackdrop) {
        const photos = album().map((a) => ({ url: a.url, caption: a.caption, credit: a.credit }));
        stopPreview = window.drawBackdrop({ appearance: look, album: photos }, host);
      }
    }

    async function upload(kind, files) {
      for (const file of files) {
        const form = new FormData();
        form.append("kind", kind === "poster" ? "album_image" : kind);
        form.append("file", file);
        try {
          const added = await api("POST", "/api/site/assets", form);
          assets.push(added);
          if (kind === "logo") look.logo_asset = added.id;
          if (kind === "favicon") look.favicon_asset = added.id;
          if (kind === "album_image") look.backdrop.album.assets.push(added.id);
          if (kind === "video") look.backdrop.video.asset = added.id;
          if (kind === "poster") look.backdrop.video.poster = added.id;
          if (kind === "animation") look.backdrop.animation.asset = added.id;
          if (added.removed && added.removed.length) status(container, `Stored; removed for safety: ${added.removed.join(", ")}`);
          else status(container, `${file.name} uploaded. Save to keep the change.`);
        } catch (error) {
          status(container, `${file.name}: ${error.message}`, true);
        }
      }
      render();
    }

    function wire() {
      container.querySelectorAll("[data-look]").forEach((input) => input.addEventListener("input", () => {
        const key = input.dataset.look;
        look[key] = key === "radius" ? Number(input.value)
          : (key.endsWith("_font") && /^\d+$/.test(input.value) ? Number(input.value) : input.value);
        if (key === "radius") container.querySelector("[data-radius-value]").textContent = `${input.value}px`;
        if (key.startsWith("accent_") || key === "surface") {
          ["light", "dark"].forEach((theme) => {
            container.querySelector(`[data-contrast="${theme}"]`).textContent = ratioNote(theme);
          });
          container.querySelector(".preview-card").style.setProperty("--accent", look.accent_light);
        }
        if (["tagline", "welcome"].includes(key)) {
          container.querySelector(key === "tagline" ? ".preview-title" : ".preview-sub").textContent = input.value;
        }
      }));
      container.querySelectorAll("[data-backdrop]").forEach((input) => input.addEventListener("input", () => {
        look.backdrop[input.dataset.backdrop] = input.value;
        drawPreview();
      }));
      container.querySelectorAll("[data-album]").forEach((input) => input.addEventListener("change", () => {
        const key = input.dataset.album;
        look.backdrop.album[key] = input.type === "checkbox" ? input.checked
          : (key === "interval" ? Number(input.value) : input.value);
        drawPreview();
      }));
      container.querySelectorAll('input[name="backdrop-kind"]').forEach((radio) => radio.addEventListener("change", () => {
        look.backdrop.kind = radio.value;
        render();
      }));
      container.querySelectorAll("[data-upload]").forEach((input) => input.addEventListener("change", () => {
        if (input.files.length) upload(input.dataset.upload, [...input.files]);
      }));
      container.querySelectorAll("[data-clear]").forEach((button) => button.addEventListener("click", () => {
        look[button.dataset.clear] = null;
        render();
      }));
      container.querySelectorAll("[data-caption], [data-credit]").forEach((input) => input.addEventListener("change", async () => {
        const id = Number(input.dataset.caption || input.dataset.credit);
        const field = input.dataset.caption ? "caption" : "credit";
        try {
          const updated = await api("PATCH", `/api/site/assets/${id}`, { [field]: input.value });
          assets = assets.map((a) => (a.id === id ? updated : a));
          drawPreview();
        } catch (error) { status(container, error.message, true); }
      }));
      container.querySelectorAll("[data-remove-asset]").forEach((button) => button.addEventListener("click", async () => {
        const id = Number(button.dataset.removeAsset);
        if (!window.confirm("Remove this photograph from the site?")) return;
        try {
          await api("DELETE", `/api/site/assets/${id}`);
          assets = assets.filter((a) => a.id !== id);
          look.backdrop.album.assets = look.backdrop.album.assets.filter((a) => a !== id);
          render();
        } catch (error) { status(container, error.message, true); }
      }));
      wireAlbumDrag();
      container.querySelector("[data-save-look]").addEventListener("click", save);
    }

    function wireAlbumDrag() {
      let dragged = null;
      container.querySelectorAll(".album-item").forEach((item) => {
        item.addEventListener("dragstart", () => { dragged = item; item.classList.add("dragging"); });
        item.addEventListener("dragend", () => { item.classList.remove("dragging"); dragged = null; });
        item.addEventListener("dragover", (event) => {
          event.preventDefault();
          if (!dragged || dragged === item) return;
          const after = event.clientX > item.getBoundingClientRect().left + item.offsetWidth / 2;
          item.parentElement.insertBefore(dragged, after ? item.nextSibling : item);
        });
        item.addEventListener("drop", async (event) => {
          event.preventDefault();
          const order = [...container.querySelectorAll(".album-item")].map((li) => Number(li.dataset.asset));
          look.backdrop.album.assets = order;
          try { await api("POST", "/api/site/assets/order", { ids: order }); } catch (error) {
            status(container, error.message, true);
          }
          drawPreview();
        });
      });
    }

    async function save() {
      try {
        const saved = await api("PUT", "/api/site/settings", { appearance: look });
        data = saved;
        look = JSON.parse(JSON.stringify(saved.appearance));
        assets = saved.assets;
        render();
        status(container, saved.removed && saved.removed.length
          ? `Saved. Removed from the custom CSS: ${saved.removed.join(", ")}` : "Saved.");
        refreshSiteCss();
        if (opts.onSaved) opts.onSaved();
      } catch (error) {
        status(container, error.message, true);
      }
    }

    render();
    return () => { if (stopPreview) stopPreview(); };
  }

  window.SiteEditors = { mountSite, mountAppearance };
})();
