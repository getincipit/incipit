/* Completion exports have one UI owner. Files stay on the host, never in a download link. */
(() => {
  "use strict";
  const running = new Map();
  const pending = [];
  const prompted = new Set();
  let showingPrompt = false;
  let dismissTimer = null;
  let hideTimer = null;
  const successDuration = 5000;
  const fadeDuration = 250;
  const status = document.createElement("div");
  status.className = "host-export-status hidden";
  status.setAttribute("role", "status");
  document.body.appendChild(status);
  async function request(url, options) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    let response;
    try { response = await fetch(url, {...options, signal: controller.signal}); }
    finally { clearTimeout(timeout); }
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Export failed");
    return body;
  }
  function say(message, failed = false, dismiss = false) {
    clearTimeout(dismissTimer);
    clearTimeout(hideTimer);
    dismissTimer = hideTimer = null;
    status.textContent = message;
    status.classList.remove("hidden", "is-fading");
    status.classList.toggle("is-error", failed);
    if (dismiss) {
      dismissTimer = setTimeout(() => {
        status.classList.add("is-fading");
        hideTimer = setTimeout(() => {
          status.classList.add("hidden");
          status.textContent = "";
        }, fadeDuration);
      }, successDuration);
    }
  }
  async function start(folder) {
    const key = `${folder.workflow}:${folder.folder_id}`;
    if (running.has(key)) return;
    running.set(key, true);
    say(`Exporting ${folder.folder_display_id || folder.folder_id} to the host…`);
    try {
      const job = await request(`/api/folder-exports/${encodeURIComponent(folder.workflow)}/${encodeURIComponent(folder.folder_id)}`, {method: "POST"});
      const deadline = Date.now() + 15 * 60 * 1000;
      let result;
      do {
        await new Promise(resolve => setTimeout(resolve, 1500));
        result = await request(`/api/folder-exports/jobs/${encodeURIComponent(job.job_id)}`);
        if (Date.now() > deadline && ["queued", "running"].includes(result.state)) {
          throw new Error("The host is still processing. Choose Export to resume checking this job; it will not start a duplicate.");
        }
      } while (result.state === "queued" || result.state === "running");
      if (result.state !== "completed") throw new Error(result.detail || "Export failed; try again");
      say(`Export completed. ${result.detail}`, false, true);
      window.dispatchEvent(new CustomEvent("folder-export-completed", {detail: folder}));
    } catch (error) {
      say(`Export could not be completed: ${error.message}`, true);
    } finally {
      running.delete(key);
    }
  }
  function prompt(folder) {
    if (!folder || document.body.classList.contains("role-reviewer")) return;
    const key = `${folder.workflow}:${folder.folder_id}`;
    if (prompted.has(key)) return;
    if (showingPrompt) { pending.push(folder); return; }
    prompted.add(key);
    showingPrompt = true;
    const previous = document.activeElement;
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="completion-export-title">
      <h3 id="completion-export-title">Export now?</h3>
      <p>Every document in this folder has now been reviewed.</p>
      <p class="completion-folder"></p>
      <p>Save to the host and its configured backup and mirror. Nothing downloads to this device.</p>
      <div class="modal-actions"><button type="button" class="btn btn-accept" data-export>Export</button>
      <button type="button" class="btn btn-ghost" data-later>Not yet</button></div></div>`;
    overlay.querySelector(".completion-folder").textContent = folder.folder_display_id || folder.folder_id;
    const close = () => {
      overlay.remove(); previous?.focus(); prompted.delete(key); showingPrompt = false;
      if (pending.length) prompt(pending.shift());
    };
    overlay.querySelector("[data-later]").onclick = close;
    overlay.querySelector("[data-export]").onclick = () => { close(); start(folder); };
    overlay.addEventListener("click", event => { if (event.target === overlay) close(); });
    overlay.addEventListener("keydown", event => {
      if (event.key === "Escape") close();
      if (event.key === "Tab") {
        event.preventDefault();
        const buttons = [...overlay.querySelectorAll("button")];
        const current = buttons.indexOf(document.activeElement);
        buttons[(current + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
      }
    });
    document.body.appendChild(overlay);
    overlay.querySelector("[data-export]").focus();
  }
  window.__folderExports__ = {prompt, start};
})();
