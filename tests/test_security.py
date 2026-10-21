"""Adversarial tests: deliberately trying to break the workspace."""

import concurrent.futures
import threading

import pytest
import conftest
from conftest import insert_rows, tiny_image_bytes
from fastapi.testclient import TestClient

from incipit import accounts, db, review_server, scoping
from incipit.config import Settings
from incipit.storage import BucketStorage


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer")
    return app, db_path


def client(app, username=None, password=None) -> TestClient:
    c = TestClient(app)
    c.cookies.clear()          # conftest signs clients in; these must start anonymous
    if username:
        c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    return c


def uid(db_path, username):
    with db.connect(db_path) as conn:
        return accounts.get_user_by_name(conn, username)["id"]


def assign(app, db_path, folder, username):
    """Assigns `folder` (created first if nothing has made it yet) to `username`."""
    with db.connect(db_path) as conn:
        conftest.seed_folder(conn, folder)
    paul = client(app, "paul", "paul-password")
    paul.post("/api/team/assign", json={
        "folder_id": folder, "workflow": "new", "reviewer_id": uid(db_path, username)
    })


def seed_folder(db_path, folder_id="E2002", workflow="new", n=2, review_status="accepted"):
    """A folder with real documents in a given review state."""
    from incipit import db as _db
    with _db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES (?, 'now', ?, ?)", (f"B-{folder_id}", n, folder_id),
        )
        for i in range(n):
            suffix = chr(ord("A") + i // 26) + chr(ord("A") + i % 26)
            doc = f"{folder_id}A{suffix}"
            insert_rows(conn,
                "INSERT OR REPLACE INTO new_documents (document_id, batch_id, status, review_status) "
                "VALUES (?, ?, 'drafted', ?)", (doc, f"B-{folder_id}", review_status),
            )


# ---------- horizontal escalation: one reviewer reaching another's work ----------


def test_a_reviewer_cannot_reach_another_reviewers_folder_by_guessing_its_id(site):
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    jack = client(app, "jack", "jack-password")

    assert jack.get("/api/my/folders").json()["folders"] == []
    assert jack.post("/api/my/submit", json={"folder_id": "E2002", "workflow": "new"}).status_code == 403


def test_a_reviewer_cannot_open_image_or_edit_another_reviewers_document_by_id(site):
    app, db_path = site
    seed_folder(db_path, "E2002")
    assign(app, db_path, "E2002", "john")
    jack = client(app, "jack", "jack-password")
    payload = {
        "reviewed_text": "not mine", "review_status": "accepted", "marginal_notes": [],
        "places": [], "names": [], "challenges": [],
    }

    assert jack.get("/api/new/documents/E2002AAA").status_code == 403
    assert jack.get("/api/new/documents/E2002AAA/pages/0/image").status_code == 403
    assert jack.post("/api/new/documents/E2002AAA/review", json=payload).status_code == 403


def test_an_admin_can_still_open_a_document_without_an_assignment(site):
    app, db_path = site
    seed_folder(db_path, "E2002")

    assert client(app, "paul", "paul-password").get(
        "/api/new/documents/E2002AAA").status_code == 200


def test_a_reviewer_cannot_submit_on_behalf_of_someone_else(site):
    """There is no actor parameter to forge -- the actor is the session."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    jack = client(app, "jack", "jack-password")

    response = jack.post("/api/my/submit", json={
        "folder_id": "E2002", "workflow": "new",
        "reviewer_id": uid(db_path, "john"), "user_id": uid(db_path, "john"),
        "actor": "john", "role": "admin",
    })

    assert response.status_code == 403


def test_a_reviewer_cannot_widen_their_own_view_through_query_parameters(site):
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    jack = client(app, "jack", "jack-password")

    for params in ({"reviewer_id": uid(db_path, "john")}, {"all": "true"}, {"role": "admin"},
                   {"user_id": uid(db_path, "john")}, {"folder_id": "E2002"}):
        assert jack.get("/api/my/folders", params=params).json()["folders"] == []


def test_a_reviewers_folder_listing_excludes_folders_they_do_not_hold(site):
    """The listing endpoints are the quiet leak: a reviewer who cannot open E2002 should not be able
    to learn it exists, who has it, or how far along it is."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    jack = client(app, "jack", "jack-password")

    body = jack.get("/api/new/folders").text
    assert "E2002" not in body


def test_a_reviewer_cannot_claim_another_reviewers_folder_through_upload_or_retarget(site):
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    jack_id = uid(db_path, "jack")
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_batches (batch_id, uploaded_at, image_count, uploaded_by) "
            "VALUES ('B-JACK-BLANK', 'now', 0, ?)", (jack_id,),
        )
    jack = client(app, "jack", "jack-password")

    retarget = jack.post(
        "/api/new/batches/B-JACK-BLANK/folder-id", json={"folder_display_id": "E2002"}
    )
    direct = jack.post(
        "/api/new/batches", data={"folder_display_id": "E2002"},
        files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))],
    )

    assert retarget.status_code == 403
    assert direct.status_code == 403
    with db.connect(db_path) as conn:
        assert db.get_batch(conn, "B-JACK-BLANK")["folder_id"] is None


