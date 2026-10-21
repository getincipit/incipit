/* What this site calls its roles ("Reviewer", "Volunteer", "Editor"), from the owner's settings.
   The server writes the site's public settings into the page as window.__SITE__; every script
   that names a role asks term() rather than spelling the word itself. */
(function () {
  "use strict";

  const site = window.__SITE__ || {};
  const labels = Object.assign({ owner: "Owner", admin: "Admin", reviewer: "Reviewer" }, site.labels || {});

  function plural(word) {
    if (/[^aeiou]y$/i.test(word)) return word.slice(0, -1) + "ies";
    if (/(s|x|z|ch|sh)$/i.test(word)) return word + "es";
    return word + "s";
  }

  /** term("reviewer"), term("admin", { plural: true }), term("reviewer", { lower: true }). */
  function term(role, options) {
    const opts = options || {};
    let word = labels[role] || role;
    if (opts.plural) word = plural(word);
    if (opts.lower) word = word.toLowerCase();
    return word;
  }

  /** Fills every <span data-term="reviewer" data-plural data-case="lower"> under `root`. */
  function applyTerms(root) {
    (root || document).querySelectorAll("[data-term]").forEach((el) => {
      el.textContent = term(el.dataset.term, { plural: "plural" in el.dataset, lower: el.dataset.case === "lower" });
    });
  }

  /** After the labels are saved, so the page says the new words without a reload. */
  function setLabels(next) {
    Object.assign(labels, next || {});
    applyTerms();
  }

  window.term = term;
  window.setTermLabels = setLabels;
  window.applyTerms = applyTerms;
  window.siteInfo = site;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => applyTerms());
  else applyTerms();
})();
