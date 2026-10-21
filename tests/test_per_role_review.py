"""A reviewer's "accepted" and an admin's "reviewed" are different facts."""
import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from starlette.testclient import TestClient
from conftest import insert_rows


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B','now',2,'E2226')")
        for suffix in ("AAA", "AAB"):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                "VALUES (?, 'B', 'drafted', 'unreviewed')", (f"E2226{suffix}",))
        accounts.assign_folder(conn, "E2226", "new",
                               accounts.get_user_by_name(conn, "john")["id"],
                               accounts.get_user_by_name(conn, "paul")["id"])
    return app, db_path


def client(app, who):
    c = TestClient(app)
    c.cookies.clear()
    c.post("/login", data={"username": who, "password": who + "-password"}, follow_redirects=False)
    return c


def statuses(c):
    return {d["document_id"]: d["review_status"] for d in c.get("/api/new/documents").json()}


def accept(c, doc, status="accepted", text="draft text"):
    """reviewed_text is required by the payload schema, so every call carries it."""
    return c.post(f"/api/new/documents/{doc}/review",
                  json={"review_status": status, "reviewer": "", "editors_notes": "",
                        "reviewed_text": text})


# ---------- the separation ----------


def test_a_reviewers_accept_does_not_mark_it_reviewed_for_the_admin(site):
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    assert accept(john, "E2226AAA").status_code == 200

    assert statuses(john)["E2226AAA"] == "accepted", "the reviewer cannot see their own work"
    assert statuses(paul)["E2226AAA"] == "unreviewed", (
        "the admin's queue is reporting a document reviewed that they have never opened"
    )


def test_an_admins_accept_does_not_touch_the_reviewers_record(site):
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    accept(john, "E2226AAA")
    accept(paul, "E2226AAA")
    assert statuses(paul)["E2226AAA"] == "accepted"
    assert statuses(john)["E2226AAA"] == "accepted"

    # and the admin reopening it does not erase what the reviewer did
    accept(paul, "E2226AAA", status="unreviewed")
    assert statuses(paul)["E2226AAA"] == "unreviewed"
    assert statuses(john)["E2226AAA"] == "accepted", "the reviewer's own record was erased"


def test_each_side_counts_only_their_own(site):
    """The queue's All/Unreviewed/Accepted counts are derived from these, so they follow."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    accept(john, "E2226AAA")
    accept(john, "E2226AAB")
    assert sorted(statuses(john).values()) == ["accepted", "accepted"]
    assert sorted(statuses(paul).values()) == ["unreviewed", "unreviewed"]


def test_the_detail_view_shows_the_asker_their_own_status(site):
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    accept(john, "E2226AAA")
    assert john.get("/api/new/documents/E2226AAA").json()["review"]["status"] == "accepted"
    assert paul.get("/api/new/documents/E2226AAA").json()["review"]["status"] == "unreviewed"


def test_a_reviewers_edits_still_save(site):
    """Only the verdict is separated."""
    app, _ = site
    john = client(app, "john")
    accept(john, "E2226AAA", text="the corrected transcription")
    assert john.get("/api/new/documents/E2226AAA").json()["transcription"]["text"] == \
        "the corrected transcription"


# ---------- the admin's accept is the one that releases the work ----------


def test_a_reviewer_cannot_make_a_document_exportable(site):
    """The point of the split."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")

    def export_count():
        _, db_path = site
        with db.connect(db_path) as conn:
            return len(db.get_new_documents_for_export(conn, "E2226"))

    assert export_count() == 0
    accept(john, "E2226AAA")
    accept(john, "E2226AAB")
    assert export_count() == 0, "a reviewer's accept released work into the export"

    accept(paul, "E2226AAA")
    assert export_count() == 1, "the admin's accept did not release the document"


def test_the_admin_accepting_does_not_depend_on_the_reviewer_having_accepted(site):
    """An admin can accept work the reviewer never marked done -- they are separate judgements, not
    a sequence, and an admin must not be blocked by a reviewer who left something untouched."""
    app, _ = site
    paul = client(app, "paul")
    accept(paul, "E2226AAA")
    _, db_path = site
    with db.connect(db_path) as conn:
        assert len(db.get_new_documents_for_export(conn, "E2226")) == 1
    assert paul.post("/api/folder-exports/new/E2226").status_code == 409


