import csv
import io
from pathlib import Path


from incipit import db, export
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch, seed_document, seed_draft

# The default profile's columns: the fixed leading ones, the registry's in order, the fixed trailing ones.
NEW_HEADER = [
    "Document ID", "Title", "Transcription", "Document Type", "Date", "Author(s)",
    "Recipient(s)", "Name(s) Mentioned", "Subjects", "Places",
    "Image Filename(s)", "Editors Notes", "Submitted by",
]


def _row_for(csv_rows: list[list[str]], document_id: str) -> list[str]:
    return next(r for r in csv_rows if r[0] == document_id)


# ---------- export_new_csv ("Add New Document" workflow) ----------


def _seed_new_document(tmp_path: Path, settings: Settings, document_id: str) -> None:
    db.init_db(settings.db_path)
    storage = BucketStorage(settings.bucket_uri)
    bucket_key, bucket_uri = storage.upload_bytes(document_id, "0001.jpg", b"fake-page-bytes", prefix="new")

    with db.connect(settings.db_path) as conn:
        seed_batch(conn, f"BATCH-{document_id}", 1, folder=document_id[:-3])
        seed_document(conn, document_id, f"BATCH-{document_id}",
            pages=[{"sequence_index": 0, "bucket_key": bucket_key, "bucket_uri": bucket_uri}],
        )
        seed_draft(
            conn, document_id, model="m", provider="p",
            result={
                "text": "Dear Sir, ...", "challenges": [], "uncertain_words": [], "marginal_notes": [],
                "places": ["Troy, N. Y."], "names": ["Ada M. Lovell"],
            },
            ai_metadata={
                "title": f"[{document_id}], Untitled", "document_type": "Letter", "date": "1900-01-01",
                "author": "Lovell, Ada Mary", "recipient": "Carver, Charles",
                "place": "Troy, N. Y.", "mentioned": "Northern Lamp Co", "subjects": "Phonograph",
            },
            ocr_draft_text="draft", output_json={}, cost_estimate=0.0005,
        )
        db.save_new_review(
            conn, document_id,
            reviewed_title=None, title_override=None, reviewed_text="Dear Sir, corrected.",
            reviewed_fields={"document_type": "Letter", "date": "1900-01-01",
                             "author": "Lovell, Ada Mary", "recipient": "Carver, Charles",
                             "place": "Troy, N. Y.", "subjects": ["Phonograph"],
                             "places": ["Troy, N. Y."], "names": ["Ada M. Lovell"]},
            review_status="accepted", reviewer="jdoe", editors_notes="",
        )


def _read_csv_rows(csv_bytes: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))


def test_export_new_csv_includes_only_accepted_documents(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "NEWDOC001")
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        db.create_batch(conn, "BATCH-PENDING", image_count=1)
        seed_document(conn, "NEWDOC_PENDING", "BATCH-PENDING", pages=[{"sequence_index": 0}])

    count, csv_bytes, document_ids = export.export_new_csv(settings)

    assert count == 1
    assert document_ids == ["NEWDOC001"]
    rows = _read_csv_rows(csv_bytes)
    assert rows[0] == NEW_HEADER
    assert len(rows) == 2  # header + NEWDOC001 only


def test_export_new_csv_uses_reviewer_edited_fields(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "NEWDOC001")

    _, csv_bytes, _ = export.export_new_csv(settings)

    rows = _read_csv_rows(csv_bytes)
    row = _row_for(rows, "NEWDOC001")
    assert dict(zip(NEW_HEADER, row)) == {
        "Document ID": "NEWDOC001",
        "Title": "[NEWDOC001], Untitled",
        "Transcription": "Dear Sir, corrected.",
        "Document Type": "Letter",
        "Date": "1900-01-01",
        "Author(s)": "Lovell, Ada Mary",
        "Recipient(s)": "Carver, Charles",
        # The one Mentioned index: the reviewer's edited names list.
        "Name(s) Mentioned": "Ada M. Lovell",
        "Subjects": "Phonograph",
        "Image Filename(s)": "0001.jpg",
        "Places": "Troy, N. Y.",
        "Editors Notes": "",
        "Submitted by": "jdoe",
    }