def test_a_reviewer_cannot_operate_on_another_reviewers_split_documents(site):
    app, db_path = site
    seed_folder(db_path, "E2002")
    seed_folder(db_path, "E3003")
    assign(app, db_path, "E2002", "john")
    assign(app, db_path, "E3003", "jack")
    jack = client(app, "jack", "jack-password")

    requests = [
        jack.post("/api/new/suggest-doc-ids", json={"folder_id": "E2002", "count": 1}),
        jack.get("/api/new/merge", params={"document_ids": "E3003AAA,E2002AAA"}),
        jack.post("/api/new/merge", json={"document_ids": ["E3003AAA", "E2002AAA"]}),
        jack.get("/api/new/resplit", params={"document_ids": "E3003AAA,E2002AAA"}),
        jack.post("/api/new/resplit", json={
            "document_ids": ["E3003AAA", "E2002AAA"], "groups": []
        }),
        jack.post("/api/new/folders/draft", params={"folder_id": "E2002"}),
        jack.post("/api/new/documents/E2002AAA/rename", json={"new_display_id": "E2002ZZZ"}),
    ]

    assert {response.status_code for response in requests} == {403}
    with db.connect(db_path) as conn:
        assert db.get_new_document(conn, "E2002AAA") is not None
        assert db.get_new_document(conn, "E2002ZZZ") is None


# ---------- vertical escalation: a reviewer becoming an admin ----------


def test_a_reviewer_cannot_promote_themselves_by_signing_up_again(site):
    app, db_path = site
    with db.connect(db_path) as conn:
        owner_id = accounts.create_owner(conn, "olive", "olive-password")
        code = accounts.create_join_code(conn, accounts.get_user(conn, owner_id), "admin")

    response = client(app).post("/signup", data={
        "username": "john", "password": "john-password", "display_name": "John", "join_code": code,
    }, follow_redirects=False)

    assert response.status_code == 400          # the username is taken; no upgrade path
    with db.connect(db_path) as conn:
        assert accounts.get_user_by_name(conn, "john")["role"] == "reviewer"


def test_the_role_field_in_a_signup_payload_is_not_trusted(site):
    """The role comes from the join code, or is reviewer under self-signup, never from the request."""
    app, db_path = site
    with db.connect(db_path) as conn:
        conn.execute("UPDATE site_settings SET reviewer_self_signup = 1")

    client(app).post("/signup", data={
        "username": "sneaky", "password": "a-password", "display_name": "Sneaky", "role": "admin",
    }, follow_redirects=False)

    with db.connect(db_path) as conn:
        assert accounts.get_user_by_name(conn, "sneaky")["role"] == "reviewer"


def test_admin_only_endpoints_refuse_a_reviewer_on_every_verb(site):
    app, _ = site
    john = client(app, "john", "john-password")

    assert john.get("/api/team/roster").status_code == 403
    assert john.post("/api/team/assign", json={"folder_id": "E1", "reviewer_id": 1}).status_code == 403
    assert john.post("/api/team/review", json={"folder_id": "E1", "status": "approved"}).status_code == 403
    assert john.get("/api/team/roster.csv").status_code == 403


