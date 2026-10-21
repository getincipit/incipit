/* Keeping a reviewer's edits without them having to remember to press Save.
 *
 * Its own file because it owns one question and nothing else: when is the right moment to write
 * what is on screen. app.js owns the document screen and the saving itself, and offers this module
 * exactly three things through window.__draft__ -- whether there is unsaved work, which document
 * it belongs to, and a request to save it. Nothing here reaches into app.js's state, and nothing
 * here can set a review status: an autosave keeps the verdict the document already has, so a
 * reviewer never finds work recorded as accepted that they did not accept.
 *
 * The history is the reason for the timings below. Every save that changes a field records a
 * revision, so saving on every keystroke would bury a real edit under its own intermediate states.
 * collab.REVISION_COALESCE_SECONDS folds one person's consecutive edits to one field into a single
 * entry, and these delays keep the writes well inside that window.
 */
(() => {
  "use strict";

  // Long enough that ordinary typing does not write, short enough that a reviewer who looks away
  // mid-sentence does not lose it.
  const IDLE_MS = 2500;
  // However continuously they type, their work reaches the server at least this often.
  const MAX_INTERVAL_MS = 30000;
  // Long enough to read, short enough to be gone before it becomes furniture.
  const SAVED_VISIBLE_MS = 2000;

  const draft = () => window.__draft__;

  let idleTimer = null;
  let sinceLastSave = null;
  let saving = false;
  let indicator = null;
  let hideTimer = null;

  function ensureIndicator() {
    const host = document.getElementById("detail-view");
    if (indicator && indicator.isConnected) {
      (host || document.body).appendChild(indicator);
      return indicator;
    }
    indicator = document.createElement("div");
    indicator.id = "autosave-state";
    indicator.className = "autosave-state";
    indicator.setAttribute("role", "status");
    // Announced only when it changes, so a screen reader is not told "saved" on every poll.
    indicator.setAttribute("aria-live", "polite");
    (host || document.body).appendChild(indicator);
    return indicator;
  }

  function say(text, className) {
    const box = ensureIndicator();
    box.textContent = text;
    box.className = `autosave-state ${className}`;
    clearTimeout(hideTimer);
    if (className === "is-saved") {
      hideTimer = setTimeout(() => {
        if (box.className === "autosave-state is-saved") box.className = "autosave-state";
      }, SAVED_VISIBLE_MS);
    }
  }

  async function save(reason) {
    const api = draft();
    if (!api || saving || !api.unsaved()) return false;
    saving = true;
    say("Saving…", "is-saving");
    try {
      const ok = await api.save();
      if (!ok) {
        // A failed save leaves the edit in the box and the document dirty, so the next attempt --
        // or the reviewer's own Save -- still has it. Saying so matters more than retrying.
        say("Not saved — keep this tab open", "is-failed");
        return false;
      }
      sinceLastSave = Date.now();
      if (api.unsaved()) {
        // Typed into while the request was in flight: what reached the server is already behind
        // the box. Saying "Saved" here would be a lie, and waiting for another keystroke to
        // schedule the next write would strand those words if the reviewer has stopped.
        say("Saving…", "is-saving");
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => save("idle"), IDLE_MS);
        return true;
      }
      say("Saved", "is-saved");
      return true;
    } catch {
      say("Not saved — keep this tab open", "is-failed");
      return false;
    } finally {
      saving = false;
      if (reason === "idle") idleTimer = null;
    }
  }

  /* An edit happened. Push the idle timer out, unless the ceiling has come due.
   *
   * Deliberately not conditional on there being unsaved work: app.js marks the document dirty from
   * its own listener on the field, which runs after this one, so the first event of an edit always
   * arrives here with nothing yet marked. A paste, an autofill or a one-character correction is
   * exactly one event, and gating on it here meant those were never saved at all. save() makes the
   * check for real at the moment it would write. */
  function noteEdit() {
    if (!draft()) return;
    if (sinceLastSave === null) sinceLastSave = Date.now();
    clearTimeout(idleTimer);
    if (Date.now() - sinceLastSave >= MAX_INTERVAL_MS) {
      save("ceiling");
      return;
    }
    idleTimer = setTimeout(() => save("idle"), IDLE_MS);
  }

  // Typing, pasting and the metadata selects all reach here; listening on the document rather than
  // on named fields means a field added to the screen later is covered without being registered.
  document.addEventListener("input", noteEdit, true);
  document.addEventListener("change", noteEdit, true);

  // Leaving a field is a natural moment to write, and cheaper than waiting out the idle timer.
  document.addEventListener("blur", (event) => {
    if (event.target && event.target.matches("textarea, input, select")) save("blur");
  }, true);

  // Switching tab or minimising is the case a reviewer never comes back from.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") save("hidden");
  });

  // Opening another document leaves the previous one's edits behind, so write them first.
  let lastDocument = null;
  setInterval(() => {
    const api = draft();
    if (!api) return;
    const id = api.documentId();
    if (id !== lastDocument) {
      if (lastDocument !== null) sinceLastSave = null;
      lastDocument = id;
    }
  }, 500);

  // save and noteEdit are published so the behaviour can be driven under node without a browser.
  window.__autosave__ = { save, noteEdit, IDLE_MS, MAX_INTERVAL_MS };
})();
