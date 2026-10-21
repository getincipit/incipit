import json
from pathlib import Path

import pytest

from incipit import db
from conftest import seed_batch, seed_document, seed_folder, seed_draft



def test_connect_enables_wal_mode(tmp_path: Path):
    """WAL mode lets a reader (e.g."""
    db_path = str(tmp_path / "extraction.db")
    with db.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_new_document_batch_ingest_and_split_proposal_round_trip(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=3)
        db.add_batch_images(
            conn,
            "BATCH1",
            [
                {"sequence_index": 0, "original_filename": "0001.jpg", "bucket_key": "new_intake/BATCH1/0001.jpg"},
                {"sequence_index": 1, "original_filename": "0002.jpg", "bucket_key": "new_intake/BATCH1/0002.jpg"},
                {"sequence_index": 2, "original_filename": "0003.jpg", "bucket_key": "new_intake/BATCH1/0003.jpg"},
            ],
        )
        db.set_batch_split_proposal(conn, "BATCH1", {0: True, 1: False, 2: True})

    with db.connect(db_path) as conn:
        batch = db.get_batch(conn, "BATCH1")
        assert batch["status"] == "split_proposed"
        assert batch["image_count"] == 3

        images = db.get_batch_images(conn, "BATCH1")
        assert [img["proposed_new_document"] for img in images] == [1, 0, 1]
        assert [img["sequence_index"] for img in images] == [0, 1, 2]


def test_set_batch_folder_id_updates_an_existing_batch(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)  # no folder_id given at upload time

    with db.connect(db_path) as conn:
        db.set_batch_folder_id(conn, "BATCH1", seed_folder(conn, "E2241"))

    with db.connect(db_path) as conn:
        assert db.get_batch(conn, "BATCH1")["folder_id"] == "E2241"







def test_naming_an_existing_folder_with_a_blank_name_leaves_its_name_alone(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        folder_id = db.ensure_folder(conn, "E2241", "E2241_Equipment & Supplies")

        assert db.ensure_folder(conn, "E2241") == folder_id
        assert db.folder_display(conn, folder_id) == ("E2241", "E2241_Equipment & Supplies")

def test_get_new_document_error_finds_the_error_message(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0}])
        db.upsert_new_document_error(conn, "NEWDOC001", "OpenRouter rejected the request (402)")

    with db.connect(db_path) as conn:
        assert db.get_new_document_error(conn, ["NEWDOC001"]) == "OpenRouter rejected the request (402)"


def test_get_new_document_error_ignores_ids_outside_the_given_list(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0}])
        db.upsert_new_document_error(conn, "NEWDOC001", "boom")

    with db.connect(db_path) as conn:
        assert db.get_new_document_error(conn, ["NEWDOC999"]) is None
        assert db.get_new_document_error(conn, []) is None


def test_delete_batch_rows_removes_batch_and_its_images(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        db.add_batch_images(
            conn, "BATCH1", [{"sequence_index": 0, "original_filename": "0001.jpg", "bucket_key": "new_intake/BATCH1/0001.jpg"}]
        )

    with db.connect(db_path) as conn:
        db.delete_batch_rows(conn, "BATCH1")

    with db.connect(db_path) as conn:
        assert db.get_batch(conn, "BATCH1") is None
        assert db.get_batch_images(conn, "BATCH1") == []


def test_get_pending_batches_excludes_confirmed_batches(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        seed_batch(conn, "UPLOADED", image_count=1, folder="D8026")
        db.add_batch_images(conn, "UPLOADED", [{"sequence_index": 0, "bucket_key": "k"}])

        db.create_batch(conn, "PROPOSED", image_count=1)
        db.add_batch_images(conn, "PROPOSED", [{"sequence_index": 0, "bucket_key": "k"}])
        db.set_batch_split_proposal(conn, "PROPOSED", {0: True})

        db.create_batch(conn, "CONFIRMED", image_count=1)
        db.add_batch_images(conn, "CONFIRMED", [{"sequence_index": 0, "bucket_key": "k"}])
        db.mark_batch_confirmed(conn, "CONFIRMED")

    with db.connect(db_path) as conn:
        pending = {row["batch_id"] for row in db.get_pending_batches(conn)}

    assert pending == {"UPLOADED", "PROPOSED"}
    assert "CONFIRMED" not in pending


def test_delete_new_document_removes_the_document_and_its_pages(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0, "bucket_key": "new/NEWDOC001/1.jpg"}]
        )

    with db.connect(db_path) as conn:
        db.delete_new_document(conn, "NEWDOC001")

    with db.connect(db_path) as conn:
        assert db.get_new_document(conn, "NEWDOC001") is None
        assert db.get_new_document_pages(conn, "NEWDOC001") == []
        # The id is immediately reusable -- no collision with the just-deleted document.
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0}])


def test_get_new_document_ids_in_folder_groups_by_docid_prefix(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "E2231AAA", "BATCH1", folder="E2231", pages=[{"sequence_index": 0}])
        seed_document(conn, "E2231AAB", "BATCH1", folder="E2231", pages=[{"sequence_index": 0}])
        seed_document(conn, "E2232AAA", "BATCH1", folder="E2232", pages=[{"sequence_index": 0}])

    with db.connect(db_path) as conn:
        assert sorted(db.get_new_document_ids_in_folder(conn, "E2231")) == ["E2231AAA", "E2231AAB"]
        assert db.get_new_document_ids_in_folder(conn, "E2232") == ["E2232AAA"]
        assert db.get_new_document_ids_in_folder(conn, "NOPE") == []


