"""Folders already on the site are renumbered so their display ids run from AAA with no gaps."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from incipit import db, new_document
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "close_docid_gaps", ROOT / "scripts" / "close_docid_gaps.py")
gaps = importlib.util.module_from_spec(spec)
sys.modules["close_docid_gaps"] = gaps
spec.loader.exec_module(gaps)


@pytest.fixture
def folder(tmp_path):
    """A folder that starts at AAB and skips AAD -- two holes to close."""
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
                        db_path=str(tmp_path / "extraction.db"))
    db.init_db(settings.db_path)
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        seed_batch(conn, "B", 3, folder="E2003", name="E2003_Articles")
        for index, document_id in enumerate(("E2003AAB", "E2003AAC", "E2003AAE")):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, text) "
                "VALUES (?, 'B', 'drafted', 'unreviewed', ?)", (document_id, f"text of {document_id}"))
            key = f"documents/E2003/{document_id}/page.jpg"
            storage.write_bytes(key, f"scan of {document_id}".encode())
            conn.execute("INSERT INTO new_document_pages (document_id, sequence_index, bucket_key) "
                         "VALUES (?, ?, ?)", (document_id, index, key))
            new_document.refresh_output_record(conn, document_id, storage)
    return settings


def run(settings, *extra):
    saved = sys.argv
    sys.argv = ["close_docid_gaps.py", "--db", settings.db_path,
                "--bucket-uri", settings.bucket_uri, *extra]
    try:
        return gaps.main()
    finally:
        sys.argv = saved


def ids(settings):
    """The display ids, which are what renumbering changes."""
    with db.connect(settings.db_path) as conn:
        return sorted(r["display_id"] for r in
                      conn.execute("SELECT display_id FROM new_documents"))


# ---------- the database ----------


def test_a_folder_that_starts_at_aab_is_renumbered(folder):
    run(folder, "--apply")

    assert ids(folder) == ["E2003AAA", "E2003AAB", "E2003AAC"]


def test_each_document_keeps_its_own_text(folder):
    """Closing a gap renames documents; it must not shuffle their contents."""
    run(folder, "--apply")

    with db.connect(folder.db_path) as conn:
        first = conn.execute(
            "SELECT text FROM new_documents WHERE display_id = 'E2003AAA'").fetchone()["text"]
    assert first == "text of E2003AAB"


def test_a_dry_run_changes_nothing(folder):
    run(folder)

    assert ids(folder) == ["E2003AAB", "E2003AAC", "E2003AAE"]


def test_a_folder_with_no_gaps_is_left_alone(folder):
    run(folder, "--apply")
    before = ids(folder)

    run(folder, "--apply")

    assert ids(folder) == before


# ---------- the bucket ----------


def test_no_scan_moves(folder):
    run(folder, "--apply")

    storage = BucketStorage(folder.bucket_uri)
    assert storage.read_bytes("documents/E2003/E2003AAB/page.jpg") == b"scan of E2003AAB"


def test_the_records_carry_the_new_display_ids(folder):
    run(folder, "--apply")

    record = json.loads(BucketStorage(folder.bucket_uri).read_record("E2003", "E2003AAB"))
    assert record["display_id"] == "E2003AAA"


# ---------- and the site closes them on its own ----------


def test_the_watcher_closes_a_gap_it_finds(folder, monkeypatch):
    """Reported: "Run the repair script automatically whenever there are anomolies in docid gap
    folders just use the watcher on the site to trigger it"."""
    from incipit import gaps as gaps_module

    assert gaps_module.find(folder.db_path)          # the fixture folder starts at AAB

    moved = gaps_module.close(folder)

    assert moved
    assert gaps_module.find(folder.db_path) == {}


def test_the_watcher_finds_nothing_in_a_tidy_folder(folder):
    from incipit import gaps as gaps_module

    gaps_module.close(folder)

    assert gaps_module.close(folder) == {}


