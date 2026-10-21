/* Settings → Access and the wizard's access step: the links a team uses to reach this host, the
   port, start at login, a test of the port from the host, opening the port on the router, and moving
   the data folder. */
(function () {
  "use strict";

  const { esc, api, remount } = window.pageKit;

  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      const was = button.textContent;
      button.textContent = "Copied";
      button.classList.add("is-done");
      setTimeout(() => { button.textContent = was; button.classList.remove("is-done"); }, 1400);
    } catch { window.prompt("Copy this address:", text); }
  }

  function linkRows(access) {
    const words = { site: "The site's name on your network", network: "On the same network",
                    name: "By this computer's name", local: "On this computer only" };
    return access.links.map((l) => `
      <div class="access-link" data-kind="${esc(l.kind)}">
        <span class="access-link-kind">${esc(words[l.kind] || l.kind)}</span>
        <code class="access-link-url">${esc(l.link)}</code>
        <button type="button" class="btn btn-secondary btn-small" data-copy="${esc(l.link)}">Copy</button>
      </div>`).join("");
  }

  async function mount(container, options) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    const opts = options || {};
    let access = await api("GET", "/api/access");
    let refreshing = null;

    function draw() {
      const login = access.start_at_login;
      container.innerHTML = `
        <div class="access-panel">
          <div class="appearance-status pipeline-status" hidden></div>
          ${access.private ? `<p class="setting-note access-warning">The site is bound to <code>${esc(access.bind)}</code>, so only this
            computer can open it. To let the team in, bind it to every interface (0.0.0.0) where it is started.</p>` : ""}
          <div class="setting-card">
            <div class="setting-card-head"><h4>The address to share</h4></div>
            <p class="setting-note">Anyone on the same network opens one of these in a browser and signs in. This computer must be on.</p>
            ${access.addresses.length ? "" : `<p class="setting-note">No network address was found; connect this computer to the network and reopen this page.</p>`}
            ${linkRows(access)}
            ${access.local_name.error ? `<p class="setting-note access-warning">${esc(access.local_name.error)}</p>` : ""}
            <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-test>Test the port from this computer</button></div>
            <ul class="access-results" data-results hidden></ul>
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Name, port and starting at login</h4></div>
            ${access.configured ? `
              <label class="field">Site name
                <span class="access-name-field"><span class="access-name-scheme">http://</span><input name="site_name" maxlength="63"
                  value="${esc(access.site_name)}" placeholder="our-archive" ${opts.owner ? "" : "disabled"} spellcheck="false" autocapitalize="off"><span
                  class="access-name-suffix">.local:${esc(access.port)}</span></span></label>
              <p class="setting-note">Letters, digits and hyphens. Windows, Macs, iPhones and iPads on the same network open this name
                with nothing set up; a change is announced at once. Leave it empty to share the IP address only.</p>
              <div class="field-row">
                <label class="field">Port<input name="port" inputmode="numeric" value="${esc(access.port)}" ${opts.owner ? "" : "disabled"}></label>
                <label class="field">HTTPS port<input name="https_port" inputmode="numeric" value="${esc(access.https.port)}" ${opts.owner ? "" : "disabled"}></label>
                <label class="field">Start the site when you log in
                  <span class="tab-switch"><input type="checkbox" name="start_at_login" ${login.registered ? "checked" : ""} ${opts.owner && login.supported ? "" : "disabled"}>
                  <span class="tab-switch-track"></span><span class="tab-switch-word">${login.registered ? "On" : "Off"}</span></span></label>
              </div>
              <p class="setting-note">A new port takes effect the next time the site starts. Data folder: <code>${esc(access.data_folder)}</code></p>
              ${login.supported ? `<p class="setting-note">At login it runs: <code>${esc(login.command)}</code></p>` : `<p class="setting-note">Starting at login is set up by the installer on this system.</p>`}
              ${opts.owner ? `<div class="settings-form-actions"><button type="button" class="btn btn-accept" data-save>Save</button></div>` : ""}`
            : `<p class="setting-note">This site is started by hand (not from a data folder), so its port is set where it is started: <code>${esc(access.bind)}:${esc(access.port)}</code>.</p>`}
          </div>
          ${secureCard(access)}
          ${internetCard(access, opts)}
          ${opts.owner && access.configured ? `<div class="setting-card" data-storage><div class="setting-card-head"><h4>Where the data lives</h4></div>
            <p class="setting-note">Reading the folder…</p></div>` : ""}
        </div>`;
      if (opts.owner && access.configured) drawStorage();
      // While the tunnel connects or the team's page waits on DNS, the page asks again.
      const busy = ["starting", "standby"].includes(access.tunnel.state) && access.tunnel.mode === "on"
        || access.fixed_link.state === "resolving";
      if (busy && !refreshing) {
        refreshing = setTimeout(async () => {
          refreshing = null;
          // A later mount owns the container now.
          if (signal.aborted) return;
          // Never while someone is typing in a field the redraw would clear.
          const typing = container.contains(document.activeElement) && document.activeElement.matches("input:not([type=checkbox])");
          try { access = await api("GET", "/api/access"); if (!typing) draw(); } catch { /* the next draw tries again */ }
        }, 3000);
      }
    }

    // What the team is told before they first open the https address, so the warning does not alarm them.
    function teamMessage(a) {
      const link = a.internet.link || (a.fixed_link.enabled && a.fixed_link.url) || a.tunnel.link || a.https.links[0] || "";
      return `Our Incipit site: ${link}

The first time you open it, your browser will warn "Your connection is not private" (Safari: "This
connection is not private"). That is expected: the site made its own security certificate instead
of buying one, so no company vouches for it. The connection is still encrypted.

Before you continue, check it is really our site: click "Not secure" or the warning's details,
view the certificate, and compare its SHA-256 fingerprint with this one:
${a.https.fingerprint}

If they match, choose Advanced, then Proceed (Chrome, Edge), Accept the Risk and Continue (Firefox),
or Show Details, then visit this website (Safari). If they do not match, do not sign in; tell me.

To stop the warning for good, download the certificate from ${link}certificate.crt and install it
as trusted on your computer.`;
    }

    function secureCard(a) {
      const h = a.https;
      return `<div class="setting-card">
        <div class="setting-card-head"><h4>The secure address (HTTPS)</h4></div>
        ${h.serving ? `<p class="setting-note">The same site, encrypted, on port <code>${esc(h.port)}</code>. This is the address the router
            opens to the internet; on your own network either address works.</p>
          ${h.links.slice(0, 2).map((l) => `<div class="access-link" data-kind="https"><span class="access-link-kind">Encrypted</span>
            <code class="access-link-url">${esc(l)}</code>
            <button type="button" class="btn btn-secondary btn-small" data-copy="${esc(l)}">Copy</button></div>`).join("")}
          <p class="setting-note">The site made its own certificate, so browsers warn once before they open it. The team checks
            this fingerprint against the one the browser shows, then continues:</p>
          <code class="access-fingerprint">${esc(h.fingerprint).replaceAll(":", ":<wbr>")}</code>
          <p class="setting-note">Valid until ${esc(h.expires)}. Installing the certificate on a computer stops the warning there.</p>
          <div class="settings-form-actions access-actions">
            <a class="btn btn-secondary btn-small" href="${esc(h.certificate_url)}" download>Download the certificate</a>
            <button type="button" class="btn btn-secondary btn-small" data-copy-message>Copy a message for the team</button>
          </div>` : `<p class="setting-note access-warning">HTTPS is not running${h.error ? `: ${esc(h.error)}` : ""}. Choose another
            HTTPS port below and restart the site.</p>`}
      </div>`;
    }

    // What the router said, in words the owner can act on.
    function internetCard(a, o) {
      const net = a.internet;
      const words = {
        off: "Off. The site is reached on this network only.",
        searching: "Asking the router to open the port…",
        open: "Open. Anyone with the address below can reach the sign-in page.",
        no_router: "No router answered. Turn on UPnP in the router's settings, or forward the port there by hand.",
        refused: "The router refused to open the port. Turn on UPnP in its settings, or forward the port there by hand.",
        double_nat: "The router opened the port, but another network sits in front of it, so the internet still cannot reach the site.",
        no_https: "Nothing is opened: only the HTTPS address goes to the internet, and HTTPS is not running.",
      };
      return `<div class="setting-card">
        <div class="setting-card-head"><h4>Reaching the site from the internet</h4></div>
        ${a.configured ? `<label class="field">Open the port on the router automatically
          <span class="tab-switch"><input type="checkbox" name="forward_port" ${net.enabled ? "checked" : ""} ${o.owner && !a.private ? "" : "disabled"}>
          <span class="tab-switch-track"></span><span class="tab-switch-word">${net.enabled ? "On" : "Off"}</span></span></label>` : ""}
        <p class="setting-note access-net is-${esc(net.state)}">${esc(words[net.state] || net.state)}${net.error && net.state !== "no_router" ? ` (${esc(net.error)})` : ""}</p>
        ${net.link ? `<div class="access-link" data-kind="internet"><span class="access-link-kind">From anywhere</span>
          <code class="access-link-url">${esc(net.link)}</code>
          <button type="button" class="btn btn-secondary btn-small" data-copy="${esc(net.link)}">Copy</button></div>
          <p class="setting-note">The internet address can change when the router restarts; this page always shows the current one.</p>` : ""}
        ${net.state === "double_nat" ? `<p class="setting-note">Ask your internet provider for a public address, or put the
          provider's modem in bridge mode, then reopen this page.</p>` : ""}
        ${net.state === "no_router" || net.state === "refused" ? `<p class="setting-note">By hand: forward TCP port
          <code>${esc(a.port)}</code> to this computer's address <code>${esc(a.addresses[0] || "…")}</code>.</p>` : ""}
        ${tunnelSection(a, o)}
      </div>`;
    }

    // The Cloudflare quick tunnel: what the router cannot open, reached through an outbound connection.
    function tunnelSection(a, o) {
      const t = a.tunnel;
      const words = {
        off: "Off.",
        standby: t.mode === "auto" ? "Not needed: the router gives the site an internet address, or is still being asked." : "Starting…",
        starting: "Connecting to Cloudflare…",
        open: "Open. Anyone with the address below can reach the sign-in page.",
        stopped: "The tunnel stopped; it is started again on its own.",
        missing: "The tunnel program is missing; reinstall Incipit to restore it.",
      };
      const choice = (mode, label) => `<button type="button" class="pb-segment${t.mode === mode ? " active" : ""}"
        data-tunnel="${mode}" ${o.owner && a.configured ? "" : "disabled"}>${label}</button>`;
      return `<div class="access-tunnel">
        <span class="field">Free Cloudflare tunnel, for networks the router cannot open</span>
        <div class="pb-segments" role="group" aria-label="Cloudflare tunnel">${choice("off", "Off")}${choice("auto", "When needed")}${choice("on", "Always")}</div>
        <p class="setting-note access-net is-${esc(t.state === "standby" ? "off" : t.state === "stopped" ? "searching" : t.state)}">${esc(words[t.state] || t.state)}${t.error && t.state !== "open" ? ` (${esc(t.error)})` : ""}</p>
        ${t.link ? `<div class="access-link" data-kind="tunnel"><span class="access-link-kind">From anywhere, through Cloudflare</span>
          <code class="access-link-url">${esc(t.link)}</code>
          <button type="button" class="btn btn-secondary btn-small" data-copy="${esc(t.link)}">Copy</button></div>` : ""}
        ${t.mode !== "off" ? fixedLinkSection(a, o) : ""}
        ${t.mode !== "off" ? `<p class="setting-note">No account or payment. Cloudflare carries the traffic and can read it on
          the way; it describes these tunnels as best-effort, with no uptime promise.</p>` : ""}
      </div>`;
    }

    // The permanent page that always leads to the tunnel's current address.
    function fixedLinkSection(a, o) {
      const f = a.fixed_link;
      const words = {
        waiting: "Made the first time the tunnel opens.",
        resolving: "Waiting for the new address to be known on the internet before putting it on the page…",
        current: `Leads to the address above${f.updated ? ` (updated ${f.updated})` : ""}.`,
        unresolved: "The new address was never known on the internet, so the page still shows the last one.",
        failed: "The page could not be updated.",
        off: "Off: share the address above again after each restart.",
      };
      const state = f.enabled ? f.state : "off";
      return `<div class="access-fixed">
        <label class="field">One permanent link for the team
          <span class="tab-switch"><input type="checkbox" name="fixed_link" ${f.enabled ? "checked" : ""} ${o.owner && a.configured ? "" : "disabled"}>
          <span class="tab-switch-track"></span><span class="tab-switch-word">${f.enabled ? "On" : "Off"}</span></span></label>
        ${f.enabled && f.url ? `<div class="access-link" data-kind="fixed"><span class="access-link-kind">The team's link, never changes</span>
          <code class="access-link-url">${esc(f.url)}</code>
          <button type="button" class="btn btn-secondary btn-small" data-copy="${esc(f.url)}">Copy</button></div>` : ""}
        <p class="setting-note access-net is-${esc(state === "current" ? "open" : state === "resolving" ? "searching" : state === "failed" || state === "unresolved" ? "refused" : "off")}">${esc(words[state] || state)}${f.error && f.enabled ? ` (${esc(f.error)})` : ""}</p>
        ${f.enabled ? `<p class="setting-note">A public page on Telegraph (telegra.ph, free, no sign-up) that the site rewrites with its
          new address each time it starts. Only this site can change it. Anyone with the link sees the address, which leads
          to the sign-in page.</p>` : ""}
        ${f.enabled && f.url && o.owner ? `<div class="settings-form-actions access-actions">
          <button type="button" class="btn btn-secondary btn-small" data-replace-link>Replace the link</button></div>` : ""}
      </div>`;
    }

    async function drawStorage() {
      const card = container.querySelector("[data-storage]");
      if (!card) return;
      let s;
      try { s = await api("GET", "/api/access/storage"); } catch (error) { card.querySelector(".setting-note").textContent = error.message; return; }
      const size = (n) => n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 2 ** 20))} MB`;
      card.innerHTML = `<div class="setting-card-head"><h4>Where the data lives</h4></div>
        <p class="setting-note"><code>${esc(s.data_folder)}</code> &middot; ${size(s.size_bytes)} used &middot; ${size(s.free_bytes)} free on that drive</p>
        ${s.move_failed ? `<p class="setting-note access-warning">${esc(s.move_failed)}</p>
          <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-cancel-move>Dismiss</button></div>` : ""}
        ${s.move_to ? `<p class="setting-note">A move to <code>${esc(s.move_to)}</code> happens the next time the site starts.</p>
          <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-cancel-move>Cancel the move</button></div>` : `
          <label class="field">Move everything to
            <input name="move_to" placeholder="D:\\Incipit Data" spellcheck="false" autocapitalize="off"></label>
          <p class="setting-note">A new or empty folder, on any drive this computer keeps connected. The site restarts, copies
            everything, checks the copy and then runs from there; the old folder is left as it was until you delete it.
            The team is signed out for a minute.</p>
          <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-move>Move the data</button></div>`}`;
    }

    // The site restarts to move; the page waits for it and then reloads.
    async function waitForSite() {
      status("Moving the data. This page reloads when the site is back…");
      await new Promise((r) => setTimeout(r, 2500));
      for (let i = 0; i < 600; i += 1) {
        try { if ((await fetch("/health", { cache: "no-store" })).ok) { window.location.reload(); return; } } catch { /* still restarting */ }
        await new Promise((r) => setTimeout(r, 1000));
      }
      status("The site has not come back yet. Check the Incipit icon on the host computer.", true);
    }

    // Applied at once, then the page asks again while the router answers.
    async function forwardChanged(wanted) {
      try {
        access = await api("PUT", "/api/access", { forward_port: wanted });
        draw();
        for (let i = 0; i < 10 && access.internet.state === "searching"; i += 1) {
          await new Promise((r) => setTimeout(r, 1500));
          access = await api("GET", "/api/access");
        }
        draw();
      } catch (error) { status(error.message, true); }
    }

    // Applied at once; the page asks again while cloudflared connects.
    async function tunnelChanged(mode) {
      try {
        access = await api("PUT", "/api/access", { tunnel: mode });
        draw();
        for (let i = 0; i < 20 && mode !== "off" && !access.tunnel.link && access.tunnel.state !== "missing"; i += 1) {
          await new Promise((r) => setTimeout(r, 1500));
          access = await api("GET", "/api/access");
          if (access.tunnel.mode === "auto" && access.internet.state === "open") break;
        }
        draw();
      } catch (error) { status(error.message, true); }
    }

    const status = (message, isError) => {
      const box = container.querySelector(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };

    on("click", async (e) => {
      const copyButton = e.target.closest("[data-copy]");
      if (copyButton) { copy(copyButton.dataset.copy, copyButton); return; }
      if (e.target.closest("[data-replace-link]")) {
        if (!window.confirm("Make a new team link? The old one will stop leading to the site, so everyone needs the new one.")) return;
        try { access = await api("POST", "/api/access/fixed-link/replace"); draw(); status("A new link is being made; copy it from here once it shows."); }
        catch (error) { status(error.message, true); }
        return;
      }
      const tunnelChoice = e.target.closest("[data-tunnel]");
      if (tunnelChoice) { tunnelChanged(tunnelChoice.dataset.tunnel); return; }
      const messageButton = e.target.closest("[data-copy-message]");
      if (messageButton) { copy(teamMessage(access), messageButton); return; }
      if (e.target.closest("[data-test]")) {
        const list = container.querySelector("[data-results]");
        list.hidden = false;
        list.innerHTML = `<li class="setting-note">Testing…</li>`;
        try {
          const { results } = await api("POST", "/api/access/test");
          list.innerHTML = results.map((r) => `<li class="${r.reachable ? "is-ok" : "is-bad"}">
            <span class="access-mark">${r.reachable ? "&#10003;" : "&#10007;"}</span> <code>${esc(r.address)}:${esc(access.port)}</code> &mdash; ${esc(r.detail)}</li>`).join("");
        } catch (error) { list.innerHTML = `<li class="is-bad">${esc(error.message)}</li>`; }
      }
      if (e.target.closest("[data-move]")) {
        const target = container.querySelector('[name="move_to"]').value;
        try {
          const moved = await api("POST", "/api/access/move", { target });
          if (moved.restarting) waitForSite();
          else { status(`The data moves to ${moved.move_to} the next time the site starts.`); drawStorage(); }
        } catch (error) { status(error.message, true); }
        return;
      }
      if (e.target.closest("[data-cancel-move]")) {
        try { await api("DELETE", "/api/access/move"); drawStorage(); status(""); } catch (error) { status(error.message, true); }
        return;
      }
      if (e.target.closest("[data-save]")) {
        const port = container.querySelector('[name="port"]').value;
        const startAtLogin = container.querySelector('[name="start_at_login"]').checked;
        const siteName = container.querySelector('[name="site_name"]').value;
        try {
          const httpsPort = container.querySelector('[name="https_port"]').value;
          access = await api("PUT", "/api/access", { port: Number(port), https_port: Number(httpsPort),
                                                    start_at_login: startAtLogin, site_name: siteName });
          const note = access.restart_needed ? " The new port is used the next time the site starts." : "";
          draw();
          status("Saved." + note);
          if (opts.onSaved) opts.onSaved();
        } catch (error) { status(error.message, true); }
      }
    });
    on("change", (e) => {
      const box = e.target.closest('[name="start_at_login"], [name="forward_port"], [name="fixed_link"]');
      if (box) box.parentElement.querySelector(".tab-switch-word").textContent = box.checked ? "On" : "Off";
      if (box && box.name === "forward_port") forwardChanged(box.checked);
      if (box && box.name === "fixed_link") {
        api("PUT", "/api/access", { fixed_link: box.checked }).then((next) => { access = next; draw(); })
          .catch((error) => status(error.message, true));
      }
    });

    draw();
  }

  window.AccessPanel = { mount };
})();
