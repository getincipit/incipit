"""Deleting a folder takes everything it put anywhere."""


import pytest

from incipit import accounts, backup, db, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch, signed_in


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        seed_batch(conn, "B", 2, folder="E2002", name="E2002_Advice")
        conn.execute("UPDATE new_batches SET status = 'confirmed', bucket_folder = 'B' "
                     "WHERE batch_id = 'B'")
        for document_id in ("E2002AAA", "E2002AAB"):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                "VALUES (?, 'B', 'drafted', 'unreviewed')", (document_id,))
            storage.write_bytes(f"documents/E2002/{document_id}/page.jpg", b"a scan")
            storage.write_bytes(f"records/E2002/{document_id}.json", b"{}")
    for name in ("E2002_Page_01.jpg", "E2002_Page_02.jpg"):
        storage.write_bytes(f"uploads/B/0000_{name}", b"a raw scan")
    return application, settings, storage


def delete_folder(application):
    return signed_in(application).delete(
        "/api/new/folders?folder_id=E2002&reviewer=Paul&reason=replacing+it")


# ---------- the working directory ----------


def test_the_raw_scans_go_with_the_folder(site):
    """The reported fault: they stayed, and the next upload duplicated every page beside them."""
    application, settings, storage = site

    delete_folder(application)

    assert storage.list_keys("uploads/B") == set()


def test_the_documents_go(site):
    application, settings, storage = site

    delete_folder(application)

    assert not storage.exists("documents/E2002/E2002AAA/page.jpg")


def test_the_records_go(site):
    """They used to be left behind, which is how records/ grew ids no row mentioned any more."""
    application, settings, storage = site

    delete_folder(application)

    assert not storage.exists("records/E2002/E2002AAA.json")


def test_the_batch_rows_go(site):
    """A batch whose scans have been deleted is not work waiting for anyone."""
    application, settings, _ = site

    delete_folder(application)

    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM new_batches WHERE folder_id = 'E2002'"
                            ).fetchone()[0] == 0


def test_the_folder_row_goes(site):
    application, settings, _ = site

    delete_folder(application)

    with db.connect(settings.db_path) as conn:
        assert db.get_folder(conn, "E2002") is None


def test_how_many_upload_folders_went_is_reported(site):
    application, _, _ = site

    assert delete_folder(application).json()["upload_folders_removed"] == 1


# ---------- and the backups ----------


def test_the_deletion_is_recorded_so_the_backup_acts_on_it(site):
    """A backup only ever adds; the log is the only thing that removes from it."""
    application, settings, _ = site

    delete_folder(application)

    with db.connect(settings.db_path) as conn:
        kinds = [r["target_kind"] for r in conn.execute(
            "SELECT target_kind FROM deletion_log WHERE target_id = 'E2002'")]
    assert "new_folder" in kinds


def test_the_backup_drops_the_folders_uploads_too(site, tmp_path):
    """Uploads are backed up by batch id, so the log names the folder's batches for removal."""
    application, settings, _ = site
    uploads = tmp_path / "backup" / backup.BUCKET_DIRNAME / "uploads"
    for batch in ("B", "other-batch"):
        (uploads / batch).mkdir(parents=True)

    delete_folder(application)
    backup.apply_deletions(settings.db_path, tmp_path / "backup")

    assert sorted(p.name for p in uploads.iterdir()) == ["other-batch"]
