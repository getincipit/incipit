"""What the folder card's status bar is allowed to say with its colour."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import terms_js

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def render(folder, role="admin"):
    """renderProgress out of app.js, against one folder, as one role sees it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blocks = []
    for name in ("isReviewerView", "fullyExported", "readyToExport", "renderProgress", "markRow",
                 "folderReviewerPill", "adminPill", "pillText"):
        start = JS.index(f"  function {name}(")
        blocks.append(JS[start:JS.index("\n  }", start) + 4])
    harness = (
        terms_js() +
        "const escapeHtml = (s) => String(s == null ? '' : s);"
        f"const document = {{ body: {{ classList: {{ contains: (c) => c === 'role-{role}' }} }} }};"
        "const state = { myId: 1 };"
        + "".join(blocks)
        + f"console.log(renderProgress({json.dumps(folder)}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def folder(**overrides):
    base = {
        "folder_id": "E2002", "assignment_status": "submitted", "all_approved": False,
        "export": None, "reviewers": [],
        "progress": {"total": 10, "viewer": {"total": 10, "reviewed": 10, "accepted": 10,
                                             "flagged": 0, "percent": 100},
                     "other": {"total": 10, "reviewed": 4, "accepted": 4, "flagged": 0,
                               "percent": 40},
                     "other_name": "John Smith"},
    }
    base.update(overrides)
    return base


def test_a_folder_the_admin_has_fully_reviewed_is_green_while_a_reviewer_holds_it():
    """From 5 October: only the admin's review gates an export, so a reviewer still holding it does not."""
    html = render(folder(assignment_status="assigned", all_approved=True))

    assert "is-complete" in html


def test_the_reviewers_track_stays_behind_the_reviewers():
    """Pale blue behind the reviewer's own bar, whoever is holding the folder."""
    html = render(folder(assignment_status="assigned"))

    assert 'class="is-theirs"' in html
    assert "40%" in html, "and it still shows what the reviewer has done"


def test_a_folder_sent_back_counts_as_reviewed_only_when_every_document_is_accepted():
    html = render(folder(assignment_status="returned", all_approved=False))

    assert "is-complete" not in html


def test_a_folder_ready_to_export_is_green():
    html = render(folder(assignment_status="submitted", all_approved=True))

    assert "is-complete" in html


def test_a_fully_exported_folder_is_green():
    html = render(folder(assignment_status="approved", all_approved=True, document_count=10,
                         export={"exported_at": "2026-09-13", "document_count": 10}))

    assert "is-complete" in html


def test_a_premature_export_is_not_green():
    """Reported in the 2026-09-30 smoke pass: E2226, exported with one of four reviewed, read as
    finished. An "export anyway" leaves three documents still owed a review."""
    half = folder(assignment_status="submitted", document_count=4,
                  export={"exported_at": "2026-09-13", "document_count": 1})
    half["progress"]["viewer"] = {"total": 4, "reviewed": 1, "accepted": 1, "flagged": 0,
                                  "percent": 25}
    html = render(half)

    assert "is-complete" not in html


def test_a_premature_export_still_shows_how_far_the_review_got():
    half = folder(assignment_status="submitted", document_count=4,
                  export={"exported_at": "2026-09-13", "document_count": 1})
    half["progress"]["viewer"] = {"total": 4, "reviewed": 1, "accepted": 1, "flagged": 0,
                                  "percent": 25}

    assert "width:25%" in render(half)


def test_a_reviewer_part_way_through_is_not_green():
    part = folder(assignment_status="submitted")
    part["progress"]["viewer"] = {"total": 10, "reviewed": 6, "accepted": 6, "flagged": 0,
                                  "percent": 60}
    html = render(part)

    assert "is-complete" not in html


def reviewer_card(status):
    return {"folder_id": "E2002", "assignment_status": status, "reviewers": [],
            "progress": {"total": 10, "viewer": {"total": 10, "reviewed": 10, "accepted": 10,
                                                 "flagged": 0, "percent": 100}}}


def test_a_reviewer_accepting_everything_is_not_yet_finished():
    """Their own full tally says nothing about whether anyone has looked at it."""
    assert "is-complete" not in render(reviewer_card("assigned"), role="reviewer")
    assert "is-complete" not in render(reviewer_card("submitted"), role="reviewer")


def test_a_reviewers_folder_turns_green_when_it_is_approved():
    assert "is-complete" in render(reviewer_card("approved"), role="reviewer")


def test_ready_to_export_is_said_on_the_card():
    assert 'st-ready">Ready to export' in JS
    assert ".st-ready" in CSS


def test_ready_to_export_waits_on_the_admins_review_and_nothing_else():
    start = JS.index("  function readyToExport(")
    block = JS[start:JS.index("\n  }", start)]

    assert "withReviewer" not in block and "!fullyExported(f)" in block, "already exported is Completed"


def test_approved_but_unexported_folders_have_a_queue_to_live_in():
    """Green with no home: approved folders appeared under All and nowhere else."""
    start = JS.index('"needs-review": (f)')
    block = JS[start:JS.index("\n    completed", start)]

    assert '"submitted"' in block and '"approved"' in block


def test_ready_to_export_is_the_only_label_a_ready_folder_carries():
    """"With you" and "Approved" beside "Ready to export" said three things about one folder."""
    start = JS.index("${readyToExport(f)")
    block = JS[start:start + 320]

    assert "Ready to export" in block
    assert ": stage ?" in block, "the stage label is the alternative, not a second badge"


def test_the_reviewers_track_is_the_pale_one():
    """Semi-transparent, so the reviewer's own bar reads on top of it."""
    theirs = CSS.index(".folder-progress-bar span.is-theirs {")
    rule = CSS[theirs:CSS.index("}", theirs)]

    assert "--accent" in rule and "opacity:.28" in rule


def test_a_partial_export_does_not_make_a_folder_completed():
    """Reported in the 2026-09-30 smoke pass: E2226, exported with 1 of its 4 documents, sat under
    Completed and left Needs review for good."""
    start = JS.index("  function fullyExported(")
    block = JS[start:JS.index("\n  }", start)]

    assert "f.export.document_count" in block and "f.document_count" in block
    assert "f.all_approved" in block
