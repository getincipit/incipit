(() => {
  "use strict";

  const state = {
    activeWorkflow: "new",      // the document kind the API routes name; the tab is activeTab
    tabs: [],                   // what /api/tabs gave this reader, in strip order
    activeTab: null,            // the tab whose folders are showing
    documents: [],       // queue list (unfiltered) -- reused for whichever workflow is active
    folders: [],         // New Documents workflow: folder summaries from /api/new/folders
    activeFolder: null,  // New Documents workflow: which folder is drilled into (null = folder grid)
    filter: "all",
    folderFilter: "all",  // folder grid: shared workflow stage (see FOLDER_FILTERS)
    folderSearch: "",     // folder grid: the typed term, lower-cased, "" when not searching
    // The batch this browser is still setting up -- uploaded, but its reviewer has not finished
    // with it yet.
    batchInFlight: null,
    renamingFolder: false,  // the rename row is open
    renameFolder: null,     // which folder it was opened for -- leaving that folder closes it
    savingFolderName: false,
    currentIndex: -1,    // index into filtered queue
    currentDoc: null,    // full detail payload for the open document
    currentPageIndex: 0,
    dirty: false,
    zoom: { scale: 1, x: 0, y: 0 },
    // How the open page is turned on screen; the page's own rotation and mirrored are what was saved.
    orientation: null,
    dragging: false,
    dragStart: { x: 0, y: 0, panX: 0, panY: 0 },
    // "Fix Split" selection mode inside a New Documents folder -- picks which already-drafted
    // documents get their boundaries re-cut (see renderResplitBar).
    resplitMode: false,
    resplitFolder: null,  // which folder Fix Split was started in -- it ends when you leave
    resplitSelection: new Set(),
    // Draft requests are scoped by folder. Keeping their keys here lets this browser start a
    // second folder while the first is still running, while preventing a duplicate start of the
    // same folder and keeping its controls disabled when the reviewer navigates back to it.
    folderDraftRuns: new Set(),
    savePromise: null,
  };

  function apiBase() {
    return "/api/new";
  }

  const MIN_SCALE = 1;
  const MAX_SCALE = 8;
  const ZOOM_STEP = 1.15;

  const el = (id) => document.getElementById(id);

  const queueView = el("queue-view");
  const detailView = el("detail-view");
  const splitReviewView = el("split-review-view");
  const navControls = el("nav-controls");
  const queueList = el("queue-list");
  const queueEmpty = el("queue-empty");
  const filterNav = el("filter-nav");
  const filterNavSection = el("filter-nav-section");
  const folderFilterSection = el("folder-filter-section");
  const folderFilterNav = el("folder-filter-nav");
  const folderSearchWrap = el("folder-search-wrap");
  const folderSearch = el("folder-search");
  const queueSubtitle = el("queue-subtitle");
  const pipelineStatus = el("pipeline-status");
  const pendingBatches = el("pending-batches");
  const pendingBatchesList = el("pending-batches-list");
  const folderGrid = el("folder-grid");
  const folderHeader = el("folder-header");
  const folderHeaderName = el("folder-header-name");
  const folderDraftingNote = el("folder-drafting-note");
  const folderDraftingNoteText = el("folder-drafting-note-text");
  const folderDraftCount = el("folder-draft-count");
  const folderDraftCountBtn = el("folder-draft-count-btn");
  const folderDraftAllBtn = el("folder-draft-all-btn");
  const folderBack = el("folder-back");
  const folderExport = el("folder-export");
  const folderDelete = el("folder-delete");
  const confirmDeleteModal = el("confirm-delete-modal");
  const confirmDeleteTitle = el("confirm-delete-title");
  const confirmDeleteMessage = el("confirm-delete-message");
  const confirmDeleteCancel = el("confirm-delete-cancel");
  const confirmDeleteConfirm = el("confirm-delete-confirm");
  const confirmDeleteReason = el("confirm-delete-reason");
  const confirmDeleteError = el("confirm-delete-error");
  const btnDeleteDocument = el("btn-delete-document");

  const workflowTabs = el("workflow-tabs");
  // The tab in use, for every request that lists or stores folders.
  const tabQuery = (lead = "?") => (state.activeTab ? `${lead}tab_id=${state.activeTab.id}` : "");
  const workflowPill = el("workflow-pill");
  const newPipelineActions = el("new-pipeline-actions");
  const btnUploadScans = el("btn-upload-scans");
  const uploadScansInput = el("upload-scans-input");
  const btnDraftPending = el("btn-draft-pending");

  const viewer = el("viewer");
  const pageImage = el("page-image");
  const pageIndicator = el("page-indicator");

  const metadataGrid = el("metadata-grid");
  const textPreview = el("text-preview");
  const textEdit = el("text-edit");
  const fieldBlocks = el("field-blocks");
  const reviewNotes = el("review-notes");
  const btnRedraft = el("btn-redraft");
  const reviewedByLine = el("reviewed-by-line");
  const reviewedByText = el("reviewed-by-text");
  const statusBadge = el("status-badge");
  const docTitle = el("doc-title");
  const docId = el("doc-id");
  const btnEditDocid = el("btn-edit-docid");
  const docIdInput = el("doc-id-input");
  const btnSaveDocid = el("btn-save-docid");
  const btnCancelDocid = el("btn-cancel-docid");
  const saveToast = el("save-toast");

  // ---------- helpers ----------

  function escapeHtml(str) {
    return (str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // The client's scans are 5142x6656.
  function pageImageUrl(url, size) {
    if (!url) return url;
    return url + (url.includes("?") ? "&" : "?") + "size=" + size;
  }

  // A page boundary as both prompts are told to write it.
  const PAGE_MARKER = /^\[page\s+(\d+)\]$/i;

  // A body paragraph gets the first-line indent the period's own letters use; a dateline,
  // salutation, address block or closing does not.
  const INDENT_MIN_WORDS = 10;
  // Anything holding a letter or a digit.
  const WORD = /[^\s]*[\p{L}\p{N}][^\s]*/gu;
  // A dateline can reach ten words once it carries a place ("Winsted, Connecticut, Thursday, the
  // twenty-fourth of February, 1920"), and it sits flush on the page like any other label.
  const DATELINE = /\b(?:1[6-9]|20)\d{2}\.?$/;
  // The tags both prompts are told to emit -- [Letterhead], [Marginal note], [Copies to notation].
  const STRUCTURAL_TAG = /^\s*\[/;
  // A motto or slogan printed on a letterhead ("Help the Blind to Help Themselves, by Giving Them
  // Work").
  const FULLY_QUOTED = /^["'“‘][^]*["'”’]$/;

  // ...but only where the document does not indent itself.
  const ALREADY_INDENTED = /^[ \t]/;

  function markUnclear(text) {
    return escapeHtml(text).replace(/\[unclear:[^\]]*\]/gi, (m) => `<mark>${m}</mark>`);
  }

  // A line long enough to be a whole paragraph on its own, rather than one line of a wrapped one.
  const PARAGRAPH_MIN_WORDS = 25;

  function isWholeParagraph(line) {
    return (line.match(WORD) || []).length >= PARAGRAPH_MIN_WORDS;
  }

  /* Blank-line-separated blocks, with page markers and whole paragraphs pulled out as their own. */
  function transcriptionBlocks(text) {
    const blocks = [];
    let run = [];
    const flush = () => {
      const body = run.join("\n").replace(/\s+$/, "");
      if (body.trim()) blocks.push({ kind: "para", text: body });
      run = [];
    };
    for (const chunk of (text || "").split(/\n[ \t]*\n+/)) {
      for (const line of chunk.split("\n")) {
        const marker = line.trim().match(PAGE_MARKER);
        // Pulled out even when the model ran it together with the surrounding paragraph: it is a
        // boundary wherever it appears, and inline it is just stray text.
        if (marker) {
          flush();
          blocks.push({ kind: "page", number: marker[1] });
        } else if (isWholeParagraph(line)) {
          flush();          // whatever preceded it -- a salutation, a running head -- ends here
          run.push(line);
          flush();
        } else {
          run.push(line);
        }
      }
      flush();
    }
    return blocks;
  }

  /* True only for a prose paragraph, which is the only kind of block that takes an indent. */
  function isBodyParagraph(text) {
    if (text.includes("\n")) return false;          // the writer's own hard wrapping: an address
    if (ALREADY_INDENTED.test(text)) return false;  // the document indents itself; see above
    const line = text.trim();
    if (DATELINE.test(line) || STRUCTURAL_TAG.test(line) || FULLY_QUOTED.test(line)) return false;
    return (line.match(WORD) || []).length >= INDENT_MIN_WORDS;
  }

  function renderHighlighted(text) {
    const blocks = transcriptionBlocks(text);
    if (!blocks.length) return "<em>(empty)</em>";
    return blocks.map((block) => {
      if (block.kind === "page") {
        return `<div class="tp-page-break"><span>Page ${escapeHtml(block.number)}</span></div>`;
      }
      const indent = isBodyParagraph(block.text);
      return `<p class="tp-para${indent ? " tp-indent" : ""}">${markUnclear(block.text)}</p>`;
    }).join("");
  }

  function badgeClass(status) {
    // A reviewer's accepted is "Reviewed", and wears the reviewed colour rather than the green a
    // reviewer's own accept uses (see badgeLabel).
    const accepted = document.body.classList.contains("role-reviewer")
      ? "badge-accepted" : "badge-reviewed";
    // Anything else, including the retired `flagged`, reads as unreviewed.
    return { unreviewed: "badge-unreviewed", accepted, needs_work: "badge-sent-back" }[status]
      || "badge-unreviewed";
  }

  function badgeLabel(status) {
    // A reviewer's "accepted" is a review; a reviewer's is an acceptance.
    const accepted = document.body.classList.contains("role-reviewer") ? "Accepted" : "Reviewed";
    // needs_work is the mark a send-back leaves, shown under the name the site has always used.
    return { unreviewed: "Unreviewed", accepted, needs_work: "Needs work" }[status] || "Unreviewed";
  }

  // isError gets a longer, distinctly-colored toast -- a real problem (e.g. an OpenRouter failure)
  // needs more than the default 2.2s a routine "Saved" confirmation gets away with, and #save-
  // toast is fixed-position (unlike #pipeline-status/#split-confirm-status), so it's the one
  // status surface that's visible regardless of which view (queue/detail/split-review) is
  // currently shown.
  // A button's label fades out over SWAP_OUT_MS before the new one lands (see swapButtonLabel).
  const SWAP_OUT_MS = 170;

  // A confirmation arrives as the button's new label lands, and moves at the same pace.
  function toast(msg, isError) {
    clearTimeout(toast._in);
    clearTimeout(toast._t);
    toast._in = setTimeout(() => {
      saveToast.textContent = msg;
      saveToast.classList.toggle("toast-error", !!isError);
      saveToast.classList.add("is-shown");
    }, SWAP_OUT_MS);
    toast._t = setTimeout(() => saveToast.classList.remove("is-shown"),
                          SWAP_OUT_MS + (isError ? 8000 : 2200));
  }

  // 409 from a costed endpoint means another reviewer's job holds the lock -- ordinary use on a
  // shared site, not a failure.
  async function handledBusy(res) {
    if (res.status !== 409) return false;
    const body = await res.json().catch(() => ({}));
    toast(body.detail || "Something else is running. Wait for it to finish, then try again.", true);
    return true;
  }

  function markDirty() {
    state.dirty = true;
    // Counts edits, not saves. A save clears `dirty` only if this has not moved since it built
    // its payload -- otherwise words typed while the request was in flight would be marked saved
    // when what reached the server was the text from before them. See saveReview.
    state.editGeneration = (state.editGeneration || 0) + 1;
  }

  // ---------- "unsaved changes" modal ----------

  const leaveModal = el("leave-modal");
  let leaveModalResolve = null;

  function showLeaveModal() {
    return new Promise((resolve) => {
      leaveModalResolve = resolve;
      leaveModal.classList.remove("hidden");
    });
  }

  function resolveLeaveModal(choice) {
    leaveModal.classList.add("hidden");
    const resolve = leaveModalResolve;
    leaveModalResolve = null;
    if (resolve) resolve(choice);
  }

  el("modal-save-leave").addEventListener("click", () => resolveLeaveModal("save"));
  el("modal-cancel").addEventListener("click", () => resolveLeaveModal("cancel"));
  el("modal-discard-leave").addEventListener("click", () => resolveLeaveModal("discard"));

  // ---------- generic delete-confirmation modal (documents, folders) ----------

  let confirmDeleteResolve = null;

  // Resolves to null (cancelled) or {reason}. The server gets the actor from the signed-in session.
  const MIN_REASON_LENGTH = 4;

  function confirmDelete(title, message) {
    confirmDeleteTitle.textContent = title;
    confirmDeleteMessage.textContent = message;
    confirmDeleteReason.value = "";
    confirmDeleteError.classList.add("hidden");
    confirmDeleteModal.classList.remove("hidden");
    confirmDeleteReason.focus();
    return new Promise((resolve) => { confirmDeleteResolve = resolve; });
  }

  function submitConfirmDelete() {
    const reason = confirmDeleteReason.value.trim();
    if (reason.length < MIN_REASON_LENGTH) {
      return showDeleteError(`Give a reason of at least ${MIN_REASON_LENGTH} characters.`);
    }
    closeConfirmDelete({ reason });
  }

  function showDeleteError(text) {
    confirmDeleteError.textContent = text;
    confirmDeleteError.classList.remove("hidden");
  }

  function closeConfirmDelete(result) {
    confirmDeleteModal.classList.add("hidden");
    const resolve = confirmDeleteResolve;
    confirmDeleteResolve = null;
    if (resolve) resolve(result);
  }

  // Query string every delete endpoint requires.
  function deletionParams(ctx) {
    return `reason=${encodeURIComponent(ctx.reason)}`;
  }

  // ---------- deletion log (Settings > Deletion Log) ----------

  const DELETION_KIND_LABELS = {
    document: "Document",
    new_document: "New document",
    new_folder: "Folder",
    pending_batch: "Pending batch",
  };

  function formatDeletedAt(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  async function loadDeletionLog() {
    const list = el("deletion-log-list");
    list.innerHTML = `<p class="deletion-log-empty">Loading…</p>`;
    let entries;
    try {
      const res = await fetch("/api/deletion-log");
      if (!res.ok) throw new Error(res.statusText);
      entries = await res.json();
    } catch (err) {
      list.innerHTML = `<p class="deletion-log-empty">Couldn't load the deletion log (${escapeHtml(String(err.message || err))}).</p>`;
      return;
    }
    if (!entries.length) {
      list.innerHTML = `<p class="deletion-log-empty">Nothing has been deleted yet.</p>`;
      return;
    }
    list.innerHTML = entries.map((e) => `
      <div class="deletion-entry">
        <div class="deletion-entry-head">
          <span class="deletion-kind">${escapeHtml(DELETION_KIND_LABELS[e.target_kind] || e.target_kind)}</span>
          <span class="deletion-target">${escapeHtml(e.summary || e.target_id)}</span>
        </div>
        <div class="deletion-entry-meta">
          <strong>${escapeHtml(e.reviewer)}</strong> &middot; ${escapeHtml(formatDeletedAt(e.deleted_at))}
          &middot; <span class="deletion-workflow">${e.workflow === "new" ? "Documents" : escapeHtml(e.workflow)}</span>
        </div>
        <div class="deletion-entry-reason">${escapeHtml(e.reason)}</div>
      </div>
    `).join("");
  }

  confirmDeleteCancel.addEventListener("click", () => closeConfirmDelete(null));
  confirmDeleteConfirm.addEventListener("click", submitConfirmDelete);
  confirmDeleteModal.addEventListener("click", (e) => {
    if (e.target === confirmDeleteModal) closeConfirmDelete(null);
  });
  // The reviewer field went when the actor started coming from the session; the reason remains.
  confirmDeleteReason.addEventListener(
    "keydown", (e) => { if (e.key === "Enter") submitConfirmDelete(); });

  // ---------- image preview (click a split-review thumbnail to see the page full-size) ----------

  const imagePreviewModal = el("image-preview-modal");
  const imagePreviewImg = el("image-preview-img");

  function openImagePreview(url) {
    imagePreviewImg.src = url;
    imagePreviewModal.classList.remove("hidden");
  }

  imagePreviewModal.addEventListener("click", () => imagePreviewModal.classList.add("hidden"));

  // ---------- drafting analytics (the Analytics link on a folder card) ---------- What one folder
  // cost, how long it took, and how fast that is.

  const analyticsModal = el("analytics-modal");
  const analyticsBody = el("analytics-body");
  const analyticsFolder = el("analytics-folder");

  function closeAnalytics() {
    analyticsModal.classList.add("hidden");
  }

  el("analytics-close").addEventListener("click", closeAnalytics);
  // Click the scrim to dismiss, but not a click that started inside the dialog -- selecting a
  // number and releasing outside the panel would otherwise close it mid-drag.
  analyticsModal.addEventListener("mousedown", (e) => {
    if (e.target === analyticsModal) closeAnalytics();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !analyticsModal.classList.contains("hidden")) closeAnalytics();
  });

  function money(value) {
    const spent = Number(value) || 0;
    // Four decimals under a cent: drafting one short document genuinely costs less than that, and
    // $0.00 would report real spend as none.
    return spent >= 0.01 ? `$${spent.toFixed(2)}` : `$${spent.toFixed(4)}`;
  }

  function duration(seconds) {
    const s = Number(seconds) || 0;
    if (!s) return "—";
    if (s < 60) return `${s.toFixed(1)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  }

  function stat(label, value, note) {
    return `<div class="analytics-stat">
      <span class="analytics-stat-value">${escapeHtml(value)}</span>
      <span class="analytics-stat-label">${escapeHtml(label)}</span>
      ${note ? `<span class="analytics-stat-note">${escapeHtml(note)}</span>` : ""}
    </div>`;
  }

  function renderAnalytics(a) {
    const rate = a.documents_per_minute;
    const totalCost = (Number(a.cost) || 0) + (Number(a.split_cost) || 0);

    const headline = [
      stat("total cost", money(totalCost),
           a.split_cost ? `incl. ${money(a.split_cost)} to split` : ""),
      stat("time drafting", duration(a.seconds),
           a.measured_count ? `across ${a.measured_count} document${a.measured_count === 1 ? "" : "s"}` : ""),
      stat("rate", rate ? `${rate.toFixed(1)} docs/min` : "—",
           a.seconds_per_document ? `${a.seconds_per_document.toFixed(1)}s per document` : ""),
    ].join("");

    const rows = [
      ["Documents drafted", `${a.drafted_count} of ${a.document_count}`],
      ["Pages drafted", String(a.page_count || 0)],
      ["Cost per document", a.cost_per_document ? money(a.cost_per_document) : "—"],
      ["Seconds per page", a.seconds_per_page ? `${a.seconds_per_page.toFixed(1)}s` : "—"],
      ["Model", modelBreakdown(a)],
      ["Prompt", a.prompt_versions.length ? a.prompt_versions.join(", ") : "—"],
      ["First drafted", (a.first_run || "").slice(0, 16).replace("T", " ") || "—"],
      ["Last drafted", (a.last_run || "").slice(0, 16).replace("T", " ") || "—"],
    ].map(([label, value]) => `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(value)}</td></tr>`).join("");

    // Rows drafted before timing was recorded carry no duration, and counting them as instant
    // would report a speed the pipeline has never reached.
    const caveat = a.unmeasured_count
      ? `<p class="analytics-caveat">${a.unmeasured_count} document${a.unmeasured_count === 1 ? " was" : "s were"}
         drafted before timing was recorded, so ${a.unmeasured_count === 1 ? "it is" : "they are"}
         counted in the cost but not in the times.</p>`
      : "";

    analyticsBody.innerHTML =
      `<div class="analytics-stats">${headline}</div><table class="analytics-table">${rows}</table>${caveat}`;
  }

  async function openAnalytics(folderId, displayName, selectedWorkflow) {
    analyticsFolder.textContent = displayName || folderId;
    analyticsBody.innerHTML = `<p class="analytics-loading">Working it out…</p>`;
    analyticsModal.classList.remove("hidden");
    try {
      const workflow = selectedWorkflow || state.activeWorkflow;
      const res = await fetch(
        `/api/analytics/folder?folder_id=${encodeURIComponent(folderId)}&workflow=${workflow}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        analyticsBody.innerHTML =
          `<p class="analytics-caveat">${escapeHtml(body.detail || "Could not load these numbers.")}</p>`;
        return;
      }
      renderAnalytics(await res.json());
    } catch (err) {
      analyticsBody.innerHTML = `<p class="analytics-caveat">Could not load these numbers: ${escapeHtml(String(err))}</p>`;
    }
  }

  // ---------- Settings & How It Works modal (gear icon, top of sidebar) ----------

  const settingsModal = el("settings-modal");
  const settingsStatus = el("settings-status");

  function setSettingsStatus(msg, isError) {
    settingsStatus.textContent = msg;
    settingsStatus.classList.remove("hidden");
    settingsStatus.classList.toggle("pipeline-status-error", !!isError);
  }

  async function loadSettingsIntoForm() {
    settingsStatus.classList.add("hidden");
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) return;
      const body = await res.json();
      el("settings-new-document-batch-size").value = body.new_document_batch_size;
      el("settings-max-total-cost").value = body.max_total_cost;
      el("settings-cost-hint").textContent =
        `All-time model spend cap -- $${body.total_cost.toFixed(4)} used so far. Nothing that ` +
        "costs money runs once this is reached.";
    } catch {
      // best-effort -- the form just keeps whatever was in it before
    }
  }

  // ---------- appearance ----------

  // Light unless someone chooses dark or the system's own setting; the head script reads it before paint.
  const THEME_KEY = "incipit-theme";
  function applyTheme(choice) {
    if (choice === "dark" || choice === "light") document.documentElement.setAttribute("data-theme", choice);
    else document.documentElement.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.setAttribute("aria-checked", String(button.dataset.themeChoice === (choice || "light")));
    });
  }
  let savedTheme = "light";
  try { savedTheme = localStorage.getItem(THEME_KEY) || "light"; } catch { /* storage blocked: light */ }
  applyTheme(savedTheme);
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.addEventListener("click", () => {
      const choice = button.dataset.themeChoice;
      try {
        localStorage.setItem(THEME_KEY, choice);
      } catch { /* storage blocked: it still applies for this visit */ }
      applyTheme(choice);
    });
  });

  function openSettingsModal() {
    settingsModal.classList.remove("hidden");
    // Whatever tab this reader actually has.
    const tab = document.querySelector(".settings-tab.active") || document.querySelector(".settings-tab");
    if (tab) tab.click();
  }

  function closeSettingsModal() {
    settingsModal.classList.add("hidden");
  }

  el("btn-settings").addEventListener("click", openSettingsModal);
  el("settings-close").addEventListener("click", closeSettingsModal);
  // Only the backdrop itself closes it -- clicking inside the form/content must not, unlike the
  // image preview modal above (which has no interactive content to protect from an accidental
  // close).
  settingsModal.addEventListener("click", (e) => {
    if (e.target === settingsModal) closeSettingsModal();
  });

  document.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const target = tab.dataset.settingsTab;
      // Every panel that is present, named rather than listed: a reviewer's page has had two of
      // them removed, and naming all three meant this handler threw on the first missing one and
      // never reached the panel it was opening.
      document.querySelectorAll(".settings-panel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.id !== "settings-panel-" + target);
      });
      // Fetched on open rather than kept live: both are admin-only reads, and a reviewer looking
      // at either wants what is true right now, possibly changed by someone else on another
      // device.
      if (target === "settings") loadSettingsIntoForm();
      if (target === "deletions") loadDeletionLog();
      if (target === "running") renderGenerationLog();
    });
  });

  el("settings-save").addEventListener("click", async () => {
    const payload = {
      new_document_batch_size: parseInt(el("settings-new-document-batch-size").value, 10),
      max_total_cost: parseFloat(el("settings-max-total-cost").value),
    };
    if (!Number.isFinite(payload.new_document_batch_size) || !Number.isFinite(payload.max_total_cost)) {
      setSettingsStatus("Every field is required.", true);
      return;
    }
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSettingsStatus(body.detail || "Couldn't save settings.", true);
        return;
      }
      await loadSettingsIntoForm();
      setSettingsStatus("Saved.", false);
    } catch (err) {
      setSettingsStatus(`Couldn't save settings: ${err}`, true);
    }
  });

  // Returns true once it's safe to navigate away from the currently open document (nothing to
  // lose, or the reviewer explicitly chose to save or discard); false if they chose Cancel.
  async function confirmLeaveDocument() {
    if (!state.dirty) return true;
    const choice = await showLeaveModal();
    if (choice === "cancel") return false;
    if (choice === "discard") {
      state.dirty = false;
      return true;
    }
    // "save" -- reuse the same save path as the Save Draft button; only proceed if it worked, so a
    // failed save (e.g. network hiccup) never silently loses the reviewer's edits.
    return saveReview(state.currentDoc.review.status, null);
  }

  // ---------- queue view ----------

  async function loadQueue() {
    // Both workflows have a folder browser now; each has its own endpoint because the grouping
    // differs (scan-folder prefix vs the live site's archival folder).
    const [documents, folders] = await Promise.all([
      fetch(`${apiBase()}/documents${tabQuery()}`).then((r) => r.json()),
      fetch(`/api/new/folders${tabQuery()}`)
        // A non-2xx response still has a JSON body (FastAPI's {"detail": "..."} error shape), so
        // it must be checked explicitly -- without this, a server error was silently treated as
        // the folder list itself, and state.folders.length rendered as literal "undefined".
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .catch((err) => {
          console.error("failed to load folders:", err);
          return [];
        }),
    ]);
    state.documents = documents;
    state.folders = folders;
    renderQueue();
    loadPendingBatches();
    refreshPendingCounts();
  }

  // What people call a document: its display id, falling back to the internal id.
  function shownId(doc) {
    return (doc && (doc.display_id || doc.document_id)) || "";
  }

  // Shows "N document(s) awaiting drafting" under the pipeline actions, so a reviewer can see how
  // much is queued (and roughly how many Draft Pending clicks are left).
  async function refreshPendingCounts() {
    try {
      const res = await fetch("/api/pending-counts");
      if (!res.ok) return;
      const counts = await res.json();
      const setNote = (id, n, noun) => {
        const note = el(id);
        if (n > 0) {
          note.textContent = `${n} ${noun}${n === 1 ? "" : "s"} awaiting drafting`;
          note.classList.remove("hidden");
        } else {
          note.classList.add("hidden");
        }
      };
      setNote("new-pending-note", counts.new_awaiting_drafting, "document");
    } catch {
      // best-effort -- the form just keeps whatever was in it before
    }
  }

  // The banner reports work that can run for minutes; a reviewer who has seen it should be able to
  // get it out of the way without losing it.
  let activityRunning = false;

  const ACTIVITY_MIN_KEY = "incipit.activityMinimized";

  function activityMinimized() {
    try { return localStorage.getItem(ACTIVITY_MIN_KEY) === "1"; } catch { return false; }
  }

  // Minimized means gone, not smaller.



  // ---------- the generation log ----------

  let activeJobs = [];

  // ---------- what is running, live ----------

  // The server's elapsed_seconds is true at the moment it was read, and the panel used to render
  // it once and leave it there -- a frozen clock a reviewer reads as a stalled pipeline.
  let activeJobsReceivedAt = Date.now();
  let activityTicker = null;

  function liveElapsed(job) {
    return Math.max(0, Math.round((job.elapsed_seconds || 0)
                                  + (Date.now() - activeJobsReceivedAt) / 1000));
  }

  function spellElapsed(seconds) {
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  // "drafting" -> "Drafting".
  function spellStep(kind) {
    const step = String(kind || "working");
    return step.charAt(0).toUpperCase() + step.slice(1);
  }

  // "qwen3.7-plus (22), qwen3.6-35b-a3b (1)" -- the second is the fallback, and how often a
  // document needed it is worth seeing.
  function modelBreakdown(a) {
    const counts = a.documents_by_model || {};
    const names = Object.keys(counts);
    if (!names.length) return a.models && a.models.length ? a.models.join(", ") : "—";
    if (names.length === 1) return names[0];
    return names.map((name) => `${name} (${counts[name]})`).join(", ");
  }

  function renderActivityBars() {
    const container = el("activity-bars");
    container.innerHTML = activeJobs.map((job) => {
      const remaining = typeof job.remaining === "number" && job.remaining > 0
        ? `<span class="activity-bar-remaining">${job.remaining} document${job.remaining === 1 ? "" : "s"} remaining</span>`
        : "";
      const folder = job.folder
        ? `<span class="activity-bar-folder">${escapeHtml(job.folder)}</span>`
        : "";
      return `
        <div class="activity-bar">
          ${folder}
          <span class="activity-bar-step">${escapeHtml(spellStep(job.kind))}</span>
          ${remaining}
          <span class="activity-bar-elapsed">${spellElapsed(liveElapsed(job))} elapsed</span>
          ${job.detail ? `<span class="activity-bar-detail">${escapeHtml(job.detail)}</span>` : ""}
        </div>`;
    }).join("");
    // While the bars are up the single status line would be a second copy of them.
    if (activeJobs.length) el("pipeline-status").classList.add("hidden");
  }

  function tickActivity() {
    renderActivityBars();
    if (!el("settings-panel-running").classList.contains("hidden")) renderGenerationLog();
    if (!activeJobs.length && activityTicker) {
      clearInterval(activityTicker);
      activityTicker = null;
    }
  }

  function renderGenerationLog() {
    const body = el("generation-log-body");
    if (!activeJobs.length) {
      body.innerHTML = '<p class="generation-log-empty">Nothing is running.</p>';
      return;
    }
    body.innerHTML = activeJobs.map((job) => {
      const elapsed = spellElapsed(liveElapsed(job));
      const who = job.owner_name ? escapeHtml(job.owner_name) : "the pipeline";
      return `
        <div class="generation-log-row">
          <span class="generation-log-kind">${escapeHtml(job.kind || "")}</span>
          <div>
            <div>${escapeHtml(job.label || "")}</div>
            ${job.detail ? `<div class="generation-log-detail">${escapeHtml(job.detail)}</div>` : ""}
            <div class="generation-log-detail">requested by ${who}</div>
          </div>
          <span class="generation-log-elapsed">${elapsed}</span>
        </div>`;
    }).join("");
  }



  // A batch can be uploaded (and even split-proposed) without ever being confirmed -- e.g. the
  // reviewer's browser wasn't open for the upload, or they navigated away before confirming.
  async function loadPendingBatches() {
    if (state.activeWorkflow !== "new") {
      pendingBatches.classList.add("hidden");
      return;
    }
    // Only on the folder grid, and only when nothing is running.
    if (!showFolderGrid() || activityRunning || !splitReviewView.classList.contains("hidden")) {
      pendingBatches.classList.add("hidden");
      return;
    }
    const res = await fetch(`/api/new/batches${tabQuery()}`);
    // Never the batch being set up right now.
    const batches = (await res.json()).filter((b) => b.batch_id !== state.batchInFlight);
    if (!batches.length) {
      pendingBatches.classList.add("hidden");
      return;
    }
    pendingBatches.classList.remove("hidden");
    pendingBatchesList.innerHTML = batches.map((b) => {
      // The one thing a reviewer couldn't previously tell from this panel: whether an "uploaded"
      // batch with no proposal yet is just sitting there untouched, or already partway through
      // being examined (e.g. they switched tabs mid-proposal and came back) --
      // split_proposal_cursor makes that visible instead of every unproposed batch looking
      // identically untouched.
      let progressText;
      if (b.status === "split_proposed") {
        progressText = "split proposed";
      } else if (b.split_proposal_cursor > 0) {
        progressText = `interrupted &mdash; ${b.split_proposal_cursor} of ${b.image_count} page(s) examined`;
      } else {
        progressText = "uploaded, never split";
      }
      if (b.uploaded_by_name) {
        // Who put this here.
        progressText += ` &middot; uploaded by ${escapeHtml(b.uploaded_by_name)}`;
      }
      return `
      <div class="pending-batch-item">
        <span>${b.image_count} page(s)${b.folder_display_id ? ` &middot; folder ${escapeHtml(b.folder_display_id)}` : ""}
          &middot; ${progressText}</span>
        <div class="pending-batch-actions">
          <button class="btn btn-secondary btn-small" data-resume-batch-id="${b.batch_id}">Resume</button>
          ${document.body.dataset.role === "reviewer" ? "" :
            `<button class="btn btn-danger btn-small" data-remove-batch-id="${b.batch_id}">Remove</button>`}
        </div>
      </div>
    `;
    }).join("");
    pendingBatchesList.querySelectorAll("button[data-resume-batch-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const res = await fetch(`/api/new/batches/${btn.dataset.resumeBatchId}`);
        if (!res.ok) { toast("Could not load batch"); return; }
        openSplitReview(await res.json());
      });
    });
    pendingBatchesList.querySelectorAll("button[data-remove-batch-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const batchId = btn.dataset.removeBatchId;
        // Discarding a batch throws away its uploaded scans and any split proposal already paid
        // for, so it goes through the same attributed-delete dialog as everything else.
        const ctx = await confirmDelete(
          "Remove batch?",
          "This permanently discards the uploaded scans in this batch and any split proposal made for them. This cannot be undone."
        );
        if (!ctx) return;
        btn.disabled = true;
        try {
          const res = await fetch(`/api/new/batches/${batchId}?${deletionParams(ctx)}`, { method: "DELETE" });
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            toast(`Remove failed: ${body.detail || res.statusText}`);
            return;
          }
          toast("Batch removed");
          loadPendingBatches();
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  // The set of documents in view: the whole queue, or, drilled into a folder, just that folder's.
  function documentsInScope() {
    if (state.activeFolder === null) return state.documents;
    return state.documents.filter((d) => d.folder_id === state.activeFolder);
  }

  function filteredDocuments() {
    const scope = documentsInScope();
    if (state.filter === "all") return scope;
    // "Commented" is not a review status -- it is the question someone left open on a document.
    if (state.filter === "commented") return scope.filter(hasOpenComments);
    return scope.filter((d) => d.review_status === state.filter);
  }

  function updateCounts() {
    const scope = documentsInScope();
    const counts = { all: scope.length, unreviewed: 0, accepted: 0, commented: 0 };
    scope.forEach((d) => {
      counts[d.review_status] = (counts[d.review_status] || 0) + 1;
      if (hasOpenComments(d)) counts.commented += 1;
    });
    Object.entries(counts).forEach(([key, n]) => {
      const badge = el(`count-${key}`);
      if (badge) badge.textContent = n;
    });
  }

  function updateWorkflowPill() {
    workflowPill.textContent = "Documents";
    workflowPill.classList.add("workflow-pill-new");
  }

  function isReviewerView() {
    return document.body.classList.contains("role-reviewer");
  }

  /* Approved, and every document it holds went out: a partial "export anyway" is not a finish. */
  function fullyExported(f) {
    return !!f.export && !!f.all_approved
      && (f.export.document_count || 0) >= (f.document_count || 0);
  }

  /* Every document carries the reviewer's verdict and the folder is not out with anybody. */
  function readyToExport(f) {
    // The admin's review alone decides this; a reviewer still holding the folder does not hold it back.
    return !isReviewerView() && !!f.all_approved && !fullyExported(f);
  }

  // Assignment, shown on the folder card itself rather than in a separate list.
  function renderProgress(f) {
    const p = f.progress;
    if (!p || !p.total) return "";
    const mine = p.viewer || p;
    const theirs = p.other;
    // A sliver so one reviewed document out of ninety is still visible, but only once there is
    // something to show: an unconditional floor drew a bar on a folder nobody had touched.
    const pct = (n) => (n.reviewed === 0 ? 0 : Math.max(2, n.percent));
    // Green says one thing: nothing is owed on this folder. A reviewer's own bar is their own work;
    // a reviewer's turns green only once the folder is ready to export, or has been.
    const done = isReviewerView()
      ? f.assignment_status === "approved"
      : readyToExport(f) || fullyExported(f);
    const theirName = p.other_name ? p.other_name.split(" ")[0] : `the ${term("reviewer", { lower: true })}`;
    // Who reviewed what, by name rather than as the viewer (admin only; a reviewer is never sent it).
    const reviewers = f.reviewers || null;
    const reviewedByAdmins = reviewers ? reviewers.reduce((n, r) => n + r.count, 0) : mine.reviewed;
    const caption = theirs
      ? `${reviewedByAdmins} of ${mine.total} reviewed`
      : `${mine.reviewed} of ${mine.total} reviewed`;
    const adminMarks = reviewers && reviewers.length ? reviewers.map(adminPill).join("") : "";
    const reviewerMarks = theirs
      ? folderReviewerPill(theirs.accepted, "accepted", "pill-reviewer", p.other_name, theirName)
      : "";
    // Reviewers and reviewer on separate labelled rows, so whose work is whose is never in doubt.
    const pills = adminMarks || reviewerMarks
      ? `<div class="folder-marks">${markRow(term("admin", { plural: true }), adminMarks)}${markRow(term("reviewer"), reviewerMarks)}</div>`
      : "";
    return `
      <div class="folder-progress" title="${escapeHtml(caption.replace(/&middot;/g, "·"))}">
        <div class="folder-progress-bar">
          ${theirs ? `<span class="is-theirs" style="width:${pct(theirs)}%"></span>` : ""}
          <span class="is-mine ${done ? "is-complete" : ""}" style="width:${pct(mine)}%"></span>
        </div>
        <div class="folder-progress-text">
          ${caption}
        </div>
        ${pills}
      </div>`;
  }

  /** One labelled row of marks on a folder card, or nothing when it has none. */
  function markRow(label, marks) {
    return marks ? `<div class="mark-row"><span class="mark-row-label">${label}</span>`
      + `<div class="mark-row-pills">${marks}</div></div>` : "";
  }

  /** What the reviewer did to a folder, one verdict at a time. */
  function folderReviewerPill(count, verdict, css, fullName, firstName) {
    if (!count) return "";
    const who = escapeHtml(fullName || `The ${term("reviewer", { lower: true })}`);
    return `<span class="pill ${css}" title="${who} ${verdict} ${count} document${count === 1 ? "" : "s"}">`
      + `${pillText(escapeHtml(firstName), `${verdict} ${count}`)}</span>`;
  }

  /* A name that gives way to "…" before the count ever does. */
  function pillText(name, rest) {
    return `<span class="pill-name">${name}</span> <span class="pill-rest">${rest}</span>`;
  }

  /** One reviewer on a folder card, in their own colour; your own opens the colour choice. */
  function adminPill(r) {
    if (!r.name) {
      return `<span class="pill pill-admin" title="${r.count} reviewed before names were recorded">`
        + `${pillText("Unnamed", `reviewed ${r.count}`)}</span>`;
    }
    const first = escapeHtml((r.name || "").split(" ")[0]);
    const style = r.bg ? ` style="--pill-bg:${r.bg};--pill-ink:${r.ink}"` : "";
    const what = `${escapeHtml(r.name)} reviewed ${r.count} document${r.count === 1 ? "" : "s"}`;
    if (r.user_id != null && r.user_id === state.myId) {
      return `<button type="button" class="pill pill-admin"${style} data-colour-picker
                title="${what} &mdash; change your colour">${pillText(first, `reviewed ${r.count}`)}</button>`;
    }
    return `<span class="pill pill-admin"${style} title="${what}">${pillText(first, `reviewed ${r.count}`)}</span>`;
  }

  // Whose desk the folder is on, said from the viewer's own side.
  const STAGE_LABELS = {
    admin: { assigned: "With {name}", submitted: "With you", returned: "With {name}",
             approved: "Approved" },
    reviewer: { assigned: "With you", submitted: "With {admin}", returned: "Needs changes",
               approved: "Approved" },
  };

  function stageLabel(f) {
    const isReviewer = document.body.classList.contains("role-reviewer");
    const table = STAGE_LABELS[isReviewer ? "reviewer" : "admin"];
    const label = table[f.assignment_status];
    if (!label) return "";
    return label.replace("{name}", (f.assigned_to_name || term("reviewer", { lower: true })).split(" ")[0])
      .replace("{admin}", term("admin", { lower: true }));
  }

  function renderAssignment(f) {
    if (!f.assignment_status || f.assignment_status === "unassigned") {
      // Admins are told a folder is spare; reviewers never see an unassigned folder at all.
      return document.body.classList.contains("role-admin")
        ? `<div class="folder-assign folder-assign-free">Unassigned</div>` : "";
    }
    const isReviewer = document.body.classList.contains("role-reviewer");
    // `??`, not `||`: `assigned` maps to the empty string on purpose, and `||` treated that as "no
    // entry" and fell through to the raw status word, printing "assigned" on the card.
    const canSend = isReviewer && (f.assignment_status === "assigned" || f.assignment_status === "returned");
    // A reviewer is not told who the folder is assigned to.
    const rows = isReviewer ? [] : [["To", f.assigned_to_name || "someone"]];
    if (f.assigned_by_name) rows.push(["Assigned by", f.assigned_by_name]);
    const note = realNote(f);
    if (note) rows.push(["Note", note]);

    return `
      <div class="folder-assign st-${escapeHtml(f.assignment_status)}">
        ${rows.map(([label_, value], i) => `
          <div class="folder-assign-row">
            <span class="folder-assign-label">${escapeHtml(label_)}</span>
            <span class="folder-assign-value" title="${escapeHtml(value)}">${escapeHtml(value)}</span>
          </div>`).join("")}
        ${canSend ? `<button type="button" class="btn btn-accept btn-small folder-send"
             data-folder="${escapeHtml(f.folder_id)}" data-shown="${escapeHtml(f.folder_display_id || "")}"
             data-workflow="${escapeHtml(f.workflow || state.activeWorkflow)}"
           >Send to ${escapeHtml(term("admin", { lower: true }))}</button>` : ""}
        ${renderAdminFolderActions(f)}
      </div>`;
  }

  // A note somebody actually wrote, or "" for one the system made up.
  function realNote(f) {
    const note = (f.latest_note || "").trim();
    if (!note) return "";
    const manufactured = `to ${(f.assigned_to_name || "").trim()}`.toLowerCase();
    return note.toLowerCase() === manufactured ? "" : note;
  }

  // Sending a folder back, on the folder itself. Approving is not a button: a folder whose last
  // document has been accepted approves itself (see collab.settle_approval).
  function renderAdminFolderActions(f) {
    if (document.body.dataset.role !== "admin" || !f.assigned_to_name) return "";
    // workspace.js stamps the role on <body> at sign-in; app.js has no user of its own.
    const workflow = escapeHtml(f.workflow || state.activeWorkflow);
    const id = escapeHtml(f.folder_id);
    // Only once the folder is back with the administrator, and never once it has gone out.
    const canReturn = !fullyExported(f)
      && (f.assignment_status === "submitted" || f.assignment_status === "approved");
    // "Send back", not "Send back to Makayla Bridgers".
    const back = canReturn
      ? `<button type="button" class="btn btn-lifecycle btn-sendback" data-return="${id}"
          data-workflow="${workflow}" data-reviewer="${escapeHtml(f.assigned_to_name || "")}"
          title="Send back to ${escapeHtml(f.assigned_to_name)}">Send back</button>` : "";
    if (!back) return "";               // no empty row taking up space on the card
    return `<div class="folder-admin-actions">${back}</div>`;
  }

  // Who is working on this folder, among admins.
  function renderClaim(f) {
    if (document.body.dataset.role !== "admin" || !f.claim) return "";
    const mine = f.claim.admin_id === state.myId;
    const when = escapeHtml((f.claim.claimed_at || "").slice(0, 16).replace("T", " "));
    return `<span class="claim-tag ${mine ? "is-mine" : ""}" title="Claimed ${when}"
      >${mine ? "Yours" : escapeHtml(f.claim.by)}</span>`;
  }

  function closeCardMenus() {
    document.querySelectorAll(".card-menu-list").forEach((list) => { list.hidden = true; });
    document.querySelectorAll(".card-menu-button").forEach(
      (b) => b.setAttribute("aria-expanded", "false"));
  }

  // An open menu has to close on anything that is not it -- including a click on the card behind
  // it, which would otherwise open the folder with the menu still hanging over the new screen.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".card-menu")) closeCardMenus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCardMenus();
  });

  // Takes a folder back from the reviewer holding it; their own verdicts are kept.
  async function unassignFolder(folderId, workflow, holderName, shown) {
    if (!window.confirm(`Unassign ${shown || "this folder"} from ${holderName}?

`
        + "They keep their edits, decisions, and comments, but the folder leaves their queue "
        + "until it is assigned again.")) return;
    const response = await fetch("/api/team/unassign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folderId, workflow }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) { toast(body.detail || "That folder could not be unassigned.", true); return; }
    toast(`${shown || "The folder"} unassigned from ${body.reviewer || holderName}`);
    loadQueue();
    if (window.__refreshTeam__) window.__refreshTeam__();
  }

  // One menu, so the card carries a name and its progress rather than five scraps of text.
  function renderCardMenu(f) {
    if (document.body.dataset.role !== "admin") return "";
    const id = escapeHtml(f.folder_id);
    const workflow = escapeHtml(f.workflow || state.activeWorkflow);
    const spent = Number(f.cost_estimate) || 0;
    const items = [];
    if (spent || Number(f.drafted_count)) {
      items.push(`<button type="button" role="menuitem" data-analytics="${id}">Drafting analytics</button>`);
    }
    if (!f.claim) {
      items.push(`<button type="button" role="menuitem" data-claim="${id}" data-workflow="${workflow}">Claim this folder</button>`);
    } else if (f.claim.admin_id === state.myId) {
      items.push(`<button type="button" role="menuitem" data-release="${id}" data-workflow="${workflow}">Release my claim</button>`);
    }
    // Taking a folder back from the reviewer holding it.
    if (f.assigned_to) {
      items.push(`<button type="button" role="menuitem" data-unassign="${id}" data-workflow="${workflow}"
        data-shown="${escapeHtml(f.folder_display_id || "")}" data-reviewer="${escapeHtml(f.assigned_to_name || `the ${term("reviewer", { lower: true })}`)}"
        >Unassign from ${escapeHtml(term("reviewer", { lower: true }))}</button>`);
    }
    // Exporting was reachable only from inside a folder, which is two clicks away from the grid
    // where an editor decides which folder is finished.
    if (Number(f.drafted_count)) {
      items.push(`<button type="button" role="menuitem" data-card-export="${id}">Export folder</button>`);
    }
    items.push(`<button type="button" role="menuitem" class="is-danger" data-card-delete="${id}">Delete folder</button>`);
    return `
      <div class="card-menu">
        <button type="button" class="card-menu-button" aria-haspopup="menu" aria-expanded="false"
                title="More actions for this folder">More</button>
        <div class="card-menu-list" role="menu" hidden>${items.join("")}</div>
      </div>`;
  }


  // Every control the card and its menu carry: the markup and the handlers travel together, or a
  // menu renders with buttons that do nothing.
  function bindFolderCard(card, f, reload) {
    const again = reload || (() => loadQueue());
    card.querySelectorAll("[data-colour-picker]").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      if (window.openColourPicker) window.openColourPicker(b, again);
    }));
    card.querySelectorAll("[data-analytics]").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        closeCardMenus();
        openAnalytics(b.dataset.analytics, folderDisplayName(f), f.workflow);
      });
    });
    card.querySelectorAll("[data-unassign]").forEach((b) => {
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        closeCardMenus();
        await unassignFolder(b.dataset.unassign, b.dataset.workflow, b.dataset.reviewer, b.dataset.shown);
        again();
      });
    });
    card.querySelectorAll("[data-claim], [data-release]").forEach((b) => {
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        closeCardMenus();
        await fetch("/api/team/claim", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            folder_id: b.dataset.claim || b.dataset.release,
            workflow: b.dataset.workflow,
            release: !!b.dataset.release,
          }),
        });
        again();
      });
    });
    card.querySelector("[data-card-export]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      closeCardMenus();
      startFolderExport(f);
    });
    card.querySelector("[data-card-delete]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      closeCardMenus();
      deleteFolder(f);
    });
    const menuButton = card.querySelector(".card-menu-button");
    menuButton?.addEventListener("click", (e) => {
      e.stopPropagation();
      const list = menuButton.nextElementSibling;
      const open = !list.hidden;
      closeCardMenus();
      if (open) return;
      list.hidden = false;
      menuButton.setAttribute("aria-expanded", "true");
    });
  }


  // Whether this folder's CSV has already gone out.
  function renderExportState(f) {
    if (!f.export) return "";
    const when = (f.export.exported_at || "").slice(0, 10);
    return `<span class="export-state is-done"
      title="Exported ${escapeHtml(when)} by ${escapeHtml(f.export.exported_by || "someone")}"
      >Exported ${escapeHtml(shortDate(when))}</span>`;
  }

  // "Sep 12" rather than "2026-09-12": the year is almost always this one, and the card is read at
  // a glance rather than filed.
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function shortDate(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!match) return iso || "";
    const [, year, month, day] = match;
    const label = `${MONTHS[Number(month) - 1]} ${Number(day)}`;
    return Number(year) === new Date().getFullYear() ? label : `${label} ${year}`;
  }

  // An open folder rather than a closed one -- it reads with more depth at this size.
  const FOLDER_ICON = `<svg class="folder-card-icon" viewBox="0 0 256 256" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M238.63574,112.9834A11.87364,11.87364,0,0,0,228.90039,108H212V88a12.01375,12.01375,0,0,0-12-12H130.6665a4.02652,4.02652,0,0,1-2.3999-.7998L100.5332,54.3999A12.07558,12.07558,0,0,0,93.3335,52H40A12.01375,12.01375,0,0,0,28,64V208c0,.01562.00439.02979.00439.04541a4.01438,4.01438,0,0,0,.06983.69189c.00928.0503.02051.09913.03174.14844a3.97651,3.97651,0,0,0,.20068.64649c.0127.03027.02881.0581.042.08838a4.04188,4.04188,0,0,0,.293.53955c.02442.03711.0459.07617.07129.11279.01514.02148.02686.04443.042.06592a3.9914,3.9914,0,0,0,.38135.44677c.03174.03272.06494.063.09815.09424a3.92028,3.92028,0,0,0,.51074.42139c.0083.00586.01562.01318.02392.01855a3.93644,3.93644,0,0,0,.59522.32276c.04687.02148.09326.043.1416.0625a4.01959,4.01959,0,0,0,.63623.19775c.04492.00977.09033.01612.13574.02442A3.99356,3.99356,0,0,0,32,212H208a4.0004,4.0004,0,0,0,3.79492-2.73535l28.49024-85.46973A11.87465,11.87465,0,0,0,238.63574,112.9834ZM40,60H93.3335a4.02652,4.02652,0,0,1,2.3999.7998l27.7334,20.8003A12.07558,12.07558,0,0,0,130.6665,84H200a4.00458,4.00458,0,0,1,4,4v20H69.76611a11.98452,11.98452,0,0,0-11.38427,8.20508L36,183.35059V64A4.00458,4.00458,0,0,1,40,60Zm192.69531,61.26465L205.11719,204H37.5498l28.42188-85.26465A3.99384,3.99384,0,0,1,69.76611,116H228.90039a3.99992,3.99992,0,0,1,3.79492,5.26465Z"/></svg>`;

  // Corner arrows, shown over a scan on hover.