# ---------- the round trip a reviewer actually makes ----------


def test_a_reviewer_who_accepts_every_document_can_hand_the_folder_back(site):
    """The whole reported failure in one test: accept everything, press Send to reviewer, and have
    it go."""
    app, _ = site
    john = client(app, "john")
    for doc in ("E2226AAA", "E2226AAB"):
        assert accept(john, doc).status_code == 200

    sent = john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new",
                                             "note": "all done"})

    assert sent.status_code == 200, sent.text
    assert [f["status"] for f in john.get("/api/my/folders").json()["folders"]] == ["submitted"]


def test_the_progress_bar_follows_the_reviewers_own_accepts(site):
    """Shown on both screens: the reviewer needs to see how far they are, and the admin needs to see
    it without opening the folder."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    accept(john, "E2226AAA")

    for who in (john, paul):
        [folder] = [f for f in who.get("/api/new/folders").json() if f["folder_id"] == "E2226"]
        assert folder["progress"]["accepted"] == 1, "the bar did not move"
        assert folder["progress"]["percent"] == 50
        assert folder["progress"]["label"] == "1 of 2 accepted"


def test_submitting_pings_the_admin_who_assigned_the_folder(site):
    """The point of submitting."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    for doc in ("E2226AAA", "E2226AAB"):
        accept(john, doc)
    assert not paul.get("/api/notifications").json()["items"], "notified before anything happened"

    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new", "note": "all done"})

    bell = paul.get("/api/notifications").json()
    assert bell["unread"] >= 1, "the admin was not told the folder came back"
    [item] = [i for i in bell["items"] if i["folder_id"] == "E2226"]
    assert item["action"] == "submitted"
    assert item["by"] == "John"
    assert item["note"] == "all done", "the reviewer's note did not travel with it"


def test_the_reviewer_is_not_pinged_by_their_own_submission(site):
    """Being told what you just did is how a badge stops being read."""
    app, _ = site
    john = client(app, "john")
    for doc in ("E2226AAA", "E2226AAB"):
        accept(john, doc)
    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new", "note": "done"})

    # The assignment Paul made is still there and still unread; what must not appear is John's own
    # submission coming back to him as news.
    actions = {i["action"] for i in john.get("/api/notifications").json()["items"]}
    assert "submitted" not in actions


def test_a_folder_with_one_document_still_untouched_is_refused(site):
    """The gate still has to mean something."""
    app, _ = site
    john = client(app, "john")
    accept(john, "E2226AAA")

    refused = john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new"})

    assert refused.status_code == 409
    assert "1 of 2" in refused.json()["detail"]


def test_the_admin_still_has_to_do_their_own_review_before_approving(site):
    """A reviewer accepting everything moves the bar; it does not release the work."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    for doc in ("E2226AAA", "E2226AAB"):
        accept(john, doc)
    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new"})

    refused = paul.post("/api/team/review", json={"folder_id": "E2226", "workflow": "new",
                                                  "status": "approved"})

    assert refused.status_code == 409, refused.text


# ---------- the two numbers an administrator reads off the Team tab ----------


def test_turnaround_is_measured_end_to_end_through_the_site(site):
    """Not from rows written by hand: assign through the API, accept every document through the
    API, submit through the API, and read the hours back off /api/team/stats."""
    app, db_path = site
    john, paul = client(app, "john"), client(app, "paul")
    for doc in ("E2226AAA", "E2226AAB"):
        assert accept(john, doc).status_code == 200
    assert john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new",
                                             "note": "done"}).status_code == 200

    # Six and a half hours between being given it and handing it back.
    with db.connect(db_path) as conn:
        conn.execute("UPDATE folder_assignments SET assigned_at = '2026-09-01T09:00:00'")
        conn.execute("UPDATE folder_events SET at = '2026-09-01T15:30:00' "
                     "WHERE action = 'submitted'")

    [row] = [c for c in paul.get("/api/team/stats").json()["contributors"] if c["name"] == "John"]
    assert row["avg_turnaround_hours"] == 6.5
    assert row["submissions"] == 1


def test_a_reviewer_who_finishes_a_folder_can_hand_it_back(site):
    """Accept everything, press Send to reviewer, and have it actually go."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    for doc in ("E2226AAA", "E2226AAB"):
        accept(john, doc)

    sent = john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new"})

    assert sent.status_code == 200, sent.text
    assert [f["status"] for f in john.get("/api/my/folders").json()["folders"]] == ["submitted"]
    # and the administrator sees it waiting, which is the point of sending it
    [row] = [r for r in paul.get("/api/team/roster").json()["roster"] if r["folder_id"] == "E2226"]
    assert row["status"] == "submitted"


