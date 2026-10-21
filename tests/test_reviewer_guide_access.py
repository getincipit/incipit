"""The reviewer's own guide must be reachable by a reviewer -- without opening anything else."""
import pathlib
import re

import pytest

from incipit import accounts, db, review_server
from incipit.config import Settings
from starlette.testclient import TestClient
from conftest import terms_js

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app


@pytest.fixture
def john(site) -> TestClient:
    c = TestClient(site)
    c.cookies.clear()          # conftest signs clients in as an admin; this one must not be
    c.post("/login", data={"username": "john", "password": "john-password"},
           follow_redirects=False)
    return c


# ---------- the button is back ----------


def test_the_reviewer_view_no_longer_removes_the_gear_button():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    listed = re.search(r"const REVIEWER_MUST_NOT_SEE = \[(.*?)\];", source, re.S)
    assert listed is not None, "the reviewer's hidden-control list has been renamed or removed"
    assert "btn-settings" not in listed.group(1), (
        "btn-settings is being removed from the reviewer's page again -- that also removes the "
        "reviewer's own How It Works guide, which lives in the same modal"
    )


def test_both_guides_exist_and_the_pipeline_reference_was_not_lost():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    for section in ("info-role-reviewer", "info-role-admin", "info-role-pipeline"):
        assert f'id="{section}"' in page, f"the {section} guide is missing"


# ---------- the button carries no authority ----------


def test_a_reviewer_is_refused_the_settings_write_behind_the_gear(john):
    """The button being on screen changes nothing about what the server will accept."""
    assert john.post("/api/settings", json={"new_document_batch_size": 99}).status_code == 403


def test_a_reviewer_is_refused_the_deletion_log_behind_the_gear(john):
    """Asked at the path the site actually serves."""
    assert john.get("/api/deletion-log").status_code == 403


# ---------- per-card controls a reviewer must not be offered ----------


def test_the_role_is_marked_on_the_body_before_anything_renders():
    """The hook the stylesheet needs."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert 'document.body.dataset.role = isAdmin() ? "admin" : "reviewer";' in source


def test_the_per_card_delete_controls_are_hidden_from_reviewers():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for control in (".queue-item-delete",):
        assert f'body[data-role="reviewer"] {control}' in css, (
            f"{control} is offered to reviewers again -- clicking it can only produce a 403"
        )


def test_the_workflow_tabs_wrap_rather_than_clipping_the_team_tab():
    """At the narrow breakpoint the sidebar is 200px and the three tabs want 227px."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = css.split(".workflow-tabs {", 1)[1].split("}", 1)[0]
    # With Notebooks there are four tabs, set two by two in a grid; either layout keeps Team whole.
    assert "flex-wrap: wrap" in block or "grid-template-columns: repeat(2" in block


def test_the_scrim_still_separates_the_layers_without_backdrop_blur():
    """The lighter scrim is only safe when the blur behind it renders."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    base = css.split(".modal-overlay {", 1)[1].split("}", 1)[0]
    assert "background: var(--overlay)" in base
    assert "--overlay: rgba(0, 0, 0, 0.45)" in css, "the light fallback scrim has been weakened"
    assert "@supports ((backdrop-filter" in css, "the blur is no longer feature-gated"


# ---------- the comment area, and the palette ----------


def test_the_comment_panel_is_always_open():
    """It sits at the foot of every document, open."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert 'panel.className = "doc-comments hidden"' in source
    assert "is-collapsed" not in source, "the panel can still be collapsed"
    assert "is-collapsed" not in css, "a collapsed state is still styled"


def test_the_comment_button_goes_to_them_rather_than_opening_them():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert 'button.addEventListener("click", showTheComments)' in source
    assert 'scrollIntoView({ behavior: "smooth", block: "nearest" })' in source


def test_there_is_no_way_to_close_them():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert "doc-comments-close" not in source
    assert "setCommentsOpen" not in source


def test_the_comment_control_sits_with_the_other_per_document_controls():
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert 'document.querySelector(".doc-header-right")' in source, (
        "the Comments button has moved out of the header row that holds the status pill"
    )


