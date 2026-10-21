"""Who reviewed a document, and which half of the folder it sits in."""

from pathlib import Path

import pytest

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import insert_rows, seed_batch, signed_in

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul Israel", "admin")
        accounts.create_user(conn, "john", "john-password", "John Halloran", "reviewer")
        seed_batch(conn, "B", 2, folder="E2013", name="E2013_Business")
        conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = 'B'")
        for document_id in ("E2013AAA", "E2013AAB"):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, "
                "ai_title) VALUES (?, 'B', 'drafted', 'unreviewed', ?)",
                (document_id, f"title of {document_id}"))
        accounts.assign_folder(conn, "E2013", "new", accounts.get_user_by_name(conn, "john")["id"],
                               accounts.get_user_by_name(conn, "paul")["id"])
    return application, settings


def documents(client):
    return {d["document_id"]: d for d in client.get("/api/new/documents").json()}


def review_as(client, document_id, status):
    return client.post(f"/api/new/documents/{document_id}/review",
                       json={"review_status": status, "reviewed_text": "text"})


# ---------- the reviewer's name comes from the login ----------


def test_reviewing_stamps_the_signed_in_reviewer(site):
    application, _ = site
    admin = signed_in(application, "paul", "paul-password")

    review_as(admin, "E2013AAA", "accepted")

    assert documents(admin)["E2013AAA"]["reviewed_by"] == "Paul Israel"


def test_a_name_posted_by_the_client_is_ignored(site):
    """The field is gone from the page; anything still sending one must not be believed."""
    application, _ = site
    admin = signed_in(application, "paul", "paul-password")

    admin.post("/api/new/documents/E2013AAA/review",
               json={"review_status": "accepted", "reviewed_text": "t", "reviewer": "Somebody Else"})

    assert documents(admin)["E2013AAA"]["reviewed_by"] == "Paul Israel"


def test_the_document_itself_says_who_reviewed_it(site):
    application, _ = site
    admin = signed_in(application, "paul", "paul-password")

    review_as(admin, "E2013AAA", "accepted")

    assert admin.get("/api/new/documents/E2013AAA").json()["review"]["reviewed_by"] == "Paul Israel"


def test_a_reviewer_is_not_told_who_reviewed_their_work(site):
    application, _ = site
    review_as(signed_in(application, "paul", "paul-password"), "E2013AAA", "accepted")

    reviewer = signed_in(application, "john", "john-password")

    assert reviewer.get("/api/new/documents/E2013AAA").json()["review"]["reviewed_by"] == ""


# ---------- the reviewer's verdict, where the reviewer can see it ----------


def test_the_reviewer_sees_which_documents_the_reviewer_accepted(site):
    application, _ = site
    reviewer = signed_in(application, "john", "john-password")
    review_as(reviewer, "E2013AAA", "accepted")

    listed = documents(signed_in(application, "paul", "paul-password"))

    assert listed["E2013AAA"]["reviewer_status"] == "accepted"
    assert listed["E2013AAB"]["reviewer_status"] == "unreviewed"


def test_the_reviewer_sees_which_reviewer_accepted_it(site):
    application, _ = site
    review_as(signed_in(application, "john", "john-password"), "E2013AAA", "accepted")

    listed = documents(signed_in(application, "paul", "paul-password"))

    assert listed["E2013AAA"]["reviewer_name"] == "John Halloran"


def test_a_reviewers_accept_is_not_the_reviewers(site):
    """The whole confusion: two buttons called Accept."""
    application, _ = site
    review_as(signed_in(application, "john", "john-password"), "E2013AAA", "accepted")

    listed = documents(signed_in(application, "paul", "paul-password"))

    assert listed["E2013AAA"]["review_status"] == "unreviewed"
    assert listed["E2013AAA"]["reviewed_by"] == ""


