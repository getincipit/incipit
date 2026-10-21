"""Approval follows the documents, so nobody presses a button that could only say yes."""

from pathlib import Path

import pytest

from incipit import accounts, collab, db
from conftest import insert_rows

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
WORKSPACE = (STATIC / "workspace.js").read_text(encoding="utf-8")


@pytest.fixture
def team(tmp_path, monkeypatch):
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    conn = db.open_connection(path)
    accounts.init_accounts(conn)
    collab.init_collab(conn)
    paul = accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
    john = accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    insert_rows(conn,"INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                 "VALUES ('b1', 'now', 3, 'E2002')")
    for suffix in ("AAA", "AAB", "AAC"):
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                     "VALUES (?, 'b1', 'drafted', 'unreviewed')", (f"E2002{suffix}",))
    accounts.assign_folder(conn, "E2002", "new", john, paul)
    accounts.set_folder_status(conn, "E2002", "new", "submitted", accounts.get_user(conn, john))
    return conn, paul


def accept(conn, *document_ids):
    for document_id in document_ids:
        conn.execute("UPDATE new_documents SET review_status = 'accepted' WHERE document_id = ?",
                     (document_id,))
        db.set_document_review(conn, document_id, "new", "admin", "accepted", "Paul")


def status(conn):
    return accounts.get_assignment(conn, "E2002", "new")["status"]


def test_the_last_acceptance_approves_the_folder(team):
    conn, paul = team
    accept(conn, "E2002AAA", "E2002AAB", "E2002AAC")

    assert collab.settle_approval(conn, "E2002AAC", "new", paul) is True
    assert status(conn) == "approved"


def test_a_folder_part_way_through_is_left_alone(team):
    conn, paul = team
    accept(conn, "E2002AAA")

    assert collab.settle_approval(conn, "E2002AAA", "new", paul) is False
    assert status(conn) == "submitted"


def test_a_flagged_document_holds_the_approval_back(team):
    conn, paul = team
    accept(conn, "E2002AAA", "E2002AAB")
    conn.execute("UPDATE new_documents SET review_status = 'flagged' WHERE document_id = 'E2002AAC'")

    assert collab.settle_approval(conn, "E2002AAC", "new", paul) is False
    assert status(conn) == "submitted"


def test_taking_one_acceptance_back_withdraws_the_approval(team):
    conn, paul = team
    accept(conn, "E2002AAA", "E2002AAB", "E2002AAC")
    collab.settle_approval(conn, "E2002AAC", "new", paul)

    conn.execute("UPDATE new_documents SET review_status = 'unreviewed' "
                 "WHERE document_id = 'E2002AAB'")
    db.set_document_review(conn, "E2002AAB", "new", "admin", "unreviewed", "Paul")

    assert collab.settle_approval(conn, "E2002AAB", "new", paul) is True
    assert status(conn) == "submitted"


def test_a_folder_nobody_submitted_is_not_approved_behind_their_back(team):
    """A folder still with the reviewer is theirs until they hand it in."""
    conn, paul = team
    accounts.set_folder_status(conn, "E2002", "new", "assigned", accounts.get_user(conn, paul))
    accept(conn, "E2002AAA", "E2002AAB", "E2002AAC")

    assert collab.settle_approval(conn, "E2002AAC", "new", paul) is False
    assert status(conn) == "assigned"


def test_the_approval_is_written_into_the_folders_history(team):
    conn, paul = team
    accept(conn, "E2002AAA", "E2002AAB", "E2002AAC")
    collab.settle_approval(conn, "E2002AAC", "new", paul)

    history = accounts.folder_history(conn, "E2002", "new")
    assert [h for h in history if h["action"] == "approved"], "nothing says why it changed"


# ---------- and so the button is gone ----------


def test_neither_view_offers_an_approve_button():
    assert "btn-approve" not in APP
    assert "btn-approve" not in WORKSPACE
    assert ">Approve</button>" not in APP + WORKSPACE


def test_an_exported_folder_offers_nothing_at_all():
    """Completed is the end of the line: no sending back what has already gone out."""
    start = APP.index("function renderAdminFolderActions(")
    block = APP[start:APP.index("\n  }", start)]

    assert "!fullyExported(f)" in block


def test_sending_back_is_still_offered_before_it_goes_out():
    start = APP.index("function renderAdminFolderActions(")
    block = APP[start:APP.index("\n  }", start)]

    assert "btn-sendback" in block
    assert '"submitted"' in block and '"approved"' in block


def test_a_folder_accepted_before_it_was_submitted_is_approved_at_startup(team):
    """Reported in the 2026-09-30 smoke pass: E2080 had all 23 accepted but stayed submitted,
    because every acceptance landed before the reviewer handed it in."""
    conn, _ = team
    accept(conn, "E2002AAA", "E2002AAB", "E2002AAC")

    assert collab.approve_ready_submissions(conn) == 1
    assert status(conn) == "approved"


def test_the_startup_pass_leaves_an_unfinished_folder_submitted(team):
    conn, _ = team
    accept(conn, "E2002AAA")

    collab.approve_ready_submissions(conn)
    assert status(conn) == "submitted"