def test_a_collapsed_panel_cannot_hide_an_open_comment():
    """The count on the button is the whole reason collapsing is safe."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert 'pill.textContent = open || ""' in source
    assert 'classList.toggle("hidden", !open)' in source


def test_the_document_header_wraps_rather_than_clipping_delete():
    """The info pane is a fraction of a split view."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = css.split(".doc-header {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in block


def test_every_folder_state_carries_its_own_hue():
    """`returned` and `approved` were the only two ever given a rule, so a folder that was assigned
    or submitted drew no colour -- the two most common states in the queue looked identical."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for state in ("assigned", "submitted", "returned", "approved", "unassigned"):
        assert f".folder-assign.st-{state}" in css, f"st-{state} has no colour of its own"


def test_the_status_vocabulary_uses_five_distinct_tokens():
    """One hue per state, whichever name the pill is written with -- the lifecycle spellings the
    folder card uses and the short ones the rest of the app still does."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    pills = {
        ".st-idle": "--gray-bg", ".st-todo": "--accent-soft", ".st-wait": "--indigo-bg",
        ".st-back": "--amber-bg", ".st-done": "--green-bg",
        ".st-unassigned": "--gray-bg", ".st-assigned": "--accent-soft",
        ".st-submitted": "--indigo-bg", ".st-returned": "--amber-bg",
        ".st-approved": "--green-bg",
    }
    rules = [line for line in css.splitlines() if line.startswith(".st-") and "{" in line]
    for selector, token in pills.items():
        mine = [r for r in rules
                if selector in [s.strip() for s in r.split("{")[0].split(",")]]
        assert mine, f"{selector} is gone"
        assert token in mine[0], f"{selector} no longer uses {token}"


def test_pill_text_uses_the_darker_ink_tokens():
    """On their own tints the ordinary accent and muted grey measure 4.23:1 and 4.46:1 -- fine for
    a large label, short of AA for the 11-12px bold type these pills use."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "--accent-ink" in css and "--muted-ink" in css
    assert "color: var(--accent-ink)" in css and "color: var(--muted-ink)" in css


def test_the_folder_icon_takes_its_colour_from_the_document_not_the_file():
    """The mark is filled rather than stroked, so `fill` carries the colour."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    icon = [l for l in source.splitlines() if "const FOLDER_ICON" in l]
    assert len(icon) == 1, "expected exactly one FOLDER_ICON definition"
    assert 'fill="currentColor"' in icon[0]
    assert "#000000" not in icon[0], "the icon still carries the source file's hard-coded black"
    assert 'width="800px"' not in icon[0], (
        "the source's intrinsic 800px would fight the 34px the stylesheet sets"
    )


def test_the_folder_icon_is_tinted_by_state():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for state, token in (("submitted", "--indigo"), ("returned", "--amber"),
                         ("approved", "--green"), ("unassigned", "--text-muted")):
        assert f".folder-card:has(.st-{state}) .folder-card-icon" in css
        assert token in css


# ---------- controls that cannot act from where they are shown ----------


def test_the_team_tab_hides_the_sidebar_that_belongs_to_the_queue():
    """Discover + Extract, Export CSV and the queue filters all act on the document queue, which is
    hidden behind the team panel."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    listed = re.search(r"const QUEUE_ONLY_SIDEBAR = \[(.*?)\];", source, re.S)
    assert listed is not None, "the queue-only sidebar list has been renamed or removed"
    for section in ("folder-filter-section", "pipeline-section", "sidebar-footer"):
        assert section in listed.group(1), f"{section} is offered on the Team tab again"
        assert f'id="{section}"' in (STATIC / "index.html").read_text(encoding="utf-8")


def test_leaving_the_team_tab_gives_the_sidebar_back():
    """Hidden inline, so it must be cleared inline -- and cleared to "", which hands the section
    back to the stylesheet rather than pinning it visible."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert 'section.style.display = on ? "none" : "";' in source