def test_a_reviewer_is_not_sent_the_admin_board(site):
    """Same rule as every other admin field: not hidden in the page, absent from the payload."""
    application, _ = site
    reviewer = signed_in(application, "john", "john-password")

    listed = documents(reviewer)["E2013AAA"]

    for field in ("reviewed_by", "reviewer_status", "reviewer_name"):
        assert field not in listed, field


def test_reviewing_can_be_taken_back(site):
    """Clicking it again returns the document to the top half."""
    application, _ = site
    admin = signed_in(application, "paul", "paul-password")
    review_as(admin, "E2013AAA", "accepted")

    review_as(admin, "E2013AAA", "unreviewed")

    assert documents(admin)["E2013AAA"]["review_status"] == "unreviewed"


# ---------- the divider ----------


def test_the_folder_is_drawn_in_two_halves_for_a_reviewer():
    assert "const divided = isAdmin() && !state.resplitMode;" in JS
    assert "queueList.appendChild(reviewDivider(" in JS


def test_reviewed_documents_go_below_the_line():
    start = JS.index("const divided = isAdmin()")
    block = JS[start:start + 600]

    assert block.index("!isReviewed(doc)") < block.index("reviewDivider("), "reviewed came first"


def test_the_line_is_the_reviewers_verdict_not_the_reviewers():
    assert 'return doc.review_status === "accepted";' in JS


def test_a_reviewer_gets_one_list():
    """Dividing on the reviewer's verdict would tell a reviewer nothing about their own work."""
    assert "isAdmin() && !state.resplitMode" in JS


def test_the_line_is_drawn_even_when_nothing_is_reviewed_yet():
    """It says where reviewed documents will go, which an absent line does not."""
    start = JS.index("const divided = isAdmin()")
    assert "if (reviewed.length)" not in JS[start:start + 600]


# ---------- what each row shows ----------


def test_the_reviewers_name_is_only_on_rows_below_the_line():
    assert "isAdmin() && isReviewed(doc) && doc.reviewed_by" in JS


def test_the_reviewers_pill_marks_an_accepted_document():
    """One pill per verdict the reviewer can give; see test_reviewer_submit_and_flags for the flagged
    one."""
    assert "accepted: { label: \"Accepted\", css: \"pill-reviewer\"" in JS
    assert "REVIEWER_VERDICTS[doc.reviewer_status]" in JS


def test_the_pill_carries_no_dot():
    """Reported: "for the accepted, there should be no green dot should just be the text"."""
    assert "pill-dot" not in JS
    assert "pill-dot" not in CSS


def test_the_pill_escapes_the_name_it_shows():
    assert "escapeHtml(doc.reviewer_name)" in JS


def test_a_reviewers_accepted_badge_reads_as_reviewed():
    assert 'document.body.classList.contains("role-reviewer") ? "Accepted" : "Reviewed"' in JS


# ---------- the button, and the field that is gone ----------


def test_the_reviewers_button_says_mark_as_reviewed():
    assert 'isAdmin() ? "Mark as Reviewed" : "Accept"' in JS


def test_the_reviewers_button_toggles_back():
    assert 'const accepted = isAdmin() ? "Mark Unreviewed" : "Mark Unaccepted";' in JS


def test_the_reviewer_name_field_is_gone_from_the_page():
    assert 'id="reviewer-name"' not in HTML


def test_nothing_still_reads_that_field():
    assert "reviewerName" not in JS


def test_the_review_is_sent_without_a_typed_name():
    start = JS.index("async function saveReview(")
    assert "reviewer:" not in JS[start:start + 700]


def test_the_notes_field_is_for_editors():
    assert "<h3>Editors Notes</h3>" in HTML


# ---------- and it is laid out ----------


def test_the_divider_has_a_rule_on_both_sides_of_its_label():
    assert ".review-divider::before, .review-divider::after" in CSS


