"""A folder's display ids run without a hole whenever a document goes away; nothing else moves."""

import json

import pytest

from incipit import accounts, db, new_document, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch, signed_in


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """Three drafted documents in one folder, each with a page and a record."""
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        seed_batch(conn, "B", 3, folder="X074", name="X074_Notes")
        conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = 'B'")
        for index, document_id in enumerate(("X074AAA", "X074AAB", "X074AAC")):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
                "ai_title) VALUES (?, 'B', 'drafted', 'unreviewed', ?, ?)",
                (document_id, f"the text of {document_id}", f"title of {document_id}"))
            key = f"documents/X074/{document_id}/page.jpg"
            storage.write_bytes(key, f"the scan of {document_id}".encode())
            conn.execute(
                "INSERT INTO new_document_pages (document_id, sequence_index, bucket_key) "
                "VALUES (?, ?, ?)", (document_id, index, key))
            new_document.refresh_output_record(conn, document_id, storage)
    return application, settings


def shown(settings):
    """{internal id: display id} for every document."""
    with db.connect(settings.db_path) as conn:
        return {r["document_id"]: r["display_id"] for r in
                conn.execute("SELECT document_id, display_id FROM new_documents")}


def record(settings, document_id):
    raw = BucketStorage(settings.bucket_uri).read_record("X074", document_id)
    return None if raw is None else json.loads(raw)


def delete(application, document_id):
    return signed_in(application).delete(f"/api/new/documents/{document_id}?reviewer=Paul&reason=a+test")


# ---------- the reported fault ----------


def test_deleting_the_first_document_closes_the_gap(folder):
    application, settings = folder

    delete(application, "X074AAA")

    assert sorted(shown(settings).values()) == ["X074AAA", "X074AAB"]


def test_deleting_from_the_middle_closes_the_gap_too(folder):
    application, settings = folder

    delete(application, "X074AAB")

    assert sorted(shown(settings).values()) == ["X074AAA", "X074AAB"]


def test_deleting_the_last_one_needs_no_renumbering(folder):
    application, settings = folder

    response = delete(application, "X074AAC")

    assert response.json()["renumbered"] == {}
    assert shown(settings) == {"X074AAA": "X074AAA", "X074AAB": "X074AAB"}


def test_the_renumbering_is_reported_back_by_internal_id(folder):
    application, _ = folder

    response = delete(application, "X074AAA")

    assert response.json()["renumbered"] == {"X074AAB": "X074AAA", "X074AAC": "X074AAB"}


# ---------- a renumbered document is the same document ----------


def test_a_renumbered_document_keeps_its_internal_id_and_text(folder):
    application, settings = folder

    delete(application, "X074AAA")

    with db.connect(settings.db_path) as conn:
        row = conn.execute("SELECT document_id, text FROM new_documents WHERE display_id = 'X074AAA'"
                           ).fetchone()
    assert tuple(row) == ("X074AAB", "the text of X074AAB")


def test_the_scans_stay_where_they_are(folder):
    application, settings = folder
    storage = BucketStorage(settings.bucket_uri)

    delete(application, "X074AAA")

    assert storage.read_bytes("documents/X074/X074AAB/page.jpg") == b"the scan of X074AAB"
    assert storage.read_bytes("documents/X074/X074AAC/page.jpg") == b"the scan of X074AAC"


def test_every_page_row_still_points_at_a_file_that_is_there(folder):
    application, settings = folder
    storage = BucketStorage(settings.bucket_uri)

    delete(application, "X074AAA")

    with db.connect(settings.db_path) as conn:
        keys = [r["bucket_key"] for r in conn.execute("SELECT bucket_key FROM new_document_pages")]
    assert keys
    for key in keys:
        assert storage.exists(key), key


def test_the_records_carry_the_new_display_ids(folder):
    application, settings = folder

    delete(application, "X074AAA")

    assert record(settings, "X074AAB")["display_id"] == "X074AAA"
    assert record(settings, "X074AAC")["display_id"] == "X074AAB"


# ---------- what a delete removes ----------


def test_the_deleted_documents_record_is_gone(folder):
    """It used to be left behind, which is how records/ grew ids no row mentioned any more."""
    application, settings = folder

    delete(application, "X074AAC")

    assert record(settings, "X074AAC") is None


def test_the_scans_of_a_deleted_document_are_really_deleted(folder):
    application, settings = folder
    storage = BucketStorage(settings.bucket_uri)

    delete(application, "X074AAC")

    assert not storage.exists("documents/X074/X074AAC/page.jpg")


def test_a_document_that_is_the_only_one_left_deletes_cleanly(folder):
    application, settings = folder

    for document_id in ("X074AAA", "X074AAB", "X074AAC"):
        delete(application, document_id)

    assert shown(settings) == {}


# ---------- a document that always fails must not hold up the queue ----------


def test_a_failed_document_is_tried_after_the_ones_never_tried(folder):
    """Measured on a real run: one document failed every round and cost the full retry backoff at
    the front of every batch."""
    application, settings = folder
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET status = 'error' WHERE document_id = 'X074AAA'")
        conn.execute("UPDATE new_documents SET status = 'split_confirmed' "
                     "WHERE document_id IN ('X074AAB', 'X074AAC')")

        pending = db.get_new_pending_document_ids(conn)

    assert pending == ["X074AAB", "X074AAC", "X074AAA"]


def test_a_queue_with_no_failures_keeps_its_order(folder):
    application, settings = folder
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET status = 'split_confirmed'")

        assert db.get_new_pending_document_ids(conn) == ["X074AAA", "X074AAB", "X074AAC"]