def test_a_pipeline_heading_with_no_buttons_under_it_is_hidden():
    """Reviewers have Discover + Extract and Export CSV removed, which on the Existing Documents
    workflow left the "Pipeline" label standing over nothing."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert "function tidyPipelineSection" in source
    body = source.split("function tidyPipelineSection", 1)[1].split("\n  }", 1)[0]
    assert 'classList.contains("hidden")' in body, (
        "reading the layout instead of the class means a section hidden by its own class is "
        "measured as empty, and stays hidden after the class comes off"
    )
    assert ".nav-action" in body


def test_the_pipeline_section_is_rechecked_when_the_workflow_changes():
    """The tabs swap which actions box is shown, so the answer changes with them."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert "setTimeout(tidyPipelineSection" in source


def test_the_pipeline_section_has_the_handle_the_code_reaches_for():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="pipeline-section"' in page


def test_every_admin_only_rule_matches_a_route_that_exists(site):
    """The fault above, made general: a rule whose pattern matches nothing protects nothing, and
    looks exactly like a rule that works."""
    from incipit import webauth

    paths = [r.path for r in site.routes if hasattr(r, "path")]
    unmatched = [pattern.pattern for pattern, _ in webauth.ADMIN_ONLY
                 if not any(pattern.match(p) for p in paths)]

    assert unmatched == [], f"these admin-only rules guard no route: {unmatched}"


def test_a_reviewer_is_not_offered_a_delete_the_server_will_refuse(john):
    """The Remove button on an unconfirmed batch issues a DELETE, and DELETE is admin-only by verb."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    rendered = re.search(r"data-remove-batch-id=.*?\n", source)
    assert rendered is not None, "the Remove button has been renamed"
    assert 'dataset.role === "reviewer"' in source, (
        "the Remove button is rendered for reviewers again -- the server refuses their DELETE"
    )
    assert john.delete("/api/new/batches/anything").status_code == 403, (
        "if a reviewer may now remove a batch, the button should come back rather than this test go"
    )


# ---------- the sidebar is written from the viewer's own side ----------


def _filter_nav(role):
    """The filter list app.js builds for one role, as [(key, label), ...]."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    table = source[source.index("  const FILTER_NAVS = {"):]
    table = table[:table.index("\n  };") + 5]
    harness = terms_js() + table + f"console.log(JSON.stringify(FILTER_NAVS[{json.dumps(role)}]));"
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return [tuple(entry) for entry in json.loads(done.stdout)]


def test_a_reviewer_sorts_by_their_own_four_states():
    """"With reviewer" told a reviewer nothing -- they are the reviewer."""
    assert _filter_nav("reviewer") == [
        ("all", "All"),
        ("mine", "Needs review"),
        ("sent", "Sent for review"),
        ("returned", "Sent back"),
        ("approved", "Approved"),
    ]


def test_an_admin_sorts_the_shared_queue_in_processing_order():
    assert _filter_nav("admin") == [
        ("all", "All"),
        ("with-reviewer", "With Reviewer"),
        ("needs-review", "Needs review"),
        ("completed", "Completed"),
    ]


def test_every_filter_offered_has_a_rule_and_an_icon_behind_it():
    """A nav entry with no matching filter silently shows every folder."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    filters = source[source.index("const FOLDER_FILTERS = {"):source.index("const FILTER_ICONS")]
    icons = source[source.index("const FILTER_ICONS = {"):source.index("const FILTER_NAVS")]

    for role in ("admin", "reviewer"):
        for key, _ in _filter_nav(role):
            assert f'{key}:' in filters or f'"{key}":' in filters, f"{key} has no rule"
            assert f'{key}:' in icons or f'"{key}":' in icons, f"{key} has no icon"


def test_the_sidebar_list_is_built_from_the_role_not_written_into_the_page():
    """Both roles shared one hard-coded list, so neither could be worded for its reader."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    nav = markup[markup.index('id="folder-filter-nav"'):]

    assert "data-folder-filter" not in nav[:200], "the static list is back in the page"
    assert "renderFilterNav();" in (STATIC / "app.js").read_text(encoding="utf-8")


def test_nothing_reads_the_role_before_it_is_known():
    """The role class lands on <body> when /api/me answers, after app.js first runs."""
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    workspace = (STATIC / "workspace.js").read_text(encoding="utf-8")

    assert "window.__roleKnown__ = () =>" in app_js
    assert "renderFilterNav();" in app_js[app_js.index("window.__roleKnown__"):]
    # and workspace.js calls it immediately after stamping the class
    stamp = workspace.index("classList.add(`role-${document.body.dataset.role}`)")
    assert "window.__roleKnown__?.()" in workspace[stamp:stamp + 200]


