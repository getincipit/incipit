"""A document's stored JSON says what the reviewer decided, not what the model drafted."""

import json

import pytest

from incipit import accounts, db, new_document, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, review_payload, signed_in


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
    return application, settings


def drafted_document(settings, document_id="E2002AAA"):
    """A drafted new-workflow document whose record holds the model's output."""
    draft = {
        "document_id": document_id,
        "metadata": {"title": "drafted title", "author": "Draft, A", "date": "1889"},
        "transcription": {"text": "what the model read", "uncertain_words": [],
                          "marginal_notes": ["a note the model found"]},
        "places": ["Newark, NJ"], "names": ["Lovell, Ada Mary"], "challenges": ["typed"],
    }
    with db.connect(settings.db_path) as conn:
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES ('B', 'now', 1, 'E2002')")
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
            "fields_json, ai_title, output_json) VALUES (?, 'B', 'drafted', 'unreviewed', ?, ?, ?, ?)",
            (document_id, draft["transcription"]["text"],
             json.dumps({"marginal_notes": draft["transcription"]["marginal_notes"],
                         "places": draft["places"], "names": draft["names"],
                         "challenges": draft["challenges"], "author": draft["metadata"]["author"],
                         "date": draft["metadata"]["date"]}),
             draft["metadata"]["title"], json.dumps(draft)))
        new_document.refresh_output_record(conn, document_id, BucketStorage(settings.bucket_uri))
    return document_id


def record(settings, document_id):
    return json.loads(BucketStorage(settings.bucket_uri).read_record("E2002", document_id))


def save(client, document_id, **changes):
    document = client.get(f"/api/new/documents/{document_id}").json()
    return client.post(f"/api/new/documents/{document_id}/review", json=review_payload(document, **changes))


# ---------- the reported fault ----------


def test_an_edit_to_the_transcription_reaches_the_record(site):
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id, reviewed_text="what the reviewer corrected it to")

    assert record(settings, document_id)["transcription"]["text"] == \
        "what the reviewer corrected it to"


def test_an_edit_to_the_metadata_reaches_the_record(site):
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id, author="Corrected, By A Reviewer", date="1890")

    stored = record(settings, document_id)["fields"]
    assert stored["author"] == "Corrected, By A Reviewer"
    assert stored["date"] == "1890"


def test_an_edit_to_the_lists_reaches_the_record(site):
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id,
         marginal_notes=["[Upper right] the reviewer's own reading"],
         places=["Albany, NY"], names=["Prentice, William H"],
         challenges=["handwriting"])

    stored = record(settings, document_id)["fields"]
    assert stored["marginal_notes"] == ["[Upper right] the reviewer's own reading"]
    assert stored["places"] == ["Albany, NY"]
    assert stored["names"] == ["Prentice, William H"]
    assert stored["challenges"] == ["handwriting"]


def test_a_document_nobody_has_touched_still_shows_the_draft(site):
    """The fallback half of the rule: until a reviewer saves, the model's reading is what is true."""
    application, settings = site
    document_id = drafted_document(settings)

    stored = record(settings, document_id)

    assert stored["transcription"]["text"] == "what the model read"
    assert stored["fields"]["author"] == "Draft, A"


def test_clearing_a_field_is_an_edit_too(site):
    """A reviewer deleting a bad marginal note must not have it reappear in the record."""
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id, marginal_notes=[])

    assert record(settings, document_id)["fields"]["marginal_notes"] == []


# ---------- and the draft is kept, not overwritten ----------


def test_the_models_draft_survives_the_correction(site):
    """The headline is the reviewer's, so the draft needs somewhere of its own -- it is the
    evidence of what the model got right or wrong, and the benchmark's only record of it."""
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id, reviewed_text="corrected", marginal_notes=[])

    stored = record(settings, document_id)
    assert stored["ai_draft"]["text"] == "what the model read"
    assert stored["ai_draft"]["fields"]["marginal_notes"] == ["a note the model found"]
    assert stored["ai_draft"]["fields"]["author"] == "Draft, A"


def test_the_review_block_still_says_who_and_when(site):
    application, settings = site
    document_id = drafted_document(settings)

    save(signed_in(application), document_id, reviewed_text="corrected")

    review = record(settings, document_id)["review"]
    assert review["status"] == "accepted"
    assert review["reviewer"] == "Paul"
    assert review["reviewed_text"] == "corrected"


def test_a_second_edit_replaces_the_first_rather_than_accumulating(site):
    """The record is one file per document, overwritten in place -- what the backup relies on."""
    application, settings = site
    document_id = drafted_document(settings)
    client = signed_in(application)

    save(client, document_id, reviewed_text="first correction")
    save(client, document_id, reviewed_text="second correction")

    stored = record(settings, document_id)
    assert stored["transcription"]["text"] == "second correction"
    assert "first correction" not in json.dumps(stored)






# ---------- the same rule in the existing-document workflow ----------


