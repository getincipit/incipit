/* Settings → Tabs and the wizard's tabs step: the tabs of the site, in order, each with its name,
   its document-id scheme, its prompt profile and whether it is open. The server owns every rule;
   this shows the list and sends one change at a time. */
(function () {
  "use strict";

  const { esc, api, remount } = window.pageKit;
  const SCHEMES = [["letters", "E2002AAA (letters)"], ["numbers", "E2002-001 (numbers)"], ["filename", "the scan's filename"]];

  async function mount(container) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    let tabs = [];
    let profiles = [];

    container.innerHTML = `
      <div class="tabs-admin">
        <div class="appearance-status pipeline-status" hidden></div>
        <ol class="tab-rows" data-rows></ol>
        <form class="setting-card tab-add" data-add novalidate>
          <div class="setting-card-head"><h4>Add a tab</h4></div>
          <div class="field-row">
            <label class="field">Name<input name="label" maxlength="40" placeholder="e.g. Ledgers" required></label>
            <label class="field">Document ids<select name="id_scheme">${SCHEMES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select></label>
            <label class="field">Prompt profile<select name="prompt_profile_id" data-profiles></select></label>
          </div>
          <div class="settings-form-actions"><button type="submit" class="btn btn-accept">Add tab</button></div>
        </form>
      </div>`;

    const $ = (selector) => container.querySelector(selector);
    const status = (message, isError) => {
      const box = $(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };

    function profileOptions(chosen) {
      return `<option value="" ${chosen ? "" : "selected"}>Default profile</option>` + profiles.map((p) =>
        `<option value="${p.id}" ${chosen === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    }

    function row(tab, index) {
      const held = tab.folder_count + tab.batch_count;
      const what = [tab.folder_count ? `${tab.folder_count} folder${tab.folder_count === 1 ? "" : "s"}` : "",
                    tab.batch_count ? `${tab.batch_count} upload${tab.batch_count === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ") || "empty";
      return `<li class="tab-row${tab.enabled ? "" : " is-paused"}" data-id="${tab.id}">
        <div class="tab-row-main">
          <input class="tab-row-label" data-f="label" value="${esc(tab.label)}" maxlength="40" aria-label="Name of the tab">
          <code class="tab-row-slug" title="Its address, fixed when it was made">/${esc(tab.slug)}</code>
          <span class="tab-row-held">${esc(what)}</span>
        </div>
        <div class="tab-row-fields">
          <label class="field">Document ids<select data-f="id_scheme" ${tab.folder_count ? "disabled title='Fixed once the tab has folders'" : ""}>${
            SCHEMES.map(([v, l]) => `<option value="${v}" ${tab.id_scheme === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
          <label class="field">Prompt profile<select data-f="prompt_profile_id">${profileOptions(tab.prompt_profile_id)}</select></label>
          <label class="field">Open to the team<span class="tab-switch"><input type="checkbox" data-f="enabled" ${tab.enabled ? "checked" : ""} aria-label="Open">
            <span class="tab-switch-track"></span><span class="tab-switch-word">${tab.enabled ? "Open" : "Paused"}</span></span></label>
        </div>
        <div class="tab-row-actions">
          <button type="button" class="btn btn-secondary btn-small" data-move="-1" ${index === 0 ? "disabled" : ""} aria-label="Move up">&uarr;</button>
          <button type="button" class="btn btn-secondary btn-small" data-move="1" ${index === tabs.length - 1 ? "disabled" : ""} aria-label="Move down">&darr;</button>
          <button type="button" class="btn btn-secondary btn-small" data-delete ${held ? "disabled title='Empty it first'" : ""}>Delete</button>
        </div>
      </li>`;
    }

    function draw() {
      $("[data-rows]").innerHTML = tabs.map(row).join("");
      $("[data-profiles]").innerHTML = profileOptions(null);
    }

    async function reload(message) {
      [tabs, profiles] = await Promise.all([
        api("GET", "/api/tabs").then((r) => r.tabs),
        api("GET", "/api/prompt-profiles").then((r) => r.profiles),
      ]);
      draw();
      if (message) status(message);
      window.__tabsReload__?.();
    }

    async function change(tabId, body, message) {
      try {
        await api("PUT", `/api/tabs/${tabId}`, body);
        await reload(message);
      } catch (error) {
        status(error.message, true);
        await reload();
      }
    }

    on("change", (e) => {
      const control = e.target.closest("[data-f]");
      const li = e.target.closest(".tab-row");
      if (!control || !li) return;
      const field = control.dataset.f;
      const value = field === "enabled" ? control.checked
        : field === "prompt_profile_id" ? (control.value ? Number(control.value) : null) : control.value;
      const said = field === "enabled" ? (value ? "The tab is open again." : "The tab is paused; nothing in it can change until it is resumed.")
        : field === "label" ? "Renamed; its address is unchanged." : "Saved.";
      change(li.dataset.id, { [field]: value }, said);
    });
    on("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches(".tab-row-label")) { e.preventDefault(); e.target.blur(); }
    });
    on("click", async (e) => {
      const li = e.target.closest(".tab-row");
      const move = e.target.closest("[data-move]");
      if (move && li) {
        const order = tabs.map((t) => t.id);
        const from = order.indexOf(Number(li.dataset.id));
        const to = from + Number(move.dataset.move);
        if (to < 0 || to >= order.length) return;
        [order[from], order[to]] = [order[to], order[from]];
        try { await api("POST", "/api/tabs/reorder", { order }); await reload("Reordered."); }
        catch (error) { status(error.message, true); }
      }
      if (e.target.closest("[data-delete]") && li) {
        const tab = tabs.find((t) => String(t.id) === li.dataset.id);
        if (!window.confirm(`Delete the ${tab.label} tab? It is empty, so nothing else is removed.`)) return;
        try { await api("DELETE", `/api/tabs/${tab.id}`); await reload("Deleted."); }
        catch (error) { status(error.message, true); }
      }
    });
    $("[data-add]").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const body = { label: form.label.value, id_scheme: form.id_scheme.value,
                     prompt_profile_id: form.prompt_profile_id.value ? Number(form.prompt_profile_id.value) : null };
      try {
        const made = await api("POST", "/api/tabs", body);
        form.reset();
        await reload(`Added ${made.tab.label} at /${made.tab.slug}.`);
      } catch (error) { status(error.message, true); }
    });

    await reload();
  }

  window.TabsAdmin = { mount };
})();