def test_export_new_csv_uses_title_override_when_set(tmp_path: Path):
    """A reviewer's typed title (title_override) wins over the derived/AI title in the exported
    Title column, so the correction actually reaches the deliverable."""
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "NEWDOC001")
    with db.connect(settings.db_path) as conn:
        db.save_new_review(
            conn, "NEWDOC001",
            reviewed_title="[NEWDOC001], Untitled", title_override="Hand-corrected title",
            reviewed_text="t", reviewed_fields={"document_type": "Letter"},
            review_status="accepted", reviewer="jdoe", editors_notes="",
        )

    _, csv_bytes, _ = export.export_new_csv(settings)

    rows = _read_csv_rows(csv_bytes)
    row = _row_for(rows, "NEWDOC001")
    assert row[NEW_HEADER.index("Title")] == "Hand-corrected title"


def test_export_new_csv_empty_when_nothing_accepted(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        db.create_batch(conn, "BATCH1", image_count=1)
        seed_document(conn, "NEWDOC_PENDING", "BATCH1", pages=[{"sequence_index": 0}])

    count, csv_bytes, document_ids = export.export_new_csv(settings)

    assert count == 0
    assert document_ids == []
    assert _read_csv_rows(csv_bytes) == [NEW_HEADER]


def _export_ids(settings, **kwargs):
    _, _, ids = export.export_new_csv(settings, **kwargs)
    return ids


def test_export_new_csv_folder_id_scopes_to_one_folder(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "E2231AAA")
    _seed_new_document(tmp_path, settings, "E2231AAB")
    _seed_new_document(tmp_path, settings, "E2232AAA")

    assert _export_ids(settings, folder_id="E2231") == ["E2231AAA", "E2231AAB"]
    assert _export_ids(settings, folder_id="E2232") == ["E2232AAA"]
    assert _export_ids(settings) == ["E2231AAA", "E2231AAB", "E2232AAA"]  # None = every folder


def test_export_new_csv_include_unapproved_adds_undrafted_reviews(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "E2231AAA")  # accepted
    _seed_new_document(tmp_path, settings, "E2231AAB")  # drafted, then set back to unreviewed below
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'unreviewed' WHERE document_id = 'E2231AAB'")

    # Default (clean) export: only the accepted one.
    assert _export_ids(settings, folder_id="E2231") == ["E2231AAA"]
    # "Continue export without approving": both drafted docs, accepted or not.
    assert _export_ids(settings, folder_id="E2231", include_unapproved=True) == ["E2231AAA", "E2231AAB"]


def test_get_new_document_folders_groups_with_counts_and_approval(tmp_path: Path):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    _seed_new_document(tmp_path, settings, "E2231AAA")  # accepted
    _seed_new_document(tmp_path, settings, "E2231AAB")  # accepted
    _seed_new_document(tmp_path, settings, "E2232AAA")  # accepted, but we add an undrafted sibling below
    with db.connect(settings.db_path) as conn:
        db.create_batch(conn, "BATCH-UNDRAFTED", image_count=1)
        seed_document(conn, "E2232AAB", "BATCH-UNDRAFTED", folder="E2232", pages=[{"sequence_index": 0}])  # split_confirmed

    with db.connect(settings.db_path) as conn:
        folders = {f["folder_id"]: f for f in db.get_new_document_folders(conn)}

    assert folders["E2231"]["document_count"] == 2
    assert folders["E2231"]["drafted_count"] == 2
    assert folders["E2231"]["accepted_count"] == 2
    assert folders["E2231"]["all_approved"] is True

    # E2232 has one accepted document and one still awaiting drafting -> not all approved.
    assert folders["E2232"]["document_count"] == 2
    assert folders["E2232"]["drafted_count"] == 1  # only the accepted one is drafted/reviewable
    assert folders["E2232"]["accepted_count"] == 1
    assert folders["E2232"]["awaiting_drafting_count"] == 1
    assert folders["E2232"]["all_approved"] is False


def test_get_new_document_folders_uses_full_folder_name_when_captured(tmp_path: Path):
    """The folder card shows the full archival name captured at upload (e.g."""
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=str(tmp_path / "extraction.db"))
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        seed_batch(conn, "BATCH-E2241", 1, folder="E2241", name="E2241_Equipment & Supplies")
        seed_document(conn, "E2241AAA", "BATCH-E2241", folder="E2241", pages=[{"sequence_index": 0}])
        seed_batch(conn, "BATCH-E2242", image_count=1, folder="E2242")  # no folder_name
        seed_document(conn, "E2242AAA", "BATCH-E2242", folder="E2242", pages=[{"sequence_index": 0}])

    with db.connect(settings.db_path) as conn:
        folders = {f["folder_id"]: f for f in db.get_new_document_folders(conn)}

    assert folders["E2241"]["folder_name"] == "E2241_Equipment & Supplies"
    assert folders["E2242"]["folder_name"] == "E2242"  # falls back to the bare id


