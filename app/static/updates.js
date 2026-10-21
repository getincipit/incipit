/* Settings → Updates: the version running, checking for a newer one, its notes and installing it
   (the owner's decision), and the support bundle. */
(function () {
  "use strict";

  const { esc, api, remount } = window.pageKit;

  async function mount(container, options) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    const opts = options || {};
    let state = await api("GET", "/api/updates");

    const status = (message, isError) => {
      const box = container.querySelector(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };

    function draw() {
      const release = state.release;
      const words = {
        unchecked: "Not checked yet.",
        up_to_date: "Incipit is up to date.",
        available: release ? `Incipit ${release.version} is ready to install.` : "",
        unreachable: `Could not check for updates (${state.error}). The site carries on as it is.`,
        not_configured: "Updates are not set up in this build; new versions are installed by hand.",
      };
      const tone = state.state === "available" ? "searching" : state.state === "up_to_date" ? "open" : "off";
      container.innerHTML = `
        <div class="access-panel">
          <div class="appearance-status pipeline-status" hidden></div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Updates</h4></div>
            <p class="setting-note">This is Incipit ${esc(state.current)}.${state.checked_at ? ` Last checked ${esc(new Date(state.checked_at).toLocaleString())}.` : ""}</p>
            <p class="setting-note access-net is-${tone}">${esc(words[state.state] || state.state)}</p>
            ${state.state === "available" && release ? `
              ${release.notes ? `<div class="update-notes">${esc(release.notes)}</div>` : ""}
              <p class="setting-note">Installing stops the site for a minute or two while the program is replaced; the data
                folder and every setting are kept, and the site comes back by itself. Anyone working will be signed out.</p>` : ""}
            <div class="settings-form-actions access-actions">
              ${state.feed ? `<button type="button" class="btn btn-secondary btn-small" data-check>Check now</button>` : ""}
              ${state.state === "available" && opts.owner ? `<button type="button" class="btn btn-accept" data-install>Install ${esc(release.version)}</button>` : ""}
            </div>
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Support bundle</h4></div>
            <p class="setting-note">A zip of the versions, the settings, the backup status and the recent logs, for someone
              helping with a problem. Keys, tokens and e-mail addresses are masked, and it holds no scans, transcriptions or
              passwords. Look through it before sending it to anyone.</p>
            <div class="settings-form-actions"><a class="btn btn-secondary btn-small" href="/api/support/bundle" download>Download a support bundle</a></div>
          </div>
        </div>`;
    }

    async function waitForNewVersion(version) {
      status(`Installing ${version}. This page reloads when the site is back…`);
      await new Promise((r) => setTimeout(r, 5000));
      for (let i = 0; i < 900; i += 1) {
        try {
          const health = await (await fetch("/health", { cache: "no-store" })).json();
          if (health.commit === version) { window.location.reload(); return; }
        } catch { /* the program is being replaced */ }
        await new Promise((r) => setTimeout(r, 2000));
      }
      status("The site has not come back yet. Check the Incipit icon on the host computer.", true);
    }

    on("click", async (e) => {
      if (e.target.closest("[data-check]")) {
        status("Checking…");
        try { state = await api("POST", "/api/updates/check"); draw(); status(""); } catch (error) { status(error.message, true); }
        return;
      }
      if (e.target.closest("[data-install]")) {
        if (!window.confirm(`Install Incipit ${state.release.version} now? The site stops for a minute or two.`)) return;
        status("Downloading and checking the installer…");
        try {
          const answer = await api("POST", "/api/updates/install");
          waitForNewVersion(answer.installing);
        } catch (error) { status(error.message, true); }
      }
    });

    draw();
  }

  window.UpdatesPanel = { mount };
})();
