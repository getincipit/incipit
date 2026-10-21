/* Settings → Models (the service, its key and the models) and Settings → Usage (what the calls
   cost, and the caps). The wizard's "model" step mounts the Models panel too. */
(function () {
  "use strict";

  const { esc, api, remount } = window.pageKit;

  const money = (n) => (n == null ? "—" : `$${Number(n).toFixed(n !== 0 && Math.abs(n) < 0.01 ? 4 : 2)}`);
  const perMillion = (m) => (m.free ? "free" : `$${m.input_per_million} in · $${m.output_per_million} out per million tokens`);

  function statusBox(container) {
    return (message, isError) => {
      const box = container.querySelector(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };
  }

  async function mountModels(container, options) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    const opts = options || {};
    let backend = await api("GET", "/api/models");
    let catalogue = [];
    let kind = backend.kind;
    const status = statusBox(container);

    function priceOf(id) {
      const found = catalogue.find((m) => m.id === id);
      return found ? perMillion(found) : "";
    }

    function draw() {
      const test = backend.last_test;
      const owner = !!opts.owner;
      container.innerHTML = `
        <div class="access-panel">
          <div class="appearance-status pipeline-status" hidden></div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>The model service</h4></div>
            <div class="pb-segments" role="group" aria-label="Model service">
              <button type="button" class="pb-segment${kind === "openrouter" ? " active" : ""}" data-kind="openrouter" ${owner ? "" : "disabled"}>OpenRouter</button>
              <button type="button" class="pb-segment${kind === "openai_compatible" ? " active" : ""}" data-kind="openai_compatible" ${owner ? "" : "disabled"}>Your own server</button>
            </div>
            ${kind === "openrouter"
              ? `<p class="setting-note">OpenRouter carries the drafting calls; you pay it directly for what the site uses. Make a key at
                  <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">openrouter.ai/settings/keys</a> and paste it here.</p>`
              : `<label class="field">Server address<input name="base_url" value="${esc(backend.base_url)}" placeholder="http://192.168.1.50:8000/v1"
                  spellcheck="false" autocapitalize="off" ${owner ? "" : "disabled"}></label>
                <p class="setting-note">Any server that speaks the OpenAI chat-completions API, such as a model you run on your own network.</p>`}
            <label class="field">API key
              <input name="api_key" type="password" autocomplete="off" spellcheck="false"
                placeholder="${backend.has_key ? `Saved key ${esc(backend.key_hint)} — type a new one to replace it` : "Paste the key"}" ${owner ? "" : "disabled"}></label>
            ${backend.key_problem ? `<p class="setting-note access-warning">${esc(backend.key_problem)}</p>` : ""}
            <p class="setting-note">The key is stored encrypted on this computer and is never shown again or sent to anyone's browser.
              Changing the service or the server's address removes the saved key, so it is only ever sent where it was made for.
              ${kind === "openai_compatible" ? "A server on your own network may need no key at all." : ""}</p>
            ${test.at ? `<p class="setting-note access-net is-${test.ok ? "open" : "refused"}">${esc(test.detail)}</p>` : ""}
            <div class="settings-form-actions access-actions">
              <button type="button" class="btn btn-secondary btn-small" data-test>Test the connection</button>
              ${owner ? `<button type="button" class="btn btn-accept" data-save>Save</button>` : ""}
            </div>
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>The models</h4></div>
            <label class="field">Drafting model<input name="model" list="model-catalogue" value="${esc(backend.model)}" spellcheck="false" autocapitalize="off" ${owner ? "" : "disabled"}></label>
            <p class="setting-note" data-price="model">${esc(priceOf(backend.model))}</p>
            <label class="field">Fallback model, for a document the first one cannot do<input name="fallback_model" list="model-catalogue"
              value="${esc(backend.fallback_model)}" spellcheck="false" autocapitalize="off" ${owner ? "" : "disabled"}></label>
            <p class="setting-note" data-price="fallback_model">${esc(priceOf(backend.fallback_model))}</p>
            <datalist id="model-catalogue">${catalogue.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} — ${esc(perMillion(m))}</option>`).join("")}</datalist>
            <p class="setting-note">${kind === "openrouter" ? "Start typing to choose from OpenRouter's models that read images; free ones are listed first." : "Use the model names your server lists."}
              A change takes effect on the next document.</p>
          </div>
        </div>`;
    }

    on("click", async (e) => {
      const kindButton = e.target.closest("[data-kind]");
      if (kindButton) { kind = kindButton.dataset.kind; draw(); return; }
      if (e.target.closest("[data-save]")) {
        const body = { kind, model: container.querySelector('[name="model"]').value,
                       fallback_model: container.querySelector('[name="fallback_model"]').value };
        const url = container.querySelector('[name="base_url"]');
        if (url) body.base_url = url.value;
        const key = container.querySelector('[name="api_key"]').value.trim();
        if (key) body.api_key = key;
        try {
          backend = await api("PUT", "/api/models", body);
          draw();
          status(key ? "Saved. Test the connection to check the key." : "Saved.");
          if (opts.onSaved) opts.onSaved(backend);
        } catch (error) { status(error.message, true); }
        return;
      }
      if (e.target.closest("[data-test]")) {
        status("Testing…");
        try {
          const result = await api("POST", "/api/models/test");
          backend = result.backend;
          draw();
          status(result.ok ? result.detail : result.detail, !result.ok);
          if (result.ok && opts.onTested) opts.onTested(result);
        } catch (error) { status(error.message, true); }
      }
    });
    on("input", (e) => {
      const field = e.target.closest('[name="model"], [name="fallback_model"]');
      if (field) container.querySelector(`[data-price="${field.name}"]`).textContent = priceOf(field.value);
    });

    draw();
    // The picker's list arrives after the page is usable; a failure leaves typing the id.
    if (kind === "openrouter") {
      api("GET", "/api/models/catalogue").then((answer) => {
        catalogue = answer.models || [];
        const typing = container.contains(document.activeElement) && document.activeElement.matches("input");
        if (!typing) draw();
      }).catch(() => {});
    }
  }

  async function mountUsage(container, options) {
    const signal = remount(container);
    const on = (type, handler) => container.addEventListener(type, handler, { signal });
    const opts = options || {};
    let usage = await api("GET", "/api/usage");
    const status = statusBox(container);

    function rows(list, unit) {
      if (!list.length) return `<p class="setting-note">Nothing yet.</p>`;
      return `<table class="usage-table"><thead><tr><th>${unit}</th><th>Calls</th><th>Tokens in / out</th><th>Cost</th></tr></thead><tbody>
        ${list.map((r) => `<tr><td class="usage-wrap">${esc(r.name || "—")}</td><td>${r.calls}</td><td>${r.input_tokens.toLocaleString()} / ${r.output_tokens.toLocaleString()}</td><td>${money(r.cost)}</td></tr>`).join("")}
        </tbody></table>`;
    }

    function draw() {
      const latest = usage.latest || [];
      container.innerHTML = `
        <div class="access-panel">
          <div class="appearance-status pipeline-status" hidden></div>
          <div class="usage-tiles">
            <div class="usage-tile"><span>This month</span><b>${money(usage.this_month)}</b></div>
            <div class="usage-tile"><span>Last month</span><b>${money(usage.last_month)}</b></div>
            <div class="usage-tile"><span>All time</span><b>${money(usage.all_time)}</b></div>
          </div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Caps</h4></div>
            <p class="setting-note">Drafting and split proposals stop before a cap is passed; the document in progress finishes.</p>
            <div class="field-row">
              <label class="field">All-time cap ($)<input name="max_total_cost" inputmode="decimal" value="${esc(usage.max_total_cost)}"></label>
              <label class="field">Monthly cap ($, empty for none)<input name="monthly_cap" inputmode="decimal" value="${usage.monthly_cap == null ? "" : esc(usage.monthly_cap)}"></label>
            </div>
            <div class="settings-form-actions"><button type="button" class="btn btn-accept" data-save-caps>Save the caps</button></div>
          </div>
          <div class="setting-card"><div class="setting-card-head"><h4>By purpose</h4></div>${rows(usage.by_purpose, "Purpose")}</div>
          <div class="setting-card"><div class="setting-card-head"><h4>By model</h4></div>${rows(usage.by_model, "Model")}</div>
          <div class="setting-card">
            <div class="setting-card-head"><h4>Latest calls</h4></div>
            <p class="setting-note">Every call the model service answered, including the ones that failed and were retried; nothing here is ever removed.</p>
            ${latest.length ? `<div class="usage-scroll"><table class="usage-table"><thead><tr><th>When</th><th>Purpose</th><th>Model</th><th>Outcome</th><th>Cost</th></tr></thead><tbody>
              ${latest.map((c) => `<tr class="is-${esc(c.outcome)}"><td class="usage-when">${esc((c.started_at || "").slice(0, 16)).replace(/-/g, "&#8209;").replace("T", " ")}</td><td>${esc(c.purpose)}</td>
                <td class="usage-wrap">${esc(c.model)}</td><td title="${esc(c.error_text || "")}">${esc(c.outcome)}${c.cost_source === "estimated" ? " (estimated)" : ""}</td><td>${money(c.cost_usd)}</td></tr>`).join("")}
              </tbody></table></div>` : `<p class="setting-note">No calls yet.</p>`}
          </div>
        </div>`;
    }

    on("click", async (e) => {
      if (!e.target.closest("[data-save-caps]")) return;
      try {
        await api("PUT", "/api/usage/caps", {
          max_total_cost: container.querySelector('[name="max_total_cost"]').value,
          monthly_cap: container.querySelector('[name="monthly_cap"]').value,
        });
        usage = await api("GET", "/api/usage");
        draw();
        status("Saved.");
        if (opts.onSaved) opts.onSaved();
      } catch (error) { status(error.message, true); }
    });

    draw();
  }

  window.ModelsPanel = { mount: mountModels };
  window.UsagePanel = { mount: mountUsage };
})();
