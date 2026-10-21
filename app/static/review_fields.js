/* The review form's catalogue fields, drawn from the document's own field registry. */
(() => {
  const { esc } = window.pageKit;
  const lines = (text) => String(text || "").split("\n").map((l) => l.trim()).filter(Boolean);

  let shown = [];

  // A list or enum with fixed options is picked from chips; anything else is typed.
  const isChoice = (field) => (field.type === "enum" || field.type === "list") && field.options?.length;
  const isList = (field) => field.type === "list" || field.type === "enum";

  function inputFor(field, value) {
    const id = `field-input-${field.key}`;
    if (isChoice(field)) {
      const chosen = new Set(value || []);
      return `<div class="field-chips" id="${id}" role="group" aria-label="${esc(field.label)}">${
        field.options.map((option) => `
          <label class="field-chip">
            <input type="checkbox" value="${esc(option)}"${chosen.has(option) ? " checked" : ""}>
            <span>${esc(option)}</span>
          </label>`).join("")}</div>`;
    }
    if (isList(field)) {
      const rows = Math.min(Math.max((value || []).length + 1, 2), 8);
      return `<textarea id="${id}" rows="${rows}" placeholder="One per line">${esc((value || []).join("\n"))}</textarea>`;
    }
    if (field.options?.length) {
      // A value outside a controlled list is kept selectable, so opening an old document loses nothing.
      const options = value && !field.options.includes(value) ? [value, ...field.options] : field.options;
      return `<select class="meta-input" id="${id}"><option value=""></option>${
        options.map((o) => `<option${o === value ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    }
    return `<input class="meta-input" id="${id}" value="${esc(value)}" title="${esc(value)}">`;
  }

  /* Draws the title, the short fields into `grid` and the list fields as blocks into `blocks`. */
  function render(registry, values, { grid, blocks, title, titleIsOverride, onInput }) {
    shown = registry;
    const short = registry.filter((f) => !isList(f));
    const long = registry.filter(isList);
    const titleHint = titleIsOverride
      ? "(edited &mdash; overrides the auto-generated title)"
      : "(auto-generated from the fields below &mdash; edit to override)";
    grid.innerHTML = `
      <div class="meta-field meta-field-wide">
        <label class="meta-label" for="meta-input-title">Title <span class="block-subtitle">${titleHint}</span></label>
        <input class="meta-input" id="meta-input-title" value="${esc(title)}" title="${esc(title)}">
      </div>` + short.map((field) => `
      <div class="meta-field">
        <label class="meta-label" for="field-input-${field.key}">${esc(field.label)}</label>
        ${inputFor(field, values[field.key])}
      </div>`).join("");
    blocks.innerHTML = long.map((field) => `
      <div class="block field-block" data-field="${esc(field.key)}" data-scope="${esc(field.scope)}">
        <h3><label for="field-input-${field.key}">${esc(field.label)}</label></h3>
        ${inputFor(field, values[field.key])}
      </div>`).join("");
    // Assigned rather than added, so redrawing the form never stacks a second listener.
    grid.oninput = onInput;
    blocks.oninput = onInput;
    // A value longer than its box can still be read in full by hovering it.
    grid.querySelectorAll("input.meta-input").forEach((input) => {
      input.addEventListener("input", () => { input.title = input.value; });
    });
  }

  /* {key: value} for every field the form shows, in the shapes the server validates. */
  function read() {
    const out = {};
    for (const field of shown) {
      const node = document.getElementById(`field-input-${field.key}`);
      if (!node) continue;
      if (isChoice(field)) {
        out[field.key] = [...node.querySelectorAll("input:checked")].map((box) => box.value);
      } else if (isList(field)) {
        out[field.key] = lines(node.value);
      } else {
        out[field.key] = node.value;
      }
    }
    return out;
  }

  /* Every input the form drew, so a redraft can lock them. */
  function controls() {
    return shown.flatMap((field) => {
      const node = document.getElementById(`field-input-${field.key}`);
      if (!node) return [];
      return isChoice(field) ? [...node.querySelectorAll("input")] : [node];
    });
  }

  /* The blocks a redraft replaces: the page fields, which the model reads off the page. */
  function pageBlocks() {
    return [...document.querySelectorAll(".field-block[data-scope='page']")];
  }

  window.ReviewFields = { render, read, controls, pageBlocks };
})();
