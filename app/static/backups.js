/* Settings → Backups: where backups go and a second copy, the last result, backing up now, and the
   snapshots kept, any of which the owner can restore into a new folder. */
(function () {
  "use strict";

  const { esc, api, remount } = window.pageKit;

  function ago(iso) {
    if (!iso) return "never";
    const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  const size = (n) => (n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

  async function mount(container, options) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    const opts = options || {};
    let state = await api("GET", "/api/backups");
    let restoring = null;
    let polling = null;

    const status = (message, isError) => {
      const box = container.querySelector(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };

    function draw() {
      if (!state.configured) {
        container.innerHTML = `<p class="setting-note">This site is started by hand, not from a data folder, so its backups are set where it is started.</p>`;
        return;
      }
      const last = state.last || {};
      const owner = !!opts.owner;
      const env = state.set_by_environment || {};
      const stale = !last.last_success_at || Date.now() - new Date(last.last_success_at).getTime() > 86400000;
      container.innerHTML = `
        <div class="access-panel">
          <div class="appearance-status pipeline-status" hidden></div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>The last backup</h4></div>
            <p class="setting-note access-net is-${state.running ? "searching" : last.ok === false || stale ? "refused" : "open"}">
              ${state.running ? "Backing up now…" : `Last good backup ${esc(ago(last.last_success_at))}.`}
              ${last.ok === false && last.detail ? ` The last attempt failed: ${esc(last.detail)}` : ""}</p>
            <p class="setting-note">The site backs itself up while it runs: the scans, records and exports every two minutes, and
              the database every ten minutes when something has changed.</p>
            <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-run ${state.running ? "disabled" : ""}>Back up now</button></div>
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Where backups go</h4></div>
            <label class="field">Backups folder<input name="destination" value="${esc(state.destination)}" spellcheck="false" autocapitalize="off"
              ${owner && !env.destination ? "" : "disabled"}></label>
            ${env.destination ? `<p class="setting-note">Set by ${esc(env.destination)} where the site is started; change it there.</p>` : ""}
            ${state.same_drive ? `<p class="setting-note access-warning">The backups are on the same drive as the data. They protect against mistakes and
              damaged files, but not against that drive failing. Choose a folder on another drive, such as an external disk.</p>` : ""}
            ${state.available ? "" : `<p class="setting-note access-warning">This folder is not available right now; reconnect its drive.</p>`}
            <label class="field">A second copy (optional)<input name="second_copy" value="${esc(state.second_copy)}" placeholder="F:\\Incipit Backups"
              spellcheck="false" autocapitalize="off" ${owner && !env.second_copy ? "" : "disabled"}></label>
            ${env.second_copy ? `<p class="setting-note">Set by ${esc(env.second_copy)} where the site is started; change it there.</p>` : ""}
            <p class="setting-note">The second copy repeats every backup in another place, for example a network share or another disk.</p>
            ${owner ? `<div class="settings-form-actions"><button type="button" class="btn btn-accept" data-save>Save</button></div>` : ""}
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Snapshots kept</h4></div>
            <p class="setting-note">Every snapshot from the last six hours, then one an hour for two days, one a day for a month and one a
              week for half a year. Restoring builds a new data folder from a snapshot; the current one is left as it is.</p>
            ${state.snapshots.length ? `<ul class="backup-list">${state.snapshots.map((s) => `
              <li class="backup-item">
                <span class="backup-when">${esc(when(s.at))}</span>
                <span class="backup-size">${esc(size(s.bytes))}${s.manifest ? "" : " · no manifest"}</span>
                ${owner ? `<button type="button" class="btn btn-secondary btn-small" data-restore="${esc(s.name)}">Restore…</button>` : ""}
                ${restoring === s.name ? `<div class="backup-restore">
                  <label class="field">Restore into a new, empty folder<input name="restore_target" placeholder="D:\\Incipit Restored" spellcheck="false" autocapitalize="off"></label>
                  <p class="setting-note">The site restarts, builds that folder from this snapshot and runs from it. Your team is signed out for a minute.</p>
                  <div class="settings-form-actions access-actions">
                    <button type="button" class="btn btn-secondary btn-small" data-restore-cancel>Cancel</button>
                    <button type="button" class="btn btn-accept" data-restore-go="${esc(s.name)}">Restore</button></div></div>` : ""}
              </li>`).join("")}</ul>` : `<p class="setting-note">No snapshots yet. The first is made about a minute after the site starts.</p>`}
          </div>
        </div>`;
    }

    function pollWhileRunning() {
      if (polling || !state.running) return;
      polling = setInterval(async () => {
        // A later mount owns the container now.
        if (signal.aborted) { clearInterval(polling); polling = null; return; }
        try {
          state = await api("GET", "/api/backups");
          if (!state.running) { clearInterval(polling); polling = null; draw(); status("Backed up."); }
        } catch { /* the next tick tries again */ }
      }, 1500);
    }

    async function waitForSite() {
      status("Restoring. This page reloads when the site is back…");
      await new Promise((r) => setTimeout(r, 2500));
      for (let i = 0; i < 900; i += 1) {
        try { if ((await fetch("/health", { cache: "no-store" })).status < 500) { window.location.reload(); return; } } catch { /* restarting */ }
        await new Promise((r) => setTimeout(r, 1000));
      }
      status("The site has not come back yet. Check the Incipit icon on the host computer.", true);
    }

    on("click", async (e) => {
      if (e.target.closest("[data-run]")) {
        try { state = await api("POST", "/api/backups/run"); draw(); pollWhileRunning(); } catch (error) { status(error.message, true); }
        return;
      }
      if (e.target.closest("[data-save]")) {
        try {
          const env = state.set_by_environment || {};
          const body = {};
          if (!env.destination) body.destination = container.querySelector('[name="destination"]').value;
          if (!env.second_copy) body.second_copy = container.querySelector('[name="second_copy"]').value;
          state = await api("PUT", "/api/backups", body);
          draw();
          status("Saved. The next backup goes there.");
        } catch (error) { status(error.message, true); }
        return;
      }
      const opener = e.target.closest("[data-restore]");
      if (opener) { restoring = opener.dataset.restore; draw(); container.querySelector('[name="restore_target"]')?.focus(); return; }
      if (e.target.closest("[data-restore-cancel]")) { restoring = null; draw(); return; }
      const go = e.target.closest("[data-restore-go]");
      if (go) {
        try {
          const answer = await api("POST", "/api/backups/restore", { snapshot: go.dataset.restoreGo,
            target: container.querySelector('[name="restore_target"]').value });
          if (answer.restarting) waitForSite();
          else status(`The restore into ${answer.restore_to} happens the next time the site starts.`);
        } catch (error) { status(error.message, true); }
      }
    });

    draw();
    pollWhileRunning();
  }

  window.BackupsPanel = { mount };
})();
