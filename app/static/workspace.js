/* Role-aware chrome layered over the existing review UI. */

(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* fetch only rejects on a network failure, never on a 4xx or 5xx. */
  async function getJSON(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) throw new Error(`${url} -> ${res.status}`);
    return res.json();
  }

  // Status pill wording, chosen so a reviewer reads it as an instruction rather than a state name.
  const STATUS = {
    unassigned: { label: "Not assigned", cls: "st-idle" },
    assigned:   { label: "To do",        cls: "st-todo" },
    submitted:  { label: `With ${term("admin", { lower: true })}`, cls: "st-wait" },
    returned:   { label: "Needs changes", cls: "st-back" },
    approved:   { label: "Approved",     cls: "st-done" },
  };

  const WORKFLOW_NAMES = { new: "Documents" };
  // What a folder's tab is called; the workflow name only for a payload that predates tabs.
  const tabLabel = (f) => f.tab_label || WORKFLOW_NAMES[f.workflow] || f.workflow;

  // The Team tab already says who is an editor and who a reviewer, so a name's role suffix is noise there.
  function plainName(name) {
    return String(name ?? "").replace(/\s*\((?:editor|owner|admin|reviewer)\)\s*$/i, "");
  }

  // Words that start with a letter only, so "Paul (Editor)" is "P" rather than "P(".
  function avatarInitials(name) {
    const words = String(name ?? "").trim().split(/\s+/).filter((w) => /^\p{L}/u.test(w));
    return words.slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
  }

  // "15 Oct, 3:16 PM" in the reader's own time, rather than the stored UTC stamp.
  function shortWhen(iso) {
    if (!iso) return "\u2014";
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return String(iso).slice(0, 16).replace("T", " ");
    const sameYear = when.getFullYear() === new Date().getFullYear();
    return when.toLocaleString(undefined, { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }),
                                            hour: "numeric", minute: "2-digit" });
  }

  let me = null;
  // Owner and admin both administer; only the owner holds the powers one person should.
  const isAdmin = () => !!me && (me.role === "admin" || me.role === "owner");
  const isOwner = () => !!me && me.role === "owner";
  let heartbeatSending = false;

  async function boot() {
    try {
      me = await getJSON("/api/me");
    } catch {
      return;  // not signed in; the gate is already redirecting
    }
    // A password someone else set is temporary: nothing else works until it is replaced.
    if (me.must_change_password) {
      demandNewPassword();
      return;
    }
    // The role goes on <body> before anything renders, so the stylesheet can hide controls that do
    // not exist yet.
    // The review track is the page's role: an owner reviews, and administers, as an admin does.
    document.body.dataset.role = isAdmin() ? "admin" : "reviewer";
    document.body.dataset.account = me.role;
    if (window.__setViewerId__) window.__setViewerId__(me.id);
    renderIdentity();
    installSidebarToggle();
    if (!isAdmin()) applyReviewerView();
    else applyAdminView();
  }

  function renderIdentity() {
    const bar = document.createElement("div");
    bar.className = "who-bar";
    // The initials stand in for a photo; the name opens the account menu, as the test on it says.
    const initials = avatarInitials(me.display_name || me.username);
    bar.innerHTML = `
      <span class="who-avatar who-${esc(me.role)}" aria-hidden="true">${esc(initials)}</span>
      <button type="button" class="who-name" id="who-name">${esc(me.display_name)}<svg class="who-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6l4 4 4-4"/></svg></button>
      <span class="who-role who-${esc(me.role)}">${esc(term(me.role))}</span>
      <button type="button" class="who-bell" id="who-bell" aria-label="Recent activity on your work"
              title="Recent activity on your work">
        <svg class="who-bell-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M18 8.5a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16s-2-1.5-2-6.5z"/>
          <path d="M10.3 19a2 2 0 0 0 3.4 0"/>
        </svg>
        <span class="who-bell-count" id="who-bell-count" hidden>0</span>
      </button>
      <button type="button" class="who-out" id="who-out">
        <svg class="who-out-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><path d="M15 16l4-4-4-4M19 12H9"/></svg>Sign out</button>`;
    document.body.prepend(bar);

    // The shell is sized 100vh, and this bar sits outside it, so without telling the stylesheet
    // how tall it is the page is always taller than the window by exactly this much -- which is
    // what pushed the review action buttons below the fold.
    const trackHeight = () => document.documentElement.style.setProperty(
      "--who-bar-h", `${Math.ceil(bar.getBoundingClientRect().height)}px`);
    trackHeight();
    if (window.ResizeObserver) new ResizeObserver(trackHeight).observe(bar);
    else window.addEventListener("resize", trackHeight);
    document.body.classList.add(`role-${document.body.dataset.role}`);
    if (me.role === "owner") document.body.classList.add("role-owner");
    window.__roleKnown__?.();

    const drawer = document.createElement("div");
    drawer.id = "notif-drawer";
    drawer.className = "notif-drawer hidden";
    document.body.appendChild(drawer);
    $("who-bell").addEventListener("click", toggleNotifications);
    $("who-name").addEventListener("click", toggleAccountMenu);
    $("who-out").addEventListener("click", async () => {
      await fetch("/logout", { method: "POST" });
      window.location.href = "/login";
    });
    loadNotifications();
    sendPresence();
    // Polled rather than pushed.
    setInterval(() => {
      loadNotifications();
      if (!document.hidden) sendPresence();
    }, 20000);

    // Coming back to the tab is the moment someone looks, so it is the moment worth being right.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        loadNotifications();
        sendPresence();
      }
    });
  }

  async function sendPresence() {
    // A slow or disconnected server must not let interval ticks accumulate pending heartbeats.
    if (heartbeatSending) return;
    heartbeatSending = true;
    const detail = $("detail-view");
    const documentId = detail && !detail.classList.contains("hidden")
      ? ($("doc-id")?.dataset.documentId || "") : "";
    const workflow = documentId
      ? (document.querySelector(".workflow-tab[data-workflow].active")?.dataset.workflow || "new")
      : "";
    try {
      await fetch("/api/presence", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: documentId, workflow }),
      });
    } catch { /* Presence is decoration; losing it must never interrupt review work. */ }
    finally { heartbeatSending = false; }
  }

  // ---------- your own account ----------

  function toggleAccountMenu() {
    const open = document.getElementById("account-menu");
    if (open) { open.remove(); return; }

    const menu = document.createElement("div");
    menu.id = "account-menu";
    menu.className = "account-menu";
    menu.innerHTML = `
      <div class="account-menu-who">${esc(me.username)}</div>
      ${isAdmin() ? `<button type="button" class="account-menu-item" id="account-colour">
        <span class="account-colour-dot" id="account-colour-dot"></span>My colour</button>` : ""}
      <button type="button" class="account-menu-item" id="account-password">Change my password</button>
      <button type="button" class="account-menu-close" id="account-close">Close my account</button>`;
    document.body.appendChild(menu);
    const name = $("who-name").getBoundingClientRect();
    menu.style.top = `${Math.ceil(name.bottom + 6)}px`;
    menu.style.left = `${Math.round(name.left)}px`;
    $("account-password").addEventListener("click", changePassword);
    const colourItem = $("account-colour");
    if (colourItem) {
      fetch("/api/reviewer-colours").then((r) => (r.ok ? r.json() : [])).then((colours) => {
        const mine = colours.find((c) => c.held_by === me.id);
        if (mine) $("account-colour-dot").style.background = mine.ink;
      });
      colourItem.addEventListener("click", (event) => {
        event.stopPropagation();
        openColourPicker(colourItem, () => {
          menu.remove();
          if (window.loadQueue) window.loadQueue();
        });
      });
    }
    $("account-close").addEventListener("click", closeAccount);
    setTimeout(() => document.addEventListener("click", function away(event) {
      if (menu.contains(event.target)) return;
      menu.remove();
      document.removeEventListener("click", away);
    }), 0);
  }

  // ---------- a reviewer's colour ----------

  /** The colours a reviewer can wear; one another reviewer already wears cannot be chosen. */
  async function openColourPicker(anchor, onChosen) {
    document.getElementById("colour-picker")?.remove();
    const response = await fetch("/api/reviewer-colours");
    if (!response.ok) return;
    const colours = await response.json();
    const picker = document.createElement("div");
    picker.id = "colour-picker";
    picker.className = "colour-picker";
    picker.setAttribute("role", "dialog");
    picker.setAttribute("aria-label", "Choose your colour");
    picker.innerHTML = `
      <div class="colour-picker-title">Your colour</div>
      <div class="colour-picker-hint">Shown beside your name on every folder you review.</div>
      <div class="colour-picker-grid">
        ${colours.map((c) => {
          const current = c.held_by === me.id;
          const taken = c.held_by != null && !current;
          const who = taken ? `${esc(c.held_by_name)}'s colour` : c.key;
          return `<button type="button" class="colour-swatch${current ? " is-current" : ""}"
                    data-colour="${c.key}" style="--swatch-bg:${c.bg};--swatch-ink:${c.ink}"
                    title="${who}" aria-label="${who}" ${taken ? "disabled" : ""}>
                    <span class="colour-swatch-dot"></span></button>`;
        }).join("")}
      </div>`;
    document.body.appendChild(picker);
    const box = anchor.getBoundingClientRect();
    picker.style.top = `${Math.ceil(box.bottom + 8)}px`;
    picker.style.left = `${Math.max(8, Math.min(Math.round(box.left),
                                               window.innerWidth - picker.offsetWidth - 8))}px`;
    picker.addEventListener("click", async (event) => {
      event.stopPropagation();
      const swatch = event.target.closest("[data-colour]");
      if (!swatch || swatch.disabled) return;
      const chosen = await fetch("/api/reviewer-colours/mine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ colour: swatch.dataset.colour }),
      });
      if (!chosen.ok) {
        const body = await chosen.json().catch(() => ({}));
        alert(body.detail || "That colour could not be set.");
        return;
      }
      picker.remove();
      if (onChosen) onChosen(swatch.dataset.colour);
    });
    setTimeout(() => document.addEventListener("click", function away(event) {
      if (picker.contains(event.target)) return;
      picker.remove();
      document.removeEventListener("click", away);
    }), 0);
  }
  window.openColourPicker = openColourPicker;

  function demandNewPassword() {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <form class="modal forced-password" role="dialog" aria-modal="true" aria-labelledby="forced-title">
        <h3 id="forced-title">Choose your own password</h3>
        <p class="rework-hint">The password you signed in with was set for you. Choose one only you know.</p>
        <label class="field">The password you were given<input type="password" name="current" required autocomplete="current-password" /></label>
        <label class="field">New password<input type="password" name="new" required minlength="8" autocomplete="new-password" /></label>
        <label class="field">New password again<input type="password" name="again" required minlength="8" autocomplete="new-password" /></label>
        <p class="forced-error auth-error" hidden></p>
        <div class="modal-actions">
          <a class="btn btn-secondary" href="#" data-sign-out>Sign out</a>
          <button type="submit" class="btn btn-accept">Save password</button>
        </div>
      </form>`;
    document.body.appendChild(overlay);
    const form = overlay.querySelector("form");
    const error = overlay.querySelector(".forced-error");
    overlay.querySelector("[data-sign-out]").addEventListener("click", async (event) => {
      event.preventDefault();
      await fetch("/logout", { method: "POST" });
      window.location.href = "/login";
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fields = Object.fromEntries(new FormData(form));
      if (fields.new !== fields.again) {
        error.textContent = "The two new passwords are not the same.";
        error.hidden = false;
        return;
      }
      const response = await fetch("/api/me/password", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current: fields.current, new: fields.new }),
      });
      if (!response.ok) {
        error.textContent = (await response.json().catch(() => ({}))).detail || "That did not work; try again.";
        error.hidden = false;
        return;
      }
      // A changed password ends every session, this one included.
      window.location.href = "/login";
    });
    form.querySelector("input").focus();
  }

  async function changePassword() {
    const current = window.prompt("Your current password:");
    if (!current) return;
    const next = window.prompt("Your new password:");
    if (!next) return;
    const response = await fetch("/api/me/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current, new: next }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "The password could not be changed.");
      return;
    }
    // Changing a password ends every session it was good for, this one included.
    alert("Password changed. Please sign in again.");
    window.location.href = "/login";
  }

  async function closeAccount() {
    // Typed rather than clicked.
    const typed = window.prompt(
      "Closing your account signs you out and ends your access for good.\n\n"
      + "Work you have already done stays credited to you in the changelog.\n\n"
      + `Type ${me.username} to confirm.`);
    if (typed === null) return;
    if (typed.trim() !== me.username) {
      alert("That did not match your username, so nothing was changed.");
      return;
    }
    const response = await fetch("/api/me/close", { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "The account could not be closed.");
      return;
    }
    window.location.href = "/login";
  }

  // ---------- notifications ----------

  let notifications = { unread: 0, items: [] };

  async function loadNotifications() {
    try {
      notifications = await getJSON("/api/notifications");
    } catch {
      return;   // a failed poll must never break the page it decorates
    }
    // Before the badge work below, which returns early when the bell is not on the page.
    noteWorkspaceVersion(notifications.version);

    const badge = $("who-bell-count");
    if (!badge) return;
    // Coerced rather than trusted: this is a count rendered straight into the chrome, so anything
    // that is not a number must read as none rather than as itself.
    const unread = Number(notifications.unread) || 0;
    badge.textContent = unread > 9 ? "9+" : String(unread);
    badge.hidden = unread === 0;
    const bell = $("who-bell");
    bell?.classList.toggle("has-unread", unread > 0);
    // A single nudge when the count actually goes up.
    if (bell && unread > lastUnread) {
      bell.classList.remove("just-arrived");
      void bell.offsetWidth;
      bell.classList.add("just-arrived");
    }
    lastUnread = unread;
  }

  let lastUnread = 0;

  const NOTIF_WORDING = {
    assigned: "assigned you", reassigned: "moved", submitted: "sent for review",
    returned: "sent back", approved: "approved",
  };

  async function toggleNotifications() {
    const drawer = $("notif-drawer");
    const opening = drawer.classList.contains("hidden");
    drawer.classList.toggle("hidden", !opening);
    if (!opening) return;

    // Same reason as the badge: the drawer must open even when the last poll came back shaped
    // wrong, rather than throwing on `.length` of undefined and leaving an empty panel.
    const items = notifications.items || [];
    drawer.innerHTML = items.length
      ? `<h3>Recent activity</h3><ul>` + items.map((n) => `
          <li class="${n.unread ? "is-unread" : ""}">
            <b>${esc(n.by)}</b> ${esc(NOTIF_WORDING[n.action] || n.action)}
            <code>${esc(n.folder_display_id || n.folder_id)}</code>
            ${n.note ? `<div class="notif-note">${esc(n.note)}</div>` : ""}
            <time>${esc((n.at || "").slice(0, 16).replace("T", " "))}</time>
          </li>`).join("") + "</ul>"
      : `<h3>Recent activity</h3><p class="notif-empty">Nothing yet.</p>`;

    // Marked read on open rather than on load: a badge that clears itself because a poll ran is a
    // badge that hides things the person never saw.
    if (notifications.unread) {
      await fetch("/api/notifications/read", { method: "POST" });
      loadNotifications();
    }
  }

  document.addEventListener("click", (e) => {
    const drawer = $("notif-drawer");
    if (!drawer || drawer.classList.contains("hidden")) return;
    if (!e.target.closest("#notif-drawer") && !e.target.closest("#who-bell")) {
      drawer.classList.add("hidden");
    }
  });

  // ---------- reviewer ----------

  function applyReviewerView() {
    // Removed from the DOM, not hidden, so they cannot be re-shown from devtools and mistaken for
    // a working feature.
    const REVIEWER_MUST_NOT_SEE = [
      "folder-export",
      "folder-delete", "btn-delete-document",
    ];
    REVIEWER_MUST_NOT_SEE.forEach((id) => $(id)?.remove());

    // The gear stays, but only as a way into the guide.
    ["settings", "deletions", "site", "appearance", "prompt", "tabs", "models", "usage", "backups", "updates", "access"].forEach((name) => {
      document.querySelector(`[data-settings-tab="${name}"]`)?.remove();
      $("settings-panel-" + name)?.remove();
    });
    // The administrator's guide goes with them.
    document.querySelector('[data-info-role="admin"]')?.remove();
    $("info-role-admin")?.remove();
    $("btn-settings")?.setAttribute("title", "How It Works");

    // No separate "my work" panel.
    tidyPipelineSection();   // after the removals above, or the heading outlives its buttons
    loadMyFolders();   // drives the workflow auto-switch and the send-back handler

  }

  // A heading over an empty space.
  function tidyPipelineSection() {
    const section = $("pipeline-section");
    if (!section) return;
    const shown = [$("new-pipeline-actions")]
      .find((box) => box && !box.classList.contains("hidden"));
    section.style.display = shown && shown.querySelector(".nav-action") ? "" : "none";
  }

  // The workflow tabs swap which actions box is shown, so the answer changes with them.
  document.addEventListener("click", (e) => {
    if (e.target.closest(".workflow-tab[data-workflow]")) setTimeout(tidyPipelineSection, 60);
  });

  async function loadMyFolders() {
    const { folders } = await getJSON("/api/my/folders");
    if (!folders.length) return;
    // Land on the pipeline the work is actually in.
    switchToWorkflowWithWork(folders);

  }

  let switchedOnce = false;
  function switchToWorkflowWithWork(folders) {
    // Only ever on the first load: after that the reviewer is steering, and yanking the tab from
    // under them every 30 seconds would be worse than the problem it solves.
    if (switchedOnce || !folders.length) return;
    switchedOnce = true;
    const wanted = folders.find((f) => f.tab_id)?.tab_id;
    const tab = document.querySelector(`.workflow-tab[data-tab="${wanted}"]`);
    if (!tab) { switchedOnce = false; return; }
    if (!tab.classList.contains("active")) tab.click();
  }

  function askSubmissionNote(shown) {
    const adminWord = term("admin", { lower: true });
    return new Promise((resolve) => {
      document.querySelector(".submit-overlay")?.remove();
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay submit-overlay";
      overlay.innerHTML = `
        <div class="modal submit-modal" role="dialog" aria-modal="true"
             aria-labelledby="submit-note-title">
          <h3 id="submit-note-title">Send ${esc(shown || "this folder")} to the ${esc(adminWord)}</h3>
          <p class="rework-hint">You can include a note for the ${esc(adminWord)}, or send it without one.</p>
          <label class="rework-note-label" for="submit-note">Note <span>(optional)</span></label>
          <textarea id="submit-note" rows="3" maxlength="4000"
                    placeholder="Anything the ${esc(adminWord)} should know?"></textarea>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" data-submit-note="cancel">Cancel</button>
            <button type="button" class="btn btn-accept" data-submit-note="send">Send to ${esc(adminWord)}</button>
          </div>
        </div>`;
      const close = (value) => {
        document.removeEventListener("keydown", onKey);
        overlay.remove();
        resolve(value);
      };
      const onKey = (event) => { if (event.key === "Escape") close(null); };
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay || event.target.closest('[data-submit-note="cancel"]')) {
          close(null);
        } else if (event.target.closest('[data-submit-note="send"]')) {
          close(overlay.querySelector("#submit-note").value.trim());
        }
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(overlay);
      overlay.querySelector("#submit-note").focus();
    });
  }

  document.addEventListener("click", async (e) => {
    // The button now lives on the folder card in the review queue (app.js renderAssignment), so
    // the handler matches that rather than the old panel's markup.
    const submit = e.target.closest(".folder-send") || e.target.closest("[data-submit]");
    if (submit) {
      e.stopPropagation();   // the card itself opens the folder; the button must not do both
      const note = await askSubmissionNote(submit.dataset.shown);
      if (note === null) return;
      const res = await fetch("/api/my/submit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          folder_id: submit.dataset.folder || submit.dataset.submit,
          workflow: submit.dataset.workflow || "new", note,
        }),
      });
      if (!res.ok) alert((await res.json()).detail || "Could not send that folder");
      else alert(`Sent to your ${term("admin", { lower: true })}.`);
      if (window.loadQueue) window.loadQueue();
      loadMyFolders();
    }
    const open = e.target.closest("[data-open]");
    if (open && window.openFolder) window.openFolder(open.dataset.open);
  });

  // ---------- the How It Works guides ---------- Two guides, one per role, opening on the
  // reader's own by default -- an admin who wants the reviewer's view can still switch to it, which
  // is worth having when they are answering a reviewer's question.

  function selectInfoRole(role) {
    document.querySelectorAll(".info-role-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.infoRole === role);
    });
    document.querySelectorAll(".info-role-panel").forEach((s) => {
      s.classList.add("hidden");
    });
    $("info-role-" + role)?.classList.remove("hidden");
  }

  async function fillLiveExamples() {
    if (!me) return;   // module-level, set by boot() from /api/me
    const host = $(isAdmin() ? "info-live-admin" : "info-live-reviewer");
    if (!host) return;

    // An admin holds no assignments of their own, so /api/my/folders would always be empty for
    // them -- the roster is their equivalent view, every assignment rather than only theirs.
    let folders = [];
    try {
      if (isAdmin()) {
        const res = await fetch("/api/team/roster");
        if (res.ok) folders = ((await res.json()).roster || []).map((r) => ({
          folder_id: r.folder_id, folder_display_id: r.folder_display_id, status: r.status,
          assignee_name: r.reviewer,
        }));
      } else {
        const res = await fetch("/api/my/folders");
        if (res.ok) folders = (await res.json()).folders || [];
      }
    } catch (err) {
      // The guide is worth reading with or without the example; a failed fetch just means the
      // reader sees the prose, which is the part that actually explains the site.
      return;
    }

    if (!folders.length) {
      host.innerHTML = `<div class="info-live-card info-live-empty">
        <b>Right now:</b> you are signed in as <b>${esc(me.display_name)}</b> (${esc(term(me.role).toLowerCase())}) and
        ${isAdmin()
          ? "no folders have been assigned to anyone yet. Assign one in the Team tab and it will appear here."
          : "nothing is assigned to you yet. When an administrator assigns you a folder it will show up in your queue and the bell will tell you."}
      </div>`;
      return;
    }

    const byStatus = {};
    folders.forEach((f) => { byStatus[f.status || "assigned"] = (byStatus[f.status || "assigned"] || 0) + 1; });
    const sample = folders[0];

    host.innerHTML = `<div class="info-live-card">
      <b>Right now:</b> signed in as <b>${esc(me.display_name)}</b> (${esc(term(me.role).toLowerCase())}) with
      <b>${folders.length}</b> folder${folders.length === 1 ? "" : "s"} in view &mdash;
      ${Object.entries(byStatus).map(([k, v]) => `${v} ${esc(k)}`).join(", ")}.
      <div class="info-live-eg">
        For example, <code>${esc(sample.folder_display_id || sample.folder_id)}</code> is
        <b>${esc(sample.status || "assigned")}</b>${sample.assignee_name ? `, held by ${esc(sample.assignee_name)}` : ""}.
        ${sample.status === "submitted" && isAdmin()
          ? " Because it is submitted, your Send back button is live on it."
          : ""}
        ${sample.status === "returned" && !isAdmin()
          ? " Because it was returned, it is editable by you again &mdash; the note says what to fix."
          : ""}
        ${sample.status === "approved" ? " Because it is approved, it is locked and exportable." : ""}
      </div>
    </div>`;
  }

  document.addEventListener("click", (e) => {
    const tab = e.target.closest(".info-role-tab");
    if (tab) selectInfoRole(tab.dataset.infoRole);
  });

  // The appearance preview's motion, stopped when another settings tab opens.
  let stopEditor = null;

  // Fill the settings tab being opened: the site's own settings fresh each time, as someone may have
  // changed them, and the guide on the reader's own role.
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-settings-tab]");
    if (t && stopEditor) { stopEditor(); stopEditor = null; }
    if (t && (t.dataset.settingsTab === "site" || t.dataset.settingsTab === "appearance") && window.SiteEditors) {
      const panel = $("settings-panel-" + t.dataset.settingsTab);
      const mount = t.dataset.settingsTab === "site" ? window.SiteEditors.mountSite : window.SiteEditors.mountAppearance;
      if (panel) mount(panel, { owner: isOwner() }).then((stop) => { stopEditor = stop || null; }).catch((error) => {
        panel.textContent = `Could not load the settings: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "prompt" && window.PromptBuilder) {
      const panel = $("settings-panel-prompt");
      if (panel) window.PromptBuilder.mount(panel).then((stop) => { stopEditor = stop || null; }).catch((error) => {
        panel.textContent = `Could not load the prompt profiles: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "tabs" && window.TabsAdmin) {
      const panel = $("settings-panel-tabs");
      if (panel) window.TabsAdmin.mount(panel).catch((error) => {
        panel.textContent = `Could not load the tabs: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "models" && window.ModelsPanel) {
      const panel = $("settings-panel-models");
      if (panel) window.ModelsPanel.mount(panel, { owner: isOwner() }).catch((error) => {
        panel.textContent = `Could not load the model settings: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "usage" && window.UsagePanel) {
      const panel = $("settings-panel-usage");
      if (panel) window.UsagePanel.mount(panel).catch((error) => {
        panel.textContent = `Could not load the usage: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "backups" && window.BackupsPanel) {
      const panel = $("settings-panel-backups");
      if (panel) window.BackupsPanel.mount(panel, { owner: isOwner() }).catch((error) => {
        panel.textContent = `Could not load the backups: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "updates" && window.UpdatesPanel) {
      const panel = $("settings-panel-updates");
      if (panel) window.UpdatesPanel.mount(panel, { owner: isOwner() }).catch((error) => {
        panel.textContent = `Could not load the updates: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "access" && window.AccessPanel) {
      const panel = $("settings-panel-access");
      if (panel) window.AccessPanel.mount(panel, { owner: isOwner() }).catch((error) => {
        panel.textContent = `Could not load the access settings: ${error.message}`;
      });
    }
    if (t && t.dataset.settingsTab === "info") {
      const role = isAdmin() ? "admin" : "reviewer";
      if (!document.querySelector(".info-role-tab.active")) selectInfoRole(role);
      fillLiveExamples();
    }
  });

  // ---------- keeping the queue current without disturbing anyone ---------- The bell announced
  // an assignment the queue behind it never showed, so the folder was invisible until a reload.

  let workspaceVersion = null;
  let queueIsStale = false;

  function detailIsOpen() {
    const detail = $("detail-view");
    return !!detail && !detail.classList.contains("hidden");
  }

  function noteWorkspaceVersion(version) {
    if (version === undefined || version === null) return;
    if (workspaceVersion === null) { workspaceVersion = version; return; }   // first poll: baseline
    if (version === workspaceVersion) return;
    workspaceVersion = version;
    queueIsStale = true;
    refreshQueueIfSafe();
  }

  function refreshQueueIfSafe() {
    if (!queueIsStale) return;
    // Someone is reading or editing a document.
    if (detailIsOpen()) return;
    queueIsStale = false;
    window.loadQueue?.();
    switchedOnce = false;      // a first assignment should still be able to land the right tab
    loadMyFolders();
  }

  // Applied on the way back to the queue, which is the first moment it is safe.
  document.addEventListener("click", (e) => {
    if (e.target.closest("#back-to-queue")) setTimeout(refreshQueueIfSafe, 250);
  });

  // ---------- collapsing the sidebar ---------- A reviewer comparing a scan against its
  // transcription wants the window, not the navigation.

  function installSidebarToggle() {
    const nav = document.querySelector("nav.sidebar") || document.querySelector(".sidebar");
    if (!nav || $("sidebar-toggle")) return;

    const toggle = document.createElement("button");
    toggle.id = "sidebar-toggle";
    toggle.type = "button";
    toggle.className = "sidebar-toggle";
    toggle.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>
      <span class="sidebar-toggle-label">Collapse</span>`;
    // Inside the sidebar, at its foot: floating over the page, it sat on the header's role badge.
    nav.appendChild(toggle);

    const apply = (collapsed) => {
      document.body.classList.toggle("sidebar-collapsed", collapsed);
      toggle.title = collapsed ? "Show the sidebar" : "Hide the sidebar";
      toggle.setAttribute("aria-label", toggle.title);
      toggle.setAttribute("aria-expanded", String(!collapsed));
    };

    let collapsed = false;
    try { collapsed = localStorage.getItem("incipit.sidebar") === "collapsed"; } catch { /* private mode */ }
    apply(collapsed);

    toggle.addEventListener("click", () => {
      collapsed = !collapsed;
      apply(collapsed);
      try { localStorage.setItem("incipit.sidebar", collapsed ? "collapsed" : "open"); } catch { /* fine */ }
    });
  }

  // ---------- comments on a document ---------- The feature both comparable platforms treat as
  // central, and the obvious gap in our own round trip: "check the date on AAC" had to be written
  // as a *folder* note because there was nowhere to attach it to AAC.

  let commentsFor = null;

  function ensureCommentPanel() {
    if ($("doc-comments")) return $("doc-comments");
    // Verified against the rendered page rather than guessed: the detail view is #detail-view and
    // #doc-id carries the open document's internal id in data-document-id.
    const host = document.querySelector(".info-scroll") || $("detail-view")
      || document.querySelector("main");
    if (!host) return null;
    const panel = document.createElement("section");
    panel.id = "doc-comments";
    panel.className = "doc-comments hidden";
    panel.innerHTML = `
      <h3>Comments <span id="doc-comments-count" class="cmt-count"></span>
        <button type="button" id="doc-history-toggle" class="btn btn-ghost btn-small">History</button>
      </h3>
      <div id="doc-history" class="doc-history hidden"></div>
      <div id="doc-comments-list" class="cmt-list"></div>
      <form id="doc-comment-form" class="cmt-form">
        <textarea id="doc-comment-body" rows="2" maxlength="4000"
                  placeholder="Note a problem on this document…"></textarea>
        <button class="btn btn-secondary btn-small" type="submit">Add comment</button>
      </form>`;
    host.appendChild(panel);
    $("doc-comment-form").addEventListener("submit", postComment);
    $("doc-history-toggle").addEventListener("click", toggleHistory);
    return panel;
  }

  // The control lives with the status pill and Delete Document, in .doc-header-right -- per
  // document controls belong together, and that row is already where a reader looks to see what
  // state the document is in.
  function ensureCommentButton() {
    if ($("btn-comments")) return $("btn-comments");
    const row = document.querySelector(".doc-header-right");
    if (!row) return null;
    const button = document.createElement("button");
    button.id = "btn-comments";
    button.type = "button";
    button.className = "btn btn-secondary btn-small btn-comments";
    button.innerHTML = `Comments <span id="btn-comments-count" class="cmt-pill hidden"></span>`;
    button.addEventListener("click", showTheComments);
    // Before Delete Document, so the destructive control stays last in the row.
    row.insertBefore(button, $("btn-delete-document") || null);
    return button;
  }

  /* The comments are always there, at the foot of the document. */
  function showTheComments() {
    const panel = $("doc-comments");
    if (!panel) return;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("doc-comment-body")?.focus({ preventScroll: true });
  }

  async function showComments(documentId, workflow) {
    const panel = ensureCommentPanel();
    if (!panel || !documentId) return;
    commentsFor = { documentId, workflow: workflow || "new" };
    const target = commentsFor;
    const isCurrent = () => commentsFor === target && panel.isConnected;
    panel.classList.remove("hidden");
    $("doc-history")?.classList.add("hidden");   // history is per document, not sticky
    ensureCommentButton();
    const res = await fetch(
      `/api/documents/${encodeURIComponent(documentId)}/comments?workflow=${encodeURIComponent(commentsFor.workflow)}`);
    if (!isCurrent()) return;
    if (!res.ok) { panel.classList.add("hidden"); return; }
    const { comments } = await res.json();
    if (!isCurrent()) return;
    const open = comments.filter((c) => !c.resolved).length;
    $("doc-comments-count").textContent = open ? `${open} open` : "";
    $("doc-comments-count").className = "cmt-count" + (open ? " has-open" : "");

    // A collapsed panel can hide the fact that someone has asked a question, so the count rides on
    // the button that opens it.
    const pill = $("btn-comments-count");
    if (pill) {
      pill.textContent = open || "";
      pill.classList.toggle("hidden", !open);
    }
    $("btn-comments")?.classList.toggle("has-open", !!open);
    $("doc-comments-list").innerHTML = commentListHTML(comments);
  }

  /* One comment thread as markup. Shared, so every kind of comment looks and
     read exactly as a document's do; it passes its own attribute for the resolve control. */
  function commentListHTML(comments, { resolveAttr = "data-resolve",
                                       empty = "No comments on this document." } = {}) {
    return comments.length ? comments.map((c) => `
      <article class="cmt ${c.resolved ? "is-resolved" : ""}">
        <header><b>${esc(c.by)}</b>
          <span class="cmt-role">${esc(c.by_role)}</span>
          <time>${esc((c.at || "").slice(0, 16).replace("T", " "))}</time>
        </header>
        <p>${esc(c.body)}</p>
        ${c.resolved ? `<span class="cmt-done">Resolved</span>`
          : `<button type="button" class="btn btn-ghost btn-small" ${resolveAttr}="${c.id}">Mark resolved</button>`}
      </article>`).join("")
      : `<p class="cmt-empty">${esc(empty)}</p>`;
  }

  function toggleHistory() {
    const box = $("doc-history");
    if (!box || !commentsFor) return;
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.innerHTML = `<p class="cmt-empty">Loading&hellip;</p>`;
    loadHistory();
  }

  /* Re-read the history of the document on screen, if its panel is open. Called after a save so an
     edit appears in the list that is describing it, rather than on the next manual reopen. */
  function refreshHistory() {
    const box = $("doc-history");
    if (box && !box.classList.contains("hidden") && commentsFor) loadHistory();
  }

  // Autosave refreshes the panel every few seconds, so only the newest request may draw it.
  let historyRequest = 0;

  async function loadHistory() {
    const box = $("doc-history");
    if (!box || !commentsFor) return;
    const request = ++historyRequest;
    const documentId = commentsFor.documentId;
    const current = () => request === historyRequest && commentsFor?.documentId === documentId;

    const res = await fetch(
      `/api/documents/${encodeURIComponent(documentId)}/revisions` +
      `?workflow=${encodeURIComponent(commentsFor.workflow)}`);
    if (!current()) return;
    if (!res.ok) { box.innerHTML = `<p class="cmt-empty">History is not available here.</p>`; return; }

    const { revisions } = await res.json();
    if (!current()) return;
    if (!revisions.length) {
      box.innerHTML = `<p class="cmt-empty">No edits recorded yet &mdash; this document is as it
        was drafted.</p>`;
      return;
    }
    box.innerHTML = historyHTML(revisions);
  }

  /* A document's history as markup, newest edits with their word-by-word changes. Shared with the
     Shared by every screen that shows a history. */
  function historyHTML(revisions) {
    return revisions.map((r) => (r.field === MERGE_NOTE_FIELD
        ? `<div class="rev rev-merge">
            <header><b>Merged</b> from ${esc(r.current || "")} by ${esc(r.by || "unknown")}
              <time>${esc((r.at || "").slice(0, 16).replace("T", " "))}</time></header>
          </div>`
        : r.field === REDRAFT_NOTE_FIELD
        ? `<div class="rev rev-merge">
            <header><b>Redrafted</b> by ${esc(r.by || "unknown")}
              <time>${esc((r.at || "").slice(0, 16).replace("T", " "))}</time></header>
          </div>`
        : r.field === CONTEXT_PASS_FIELD
        ? `<div class="rev">
            <header><b>Readings proposed for [unclear]</b> by the free model, still marked uncertain
              <time>${esc((r.at || "").slice(0, 16).replace("T", " "))}</time></header>
            ${revisionDiff(r)}
          </div>`
        : `<div class="rev">
            <header><b>${esc(r.label || r.field)}</b> changed by ${esc(r.by || "unknown")}
              <time>${esc((r.at || "").slice(0, 16).replace("T", " "))}</time></header>
            ${revisionDiff(r)}
          </div>`)).join("");
  }

  // Not a field anybody edited: the history's note that this document is several joined. Mirrors
  // collab.MERGE_NOTE_FIELD.
  const MERGE_NOTE_FIELD = "merged_from";
  // Mirrors collab.REDRAFT_NOTE_FIELD: the model replaced the text, nobody edited it.
  const REDRAFT_NOTE_FIELD = "redrafted";
  // The [unclear] context pass's history field: no person made this change, so it names none.
  const CONTEXT_PASS_FIELD = "unclear_proposals";

  function revisionDiff(revision) {
    const before = revisionValue(revision.field, revision.previous);
    if (!revision.has_current) {
      // Rows made before complete diffs were introduced genuinely do not contain the replacement.
      // Say only what the database knows rather than presenting an empty value as a deletion.
      return `<div class="rev-diff rev-legacy"><span class="rev-label">previously</span>
        <span>${esc(truncate(before))}</span></div>`;
    }
    const after = revisionValue(revision.field, revision.current);
    if (!before && after) {
      return `<div class="rev-diff"><span class="rev-label">added</span>
        <ins>${esc(truncate(after))}</ins></div>`;
    }
    if (before && !after) {
      return `<div class="rev-diff"><span class="rev-label">removed</span>
        <del>${esc(truncate(before))}</del></div>`;
    }
    // A list gained an entry: say which one, rather than striking the whole field through and
    // retyping it. Marginal notes, places, names and challenges are all lists, and adding one note
    // read as deleting everything and writing it again.
    const itemDiff = listDiff(revision.field, revision.previous, revision.current);
    if (itemDiff) return itemDiff;
    // Text that grew or shrank at one end: show the part that moved, not the whole paragraph.
    if (after.startsWith(before)) {
      return `<div class="rev-diff"><span class="rev-label">added</span>
        <ins>${esc(truncate(after.slice(before.length).trim()))}</ins></div>`;
    }
    if (before.startsWith(after)) {
      return `<div class="rev-diff"><span class="rev-label">removed</span>
        <del>${esc(truncate(before.slice(after.length).trim()))}</del></div>`;
    }
    // A word changed in the middle: show that word, not the paragraph around it. Without this a
    // one-word correction struck the whole transcription through and retyped it, which says
    // nothing about what the edit was.
    const inline = wordDiff(before, after);
    if (inline) {
      const lead = `${inline.clippedLead ? "&hellip;" : ""}${esc(inline.lead)}`;
      const trail = `${esc(inline.trail)}${inline.clippedTrail ? "&hellip;" : ""}`;
      return `<div class="rev-diff rev-change">
        <span class="rev-context">${lead}</span>
        ${inline.removed ? `<del>${esc(truncate(inline.removed))}</del>` : ""}
        ${inline.added ? `<ins>${esc(truncate(inline.added))}</ins>` : ""}
        <span class="rev-context">${trail}</span></div>`;
    }
    return `<div class="rev-diff rev-change">
      <span class="rev-label">from</span><del>${esc(truncate(before))}</del>
      <span class="rev-label">to</span><ins>${esc(truncate(after))}</ins></div>`;
  }

  /* Words with the whitespace that follows them, so the pieces rejoin exactly as they were. */
  function words(text) {
    return String(text === null || text === undefined ? "" : text).match(/\s*\S+\s*/g) || [];
  }

  // Enough of the sentence either side to place the change on the page, without reprinting it.
  const DIFF_CONTEXT_WORDS = 6;

  /* The one stretch that changed, with a little of the text either side, or null when the two
     share no beginning and no end and the field really was rewritten. */
  function wordDiff(before, after) {
    const was = words(before), now = words(after);
    let head = 0;
    while (head < was.length && head < now.length && was[head] === now[head]) head++;
    let tail = 0;
    while (tail < was.length - head && tail < now.length - head
           && was[was.length - 1 - tail] === now[now.length - 1 - tail]) tail++;
    if (!head && !tail) return null;
    const leadFrom = Math.max(0, head - DIFF_CONTEXT_WORDS);
    const trailEnd = was.length - tail + DIFF_CONTEXT_WORDS;
    return {
      lead: was.slice(leadFrom, head).join("").trimStart(),
      removed: was.slice(head, was.length - tail).join("").trim(),
      added: now.slice(head, now.length - tail).join("").trim(),
      trail: was.slice(was.length - tail, trailEnd).join("").trimEnd(),
      clippedLead: leadFrom > 0,
      clippedTrail: trailEnd < was.length,
    };
  }

  /* Both sides of a list field as arrays, or null when the field does not hold one. */
  // A catalogue field's list value is stored as JSON; a string field never starts with "[".
  function isListField(field, value) {
    const name = String(field || "");
    return name.endsWith("_json") || (name.startsWith("field:")
      && (value === null || value === undefined || String(value).startsWith("[")));
  }

  function revisionList(field, value) {
    if (!isListField(field, value)) return null;
    try {
      const items = JSON.parse(value === null || value === undefined ? "[]" : value);
      return Array.isArray(items) ? items.map((item) => String(item)) : null;
    } catch {
      return null;                      // an older row holding plain text is not a list
    }
  }

  /* What entries a list gained and lost, or "" when this is not a list edit worth itemising. */
  function listDiff(field, previous, current) {
    const before = revisionList(field, previous);
    const after = revisionList(field, current);
    if (!before || !after) return "";
    const added = after.filter((item) => !before.includes(item));
    const removed = before.filter((item) => !after.includes(item));
    if (!added.length && !removed.length) {
      // Same entries, different order: real, and not a rewrite.
      return `<div class="rev-diff"><span class="rev-label">reordered</span>
        <span>${esc(truncate(after.join(", ")))}</span></div>`;
    }
    const kept = after.length - added.length;
    const parts = [];
    if (added.length) {
      parts.push(`<span class="rev-label">added</span><ins>${esc(truncate(added.join(", ")))}</ins>`);
    }
    if (removed.length) {
      parts.push(`<span class="rev-label">removed</span><del>${esc(truncate(removed.join(", ")))}</del>`);
    }
    const untouched = kept
      ? `<span class="rev-kept">${kept} other entr${kept === 1 ? "y" : "ies"} unchanged</span>` : "";
    return `<div class="rev-diff rev-change">${parts.join(" ")}${untouched}</div>`;
  }

  function revisionValue(field, value) {
    if (value === null || value === undefined) return "";
    if (field === "reviewed_pages") {
      try {
        const pages = JSON.parse(value);
        if (Array.isArray(pages)) return pages.map((page, index) => [
          `Page position ${index + 1}: ${page.position} (${page.content})`,
          ...(page.lines || []),
          ...(page.tables || []).flatMap(table => [table.caption || "", (table.columns || []).join(" | "),
            ...(table.rows || []).map(row => row.join(" | "))]),
          ...(page.drawings || []).flatMap(drawing => [drawing.summary || "", ...(drawing.labels || [])]),
          `People: ${(page.people || []).join(", ")}`, `Places: ${(page.places || []).join(", ")}`,
        ].filter(Boolean).join("\n")).join("\n\n");
      } catch { /* retain malformed legacy history without losing it */ }
    }
    if (isListField(field, value)) {
      try {
        const items = JSON.parse(value);
        if (Array.isArray(items)) return items.join(", ");
      } catch { /* retain old malformed/plain values verbatim */ }
    }
    return String(value);
  }

  function truncate(value, limit = 220) {
    const text = (value === null || value === undefined) ? "" : String(value);
    if (!text) return "(empty)";
    return text.length > limit ? text.slice(0, limit) + "\u2026" : text;
  }

  async function postComment(e) {
    e.preventDefault();
    if (!commentsFor) return;
    const body = $("doc-comment-body").value.trim();
    if (!body) return;
    const res = await fetch(`/api/documents/${encodeURIComponent(commentsFor.documentId)}/comments`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body, workflow: commentsFor.workflow }),
    });
    if (!res.ok) { alert((await res.json()).detail || "Could not add that comment"); return; }
    $("doc-comment-body").value = "";
    showComments(commentsFor.documentId, commentsFor.workflow);
  }

  document.addEventListener("click", async (e) => {
    const resolve = e.target.closest("[data-resolve]");
    if (!resolve) return;
    await fetch(`/api/comments/${resolve.dataset.resolve}/resolve`, { method: "POST" });
    if (commentsFor) showComments(commentsFor.documentId, commentsFor.workflow);
  });

  // The detail view is app.js's; rather than reach into it, watch for the document id it renders.
  let lastDocument = null;
  setInterval(() => {
    const detail = $("detail-view");
    const visible = detail && !detail.classList.contains("hidden");
    const id = visible ? ($("doc-id")?.dataset.documentId || null) : null;
    if (id && id !== lastDocument) {
      lastDocument = id;
      showComments(id, document.querySelector(".workflow-tab.active")?.dataset.workflow || "new");
      sendPresence();
    } else if (!id && lastDocument) {
      lastDocument = null;
      $("doc-comments")?.classList.add("hidden");
      $("btn-comments")?.remove();   // the header is rebuilt per document; do not leave a stale one
      sendPresence();
    }
  }, 900);

  // ---------- admin ----------

  function applyAdminView() {
    const nav = $("workflow-tabs");
    const queueView = $("queue-view");
    if (!nav || !queueView) return;

    // Its own class rather than `.workflow-tab`.
    const tab = document.createElement("button");
    tab.id = "tab-team";
    tab.className = "workflow-tab team-tab";
    tab.textContent = "Team";
    nav.appendChild(tab);

    const panel = document.createElement("section");
    panel.id = "team-panel";
    // Scrolls inside the shell like the queue does, so the sidebar runs the full height beside it.
    panel.className = "team-panel scroll-pane hidden";
    panel.innerHTML = `
      <div class="team-head">
        <h2>Who is working on what</h2>
        <a class="btn btn-secondary btn-small" href="/api/team/roster.csv">Export CSV</a>
      </div>
      <section class="team-presence" aria-labelledby="team-presence-title">
        <div class="team-presence-head">
          <button type="button" id="team-presence-title" class="team-presence-toggle"
                  aria-expanded="false" aria-controls="team-presence-list">
            <span class="presence-chevron" aria-hidden="true">&#8250;</span>
            <span>Active now</span>
          </button>
          <span id="team-presence-count" class="team-presence-count"></span>
        </div>
        <div id="team-presence-list" class="team-presence-list" hidden>
          <p class="presence-empty">Checking who is here&hellip;</p>
        </div>
      </section>
      <form id="assign-form" class="assign-card">
        <h3 class="assign-card-title">Assign a folder</h3>
        <div class="assign-fields">
          <label class="assign-field assign-field-folder"><span>Folder</span>
            <select id="assign-folder" required title="Folders that actually exist"></select></label>
          <label class="assign-field"><span>${esc(term("reviewer"))}</span>
            <select id="assign-reviewer" required></select></label>
          <label class="assign-field"><span>Note <em>optional</em></span>
            <input id="assign-note" placeholder="Where to start, a deadline&hellip;" /></label>
          <button class="btn btn-accept" type="submit">Assign</button>
        </div>
      </form>
      <section class="team-card" aria-labelledby="team-assignments-title">
        <header class="team-card-head">
          <h3 id="team-assignments-title">Assignments</h3>
          <span class="team-card-hint">Click a name for that person&rsquo;s statistics</span>
        </header>
        <div class="team-table-scroll">
          <table class="roster"><thead><tr>
            <th>${esc(term("reviewer"))}</th><th>Folder</th><th>Status</th><th>Assigned by</th>
            <th>Last activity</th><th class="roster-actions-head" aria-label="Actions"></th>
          </tr></thead><tbody id="roster-body"></tbody></table>
        </div>
      </section>
      <section class="team-card" aria-labelledby="team-claims-title">
        <header class="team-card-head">
          <h3 id="team-claims-title">Claims</h3>
          <span class="team-card-hint">Which ${esc(term("admin", { plural: true, lower: true }))} are working on what</span>
        </header>
        <div class="team-table-scroll">
          <table class="roster claims"><thead><tr>
            <th>${esc(term("admin"))}</th><th>Folder</th><th>Claimed</th>
          </tr></thead><tbody id="claims-body"></tbody></table>
        </div>
      </section>
      <div id="people-cards"></div>`;
    queueView.parentElement.insertBefore(panel, queueView.nextSibling);

    // The sidebar belongs to the document queue: filters that count documents, and pipeline
    // actions that run over them.
    const QUEUE_ONLY_SIDEBAR = ["folder-search-wrap", "filter-nav-section", "folder-filter-section",
                                "pipeline-section", "sidebar-footer"];

    function showTeam(on) {
      panel.classList.toggle("hidden", !on);
      // Accounts change from other screens and other people, so the cards are read when shown.
      if (on && window.PeopleCards) {
        window.PeopleCards.mount($("people-cards"), me).catch((error) => {
          $("people-cards").textContent = `Could not load the people on this site: ${error.message}`;
        });
      }
      QUEUE_ONLY_SIDEBAR.forEach((id) => {
        const section = $(id);
        if (section) section.style.display = on ? "none" : "";
      });
      if (!on) tidyPipelineSection();   // "" hands it back to the stylesheet; re-check it is wanted
      // Inline style rather than the `hidden` class: app.js re-renders the queue on its own poll
      // and rewrites classList as it goes, which quietly un-hid it a second after the tab was
      // clicked.
      queueView.style.display = on ? "none" : "";
      if ($("detail-view")) $("detail-view").style.display = on ? "none" : "";
      // The document's "Back" bar belongs to that document, so it goes with it rather than staying
      // over Team; "" hands it back to app.js, which decides from the view that is then showing.
      if ($("nav-controls")) $("nav-controls").style.display = on ? "none" : "";
      // Resolve the live node: the navigation owner is free to rebuild its strip.
      $("tab-team")?.classList.toggle("active", on);
      // The pipeline's own tabs go back to looking unselected while Team is up, and are restored
      // by their own handler when one of them is clicked.
      if (on) nav.querySelectorAll(".workflow-tab[data-workflow]").forEach((t) => t.classList.remove("active"));
      clearInterval(presenceTimer);
      presenceTimer = null;
      if (on) {
        sendPresence();
        loadRoster();
        loadPresence();
        presenceTimer = setInterval(loadPresence, 15000);
        window.__route__?.note({ area: "team" });
      }
    }

    // Back to Team goes through the tab itself, which already hands every other screen back.
    window.__route__?.owns("team", {
      restore: async () => {
        const tab = $("tab-team");
        if (!tab) throw new Error("no Team tab for this account");
        tab.click();
      },
      leave: async () => true,
    });

    // Delegated because app.js redraws this tab strip as workflows change; a listener attached to
    // the original Team button would disappear with that node even though its replacement looks
    // identical.
    document.addEventListener("click", (e) => {
      if (e.target.closest("#tab-team")) showTeam(true);
      else if (e.target.closest(".workflow-tab[data-workflow]")) showTeam(false);
    });
    $("team-presence-title").addEventListener("click", togglePresencePanel);
    setPresenceExpanded(readPresencePreference());
    $("assign-form").addEventListener("submit", assign);
  }

  let presenceTimer = null;
  let presenceLoading = false;

  function readPresencePreference() {
    try { return localStorage.getItem("incipit-presence-expanded") === "true"; }
    catch { return false; }
  }

  function setPresenceExpanded(expanded) {
    const toggle = $("team-presence-title");
    const list = $("team-presence-list");
    if (!toggle || !list) return;
    toggle.setAttribute("aria-expanded", String(expanded));
    list.hidden = !expanded;
    toggle.closest(".team-presence")?.classList.toggle("is-expanded", expanded);
  }

  function togglePresencePanel() {
    const expanded = $("team-presence-title")?.getAttribute("aria-expanded") !== "true";
    setPresenceExpanded(expanded);
    try { localStorage.setItem("incipit-presence-expanded", String(expanded)); }
    catch { /* A blocked preference store must not make the panel unusable. */ }
  }

  function relativePresence(seconds) {
    if (seconds < 60) return "Active now";
    const minutes = Math.max(1, Math.floor(seconds / 60));
    return `Active ${minutes}m ago`;
  }

  async function loadPresence() {
    if ($("team-panel")?.classList.contains("hidden") || presenceLoading) return;
    presenceLoading = true;
    let people;
    try {
      ({ people } = await getJSON("/api/team/presence"));
    } catch {
      return;
    } finally {
      presenceLoading = false;
    }
    people = people || [];
    const active = people.filter((person) => person.active).length;
    $("team-presence-count").textContent = active ? `${active} online` : "No one online";
    $("team-presence-list").innerHTML = people.length ? people.map((person) => {
      const isMe = person.user_id === me.id;
      const work = person.document_id
        ? `Working on <code>${esc(person.document_id)}</code><span class="presence-workflow">${esc(WORKFLOW_NAMES[person.workflow] || person.workflow)}</span>`
        : "Browsing the site";
      return `<article class="presence-person${person.active ? " is-active" : ""}">
        <span class="presence-dot" aria-hidden="true"></span>
        <div class="presence-person-main">
          <div><strong>${esc(plainName(person.name))}</strong>${isMe ? `<span class="presence-you">You</span>` : ""}
            <span class="presence-role">${esc(term(person.role))}</span></div>
          <div class="presence-work">${work}</div>
        </div>
        <span class="presence-when">${esc(relativePresence(person.seconds_ago || 0))}</span>
      </article>`;
    }).join("") : `<p class="presence-empty">No one has been active in the last 10 minutes.</p>`;
  }

  // Statistics for one person, opened from their name in Assignments.
  let contributorStats = new Map();

  function showReviewerStats(name) {
    // One panel at a time: clicking a name twice stacked a second copy on the first, and closing
    // the top one left the page looking unresponsive.
    document.getElementById("reviewer-stats")?.remove();
    const c = contributorStats.get(name);
    const panel = document.createElement("div");
    panel.id = "reviewer-stats";
    panel.className = "modal-overlay";
    const stat = (label, value, hint) => `
      <div class="reviewer-stat">
        <div class="reviewer-stat-value">${esc(value)}</div>
        <div class="reviewer-stat-label">${esc(label)}</div>
        ${hint ? `<div class="reviewer-stat-hint">${esc(hint)}</div>` : ""}
      </div>`;

    // "How many" is the first question and "which ones" is the second, so the two counts that
    // stand for a list of folders open into that list rather than sending the reader off to the
    // roster to work it out.
    const folderStat = (label, folders, key) => {
      if (!folders || !folders.length) return stat(label, folders ? folders.length : 0);
      return `
        <div class="reviewer-stat reviewer-stat-open" data-folders="${esc(key)}">
          <button type="button" class="reviewer-stat-toggle" aria-expanded="false">
            <span class="reviewer-stat-value">${folders.length}</span>
            <span class="reviewer-stat-label">${esc(label)}</span>
            <span class="reviewer-stat-more">Show</span>
          </button>
          <ul class="reviewer-stat-list" hidden>
            ${folders.map((f) => `
              <li><code>${esc(f.folder_display_id || f.folder_id)}</code>
                <span class="wf">${esc(f.workflow)}</span>
                ${f.status ? `<span class="reviewer-stat-state">${esc(STATUS[f.status]?.label || f.status)}</span>` : ""}
              </li>`).join("")}
          </ul>
        </div>`;
    };
    const body = !c ? `<p class="analytics-caveat">No recorded activity for this person yet.</p>` : `
      <div class="reviewer-stat-grid">
        ${folderStat("Folders held", c.held, "held")}
        ${folderStat("Folders approved", c.approved, "approved")}
        ${stat("Documents accepted", c.documents_accepted)}
        ${stat("Submissions", c.submissions)}
        ${stat("Sent back", c.returns, c.returns ? "work that came back for changes" : "")}
        ${stat("Avg turnaround", c.avg_turnaround_hours == null ? "—" : c.avg_turnaround_hours + "h",
               "from being given a folder to first submitting it")}
        ${stat("Last active", (c.last_active || "").slice(0, 10) || "never",
               "any edit or acceptance, not only submitting")}
      </div>`;
    panel.innerHTML = `
      <div class="modal analytics-modal">
        <div class="analytics-head">
          <div>
            <h3>Statistics</h3>
            <p class="analytics-folder">${esc(name)}</p>
          </div>
          <button class="analytics-close" id="reviewer-stats-close" aria-label="Close">&times;</button>
        </div>
        <div>${body}</div>
      </div>`;
    document.body.appendChild(panel);
    panel.querySelectorAll(".reviewer-stat-toggle").forEach((toggle) => {
      toggle.addEventListener("click", () => {
        const list = toggle.nextElementSibling;
        const open = list.hidden;
        list.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.querySelector(".reviewer-stat-more").textContent = open ? "Hide" : "Show";
      });
    });
    const close = () => panel.remove();
    $("reviewer-stats-close").addEventListener("click", close);
    panel.addEventListener("click", (e) => { if (e.target === panel) close(); });
    document.addEventListener("keydown", function esc_(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc_); }
    });
  }

  async function loadRoster() {
    const [{ roster, reviewers }, { folders }, { contributors }, { claims }] = await Promise.all([
      getJSON("/api/team/roster"),
      getJSON("/api/team/folders"),
      getJSON("/api/team/stats"),
      getJSON("/api/team/claims"),
    ]);

    // A folder named as its card names it, so a row reads the same as the queue.
    const labels = new Map(folders.map((f) => [`${f.workflow}:${f.folder_id}`, f]));
    const folderCell = (folderId, workflow) => {
      const folder = labels.get(`${workflow}:${folderId}`) || {};
      const shown = folder.folder_display_id || folderId;
      const title = folder.label;
      const named = title && title !== shown;
      return `<div class="folder-cell">
        ${named ? `<span class="folder-cell-title" title="${esc(title)}">${esc(title)}</span>` : ""}
        <span class="folder-cell-meta"><code>${esc(shown)}</code>
          <span class="wf">${esc(WORKFLOW_NAMES[workflow] || workflow)}</span></span>
      </div>`;
    };

    // Who among the editors is working on what.
    const claimRows = Object.entries(claims);
    $("claims-body").innerHTML = claimRows.length ? claimRows.map(([folder, c]) => `
      <tr>
        <td class="roster-person">${esc(plainName(c.by))}</td>
        <td>${folderCell(folder, c.workflow)}</td>
        <td class="roster-when">${esc(shortWhen(c.claimed_at))}</td>
      </tr>`).join("")
      : `<tr><td colspan="3" class="roster-empty">No folders claimed. Claiming is optional &mdash;
           it tells other editors what you are working on, and stops nobody from opening it.</td></tr>`;

    // Kept for the per-reviewer panel rather than drawn as a table of its own.
    contributorStats = new Map(contributors.map((c) => [c.name, c]));
    $("assign-reviewer").innerHTML = `<option value="" disabled selected>Choose a ${esc(term("reviewer", { lower: true }))}&hellip;</option>` +
      reviewers.map((s) => `<option value="${s.id}">${esc(plainName(s.display_name))}</option>`).join("");

    // One group per tab, unassigned first -- but assigned folders stay selectable, because
    // reassigning is normal (someone is away, or the work moved).
    const option = (f) =>
      `<option value="${esc(f.folder_id)}" data-workflow="${esc(f.workflow)}">` +
      `${esc(f.label)}${f.documents ? ` \u00b7 ${f.documents} doc${f.documents === 1 ? "" : "s"}` : ""}` +
      `${f.assigned_to_name ? ` \u2014 with ${esc(plainName(f.assigned_to_name))}` : ""}</option>`;
    const groups = [...new Set(folders.map(tabLabel))].map((label) => {
      const mine = folders.filter((f) => tabLabel(f) === label);
      const ordered = [...mine.filter((f) => f.assignment_status === "unassigned"),
                       ...mine.filter((f) => f.assignment_status !== "unassigned")];
      return ordered.length ? `<optgroup label="${esc(label)}">${ordered.map(option).join("")}</optgroup>` : "";
    }).join("");
    $("assign-folder").innerHTML = folders.length
      ? `<option value="" disabled selected>Choose a folder&hellip;</option>${groups}`
      : `<option value="">No folders yet \u2014 upload some scans first</option>`;
    $("roster-body").innerHTML = roster.length ? roster.map((r) => {
      const s = STATUS[r.status] || STATUS.assigned;
      return `<tr>
        <td class="roster-person"><button type="button" class="reviewer-stats-link" data-reviewer="${esc(r.reviewer)}"
                    title="Statistics for ${esc(plainName(r.reviewer))}">${esc(plainName(r.reviewer))}</button></td>
        <td>${folderCell(r.folder_id, r.workflow)}</td>
        <td><span class="st ${s.cls}">${s.label}</span></td>
        <td class="roster-person">${esc(plainName(r.assigned_by))}</td>
        <td class="roster-when">${esc(shortWhen(r.last_activity))}</td>
        <td class="roster-actions">${r.status === "submitted" ? `
          <button class="btn btn-lifecycle btn-sendback" data-return="${esc(r.folder_id)}"
                  data-workflow="${esc(r.workflow)}"
                  data-reviewer="${esc(r.reviewer)}">Send back</button>` : ""}
          <button class="btn btn-lifecycle btn-unassign" data-unassign="${esc(r.folder_id)}"
                  data-shown="${esc(r.folder_display_id || r.folder_id)}"
                  data-workflow="${esc(r.workflow)}"
                  data-reviewer="${esc(r.reviewer)}">Unassign</button>
        </td></tr>`;
    }).join("") : `<tr><td colspan="6" class="roster-empty">Nothing assigned yet.</td></tr>`;
  }

  async function assign(e) {
    e.preventDefault();
    const res = await fetch("/api/team/assign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder_id: $("assign-folder").value,
        workflow: $("assign-folder").selectedOptions[0]?.dataset.workflow || "new",
        reviewer_id: Number($("assign-reviewer").value), note: $("assign-note").value.trim(),
      }),
    });
    if (!res.ok) { alert((await res.json()).detail || "Could not assign"); return; }
    $("assign-folder").value = ""; $("assign-note").value = "";
    loadRoster();
  }

  document.addEventListener("click", (e) => {
    // `.reviewer-stats-link`, not `[data-reviewer]`: the Send back buttons carry the reviewer's name
    // too, so the broader selector opened their statistics every time a folder was returned.
    const link = e.target.closest(".reviewer-stats-link");
    if (link) showReviewerStats(link.dataset.reviewer);
  });

  // Sending a folder back: which documents need more work, and why.
  function pageThumb(base, documentId) {
    return `${base}/documents/${encodeURIComponent(documentId)}/pages/0/image?size=thumb`;
  }

  function showPage(src) {
    const viewer = document.createElement("div");
    viewer.className = "modal-overlay rework-viewer";
    viewer.innerHTML = `<img src="${esc(src.replace("size=thumb", "size=view"))}" alt="" />`;
    const close = () => {
      viewer.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    viewer.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(viewer);
  }

  async function askWhatNeedsWork(folderId, workflow, reviewerName) {
    let documents = [];
    try {
      const all = await getJSON("/api/new/documents");
      documents = all.filter((d) => d.folder_id === folderId);
    } catch { /* the picker is a convenience; a note alone still returns the folder */ }

    return new Promise((resolve) => {
      // One picker at a time.
      document.querySelector(".rework-overlay")?.remove();
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay rework-overlay";
      overlay.innerHTML = `
        <div class="modal rework-modal">
          <h3>Send back to ${esc(reviewerName || `the ${term("reviewer", { lower: true })}`)}</h3>
          <p class="rework-hint">Tick anything that needs more work &mdash; they will see those
            documents marked <strong>Needs work</strong> in their queue.</p>
          <div class="rework-list">${documents.map((d) => `
            <label class="rework-item">
              <input type="checkbox" value="${esc(d.document_id)}" />
              <img class="rework-thumb" loading="lazy" alt=""
                   src="${esc(d.thumbnail || pageThumb(base, d.document_id))}"
                   data-enlarge="${esc(d.thumbnail || pageThumb(base, d.document_id))}"
                   title="Click to enlarge" />
              <span class="rework-lines">
                <span class="rework-id">${esc(d.display_id || d.document_id)}</span>
                <span class="rework-title">${esc(d.title || "")}</span>
              </span>
            </label>`).join("") || `<p class="rework-empty">No documents in this folder yet.</p>`}
          </div>
          <label class="rework-note-label" for="rework-note">Reason for sending back
            <span class="rework-required">required</span></label>
          <textarea id="rework-note" class="rework-note" rows="3" aria-describedby="rework-note-error"
                    placeholder="What needs changing? (the ${esc(term("reviewer", { lower: true }))} sees this)"></textarea>
          <p id="rework-note-error" class="rework-note-error" hidden>You must enter a reason to send
            this back to the reviewer.</p>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" data-rework="cancel">Cancel</button>
            <button type="button" class="btn btn-send-back" data-rework="send">Send back</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (value) => { overlay.remove(); resolve(value); };
      overlay.addEventListener("click", (e) => {
        // Enlarging a page must not also tick the row it sits in.
        const thumb = e.target.closest("[data-enlarge]");
        if (thumb) {
          e.preventDefault();
          return showPage(thumb.dataset.enlarge);
        }
        if (e.target === overlay || e.target.closest('[data-rework="cancel"]')) return close(null);
        if (!e.target.closest('[data-rework="send"]')) return;
        const box = overlay.querySelector("#rework-note");
        const note = box.value.trim();
        const picked = [...overlay.querySelectorAll("input:checked")].map((i) => i.value);
        if (!note) {
          box.classList.add("is-invalid");
          box.setAttribute("aria-invalid", "true");
          overlay.querySelector("#rework-note-error").hidden = false;
          box.focus();
          return;
        }
        close({ note, rework_documents: picked });
      });
      const box = overlay.querySelector("#rework-note");
      // Typing a reason clears the warning at once.
      box.addEventListener("input", () => {
        if (!box.value.trim()) return;
        box.classList.remove("is-invalid");
        box.removeAttribute("aria-invalid");
        overlay.querySelector("#rework-note-error").hidden = true;
      });
      box.focus();
    });
  }

  // Takes a folder back from the reviewer holding it; their own verdicts are kept.
  document.addEventListener("click", async (e) => {
    const drop = e.target.closest("[data-unassign]");
    if (!drop) return;
    const folder_id = drop.dataset.unassign;
    const reviewer = drop.dataset.reviewer || `that ${term("reviewer", { lower: true })}`;
    if (!window.confirm(`Unassign ${drop.dataset.shown || folder_id} from ${reviewer}?

`
        + "They keep their edits, decisions, and comments, but the folder leaves their queue "
        + "until it is assigned again.")) return;
    const res = await fetch("/api/team/unassign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id, workflow: drop.dataset.workflow || "new" }),
    });
    if (!res.ok) {
      alert((await res.json().catch(() => ({}))).detail || "Could not unassign that folder");
      return;
    }
    window.loadQueue?.();
    loadRoster();
  });

  // Approving is no longer a button: a folder approves itself once its last document is accepted
  // (collab.settle_approval), so the only verdict left to press is sending it back.
  document.addEventListener("click", async (e) => {
    const back = e.target.closest("[data-return]");
    if (!back) return;
    const folder_id = back.dataset.return;
    const workflow = back.dataset.workflow || "new";

    const answer = await askWhatNeedsWork(folder_id, workflow, back.dataset.reviewer);
    if (!answer) return;                         // cancelled
    const { note, rework_documents } = answer;

    const res = await fetch("/api/team/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id, workflow, status: "returned", note, rework_documents }),
    });
    if (!res.ok) {
      alert((await res.json()).detail || "Could not update that folder");
      return;
    }
    // Both, and the queue first.
    window.loadQueue?.();
    loadRoster();
  });

  // The queue redraws the Team tab after it unassigns a folder.
  window.__refreshTeam__ = loadRoster;
  // app.js owns saving; this is how a save tells the history panel to catch up.
  window.__refreshHistory__ = refreshHistory;
  window.__docThreads__ = { comments: commentListHTML, history: historyHTML };
  window.__workflowNotes__ = { submit: askSubmissionNote, return: askWhatNeedsWork };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
