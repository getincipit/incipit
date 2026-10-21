/* The landing page's estimator: what a collection costs in people's time by hand and with Incipit,
   how long the model works on it unattended, and what the model calls cost. People time and machine
   time are different kinds of time and are never added together. Every default is an assumption the
   reader is asked to change; the machine figures say what they were measured on. */
(function (root) {
  "use strict";

  const DEFAULTS = {
    documents: 500,
    pagesPerDocument: 1.6,            // measured: 154 documents averaged 1.55 pages
    transcribeMinutesPerPage: 20,     // assumption: typing a handwritten or typed page by hand
    indexMinutesPerDocument: 10,      // assumption: writing its catalogue record by hand
    reviewMinutesPerPage: 5,          // assumption: checking a draft against the scan
    reviewIndexMinutesPerDocument: 2, // assumption: checking the drafted record
    machineSecondsPerPage: 11,        // measured: median on 154 documents from one archive (90th percentile 23 s)
    costPerPage: 0.002,               // assumption: varies with the model and its prices (one archive measured $0.0017)
  };

  // Non-numbers and negatives become the default, so a half-typed field never shows NaN.
  function clean(inputs) {
    const out = {};
    for (const key of Object.keys(DEFAULTS)) {
      const value = Number(inputs && inputs[key]);
      out[key] = Number.isFinite(value) && value >= 0 ? value : DEFAULTS[key];
    }
    return out;
  }

  function estimate(inputs) {
    const x = clean(inputs);
    const pages = x.documents * x.pagesPerDocument;
    const byHand = (pages * x.transcribeMinutesPerPage + x.documents * x.indexMinutesPerDocument) / 60;
    const withIncipit = (pages * x.reviewMinutesPerPage + x.documents * x.reviewIndexMinutesPerDocument) / 60;
    return {
      pages,
      people: { byHandHours: byHand, withIncipitHours: withIncipit, savedHours: Math.max(byHand - withIncipit, 0),
                savedShare: byHand > 0 ? Math.max(byHand - withIncipit, 0) / byHand : 0 },
      machine: { hours: (pages * x.machineSecondsPerPage) / 3600 },
      cost: { dollars: pages * x.costPerPage },
    };
  }

  const api = { DEFAULTS, estimate, clean };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Estimator = api;
})(typeof window !== "undefined" ? window : globalThis);
