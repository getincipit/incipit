/* The first-run wizard. Without an owner it asks for the bootstrap token's owner account; with one,
   it walks the owner through the steps the site has so far and finishes when each is done. */
(function () {
  "use strict";

  const steps = document.getElementById("setup-steps");
  const panel = document.getElementById("setup-panel");
  const token = new URLSearchParams(location.search).get("token") || "";
  const { esc, api } = window.pageKit;
  let stopEditor = null;
  let me = null;


  function drawSteps(state, current) {
    steps.innerHTML = state.steps.map((step, i) => `
      <li class="${step.done ? "is-done" : ""} ${step.id === current ? "is-current" : ""}">
        <button type="button" data-step="${esc(step.id)}" ${step.id === "owner" ? "disabled" : ""}>
          <span class="setup-step-number">${step.done ? "&#10003;" : i + 1}</span>${esc(step.title)}</button>
      </li>`).join("") + `
      <li class="setup-finish"><button type="button" class="btn btn-accept" data-finish
        ${state.can_finish ? "" : "disabled"}>Finish setup</button></li>`;
    steps.querySelectorAll("[data-step]").forEach((button) => button.addEventListener("click", () => open(button.dataset.step)));
    steps.querySelector("[data-finish]").addEventListener("click", finish);
  }

  // ---------- step 1: the owner, created with the bootstrap token ----------

  function ownerForm() {
    steps.innerHTML = `<li class="is-current"><button type="button" disabled>
      <span class="setup-step-number">1</span>Create the owner account</button></li>`;
    panel.innerHTML = `
      <h2>The owner account</h2>
      <p class="setting-note">The owner holds the last word on this site: who administers it, how people join,
        and how it looks. There is exactly one, and only the owner can hand the role on.</p>
      <form class="setup-form" data-owner>
        <label class="field">Full name<input name="display_name" required autocomplete="name" /></label>
        <label class="field">Username<input name="username" required autocomplete="username" /></label>
        <label class="field">Password<input name="password" type="password" required minlength="8" autocomplete="new-password" /></label>
        <p class="auth-error" hidden></p>
        <button type="submit" class="btn btn-accept">Create the owner</button>
      </form>`;
    panel.querySelector("[data-owner]").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(event.target));
      try {
        await api("POST", "/api/setup/owner", { ...form, token });
        // Signed in as the owner now; the rest of the wizard is theirs.
        location.href = "/setup";
      } catch (error) {
        const box = panel.querySelector(".auth-error");
        box.textContent = error.status === 404 ? "This setup link has already been used." : error.message;
        box.hidden = false;
      }
    });
  }

  // ---------- the owner's steps ----------

  async function markDone(step) {
    const state = await api("POST", `/api/setup/steps/${step}`, { done: true });
    const next = state.steps.find((s) => !s.done);
    drawSteps(state, next ? next.id : step);
    if (next) open(next.id);
    else panel.innerHTML = `<h2>Ready</h2><p>Every step is done. Finish setup to open the site.</p>`;
  }

  async function open(step) {
    if (stopEditor) { stopEditor(); stopEditor = null; }
    const state = await api("GET", "/api/setup");
    drawSteps(state, step);
    if (step === "site") {
      panel.innerHTML = `<h2>The site and how it looks</h2>
        <p class="setting-note">Its name, what people are called, and the sign-in page everyone sees first.
          Everything here can be changed later under Settings.</p>
        <div data-site></div><div data-look></div>
        <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-done>This step is done</button></div>`;
      await window.SiteEditors.mountSite(panel.querySelector("[data-site]"), { owner: true });
      stopEditor = await window.SiteEditors.mountAppearance(panel.querySelector("[data-look]"), { owner: true });
    } else if (step === "prompt") {
      panel.innerHTML = `<h2>What the model is told</h2>
        <p class="setting-note">Describe the collection and how it is catalogued. The prompt on the right is exactly what
          the model will read; it can be changed later under Settings, and every change is kept as a version.</p>
        <div data-prompt-builder></div>
        <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-done>This step is done</button></div>`;
      stopEditor = await window.PromptBuilder.mount(panel.querySelector("[data-prompt-builder]"), { wizard: true });
    } else if (step === "model") {
      panel.innerHTML = `<h2>Connect the transcription model</h2>
        <p class="setting-note">Incipit drafts transcriptions by sending each scan to a model service. Paste your OpenRouter key
          (or point it at a server of your own), test it, and choose the models. You can change this later under Settings → Models.</p>
        <div data-models></div>
        <p class="setting-note">This step is done once the connection test passes.</p>`;
      await window.ModelsPanel.mount(panel.querySelector("[data-models]"), {
        owner: true, onTested: () => markDone(step).catch(showError) });
    } else if (step === "tabs") {
      panel.innerHTML = `<h2>The tabs</h2>
        <p class="setting-note">Each tab holds its own folders, numbers its documents its own way and drafts with its own
          prompt profile. One is enough to begin; more can be added later under Settings.</p>
        <div data-tabs-admin></div>
        <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-done>This step is done</button></div>`;
      await window.TabsAdmin.mount(panel.querySelector("[data-tabs-admin]"));
    } else if (step === "access") {
      panel.innerHTML = `<h2>Share the address</h2>
        <p class="setting-note">Your team opens the site in a browser at one of these addresses while this computer is on.
          You can come back to this under Settings → Access.</p>
        <div data-access></div>
        <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-done>This step is done</button></div>`;
      await window.AccessPanel.mount(panel.querySelector("[data-access]"), { owner: true });
    } else if (step === "people") {
      panel.innerHTML = `<h2>Your team</h2>
        <p class="setting-note">Add people now, or make a join code and send it with the sign-in link. You can
          do this later in the Team tab.</p>
        <div data-people></div>
        <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-done>This step is done</button></div>`;
      await window.PeopleCards.mount(panel.querySelector("[data-people]"), me);
    }
    panel.querySelector("[data-done]")?.addEventListener("click", () => markDone(step).catch(showError));
  }

  async function finish() {
    try {
      await api("POST", "/api/setup/finish");
      location.href = "/";
    } catch (error) { showError(error); }
  }

  function showError(error) {
    const box = document.createElement("p");
    box.className = "auth-error";
    box.textContent = error.message;
    panel.prepend(box);
  }

  async function start() {
    try {
      me = await api("GET", "/api/me");
      const state = await api("GET", "/api/setup");
      const next = state.steps.find((s) => !s.done);
      drawSteps(state, next ? next.id : "site");
      await open(next ? next.id : "site");
    } catch (error) {
      if (error.status === 401 && token) ownerForm();
      else panel.innerHTML = `<p class="auth-error">${esc(error.message)}</p>`;
    }
  }

  start();
})();