# ---------- session handling ----------


def test_one_reviewers_session_does_not_carry_over_to_another(site):
    app, _ = site
    john = client(app, "john", "john-password")
    jack = client(app, "jack", "jack-password")

    assert john.get("/api/me").json()["username"] == "john"
    assert jack.get("/api/me").json()["username"] == "jack"


def test_deactivating_an_account_severs_a_live_session_immediately(site):
    """Not at cookie expiry -- someone removed from the project loses access now."""
    app, db_path = site
    john = client(app, "john", "john-password")
    assert john.get("/api/me").status_code == 200

    with db.connect(db_path) as conn:
        accounts.set_active(conn, uid(db_path, "john"), False)

    assert john.get("/api/me").status_code == 401


# ---------- injection and traversal ----------


@pytest.mark.parametrize("hostile", [
    "E2002'; DROP TABLE users; --",
    "' OR '1'='1",
    "E2002' UNION SELECT username, password_hash FROM users --",
])
def test_sql_injection_through_a_folder_id_does_nothing(site, hostile):
    """Every query is parameterised."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")

    paul.post("/api/team/assign", json={
        "folder_id": hostile, "workflow": "new", "reviewer_id": uid(db_path, "john")
    })

    with db.connect(db_path) as conn:
        # 3 real accounts plus conftest's testadmin -- the point is that the table still exists and
        # still has its rows, not the exact number.
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 3
        row = accounts.get_assignment(conn, hostile, "new")
    assert row is not None and row["folder_id"] == hostile   # stored as literal text, not executed


@pytest.mark.parametrize("hostile", ["../../../etc/passwd", "..\\..\\windows\\system32", "/etc/shadow"])
def test_a_traversal_shaped_folder_id_cannot_escape_anything(site, hostile):
    """A folder id reaches the filesystem elsewhere in the pipeline, so it must stay inert here."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")

    response = paul.post("/api/team/assign", json={
        "folder_id": hostile, "workflow": "new", "reviewer_id": uid(db_path, "john")
    })

    assert response.status_code in (200, 400)
    john = client(app, "john", "john-password")
    assert john.get("/api/my/folders").status_code == 200      # nothing crashed


def test_an_unknown_workflow_is_refused_rather_than_stored(site):
    """workflow is CHECK-constrained, and the assign path validates before writing."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")

    response = paul.post("/api/team/assign", json={
        "folder_id": "E2002", "workflow": "sideways", "reviewer_id": uid(db_path, "john")
    })

    assert response.status_code >= 400
    with db.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM folder_assignments WHERE workflow='sideways'"
        ).fetchone()[0] == 0


def test_a_status_outside_the_lifecycle_is_refused(site):
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    paul = client(app, "paul", "paul-password")

    assert paul.post("/api/team/review", json={
        "folder_id": "E2002", "status": "exported"
    }).status_code == 400


def test_assigning_to_a_user_who_does_not_exist_is_refused(site):
    app, _ = site
    paul = client(app, "paul", "paul-password")

    response = paul.post("/api/team/assign", json={
        "folder_id": "E2002", "workflow": "new", "reviewer_id": 999999
    })

    assert response.status_code >= 400


def test_a_hostile_note_is_stored_verbatim_and_escaped_by_the_client(site):
    """Notes are shown to other people."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    paul = client(app, "paul", "paul-password")
    payload = "<script>alert('xss')</script>"

    paul.post("/api/team/review", json={"folder_id": "E2002", "status": "returned", "note": payload})

    history = paul.get("/api/team/history", params={"folder_id": "E2002"}).json()["history"]
    assert history[-1]["note"] == payload          # unchanged in storage
    # The client escapes on render (workspace.js `esc`), which is where escaping belongs.


# ---------- concurrency ----------


