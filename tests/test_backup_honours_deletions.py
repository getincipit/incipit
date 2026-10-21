"""A folder, document or batch deleted on the site goes from the backups too, and nothing else does."""

import sqlite3
import json

import pytest

from incipit import backup, db
from conftest import insert_rows


@pytest.fixture
def drive(tmp_path):
    """A backup holding two folders and one pending batch, and a database that has deleted nothing."""
    db_path = tmp_path / "extraction.db"
    db.init_db(str(db_path))

    root = tmp_path / "backup"
    bucket = root / "bucket"
    for folder, documents in (("f-articles", ("d-aaa", "d-aab")), ("f-advice", ("d-adv",))):
        for document in documents:
            (bucket / "documents" / folder / document).mkdir(parents=True)
            (bucket / "documents" / folder / document / "page.jpg").write_bytes(b"a scan")
            (bucket / "records" / folder).mkdir(parents=True, exist_ok=True)
            (bucket / "records" / folder / f"{document}.json").write_text("{}", encoding="utf-8")
    for batch in ("b-articles", "b-pending"):
        (bucket / "uploads" / batch).mkdir(parents=True)
        (bucket / "uploads" / batch / "0000.jpg").write_bytes(b"raw")
    return str(db_path), root


def record(db_path, kind, target, **details):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO deletion_log (deleted_at, reviewer, reason, workflow, target_kind, target_id, "
        "details_json) VALUES ('now', 'Paul', 'a test', 'new', ?, ?, ?)",
        (kind, target, json.dumps(details)))
    conn.commit()
    conn.close()


def delete_articles(db_path):
    record(db_path, "new_folder", "f-articles", document_ids=["d-aaa", "d-aab"],
           batch_ids=["b-articles"])


def names(path):
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


# ---------- what a recorded deletion removes ----------


def test_a_deleted_folder_takes_its_scans_and_records(drive):
    db_path, root = drive
    delete_articles(db_path)

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "documents") == ["f-advice"]
    assert names(root / "bucket" / "records") == ["f-advice"]


def test_a_deleted_folders_uploads_go_with_it(drive):
    db_path, root = drive
    delete_articles(db_path)

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "uploads") == ["b-pending"]


def test_a_deleted_document_goes_without_taking_its_neighbours(drive):
    db_path, root = drive
    record(db_path, "new_document", "d-aaa")

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "documents" / "f-articles") == ["d-aab"]
    assert names(root / "bucket" / "records" / "f-articles") == ["d-aab.json"]


def test_a_deleted_batch_takes_its_raw_scans(drive):
    db_path, root = drive
    record(db_path, "pending_batch", "b-pending")

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "uploads") == ["b-articles"]


# ---------- and what it leaves alone ----------


def test_nothing_is_removed_without_a_recorded_deletion(drive):
    db_path, root = drive

    assert backup.apply_deletions(db_path, root) == 0
    assert names(root / "bucket" / "documents") == ["f-advice", "f-articles"]


def test_applying_the_same_deletion_twice_is_harmless(drive):
    db_path, root = drive
    delete_articles(db_path)

    assert backup.apply_deletions(db_path, root) > 0
    assert backup.apply_deletions(db_path, root) == 0


def test_a_database_that_will_not_open_removes_nothing(drive, tmp_path):
    _, root = drive

    assert backup.apply_deletions(tmp_path / "nothing.db", root) == 0
    assert names(root / "bucket" / "documents") == ["f-advice", "f-articles"]


def test_a_log_entry_with_unreadable_details_still_removes_its_target(drive):
    db_path, root = drive
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO deletion_log (deleted_at, reviewer, reason, workflow, target_kind, "
                 "target_id, details_json) VALUES ('now', 'P', 'r', 'new', 'new_folder', "
                 "'f-articles', 'not json')")
    conn.commit()
    conn.close()

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "documents") == ["f-advice"]


# ---------- the run reports it ----------


def test_a_backup_run_applies_and_reports_the_removals(drive, tmp_path):
    db_path, root = drive
    delete_articles(db_path)

    summary = backup.run_backup(db_path, root, data_dir=tmp_path / "data")

    assert summary["deletions_applied"] > 0
    assert names(root / "bucket" / "documents") == ["f-advice"]


def test_the_mirror_is_cleared_too(drive, tmp_path):
    """Otherwise the offsite copy keeps handing the deleted folder back."""
    db_path, root = drive
    mirror = tmp_path / "mirror"
    backup.run_backup(db_path, root, data_dir=tmp_path / "data", mirror_dir=mirror)
    assert (mirror / "bucket" / "documents" / "f-articles").is_dir()

    delete_articles(db_path)
    backup.run_backup(db_path, root, data_dir=tmp_path / "data", mirror_dir=mirror)

    assert not (mirror / "bucket" / "documents" / "f-articles").exists()


# ---------- what the database still has ----------


def test_a_folder_the_database_still_has_keeps_its_backup(drive):
    """The log is append-only and says nothing about what happened next."""
    db_path, root = drive
    record(db_path, "new_folder", "f-articles")
    conn = sqlite3.connect(db_path)
    insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                      "VALUES ('B2', 'now', 2, 'f-articles')")
    conn.commit()
    conn.close()

    backup.apply_deletions(db_path, root)

    assert names(root / "bucket" / "documents") == ["f-advice", "f-articles"]


def test_a_document_the_database_still_has_keeps_its_backup(drive):
    db_path, root = drive
    record(db_path, "new_document", "d-aab")
    conn = sqlite3.connect(db_path)
    insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                      "VALUES ('B2', 'now', 1, 'f-articles')")
    insert_rows(conn, "INSERT INTO new_documents (document_id, batch_id, status) "
                      "VALUES ('d-aab', 'B2', 'drafted')")
    conn.commit()
    conn.close()

    backup.apply_deletions(db_path, root)

    assert "d-aab" in names(root / "bucket" / "documents" / "f-articles")
