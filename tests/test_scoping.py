"""Narrowing the shared workspace to one person's view."""


import pytest

from incipit import accounts, db, scoping
from conftest import insert_rows


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    c = db.open_connection(path)
    accounts.init_accounts(c)
    return c


@pytest.fixture
def team(conn, monkeypatch):
    ids = {
        "paul": accounts.create_user(conn, "paul", "paul-password", "Paul", "admin"),
        "john": accounts.create_user(conn, "john", "john-password", "John", "reviewer"),
        "jack": accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer"),
    }
    accounts.assign_folder(conn, "E2002", "new", ids["john"], ids["paul"])
    accounts.assign_folder(conn, "E2003", "new", ids["jack"], ids["paul"])
    return ids


def user(conn, uid):
    return accounts.get_user(conn, uid)


FOLDERS = [{"folder_id": "E2002"}, {"folder_id": "E2003"}, {"folder_id": "E2004"}]


# ---------- folders ----------


def test_an_admin_is_not_filtered_at_all(conn, team):
    """None rather than a set of every id: at 150,000 documents, building the full set only to
    intersect it is work with no purpose."""
    assert scoping.visible_folder_ids(conn, user(conn, team["paul"]), "new") is None
    # Folders now carry their progress as well as their assignment, so a card can say "7 of 11
    # accepted" without a second request.
    from incipit import collab
    assert scoping.filter_folders(conn, user(conn, team["paul"]), FOLDERS) == [
        {
            **f,
            **scoping.assignment_summary(conn, f["folder_id"]),
            "progress": scoping.scope_progress(
                collab.folder_progress(conn, f["folder_id"], "new"),
                user(conn, team["paul"]),
                scoping.assignment_summary(conn, f["folder_id"])),
        }
        for f in FOLDERS
    ]


def test_a_reviewer_sees_only_the_folder_assigned_to_them(conn, team):
    john = scoping.filter_folders(conn, user(conn, team["john"]), FOLDERS)
    jack = scoping.filter_folders(conn, user(conn, team["jack"]), FOLDERS)

    assert [f["folder_id"] for f in john] == ["E2002"]
    assert [f["folder_id"] for f in jack] == ["E2003"]


def test_an_unassigned_folder_is_invisible_to_reviewers(conn, team):
    """E2004 belongs to nobody."""
    assert "E2004" not in [f["folder_id"] for f in scoping.filter_folders(conn, user(conn, team["john"]), FOLDERS)]


def test_a_reviewer_keeps_sight_of_a_folder_they_uploaded_before_it_is_assigned(conn, team):
    """Otherwise their own upload would appear to have failed: they would click upload, and the
    folder would simply not be there until an admin happened to act."""
    insert_rows(conn,
        "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_by) "
        "VALUES ('B1', 'now', 3, 'E2999', ?)", (team["john"],)
    )

    visible = scoping.visible_folder_ids(conn, user(conn, team["john"]), "new")

    assert "E2999" in visible
    assert "E2999" not in scoping.visible_folder_ids(conn, user(conn, team["jack"]), "new")


def test_an_uploader_loses_the_folder_once_it_is_assigned_to_somebody_else(conn, team):
    """Reported in review: the upload row is kept for the audit trail, and unioning it in forever
    left the original uploader able to read and redraft a folder reassigned away from them."""
    insert_rows(conn,
        "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_by) "
        "VALUES ('B1', 'now', 3, 'E2999', ?)", (team["john"],)
    )
    accounts.assign_folder(conn, "E2999", "new", team["jack"], team["paul"])

    assert "E2999" not in scoping.visible_folder_ids(conn, user(conn, team["john"]), "new")


def test_an_uploader_still_holds_a_folder_assigned_back_to_them(conn, team):
    insert_rows(conn,
        "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_by) "
        "VALUES ('B1', 'now', 3, 'E2999', ?)", (team["john"],)
    )
    accounts.assign_folder(conn, "E2999", "new", team["john"], team["paul"])

    assert "E2999" in scoping.visible_folder_ids(conn, user(conn, team["john"]), "new")


def test_folders_carry_their_assignment_so_a_dashboard_can_explain_itself(conn, team):
    """A reviewer's list should say "returned -- check the dates", not just name a folder."""
    john, paul = user(conn, team["john"]), user(conn, team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "check the dates on pages 3-5")

    folder = scoping.filter_folders(conn, john, FOLDERS)[0]

    assert folder["assignment_status"] == "returned"
    assert folder["latest_note"] == "check the dates on pages 3-5"
    assert folder["latest_note_by"] == "Paul"
    assert folder["assigned_to_name"] == "John"