def test_get_new_pending_document_ids_in_folder_only_returns_undrafted_docs_in_that_folder(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "E2231AAA", "BATCH1", folder="E2231", pages=[{"sequence_index": 0}])  # split_confirmed
        seed_document(conn, "E2231AAB", "BATCH1", folder="E2231", pages=[{"sequence_index": 0}])  # split_confirmed
        seed_document(conn, "E2232AAA", "BATCH1", folder="E2232", pages=[{"sequence_index": 0}])  # different folder
        db.upsert_new_document_error(conn, "E2231AAB", "boom")  # still "pending" -- error is retriable
        seed_draft(
            conn, "E2231AAA", model="m", provider="p",
            result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
            ai_metadata={"title": "", "document_type": "", "date": "", "author": "", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
            ocr_draft_text="d", output_json={}, cost_estimate=0.0,
        )

    with db.connect(db_path) as conn:
        # E2231AAA is now drafted -- excluded.
        assert db.get_new_pending_document_ids_in_folder(conn, "E2231") == ["E2231AAB"]
        assert db.get_new_pending_document_ids_in_folder(conn, "E2232") == ["E2232AAA"]
        assert db.get_new_pending_document_ids_in_folder(conn, "NOPE") == []


def test_rename_new_document_updates_every_reference(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        db.add_batch_images(
            conn, "BATCH1", [{"sequence_index": 0, "bucket_key": "new_intake/BATCH1/0001.jpg"}]
        )
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0, "bucket_key": "new/NEWDOC001/1.jpg"}]
        )
        db.assign_batch_images(conn, "BATCH1", {0: "NEWDOC001"})

    with db.connect(db_path) as conn:
        db.rename_new_document(conn, "NEWDOC001", "NEWDOC002")

    with db.connect(db_path) as conn:
        # Only the display id changes; every row keyed by the internal id is untouched.
        assert db.display_id_of(conn, "NEWDOC001") == "NEWDOC002"
        assert db.get_new_document_pages(conn, "NEWDOC001")[0]["bucket_key"] == "new/NEWDOC001/1.jpg"
        assert db.get_batch_images(conn, "BATCH1")[0]["assigned_document_id"] == "NEWDOC001"


def test_new_document_draft_review_and_export_round_trip(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC001", "BATCH1",
            pages=[{"sequence_index": 0, "bucket_key": "new/NEWDOC001/1.jpg", "bucket_uri": "file:///new/NEWDOC001/1.jpg"}],
        )

    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "NEWDOC001") == "split_confirmed"
        assert db.get_new_pending_document_ids(conn) == ["NEWDOC001"]
        assert len(db.get_new_document_pages(conn, "NEWDOC001")) == 1

        seed_draft(
            conn, "NEWDOC001", model="qwen/qwen3.6-27b", provider="openrouter",
            result={
                "text": "Dear Sir, ...", "challenges": ["typed"], "uncertain_words": [],
                "marginal_notes": [], "places": ["Troy, N. Y."], "names": ["Ada M. Lovell"],
            },
            ai_metadata={
                "title": "[NEWDOC001], Letter from A to B, January 1st, 1900",
                "document_type": "Letter", "date": "1900-01-01", "author": "A", "recipient": "B",
                "place": "Troy, N. Y.", "mentioned": "", "subjects": "",
            },
            ocr_draft_text="draft", output_json={"document_id": "NEWDOC001"}, cost_estimate=0.001,
        )

    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "NEWDOC001") == "drafted"
        assert db.get_new_pending_document_ids(conn) == []
        # Spend is the ledger's: a cost written on a document is a label, not a billed call.
        assert db.get_new_document(conn, "NEWDOC001")["cost_estimate"] == 0.001

        queue = db.get_new_review_queue(conn)
        assert len(queue) == 1
        assert queue[0]["title"] == "[NEWDOC001], Letter from A to B, January 1st, 1900"
        assert queue[0]["page_count"] == 1

        db.save_new_review(
            conn, "NEWDOC001",
            reviewed_title="Custom Title", title_override=None, reviewed_text="Dear Sir, edited.",
            reviewed_fields={"document_type": "Letter", "author": "A", "places": ["Troy, N. Y."]},
            review_status="accepted", reviewer="jdoe", editors_notes="",
        )

    with db.connect(db_path) as conn:
        accepted = db.get_new_documents_for_export(conn)
        assert len(accepted) == 1
        assert accepted[0]["text"] == "Dear Sir, edited."
        assert json.loads(accepted[0]["reviewed_fields_json"])["document_type"] == "Letter"


def test_upsert_new_document_error_sets_status(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC001", "BATCH1", pages=[{"sequence_index": 0}])
        db.upsert_new_document_error(conn, "NEWDOC001", "OCR failed")

    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "NEWDOC001") == "error"
        assert db.get_new_pending_document_ids(conn) == ["NEWDOC001"]  # error is retriable, still "pending"


def test_connect_upgrades_a_pre_wal_database(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    # A database file created before this change existed, still on SQLite's default journal mode.
    conn = db.open_connection(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    with db.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_every_connection_enforces_foreign_keys(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(db.IntegrityError):
            conn.execute(
                "INSERT INTO new_document_pages (document_id, sequence_index, bucket_key) "
                "VALUES ('missing', 0, 'k')"
            )


