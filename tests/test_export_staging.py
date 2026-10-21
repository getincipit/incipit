"""Published deliverables follow their owning folder through deletion, replacement and rename."""

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, export_archive, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows



@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"),
        backup_dir=str(tmp_path / "backup"), backup_mirror=str(tmp_path / "mirror"),
    )
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
    client = TestClient(application)
    client.cookies.clear()
    client.post("/login", data={"username": "paul", "password": "paul-password"},
                follow_redirects=False)
    return client, db_path, tmp_path


def exported_document(db_path, tmp_path, document_id="E2231AAA"):
    """Seed an already published generation without coupling lifecycle tests to generation."""
    storage = BucketStorage(f"file://{(tmp_path / 'bucket').as_posix()}")
    key, uri = storage.upload_bytes(document_id, "0001.jpg", b"page", prefix="documents")
    folder_id = document_id[:5]
    archive = tmp_path / "export" / export_archive.relative_path("new", folder_id, "Advice")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"zip")
    (archive.parent / "document.json").write_text('{}')
    with db.connect(db_path) as conn:
        # The batch is what a folder's name hangs off, so a nameable folder needs one.
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id, "
            "uploaded_folder_name) VALUES (?, 'now', 1, ?, 'Advice')",
            (f"B-{folder_id}", folder_id))
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status, "
            "reviewed_text) VALUES (?, ?, 'drafted', 'accepted', 'text')",
            (document_id, f"B-{folder_id}"))
        conn.execute(
            "INSERT INTO new_document_pages (document_id, sequence_index, bucket_key, bucket_uri) "
            "VALUES (?, 0, ?, ?)", (document_id, key, uri))


# ---------- and what a previous run died holding ----------


# ---------- and what happens to a deliverable when its folder does not survive ----------


def exported_zips(tmp_path, workflow="new"):
    return sorted(p.relative_to(tmp_path / "export").as_posix()
                  for p in (tmp_path / "export" / workflow).rglob("*.zip"))


def test_deleting_a_folder_takes_its_exported_zip_with_it(site):
    """Reported in review: the export tree is what the backup pass treats as authoritative, so a
    deliverable left behind here keeps being replicated after the folder is gone."""
    client, db_path, tmp_path = site
    exported_document(db_path, tmp_path)
    assert exported_zips(tmp_path)

    client.request("DELETE", "/api/new/folders",
                   params={"folder_id": "E2231", "reason": "uploaded the wrong scans"})

    assert exported_zips(tmp_path) == []


def test_replacing_a_folder_takes_its_exported_zip_with_it(site):
    client, db_path, tmp_path = site
    exported_document(db_path, tmp_path)

    from conftest import tiny_image_bytes
    client.post("/api/new/batches",
                files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))],
                data={"folder_display_id": "E2231", "replace": "true"})

    assert exported_zips(tmp_path) == []


def test_renaming_a_folder_leaves_its_exported_zip_where_it_is(site):
    """folder_exports still says the folder was exported, and its directory is the internal id."""
    client, db_path, tmp_path = site
    exported_document(db_path, tmp_path)
    before = exported_zips(tmp_path)

    client.post("/api/new/folders/E2231/name", json={"folder_name": "Storage Battery"})

    assert exported_zips(tmp_path) == before