def test_an_unassigned_folder_reports_itself_as_such_to_an_admin(conn, team):
    summary = scoping.assignment_summary(conn, "E2004", "new")
    assert summary["assignment_status"] == "unassigned"
    assert summary["assigned_to"] is None


# ---------- pending uploads ----------


def _batch(conn, batch_id, folder_id, uploader):
    insert_rows(conn,
        "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, uploaded_by) "
        "VALUES (?, 'now', 2, ?, ?)", (batch_id, folder_id, uploader),
    )
    return {"batch_id": batch_id, "folder_id": folder_id}


def test_both_admins_see_a_new_upload_and_who_made_it(conn, team):
    """Explicitly asked for: when a reviewer uploads, the admins need to see that a folder arrived
    and whose it is."""
    batches = [_batch(conn, "B1", "E2999", team["john"])]

    seen = scoping.filter_batches(conn, user(conn, team["paul"]), batches)

    assert len(seen) == 1
    assert seen[0]["uploaded_by_name"] == "John"


def test_a_reviewer_sees_only_their_own_uploads(conn, team):
    """Another reviewer's half-split batch is not their business, and acting on it would collide
    with its owner."""
    batches = [_batch(conn, "B1", "E2999", team["john"]), _batch(conn, "B2", "E2888", team["jack"])]

    assert [b["batch_id"] for b in scoping.filter_batches(conn, user(conn, team["john"]), batches)] == ["B1"]
    assert [b["batch_id"] for b in scoping.filter_batches(conn, user(conn, team["jack"]), batches)] == ["B2"]
    assert len(scoping.filter_batches(conn, user(conn, team["paul"]), batches)) == 2


def test_a_batch_uploaded_before_accounts_existed_is_admin_only(conn, team):
    """uploaded_by is NULL for those."""
    batches = [_batch(conn, "OLD", "E1000", None)]

    assert scoping.filter_batches(conn, user(conn, team["john"]), batches) == []
    assert len(scoping.filter_batches(conn, user(conn, team["paul"]), batches)) == 1


# ---------- the review queue ----------


DOCS = [
    {"document_id": "d1", "folder_id": "E2002"}, {"document_id": "d2", "folder_id": "E2002"},
    {"document_id": "d3", "folder_id": "E2003"}, {"document_id": "d4", "folder_id": "E2004"},
]


@pytest.fixture
def stored(conn):
    """DOCS as rows, each in a batch of its folder."""
    for doc in DOCS:
        insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                          "VALUES (?, 'now', 1, ?)", (f"B-{doc['document_id']}", doc["folder_id"]))
        insert_rows(conn, "INSERT INTO new_documents (document_id, batch_id) VALUES (?, ?)",
                    (doc["document_id"], f"B-{doc['document_id']}"))
    conn.commit()


def test_a_reviewers_queue_holds_only_their_folders_documents(conn, team):
    seen = scoping.filter_documents(conn, user(conn, team["john"]), DOCS)
    assert [d["document_id"] for d in seen] == ["d1", "d2"]


def test_the_folder_is_read_from_the_database_when_a_row_omits_it(conn, team, stored):
    """A document must never slip through unfiltered merely because one listing endpoint left the
    folder column out of its SELECT."""
    bare = [{"document_id": d["document_id"]} for d in DOCS]
    seen = scoping.filter_documents(conn, user(conn, team["jack"]), bare)
    assert [d["document_id"] for d in seen] == ["d3"]


def test_a_document_with_no_folder_anywhere_is_hidden_from_a_reviewer(conn, team):
    seen = scoping.filter_documents(conn, user(conn, team["john"]), [{"document_id": "nowhere"}])
    assert seen == []


def test_an_admins_queue_is_untouched(conn, team):
    assert scoping.filter_documents(conn, user(conn, team["paul"]), DOCS) == DOCS


# ---------- the activity banner ----------


def test_a_reviewer_does_not_see_someone_elses_pipeline_running(conn, team):
    """Drafting messages are shared, costed work a reviewer can neither start, stop, nor pay for."""
    entries = [
        {"kind": "drafting", "label": "Transcribing new documents (Qwen)", "owner_id": None},
        {"kind": "uploading", "label": "Uploading E2999", "owner_id": team["john"]},
        {"kind": "uploading", "label": "Uploading E2888", "owner_id": team["jack"]},
    ]

    john = scoping.visible_activity(user(conn, team["john"]), entries)

    assert [e["label"] for e in john] == ["Uploading E2999"]


def test_an_admin_sees_every_running_job(conn, team):
    entries = [{"kind": "drafting", "label": "Transcribing", "owner_id": None}]
    assert scoping.visible_activity(user(conn, team["paul"]), entries) == entries


# ---------- uploading ----------