def test_two_reviewers_working_at_the_same_time_do_not_see_each_other(site):
    """The realistic load: several people signed in at once."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    assign(app, db_path, "E2003", "jack")
    john = client(app, "john", "john-password")
    jack = client(app, "jack", "jack-password")

    def fetch(c):
        return [f["folder_id"] for f in c.get("/api/my/folders").json()["folders"]]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: fetch(c), [john, jack] * 15))

    assert all(r == ["E2002"] for r in results[0::2])
    assert all(r == ["E2003"] for r in results[1::2])


def test_many_simultaneous_logins_all_succeed(site):
    """Each login costs ~60ms of scrypt by design."""
    app, _ = site
    names = [("john", "john-password"), ("jack", "jack-password"), ("paul", "paul-password")] * 5

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        who = list(pool.map(lambda np: client(app, *np).get("/api/me").json()["username"], names))

    assert who == [n for n, _ in names]      # everyone got their own identity, in order


def test_concurrent_assignment_of_one_folder_leaves_exactly_one_owner(site):
    """Two admins assigning the same folder at the same moment."""
    app, db_path = site
    john_id, jack_id = uid(db_path, "john"), uid(db_path, "jack")
    paul = client(app, "paul", "paul-password")

    def do(reviewer_id):
        return paul.post("/api/team/assign", json={
            "folder_id": "E2002", "workflow": "new", "reviewer_id": reviewer_id
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(do, [john_id, jack_id] * 10))

    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM folder_assignments WHERE folder_id='E2002' AND workflow='new'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["assigned_to"] in (john_id, jack_id)


def test_the_changelog_records_every_concurrent_action(site):
    """Append-only under contention: nothing may be lost to a race, because the changelog is the
    record of what happened and a missing line is a false record."""
    app, db_path = site
    seed_folder(db_path, "E2002")
    assign(app, db_path, "E2002", "john")
    john = client(app, "john", "john-password")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(
            lambda i: john.post("/api/my/submit", json={"folder_id": "E2002", "note": f"pass {i}"}),
            range(12),
        ))

    history = client(app, "paul", "paul-password").get(
        "/api/team/history", params={"folder_id": "E2002"}
    ).json()["history"]
    assert len([e for e in history if e["action"] == "submitted"]) == 12


def test_a_reader_is_not_blocked_by_a_writer(site):
    """WAL plus short transactions: someone reading their dashboard must not wait on someone else
    writing."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    john = client(app, "john", "john-password")
    paul = client(app, "paul", "paul-password")
    stop = threading.Event()

    def keep_writing():
        i = 0
        while not stop.is_set() and i < 40:
            paul.post("/api/team/review", json={
                "folder_id": "E2002", "status": "returned", "note": f"note {i}"
            })
            i += 1

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        for _ in range(20):
            assert john.get("/api/my/folders").status_code == 200
    finally:
        stop.set()
        writer.join(timeout=10)


# ---------- the scoping helpers under hostile input ----------


def test_scoping_never_widens_a_view_for_a_malformed_row(site):
    """A row missing its folder must be excluded, not included."""
    app, db_path = site
    assign(app, db_path, "E2002", "john")
    with db.connect(db_path) as conn:
        john = accounts.get_user_by_name(conn, "john")
        docs = [{"document_id": ""}, {"document_id": None}, {}, {"document_id": "E2003AAA"}]
        assert scoping.filter_documents(conn, john, docs) == []


def test_a_reviewer_with_no_assignments_sees_nothing_rather_than_everything(site):
    """The empty case is where a filter most often inverts -- an empty allow-list meaning "allow
    all" is a classic."""
    app, db_path = site
    with db.connect(db_path) as conn:
        jack = accounts.get_user_by_name(conn, "jack")
        assert scoping.visible_folder_ids(conn, jack, "new") == set()
        assert scoping.filter_folders(conn, jack, [{"folder_id": "E2002"}]) == []
        assert scoping.filter_documents(conn, jack, [{"document_id": "E2002AAA"}]) == []


# ---------- attacking the collaboration features ----------


def test_resolving_a_comment_that_does_not_exist_is_a_404_not_a_crash(site):
    app, _ = site
    assert client(app, "paul", "paul-password").post("/api/comments/999999/resolve").status_code == 404


def test_a_reviewer_cannot_read_contributor_statistics(site):
    """Throughput and return rates for every reviewer is supervision data, not peer data."""
    assert client(site[0], "john", "john-password").get("/api/team/stats").status_code == 403