const EXPAND_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>`;

const TRASH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>`;

  // Both workflows now open on a grid of folders; clicking one drills into its document list.
  function showFolderGrid() {
    return state.activeFolder === null;
  }

  /* Re-decides what belongs on screen after a view change, without waiting for the next poll. */
  function syncAnnouncements() {
    loadPendingBatches();
  }

  // The status filter chips (All/Unreviewed/Commented/Accepted) apply to documents currently in
  // scope" -- but at the New Documents top level (the folder grid) there IS no single scope, so
  // showing counts there was always either stale (renderFolderGrid never updated them) or
  // misleading (whichever folder/workflow was last viewed).
  function updateFilterNavVisibility() {
    const onGrid = showFolderGrid();
    filterNavSection.classList.toggle("hidden", onGrid);
    // The two never show together: inside a folder you filter documents, on the grid you filter
    // folders, and the question "which of these is mine to act on" only has meaning at one level.
    renderFilterNav();
    // On the grid, whichever workflow it is.
    folderFilterSection?.classList.toggle("hidden", !onGrid);
    // It narrows the folder list, so it belongs with it: gone inside a folder, where there is no
    // folder list to narrow.
    folderSearchWrap?.classList.toggle("hidden", !onGrid);
  }

  const FOLDER_FILTERS = {
    all: () => true,
    // Reviewers receive only their assigned folders from the server; this is their personal
    // incomplete-document view. Admin navigation uses the shared lifecycle filters below.
    mine: (f) => {
      const p = f.progress;
      if (!p || !p.total || !p.viewer) return false;
      const onMyDesk = f.assignment_status === "assigned" || f.assignment_status === "returned";
      return onMyDesk && p.viewer.reviewed < p.total;
    },
    // Admin queues are shared team queues. No assignment or claim ownership narrows them.
    "with-reviewer": (f) => !fullyExported(f) &&
      (f.assignment_status === "assigned" || f.assignment_status === "returned"),
    // Approved but not yet exported still wants a reviewer: it is where Ready to export lives.
    "needs-review": (f) => !fullyExported(f)
      && (f.assignment_status === "submitted" || f.assignment_status === "approved"),
    completed: (f) => fullyExported(f),
    // Reviewer-only lifecycle entries. Reviewer folder visibility is enforced by the server.
    sent: (f) => f.assignment_status === "submitted",
    returned: (f) => f.assignment_status === "returned",
    approved: (f) => f.assignment_status === "approved",
  };

  const FILTER_ICONS = {
    all: `<path d="M4 6h16M4 12h16M4 18h16"/>`,
    mine: `<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>`,
    "with-reviewer": `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>`,
    "needs-review": `<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>`,
    completed: `<path d="M20 6L9 17l-5-5"/>`,
    sent: `<path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>`,
    returned: `<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>`,
    approved: `<path d="M20 6L9 17l-5-5"/>`,
  };

  // Admins share a lifecycle queue; reviewers retain the statuses meaningful to their own work.
  const FILTER_NAVS = {
    admin: [
      ["all", "All"], ["with-reviewer", `With ${term("reviewer")}`],
      ["needs-review", "Needs review"], ["completed", "Completed"],
    ],
    reviewer: [
      ["all", "All"], ["mine", "Needs review"], ["sent", "Sent for review"],
      ["returned", "Sent back"], ["approved", "Approved"],
    ],
  };

  // Redrawn rather than drawn once: the role class lands on <body> when /api/me answers, which is
  // after this file first runs, so building the list at boot always built the admin one.
  function renderFilterNav() {
    if (!folderFilterNav) return;
    const role = document.body.classList.contains("role-reviewer") ? "reviewer" : "admin";
    if (folderFilterNav.dataset.role === role) return;
    folderFilterNav.dataset.role = role;
    folderFilterNav.innerHTML = FILTER_NAVS[role].map(
      ([key, label], i) => `
        <button class="nav-item${i === 0 ? " active" : ""}" data-folder-filter="${key}">
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${FILTER_ICONS[key]}</svg>
          <span class="nav-item-label">${label}</span>
          <span class="nav-count" id="count-folders-${key}">0</span>
        </button>`).join("");
    // A filter the other role does not have would otherwise stay selected and show nothing.
    if (!FILTER_NAVS[role].some(([key]) => key === state.folderFilter)) state.folderFilter = "all";
    // The document filters are in the page rather than built here, but the same word has to change
    // with the role: a reviewer's "accepted" is a review (see badgeLabel).
    const accepted = filterNav.querySelector('[data-filter="accepted"] .nav-item-label');
    if (accepted) accepted.textContent = role === "reviewer" ? "Accepted" : "Reviewed";
  }

  // What a typed term is matched against: what the card shows, and who it is with.
  function folderSearchText(f) {
    return [f.archive_title, f.folder_title, f.folder_display_id, f.folder_name, f.assigned_to_name,
            f.assigned_by_name, f.claim && f.claim.admin_name]
      .filter(Boolean).join(" ").toLowerCase();
  }

  function matchesFolderSearch(f) {
    return !state.folderSearch || folderSearchText(f).includes(state.folderSearch);
  }

  function visibleFolders() {
    const matches = FOLDER_FILTERS[state.folderFilter] || FOLDER_FILTERS.all;
    return state.folders.filter((f) => matches(f) && matchesFolderSearch(f));
  }

  function updateFolderCounts() {
    // Counted within the search, so the badges agree with what is on screen rather than telling
    // someone there are eleven approved folders while they are looking at one.
    const searched = state.folders.filter(matchesFolderSearch);
    for (const [name, matches] of Object.entries(FOLDER_FILTERS)) {
      const badge = el(`count-folders-${name}`);
      if (badge) badge.textContent = searched.filter(matches).length;
    }
  }

  // Typed into, not submitted: the grid narrows as you go.
  const SEARCH_DEBOUNCE_MS = 250;
  const SEARCH_MIN_CHARS = 3;
  let searchTimer = null;

  function applyFolderSearch() {
    const typed = folderSearch.value.trim().toLowerCase();
    // Below the minimum this is the start of a term rather than one, and matching on it would
    // empty the grid under someone mid-word.
    const term = typed.length >= SEARCH_MIN_CHARS ? typed : "";
    if (term === state.folderSearch) return;
    state.folderSearch = term;
    if (showFolderGrid()) renderFolderGrid();
  }

  folderSearch?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFolderSearch, SEARCH_DEBOUNCE_MS);
  });
  // Escape clears it at once -- waiting a quarter second to undo a search reads as a stuck field.
  folderSearch?.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    clearTimeout(searchTimer);
    folderSearch.value = "";
    applyFolderSearch();
  });

  function renderFolderGrid() {
    updateWorkflowPill();
    updateFilterNavVisibility();
    folderGrid.innerHTML = "";
    folderHeader.classList.add("hidden");
    folderDraftingNote.classList.add("hidden");
    queueList.classList.add("hidden");
    const shown = visibleFolders();
    updateFolderCounts();
    queueEmpty.classList.toggle("hidden", shown.length > 0);
    // The empty state is written for documents; on the grid it is folders that are missing, and
    // "no documents match" sends someone looking for a document filter that is not on screen.
    if (!shown.length) {
      queueEmpty.textContent = state.folderSearch
        ? `No folders match “${folderSearch ? folderSearch.value.trim() : ""}”.`
        : state.folders.length
          ? "No folders in this state."
          : "No folders yet — upload some scans to get started.";
    }
    folderGrid.classList.remove("hidden");
    const of = shown.length === state.folders.length ? "" : ` of ${state.folders.length}`;
    queueSubtitle.textContent = `${shown.length}${of} folder${shown.length === 1 ? "" : "s"}`;
    // Deliberately no document picker here.

    shown.forEach((f) => {
      const card = document.createElement("div");
      card.className = "folder-card";
      // The sub-line separates the two lifecycle stages so a reviewer understands why drilling in
      // may show fewer documents than the folder holds: only drafted (transcribed) documents are
      // reviewable; the rest are still awaiting drafting.
      const ready = `${f.drafted_count} ready to review`;
      const awaiting = f.awaiting_drafting_count
        ? ` &middot; ${f.awaiting_drafting_count} awaiting drafting`
        : "";
      // "3 of 4 approved" is the administrator's own running verdict on work a reviewer has handed
      // in -- how far through checking it somebody else is.
      const isReviewer = document.body.classList.contains("role-reviewer");
      // Deleting a folder wholesale only means something for New Documents, where the images are
      // held locally by this app.
      const canDelete = true;
      const stage = stageLabel(f);
      card.innerHTML = `
        <div class="folder-card-top">
          <div class="folder-card-title">
            ${FOLDER_ICON}
            <span class="folder-card-name">${escapeHtml(folderDisplayName(f))}</span>
          </div>
        </div>
        <div class="folder-card-sub">
          <span>${ready}${awaiting}</span>
          ${readyToExport(f)
            ? `<span class="st st-small st-ready">Ready to export</span>`
            : stage ? `<span class="st st-small st-${escapeHtml(f.assignment_status)}">${escapeHtml(stage)}</span>` : ""}
        </div>
        ${renderProgress(f)}
        ${renderAssignment(f)}
        <div class="folder-card-foot">
          ${isReviewer ? "" : renderExportState(f)}
          ${renderClaim(f)}
          ${f.awaiting_drafting_count ? `<span class="folder-card-cta">Open to draft &rsaquo;</span>` : ""}
          ${renderCardMenu(f)}
        </div>
      `;
      card.addEventListener("click", (e) => {
        // Every control on the card, which itself opens the folder, must not open it.
        if (e.target.closest("[data-claim], [data-release], [data-analytics], "
                             + "[data-return], [data-colour-picker], [data-unassign], "
                             + ".folder-send, .card-menu")) return;
        // A click while a menu is open dismisses it and does nothing else -- opening the folder as
        // well would carry the menu's own click into a screen it does not belong to.
        if (document.querySelector(".card-menu-list:not([hidden])")) return;
        openFolder(f.folder_id);
      });
      bindFolderCard(card, f, () => loadQueue());
      folderGrid.appendChild(card);
    });
  }

  // The folder's full name for display: its name, else its bare id.
  function folderDisplayName(folder) {
    return folder.folder_name || folder.folder_display_id || "Ungrouped";
  }

  function openFolder(folderId) {
    state.activeFolder = folderId;
    state.filter = "all";
    [...filterNav.children].forEach((c) => c.classList.toggle("active", c.dataset.filter === "all"));
    renderQueue();
    syncFolderDraftControls();
    syncAnnouncements();
  }

  /* A reviewer has been through this one. */
  function isReviewed(doc) {
    return doc.review_status === "accepted";
  }

  /* The line the folder is split on: above it is work, below it is done. */
  function reviewDivider(reviewedCount, total) {
    const row = document.createElement("div");
    row.className = "review-divider";
    row.innerHTML = `
      <span class="review-divider-label">Reviewed
        <span class="review-divider-count">${reviewedCount} of ${total}</span>
      </span>`;
    return row;
  }

  /* What the reviewer said about it. A document they want looked at carries a comment instead. */
  const REVIEWER_VERDICTS = {
    accepted: { label: "Accepted", css: "pill-reviewer", said: "accepted" },
  };

  function reviewerPill(doc) {
    const verdict = REVIEWER_VERDICTS[doc.reviewer_status];
    if (!verdict) return "";
    // A folder is assigned to one reviewer at a time, so the name told nobody anything the folder
    // did not already say -- it stays in the tooltip.
    const who = doc.reviewer_name
      ? `${escapeHtml(doc.reviewer_name)} ${verdict.said} this document`
      : `The ${term("reviewer", { lower: true })} ${verdict.said} this document`;
    return `<span class="pill ${verdict.css}" title="${who}">${verdict.label}</span>`;
  }

  const COMMENT_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M20 4H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h3v4l4-4h9a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1z"/>
    <path d="M7.5 9h9M7.5 12.5h6"/></svg>`;

  function hasOpenComments(doc) {
    return (doc.open_comments || 0) > 0;
  }

  /* The comment chip: how many, whether any are still open, and a way to read them from here. */
  function commentChip(doc) {
    const total = doc.comments || 0;
    const open = doc.open_comments || 0;
    const title = total === 0
      ? "No comments yet"
      : `${total} comment${total === 1 ? "" : "s"}${open ? `, ${open} still open` : ", all resolved"}`;
    // The count sits in the corner of the icon, so a row is the same height with or without it.
    const count = total ? `<span class="queue-comment-count">${total > 99 ? "99+" : total}</span>` : "";
    return `<button class="queue-comments${open ? " has-open" : ""}${total ? "" : " is-empty"}"
      title="${title}" aria-label="${title}">${COMMENT_ICON}${count}</button>`;
  }

  /* Reading a thread is not reviewing a document: the peek answers "what is being asked here"
     without loading the scans, the transcription and the whole side-by-side screen. */
  const peek = el("comment-peek");
  let peekOpensAt = -1;
  let peekRequest = 0;

  function closeCommentPeek() {
    peekRequest += 1; // any response still in flight belongs to the dialog being closed
    peek.classList.add("hidden");
    peekOpensAt = -1;
  }

  // What Open does in a peek raised outside a folder's document list.
  let peekOnOpen = null;

  async function openCommentPeek(doc, index, { workflow: asked, onOpen, openLabel } = {}) {
    const request = ++peekRequest;
    peekOpensAt = index;
    peekOnOpen = onOpen || null;
    el("comment-peek-open").textContent = openLabel || "Open the document";
    el("comment-peek-doc").textContent = doc.title || shownId(doc);
    el("comment-peek-list").innerHTML = `<p class="comment-peek-empty">Loading…</p>`;
    peek.classList.remove("hidden");
    el("comment-peek-close").focus({ preventScroll: true });

    const workflow = asked || state.activeWorkflow;
    let comments = [];
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(doc.document_id)}/comments`
        + `?workflow=${encodeURIComponent(workflow)}`);
      if (!res.ok) throw new Error(res.status);
      ({ comments } = await res.json());
    } catch (err) {
      if (request !== peekRequest) return;
      el("comment-peek-list").innerHTML =
        `<p class="comment-peek-empty">Those comments could not be loaded.</p>`;
      return;
    }
    // Still the document the reviewer asked about -- a slow answer must not land in a later peek.
    if (request !== peekRequest) return;
    el("comment-peek-list").innerHTML = comments.length
      ? comments.map(commentPeekRow).join("")
      : `<p class="comment-peek-empty">No comments on this document yet.</p>`;
  }

  function commentPeekRow(comment) {
    const when = formatDeletedAt(comment.at);
    return `<div class="comment-peek-item${comment.resolved ? " is-resolved" : ""}">
      <div class="comment-peek-meta">
        <span class="comment-peek-who">${escapeHtml(comment.by)}</span>
        <span class="comment-peek-when">${escapeHtml(when)}</span>
        ${comment.resolved ? `<span class="comment-peek-done">Resolved</span>` : ""}
      </div>
      <div class="comment-peek-body">${escapeHtml(comment.body)}</div>
    </div>`;
  }

  el("comment-peek-close").addEventListener("click", closeCommentPeek);
  // The backdrop is the dialog itself, so only a click that missed the card closes it.
  peek.addEventListener("click", (e) => { if (e.target === peek) closeCommentPeek(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !peek.classList.contains("hidden")) closeCommentPeek();
  });
  el("comment-peek-open").addEventListener("click", () => {
    const index = peekOpensAt;
    const onOpen = peekOnOpen;
    closeCommentPeek();
    if (onOpen) onOpen();
    else if (index >= 0) openDocumentByIndex(index);
  });

  function queueRow(doc, docs) {
    const item = document.createElement("div");
    item.className = "queue-item"
      + (state.resplitSelection.has(doc.document_id) ? " is-resplit-selected" : "")
      + (isAdmin() && isReviewed(doc) ? " is-reviewed" : "");
    // Who reviewed it, beside its name, so a folder answers "which of us did this one" without
    // opening anything.
    const reviewedBy = isAdmin() && isReviewed(doc) && doc.reviewed_by
      ? `<span class="queue-item-reviewed-by">Reviewed by ${escapeHtml(doc.reviewed_by)}</span>` : "";
    item.innerHTML = `
      ${state.resplitMode ? `<input type="checkbox" class="queue-item-pick" ${state.resplitSelection.has(doc.document_id) ? "checked" : ""} />` : ""}
      <div class="queue-item-main">
        <div class="queue-item-title">${escapeHtml(doc.title) || escapeHtml(shownId(doc))}${reviewedBy}</div>
        <div class="queue-item-sub">${escapeHtml(shownId(doc))} &middot; ${doc.page_count} page${doc.page_count === 1 ? "" : "s"}
          ${doc.challenges.map((c) => `<span class="tag">${escapeHtml(c)}</span>`).join("")}
        </div>
      </div>
      <div class="queue-item-right">
        ${reviewerPill(doc)}
        ${doc.sent_back ? `<span class="badge badge-sent-back"
           title="The ${escapeHtml(term("admin", { lower: true }))} picked this one out when they sent the folder back">Needs work</span>` : ""}
        <span class="badge ${badgeClass(doc.review_status)}">${badgeLabel(doc.review_status)}</span>
        ${state.resplitMode ? "" : commentChip(doc)}
        ${state.resplitMode ? "" : `<button class="queue-item-delete" title="Delete document" aria-label="Delete document">${TRASH_ICON}</button>`}
      </div>
    `;
    // In Fix Split mode a row is a selection checkbox, not a link into the document -- so it gets
    // the picking handler *instead of* the open/delete pair, never both.
    if (state.resplitMode) {
      item.addEventListener("click", () => {
        if (state.resplitSelection.has(doc.document_id)) state.resplitSelection.delete(doc.document_id);
        else state.resplitSelection.add(doc.document_id);
        renderQueue();
      });
    } else {
      item.addEventListener("click", () => openDocumentByIndex(docs.indexOf(doc)));
      item.querySelector(".queue-comments").addEventListener("click", (e) => {
        e.stopPropagation(); // reading the thread is not opening the document
        openCommentPeek(doc, docs.indexOf(doc));
      });
      item.querySelector(".queue-item-delete").addEventListener("click", (e) => {
        e.stopPropagation(); // must not also open the document
        deleteDocumentFromQueue(doc.document_id);
      });
    }
    return item;
  }

  function renderQueue() {
    endResplitIfLeftTheFolder();
    endRenameIfLeftTheFolder();
    // The grid is a view too: the bar must be settled on the way back to it, not only on the way in.
    if (showFolderGrid()) { renderFolderGrid(); updateBackButton(); return; }
    updateFilterNavVisibility();
    folderGrid.classList.add("hidden");
    queueList.classList.remove("hidden");

    const inFolder = state.activeFolder !== null;
    const isNew = state.activeWorkflow === "new";
    folderHeader.classList.toggle("hidden", !inFolder);
    const folderSummary = inFolder ? currentFolderSummary() : null;
    if (inFolder) {
      folderHeaderName.textContent = folderSummary
        ? (folderSummary.archive_title || folderSummary.folder_title || folderDisplayName(folderSummary))
        : "Ungrouped";
      renderFolderRename(folderSummary, isNew);
    }
    // Fix Split rebuilds from an intake scan batch and Delete Folder removes locally-held images
    // -- neither exists for documents that live on the site, so both are New Documents only.
    folderDelete.classList.toggle("hidden", !isNew);
    folderExport.classList.toggle("hidden", !isNew);
    // When the open folder still has undrafted documents, explain that they aren't listed yet --
    // this is the "when will new documents appear" gap: they appear here once drafted -- and let
    // the reviewer choose how many of THIS folder's pending documents to draft now (or all of
    // them), independent of the workflow-wide Draft Pending button/batch size.
    const awaiting = folderSummary ? folderSummary.awaiting_drafting_count : 0;
    folderDraftingNote.classList.toggle("hidden", !isNew || !inFolder || !awaiting);
    if (isNew && inFolder && awaiting) {
      folderDraftingNoteText.textContent =
        `${awaiting} more document${awaiting === 1 ? "" : "s"} in this folder ${awaiting === 1 ? "is" : "are"} `
        + `still awaiting drafting and won't appear here until transcribed.`;
      folderDraftCount.min = "1";
      folderDraftCount.max = String(awaiting);
      folderDraftCount.value = String(Math.min(awaiting, 5));
      folderDraftAllBtn.textContent = `Draft All (${awaiting})`;
    }

    const docs = filteredDocuments();
    updateWorkflowPill();
    updateCounts();
    queueSubtitle.textContent = `${docs.length} document${docs.length === 1 ? "" : "s"}`;
    queueList.innerHTML = "";
    queueEmpty.classList.toggle("hidden", docs.length > 0);

    // Fix Split is only offered inside a folder of the New Documents workflow -- it rebuilds from
    // the intake scan batch, which only uploaded documents have.
    el("folder-resplit").classList.toggle("hidden", !isNew || !inFolder);
    renderResplitBar();

    // A reviewer's folder is in two halves: what still needs them, and what they have already been
    // through.
    const divided = isAdmin() && !state.resplitMode;
    if (!divided) {
      docs.forEach((doc) => queueList.appendChild(queueRow(doc, docs)));
    } else {
      const reviewed = docs.filter(isReviewed);
      docs.filter((doc) => !isReviewed(doc))
          .forEach((doc) => queueList.appendChild(queueRow(doc, docs)));
      queueList.appendChild(reviewDivider(reviewed.length, docs.length));
      reviewed.forEach((doc) => queueList.appendChild(queueRow(doc, docs)));
    }
    updateBackButton();   // entering/leaving a folder changes where Back goes
  }

  filterNav.addEventListener("click", async (e) => {
    const btn = e.target.closest(".nav-item");
    if (!btn) return;

    // A document can be open (detail view) when one of these is clicked -- previously this just
    // silently updated the hidden queue list behind it, so the buttons looked unresponsive.
    const leavingDetail = !detailView.classList.contains("hidden");
    if (leavingDetail && !(await confirmLeaveDocument())) return;
    // Same idea for an in-progress split review -- there's no unsaved-edit tracking there (same as
    // the split screen's own "Cancel" button), so just navigate away like Cancel would.
    const leavingSplitReview = !splitReviewView.classList.contains("hidden");

    state.filter = btn.dataset.filter;
    [...filterNav.children].forEach((c) => c.classList.toggle("active", c === btn));

    if (leavingDetail || leavingSplitReview) {
      showQueue(); // also re-fetches, in case anything changed while the document was open
    } else {
      renderQueue();
    }
  });

  folderFilterNav?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-folder-filter]");
    if (!btn) return;
    state.folderFilter = btn.dataset.folderFilter;
    [...folderFilterNav.children].forEach((c) => c.classList.toggle("active", c === btn));
    renderQueue();
  });

  // ---------- New Documents folder browser ----------

  function leaveFolder() {
    state.activeFolder = null;
    state.filter = "all";
    [...filterNav.children].forEach((c) => c.classList.toggle("active", c.dataset.filter === "all"));
    renderQueue();
  }
  folderBack.addEventListener("click", leaveFolder);

  // ---------- renaming a folder after its split is confirmed ----------

  const folderRenameButton = el("folder-rename");
  const folderRenameEdit = el("folder-rename-edit");
  const folderRenameInput = el("folder-rename-input");
  const folderRenameHint = el("folder-rename-hint");

  // Editors only: the name is half of what every file is stored under, so it is not a label a
  // reviewer can change.
  function canRenameFolder(isNew) {
    return isNew && document.body.classList.contains("role-admin");
  }

  function renderFolderRename(folderSummary, isNew) {
    const allowed = canRenameFolder(isNew) && !!folderSummary;
    if (!allowed) closeFolderRename();
    const editing = allowed && state.renamingFolder;
    // Only the row below the header is toggled.
    folderRenameButton.classList.toggle("hidden", !allowed);
    folderRenameButton.classList.toggle("is-open", editing);
    folderRenameEdit.classList.toggle("hidden", !editing);
    if (editing) updateRenameHint();
  }

  function closeFolderRename() {
    state.renamingFolder = false;
    state.renameFolder = null;
  }

  // The editor belongs to the folder it was opened in.
  function endRenameIfLeftTheFolder() {
    if (state.renamingFolder && state.activeFolder !== state.renameFolder) {
      closeFolderRename();
      folderRenameEdit.classList.add("hidden");
      folderRenameButton.classList.remove("is-open");
    }
  }

  function updateRenameHint() {
    const title = folderTitleFor(currentFolderSummary()?.folder_display_id, folderRenameInput.value);
    folderRenameHint.textContent = title ? `Saved as ${title}` : "";
  }

  folderRenameButton.addEventListener("click", () => {
    if (state.renamingFolder) { closeFolderRename(); renderQueue(); return; }
    const folder = currentFolderSummary();
    state.renamingFolder = true;
    state.renameFolder = state.activeFolder;
    folderRenameInput.value = (folder && folder.folder_name) || "";
    renderFolderRename(folder, state.activeWorkflow === "new");
    folderRenameInput.focus();
    folderRenameInput.select();
  });

  el("folder-rename-cancel").addEventListener("click", () => {
    closeFolderRename();
    renderQueue();
  });

  folderRenameInput.addEventListener("input", updateRenameHint);
  folderRenameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveFolderName(folderRenameInput.value);
    if (event.key === "Escape") { closeFolderRename(); renderQueue(); }
  });

  el("folder-rename-save").addEventListener("click", () => saveFolderName(folderRenameInput.value));
  // Blank is what the server reads as "back to the name it was uploaded under".
  el("folder-rename-default").addEventListener("click", () => saveFolderName(""));

  async function saveFolderName(name) {
    const folderId = state.activeFolder;
    if (!folderId || state.savingFolderName) return;
    // Saving moves every one of the folder's files, so it is seconds rather than instant.
    state.savingFolderName = true;
    folderRenameHint.textContent = "Renaming the folder and moving its files…";
    ["folder-rename-save", "folder-rename-default", "folder-rename-cancel"]
      .forEach((id) => { el(id).disabled = true; });
    try {
      const res = await fetch(`/api/new/folders/${encodeURIComponent(folderId)}/name`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_name: name }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        toast(detail.detail || "Could not rename this folder", true);
        return;
      }
      const result = await res.json();
      closeFolderRename();
      toast(`Folder renamed to ${result.folder_title}`);
      await loadQueue();
    } catch (err) {
      toast("Could not rename this folder", true);
    } finally {
      state.savingFolderName = false;
      ["folder-rename-save", "folder-rename-default", "folder-rename-cancel"]
        .forEach((id) => { el(id).disabled = false; });
    }
  }

  // ---------- fixing a split after drafting ----------

  function renderResplitBar() {
    const bar = el("resplit-bar");
    bar.classList.toggle("hidden", !state.resplitMode);
    el("folder-resplit").textContent = state.resplitMode ? "Exit Fix Split" : "Fix Split";
    if (!state.resplitMode) return;
    const n = state.resplitSelection.size;
    el("resplit-bar-count").textContent = n === 0
      ? "Select the documents whose boundaries are wrong."
      : `${n} document${n === 1 ? "" : "s"} selected.`;
    // Merging needs at least two documents to join; re-cutting works on any selection.
    el("resplit-merge").disabled = n < 2;
    el("resplit-start").disabled = n === 0;
    el("resplit-bar-hint").textContent = n >= 2
      ? "Merge keeps their transcriptions and only re-reads the metadata. Re-cutting replaces both."
      : n === 1
        ? "Re-cutting this document replaces its transcription, edits and review status."
        : "Pick two or more that are really one document to merge them, or one document to cut it up.";
  }

  function exitResplitMode() {
    state.resplitMode = false;
    state.resplitFolder = null;
    state.resplitSelection = new Set();
    renderQueue();
  }

  // Fix Split belongs to the folder it was started in: leaving the folder ends it, rather than
  // carrying the banner across the rest of the site until somebody presses Cancel.
  function endResplitIfLeftTheFolder() {
    if (state.resplitMode && state.activeFolder !== state.resplitFolder) {
      state.resplitMode = false;
      state.resplitFolder = null;
      state.resplitSelection = new Set();
      // The folder grid returns before renderResplitBar is reached, which is how the banner came
      // to sit over the rest of the site -- so hide it here rather than relying on that call.
      renderResplitBar();
    }
  }

  el("folder-resplit").addEventListener("click", () => {
    if (state.resplitMode) { exitResplitMode(); return; }
    state.resplitMode = true;
    state.resplitFolder = state.activeFolder;
    state.resplitSelection = new Set();
    renderQueue();
  });
  el("resplit-cancel").addEventListener("click", exitResplitMode);

  // Merging: the cheap correction.
  el("resplit-merge").addEventListener("click", async () => {
    const ids = [...state.resplitSelection];
    const btn = el("resplit-merge");
    btn.disabled = true;
    try {
      const res = await fetch(`/api/new/merge?document_ids=${encodeURIComponent(ids.join(","))}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast(`Can't merge these: ${body.detail || res.statusText}`, true);
        return;
      }
      const plan = await res.json();
      if (!plan.all_drafted) {
        toast("Merging keeps the existing transcriptions, so every document has to be drafted first.", true);
        return;
      }
      const renumberCount = Object.keys(plan.renumber || {}).length;
      const lines = [
        `Merge ${plan.display_ids.join(", ")} into one ${plan.page_count}-page document, ${plan.target_display_id}?`,
        "",
        "Their transcriptions are kept and joined in scan order. The metadata is re-read from the",
        "joined text, and the merged document goes back to unreviewed so it can be read as one",
        "document.",
      ];
      if (renumberCount) {
        lines.push("", `${renumberCount} later document(s) in this folder will be renumbered to close the gap.`);
      }
      if (!confirm(lines.join("\n"))) return;

      const apply = await fetch("/api/new/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: ids }),
      });
      if (await handledBusy(apply)) return;
      if (!apply.ok) {
        const body = await apply.json().catch(() => ({}));
        toast(`Merge failed: ${body.detail || apply.statusText}`, true);
        return;
      }
      const result = await apply.json();
      exitResplitMode();
      const renamed = Object.keys(result.renumbered || {}).length;
      toast(
        `Merged into ${result.display_id || result.document_id} (${result.page_count} pages).`
        + (renamed ? ` ${renamed} document(s) renumbered.` : "")
      );
      await loadQueue();
    } finally {
      btn.disabled = false;
      renderResplitBar();
    }
  });

  el("resplit-start").addEventListener("click", async () => {
    const ids = [...state.resplitSelection];
    const btn = el("resplit-start");
    btn.disabled = true;
    try {
      const res = await fetch(`/api/new/resplit?document_ids=${encodeURIComponent(ids.join(","))}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast(`Can't fix these together: ${body.detail || res.statusText}`, true);
        return;
      }
      const payload = await res.json();
      // Reuse the split-review screen wholesale -- same viewer, same boundary controls -- by
      // shaping the re-split payload like a batch.
      exitResplitMode();
      openSplitReview({
        batch_id: payload.batch_id,
        folder_id: payload.folder_id,
        folder_display_id: payload.folder_display_id,
        folder_name: "",
        image_count: payload.pages.length,
        split_proposal_cursor: payload.pages.length, // already grouped; nothing left to propose
        proposal_error: null,
        suggested_document_ids: payload.suggested_document_ids,
        resplit_document_ids: payload.document_ids,
        pages: payload.pages.map((p) => ({
          sequence_index: p.sequence_index,
          original_filename: p.original_filename,
          image_url: p.image_url,
          proposed_new_document: p.starts_new_document,
          is_duplicate_scan: false,
        })),
      });
      setSplitConfirmStatus(
        `Fixing the split across ${ids.length} document(s): ${ids.join(", ")}. `
        + "Re-cut the boundaries below. When you confirm, these documents are rebuilt and re-drafted — "
        + "their current transcriptions, edits and review status are replaced.",
        true
      );
    } finally {
      btn.disabled = false;
    }
  });

  // A folder's display title: FolderID_Group, and just the id when nobody named it.
  function folderTitleFor(folderId, folderName) {
    const id = (folderId || "").trim();
    let group = (folderName || "").replace(/[\/:*?"<>|]+/g, "-").trim().replace(/[. ]+$/, "");
    if (!group) return id.slice(0, 120);
    if (id && (group === id || group.startsWith(id + "_") || group.startsWith(id + " "))) {
      return group.slice(0, 120);
    }
    if (!id) return group.slice(0, 120);
    return (id + "_" + group).slice(0, 120);
  }

  function currentFolderSummary() {
    return state.folders.find((f) => f.folder_id === state.activeFolder) || null;
  }

  function startFolderExport(folder) {
    if (!folder) return;
    window.__folderExports__?.start({workflow: state.activeWorkflow, folder_id: folder.folder_id,
                                     folder_display_id: folder.folder_display_id});
  }
  folderExport.addEventListener("click", () => startFolderExport(currentFolderSummary()));
  window.addEventListener("folder-export-completed", () => loadQueue());
  // Deletes every document in the currently-open folder, then returns to the folder grid (the
  // folder itself has no row of its own -- once every document is gone, it just stops appearing).
  folderDelete.addEventListener("click", async () => {
    const folder = currentFolderSummary();
    if (!folder) return;
    await deleteFolder(folder);
  });

  async function deleteFolder(folder) {
    const name = folderDisplayName(folder);
    const ctx = await confirmDelete(
      "Delete folder?",
      `This permanently deletes all ${folder.document_count} document${folder.document_count === 1 ? "" : "s"} `
        + `in "${name}", including their images. This cannot be undone.`
    );
    if (!ctx) return;
    // The two workflows group folders differently -- a DocID prefix for New Documents, the live
    // so they have separate endpoints and the wrong one
    // would match nothing.
    const endpoint = `/api/new/folders${tabQuery()}`;
    const res = await fetch(
      `${endpoint}?folder_id=${encodeURIComponent(folder.folder_id)}&${deletionParams(ctx)}`,
      { method: "DELETE" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      toast(`Delete failed: ${body.detail || res.statusText}`);
      return;
    }
    toast(`Deleted folder "${name}"`);
    if (state.activeFolder === folder.folder_id) state.activeFolder = null; // back to the folder grid
    loadQueue();
  }

  // ---------- delete a single document (either workflow) ----------

  async function deleteDocumentById(documentId) {
    const ctx = await confirmDelete("Delete document?", `This permanently deletes ${documentId} and its images. This cannot be undone.`);
    if (!ctx) return false;
    const res = await fetch(
      `${apiBase()}/documents/${encodeURIComponent(documentId)}?${deletionParams(ctx)}`,
      { method: "DELETE" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      toast(`Delete failed: ${body.detail || res.statusText}`);
      return false;
    }
    toast(`Deleted ${documentId}`);
    return true;
  }

  // Delete clicked from a queue-item row's trash icon -- stays on the queue, just refreshes it.
  async function deleteDocumentFromQueue(documentId) {
    if (await deleteDocumentById(documentId)) loadQueue();
  }

  // Delete clicked from inside the open document's detail view -- nothing left to show, so return
  // to the queue (also re-fetches, same as the regular "back to queue" path).
  btnDeleteDocument.addEventListener("click", async () => {
    if (!state.currentDoc) return;
    if (await deleteDocumentById(state.currentDoc.document_id)) {
      state.dirty = false; // the document is gone -- no unsaved-changes prompt should fire on the way out
      showQueue();
    }
  });

  // ---------- folder-scoped drafting ----------

  // How many documents are still pending in a folder, or null if that can't be determined right
  // now.
  async function folderPendingCount(folderId) {
    try {
      const res = await fetch(`/api/new/pending-count?folder_id=${encodeURIComponent(folderId)}`);
      if (!res.ok) return null;
      return (await res.json()).pending;
    } catch {
      return null;
    }
  }

  // `count` omitted drafts every pending document in the folder ("Draft All"); given, drafts that
  // many (in DocID order).
  function folderDraftKey(folderId) {
    return folderId || "__ungrouped__";
  }

  function syncFolderDraftControls() {
    const running = state.activeFolder !== null
      && state.folderDraftRuns.has(folderDraftKey(state.activeFolder));
    folderDraftCount.disabled = running;
    folderDraftCountBtn.disabled = running;
    folderDraftAllBtn.disabled = running;
  }

  async function runFolderDraft(count) {
    const folderId = state.activeFolder;
    if (folderId === null) return;
    const label = folderId || "Ungrouped";
    const runKey = folderDraftKey(folderId);
    if (state.folderDraftRuns.has(runKey)) {
      toast(`${label} is already drafting`, true);
      return;
    }

    state.folderDraftRuns.add(runKey);
    syncFolderDraftControls();
    // No pending poll: the folder's own undrafted total is not what this run is drafting.
    const stopProgress = startProgressIndicator(showPipelineStatus, `Drafting ${label}`);

    const startedPending = await folderPendingCount(folderId);
    const target = count != null ? count : (startedPending ?? 0);
    let drafted = 0;
    const countdown = () => stopProgress.setLabel(
      `${label} — drafting, ${target - drafted} document${target - drafted === 1 ? "" : "s"} remaining`);
    countdown();

    try {
      while (drafted < target) {
        const params = new URLSearchParams({ folder_id: folderId });
        params.set("count", String(target - drafted));
        // What the reviewer asked for, so the activity bar counts the run down rather than a chunk.
        params.set("total", String(target));
        params.set("done", String(drafted));

        const res = await fetch(`/api/new/folders/draft?${params.toString()}`, { method: "POST" });
        if (await handledBusy(res)) return;
        if (res.status === 404) break;  // nothing left pending -- the loop is simply done
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          showPipelineStatus(`Drafting failed: ${body.detail || res.statusText}`, true);
          return;
        }
        const body = await res.json();
        drafted += body.drafted;

        if (body.terminal) {
          // The account, not the folder: no key, a bad key, or no credits.
          showPipelineStatus(
            `Stopped after drafting ${drafted} document(s) in ${label}. ${body.terminal}`, true
          );
          return;
        }
        if (body.error) {
          showPipelineStatus(`Drafting failed: ${body.error}`, true);
          return;
        }
        // Neither drafted nor errored: the $ cap, or nothing left.
        if (body.drafted === 0) break;

        countdown();
        loadQueue();
      }

      showPipelineStatus(
        drafted > 0
          ? `Drafted ${drafted} document(s) in ${label}.`
          : "Nothing left to draft (or the $ budget cap was reached)."
      );
      loadQueue();
    } catch (err) {
      // A transport failure, not a drafting failure.
      const left = await folderPendingCount(folderId);
      const done = left == null ? drafted : Math.max((startedPending ?? left) - left, drafted);
      showPipelineStatus(
        `Connection lost after drafting ${done} document(s) in ${label}`
        + (left == null ? "" : ` (${left} still to do)`)
        + ". Nothing was lost — click Draft again to carry on.",
        true
      );
      loadQueue();
    } finally {
      stopProgress();
      state.folderDraftRuns.delete(runKey);
      syncFolderDraftControls();
    }
  }

  folderDraftCountBtn.addEventListener("click", () => {
    const count = parseInt(folderDraftCount.value, 10);
    if (!Number.isInteger(count) || count < 1) { toast("Enter a number of documents to draft"); return; }
    runFolderDraft(count);
  });
  folderDraftAllBtn.addEventListener("click", () => runFolderDraft(null));

  // ---------- pipeline actions ----------

  function showPipelineStatus(msg, isError) {
    pipelineStatus.textContent = msg;
    pipelineStatus.classList.remove("hidden");
    pipelineStatus.classList.toggle("pipeline-status-error", !!isError);
  }

  // Drafting a folder, or retrying a large split proposal, takes long enough that a static button
  // label is indistinguishable from a hung request.
  const PROGRESS_GRACE_MS = 400;
  // Once it is up it stays up this long, so work that lands just after the grace period does not
  // replace the flash with a shorter one.
  const PROGRESS_MIN_VISIBLE_MS = 600;

  // `yieldsToActivityBars` is true for the shared pipeline strip, which renderActivityBars hides
  // once the bars are up. A caller writing somewhere else -- the split view has its own line --
  // passes false, or its clock stops the moment the server registers a job and never moves again.
  function startProgressIndicator(setStatus, label, pollUrl, pendingNoun,
                                  yieldsToActivityBars = true) {
    const startedAt = Date.now();
    // The ticker rewrites the line every second, so a caller with more to say sets its label.
    let said = label;
    let pending = null;
    let shown = false;
    let shownAt = 0;

    function render() {
      // The activity bars narrate the same work, with more in them, and renderActivityBars hides
      // the shared strip whenever they are up -- but only that one.
      if (!shown || (yieldsToActivityBars && activeJobs.length)) return;
      const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
      const pendingNote = pending === null ? "" : ` -- ${pending} ${pendingNoun || "document(s)"} still pending`;
      setStatus(`${said}... (${elapsedSeconds}s elapsed${pendingNote})`);
    }

    const graceTimer = setTimeout(() => {
      shown = true;
      shownAt = Date.now();
      render();
    }, PROGRESS_GRACE_MS);
    const tickTimer = setInterval(render, 1000);
    const pollTimer = pollUrl
      ? setInterval(async () => {
          try {
            const res = await fetch(pollUrl);
            if (res.ok) {
              pending = (await res.json()).pending;
              render();
            }
          } catch {
            // A failed poll just skips this update -- the elapsed-time ticker alone still proves
            // the request is alive, and a transient polling hiccup isn't worth surfacing here.
          }
        }, 3000)
      : null;

    // Returns how much longer the strip owes the screen: 0 when it was never shown, so a caller
    // that hides on success hides at once for work that finished inside the grace period.
    let stopped = false;
    const stop = () => {
      clearTimeout(graceTimer);
      clearInterval(tickTimer);
      if (pollTimer) clearInterval(pollTimer);
      if (stopped || !shown) { stopped = true; return 0; }
      stopped = true;
      return Math.max(0, PROGRESS_MIN_VISIBLE_MS - (Date.now() - shownAt));
    };
    stop.setLabel = (next) => { said = next; render(); };
    return stop;
  }

  /* Hides the status strip once it has been up long enough not to read as a flash. */
  function hidePipelineStatusIn(milliseconds) {
    if (!milliseconds) { pipelineStatus.classList.add("hidden"); return; }
    setTimeout(() => pipelineStatus.classList.add("hidden"), milliseconds);
  }

  async function runPipelineAction(button, label, request, onSuccess) {
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = `${label}...`;
    try {
      const res = await request();
      if (await handledBusy(res)) return;
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showPipelineStatus(`${label} failed: ${body.detail || res.statusText}`, true);
        return;
      }
      const body = await res.json();
      onSuccess(body);
    } catch (err) {
      showPipelineStatus(`${label} failed: ${err}`, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  // A large split confirm only auto-drafts one batch (the Settings menu's configured draft batch
  // size) at a time -- this is how a reviewer continues drafting whatever's still stuck at
  // "split_confirmed".
  btnDraftPending.addEventListener("click", async () => {
    btnDraftPending.disabled = true;
    const originalText = btnDraftPending.textContent;
    const stopProgress = startProgressIndicator(showPipelineStatus, "Drafting", `/api/new/pending-count${tabQuery()}`);
    try {
      const res = await fetch("/api/new/draft-pending", { method: "POST" });
      if (await handledBusy(res)) return;
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showPipelineStatus(`Drafting failed: ${body.detail || res.statusText}`, true);
        return;
      }
      const body = await res.json();
      // transcribe_new_document isolates per-document failures -- draft-pending "succeeding" with
      // drafted=0 was previously indistinguishable between "nothing was pending" and "everything
      // pending just failed again" (e.g. still no OpenRouter credits).
      if (body.error) {
        showPipelineStatus(`Drafting failed: ${body.error}`, true);
      } else {
        showPipelineStatus(
          body.drafted > 0
            ? `Drafted ${body.drafted} more document(s). Click again if more are still pending.`
            : "Nothing left to draft (or the $ budget cap was reached)."
        );
      }
      loadQueue();
    } catch (err) {
      showPipelineStatus(`Drafting failed: ${err}`, true);
    } finally {
      stopProgress();
      btnDraftPending.disabled = false;
      btnDraftPending.textContent = originalText;
    }
  });

  // ---------- the tab strip ----------

  // Drawn from /api/tabs, ahead of Team, which workspace.js appends last; a reviewer is given
  // only the tabs that are open to them.
  async function loadTabs() {
    const res = await fetch("/api/tabs");
    state.tabs = res.ok ? (await res.json()).tabs : [];
    window.__tabs__ = state.tabs.map((t) => t.slug);
    const wanted = window.__route__?.routeOf?.(location.hash)?.tab;
    state.activeTab = state.tabs.find((t) => t.slug === wanted)
      || state.tabs.find((t) => t.id === state.activeTab?.id) || state.tabs[0] || null;
    renderTabStrip();
  }

  function renderTabStrip() {
    workflowTabs.querySelectorAll(".workflow-tab[data-tab]").forEach((b) => b.remove());
    const team = workflowTabs.querySelector("#tab-team");
    for (const tab of state.tabs) {
      const btn = document.createElement("button");
      btn.className = "workflow-tab" + (tab.id === state.activeTab?.id ? " active" : "");
      btn.dataset.workflow = "new";
      btn.dataset.tab = String(tab.id);
      btn.dataset.slug = tab.slug;
      btn.textContent = tab.label;
      btn.setAttribute("aria-label", tab.label);
      workflowTabs.insertBefore(btn, team);
    }
  }
  // Settings -> Tabs redraws the strip after a change.
  window.__tabsReload__ = loadTabs;

  workflowTabs.addEventListener("click", (e) => { state.switching = onWorkflowTab(e); });

  async function onWorkflowTab(e) {
    const btn = e.target.closest(".workflow-tab");
    // Team shares the visual tab strip but owns no tab of folders. Leave it to workspace.js.
    if (!btn?.dataset.tab) return;
    const tab = state.tabs.find((t) => String(t.id) === btn.dataset.tab);
    // The Team view hides the queue without changing the tab, so returning to the same tab must
    // still restore the selected button.
    if (!tab || tab.id === state.activeTab?.id) {
      [...workflowTabs.children].forEach((c) => c.classList.toggle("active", c === btn));
      // Team hands the screen back in its own listener, after this one.
      setTimeout(noteRoute, 0);
      return;
    }

    const leavingDetail = !detailView.classList.contains("hidden");
    if (leavingDetail && !(await confirmLeaveDocument())) return;

    state.activeTab = tab;
    state.activeFolder = null; // every tab lands on its folder grid
    [...workflowTabs.children].forEach((c) => c.classList.toggle("active", c === btn));

    newPipelineActions.classList.remove("hidden");
    pipelineStatus.classList.add("hidden");

    state.filter = "all";
    // A term typed against one workflow's folders would otherwise carry over and empty the other
    // one, with nothing on screen saying why.
    state.folderSearch = "";
    if (folderSearch) folderSearch.value = "";
    [...filterNav.children].forEach((c) => c.classList.toggle("active", c.dataset.filter === "all"));
    showQueue();
    await loadQueue();
  }

  // ---------- "Add New Document": upload + split review ----------

  let splitState = null; // { batchId, pages: [{sequence_index, original_filename, image_url, startsNew}], groupNames: {} }


  btnUploadScans.addEventListener("click", () => uploadScansInput.click());

  // Replacing a folder destroys finished work, so it asks for the folder's own id to be typed
  // rather than for a click -- the same bar the delete dialog sets, for the same reason.
  function confirmReplaceFolder(folderId, detail) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay replace-overlay";
      overlay.innerHTML = `
        <div class="modal replace-modal">
          <h3>${escapeHtml(folderId)} is already in the archive</h3>
          <p class="replace-detail">${escapeHtml(detail)}</p>
          <label class="replace-field">
            <span>Type <strong>${escapeHtml(folderId)}</strong> to replace it</span>
            <input type="text" id="replace-confirm" autocomplete="off" spellcheck="false" />
          </label>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" data-replace="cancel">Cancel</button>
            <button type="button" class="btn btn-danger" data-replace="go" disabled>Replace folder</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const input = overlay.querySelector("#replace-confirm");
      const go = overlay.querySelector('[data-replace="go"]');
      const close = (value) => { overlay.remove(); resolve(value); };
      input.addEventListener("input", () => {
        go.disabled = input.value.trim().toUpperCase() !== folderId.toUpperCase();
      });
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay || e.target.closest('[data-replace="cancel"]')) return close(false);
        if (e.target.closest('[data-replace="go"]') && !go.disabled) return close(true);
      });
      input.focus();
    });
  }

  uploadScansInput.addEventListener("change", async () => {
    const files = [...uploadScansInput.files];
    uploadScansInput.value = ""; // allow re-selecting the same folder again later
    if (!files.length) return;

    // webkitdirectory doesn't guarantee any particular order across browsers/OSes -- sort by the
    // relative path (which carries the original scan filenames) so upload order matches scan
    // order.
    files.sort((a, b) =>
      (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, undefined, { numeric: true })
    );

    // The physical folder itself is usually named with its Folder ID as a prefix (e.g. Windows
    // folders "E2241_Equipment & Supplies" or "D8026 1880 Document File") -- pull that code
    // straight off the picked directory name so the reviewer never has to type it for a normally-
    // named folder.
    const topFolderName = (files[0].webkitRelativePath || "").split("/")[0];
    const detectedFolderId = (topFolderName.match(/^([A-Za-z]+\d+)/) || [])[1] || "";

    const formData = new FormData();
    formData.append("folder_display_id", detectedFolderId);
    if (state.activeTab) formData.append("tab_id", state.activeTab.id);
    // The full directory name (e.g. "E2241_Equipment & Supplies") is kept for the folder-browser
    // label; only sent when it's more than the bare code, so a folder named just "E2241" doesn't
    // store a redundant name.
    if (topFolderName && topFolderName !== detectedFolderId) {
      formData.append("folder_name", topFolderName);
    }
    files.forEach((f) => formData.append("files", f, f.name));

    showPipelineStatus(`Uploading ${files.length} image(s)...`);
    let res;
    try {
      res = await fetch("/api/new/batches", { method: "POST", body: formData });
    } catch (err) {
      showPipelineStatus(`Upload failed: ${err}`, true);
      return;
    }
    // The folder already exists.
    if (res.status === 409 && res.headers.get("x-folder-exists")) {
      const body = await res.json().catch(() => ({}));
      const go = await confirmReplaceFolder(res.headers.get("x-folder-exists"), body.detail || "");
      if (!go) {
        showPipelineStatus("Upload cancelled — the folder already in the archive is untouched.");
        return;
      }
      formData.append("replace", "true");
      showPipelineStatus(`Replacing ${detectedFolderId}...`);
      try {
        res = await fetch("/api/new/batches", { method: "POST", body: formData });
      } catch (err) {
        showPipelineStatus(`Upload failed: ${err}`, true);
        return;
      }
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showPipelineStatus(`Upload failed: ${body.detail || res.statusText}`, true);
      return;
    }
    const batch = await res.json();
    state.batchInFlight = batch.batch_id;
    // Upload is ingest-only (fast) -- the batch already exists in "Awaiting split confirmation"
    // from this point on, even if the reviewer switches tabs before split proposal (the next, much
    // slower step) ever starts or finishes.
    await runInitialSplitProposal(batch.batch_id);
  });

  // Runs the first split-proposal attempt for a freshly-uploaded batch (POST .../retry-proposal is
  // the same resumable call the split-review screen's "Retry Split Proposal" button uses --
  // there's nothing "first-attempt-specific" about it, run_propose_split just starts from cursor
  // 0).
  async function runInitialSplitProposal(batchId) {
    const stopProgress = startProgressIndicator(
      showPipelineStatus, "Proposing split", `/api/new/batches/${encodeURIComponent(batchId)}/progress`, "page(s)"
    );
    try {
      const res = await fetch(`/api/new/batches/${encodeURIComponent(batchId)}/retry-proposal`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showPipelineStatus(`Split proposal failed: ${body.detail || res.statusText}`, true);
        return;
      }
      const batch = await res.json();
      hidePipelineStatusIn(stopProgress());
      openSplitReview(batch);
    } catch (err) {
      showPipelineStatus(`Split proposal failed: ${err}`, true);
    } finally {
      stopProgress();
      // Whatever happened, this browser is done setting it up: if it did not get as far as the
      // split screen, it really is waiting for someone and belongs in the panel.
      state.batchInFlight = null;
      loadPendingBatches();
    }
  }

  function openSplitReview(batch) {
    // suggested_document_ids (display ids from the tab's scheme -- see docid.py) is one entry per
    // group in the initial proposed split, in order; a deterministic suggestion, not an AI guess,
    // and fully editable like everything else here.
    const pages = batch.pages.map((p) => ({
      ...p, startsNew: p.proposed_new_document, isDuplicate: p.is_duplicate_scan, keepDuplicate: false,
    }));
    if (pages.length) pages[0].startsNew = true;
    const firstSeqs = pages.filter((p) => p.startsNew).map((p) => p.sequence_index);
    const groupNames = {};
    (batch.suggested_document_ids || []).forEach((id, i) => {
      if (firstSeqs[i] !== undefined) groupNames[firstSeqs[i]] = id;
    });
    splitState = {
      batchId: batch.batch_id,
      folderId: batch.folder_id || "",
      folderDisplayId: batch.folder_display_id || "",
      folderName: batch.folder_name || "",
      // keepDuplicate defaults to false -- a page the model flagged as a duplicate scan (see
      // split_prompt.md) is excluded from its group's sequence_indices by default
      // (computeSplitGroups), both here and in the confirmed document/export; the reviewer can
      // override per page if the AI got it wrong.
      pages,
      groupNames,
      // Stable group identities in the reviewer's chosen order. Pages remain in physical scan
      // order; confirmation assigns positional DocIDs to these identities.
      groupOrder: firstSeqs,
      mergeTargets: {},
      mergeSelection: new Set(),
      // Viewer position and progress.
      cursor: 0,
      pageCursor: 0,
      approved: new Set(),
      // The first-page sequence_index of every proposed document the reviewer has removed.
      dropped: new Set(),
      // Names the reviewer typed themselves, which renumbering after a removal leaves alone.
      manualNames: new Set(),
      fixing: false,
      // Set when the screen is opened to fix an already-drafted split rather than to confirm a
      // fresh batch -- changes the confirm call, the wording, and nothing else.
      resplitDocumentIds: batch.resplit_document_ids || null,
    };
    queueView.classList.add("hidden");
    detailView.classList.add("hidden");
    navControls.classList.add("hidden");
    splitReviewView.classList.remove("hidden");
    syncAnnouncements();
    updateBackButton();
    // batch.proposal_error must be shown here, on the now-visible split-review screen, not on
    // #pipeline-status -- that element lives inside #queue-view, which was just hidden above, so a
    // status set on it before this point is invisible to the reviewer (confirmed live: an
    // OpenRouter failure, e.g. no credits, left every page defaulting to "one big document" with
    // no visible warning at all, even though the error message was in fact being set -- just on a
    // hidden element).
    const pagesExamined = batch.split_proposal_cursor || 0;
    const totalPages = batch.image_count || splitState.pages.length;
    const notFullyProposed = pagesExamined < totalPages;
    if (batch.proposal_error || notFullyProposed) {
      // A large batch is proposed across multiple windowed model calls, each persisted as soon as
      // it finishes (see split.py's run_propose_split) -- a later window failing (or the reviewer
      // simply navigating away) doesn't discard earlier windows' real proposals.
      const proposedGroupCount = splitState.pages.filter((p) => p.startsNew).length;
      const progressNote = proposedGroupCount > 1
        ? `${proposedGroupCount} document(s) were proposed so far -- review the rest below, or `
          + "click Retry Split Proposal to automatically continue where it left off."
        : "No boundaries have been proposed yet -- every page currently shows as one document "
          + "below; click Retry Split Proposal to propose them, or mark up the boundaries by hand.";
      const remaining = totalPages - pagesExamined;
      const remainingNote = notFullyProposed
        ? ` (${remaining} page${remaining === 1 ? "" : "s"} still to examine.)`
        : "";
      const headline = batch.proposal_error
        ? `Automatic split proposal failed: ${batch.proposal_error}`
        : "Split proposal hasn't finished yet -- it may still be running, or the browser was closed before it completed.";
      setSplitConfirmStatus(`${headline}${remainingNote} ${progressNote}`, true);
      el("split-retry-proposal").classList.remove("hidden");
    } else {
      el("split-confirm-status").classList.add("hidden"); // clear any status left from a prior attempt
      el("split-retry-proposal").classList.add("hidden");
    }
    el("split-folder-id-input").value = splitState.folderDisplayId;
    el("split-folder-name-input").value = splitState.folderName;
    renderSplitReview();
  }

  // Wired once: the controls are static markup, so re-binding per render would stack listeners.
  function wireSplitViewer() {
    el("split-page-prev").addEventListener("click", () => { splitState.pageCursor -= 1; renderSplitReview(); });
    el("split-page-next").addEventListener("click", () => { splitState.pageCursor += 1; renderSplitReview(); });
    el("split-page-zoom").addEventListener("click", () => openImagePreview(el("split-viewer-img").src));
    el("split-viewer-img").addEventListener("click", () => openImagePreview(el("split-viewer-img").src));

    el("split-doc-prev").addEventListener("click", () => {
      splitState.cursor -= 1; splitState.pageCursor = 0; splitState.fixing = false; renderSplitReview();
    });
    el("split-doc-next").addEventListener("click", () => {
      splitState.cursor += 1; splitState.pageCursor = 0; splitState.fixing = false; renderSplitReview();
    });

    el("split-looks-right").addEventListener("click", () => {
      const groups = splitGroups();
      const group = groups[splitState.cursor];
      if (!group) return;
      splitState.approved.add(group.firstSeq);
      splitState.fixing = false;
      // Advance to the next unconfirmed document rather than simply the next one, so a reviewer
      // who jumped around isn't walked back through work they've already done.
      const nextUnconfirmed = groups.findIndex((g, i) => i > splitState.cursor && !splitState.approved.has(g.firstSeq));
      if (nextUnconfirmed !== -1) splitState.cursor = nextUnconfirmed;
      else if (splitState.cursor < groups.length - 1) splitState.cursor += 1;
      splitState.pageCursor = 0;
      renderSplitReview();
    });

    el("split-needs-fixing").addEventListener("click", () => {
      splitState.fixing = !splitState.fixing;
      const group = splitGroups()[splitState.cursor];
      if (group) splitState.approved.delete(group.firstSeq);
      renderSplitReview();
    });

    el("split-doc-name").addEventListener("input", (e) => {
      const group = splitGroups()[splitState.cursor];
      if (group) {
        splitState.groupNames[group.firstSeq] = e.target.value;
        splitState.manualNames.add(group.firstSeq);
      }
      e.target.classList.toggle("input-error", !e.target.value.trim());
    });

    el("split-doc-drop").addEventListener("click", async () => {
      const group = splitGroups()[splitState.cursor];
      if (!group) return;
      if (splitState.dropped.has(group.firstSeq)) {
        splitState.dropped.delete(group.firstSeq);
      } else {
        const kept = splitGroups().filter((g) => !splitState.dropped.has(g.firstSeq)).length;
        if (kept <= 1) {
          setSplitConfirmStatus("A batch has to become at least one document. Delete the whole "
                                + "batch instead if none of these scans should be kept.", true);
          return;
        }
        splitState.dropped.add(group.firstSeq);
        splitState.approved.delete(group.firstSeq);
        splitState.manualNames.delete(group.firstSeq);
      }
      await assignPositionalSplitNames();
      renderSplitReview();
    });

    el("split-merge-selected").addEventListener("click", async () => {
      await mergeProposedGroups([...splitState.mergeSelection]);
    });

    // Arrow keys page through the document being looked at -- the split screen is a reading task,
    // and reaching for the mouse for every page of a 90-scan folder is the friction being removed.
    document.addEventListener("keydown", (e) => {
      if (splitReviewView.classList.contains("hidden")) return;
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key === "ArrowLeft" && !el("split-page-prev").disabled) {
        splitState.pageCursor -= 1; renderSplitReview();
      } else if (e.key === "ArrowRight" && !el("split-page-next").disabled) {
        splitState.pageCursor += 1; renderSplitReview();
      }
    });
  }

  // ---------- split review: one proposed document at a time ----------

  // The reviewer judges a *document*, not a page -- once per document instead of once per page
  // turns a 91-scan folder from 91 checkbox decisions into ~30 confirmations.
  function scanSplitGroups() {
    const baseGroups = [];
    splitState.pages.forEach((p, i) => {
      if (p.startsNew || baseGroups.length === 0) {
        baseGroups.push({ firstSeq: p.sequence_index, memberFirstSeqs: [p.sequence_index], pages: [] });
      }
      baseGroups[baseGroups.length - 1].pages.push({ ...p, batchIndex: i });
    });
    const merged = new Map();
    const order = [];
    baseGroups.forEach((group) => {
      const target = splitState.mergeTargets?.[group.firstSeq] ?? group.firstSeq;
      if (!merged.has(target)) {
        merged.set(target, { firstSeq: target, memberFirstSeqs: [], pages: [] });
        order.push(target);
      }
      const logical = merged.get(target);
      logical.memberFirstSeqs.push(group.firstSeq);
      logical.pages.push(...group.pages);
    });
    return order.map((firstSeq) => merged.get(firstSeq));
  }

  function moveSplitOrder(order, draggedFirstSeq, targetFirstSeq) {
    const moved = [...order];
    const fromIndex = moved.indexOf(draggedFirstSeq);
    const toIndex = moved.indexOf(targetFirstSeq);
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return moved;
    moved.splice(fromIndex, 1);
    moved.splice(toIndex, 0, draggedFirstSeq);
    return moved;
  }

  function splitGroups() {
    const scanned = scanSplitGroups();
    const byFirstSeq = new Map(scanned.map((group) => [group.firstSeq, group]));
    const keptOrder = (splitState.groupOrder || []).filter((seq) => byFirstSeq.has(seq));
    scanned.forEach((group) => {
      if (keptOrder.includes(group.firstSeq)) return;
      const scanIndex = scanned.findIndex((candidate) => candidate.firstSeq === group.firstSeq);
      const previous = scanned[scanIndex - 1];
      const after = previous ? keptOrder.indexOf(previous.firstSeq) : -1;
      if (after >= 0) keptOrder.splice(after + 1, 0, group.firstSeq);
      else keptOrder.push(group.firstSeq);
    });
    splitState.groupOrder = keptOrder;
    return keptOrder.map((seq) => byFirstSeq.get(seq));
  }

  // Approval is remembered against a group's first page rather than its position, so approving
  // document 3 then changing a boundary in document 1 doesn't silently mark the wrong one done.
  function pruneSplitApprovals(groups) {
    const firstSeqs = new Set(
      groups.filter((group) => !splitState.dropped.has(group.firstSeq)).map((g) => g.firstSeq));
    splitState.approved = new Set([...splitState.approved].filter((seq) => firstSeqs.has(seq)));
    splitState.mergeSelection = new Set(
      [...splitState.mergeSelection].filter((seq) => firstSeqs.has(seq)));
  }

  async function mergeProposedGroups(firstSeqs) {
    const wanted = new Set(firstSeqs);
    const selected = splitGroups().filter(
      (group) => wanted.has(group.firstSeq) && !splitState.dropped.has(group.firstSeq));
    if (selected.length < 2) return;
    const target = selected[0].firstSeq;
    selected.forEach((group) => {
      group.memberFirstSeqs.forEach((member) => { splitState.mergeTargets[member] = target; });
      if (group.firstSeq !== target) {
        delete splitState.groupNames[group.firstSeq];
        splitState.manualNames.delete(group.firstSeq);
        splitState.approved.delete(group.firstSeq);
      }
    });
    splitState.groupOrder = splitState.groupOrder.filter(
      (seq) => seq === target || !wanted.has(seq));
    splitState.approved.delete(target);
    splitState.mergeSelection.clear();
    splitState.cursor = Math.max(0, splitState.groupOrder.indexOf(target));
    splitState.pageCursor = 0;
    await assignPositionalSplitNames();
    renderSplitReview();
  }

  function renderSplitReview() {
    const pages = splitState.pages;
    if (pages.length) pages[0].startsNew = true; // the sequence's first page always starts a document

    const groups = splitGroups();
    pruneSplitApprovals(groups);
    if (splitState.cursor >= groups.length) splitState.cursor = groups.length - 1;
    if (splitState.cursor < 0) splitState.cursor = 0;
    const group = groups[splitState.cursor];
    if (!group) return;
    if (splitState.pageCursor >= group.pages.length) splitState.pageCursor = group.pages.length - 1;
    if (splitState.pageCursor < 0) splitState.pageCursor = 0;

    const removed = splitState.dropped.has(group.firstSeq);
    const keptGroups = groups.filter((g) => !splitState.dropped.has(g.firstSeq));
    const approvedCount = splitState.approved.size;
    el("split-doc-progress").textContent =
      `Document ${splitState.cursor + 1} of ${groups.length}`
      + (splitState.dropped.size ? ` · ${splitState.dropped.size} removed` : "");
    const kept = group.pages.filter((p) => !(p.isDuplicate && !p.keepDuplicate)).length;
    el("split-doc-pagecount").textContent = removed
      ? `${kept} page${kept === 1 ? "" : "s"} — removed, these scans will not become a document`
      : `${kept} page${kept === 1 ? "" : "s"} in this document · ${approvedCount} of `
        + `${keptGroups.length} confirmed`;

    renderSplitDocPanel(groups, approvedCount);

    const nameInput = el("split-doc-name");
    nameInput.value = splitState.groupNames[group.firstSeq] || "";
    nameInput.disabled = removed;
    nameInput.classList.toggle("input-error", !removed && !nameInput.value.trim());
    splitReviewView.classList.toggle("doc-removed", removed);
    el("split-doc-drop").textContent = removed ? "Restore document" : "Remove document";
    el("split-looks-right").disabled = removed;
    el("split-needs-fixing").disabled = removed;

    const page = group.pages[splitState.pageCursor];
    el("split-viewer-img").src = pageImageUrl(page.image_url, "view");
    el("split-page-label").textContent = `Page ${splitState.pageCursor + 1} of ${group.pages.length}`;
    el("split-page-filename").textContent = page.original_filename || "";
    el("split-page-prev").disabled = splitState.pageCursor === 0;
    el("split-page-next").disabled = splitState.pageCursor >= group.pages.length - 1;

    const excluded = page.isDuplicate && !page.keepDuplicate;
    const banner = el("split-duplicate-banner");
    banner.classList.toggle("hidden", !page.isDuplicate);
    if (page.isDuplicate) {
      banner.textContent = excluded
        ? "Looks like a duplicate scan of the previous page — it will be left out of this document."
        : "Flagged as a duplicate scan, but kept in this document.";
    }

    el("split-doc-prev").disabled = splitState.cursor === 0;
    el("split-doc-next").disabled = splitState.cursor >= groups.length - 1;
    const approved = splitState.approved.has(group.firstSeq);
    const looksRight = el("split-looks-right");
    looksRight.textContent = approved ? "Confirmed ✓" : "Looks right →";
    looksRight.classList.toggle("is-approved", approved);

    renderSplitFixPanel(group, groups);
  }

  // Every proposed document at once.
  function renderSplitDocPanel(groups, approvedCount) {
    const keptCount = groups.filter((g) => !splitState.dropped.has(g.firstSeq)).length;
    el("split-panel-progress").textContent = `${approvedCount}/${keptCount} confirmed`;
    el("split-merge-selected").disabled = splitState.mergeSelection.size < 2;
    const list = el("split-doc-list");

    // A rebuild is needed only when the number of documents changes -- which happens when the
    // reviewer moves a boundary -- or when drag/drop changes their order -- not when they merely
    // look at one.
    const orderSignature = groups.map((group) => group.firstSeq).join(",");
    let rebuildNeeded = false;
    if (list.children.length !== groups.length) rebuildNeeded = true;
    if (list.dataset.order !== orderSignature) rebuildNeeded = true;
    if (rebuildNeeded) {
      list.innerHTML = groups.map((g, i) => `
        <div class="split-doc-row" role="button" tabindex="0" data-doc="${i}"
             data-first-seq="${g.firstSeq}" draggable="true">
          <span class="split-doc-drag" aria-hidden="true">⋮⋮</span>
          <input class="split-doc-merge-pick" type="checkbox" aria-label="Select for merge" />
          <img class="split-doc-row-thumb" src="${pageImageUrl(g.pages[0].image_url, "thumb")}"
               alt="" loading="lazy" />
          <span class="split-doc-row-text">
            <span class="split-doc-row-title"></span>
            <span class="split-doc-row-sub"></span>
          </span>
          <span class="split-doc-row-mark"></span>
        </div>`).join("");
      list.dataset.order = orderSignature;

      list.querySelectorAll(".split-doc-row").forEach((row) => {
        row.addEventListener("click", () => {
          splitState.cursor = Number(row.dataset.doc);
          splitState.pageCursor = 0;
          splitState.fixing = false;
          renderSplitReview();
        });
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            row.click();
          }
        });
        const pick = row.querySelector(".split-doc-merge-pick");
        pick.addEventListener("click", (event) => event.stopPropagation());
        pick.addEventListener("change", () => {
          const firstSeq = Number(row.dataset.firstSeq);
          if (pick.checked) splitState.mergeSelection.add(firstSeq);
          else splitState.mergeSelection.delete(firstSeq);
          renderSplitDocPanel(splitGroups(), splitState.approved.size);
        });
        row.addEventListener("dragstart", (event) => {
          splitState.draggedFirstSeq = Number(row.dataset.firstSeq);
          row.classList.add("is-dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", row.dataset.firstSeq);
        });
        row.addEventListener("dragend", () => {
          splitState.draggedFirstSeq = null;
          row.classList.remove("is-dragging");
          list.querySelectorAll(".split-doc-row").forEach((item) => item.classList.remove("is-drop-target"));
        });
        row.addEventListener("dragover", (event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
          row.classList.add("is-drop-target");
        });
        row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
        row.addEventListener("drop", async (event) => {
          event.preventDefault();
          const from = splitState.draggedFirstSeq ?? Number(event.dataTransfer.getData("text/plain"));
          const to = Number(row.dataset.firstSeq);
          row.classList.remove("is-drop-target");
          if (from === to) return;
          splitState.groupOrder = moveSplitOrder(splitState.groupOrder, from, to);
          splitState.cursor = splitState.groupOrder.indexOf(from);
          splitState.pageCursor = 0;
          await assignPositionalSplitNames();
          renderSplitReview();
        });
      });
    }

    groups.forEach((g, i) => {
      const row = list.children[i];
      if (!row) return;
      const approved = splitState.approved.has(g.firstSeq);
      const removed = splitState.dropped.has(g.firstSeq);
      const kept = g.pages.filter((p) => !(p.isDuplicate && !p.keepDuplicate)).length;
      const name = (splitState.groupNames[g.firstSeq] || "").trim();
      row.classList.toggle("is-current", i === splitState.cursor);
      row.classList.toggle("is-approved", approved && !removed);
      row.classList.toggle("is-removed", removed);
      const pick = row.querySelector(".split-doc-merge-pick");
      pick.checked = splitState.mergeSelection.has(g.firstSeq);
      pick.disabled = removed;
      row.querySelector(".split-doc-row-title").textContent = name || `Document ${i + 1}`;
      row.querySelector(".split-doc-row-sub").textContent = removed
        ? "removed" : `${kept} page${kept === 1 ? "" : "s"}`;
      row.querySelector(".split-doc-row-mark").textContent =
        removed ? "✕" : (approved ? "✓" : "");
      // A boundary moved earlier in the sequence changes which page begins this document.
      const thumb = pageImageUrl(g.pages[0].image_url, "thumb");
      const img = row.querySelector(".split-doc-row-thumb");
      if (!img.getAttribute("src").endsWith(thumb)) img.setAttribute("src", thumb);
    });
    // Keep the current row in view when the cursor moves by button or keyboard rather than by a
    // click in the panel -- otherwise it scrolls out of sight and the panel stops being a map.
    const current = el("split-doc-list").querySelector(".split-doc-row.is-current");
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  // Shown only after "Needs fixing" -- the two things that can actually be wrong with a proposed
  // document: it should end earlier (a page inside it starts the next document), or it shouldn't
  // end here at all (it continues into the next document).
  function renderSplitFixPanel(group, groups) {
    const panel = el("split-fix-panel");
    if (!splitState.fixing) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");

    const current = groups[splitState.cursor];
    const nextListed = groups[splitState.cursor + 1];
    const canMerge = Boolean(nextListed && !splitState.dropped.has(nextListed.firstSeq));
    panel.innerHTML = `
      <p class="split-fix-lede">Where does this document actually end?</p>
      <div class="split-fix-strip">
        ${group.pages.map((p, i) => `
          <div class="split-fix-page ${i === splitState.pageCursor ? "is-current" : ""}">
            <img src="${pageImageUrl(p.image_url, 'thumb')}" alt="Page ${i + 1}" data-goto="${i}" loading="lazy" />
            <span class="split-fix-page-label">Page ${i + 1}</span>
            ${i === 0
              ? '<span class="split-fix-note">starts here</span>'
              : `<button class="btn btn-secondary btn-small" data-splitat="${p.sequence_index}">Split here ↑</button>`}
            ${p.isDuplicate ? `
              <label class="split-toggle split-fix-dup">
                <input type="checkbox" class="split-keep-duplicate-input" data-seq="${p.sequence_index}"
                       ${p.keepDuplicate ? "checked" : ""} /> keep duplicate
              </label>` : ""}
          </div>
        `).join("")}
      </div>
      <div class="split-fix-actions">
        ${canMerge ? '<button id="split-merge-next" class="btn btn-secondary">Merge with next listed document</button>' : ""}
        <button id="split-fix-done" class="btn btn-ghost">Done fixing</button>
      </div>
    `;

    panel.querySelectorAll("[data-goto]").forEach((img) => {
      img.addEventListener("click", () => { splitState.pageCursor = Number(img.dataset.goto); renderSplitReview(); });
    });
    panel.querySelectorAll("[data-splitat]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const seq = Number(btn.dataset.splitat);
        splitState.pages.find((p) => p.sequence_index === seq).startsNew = true;
        // The reviewer is looking at the first half; the new document lands right after it.
        splitState.approved.delete(splitState.pages.find((p) => p.sequence_index === seq).sequence_index);
        splitState.pageCursor = 0;
        renderSplitReview();
        fillMissingGroupSuggestions(); // the cut just created a new, still-unnamed document
      });
    });
    panel.querySelectorAll(".split-keep-duplicate-input").forEach((input) => {
      input.addEventListener("change", (e) => {
        const seq = Number(e.target.dataset.seq);
        splitState.pages.find((p) => p.sequence_index === seq).keepDuplicate = e.target.checked;
        renderSplitReview();
      });
    });
    const merge = el("split-merge-next");
    if (merge) {
      merge.addEventListener("click", async () => {
        await mergeProposedGroups([current.firstSeq, nextListed.firstSeq]);
      });
    }
    el("split-fix-done").addEventListener("click", () => { splitState.fixing = false; renderSplitReview(); });
  }

  function computeSplitGroups() {
    // This is deliberately derived from the same logical groups the panel renders. A previous
    // implementation rebuilt the payload from physical scan boundaries, which made a drag or a
    // non-adjacent merge look successful but disappear at confirmation time.
    return splitGroups().map((group) => ({
      display_id: (splitState.groupNames[group.firstSeq] || "").trim(),
      firstSeq: group.firstSeq,
      // Pages within a merged document remain in physical scan order, even when the cards were
      // selected or arranged in a different order.
      sequence_indices: group.pages
        .filter((page) => !(page.isDuplicate && !page.keepDuplicate))
        .map((page) => page.sequence_index),
    }));
  }

  // What confirm actually sends: the groups the reviewer has not removed.
  function keptSplitGroups() {
    return computeSplitGroups()
      .filter((g) => !splitState.dropped.has(g.firstSeq))
      .map(({ display_id, sequence_indices }) => ({ display_id, sequence_indices }));
  }

  // After a removal the remaining documents take the ids that freed up, so the folder's DocIDs run
  // without a hole -- the same rule a delete follows after drafting.
  async function renumberSplitNames() {
    if (!splitState.folderId) return;
    const groups = computeSplitGroups();
    groups.forEach((g, i) => {
      if (!splitState.manualNames.has(g.firstSeq)) splitState.groupNames[g.firstSeq] = "";
    });
    await fillMissingGroupSuggestions();
  }

  // The model's initial proposal only gets one suggested name per group it found (see openSplitReview);
  // if the reviewer's own toggling adds a boundary the model missed, the new group it creates has no
  // name yet.
  async function fillMissingGroupSuggestions() {
    if (!splitState.folderId) return;
    const groups = computeSplitGroups();
    const missingIndices = groups
      .map((g, i) => (g.display_id || splitState.dropped.has(g.firstSeq) ? -1 : i))
      .filter((i) => i >= 0);
    if (!missingIndices.length) return;

    const usedIds = groups.map((g) => g.display_id).filter(Boolean);
    let body;
    try {
      const res = await fetch("/api/new/suggest-doc-ids", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_id: splitState.folderId, count: missingIndices.length, exclude: usedIds }),
      });
      if (!res.ok) return; // best-effort -- the reviewer can still type a name manually
      body = await res.json();
    } catch {
      return;
    }

    missingIndices.forEach((groupIndex, j) => {
      const suggested = body.suggested_document_ids[j];
      const group = groups[groupIndex];
      if (suggested && group) splitState.groupNames[group.firstSeq] = suggested;
    });
    renderSplitReview();
  }

  async function assignPositionalSplitNames() {
    if (!splitState.folderId) return;
    const groups = splitGroups();
    const kept = groups.filter((group) => !splitState.dropped.has(group.firstSeq));
    try {
      const res = await fetch("/api/new/suggest-doc-ids", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_id: splitState.folderId, count: kept.length, exclude: [] }),
      });
      if (!res.ok) return;
      const body = await res.json();
      kept.forEach((group, index) => {
        if (body.suggested_document_ids[index]) {
          splitState.groupNames[group.firstSeq] = body.suggested_document_ids[index];
        }
      });
      splitState.manualNames.clear();
    } catch {
      // Reordering itself is local and remains usable; IDs can still be typed if lookup fails.
    }
  }

  // Lets a reviewer set (or fix) this batch's Folder ID -- and optionally its descriptive Folder
  // Name -- after upload -- e.g. the auto-detected id was left blank, or (see the folder-name
  // input's own history) the reviewer had to retype/correct the id here after the original upload
  // attempt never captured a name at all, which used to leave the folder permanently stuck showing
  // just its bare code on the folder-browser card with no way to fix it.
  el("split-folder-id-apply").addEventListener("click", async () => {
    const folderId = el("split-folder-id-input").value.trim();
    const folderName = el("split-folder-name-input").value.trim();
    if (!folderId || !splitState) return;
    const btn = el("split-folder-id-apply");
    btn.disabled = true;
    try {
      const res = await fetch(`/api/new/batches/${splitState.batchId}/folder-id`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_display_id: folderId, folder_name: folderName }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSplitConfirmStatus(`Couldn't save that Folder ID: ${body.detail || "try again"}.`, true);
        return;
      }
      const saved = await res.json();
      splitState.folderId = saved.folder_id || "";
      splitState.folderDisplayId = saved.folder_display_id || folderId;
      if (folderName) splitState.folderName = folderName;
      await fillMissingGroupSuggestions();
    } finally {
      btn.disabled = false;
    }
  });

  // Retries whatever part of the batch didn't get automatically proposed -- run_propose_split
  // resumes from where it left off server-side (split_proposal_cursor), so this never re-examines
  // or re-bills pages already proposed successfully, even across more than one retry.
  el("split-retry-proposal").addEventListener("click", async () => {
    const btn = el("split-retry-proposal");
    if (!splitState) return;
    btn.disabled = true;
    const stopProgress = startProgressIndicator(
      setSplitConfirmStatus, "Retrying split proposal",
      `/api/new/batches/${encodeURIComponent(splitState.batchId)}/progress`, "page(s)", false
    );
    try {
      const res = await fetch(`/api/new/batches/${splitState.batchId}/retry-proposal`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSplitConfirmStatus(`Retry failed: ${body.detail || res.statusText}`, true);
        return;
      }
      openSplitReview(await res.json());
    } catch (err) {
      setSplitConfirmStatus(`Retry failed: ${err}`, true);
    } finally {
      stopProgress();
      btn.disabled = false;
    }
  });

  el("split-back").addEventListener("click", () => {
    splitState = null;
    splitReviewView.classList.add("hidden");
    showQueue();
  });

  function setSplitConfirmStatus(msg, isError) {
    const statusEl = el("split-confirm-status");
    statusEl.textContent = msg;
    statusEl.classList.remove("hidden");
    statusEl.classList.toggle("pipeline-status-error", !!isError);
  }

  el("split-confirm").addEventListener("click", async () => {
    const all = computeSplitGroups();
    const groups = keptSplitGroups();
    const missingGroupIndices = all
      .map((g, i) => (g.display_id || splitState.dropped.has(g.firstSeq) ? -1 : i))
      .filter((i) => i >= 0);
    if (missingGroupIndices.length) {
      // Only one name field exists now (the document being viewed), so an unnamed document can be
      // off-screen entirely -- take the reviewer to the first one instead of highlighting fields
      // they can't see.
      setSplitConfirmStatus(
        missingGroupIndices.length === 1
          ? "1 document still needs an ID — jumped to it below."
          : `${missingGroupIndices.length} documents still need an ID — jumped to the first one below.`,
        true
      );
      splitState.cursor = missingGroupIndices[0];
      splitState.pageCursor = 0;
      renderSplitReview();
      el("split-doc-name").focus();
      return;
    }

    // Confirming while documents are still unreviewed is allowed (the boundaries may simply all be
    // right), but it shouldn't be silent -- this is the last point before real drafting spend.
    const unconfirmed = groups.length - splitState.approved.size;

    if (unconfirmed > 0 && !splitState.confirmedUnreviewed) {
      splitState.confirmedUnreviewed = true;
      setSplitConfirmStatus(
        `${unconfirmed} of ${groups.length} document(s) haven't been checked yet. `
        + "Click Confirm & Draft again to continue anyway, or step through them first.",
        true
      );
      return;
    }

    const btn = el("split-confirm");
    btn.disabled = true;
    const original = btn.textContent;
    // Confirming (fast) and drafting the first batch (slow -- real OpenRouter calls, often
    // Model drafting happens inside this request; the elapsed-time +
    // pending-count indicator is what actually proves it's still working, not stuck, for however
    // long that takes -- the button label alone couldn't tell those two apart.
    const isResplit = Boolean(splitState.resplitDocumentIds);
    const stopProgress = startProgressIndicator(
      setSplitConfirmStatus,
      isResplit ? "Rebuilding documents and drafting" : "Confirming split and drafting",
      "/api/new/pending-count", undefined, false
    );
    try {
      const res = isResplit
        ? await fetch("/api/new/resplit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ document_ids: splitState.resplitDocumentIds, groups }),
          })
        : await fetch(`/api/new/batches/${splitState.batchId}/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ groups }),
          });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSplitConfirmStatus(`${isResplit ? "Fix" : "Confirm"} failed: ${body.detail || res.statusText}`, true);
        return;
      }
      const body = await res.json();
      // The split confirm itself succeeded (documents were created either way) -- body.error means
      // only the auto-triggered draft attempt failed (isolated per document server-side, e.g.
      // OpenRouter out of credits).
      if (body.error) {
        toast(`Created ${body.created.length} document(s), but drafting failed: ${body.error}`, true);
      } else {
        toast(`Created ${body.created.length} document(s); drafting started (use Draft Pending if more than a few)`);
      }
      splitState = null;
      splitReviewView.classList.add("hidden");
      showQueue();
    } catch (err) {
      setSplitConfirmStatus(`Confirm failed: ${err}`, true);
    } finally {
      stopProgress();
      btn.disabled = false;
      btn.textContent = original;
    }
  });

  // ---------- navigation between queue and detail ----------

  function showQueue() {
    detailView.classList.add("hidden");
    splitReviewView.classList.add("hidden");
    queueView.classList.remove("hidden");
    syncAnnouncements();
    state.currentDoc = null;
    loadQueue();
    updateBackButton();   // still a target if we're inside a folder
  }

  // The button names its own destination rather than making you guess.
  function backTarget() {
    if (!splitReviewView.classList.contains("hidden")) {
      return { label: "← Back to queue", go: () => { splitState = null; showQueue(); } };
    }
    if (!detailView.classList.contains("hidden")) {
      const folder = state.activeWorkflow === "new" && state.activeFolder !== null;
      return { label: folder ? "← Back to folder" : "← Back to queue", go: showQueue, confirmLeave: true };
    }
    // Inside a folder's list the header already carries "← All folders" beside the folder's name;
    // a second control up here said the same thing, and was left behind when that one was used.
    return null;
  }

  // Called after every view change so the bar is present exactly when there's somewhere to go.
  function updateBackButton() {
    const target = backTarget();
    navControls.classList.toggle("hidden", !target);
    if (target) el("back-to-queue").textContent = target.label;
    // Prev/Next only mean anything while a document is open.
    el("prev-doc").classList.toggle("hidden", detailView.classList.contains("hidden"));
    el("next-doc").classList.toggle("hidden", detailView.classList.contains("hidden"));
    el("doc-position").classList.toggle("hidden", detailView.classList.contains("hidden"));
    noteRoute();
  }

  // ---------- the browser's Back and Forward (see history.js) ----------

  // Where the queue's side of the site is; null while Team or a split review is showing.
  function currentRoute() {
    if (queueView.style.display === "none" || !splitReviewView.classList.contains("hidden")) return null;
    const folder = state.activeFolder;
    if (!detailView.classList.contains("hidden") && state.currentDoc) {
      return { area: "doc", tab: state.activeTab?.slug, folder, doc: state.currentDoc.document_id };
    }
    return { area: "queue", tab: state.activeTab?.slug, folder, doc: null };
  }

  function noteRoute() {
    const route = currentRoute();
    if (!route || !window.__route__) return;
    // Prev/Next between documents of one folder is one stop, so Back returns to the folder list.
    const was = history.state?.route;
    const replace = route.area === "doc" && was?.area === "doc" && was.tab === route.tab
      && was.folder === route.folder;
    window.__route__.note(route, { replace });
  }

  async function restoreRoute(route) {
    const slug = route.tab || state.tabs[0]?.slug;
    const tab = workflowTabs.querySelector(`.workflow-tab[data-slug="${slug}"]`);
    if (!tab) throw new Error("no such tab");
    const away = queueView.style.display === "none";
    if (away || slug !== state.activeTab?.slug) {
      const before = state.activeTab?.id;
      // A real click, so Team hands the screen back exactly as it does for one.
      tab.click();
      await state.switching;
      if (slug !== state.activeTab?.slug) throw new Error("tab switch refused");
      // A change of tab loads its queue itself; coming back from Team to the same tab does not.
      if (state.activeTab?.id === before) await loadQueue();
    }
    if (!detailView.classList.contains("hidden") || !splitReviewView.classList.contains("hidden")) {
      splitState = null;
      showQueue();
    }
    const folder = route.folder ?? null;
    if (folder !== state.activeFolder) {
      if (folder === null) leaveFolder();
      else openFolder(folder);
    }
    if (route.doc) {
      const index = filteredDocuments().findIndex((d) => d.document_id === route.doc);
      if (index >= 0) state.currentIndex = index;
      await openDocument(route.doc);
      if (detailView.classList.contains("hidden") || state.currentDoc?.document_id !== route.doc)
        throw new Error("document did not open");
    }
    updateBackButton();
  }

  window.__route__?.owns("queue", {
    restore: restoreRoute,
    // An open document with unsaved edits asks first, as every other way out of it does.
    leave: async () => (!state.currentDoc || detailView.classList.contains("hidden")
      || queueView.style.display === "none" ? true : confirmLeaveDocument()),
  });

  el("back-to-queue").addEventListener("click", async () => {
    const target = backTarget();
    if (!target) return;
    if (target.confirmLeave && !(await confirmLeaveDocument())) return;
    target.go();
    updateBackButton();
  });

  async function openDocumentByIndex(index) {
    const docs = filteredDocuments();
    if (index < 0 || index >= docs.length) return;
    state.currentIndex = index;
    await openDocument(docs[index].document_id);
  }

  el("prev-doc").addEventListener("click", async () => {
    if (!(await confirmLeaveDocument())) return;
    openDocumentByIndex(state.currentIndex - 1);
  });
  el("next-doc").addEventListener("click", async () => {
    if (!(await confirmLeaveDocument())) return;
    openDocumentByIndex(state.currentIndex + 1);
  });

  async function openDocument(documentId) {
    // The last document asked for wins: a slower, earlier open must not land on top of it.
    const request = (state.openRequest = (state.openRequest || 0) + 1);
    state.openingId = documentId;
    // A redraft marks the fields it replaced; opening anything drops that mark, including the
    // reopen the redraft itself does before it marks them again.
    clearRedrafted();
    const res = await fetch(`${apiBase()}/documents/${encodeURIComponent(documentId)}`);
    if (request !== state.openRequest) return;
    if (!res.ok) {
      state.openingId = state.currentDoc?.document_id || null;
      toast("Could not load document");
      return;
    }
    const opened = await res.json();
    if (request !== state.openRequest) return;
    state.currentDoc = opened;
    state.currentPageIndex = 0;
    state.dirty = false;
    renderDetail();
    const redrafting = state.redraftingId === documentId;
    lockReviewFields(redrafting || !!opened.locked);
    showLocked(redrafting || !!opened.locked, !!opened.locked);

    queueView.classList.add("hidden");
    splitReviewView.classList.add("hidden");
    detailView.classList.remove("hidden");
    syncAnnouncements();
    updateBackButton();
  }

  function updateDocPosition() {
    const docs = filteredDocuments();
    el("doc-position").textContent = docs.length ? `${state.currentIndex + 1} / ${docs.length}` : "";
    el("prev-doc").disabled = state.currentIndex <= 0;
    el("next-doc").disabled = state.currentIndex >= docs.length - 1;
  }

  // ---------- detail rendering ----------

  // ---------- DocID rename ("Add New Document" workflow only) ----------

  function resetDocIdEditUI() {
    docId.classList.remove("hidden");
    btnEditDocid.classList.toggle("hidden", state.activeWorkflow !== "new");
    btnRedraft?.classList.toggle("hidden", state.activeWorkflow !== "new");
    docIdInput.classList.add("hidden");
    btnSaveDocid.classList.add("hidden");
    btnCancelDocid.classList.add("hidden");
  }

  btnEditDocid.addEventListener("click", () => {
    docIdInput.value = shownId(state.currentDoc);
    docId.classList.add("hidden");
    btnEditDocid.classList.add("hidden");
    docIdInput.classList.remove("hidden");
    btnSaveDocid.classList.remove("hidden");
    btnCancelDocid.classList.remove("hidden");
    docIdInput.focus();
    docIdInput.select();
  });

  btnCancelDocid.addEventListener("click", resetDocIdEditUI);

  docIdInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") btnSaveDocid.click();
    else if (e.key === "Escape") resetDocIdEditUI();
  });

  btnSaveDocid.addEventListener("click", async () => {
    const newId = docIdInput.value.trim();
    const oldId = state.currentDoc.document_id;
    if (!newId || newId === shownId(state.currentDoc)) {
      resetDocIdEditUI();
      return;
    }
    // Renaming replaces state.currentDoc wholesale (see below) -- flush or discard any other
    // unsaved edit first via the same modal every other navigation-away path uses, rather than
    // silently losing it.
    if (!(await confirmLeaveDocument())) return;

    btnSaveDocid.disabled = true;
    try {
      const res = await fetch(`/api/new/documents/${encodeURIComponent(oldId)}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_display_id: newId }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast(`Rename failed: ${body.detail || res.statusText}`);
        return;
      }
      // Keep the in-memory queue row's display id in sync, as saveReview patches its badge.
      const queued = state.documents.find((d) => d.document_id === oldId);
      if (queued) queued.display_id = newId;
      await openDocument(oldId);
      toast(`Renamed to ${newId}`);
    } finally {
      btnSaveDocid.disabled = false;
    }
  });

  function renderMetadataGrid(doc) {
    // The fields come from the registry of the prompt version that drafted this document.
    window.ReviewFields.render(doc.field_registry || [], doc.fields || {}, {
      grid: metadataGrid, blocks: fieldBlocks, title: doc.metadata.title,
      titleIsOverride: doc.metadata.title_is_override, onInput: markDirty,
    });
  }

  function renderDetail() {
    const doc = state.currentDoc;
    updateDocPosition();

    docTitle.textContent = doc.metadata.title || shownId(doc);
    docId.textContent = shownId(doc);
    // The Team script reads which document is open from here; it needs the internal id.
    docId.dataset.documentId = doc.document_id;
    resetDocIdEditUI();
    statusBadge.className = `badge ${badgeClass(doc.review.status)}`;
    statusBadge.textContent = badgeLabel(doc.review.status);
    syncActionButtons(false);

    renderMetadataGrid(doc);

    textEdit.value = doc.transcription.text;
    textPreview.innerHTML = renderHighlighted(doc.transcription.text);
    reviewNotes.value = doc.review.editors_notes || "";
    renderReviewedBy(doc);

    textPreview.classList.remove("hidden");
    textEdit.classList.add("hidden");
    el("toggle-edit").textContent = "Edit";

    renderPage();
  }

  function renderPage() {
    const doc = state.currentDoc;
    const pages = doc.pages;
    const idx = state.currentPageIndex;
    pageIndicator.textContent = `Page ${idx + 1}/${pages.length}`;
    el("page-prev").disabled = idx <= 0;
    el("page-next").disabled = idx >= pages.length - 1;
    if (pages.length) {
      pageImage.src = pageImageUrl(pages[idx].image_url, "view");
    }
    state.orientation = savedOrientation();
    resetZoom();
  }

  el("page-prev").addEventListener("click", () => {
    if (state.currentPageIndex > 0) {
      state.currentPageIndex -= 1;
      renderPage();
    }
  });
  el("page-next").addEventListener("click", () => {
    if (state.currentPageIndex < state.currentDoc.pages.length - 1) {
      state.currentPageIndex += 1;
      renderPage();
    }
  });

  // ---------- edit / preview toggle ----------

  el("toggle-edit").addEventListener("click", () => {
    const editing = !textEdit.classList.contains("hidden");
    if (editing) {
      // switching to preview: re-render highlights from the textarea's current value
      textPreview.innerHTML = renderHighlighted(textEdit.value);
      textPreview.classList.remove("hidden");
      textEdit.classList.add("hidden");
      el("toggle-edit").textContent = "Edit";
    } else {
      textPreview.classList.add("hidden");
      textEdit.classList.remove("hidden");
      textEdit.focus();
      el("toggle-edit").textContent = "Preview";
    }
  });

  [textEdit, reviewNotes].forEach((field) => field.addEventListener("input", markDirty));

  // ---------- indenting by hand ---------- Tab indents the paragraph the cursor is in, instead of
  // jumping to the marginal notes field.
  const INDENT_SPACES = "    ";
  // Anything a Tab could have put there: our own four, a shorter run left by an earlier outdent,
  // or a literal tab from a paste.
  const ONE_INDENT = new RegExp(`^(?: {1,${INDENT_SPACES.length}}|\\t)`);

  function shiftIndent(outdent) {
    const value = textEdit.value;
    const caret = textEdit.selectionStart;
    const selected = textEdit.selectionEnd > caret;
    const from = value.lastIndexOf("\n", caret - 1) + 1;
    // A selection dragged to the start of the next line should not indent that line as well -- the
    // reviewer selected the text above it, not it.
    let through = textEdit.selectionEnd;
    if (selected && value[through - 1] === "\n") through -= 1;
    let to = value.indexOf("\n", Math.max(through, caret));
    if (to < 0) to = value.length;

    const before = value.slice(from, to);
    const after = before
      .split("\n")
      .map((line) => (outdent ? line.replace(ONE_INDENT, "") : INDENT_SPACES + line))
      .join("\n");
    if (after === before) return;  // already flush, and Shift+Tab has nothing left to remove

    textEdit.setSelectionRange(from, to);
    // insertText rather than assigning .value, so this lands on the browser's own undo stack -- an
    // indent the reviewer cannot Ctrl+Z out of is worse than no shortcut at all.
    let inserted = false;
    try {
      inserted = document.execCommand("insertText", false, after);
    } catch (err) {
      inserted = false;
    }
    if (!inserted) textEdit.setRangeText(after, from, to, "end");

    if (selected) {
      textEdit.setSelectionRange(from, from + after.length);
    } else {
      // One line, so the whole change in length happened before the cursor.
      const at = Math.max(from, caret + (after.length - before.length));
      textEdit.setSelectionRange(at, at);
    }
    markDirty();
  }

  textEdit.addEventListener("keydown", (event) => {
    // Trapping Tab takes away the keyboard route out of the field, so Escape gives one back --
    // otherwise this editor becomes a place a keyboard-only reviewer cannot leave.
    if (event.key === "Escape") {
      textEdit.blur();
      return;
    }
    if (event.key !== "Tab" || event.ctrlKey || event.altKey || event.metaKey) return;
    event.preventDefault();
    shiftIndent(event.shiftKey);
  });

  // ---------- zoom / pan viewer ----------

  function applyTransform() {
    pageImage.style.transform = `translate(${state.zoom.x}px, ${state.zoom.y}px) scale(${state.zoom.scale})` +
      orientationTransform(state.orientation, pageImage.offsetWidth, pageImage.offsetHeight,
                           viewer.clientWidth, viewer.clientHeight);
    renderOrientationSave();
  }

  function resetZoom() {
    state.zoom = { scale: 1, x: 0, y: 0 };
    applyTransform();
  }

  // ---------- page orientation ----------

  // A clockwise rotation applied after an optional left-to-right mirror, as the export applies it.
  function turnOrientation(o, action) {
    const r = o.rotation, m = o.mirrored;
    if (action === "left") return { rotation: (r + 270) % 360, mirrored: m };
    if (action === "right") return { rotation: (r + 90) % 360, mirrored: m };
    // Mirroring the screen reverses the turn already made; top-to-bottom is that plus a half turn.
    if (action === "flip-h") return { rotation: (360 - r) % 360, mirrored: !m };
    if (action === "flip-v") return { rotation: (540 - r) % 360, mirrored: !m };
    return o;
  }

  // Turned about the image's centre, and a sideways page sized to fit the viewer as an upright one does.
  function orientationTransform(o, width, height, viewWidth, viewHeight) {
    if (!o || (!o.rotation && !o.mirrored)) return "";
    const fit = o.rotation % 180 && width && height ? Math.min(viewWidth / height, viewHeight / width) : 1;
    return ` translate(${width / 2}px, ${height / 2}px) rotate(${o.rotation}deg)` +
      ` scale(${(o.mirrored ? -1 : 1) * fit}, ${fit}) translate(${-width / 2}px, ${-height / 2}px)`;
  }

  function currentPage() {
    const pages = state.currentDoc?.pages || [];
    return pages[state.currentPageIndex] || null;
  }

  function savedOrientation() {
    const page = currentPage();
    return { rotation: page?.rotation || 0, mirrored: !!page?.mirrored };
  }

  function renderOrientationSave() {
    const saved = savedOrientation();
    const o = state.orientation || saved;
    const changed = o.rotation !== saved.rotation || o.mirrored !== saved.mirrored;
    el("orient-save").classList.toggle("hidden", !changed || !currentPage() || !!state.currentDoc?.locked);
  }

  document.querySelectorAll(".orient-controls [data-turn]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!currentPage()) return;
      state.orientation = turnOrientation(state.orientation || savedOrientation(), button.dataset.turn);
      applyTransform();
    });
  });

  el("orient-save").addEventListener("click", async () => {
    const page = currentPage();
    if (!page) return;
    const wanted = { ...state.orientation };
    const button = el("orient-save");
    button.disabled = true;
    try {
      const res = await fetch(page.image_url.replace(/\/image$/, "/orientation"), {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(wanted),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      Object.assign(page, wanted);
      toast("Orientation saved");
    } catch (err) {
      toast(`Orientation not saved: ${err.message}`, true);
    } finally {
      button.disabled = false;
      renderOrientationSave();
    }
  });

  // The turn is laid out from the image's own size, which is known only once it has loaded.
  pageImage.addEventListener("load", applyTransform);
  window.addEventListener("resize", () => { if (state.orientation) applyTransform(); });

  function zoomAt(anchorX, anchorY, factor) {
    const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, state.zoom.scale * factor));
    state.zoom.x = anchorX - ((anchorX - state.zoom.x) * newScale) / state.zoom.scale;
    state.zoom.y = anchorY - ((anchorY - state.zoom.y) * newScale) / state.zoom.scale;
    state.zoom.scale = newScale;
    applyTransform();
  }

  function zoomAtCenter(factor) {
    const rect = viewer.getBoundingClientRect();
    zoomAt(rect.width / 2, rect.height / 2, factor);
  }

  // Reset View also drops an unsaved turn, back to how the page was saved.
  el("zoom-reset").addEventListener("click", () => { state.orientation = savedOrientation(); resetZoom(); });
  el("zoom-in").addEventListener("click", () => zoomAtCenter(ZOOM_STEP));
  el("zoom-out").addEventListener("click", () => zoomAtCenter(1 / ZOOM_STEP));

  viewer.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = viewer.getBoundingClientRect();
    const factor = e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor);
  }, { passive: false });

  // Dragging is press-and-hold only: it starts on left-button mousedown, tracks the mouse while
  // the button stays down, and stops the instant the button is released (or the window/context
  // menu steals focus, so it can never get stuck "on").
  viewer.addEventListener("contextmenu", (e) => e.preventDefault());

  viewer.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    state.dragging = true;
    viewer.classList.add("dragging");
    state.dragStart = { x: e.clientX, y: e.clientY, panX: state.zoom.x, panY: state.zoom.y };
  });
  window.addEventListener("mousemove", (e) => {
    if (!state.dragging) return;
    state.zoom.x = state.dragStart.panX + (e.clientX - state.dragStart.x);
    state.zoom.y = state.dragStart.panY + (e.clientY - state.dragStart.y);
    applyTransform();
  });

  function stopDragging() {
    state.dragging = false;
    viewer.classList.remove("dragging");
  }
  window.addEventListener("mouseup", stopDragging);
  window.addEventListener("blur", stopDragging);

  // ---------- save actions ----------


  // Accept carries its own undo rather than a separate "Mark Unreviewed" button: the way to take
  // an acceptance back is the button that made it.
  function buttonLabel(button) {
    let label = button.querySelector(".btn-label");
    if (!label) {
      label = document.createElement("span");
      label.className = "btn-label";
      label.textContent = button.textContent.trim();
      button.textContent = "";
      button.appendChild(label);
    }
    return label;
  }

  // "Accept" and "Mark Unreviewed" are different widths, so swapping the text outright made the
  // button -- and every button beside it -- jump.
  function measureLabelWidth(button, text) {
    const ghost = button.cloneNode(true);
    ghost.removeAttribute("id");
    buttonLabel(ghost).textContent = text;
    ghost.style.cssText = "position:absolute;visibility:hidden;pointer-events:none;width:auto;";
    button.parentNode.appendChild(ghost);
    const width = ghost.getBoundingClientRect().width;
    ghost.remove();
    return width;
  }

  // Both of a button's labels share one width, so turning a verdict over moves nothing beside it.
  function reserveLabelWidth(button, texts) {
    const key = texts.join("|");
    if (button.dataset.reserved === key) return;
    const widest = Math.max(...texts.map((t) => measureLabelWidth(button, t)));
    if (!widest) {
      // Asked for before the view was laid out, so measured again once it is.
      if ((button.reserveTries = (button.reserveTries || 0) + 1) < 10) {
        requestAnimationFrame(() => reserveLabelWidth(button, texts));
      }
      return;
    }
    button.reserveTries = 0;
    button.dataset.reserved = key;
    button.style.minWidth = `${Math.ceil(widest)}px`;
  }

  function swapButtonLabel(button, text, on) {
    const label = buttonLabel(button);
    // Already showing it, or already on the way to it.
    if (label.textContent === text || button.swapTo === text) return;
    button.swapTo = text;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      label.textContent = text;
      button.classList.toggle("is-on", on);
      button.swapTo = null;
      return;
    }
    clearTimeout(button.swapTimer);
    button.classList.add("is-swapping");
    button.swapTimer = setTimeout(() => {
      label.textContent = text;
      // The fill changes with the label, so the button reads as one object turning over.
      button.classList.toggle("is-on", on);
      button.classList.remove("is-swapping");
      button.swapTo = null;
    }, SWAP_OUT_MS);
  }

  /* The viewer is a reviewer, not a reviewer. */
  function isAdmin() {
    return !document.body.classList.contains("role-reviewer");
  }

  /* Drafting one document again, for when the model returned nothing for it. */
  async function redraftThisDocument() {
    const doc = state.currentDoc;
    if (!doc) return;
    if (state.dirty && !(await confirmLeaveDocument())) return;
    const empty = !(doc.transcription && (doc.transcription.text || "").trim());
    if (!window.confirm(
        `Draft ${shownId(doc)} again?

`
        + (empty ? "This document has no transcription."
                 : "Its transcription, marginal notes and indexes will be replaced by the new draft;"
                   + " what they say now stays in History.")
        + " Title, date, author and the other metadata edits are kept.")) return;
    // Preserve edits already waiting for autosave before replacing the model-owned fields.
    if (state.dirty && !(await saveReview(doc.review.status, null, { quiet: true }))) return;
    // Locked while it runs: anything typed now would be saved by autosave, then replaced unseen.
    state.redraftingId = doc.document_id;
    lockReviewFields(true);
    showLocked(true, false);
    // Says what it is doing without changing width, so the bar around it stays put.
    btnRedraft.disabled = true;
    btnRedraft.classList.add("is-busy");
    reserveLabelWidth(btnRedraft, ["Redraft", "Drafting..."]);
    const was = buttonLabel(btnRedraft).textContent;
    setButtonLabel(btnRedraft, "Drafting...", false);
    try {
      const res = await fetch(`/api/new/documents/${encodeURIComponent(doc.document_id)}/redraft`,
                              { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { toast(body.detail || `Redraft failed: ${res.statusText}`, true); return; }
      // `drafted`, not `characters`: a failed redraft still has the old draft's text to count.
      if (!body.drafted) {
        toast(`Redraft of ${shownId(doc)} failed; its draft is unchanged — ${body.error}`, true);
        return;
      }
      toast(body.characters ? `${shownId(doc)} drafted again — ${body.characters} characters`
                            : "The model returned nothing again", !body.characters);
      // The reviewer may have moved on while the model worked; reopening would pull them back.
      if ((state.openingId || state.currentDoc?.document_id) !== doc.document_id) return;
      state.redraftingId = null;
      // By id: the list index can point elsewhere once the queue has been redrawn or filtered.
      await openDocument(doc.document_id);
      showRedrafted();
      window.__refreshHistory__?.();
    } catch (err) {
      toast(`Redraft failed: ${err}`, true);
    } finally {
      state.redraftingId = null;
      if (state.currentDoc?.document_id === doc.document_id) {
        lockReviewFields(!!state.currentDoc.locked);
        showLocked(!!state.currentDoc.locked, !!state.currentDoc.locked);
      }
      btnRedraft.classList.remove("is-busy");
      setButtonLabel(btnRedraft, was, false);
    }
  }

  /* An approved folder's document, for a reviewer: nothing to press, and a line saying why. */
  function showLocked(locked, explain = locked) {
    [el("btn-save-draft"), el("btn-accept"), btnRedraft].forEach((button) => {
      if (button) button.disabled = locked;
    });
    el("locked-note")?.classList.toggle("hidden", !explain);
  }

  /* Every field a save reads, so nothing can be typed into a document while it is being redrafted. */
  function lockReviewFields(locked) {
    const fields = [textEdit, reviewNotes, el("meta-input-title"), ...window.ReviewFields.controls()];
    fields.forEach((field) => {
      if (!field) return;
      if (field.tagName === "SELECT" || field.type === "checkbox") field.disabled = locked;
      else field.readOnly = locked;
    });
  }

  /* Marks the fields a redraft actually replaces (the text and the page fields), because a toast alone left the reviewer
     comparing the screen against their memory of the old draft to see whether anything happened. */
  let redraftMarkerListeners = null;

  function showRedrafted() {
    clearRedrafted();
    const listeners = new AbortController();
    redraftMarkerListeners = listeners;
    [textEdit.closest(".block"), ...window.ReviewFields.pageBlocks()].forEach((block) => {
      if (!block) return;
      block.classList.add("is-redrafted");
      // Cleared by the first keystroke in that field: from then on the reviewer knows, and the
      // mark would be describing their own words rather than the model's.
      block.addEventListener("input", () => {
        block.classList.remove("is-redrafted");
        if (!document.querySelector(".block.is-redrafted")) {
          el("redraft-stamp")?.classList.add("hidden");
          listeners.abort();
          if (redraftMarkerListeners === listeners) redraftMarkerListeners = null;
        }
      }, { once: true, signal: listeners.signal });
    });
    const stamp = el("redraft-stamp");
    if (!stamp) return;
    stamp.textContent = `Redrafted ${new Date().toLocaleTimeString()}`;
    stamp.classList.remove("hidden");
  }

  /* Marks nothing rather than the wrong thing: the fields below belong to whichever document is
     open now, and the previous one's redraft has nothing to say about them. */
  function clearRedrafted() {
    redraftMarkerListeners?.abort();
    redraftMarkerListeners = null;
    document.querySelectorAll(".block.is-redrafted")
      .forEach((block) => block.classList.remove("is-redrafted"));
    el("redraft-stamp")?.classList.add("hidden");
  }

  btnRedraft?.addEventListener("click", redraftThisDocument);

  /* The bottom of a document says who reviewed it, when a reviewer has. */
  function renderReviewedBy(doc) {
    // The name alone is not enough: a document can carry one from a verdict that has since been
    // taken back, and the detail view keeps its status under `review`, not on the row.
    const ruled = (doc.review && doc.review.status) === "accepted";
    const who = (ruled && doc.review.reviewed_by) || "";
    reviewedByLine.classList.toggle("hidden", !who);
    // Cleared rather than just hidden: a name left in the DOM is still read out, and is the next
    // thing to flash up if anything ever shows this line without setting it.
    reviewedByText.textContent = who ? `Reviewed by ${who}` : "";
  }

  // Opening a document is not a change of mind, so its buttons arrive already in the right state.
  function setButtonLabel(button, text, on) {
    clearTimeout(button.swapTimer);
    button.swapTo = null;
    button.classList.remove("is-swapping");
    buttonLabel(button).textContent = text;
    button.classList.toggle("is-on", on);
  }

  function syncActionButtons(animate = true) {
    const status = state.currentDoc?.review?.status || "unreviewed";
    // Both roles press a button called Accept, so the reviewer's says Reviewed instead.
    const accepted = isAdmin() ? "Mark Unreviewed" : "Mark Unaccepted";
    const apply = animate ? swapButtonLabel : setButtonLabel;
    reserveLabelWidth(el("btn-accept"), [accepted, isAdmin() ? "Mark as Reviewed" : "Accept"]);
    apply(el("btn-accept"), status === "accepted" ? accepted : (isAdmin() ? "Mark as Reviewed" : "Accept"),
          status === "accepted");
  }

  // `quiet` is autosave.js saving in the background: no toast, and no refetch, because the
  // reviewer is still typing into the fields a refetch would redraw.
  async function saveReview(status, triggerButton, { quiet = false } = {}) {
    // Autosave may already be writing when the reviewer makes an explicit decision. Never discard
    // that click: wait for the background write, then save the decision against the same document.
    // A second autosave can simply stand down because the edit generation will schedule another.
    if (state.saving) {
      if (quiet) return false;
      const documentId = state.currentDoc?.document_id;
      try { await state.savePromise; } catch { /* retry below and surface that result */ }
      if (!documentId || state.currentDoc?.document_id !== documentId) return false;
      return saveReview(status, triggerButton, { quiet });
    }
    const doc = state.currentDoc;
    const isNew = state.activeWorkflow === "new";
    const payload = {
      reviewed_text: textEdit.value,
      fields: window.ReviewFields.read(),
      review_status: status,
      editors_notes: reviewNotes.value,
    };
    if (isNew) {
      payload.title = el("meta-input-title").value;
      payload.draft_version = doc.draft_version || "";
    }

    // What this payload represents. Anything typed after this point is not in it.
    const generation = state.editGeneration || 0;

    // Only the pressed button shows it is busy; the others are left exactly as they were.
    state.saving = true;
    if (triggerButton) triggerButton.classList.add("is-busy");

    let res;
    try {
      state.savePromise = fetch(`${apiBase()}/documents/${encodeURIComponent(doc.document_id)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      res = await state.savePromise;
    } finally {
      state.saving = false;
      state.savePromise = null;
      if (triggerButton) triggerButton.classList.remove("is-busy");
    }

    if (!res.ok) {
      // The server's reason, where it gave one: "Save failed" alone left a locked folder a mystery.
      const detail = (await res.json().catch(() => ({}))).detail;
      toast(detail ? `Save failed: ${detail}` : "Save failed", true);
      return false;
    }
    const body = await res.json();
    window.__folderExports__?.prompt(body.completed_folder);
    const queued = state.documents.find((d) => d.document_id === doc.document_id);
    if (queued) {
      queued.review_status = status;
      queued.challenges = payload.fields.challenges || queued.challenges;
    }
    // The reviewer has moved on while this was in flight: redrawing now would paint this document's
    // status onto another one, or reopen this one over it.
    if ((state.openingId || state.currentDoc?.document_id) !== doc.document_id) return true;
    // Only if nothing was typed while this was in flight. Clearing it unconditionally lost those
    // words twice over: the next autosave saw nothing to do, and leaving the page did not warn.
    if ((state.editGeneration || 0) === generation) state.dirty = false;

    if (isNew && !quiet) {
      // Simplest way to reflect the freshly recomputed title (see build_document_title) after any
      // metadata edit -- a full refetch rather than hand-patching every field client-side.
      const pageIndex = state.currentPageIndex;
      await openDocument(doc.document_id);
      state.currentPageIndex = Math.min(pageIndex, state.currentDoc.pages.length - 1);
      renderPage();
    } else if (isNew) {
      // A quiet save must not redraw the screen: the reviewer's caret is in one of these fields.
      // The title the server recomputed is picked up by the next real save or reopen.
      doc.review.status = status;
      doc.transcription.text = payload.reviewed_text;
    } else {
      doc.review.status = status;
      doc.review.reviewer = payload.reviewer;
      doc.review.editors_notes = payload.editors_notes;
      doc.transcription.text = payload.reviewed_text;
      doc.fields = payload.fields;

      statusBadge.className = `badge ${badgeClass(status)}`;
      statusBadge.textContent = badgeLabel(status);
    }
    // Both paths: the new-documents branch above refetches the document, which redraws the badge
    // but not these, and patches state in place.
    syncActionButtons();
    // The reported bug: only the refetching path redrew this, so marking a document unreviewed
    // anywhere else left "Reviewed by ..." standing under a document nobody had reviewed.
    renderReviewedBy(state.currentDoc || doc);

    // The History panel is describing this very document, so an edit should appear in it now
    // rather than the next time the reviewer reopens it.
    window.__refreshHistory__?.();

    // An autosave says so in its own indicator; a toast every few seconds would be its own noise.
    if (quiet) return true;

    let message = `Saved (${badgeLabel(status)})`;
    if (body.error) {
      // The save itself succeeded -- only the auto-triggered next-batch transcribe failed
      // (isolated server-side per document).
      toast(`${message} — auto-transcribe failed: ${body.error}`, true);
      return true;
    }
    if (body.transcribed > 0) {
      message += ` — transcribed ${body.transcribed} more document(s)`;
    } else if (body.reason === "budget_exhausted") {
      message += " — $ budget cap reached, no more auto-transcribing";
    }
    toast(message);
    return true;
  }

  el("btn-save-draft").addEventListener("click", (e) => saveReview(state.currentDoc.review.status, e.target));
  // Each toggles against the status the document is actually in, read at click time rather than
  // captured when the handler was bound.
  el("btn-accept").addEventListener("click", (e) =>
    saveReview(state.currentDoc?.review?.status === "accepted" ? "unreviewed" : "accepted", e.target));

  // Closing the tab, reloading, or navigating by URL bypasses all the in-app confirmation above
  // entirely -- this is the browser's own last line of defense for that case.
  window.addEventListener("beforeunload", (e) => {
    if (!state.dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // ---------- boot ----------

  // ---------- what the pipeline is doing, shared across every reviewer ----------

  // Polled from every screen, not tracked per-view: the old status lived in whichever view started
  // the work, so switching tabs threw it away and other reviewers saw nothing.
  const ACTIVITY_POLL_IDLE_MS = 5000;
  const ACTIVITY_POLL_BUSY_MS = 2000;   // tighter while something is running, so elapsed time ticks
  let activityTimer = null;

  // Background work reports its failures here, because the request that started it has returned.
  // The first poll only learns what is already there, so a reload does not replay old failures.
  let problemsShown = null;

  function announceProblems(problems) {
    const firstPoll = problemsShown === null;
    problemsShown = problemsShown || new Set();
    problems.forEach((problem) => {
      if (problemsShown.has(problem.id)) return;
      problemsShown.add(problem.id);
      if (!firstPoll) toast(problem.message, true);
    });
  }

  async function pollActivity() {
    let body;
    try {
      // The status check is the point: fetch resolves on a 401 too, and the gate's error body
      // parses as JSON quite happily, so without this an expired session reads as "nothing
      // running" -- indistinguishable from a quiet pipeline.
      const res = await fetch("/api/activity");
      if (!res.ok) throw new Error(`activity -> ${res.status}`);
      body = await res.json();
    } catch {
      // A failed poll is not worth shouting about -- the next one is 5s away, and the banner going
      // quiet is better than a scary error over a transient blip.
      activityTimer = setTimeout(pollActivity, ACTIVITY_POLL_IDLE_MS);
      return;
    }
    announceProblems(body.problems || []);
    const running = body.active && body.active.length ? body.active[0] : null;
    const wasRunning = activityRunning;
    activityRunning = !!running;
    activeJobs = body.active || [];
    activeJobsReceivedAt = Date.now();
    renderActivityBars();
    if (!el("settings-panel-running").classList.contains("hidden")) renderGenerationLog();
    // A second-by-second clock, independent of the poll: the poll says what is running, this says
    // how long it has been running for.
    if (activeJobs.length && !activityTicker) activityTicker = setInterval(tickActivity, 1000);
    if (activityRunning !== wasRunning) loadPendingBatches();
    // No site-wide banner any more: what is running is said where the work is -- the folder's own
    // drafting note and the pipeline status line -- and in Settings > Running Now for the whole
    // picture.
    if (!running && wasRunning) {
      // Work just finished -- refresh whatever list is on screen so the results appear without the
      // reviewer having to know to reload.
      loadQueue();
    }
    activityTimer = setTimeout(pollActivity, running ? ACTIVITY_POLL_BUSY_MS : ACTIVITY_POLL_IDLE_MS);
  }

  // The scroll bar shows while the pointer is within reach of it, and nowhere else.
  const EDGE_PX = 28;
  function watchTheEdge(event) {
    document.querySelectorAll(".scroll-pane").forEach((pane) => {
      const box = pane.getBoundingClientRect();
      const near = event.clientX > box.right - EDGE_PX && event.clientX <= box.right
        && event.clientY >= box.top && event.clientY <= box.bottom;
      pane.classList.toggle("at-the-edge", near);
    });
  }
  document.addEventListener("pointermove", watchTheEdge);
  // A pointer that leaves the window never gets a last move event inside it.
  document.addEventListener("pointerleave", () => {
    document.querySelectorAll(".scroll-pane.at-the-edge")
      .forEach((pane) => pane.classList.remove("at-the-edge"));
  });

  renderFilterNav();
  wireSplitViewer();
  pollActivity();
  loadTabs().catch(() => {}).then(loadQueue)
    .then(() => window.__route__?.start(currentRoute()), () => window.__route__?.start(currentRoute()));

  // workspace.js drives the reviewer/admin round trip and needs to redraw the queue after an
  // assignment arrives or a folder is submitted.
  window.__setViewerId__ = (id) => { state.myId = id; };
  // workspace.js adds `role-<role>` to <body> when /api/me answers.
  window.__roleKnown__ = () => {
    renderFilterNav();

  };
  window.loadQueue = loadQueue;
  window.openFolder = openFolder;

  // The folder card's own parts, so another screen can draw the same card rather than a lookalike
  // that drifts from it. Each takes a folder payload and returns the markup it owns.
  window.__folderCard__ = {
    progress: renderProgress,
    assignment: renderAssignment,
    stage: stageLabel,
    readyToExport,
    menu: renderCardMenu,
    claim: renderClaim,
    exportState: renderExportState,
    bind: bindFolderCard,
    // The document list's own parts.
    row: { reviewerPill, badgeClass, badgeLabel, commentChip, reviewDivider, isReviewed,
           peek: openCommentPeek },
    filters: FOLDER_FILTERS,
    filterNavs: FILTER_NAVS,
    filterIcons: FILTER_ICONS,
  };

  // The whole of what autosave.js may do: ask whether this screen has unsaved work, and save it
  // as it stands. It deliberately cannot set a review status -- an autosave keeps the one the
  // document already has, so nothing a reviewer has not decided is ever recorded as their verdict.
  window.__draft__ = {
    unsaved: () => Boolean(state.dirty && state.currentDoc && !state.saving && !state.currentDoc.locked),
    documentId: () => state.currentDoc?.document_id || null,
    save: () => saveReview(state.currentDoc.review.status, null, { quiet: true }),
  };
})();