# ---------- a reviewer's edit has to reach the deliverable ----------


def test_a_reviewers_edit_appears_in_the_exported_csv(tmp_path: Path):
    """The end of the round trip, asserted from the outside: correct a document on the site, export
    its folder, and read the correction back out of the CSV."""
    import io

    from fastapi.testclient import TestClient

    from incipit import accounts, review_server
    from incipit.config import Settings
    from incipit.storage import BucketStorage

    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    db.init_db(db_path)
    storage = BucketStorage(settings.bucket_uri)

    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, "
                     "uploaded_folder_name) VALUES ('B', 'now', 1, 'E2002', 'E2002_Advice_jpegs')")
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
            "ai_title, fields_json, output_json) VALUES ('E2002AAA', 'B', 'drafted', "
            "'unreviewed', ?, '[E2002AAA], Letter', ?, '{}')",
            ("Dear Sir, I have the honor to acknowlege your favor.",
             '{"author": "Edmunds, Henry", "date": "1920-01-19"}'))
        key, uri = storage.upload_bytes("E2002AAA", "page.jpg", b"scan",
                                        prefix="documents")
        conn.execute("INSERT INTO new_document_pages (document_id, sequence_index, bucket_key, "
                     "bucket_uri) VALUES ('E2002AAA', 0, ?, ?)", (key, uri))

    client = TestClient(review_server.create_app(settings, str(tmp_path / "export")))
    client.cookies.clear()
    client.post("/login", data={"username": "paul", "password": "paul-password", "next": "/"},
                follow_redirects=False)

    corrected = "Dear Sir, I have the honour to acknowledge your favour."
    saved = client.post("/api/new/documents/E2002AAA/review", json={
        "reviewed_text": corrected,
        "fields": {"document_type": "Letter", "author": "Edmunds, Henry Jr.",
                   "recipient": "Lovell, Ada Mary", "date": "1920-01-19", "place": "London"},
        "review_status": "accepted", "editors_notes": "spelling corrected",
    })
    assert saved.status_code == 200, saved.text

    _, csv_body, _ = export.export_new_csv(settings, "E2002")
    rows = list(csv.DictReader(io.StringIO(csv_body.decode("utf-8-sig"))))

    [row] = [r for r in rows if r["Document ID"] == "E2002AAA"]
    assert row["Transcription"] == corrected, "the CSV carries the AI draft, not the correction"
    assert "acknowlege your favor" not in row["Transcription"], "the uncorrected draft survived"
    assert row["Author(s)"] == "Edmunds, Henry Jr.", "the edited metadata did not reach the export"


# ---------- "the exported folder should be named for the folder" ----------
