"""Comments replaced flagging: the folder's document list carries the count, and no flag control."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from conftest import insert_rows

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export"), str(tmp_path / "logs"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        insert_rows(conn,"INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, "
                     "folder_id) VALUES ('b1', 'now', 1, 'E2002')")
        for doc_id in ("E2002AAA", "E2002AAB"):
            insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                         f"VALUES ('{doc_id}', 'b1', 'drafted', 'unreviewed')")
    client = TestClient(app)
    client.post("/login", data={"username": "paul", "password": "paul-password"},
                follow_redirects=False)
    return client, db_path


def rows(client):
    return {row["document_id"]: row for row in client.get("/api/new/documents").json()}


# ---------- the counts the list draws ----------


def test_a_document_with_no_comments_counts_none(site):
    client, _ = site

    assert rows(client)["E2002AAA"]["comments"] == 0
    assert rows(client)["E2002AAA"]["open_comments"] == 0


def test_the_list_says_how_many_comments_a_document_carries(site):
    client, db_path = site
    with db.connect(db_path) as conn:
        author = accounts.get_user_by_name(conn, "paul")["id"]
        collab.add_comment(conn, "E2002AAA", "new", author, "which date is right?")
        collab.add_comment(conn, "E2002AAA", "new", author, "and whose signature is this?")

    assert rows(client)["E2002AAA"]["comments"] == 2


def test_a_resolved_comment_still_counts_but_is_not_open(site):
    """The chip says how many comments there are; only an unresolved one is still asking."""
    client, db_path = site
    with db.connect(db_path) as conn:
        author = accounts.get_user_by_name(conn, "paul")["id"]
        first = collab.add_comment(conn, "E2002AAA", "new", author, "which date is right?")
        collab.add_comment(conn, "E2002AAA", "new", author, "and whose signature is this?")
        collab.resolve_comment(conn, first, author)

    row = rows(client)["E2002AAA"]
    assert (row["comments"], row["open_comments"]) == (2, 1)


def test_one_documents_comments_do_not_count_against_another(site):
    client, db_path = site
    with db.connect(db_path) as conn:
        author = accounts.get_user_by_name(conn, "paul")["id"]
        collab.add_comment(conn, "E2002AAA", "new", author, "which date is right?")

    assert rows(client)["E2002AAB"]["comments"] == 0


def test_the_counts_are_read_in_one_query_for_the_whole_queue(site):
    """A count per row is a database round trip per document, and a folder holds hundreds."""
    client, db_path = site
    with db.connect(db_path) as conn:
        author = accounts.get_user_by_name(conn, "paul")["id"]
        collab.add_comment(conn, "E2002AAA", "new", author, "one")
        collab.add_comment(conn, "E2002AAB", "new", author, "two")
        counts = collab.comment_counts(conn, "new")

    assert counts == {"E2002AAA": {"total": 1, "open": 1},
                      "E2002AAB": {"total": 1, "open": 1}}


def test_a_reviewers_list_carries_the_comments_too(site):
    """Comments are how either role says a document needs another look, so both are sent them."""
    client, db_path = site
    with db.connect(db_path) as conn:
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        author = accounts.get_user_by_name(conn, "paul")["id"]
        collab.add_comment(conn, "E2002AAA", "new", author, "which date is right?")
        accounts.assign_folder(conn, "E2002", "new", accounts.get_user_by_name(conn, "john")["id"],
                               author)
    client.post("/login", data={"username": "john", "password": "john-password"},
                follow_redirects=False)

    assert rows(client)["E2002AAA"]["comments"] == 1


# ---------- what the chip renders ----------


def chip(doc):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = APP.index("  function commentChip(")
    harness = (
        "const escapeHtml = (s) => String(s == null ? '' : s);"
        "const COMMENT_ICON = '<svg/>';"
        + APP[start:APP.index("\n  }", start) + 4]
        + f"console.log(commentChip({json.dumps(doc)}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return " ".join(done.stdout.split())


def test_the_chip_shows_the_number_of_comments(site):
    assert '<span class="queue-comment-count">3</span>' in chip(
        {"comments": 3, "open_comments": 1})


def test_a_document_with_no_comments_still_gets_a_chip_but_no_number(site):
    """Every row is the same height and the same shape, commented or not."""
    html = chip({"comments": 0, "open_comments": 0})

    assert "queue-comments" in html and "is-empty" in html
    assert "queue-comment-count" not in html


def test_an_open_comment_marks_the_chip_apart_from_a_resolved_one(site):
    assert "has-open" in chip({"comments": 1, "open_comments": 1})
    assert "has-open" not in chip({"comments": 1, "open_comments": 0})


def test_a_hundred_comments_do_not_widen_the_row(site):
    assert ">99+<" in chip({"comments": 140, "open_comments": 2})


def test_a_slow_comment_request_cannot_overwrite_a_later_peek(site):
    """Both successful and failed old requests must stand down after another dialog is opened."""
    start = APP.index("async function openCommentPeek(")
    body = APP[start:APP.index("function commentPeekRow", start)]

    assert body.count("request !== peekRequest") == 2


# ---------- flagging is gone ----------


def test_the_document_screen_no_longer_offers_a_flag(site):
    """A document needing another look gets a comment, which says what is wrong with it."""
    assert 'id="btn-flag"' not in INDEX
    assert "btn-flag" not in APP


def test_the_queue_filters_on_comments_where_it_filtered_on_flags(site):
    assert 'data-filter="commented"' in INDEX
    assert 'data-filter="flagged"' not in INDEX


def test_a_status_left_behind_by_the_old_button_reads_as_unreviewed(site):
    """Nothing in the database is flagged, but a record that was must not draw a broken badge."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = APP.index("  function badgeLabel(")
    harness = (
        "const classes = new Set(['role-admin']);"
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + APP[start:APP.index("\n  }", start) + 4]
        + "console.log(badgeLabel('flagged'));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "Unreviewed"