def test_the_pill_and_the_badge_are_one_object_in_two_colours():
    """They sit side by side on a row, so they have to agree on more than height: same case and
    letter spacing, or one reads as a label and the other as a tag."""
    badge_at = CSS.index(chr(10) + ".badge {")
    pill = CSS[CSS.index(".pill {"):CSS.index(".pill {") + 420]
    badge = CSS[badge_at:badge_at + 420]

    for rule in ("padding: 5px 12px", "font-size: 12px", "line-height: 1.25",
                 "text-transform: uppercase", "letter-spacing: 0.03em"):
        assert rule in pill, f"pill is missing {rule}"
        assert rule in badge, f"badge is missing {rule}"


def test_the_row_gives_its_badges_room_before_its_title():
    """A long title plus a reviewer's name must not squeeze the pills off the end of the row."""
    assert "flex-shrink: 0" in CSS[CSS.index(".queue-item-right {"):CSS.index(".queue-item-right {") + 120]
    assert "min-width: 0" in CSS[CSS.index(".queue-item-main {"):CSS.index(".queue-item-main {") + 130]


# ---------- review notes became editors notes ----------


def test_migrating_twice_is_harmless(tmp_path):
    path = str(tmp_path / "twice.db")
    db.init_db(path)

    db.init_db(path)

    with db.connect(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(new_documents)")}
    assert "editors_notes" in columns


def test_the_notes_reach_the_document_record(site):
    """"a field in the JSON files" -- the record is what the backup copies."""
    application, settings = site
    admin = signed_in(application, "paul", "paul-password")

    admin.post("/api/new/documents/E2013AAA/review",
               json={"review_status": "accepted", "reviewed_text": "t",
                     "editors_notes": "check the date on page 3"})

    assert admin.get("/api/new/documents/E2013AAA").json()["review"]["editors_notes"] \
        == "check the date on page 3"


# ---------- reviewed does not look like accepted ----------


def test_reviewed_has_its_own_colour():
    """Not the green a reviewer's Accept uses, and not the indigo comments own -- a reviewer's
    verdict is a third thing, so it gets a third colour."""
    assert ".badge-reviewed { background: var(--reviewed-bg); color: var(--reviewed); }" in CSS
    assert ".badge-accepted" in CSS, "a reviewer's own accept keeps its own colour"
    assert "--reviewed: #0a6f7d;" in CSS


def test_the_reviewers_button_carries_the_reviewed_colour():
    assert "body:not(.role-reviewer) #btn-accept {" in CSS
    start = CSS.index("body:not(.role-reviewer) #btn-accept {")
    assert "var(--reviewed)" in CSS[start:start + 160]


def test_only_the_review_button_changes_colour():
    """.btn-accept is the green every affirmative button uses -- Confirm & Draft, Looks right,
    Draft Selected, Save Settings."""
    assert "body:not(.role-reviewer) .btn-accept {" not in CSS


def test_the_other_affirmative_buttons_are_untouched():
    buttons = (STATIC / "index.html").read_text(encoding="utf-8")

    for control in ("split-confirm", "split-looks-right", "settings-save"):
        marker = f'id="{control}" class="btn btn-accept'
        assert marker in buttons, f"{control} no longer uses the shared green"


def test_a_reviewers_accept_button_is_still_green():
    assert ".btn-accept { border-color: var(--green); background: var(--green); color: var(--on-colour); }" in CSS


def test_the_badge_follows_the_role_too():
    assert '"badge-accepted" : "badge-reviewed"' in JS


def test_a_reviewed_row_is_not_tinted():
    """Reported: "the reviewed documents should not be entirely blue it makes it difficult to read
    other information about the document such as pages docid challenges"."""
    assert ".queue-item.is-reviewed {" not in CSS
    assert ".queue-item.is-reviewed .queue-item-title" not in CSS


def test_the_reviewed_colour_is_not_the_comments_colour():
    """--indigo is the comment panel's, and the palette keeps it out of the verdict vocabulary."""
    assert "--reviewed: #0a6f7d;" in CSS
    assert "--indigo: #4340c4;" in CSS
