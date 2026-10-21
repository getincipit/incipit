/* Settings → Prompt and the wizard's prompt step: the questionnaire, the prompt it compiles to, the
   profile's versions, a sample test, and which profile each tab uses. The server compiles and checks
   everything; this gathers answers and shows what came back. */
(function () {
  "use strict";

  const { esc, api } = window.pageKit;

  const STYLES = {
    names: ["How names are written", { last_first: "Last, First Middle", first_last: "First Middle Last", as_written: "As written" }],
    us_places: ["US places", { usps: "With USPS code (Newark, NJ)", full_state: "With the full state (Newark, New Jersey)", as_written: "As written" }],
    world_places: ["Places elsewhere", { country: "With the country (Lyon, France)", as_written: "As written" }],
    dates: ["Dates", { iso: "ISO (1901-03-04)", as_written: "As written" }],
    separator: ["Between several entries", { "; ": "Semicolon (A; B)", " | ": "Bar (A | B)" }],
    uncertain: ["An unreadable word", { bracket_unclear: "[unclear: word?]", bracket_illegible: "[illegible]", question: "word[?]" }],
    marginal_notes: ["Marginal notes", { positioned: "Their own field, with position and page", inline: "Inline in the text", ignore: "Left out" }],
    crossouts: ["Crossed-out text", { final: "Only the final reading", marked: "Kept and marked" }],
  };
  const SWITCHES = {
    structural_tags: "Tag letterheads, signatures, stamps and other structural parts",
    overwritten_hand: "Look closely for handwriting written over a typed page",
  };
  const TYPES = { string: "Text", list: "List", enum: "Choice" };
  const PREVIEWS = [["document_prompt", "Document"], ["split_prompt", "Split"], ["merge_prompt", "Merge"]];

  // "Name — note" per line, the way the builder shows people, organisations and places.
  const entitiesToText = (list) => (list || []).map((e) => (e.note ? `${e.name} — ${e.note}` : e.name)).join("\n");
  const textToEntities = (text) => String(text || "").split("\n").map((line) => line.trim()).filter(Boolean)
    .map((line) => {
      const [name, ...rest] = line.split(/\s+(?:—|--)\s+/);
      return { name: name.trim(), note: rest.join(" — ").trim() };
    });
  const textToList = (text, splitter = /\n/) => String(text || "").split(splitter).map((t) => t.trim()).filter(Boolean);

  /* Lines of `before` and `after` marked kept, removed or added (a longest-common-subsequence diff). */
  function lineDiff(before, after) {
    const a = before.split("\n"), b = after.split("\n");
    const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
    for (let i = a.length - 1; i >= 0; i--) {
      for (let j = b.length - 1; j >= 0; j--) {
        table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    const out = [];
    let i = 0, j = 0;
    while (i < a.length && j < b.length) {
      if (a[i] === b[j]) { out.push(["same", a[i]]); i++; j++; }
      else if (table[i + 1][j] >= table[i][j + 1]) out.push(["gone", a[i++]]);
      else out.push(["new", b[j++]]);
    }
    while (i < a.length) out.push(["gone", a[i++]]);
    while (j < b.length) out.push(["new", b[j++]]);
    return out;
  }

  function fieldRows(questionnaire, answers) {
    const overrides = answers.fields || {};
    return questionnaire.builtin_fields.map((field) => {
      const own = overrides[field.key] || {};
      const enabled = own.enabled !== false;
      return `<tr data-builtin="${esc(field.key)}">
        <td><label class="check"><input type="checkbox" data-f="enabled" ${enabled ? "checked" : ""}> <span class="pb-key">${esc(field.key)}</span></label></td>
        <td><input data-f="label" maxlength="60" value="${esc(own.label ?? field.label)}" aria-label="Label for ${esc(field.key)}"></td>
        <td><input data-f="export_column" maxlength="60" value="${esc(own.export_column ?? field.export_column)}" placeholder="not exported" aria-label="Export column for ${esc(field.key)}"></td>
        <td class="pb-type">${esc(TYPES[field.type] || field.type)}</td>
      </tr>`;
    }).join("");
  }

  function customRow(field) {
    const f = field || { key: "", label: "", type: "string", scope: "document", options: [], export_column: "", instruction: "" };
    return `<div class="pb-custom setting-card" data-custom>
      <div class="field-row">
        <label class="field">Key<input data-c="key" maxlength="32" value="${esc(f.key)}" placeholder="e.g. language"></label>
        <label class="field">Label<input data-c="label" maxlength="60" value="${esc(f.label)}"></label>
        <label class="field">Kind<select data-c="type">${Object.entries(TYPES).map(([v, l]) => `<option value="${v}" ${f.type === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="field">Read from<select data-c="scope"><option value="document" ${f.scope !== "page" ? "selected" : ""}>The whole document</option><option value="page" ${f.scope === "page" ? "selected" : ""}>Each page</option></select></label>
      </div>
      <div class="field-row">
        <label class="field">Choices (comma-separated)<input data-c="options" value="${esc((f.options || []).join(", "))}"></label>
        <label class="field">Export column<input data-c="export_column" maxlength="60" value="${esc(f.export_column)}"></label>
      </div>
      <label class="field">What the model should write here<textarea data-c="instruction" rows="2">${esc(f.instruction)}</textarea></label>
      <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-remove-custom>Remove field</button></div>
    </div>`;
  }

  function formHtml(questionnaire, a) {
    const vocab = (key, label) => {
      const v = (a.vocabularies || {})[key] || { mode: "free", terms: [] };
      return `<div class="pb-vocab" data-vocab="${key}">
        <label class="field">${label}<select data-v="mode">
          <option value="free" ${v.mode !== "controlled" ? "selected" : ""}>Free: the model writes the term</option>
          <option value="controlled" ${v.mode === "controlled" ? "selected" : ""}>Controlled: only these terms</option></select></label>
        <label class="field">Terms, one per line<textarea data-v="terms" rows="3">${esc((v.terms || []).join("\n"))}</textarea></label>
      </div>`;
    };
    const styles = a.styles || {};
    return `
      <section class="setting-card" data-section="collection">
        <div class="setting-card-head"><h4>The collection</h4></div>
        <label class="field">Collection<input name="collection" maxlength="200" value="${esc(a.collection)}" placeholder="e.g. the papers of a civil rights organisation"></label>
        <label class="field">What it holds<input name="material" maxlength="400" value="${esc(a.material)}"></label>
        <div class="field-row">
          <label class="field">From (year)<input name="period_from" inputmode="numeric" maxlength="4" value="${esc(a.period_from ?? "")}"></label>
          <label class="field">To (year)<input name="period_to" inputmode="numeric" maxlength="4" value="${esc(a.period_to ?? "")}"></label>
          <label class="field">Languages (comma-separated)<input name="languages" value="${esc((a.languages || []).join(", "))}"></label>
        </div>
        <label class="field">Region<input name="region" maxlength="200" value="${esc(a.region)}"></label>
      </section>
      <section class="setting-card" data-section="entities">
        <div class="setting-card-head"><h4>People, organisations and places it mentions</h4></div>
        <p class="setting-note">One per line, with an optional note after a dash: <code>Smith, Jane — secretary, 1950s</code>. These help the
          model spell names it half-reads; it is told never to add one the page does not show.</p>
        <label class="field">People<textarea name="people" rows="3">${esc(entitiesToText(a.people))}</textarea></label>
        <label class="field">Organisations<textarea name="organisations" rows="3">${esc(entitiesToText(a.organisations))}</textarea></label>
        <label class="field">Places<textarea name="places" rows="3">${esc(entitiesToText(a.places))}</textarea></label>
      </section>
      <section class="setting-card" data-section="vocabularies">
        <div class="setting-card-head"><h4>Document types and subjects</h4></div>
        <div class="field-row">${vocab("document_types", "Document types")}${vocab("subjects", "Subjects")}</div>
      </section>
      <section class="setting-card" data-section="styles">
        <div class="setting-card-head"><h4>How the index is written</h4></div>
        <div class="pb-styles">${Object.entries(STYLES).map(([key, [label, options]]) => `
          <label class="field">${label}<select data-style="${key}">${Object.entries(options).map(([value, text]) =>
            `<option value="${esc(value)}" ${styles[key] === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select></label>`).join("")}
        </div>
        ${Object.entries(SWITCHES).map(([key, label]) => `
          <label class="check"><input type="checkbox" data-switch="${key}" ${styles[key] !== false ? "checked" : ""}> ${label}</label>`).join("")}
      </section>
      <section class="setting-card" data-section="fields">
        <div class="setting-card-head"><h4>What is catalogued</h4></div>
        <p class="setting-note">Each field becomes a line in the prompt, a box on the review form and a column in the export.</p>
        <div class="pb-table-wrap"><table class="pb-fields">
          <thead><tr><th>Field</th><th>Label</th><th>Export column</th><th>Kind</th></tr></thead>
          <tbody>${fieldRows(questionnaire, a)}</tbody></table></div>
        <div data-customs>${(a.custom_fields || []).map(customRow).join("")}</div>
        <div class="settings-form-actions"><button type="button" class="btn btn-secondary btn-small" data-add-custom>Add a field</button></div>
        <label class="field">Title template<input name="title_template" maxlength="200" value="${esc(a.title_template)}"></label>
        <p class="setting-note">Write field keys in braces, e.g. <code>[{display_id}], {document_type} from {author}, {date}</code>. A clause whose field is empty is left out.</p>
      </section>
      <section class="setting-card" data-section="special">
        <div class="setting-card-head"><h4>Anything else the model should know</h4></div>
        <label class="field">Instructions for this archive<textarea name="special_instructions" rows="5" maxlength="6000">${esc(a.special_instructions)}</textarea></label>
      </section>`;
  }

  function readForm(form) {
    const value = (name) => form.querySelector(`[name="${name}"]`)?.value ?? "";
    const answers = {
      collection: value("collection"), material: value("material"), region: value("region"),
      period_from: value("period_from").trim() || null, period_to: value("period_to").trim() || null,
      languages: textToList(value("languages"), /,/),
      people: textToEntities(value("people")), organisations: textToEntities(value("organisations")),
      places: textToEntities(value("places")),
      vocabularies: {}, styles: {}, fields: {}, custom_fields: [],
      title_template: value("title_template"), special_instructions: value("special_instructions"),
    };
    form.querySelectorAll("[data-vocab]").forEach((box) => {
      answers.vocabularies[box.dataset.vocab] = {
        mode: box.querySelector('[data-v="mode"]').value, terms: textToList(box.querySelector('[data-v="terms"]').value) };
    });
    form.querySelectorAll("[data-style]").forEach((select) => { answers.styles[select.dataset.style] = select.value; });
    form.querySelectorAll("[data-switch]").forEach((box) => { answers.styles[box.dataset.switch] = box.checked; });
    form.querySelectorAll("[data-builtin]").forEach((row) => {
      answers.fields[row.dataset.builtin] = {
        enabled: row.querySelector('[data-f="enabled"]').checked,
        label: row.querySelector('[data-f="label"]').value,
        export_column: row.querySelector('[data-f="export_column"]').value };
    });
    form.querySelectorAll("[data-custom]").forEach((card) => {
      const c = (name) => card.querySelector(`[data-c="${name}"]`).value;
      answers.custom_fields.push({ key: c("key").trim(), label: c("label"), type: c("type"), scope: c("scope"),
        options: textToList(c("options"), /,/), export_column: c("export_column"), instruction: c("instruction") });
    });
    return answers;
  }

  async function mount(container, options) {
    const opts = options || {};
    const questionnaire = await api("GET", "/api/prompt-profiles/questionnaire");
    let listing = await api("GET", "/api/prompt-profiles");
    let profileId = opts.profileId || listing.profiles[0]?.id;
    let profile = await api("GET", `/api/prompt-profiles/${profileId}`);
    let preview = profile.current;
    let showing = "document_prompt";
    let comparing = null;
    let timer = null;
    let generation = 0;
    let stopped = false;

    container.innerHTML = `
      <div class="prompt-builder">
        <div class="appearance-status pipeline-status" hidden></div>
        <div class="pb-bar setting-card">
          <label class="field">Profile<select data-profile></select></label>
          <div class="pb-bar-actions">
            <button type="button" class="btn btn-secondary btn-small" data-new-profile>New profile</button>
            <button type="button" class="btn btn-secondary btn-small" data-rename>Rename</button>
          </div>
        </div>
        <div class="pb-columns">
          <form class="pb-form" novalidate></form>
          <aside class="pb-preview">
            <div class="pb-preview-card setting-card">
              <div class="pb-preview-head">
                <div class="pb-segments" role="tablist">${PREVIEWS.map(([key, label]) =>
                  `<button type="button" role="tab" class="pb-segment" data-show="${key}">${label}</button>`).join("")}</div>
                <span class="pb-state" data-state>Up to date</span>
              </div>
              <div class="pb-error" data-error hidden></div>
              <p class="pb-sample"><span class="meta-label">A title it would give</span><span data-title></span></p>
              <div class="pb-columns-list" data-columns></div>
              <pre class="pb-prompt" data-prompt tabindex="0"></pre>
            </div>
            <div class="setting-card">
              <div class="setting-card-head"><h4>Versions</h4></div>
              <ol class="pb-versions" data-versions></ol>
            </div>
            <div class="setting-card">
              <div class="setting-card-head"><h4>Try it on a sample</h4></div>
              <p class="setting-note">Drafts 1 to 3 scans with the current version. It costs a draft and counts toward the spending cap.</p>
              <div class="pb-test-row"><input type="file" accept="image/*" multiple data-test-files>
                <button type="button" class="btn btn-secondary btn-small" data-test>Run test</button></div>
              <div class="pb-test-result" data-test-result hidden></div>
            </div>
          </aside>
        </div>
        <div class="settings-form-actions pb-save">
          <button type="button" class="btn btn-accept" data-save>Save as a new version</button>
        </div>
      </div>`;

    const $ = (selector) => container.querySelector(selector);
    const form = $(".pb-form");
    const status = (message, isError) => {
      const box = $(".appearance-status");
      box.textContent = message || "";
      box.classList.toggle("is-error", !!isError);
      box.hidden = !message;
    };

    function drawProfiles() {
      $("[data-profile]").innerHTML = listing.profiles.map((p) => `<option value="${p.id}" ${p.id === profileId ? "selected" : ""}>${
        esc(p.name)} · v${p.current_version_no}${p.used_by ? ` · ${esc(p.used_by)}` : ""}</option>`).join("");
    }

    function drawForm() {
      form.innerHTML = formHtml(questionnaire, profile.current.answers);
    }

    function drawPreview() {
      container.querySelectorAll(".pb-segment").forEach((b) => {
        b.classList.toggle("active", b.dataset.show === showing);
        b.setAttribute("aria-selected", b.dataset.show === showing);
      });
      $("[data-title]").textContent = preview.sample_title || "";
      $("[data-columns]").innerHTML = (preview.export_columns || []).map((c) => `<span class="pb-column">${esc(c)}</span>`).join("");
      const pre = $("[data-prompt]");
      if (comparing) {
        pre.innerHTML = lineDiff(comparing.text, preview[showing] || "").map(([kind, line]) =>
          `<span class="pb-line pb-${kind}">${esc(line) || " "}</span>`).join("");
      } else {
        pre.textContent = preview[showing] || "";
      }
      pre.classList.remove("is-fresh");
      void pre.offsetWidth;   // restarts the fade so a new compile reads as new
      pre.classList.add("is-fresh");
    }

    function drawVersions() {
      $("[data-versions]").innerHTML = profile.versions.map((v) => {
        const current = v.id === profile.current_version_id;
        return `<li class="pb-version${current ? " is-current" : ""}">
          <div><b>v${v.version_no}</b>${current ? ' <span class="badge badge-accepted">Current</span>' : ""}
            <span class="setting-note">${esc((v.created_at || "").slice(0, 10))}${v.created_by ? ` · ${esc(v.created_by)}` : ""} · ${v.documents} document${v.documents === 1 ? "" : "s"}</span></div>
          <div class="pb-version-actions">
            <button type="button" class="btn btn-secondary btn-small" data-compare="${v.id}">${comparing?.id === v.id ? "Stop comparing" : "Compare"}</button>
            ${current ? "" : `<button type="button" class="btn btn-secondary btn-small" data-make-current="${v.id}">Make current</button>`}
          </div></li>`;
      }).join("");
    }

    async function load(id) {
      profileId = id;
      profile = await api("GET", `/api/prompt-profiles/${id}`);
      preview = profile.current;
      comparing = null;
      drawProfiles(); drawForm(); drawPreview(); drawVersions();
    }

    function compileSoon() {
      clearTimeout(timer);
      $("[data-state]").textContent = "Compiling…";
      $("[data-state]").classList.add("is-busy");
      timer = setTimeout(async () => {
        const mine = ++generation;
        try {
          const compiled = await api("POST", "/api/prompt-profiles/preview", { answers: readForm(form) });
          if (stopped || mine !== generation) return;
          preview = compiled;
          $("[data-error]").hidden = true;
          $("[data-state]").textContent = compiled.content_hash === profile.current.content_hash ? "Up to date" : "Unsaved changes";
          drawPreview();
        } catch (error) {
          if (stopped || mine !== generation) return;
          // The answer the compiler refused, said plainly; the last good preview stays on screen.
          $("[data-error]").textContent = error.message;
          $("[data-error]").hidden = false;
          $("[data-state]").textContent = "Cannot be used yet";
        } finally {
          if (mine === generation) $("[data-state]").classList.remove("is-busy");
        }
      }, 350);
    }

    form.addEventListener("input", compileSoon);
    form.addEventListener("change", compileSoon);
    form.addEventListener("click", (e) => {
      if (e.target.closest("[data-add-custom]")) {
        form.querySelector("[data-customs]").insertAdjacentHTML("beforeend", customRow());
        compileSoon();
      } else if (e.target.closest("[data-remove-custom]")) {
        e.target.closest("[data-custom]").remove();
        compileSoon();
      }
    });
    container.querySelectorAll(".pb-segment").forEach((b) => b.addEventListener("click", () => {
      showing = b.dataset.show;
      if (comparing) compare(comparing.id, true);
      else drawPreview();
    }));

    async function compare(versionId, keep) {
      if (comparing?.id === versionId && !keep) { comparing = null; drawVersions(); drawPreview(); return; }
      const version = await api("GET", `/api/prompt-profiles/versions/${versionId}`);
      comparing = { id: versionId, text: version[showing] || "" };
      drawVersions(); drawPreview();
    }

    $("[data-versions]").addEventListener("click", async (e) => {
      const cmp = e.target.closest("[data-compare]");
      const cur = e.target.closest("[data-make-current]");
      try {
        if (cmp) await compare(Number(cmp.dataset.compare));
        if (cur) {
          await api("POST", `/api/prompt-profiles/${profileId}/current`, { version_id: Number(cur.dataset.makeCurrent) });
          listing = await api("GET", "/api/prompt-profiles");
          await load(profileId);
          status("That version is current again; new drafts use it.");
        }
      } catch (error) { status(error.message, true); }
    });

    $("[data-profile]").addEventListener("change", (e) => load(Number(e.target.value)).catch((error) => status(error.message, true)));
    $("[data-new-profile]").addEventListener("click", async () => {
      const name = window.prompt("Name the new profile (it starts from the answers on screen):");
      if (!name) return;
      try {
        const { id } = await api("POST", "/api/prompt-profiles", { name, answers: readForm(form) });
        listing = await api("GET", "/api/prompt-profiles");
        await load(id);
        status("Profile created.");
      } catch (error) { status(error.message, true); }
    });
    $("[data-rename]").addEventListener("click", async () => {
      const name = window.prompt("New name for this profile:", profile.name);
      if (!name) return;
      try {
        await api("PUT", `/api/prompt-profiles/${profileId}`, { name });
        listing = await api("GET", "/api/prompt-profiles");
        drawProfiles();
        status("Renamed.");
      } catch (error) { status(error.message, true); }
    });
    $("[data-save]").addEventListener("click", async () => {
      const button = $("[data-save]");
      button.classList.add("is-busy");
      try {
        await api("POST", `/api/prompt-profiles/${profileId}/versions`, { answers: readForm(form) });
        listing = await api("GET", "/api/prompt-profiles");
        await load(profileId);
        status(`Saved as version ${profile.versions[0].version_no}. New drafts use it; documents already drafted keep theirs.`);
        if (opts.onSaved) opts.onSaved();
      } catch (error) {
        // Nothing was saved: the current version stays in use.
        status(`${error.message} The current version is still in use.`, true);
      } finally { button.classList.remove("is-busy"); }
    });
    $("[data-test]").addEventListener("click", async () => {
      const files = [...$("[data-test-files]").files];
      const box = $("[data-test-result]");
      if (!files.length) { status("Choose one to three scans first.", true); return; }
      const body = new FormData();
      files.forEach((file) => body.append("files", file));
      const button = $("[data-test]");
      button.classList.add("is-busy");
      try {
        const result = await api("POST", `/api/prompt-profiles/${profileId}/test`, body);
        box.innerHTML = `<div class="meta-label">Transcription</div><pre class="pb-prompt">${esc(result.text)}</pre>
          <dl class="pb-test-fields">${(result.field_registry || []).map((f) => {
            const v = result.fields[f.key];
            return `<dt>${esc(f.label)}</dt><dd>${esc(Array.isArray(v) ? v.join("; ") : v) || "<span class='setting-note'>(empty)</span>"}</dd>`;
          }).join("")}</dl>
          <p class="setting-note">Cost $${Number(result.cost || 0).toFixed(4)}</p>`;
        box.hidden = false;
      } catch (error) { status(error.message, true); }
      finally { button.classList.remove("is-busy"); }
    });

    drawProfiles(); drawForm(); drawPreview(); drawVersions();
    return () => { stopped = true; clearTimeout(timer); };
  }

  window.PromptBuilder = { mount, lineDiff };
})();
