"""One test per change asked for, named after the request it answers."""
import csv
import io
import pathlib

import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from conftest import insert_rows, signed_in, terms_js

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
PARAGRAPHED = "First para.\n\n[page 2]\n\nSecond para."


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export" / "out.csv"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        for folder, n in (("E2226", 2), ("E2229", 1)):
            insert_rows(conn,
                "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                "VALUES (?, 'now', ?, ?)", (f"B-{folder}", n, folder))
            for i in range(n):
                insert_rows(conn,
                    "INSERT INTO new_documents "
                    "(document_id, batch_id, status, review_status, text) "
                    "VALUES (?, ?, 'drafted', 'accepted', ?)",
                    (f"{folder}AA{chr(65 + i)}", f"B-{folder}", PARAGRAPHED))
        accounts.assign_folder(conn, "E2226", "new",
                               accounts.get_user_by_name(conn, "john")["id"],
                               accounts.get_user_by_name(conn, "paul")["id"])
    return app, db_path


# ---------- "I should be able to select which folder I want to specifically export" ----------


# ---------- "the name of the CSV file matches the folder name with _transcription" ----------


# ---------- "a distinction between folders exported and folders not exported" ----------


def test_the_card_shows_which_state_a_folder_is_in():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "renderExportState" in source
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".export-state.is-done" in css


# ---------- "there is no clear button to send back to the reviewer" ----------


def test_an_admin_can_send_a_folder_back_from_the_queue(site):
    app, _ = site
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "renderAdminFolderActions" in source
    assert "Send back to" in source, "the folder card offers no way to return a folder"
    res = signed_in(app, "paul").post("/api/team/review", json={
        "folder_id": "E2226", "workflow": "new", "status": "returned",
        "note": "check the dates"})
    assert res.status_code == 200


# ---------- "the status bar should also be replicated in the existing documents" ----------


# ---------- "paragraph breaks should also exist in the final export" ----------


def test_paragraph_and_page_breaks_survive_the_csv(site):
    from incipit.export import export_new_csv
    _, db_path = site
    settings = Settings(db_path=db_path)
    _, body, _ = export_new_csv(settings, "E2226")
    text = body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    cells = [cell for row in rows for cell in row]
    assert any("[page 2]" in cell for cell in cells), "the page break was lost on export"
    assert any("\n\n" in cell for cell in cells), "paragraph breaks were flattened"


def test_the_prompt_asks_for_paragraphs_rather_than_one_block():
    from incipit import prompting

    text = prompting.compile({}).document_prompt
    assert "single continuous paragraph with NO line breaks" not in text, (
        "the prompt still forbids the paragraph breaks that were asked for"
    )
    assert "[page 2]" in text


# ---------- "the side panel needs to be able to be collapsed" ----------


def test_the_sidebar_toggle_lives_at_the_foot_of_the_sidebar():
    """Reported: floating over the page at the top, the toggle sat on the header's ADMIN badge."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    start = source.index("function installSidebarToggle(")
    block = source[start:source.index("\n  }\n", start)]
    assert "nav.appendChild(toggle)" in block
    assert "document.body.appendChild(toggle)" not in block


def test_the_sidebar_toggle_is_not_positioned_over_the_page():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".sidebar-toggle {"):css.index("}", css.index(".sidebar-toggle {"))]
    assert "position: fixed" not in rule
    assert "position: sticky" in rule


def test_a_collapsed_sidebar_leaves_a_rail_for_its_toggle():
    """Collapsing it to nothing left the toggle floating over the document screen."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "var(--sidebar-width) - var(--sidebar-rail)" in css


def test_the_transcription_fills_the_window_instead_of_leaving_a_band_of_space():
    """Originally done by removing the ceiling entirely and flexing the column, which is what made
    the cards overlap and the transcription unreadable."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "max-height: 60vh" in css
    assert "max-height: 340px" not in css.split(".text-preview {", 1)[1].split("}", 1)[0]


# ---------- "there should be a notification on the reviewer end ... and for the admin" ----------


def test_a_reviewer_is_notified_when_a_folder_is_assigned(site):
    app, db_path = site
    john = signed_in(app, "john")
    payload = john.get("/api/notifications").json()
    kinds = [i.get("action") for i in payload["items"]]
    assert "assigned" in kinds, f"the reviewer was not told about their new folder: {kinds}"
    assert payload["unread"] >= 1


def test_an_admin_is_notified_when_the_folder_comes_back(site):
    app, _ = site
    john, paul = signed_in(app, "john"), signed_in(app, "paul")
    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new", "note": "done"})

    payload = paul.get("/api/notifications").json()
    kinds = [i.get("action") for i in payload["items"]]
    assert "submitted" in kinds, f"the admin was not told the folder came back: {kinds}"


def test_nobody_is_notified_about_their_own_action(site):
    """A badge that counts your own clicks stops being read."""
    app, _ = site
    john = signed_in(app, "john")
    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new", "note": "done"})
    kinds = [i.get("action") for i in john.get("/api/notifications").json()["items"]]
    assert "submitted" not in kinds


# ---------- "no style errors or overlapping fields when I zoom in or out" ----------


def test_the_info_column_is_not_a_flex_container():
    """The overlap that made the transcription unreadable."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".info-scroll { display: flex" not in css
    assert ".transcription-block { display: flex; flex-direction: column" not in css


def test_the_transcription_ceiling_scales_with_the_viewport():
    """A fixed pixel ceiling clips at high browser zoom, where the same text needs far more of
    them."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "max-height: 60vh" in css


def test_the_comment_panel_lives_in_the_info_column():
    """Appended to #detail-view -- a row flex container holding the image and the metadata pane --
    it became a third column and held a band of empty width open beside the fields even while
    collapsed."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert 'document.querySelector(".info-scroll")' in source


def test_the_split_view_stacks_before_the_panes_get_too_narrow():
    """Whatever the window size or zoom level, the two panes must stop sitting side by side rather
    than squeezing until their contents collide."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "@media (max-width: 900px)" in css
    block = css.split("@media (max-width: 900px) {", 1)[1].split("}\n@media", 1)[0]
    assert "flex-direction: column" in block


# ---------- "Send back clutters the queue on folders nobody has handed in" ----------


def _admin_folder_actions(assignment_status):
    """Runs renderAdminFolderActions for one folder state and returns the markup it produces."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("  function renderAdminFolderActions(f) {")
    end = source.index("\n  }", start) + len("\n  }")
    harness = f"""
    const escapeHtml = (s) => String(s);
    const state = {{ activeWorkflow: "new" }};
    global.document = {{ body: {{ dataset: {{ role: "admin" }} }} }};
    {source[source.index("  function fullyExported("):source.index("\n  }", source.index("  function fullyExported(")) + 4]}
    {source[start:end]}
    console.log(renderAdminFolderActions({{
      folder_id: "E2002", workflow: "new", assigned_to_name: "John",
      assignment_status: {json.dumps(assignment_status)},
    }}));
    """
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("status", ["assigned", "returned"])
def test_send_back_is_not_offered_on_a_folder_the_reviewer_still_holds(status):
    """The reported clutter."""
    assert "Send back" not in _admin_folder_actions(status)


def test_a_folder_with_no_action_to_offer_draws_no_row_at_all():
    """Otherwise the card keeps an empty strip where the buttons used to be."""
    assert _admin_folder_actions("assigned") == ""


def test_send_back_appears_once_it_is_handed_in():
    """Approving is not offered at all: a folder approves itself once every document is accepted
    (see test_a_finished_folder_approves_itself.py)."""
    markup = _admin_folder_actions("submitted")
    assert "Send back to John" in markup
    assert "Approve" not in markup


def test_approved_work_can_still_be_reopened():
    """The one place a locked folder can be handed back."""
    markup = _admin_folder_actions("approved")
    assert "Send back to John" in markup


# ---------- "the assignment block is cluttered and repeats itself" ----------


def _assignment_block(folder, role="admin"):
    """Runs renderAssignment for one folder and returns the markup it produces."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")

    def block(name):
        start = source.index(f"  function {name}(")
        return source[start:source.index("\n  }", start) + len("\n  }")]

    # Read from app.js rather than restated here.
    labels_at = source.index("  const STAGE_LABELS = {")
    end = source.index("\n  };", labels_at) + 5
    labels = source[labels_at:end]

    harness = terms_js() + f"""
    const escapeHtml = (s) => String(s == null ? "" : s);
    const state = {{ activeWorkflow: "new" }};
    const classes = new Set([{json.dumps("role-" + role)}]);
    global.document = {{ body: {{ dataset: {{ role: {json.dumps(role)} }},
                                  classList: {{ contains: (c) => classes.has(c) }} }} }};
    {labels}
    {block("fullyExported")}
    {block("stageLabel")}
    {block("renderAdminFolderActions")}
    {block("realNote")}
    {block("renderAssignment")}
    console.log(renderAssignment({json.dumps(folder)}));
    """
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return " ".join(done.stdout.split())


def _stage_label(folder, role="admin"):
    """Runs stageLabel for one folder, as the given role sees it."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    labels_at = source.index("  const STAGE_LABELS = {")
    labels = source[labels_at:source.index("\n  };", labels_at) + 5]
    start = source.index("  function stageLabel(")
    body = source[start:source.index("\n  }", start) + 4]
    classes = json.dumps(["role-" + role])
    harness = (
        terms_js() +
        "const classes = new Set(" + classes + ");"
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + labels + body +
        "console.log(stageLabel(" + json.dumps(folder) + "));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()

ASSIGNED = {"folder_id": "E2002", "workflow": "new", "assignment_status": "assigned",
            "assigned_to_name": "Sarah Jung", "assigned_by_name": "Paul Israel",
            "latest_note": "to Sarah Jung", "latest_note_by": "Paul Israel"}


def test_the_reviewer_is_named_once_not_three_times():
    """The reported clutter: the card showed the reviewer, then "Paul Israel: to Sarah Jung" as
    though it were a note, then a button carrying the name a third time."""
    markup = _assignment_block(ASSIGNED)

    assert markup.count("Sarah Jung") == 2, markup   # the To: value, and its title attribute
    assert "to Sarah Jung" not in markup


def test_the_admin_is_labelled_rather_than_disguised_as_a_note():
    markup = _assignment_block(ASSIGNED)

    assert "Assigned by" in markup and "Paul Israel" in markup
    assert "folder-assign-note" not in markup, "the manufactured note is still being drawn"


def test_a_note_somebody_wrote_is_still_shown():
    folder = dict(ASSIGNED, latest_note="check the dates on page 3")
    markup = _assignment_block(folder)

    assert "Note" in markup and "check the dates on page 3" in markup


def test_no_note_row_at_all_when_there_is_no_note():
    folder = dict(ASSIGNED, latest_note="")
    markup = _assignment_block(folder)

    assert ">Note<" not in markup


def test_a_reviewer_is_not_told_the_folder_is_assigned_to_themselves():
    markup = _assignment_block(ASSIGNED, role="reviewer")

    assert "Sarah Jung" not in markup
    assert "Assigned by" in markup and "Paul Israel" in markup


def test_the_assignment_block_carries_no_status_pill_of_its_own():
    """The pill moved to the card's top line, beside the folder name: one fixed place for "whose
    desk is this on", rather than one that moved with whichever row happened to be first."""
    markup = _assignment_block(dict(ASSIGNED, assignment_status="submitted"))

    assert "class=\"st st-" not in markup


def test_a_long_name_is_clipped_rather_than_wrapped():
    """Wrapping is what made one card in a row twice the height of its neighbour."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "text-overflow:ellipsis" in css.replace(" ", "")
    assert ".folder-card-foot" in css and "margin-top: auto" in css


# ---------- "the Contributors table is redundant" ----------


def test_the_contributors_table_is_gone():
    """Every column it had is either in Assignments below it (who, which folder, how active) or is
    a number worth reading one person at a time."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert 'id="stats-body"' not in source, "the Contributors table is still being drawn"
    assert "Avg turnaround</th>" not in source


def test_a_reviewer_name_in_assignments_opens_their_statistics():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert 'class="reviewer-stats-link" data-reviewer=' in source
    assert "function showReviewerStats" in source
    # a button, so the table is navigable by keyboard
    assert '<button type="button" class="reviewer-stats-link"' in source


def test_the_statistics_panel_carries_every_number_the_table_did():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    start = source.index("function showReviewerStats")
    panel = source[start:source.index("\n  async function loadRoster", start)]

    for field in ("documents_accepted", "submissions", "returns",
                  "avg_turnaround_hours", "last_active"):
        assert field in panel, f"{field} was dropped along with the table"
    # The two folder counts are drawn from the lists behind them now, so they are named by their
    # labels rather than by a count field -- the numbers are still both there.
    for label in ("Folders held", "Folders approved"):
        assert label in panel, f"{label} was dropped along with the table"
    assert "folders.length" in panel


def test_the_editors_claims_table_is_still_drawn():
    """Left alone at first, then restyled with Assignments on 15 October; the table itself stays."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert 'id="claims-body"' in source
    assert ">Claims</h3>" in source

def test_an_assigned_folder_carries_no_status_badge():
    """A card naming who it went to and who sent it already says it is somebody's to do."""
    markup = _assignment_block(ASSIGNED)

    assert "To do" not in markup
    assert "folder-assign-value" in markup, "the names went with it"


@pytest.mark.parametrize("status,admin,reviewer", [
    ("assigned", "With Sarah", "With you"),
    ("submitted", "With you", "With admin"),
    ("returned", "With Sarah", "Needs changes"),
    ("approved", "Approved", "Approved"),
])
def test_the_stage_chip_is_written_from_the_viewers_own_side(status, admin, reviewer):
    """"With reviewer" told an admin nothing -- they are the reviewer."""
    folder = dict(ASSIGNED, assignment_status=status)

    assert _stage_label(folder, "admin") == admin
    assert _stage_label(folder, "reviewer") == reviewer


def test_the_names_are_set_apart_from_their_labels():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".folder-assign-value"):css.index(".folder-assign-value") + 260]
    assert "font-style:italic" in block.replace(" ", "")


# ---------- "Send back is too large; let me sort folders by whose desk they are on" ----------


def test_send_back_does_not_repeat_the_name_the_card_already_carries():
    """"Send back to Makayla Bridgers" wrapped onto three lines and became the tallest thing on the
    card."""
    markup = _assignment_block(dict(ASSIGNED, assignment_status="submitted"))

    assert ">Send back</button>" in markup.replace("\n", " ")
    assert "Send back to Sarah Jung<" not in markup
    assert 'title="Send back to Sarah Jung"' in markup


def test_the_buttons_are_sized_to_their_labels():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".folder-admin-actions"):]
    assert "white-space: nowrap" in block[:700], "a long label can still wrap the button"


def _folder_filters():
    """FOLDER_FILTERS, executed, so the states each filter claims are the ones it really matches."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("  const FOLDER_FILTERS = {")
    end = source.index(chr(10) + "  };", start) + 5
    table = source[start:end]
    harness = (
        terms_js() +
        "const state = { myId: 1 };"
        'const classes = new Set(["role-admin"]);'
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + source[source.index("  function fullyExported("):source.index("\n  }", source.index("  function fullyExported(")) + 4] + table +
        'const states = ["assigned", "returned", "submitted", "approved", "unassigned"];'
        "const out = {};"
        "for (const [name, matches] of Object.entries(FOLDER_FILTERS)) {"
        # every folder is the viewer's own, so this measures the lifecycle rule alone
        "  out[name] = states.filter((s) => matches({ assignment_status: s, assigned_by: 1 }));"
        "}"
        "console.log(JSON.stringify(out));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)

def test_with_reviewer_covers_both_states_a_reviewer_holds_it_in():
    """Freshly assigned and sent back for changes are one question for an admin -- what am I
    waiting on -- so they are one list."""
    assert set(_folder_filters()["with-reviewer"]) == {"assigned", "returned"}


def test_needs_review_is_what_the_admin_team_still_owes_something_on():
    """Approved but unexported belongs here too: it is where Ready to export is read."""
    assert set(_folder_filters()["needs-review"]) == {"submitted", "approved"}


def test_all_keeps_everything():
    filters = _folder_filters()
    assert len(filters["all"]) == 5, "All stopped showing every folder"


def test_the_folder_filters_and_the_document_filters_never_show_together():
    """Inside a folder you filter documents; on the grid you filter folders."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    start = source.index("function updateFilterNavVisibility")
    block = source[start:source.index("\n  }", start)]
    assert "filterNavSection.classList.toggle(\"hidden\", onGrid)" in block
    assert "!onGrid" in block


# ---------- "Send back opened the folder instead of sending it back" ----------


def test_the_lifecycle_buttons_do_not_also_open_the_folder():
    """The card opens the folder when clicked, and every control sitting on it has to be excluded
    from that -- Approve and Send back were not, so clicking either navigated instead of acting."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    start = source.index('card.addEventListener("click"')
    guard = source[start:source.index("openFolder(f.folder_id)", start)]

    # Claim, Release, Analytics and Delete now live inside the menu, so excluding the menu excludes
    # all four.  The reviewer's submit button is rendered by app.js but handled by workspace.js;
    # it still has to be stopped here before the card's own handler opens the folder.
    for control in ("data-return", "folder-send", "card-menu"):
        assert control in guard, control


# ---------- "after sending back to a reviewer, their statistics show up" ----------


def test_sending_a_folder_back_does_not_open_the_reviewers_statistics():
    """The Send back buttons carry the reviewer's name so the picker can address them, and the
    statistics panel was listening for anything carrying a reviewer."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    start = source.index('const link = e.target.closest(')
    selector = source[start:source.index(")", start)]

    assert "reviewer-stats-link" in selector
    assert '"[data-reviewer]"' not in selector


def test_only_one_of_each_panel_can_be_open_at_a_time():
    """Two Send back clicks stacked two pickers, and a tick on the one underneath was still counted
    when the one on top was sent."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert '.rework-overlay")?.remove()' in source
    assert 'getElementById("reviewer-stats")?.remove()' in source


def test_the_picker_is_wide_enough_for_a_whole_document_title():
    """An editor tells two documents apart by their titles; a clipped one defeats the purpose."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    modal = css[css.index(".rework-modal"):css.index(".rework-modal") + 60]
    title = css[css.index(".rework-title"):css.index(".rework-title") + 120]

    assert "760px" in modal
    assert "text-overflow: ellipsis" not in title and "nowrap" not in title


# ---------- "\"With you\" clutters the folder title" ----------


def test_the_stage_chip_is_not_on_the_title_line():
    """The title must never be crowded: the chip sits on the sub-line beneath it."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    top = source[source.index('<div class="folder-card-top">'):source.index('folder-card-sub')]

    assert "st-" not in top, "the stage chip is back on the folder name's line"
    sub = source[source.index('<div class="folder-card-sub">'):]
    assert "st-small" in sub[:400]


def test_the_stage_chip_is_the_smaller_pill():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    small = css.index(".st-small")
    plain = css.index(".st { padding")

    assert small > plain, "the smaller size has to come after .st to win on source order"
    assert "font-size: 11px" in css[small:small + 120]


# ---------- "too much text in the footer; icons are not clear either" ----------


def test_the_footer_states_facts_and_hides_actions_behind_one_worded_menu():
    """It carried Analytics, the export state, Claim, Release and a trash icon as five separate
    scraps."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    foot = source[source.index('<div class="folder-card-foot">'):]
    foot = foot[:foot.index("</div>")]

    assert "renderCardMenu(f)" in foot
    assert "renderExportState(f)" in foot and "renderClaim(f)" in foot
    assert "renderAnalyticsLink" not in source, "the loose analytics link is still drawn"

    menu = source[source.index("function renderCardMenu("):]
    menu = menu[:menu.index("\n  }")]
    for label in ("Drafting analytics", "Claim this folder", "Release my claim", "Delete folder"):
        assert label in menu, label


def test_a_folder_that_has_not_been_exported_says_nothing_about_it():
    """"Not exported" sat on nearly every card and named no action."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = source[source.index("function renderExportState("):]
    block = block[:block.index("\n  }")]

    assert "Not exported" not in block
    assert 'if (!f.export) return ""' in block


def test_the_export_date_is_written_for_a_glance():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "shortDate(" in source
    assert '"Jan", "Feb"' in source


def test_the_trash_icon_is_gone_from_the_folder_title():
    """The one irreversible action on the card was a symbol in the corner, next to the name."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    top = source[source.index('<div class="folder-card-top">'):source.index('folder-card-sub')]

    assert "folder-card-delete" not in top
    assert "data-card-delete" in source, "delete has to still be reachable, in the menu"


def test_an_open_menu_swallows_the_click_that_dismisses_it():
    """Otherwise the folder opens underneath and the menu's click lands on the next screen."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    handler = source[source.index('card.addEventListener("click"'):]
    handler = handler[:handler.index("openFolder(f.folder_id)")]

    assert '.card-menu-list:not([hidden])' in handler


def test_the_menu_really_hides_when_it_is_closed():
    """Regression: `display: flex` on the list beat the `hidden` attribute, so every card showed
    its whole menu inline -- the opposite of what the menu is for."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".card-menu-list[hidden] { display: none; }" in css


# ---------- "sending a folder back is slow and not instantaneous" ----------


def test_sending_a_folder_back_redraws_the_queue_it_was_clicked_from():
    """The POST always took about 25ms."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    handler = source[source.index('const back = e.target.closest("[data-return]")'):]
    handler = handler[:handler.index("\n  });")]

    assert "window.loadQueue?.()" in handler
    assert "loadRoster()" in handler


def test_the_queue_fetches_its_two_lists_at_once():
    """They are independent, and the folder grid was waiting on the whole document list before it
    could redraw one card."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = source[source.index("async function loadQueue()"):]
    block = block[:block.index("renderQueue();")]

    assert "Promise.all" in block
    assert "await fetch" not in block, "a sequential fetch is back in the queue load"


# ---------- "the exported folder should be named for the folder" ----------


def test_export_is_reachable_from_the_folder_card():
    """It used to live only inside a folder -- two clicks from the grid where an editor decides
    which folder is finished."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    menu = source[source.index("function renderCardMenu("):]
    menu = menu[:menu.index("\n  }")]

    assert "Export folder" in menu
    assert "data-card-export" in menu


# ---------- "the accept button feels glitchy and quick" ----------


def test_the_label_swap_is_a_transition_rather_than_a_frame():
    """The word and the fill changed on the same frame, which reads as a flicker."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    swap = source[source.index("function swapButtonLabel("):]
    swap = swap[:swap.index("\n  }\n")]

    assert "is-swapping" in swap
    assert "prefers-reduced-motion" in swap
    assert "measureLabelWidth" in source, "the width is not measured, so the button still jumps"


def test_the_swap_is_slow_enough_to_be_seen():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    out = int(source.split("SWAP_OUT_MS = ")[1].split(";")[0])

    assert 120 <= out <= 260, out


def test_the_press_and_the_colour_change_are_shared_by_every_action_button():
    """Accept and Save Draft both carry btn-deep, so the press and the transition are the same on
    each; `color` was missing from the list, so text colour snapped while the fill faded underneath
    it."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    base = css[css.index(".btn {"):css.index(".btn {") + 700]

    assert "color var(--fast)" in base
    assert ".btn-deep:active" in css
    assert ".btn-label" in css and "transition: opacity" in css

    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    for button in ("btn-accept", "btn-save-draft"):
        row = [line for line in markup.splitlines() if f'id="{button}"' in line]
        assert row and "btn-deep" in row[0], button


# ---------- "the notification icon is tough to notice" ----------


def test_the_bell_is_an_icon_rather_than_a_bare_dot():
    """It was a grey filled circle, which on the dark identity bar read as punctuation rather than
    a control."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    bell = source[source.index('class="who-bell"'):]
    bell = bell[:bell.index("</button>")]

    assert "<svg" in bell and "who-bell-icon" in bell
    assert "&#9679;" not in bell, "the old dot is still there"
    assert 'aria-label="Recent activity on your work"' in bell


def test_the_bell_is_legible_at_rest_and_unmistakable_unread():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    resting = css[css.index(".who-bell {"):css.index(".who-bell-icon")]
    unread = css[css.index(".who-bell.has-unread {"):]
    unread = unread[:unread.index("}")]

    # the page's own ink on the light bar, where the pale grey mixed for the old dark bar would vanish
    assert "var(--text)" in resting
    assert "border-radius:999px" in resting, "no hit area, so nothing to hover"
    assert "var(--accent" in unread and "background" in unread


def test_the_bell_rings_once_on_arrival_not_on_every_poll():
    """A looping animation in the chrome is the kind of thing people learn to stop seeing."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "unread > lastUnread" in source
    assert "just-arrived" in source and "just-arrived" in css
    ring = css[css.index("@keyframes bell-arrive"):]
    assert "infinite" not in ring[:400]
    assert "prefers-reduced-motion" in css[css.index(".who-bell.just-arrived"):][:400]


# ---------- "let me see which folders, not just how many" ----------


def test_the_folder_counts_open_into_the_folders_they_count():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    block = source[source.index("const folderStat ="):]
    block = block[:block.index("\n    };")]

    assert "reviewer-stat-toggle" in block
    assert "f.folder_id" in block and "f.workflow" in block
    assert 'aria-expanded="false"' in block


def test_a_person_with_no_folders_gets_a_plain_count_not_an_empty_list():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    block = source[source.index("const folderStat ="):]
    block = block[:block.index("\n    };")]

    assert "if (!folders || !folders.length) return stat(" in block


# ---------- "nothing stopped me re-uploading a folder I had deleted" ----------


def test_the_upload_offers_to_replace_rather_than_failing_silently():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    handler = source[source.index('uploadScansInput.addEventListener("change"'):]

    assert 'res.status === 409 && res.headers.get("x-folder-exists")' in handler
    assert "confirmReplaceFolder(" in handler
    assert 'formData.append("replace", "true")' in handler


def test_replacing_a_folder_asks_for_its_id_to_be_typed():
    """The same bar the delete dialog sets, for the same reason: it cannot be undone."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    dialog = source[source.index("function confirmReplaceFolder("):]
    dialog = dialog[:dialog.index("\n  }")]

    assert "replace-confirm" in dialog
    assert "toUpperCase() !== folderId.toUpperCase()" in dialog
    assert "disabled" in dialog


# ---------- "the site-wide banner is not necessary any more" ----------


def test_the_site_wide_activity_banner_is_gone():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    source = (STATIC / "app.js").read_text(encoding="utf-8")

    for gone in ("activity-banner", "activity-pill", "activity-minimize", "activity-label"):
        assert gone not in markup, gone
        assert gone not in css, gone
        assert gone not in source, gone


def test_what_the_banner_was_for_is_still_reachable():
    """The folder's own drafting note and the pipeline status line say what is happening where the
    work is; the whole picture moved into Settings rather than being lost with the banner."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    source = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="pipeline-status"' in markup
    assert 'id="folder-drafting-note"' in markup
    assert 'data-settings-tab="running"' in markup
    assert 'id="settings-panel-running"' in markup
    assert 'if (target === "running") renderGenerationLog();' in source


def test_the_poll_that_refreshes_the_queue_when_work_finishes_survived():
    """Removing the banner must not take the thing that notices a draft finishing."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    poll = source[source.index("async function pollActivity()"):]
    poll = poll[:poll.index("\n  }")]

    assert "if (!running && wasRunning)" in poll
    assert "loadQueue();" in poll


# ---------- "the folders and review queue are right-aligned" ----------


def test_the_page_markup_is_properly_nested():
    """Reported after the activity banner was removed: every view drifted to the right on every
    tab."""
    from html.parser import HTMLParser

    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}

    class Nesting(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.problems = []

        def handle_starttag(self, tag, attrs):
            if tag not in void:
                self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag in void:
                return
            if not self.stack:
                self.problems.append(f"line {self.getpos()[0]}: </{tag}> with nothing open")
            elif self.stack[-1][0] != tag:
                self.problems.append(
                    f"line {self.getpos()[0]}: </{tag}> closes <{self.stack[-1][0]}> "
                    f"opened on line {self.stack[-1][1]}")
                self.stack.pop()
            else:
                self.stack.pop()

    parser = Nesting()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))

    assert parser.problems == [], parser.problems
    assert parser.stack == [], f"never closed: {parser.stack}"


def test_every_view_is_inside_the_main_column():
    """The symptom the nesting bug produced: a view outside `.main` is laid out as a flex sibling
    of the sidebar, which pushes it across the page."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    main = markup[markup.index('<div class="main">'):]

    for view in ('id="queue-view"', 'id="detail-view"', 'id="nav-controls"'):
        assert view in main, f"{view} is outside .main"


def test_a_folder_list_has_one_way_back_to_all_folders():
    """Reported: "← All folders" beside a folder's name left a stale "← Back to all folders" bar at
    the top, which then followed the reviewer round the site. The header's control is the only one."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    target = source[source.index("  function backTarget() {"):source.index("  function updateBackButton()")]
    assert "Back to all folders" not in target


def test_returning_to_the_folder_grid_settles_the_back_bar():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "if (showFolderGrid()) { renderFolderGrid(); updateBackButton(); return; }" in source