def test_a_reviewer_is_not_told_why_a_folder_they_do_not_hold_is_unfinished(site):
    """Regression for a real ordering bug: readiness was checked before permission, so asking about
    someone else's folder answered "3 of 4 documents still need reviewing" -- the state of their
    work -- and only refused afterwards."""
    app, db_path = site
    seed_folder(db_path, "E2002", review_status="unreviewed")
    assign(app, db_path, "E2002", "john")

    response = client(app, "jack", "jack-password").post(
        "/api/my/submit", json={"folder_id": "E2002", "workflow": "new"})

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "not assigned to you" in detail
    for leak in ("document", "review", "of 4", "unreviewed"):
        assert leak not in detail, f"the refusal leaked folder state: {detail!r}"


def test_folder_progress_cannot_be_forged_through_the_api(site):
    """Progress is computed from the documents on every read, so there is no field to poison -- a
    request cannot claim a folder is finished."""
    app, db_path = site
    seed_folder(db_path, "E2002", n=3, review_status="unreviewed")
    assign(app, db_path, "E2002", "john")
    john = client(app, "john", "john-password")

    forged = john.post("/api/my/submit", json={
        "folder_id": "E2002", "workflow": "new",
        "progress": {"total": 3, "accepted": 3, "outstanding": 0},   # ignored
    })

    assert forged.status_code == 409
    assert "3 of 3" in forged.json()["detail"]


def test_an_admin_cannot_approve_an_empty_folder_through_the_api(site):
    """The original defect: E2002 sat at "approved" holding zero documents, so the roster reported
    finished work that did not exist."""
    app, db_path = site
    with db.connect(db_path) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B-E2002', 'now', 0, 'E2002')")
    assign(app, db_path, "E2002", "john")

    response = client(app, "paul", "paul-password").post(
        "/api/team/review", json={"folder_id": "E2002", "workflow": "new", "status": "approved"})

    assert response.status_code == 409
    assert "nothing to approve" in response.json()["detail"]


def test_notifications_never_name_a_folder_the_reader_cannot_see(site):
    """A notification carries a folder id and a note."""
    app, db_path = site
    seed_folder(db_path, "E2002")
    seed_folder(db_path, "E2003")
    assign(app, db_path, "E2002", "john")
    assign(app, db_path, "E2003", "jack")
    john = client(app, "john", "john-password")
    john.post("/api/my/submit", json={"folder_id": "E2002", "workflow": "new"})
    client(app, "jack", "jack-password").post("/api/my/submit", json={"folder_id": "E2003", "workflow": "new"})

    folders = {i["folder_id"] for i in john.get("/api/notifications").json()["items"]}

    assert "E2003" not in folders


# ---------- and one person's drafting run says nothing about anyone else's ----------


def pending_document(db_path, folder_id, suffix="AAA"):
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES (?, 'now', 1, ?)", (f"B-{folder_id}", folder_id))
        insert_rows(conn,
            "INSERT OR REPLACE INTO new_documents (document_id, batch_id, status) "
            "VALUES (?, ?, 'split_confirmed')", (f"{folder_id}{suffix}", f"B-{folder_id}"))
    return f"{folder_id}{suffix}"


