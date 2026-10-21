"""Both workflows get the same review filters; only their Pipeline actions differ."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def nav_for(role):
    start = JS.index("const FILTER_NAVS = {")
    block = JS[start:JS.index("\n  };", start)]
    section = block[block.index(f"{role}: ["):]
    return section[:section.index(chr(10) + "    ],")]


# ---------- the reported fault ----------


def test_the_filters_are_not_hidden_by_an_empty_folder_list():
    assert 'folderFilterSection?.classList.toggle("hidden", !onGrid);' in JS


def test_they_are_still_hidden_inside_a_folder():
    """There they would answer a question the screen is not posing: you are in one folder."""
    assert '!onGrid' in JS
    assert 'filterNavSection.classList.toggle("hidden", onGrid)' in JS


def test_the_same_nav_serves_both_workflows():
    """One list, keyed by role and not by workflow -- which is what makes the tabs agree."""
    start = JS.index("function renderFilterNav(")
    block = JS[start:JS.index("\n  }", start)]

    assert "activeWorkflow" not in block


# ---------- what each role sees, on either tab ----------


def test_a_reviewer_gets_all_needs_review_sent_sent_back_and_approved():
    reviewer = nav_for("reviewer")

    for label in ("All", "Needs review", "Sent for review", "Sent back", "Approved"):
        assert f'"{label}"' in reviewer, label


def test_an_admin_sees_the_shared_workflow_in_processing_order():
    admin = nav_for("admin")

    quoted = ['"All"', '`With ${term("reviewer")}`', '"Needs review"', '"Completed"']
    assert [admin.index(label) for label in quoted] == sorted(admin.index(label) for label in quoted)
    for removed in ("Needs my review", "Sent for review", "Approved"):
        assert f'"{removed}"' not in admin


# ---------- the difference that is meant to be there ----------


def test_only_the_new_workflow_offers_upload_and_draft_pending():
    actions = HTML[HTML.index('id="new-pipeline-actions"'):]
    actions = actions[:actions.index("</div>\n      </div>")]

    assert "Upload Scans" in actions and "Draft Pending" in actions


