"""One Mentioned field, not two."""

import csv
import io
import json

import pytest

from incipit import accounts, db, export, new_document, prompting, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch, signed_in

DEFAULT = prompting.compile({})


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        seed_batch(conn, "B", 1, folder="E2002", name="E2002_Advice")
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
            "fields_json) VALUES ('E2002AAA', 'B', 'drafted', 'unreviewed', 'the text', ?)",
            (json.dumps({"names": ["Marsh, Henry", "Lovell Manufacturing Company"],
                         "author": "Hayes, Howard W", "recipient": "Gilmore, William Edgar",
                         "subjects": ["Phonograph"]}),))
        new_document.refresh_output_record(conn, "E2002AAA", BucketStorage(settings.bucket_uri))
    return application, settings


def detail(client, document_id="E2002AAA"):
    return client.get(f"/api/new/documents/{document_id}").json()


def save(client, document_id="E2002AAA", **changes):
    document = detail(client, document_id)
    payload = {
        "reviewed_text": document["transcription"]["text"], "fields": {**document["fields"], **changes},
        "review_status": "accepted", "reviewer": "Paul", "editors_notes": "",
        "title": document["metadata"]["title"],
    }
    return client.post(f"/api/new/documents/{document_id}/review", json=payload)


# ---------- the field that went ----------


def test_the_document_payload_has_no_second_mentioned_field(site):
    application, _ = site

    metadata = detail(signed_in(application))["metadata"]

    assert "mentioned" not in metadata


def test_the_surviving_field_is_the_names_list(site):
    application, _ = site

    document = detail(signed_in(application))

    assert document["fields"]["names"] == ["Marsh, Henry", "Lovell Manufacturing Company"]


def test_the_record_carries_one_index_too(site):
    application, settings = site

    stored = json.loads(BucketStorage(settings.bucket_uri).read_record("E2002", "E2002AAA"))

    assert stored["fields"]["names"] == ["Marsh, Henry", "Lovell Manufacturing Company"]
    assert "mentioned" not in stored["fields"]


# ---------- editing it ----------


def test_a_reviewers_edit_to_mentioned_is_saved(site):
    application, settings = site
    client = signed_in(application)

    save(client, names=["Prentice, William H", "Northern Lamp Co"])

    assert detail(client)["fields"]["names"] == ["Prentice, William H", "Northern Lamp Co"]


def test_the_edit_reaches_the_record(site):
    application, settings = site

    save(signed_in(application), names=["Prentice, William H"])

    stored = json.loads(BucketStorage(settings.bucket_uri).read_record("E2002", "E2002AAA"))
    assert stored["fields"]["names"] == ["Prentice, William H"]


def test_a_payload_that_still_sends_the_old_field_is_refused(site):
    """The review payload is checked against the registry, which has no `mentioned` field."""
    application, _ = site

    response = save(signed_in(application), mentioned="Someone, Else")

    assert response.status_code == 400


# ---------- the export ----------


def test_the_export_writes_the_index_into_name_s_mentioned(site):
    application, settings = site
    client = signed_in(application)
    save(client, names=["Marsh, Henry", "Lovell Manufacturing Company"])

    _, csv_bytes, _ = export.export_new_csv(settings)

    row = next(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert row["Name(s) Mentioned"] == "Marsh, Henry; Lovell Manufacturing Company"


# ---------- what the model is told ----------


def test_the_schema_no_longer_has_a_mentioned_field():
    schema = DEFAULT.document_schema["json_schema"]["schema"]
    assert "mentioned" not in schema["properties"]
    assert "mentioned" not in schema["required"]
    assert "names" in schema["required"]


# ---------- the two prompt rules that moved with it ----------


def test_a_subject_stays_general_and_never_names_a_person():
    rule = DEFAULT.document_prompt
    rule = rule[rule.index("- `subjects`:"):]
    rule = rule[:rule.index("\n-")] if "\n-" in rule else rule
    assert "Never a person's name" in rule


def test_a_letterhead_company_is_recorded_as_a_second_author():
    """Reported: "if a letter has the Letter head: Northewestern University, and was written by
    Charles D. Hurd, then the author field should now be ..."""
    rule = DEFAULT.document_prompt
    rule = rule[rule.index("- `author`:"):]
    rule = rule[:rule.index("\n-")] if "\n-" in rule else rule
    assert "letterhead" in rule.lower()
    assert "Smith, Jane; Acme Manufacturing Company" in rule