def test_turnaround_stays_blank_until_something_is_actually_submitted(site):
    """Nought hours would read as the fastest person on the list."""
    app, _ = site
    john, paul = client(app, "john"), client(app, "paul")
    accept(john, "E2226AAA")

    [row] = [c for c in paul.get("/api/team/stats").json()["contributors"] if c["name"] == "John"]
    assert row["avg_turnaround_hours"] is None


# ---------- an approval only holds while the folder is still fully accepted ----------


def _approved_folder(app, db_path):
    """John hands E2226 in, Paul accepts both documents and approves it."""
    john, paul = client(app, "john"), client(app, "paul")
    for doc in ("E2226AAA", "E2226AAB"):
        accept(john, doc)
    john.post("/api/my/submit", json={"folder_id": "E2226", "workflow": "new"})
    for doc in ("E2226AAA", "E2226AAB"):
        accept(paul, doc)
    approved = paul.post("/api/team/review", json={"folder_id": "E2226", "workflow": "new",
                                                   "status": "approved"})
    assert approved.status_code == 200, approved.text
    return john, paul


def test_un_accepting_a_document_takes_the_folder_out_of_approved(site):
    """Reported: the card read Approved over a footer saying 3 of 4 accepted."""
    app, db_path = site
    john, paul = _approved_folder(app, db_path)

    accept(paul, "E2226AAA", status="unreviewed")

    [row] = [r for r in paul.get("/api/team/roster").json()["roster"]
             if r["folder_id"] == "E2226"]
    assert row["status"] == "submitted", "the folder is still marked approved"


def test_the_reviewer_sees_it_back_with_the_reviewer(site):
    app, db_path = site
    john, paul = _approved_folder(app, db_path)

    accept(paul, "E2226AAA", status="unreviewed")

    assert [f["status"] for f in john.get("/api/my/folders").json()["folders"]] == ["submitted"]


def test_the_folder_card_stops_saying_approved(site):
    app, db_path = site
    john, paul = _approved_folder(app, db_path)

    accept(paul, "E2226AAA", status="unreviewed")

    [card] = [f for f in paul.get("/api/new/folders").json() if f["folder_id"] == "E2226"]
    assert card["assignment_status"] == "submitted"


def test_an_approval_that_still_holds_is_left_alone(site):
    """Editing an accepted document without changing its verdict must not undo the approval."""
    app, db_path = site
    john, paul = _approved_folder(app, db_path)

    accept(paul, "E2226AAA", text="a small correction")

    [row] = [r for r in paul.get("/api/team/roster").json()["roster"]
             if r["folder_id"] == "E2226"]
    assert row["status"] == "approved"


def test_the_withdrawal_says_why_in_the_changelog(site):
    """An approval disappearing without explanation is the kind of thing people rediscover by
    argument."""
    app, db_path = site
    john, paul = _approved_folder(app, db_path)

    accept(paul, "E2226AAA", status="unreviewed")

    with db.connect(db_path) as conn:
        notes = [e["note"] for e in accounts.folder_history(conn, "E2226", "new")]
    assert any("approval withdrawn" in (n or "") for n in notes), notes