def test_draft_pending_counts_the_reviewer_s_own_documents_and_not_the_queue(site, monkeypatch):
    """Reported in review: the count was the global queue's before and after, so a reviewer whose
    own documents all drafted was told nothing had been drafted whenever somebody else had work
    waiting."""
    app, db_path = site
    mine = pending_document(db_path, "E2002")
    pending_document(db_path, "E3003")          # Jack's, and it stays pending throughout
    assign(app, db_path, "E2002", "john")
    assign(app, db_path, "E3003", "jack")

    def draft_mine(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            conn.execute("UPDATE new_documents SET status = 'drafted' WHERE document_id = ?",
                         (mine,))

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", draft_mine)

    body = client(app, "john", "john-password").post("/api/new/draft-pending").json()

    assert body["drafted"] == 1


# ---------- and two people replacing the same folder at once ----------


def test_two_overlapping_replacements_leave_one_folder_not_two(site):
    """Reported in review: each request snapshotted the folder before the other purged, so each
    purged only the batch ids it had seen and both replacement batches survived."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")
    paul.post("/api/new/batches", files=[("files", ("a.jpg", tiny_image_bytes(), "image/jpeg"))],
              data={"folder_display_id": "E9001"})
    with db.connect(db_path) as conn:
        insert_rows(conn, "INSERT INTO new_documents (document_id, batch_id, status) "
                          "SELECT 'E9001AAA', batch_id, 'drafted' FROM new_batches")

    def replace(name):
        return client(app, "paul", "paul-password").post(
            "/api/new/batches",
            files=[("files", (name, tiny_image_bytes(), "image/jpeg"))],
            data={"folder_display_id": "E9001", "replace": "true"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(replace, ["one.jpg", "two.jpg"]))

    assert [r.status_code for r in responses] == [200, 200]
    with db.connect(db_path) as conn:
        surviving = [row["batch_id"] for row in
                     conn.execute("SELECT b.batch_id FROM new_batches b JOIN folders f "
                                  "ON f.folder_id = b.folder_id WHERE f.display_id = 'E9001'")]
    assert len(surviving) == 1, f"{len(surviving)} replacement batches are live at once"


def test_a_replacement_answers_before_the_next_one_can_purge_it(site, monkeypatch):
    """The overlap above returned 404 about one run in five: the first upload built its response
    after releasing the folder lock, by which time the second had purged its batch."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")
    paul.post("/api/new/batches", files=[("files", ("a.jpg", tiny_image_bytes(), "image/jpeg"))],
              data={"folder_display_id": "E9001"})
    with db.connect(db_path) as conn:
        insert_rows(conn, "INSERT INTO new_documents (document_id, batch_id, status) "
                          "SELECT 'E9001AAA', batch_id, 'drafted' FROM new_batches")

    # The first replacement's response is slow to build, which is when the race used to open.
    real_get_batch, slowed = db.get_batch, []

    def slow_first_answer(conn, batch_id):
        if not slowed:
            slowed.append(batch_id)
            import time
            time.sleep(0.5)
        return real_get_batch(conn, batch_id)

    def replace(name, delay):
        import time
        time.sleep(delay)
        return client(app, "paul", "paul-password").post(
            "/api/new/batches",
            files=[("files", (name, tiny_image_bytes(), "image/jpeg"))],
            data={"folder_display_id": "E9001", "replace": "true"})

    monkeypatch.setattr(db, "get_batch", slow_first_answer)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(replace, "one.jpg", 0)
        second = pool.submit(replace, "two.jpg", 0.2)
        responses = [first.result(), second.result()]

    assert [r.status_code for r in responses] == [200, 200]


def test_the_surviving_replacement_still_has_its_scans(site, tmp_path):
    """Whichever upload won, its own images must not have gone with the one it replaced."""
    app, db_path = site
    paul = client(app, "paul", "paul-password")
    paul.post("/api/new/batches", files=[("files", ("a.jpg", tiny_image_bytes(), "image/jpeg"))],
              data={"folder_display_id": "E9001"})
    with db.connect(db_path) as conn:
        insert_rows(conn, "INSERT INTO new_documents (document_id, batch_id, status) "
                          "SELECT 'E9001AAA', batch_id, 'drafted' FROM new_batches")

    def replace(name):
        return client(app, "paul", "paul-password").post(
            "/api/new/batches",
            files=[("files", (name, tiny_image_bytes(), "image/jpeg"))],
            data={"folder_display_id": "E9001", "replace": "true"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(replace, ["one.jpg", "two.jpg"]))

    storage = BucketStorage(f"file://{(tmp_path / 'bucket').as_posix()}")
    with db.connect(db_path) as conn:
        keys = [row["bucket_key"] for row in conn.execute(
            "SELECT i.bucket_key AS bucket_key FROM new_batch_images i "
            "JOIN new_batches b ON b.batch_id = i.batch_id "
            "JOIN folders f ON f.folder_id = b.folder_id WHERE f.display_id = 'E9001'")]
    assert keys and all(storage.exists(key) for key in keys)
