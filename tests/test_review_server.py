import io
import json
import zipfile
from pathlib import Path

import pytest
from conftest import bill, insert_rows, seed_batch, seed_document, tiny_image_bytes, seed_draft
from fastapi.testclient import TestClient


from incipit import activity, db, docid, fields, modeling, openrouter, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage

# Confirmed documents take their display ids as internal ids, so the tests below can name them.
pytestmark = pytest.mark.usefixtures("readable_ids")

FAKE_RESULT = {
    "text": "The quick brown fox [unclear: jumped?] over the lazy dog.",
    "uncertain_words": ["[unclear: jumped?]"],
    "marginal_notes": ["see reel 215"],
    "places": ["Newark, N. J."],
    "names": ["Howard W. Hayes"],
    "challenges": ["typed"],
}


def _build_app(tmp_path: Path):
    db_path = str(tmp_path / "extraction.db")
    bucket_uri = f"file://{(tmp_path / 'bucket').as_posix()}"
    settings = Settings(bucket_uri=bucket_uri, db_path=db_path)

    db.init_db(db_path)
    export_dir = str(tmp_path / "export")
    app = review_server.create_app(settings, export_dir)
    return TestClient(app), db_path


def export_csv_text(zip_bytes: bytes) -> str:
    """The CSV inside an export zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        assert len(names) == 1, names
        return zf.read(names[0]).decode("utf-8")


# ---------- "Add New Document" workflow ----------


def test_get_pending_batches_lists_unconfirmed_batches_only(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post(
        "/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))], data={"folder_display_id": "D8026"}
    )
    batch_id = resp.json()["batch_id"]

    pending = client.get("/api/new/batches").json()
    assert len(pending) == 1
    assert pending[0]["batch_id"] == batch_id
    assert pending[0]["folder_id"] == "D8026"
    assert pending[0]["image_count"] == 1

    with db.connect(db_path) as conn:
        db.mark_batch_confirmed(conn, batch_id)
    assert client.get("/api/new/batches").json() == []


def test_a_batch_that_already_produced_documents_is_not_awaiting_a_split(tmp_path: Path):
    """Reported: "Awaiting split confirmation" over a folder that plainly exists."""
    client, db_path = _build_app(tmp_path)
    with db.connect(db_path) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, "
                     "status) VALUES ('B1', 'now', 2, 'E2002', 'uploaded')")
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, "
                     "status) VALUES ('B2', 'now', 2, 'E2226', 'uploaded')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status) "
                     "VALUES ('E2002AAA', 'B1', 'drafted')")

    pending = client.get("/api/new/batches").json()

    assert [b["batch_id"] for b in pending] == ["B2"],         "a batch whose documents already exist was still offered for splitting"


def test_post_new_batch_ingests_without_proposing_split(tmp_path: Path, monkeypatch):
    """Upload is ingest-only now -- split proposal is a separate call the frontend makes right
    after (see post_retry_proposal), not something POST /api/new/batches does itself."""
    client, _ = _build_app(tmp_path)
    propose_calls = []
    monkeypatch.setattr(
        "incipit.review_server.run_propose_split",
        lambda batch_id, settings_arg: propose_calls.append(batch_id),
    )

    resp = client.post(
        "/api/new/batches",
        files=[
            ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
            ("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "uploaded"
    assert body["proposal_error"] is None
    assert body["image_count"] == 2
    assert body["split_proposal_cursor"] == 0
    assert [p["proposed_new_document"] for p in body["pages"]] == [False, False]  # not proposed yet
    assert body["pages"][0]["image_url"] == f"/api/new/batches/{body['batch_id']}/images/0"
    assert propose_calls == []  # run_propose_split was never invoked by the upload itself


def test_post_retry_proposal_runs_the_first_proposal_attempt(tmp_path: Path, monkeypatch):
    """The same endpoint the "Retry Split Proposal" button uses also serves as the very first
    proposal attempt, called by the frontend immediately after a fresh upload completes."""
    client, db_path = _build_app(tmp_path)

    def fake_propose(batch_id, settings_arg):
        with db.connect(db_path) as conn:
            db.set_batch_split_proposal(conn, batch_id, {0: True, 1: False}, cost_estimate=0.0002)

    monkeypatch.setattr("incipit.review_server.run_propose_split", fake_propose)

    upload = client.post(
        "/api/new/batches",
        files=[
            ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
            ("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg")),
        ],
    )
    batch_id = upload.json()["batch_id"]

    resp = client.post(f"/api/new/batches/{batch_id}/retry-proposal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "split_proposed"
    assert body["proposal_error"] is None
    assert [p["proposed_new_document"] for p in body["pages"]] == [True, False]


def test_proposal_error_surfaces_from_retry_proposal_not_from_upload(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr(
        "incipit.review_server.run_propose_split",
        lambda batch_id, settings_arg: (_ for _ in ()).throw(RuntimeError("cap reached")),
    )

    upload = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    assert upload.json()["proposal_error"] is None  # upload itself never calls run_propose_split
    batch_id = upload.json()["batch_id"]

    resp = client.post(f"/api/new/batches/{batch_id}/retry-proposal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_error"] == "cap reached"
    assert body["status"] == "uploaded"  # split was never proposed


def test_get_batch_progress_reports_pages_still_unexamined(tmp_path: Path):
    client, db_path = _build_app(tmp_path)
    resp = client.post(
        "/api/new/batches",
        files=[
            ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
            ("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg")),
            ("files", ("0003.jpg", tiny_image_bytes(), "image/jpeg")),
        ],
    )
    batch_id = resp.json()["batch_id"]

    # Nothing examined yet -- all 3 pages still pending.
    assert client.get(f"/api/new/batches/{batch_id}/progress").json() == {"pending": 3}

    with db.connect(db_path) as conn:
        db.set_batch_split_proposal(conn, batch_id, {0: True}, proposal_cursor=1)
    assert client.get(f"/api/new/batches/{batch_id}/progress").json() == {"pending": 2}


def test_get_batch_progress_404_for_unknown_batch(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.get("/api/new/batches/NOPE/progress")
    assert resp.status_code == 404


def test_post_new_batch_suggests_doc_ids_when_folder_id_given(tmp_path: Path):
    client, _ = _build_app(tmp_path)

    resp = client.post(
        "/api/new/batches",
        data={"folder_display_id": "D8026"},
        files=[
            ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
            ("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder_id"] == "D8026"
    # No proposal has run yet (upload is ingest-only) -- suggest_document_ids still treats the
    # first page as always starting one document by convention.
    assert body["suggested_document_ids"] == ["D8026AAA"]


def test_post_new_batch_without_folder_id_has_no_suggestions(tmp_path: Path):
    client, _ = _build_app(tmp_path)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder_id"] == ""
    assert body["suggested_document_ids"] == []


def test_get_new_batch_image_returns_bytes(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    page_bytes = tiny_image_bytes()

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", page_bytes, "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    img_resp = client.get(f"/api/new/batches/{batch_id}/images/0")
    assert img_resp.status_code == 200
    assert img_resp.content == page_bytes


def test_delete_pending_batch_removes_it_from_the_pending_list(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    assert any(b["batch_id"] == batch_id for b in client.get("/api/new/batches").json())

    del_resp = client.request("DELETE", f"/api/new/batches/{batch_id}?{DEL_Q}")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"ok": True}

    assert client.get("/api/new/batches").json() == []
    assert client.get(f"/api/new/batches/{batch_id}/images/0").status_code == 404


def test_delete_pending_batch_404s_for_unknown_batch(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.request("DELETE", "/api/new/batches/NONEXISTENT")
    assert resp.status_code == 400


def test_delete_pending_batch_rejects_a_confirmed_batch(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    resp = client.request("DELETE", f"/api/new/batches/{batch_id}?{DEL_Q}")
    assert resp.status_code == 400


def test_post_batch_folder_id_sets_it_and_fills_in_suggested_names(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    assert resp.json()["folder_id"] == ""

    resp = client.post(f"/api/new/batches/{batch_id}/folder-id", json={"folder_display_id": "E2241"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder_id"] == "E2241"

    assert client.get(f"/api/new/batches/{batch_id}").json()["folder_id"] == "E2241"


def test_post_batch_folder_id_can_also_set_the_folder_name(tmp_path: Path, monkeypatch):
    """Regression test: fixing the Folder ID here used to be a dead end for folder_name -- a batch
    corrected this way (e.g."""
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    assert resp.json()["folder_name"] == ""

    resp = client.post(
        f"/api/new/batches/{batch_id}/folder-id",
        json={"folder_display_id": "E2241", "folder_name": "E2241_Equipment & Supplies"},
    )
    assert resp.status_code == 200
    assert resp.json()["folder_name"] == "E2241_Equipment & Supplies"

    assert client.get(f"/api/new/batches/{batch_id}").json()["folder_name"] == "E2241_Equipment & Supplies"


def test_post_batch_folder_id_with_a_blank_folder_name_leaves_an_existing_one_untouched(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post(
        "/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))],
        data={"folder_display_id": "E2241", "folder_name": "E2241_Equipment & Supplies"},
    )
    batch_id = resp.json()["batch_id"]

    # Only correcting the id here, folder_name omitted -- the already-good name must survive.
    resp = client.post(f"/api/new/batches/{batch_id}/folder-id", json={"folder_display_id": "E2241"})
    assert resp.json()["folder_name"] == "E2241_Equipment & Supplies"


def test_post_batch_folder_id_404s_for_unknown_batch(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/new/batches/NONEXISTENT/folder-id", json={"folder_display_id": "E2241"})
    assert resp.status_code == 404


def test_post_batch_folder_id_rejects_a_confirmed_batch(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    resp = client.post(f"/api/new/batches/{batch_id}/folder-id", json={"folder_display_id": "E2241"})
    assert resp.status_code == 400


def test_post_retry_proposal_resumes_and_completes_the_split(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    # Simulate a batch that failed outright before any proposal succeeded (cursor still at 0).
    with db.connect(db_path) as conn:
        assert db.get_batch(conn, batch_id)["split_proposal_cursor"] == 0

    def fake_propose_split(batch_id_arg, settings_arg):
        with db.connect(db_path) as conn:
            db.set_batch_split_proposal(conn, batch_id_arg, {0: True}, cost_estimate=0.0005, proposal_cursor=1)
        return {0: True}

    monkeypatch.setattr("incipit.review_server.run_propose_split", fake_propose_split)

    resp = client.post(f"/api/new/batches/{batch_id}/retry-proposal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_error"] is None
    assert body["suggested_document_ids"] == []  # no folder_id set in this test

    with db.connect(db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["split_proposal_cursor"] == 1
        assert batch["split_cost_estimate"] == 0.0005


def test_post_retry_proposal_reports_the_error_if_it_fails_again(tmp_path: Path, monkeypatch):
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    def fake_propose_split_fails(batch_id_arg, settings_arg):
        raise RuntimeError("still no OpenRouter credits")

    monkeypatch.setattr("incipit.review_server.run_propose_split", fake_propose_split_fails)

    resp = client.post(f"/api/new/batches/{batch_id}/retry-proposal")
    assert resp.status_code == 200  # the batch itself is fine -- only the retry attempt failed
    body = resp.json()
    assert "still no OpenRouter credits" in body["proposal_error"]


def test_post_retry_proposal_404s_for_unknown_batch(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/new/batches/NONEXISTENT/retry-proposal")
    assert resp.status_code == 404


def test_post_confirm_split_creates_documents_and_triggers_draft(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    drafted = []
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda settings_arg, options_arg=None, **_: drafted.append(1),
    )

    resp = client.post(
        f"/api/new/batches/{batch_id}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"created": ["NEWDOC001"], "error": None}
    assert drafted == [1]

    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "NEWDOC001") == "split_confirmed"


def test_post_confirm_split_drafts_only_the_documents_it_just_made(tmp_path: Path, monkeypatch):
    """Reported in review: without naming them the runner took the global pending queue, so
    confirming one folder spent the shared budget drafting somebody else's."""
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    asked = []
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda settings_arg, options_arg=None, document_ids=None, **_: asked.append(document_ids),
    )

    client.post(
        f"/api/new/batches/{batch_id}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )

    assert asked == [["NEWDOC001"]]


def test_post_confirm_split_surfaces_a_draft_error(tmp_path: Path, monkeypatch):
    """transcribe_new_document isolates per-document failures -- without surfacing the error here,
    a confirm whose auto-draft fails on every document (e.g."""
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    def fake_draft(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            db.upsert_new_document_error(conn, "NEWDOC001", "OpenRouter rejected the request (402)")

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", fake_draft)

    resp = client.post(
        f"/api/new/batches/{batch_id}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == ["NEWDOC001"]
    assert "402" in body["error"]


def test_post_confirm_split_surfaces_an_account_error_without_losing_confirmation(
    tmp_path: Path, monkeypatch
):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda *_: None)
    batch = client.post(
        "/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))]
    ).json()
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda *a, **kw: (_ for _ in ()).throw(openrouter.AccountError("OPENROUTER_API_KEY is not set")),
    )

    resp = client.post(
        f"/api/new/batches/{batch['batch_id']}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )

    assert resp.status_code == 200
    assert "API_KEY" in resp.json()["error"]
    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "NEWDOC001") == "split_confirmed"


def test_post_confirm_split_rejects_id_collision(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )
    upload = {"files": [("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))],
              "data": {"folder_display_id": "E2231"}}
    # Two batches of one folder, both awaiting their split.
    first = client.post("/api/new/batches", **upload).json()["batch_id"]
    batch_id = client.post("/api/new/batches", **upload).json()["batch_id"]
    client.post(f"/api/new/batches/{first}/confirm",
                json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    # NEWDOC001 already exists in this folder -- it must be rejected, not overwritten.
    resp = client.post(
        f"/api/new/batches/{batch_id}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )
    assert resp.status_code == 400


def test_new_document_title_override_round_trip(tmp_path: Path, monkeypatch):
    """A reviewer can replace the mechanically-derived title with a typed one; it persists, is
    flagged as an override, and clears back to the derived title when re-typed to match."""
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    def fake_draft(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            for doc_id in db.get_new_pending_document_ids(conn):
                seed_draft(
                    conn, doc_id, model="m", provider="p",
                    result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
                    ai_metadata={"title": "", "document_type": "Letter", "date": "1900-01-01",
                                 "author": "Lovell, Ada Mary", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
                    ocr_draft_text="d", output_json={}, cost_estimate=0.0001,
                )

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", fake_draft)
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    derived = "[NEWDOC001], Letter from Ada Mary Lovell, January 1st, 1900"
    base_review = {
        "document_type": "Letter", "date": "1900-01-01", "author": "Lovell, Ada Mary",
        "recipient": "", "place": "", "mentioned": "", "subjects": "", "reviewed_text": "t",
        "marginal_notes": [], "places": [], "names": [], "challenges": [],
        "review_status": "accepted", "reviewer": "jd", "editors_notes": "",
    }

    # A typed title different from the derived one is stored as an override and flagged as such.
    client.post("/api/new/documents/NEWDOC001/review", json={**base_review, "title": "Lovell's shipping memo"})
    detail = client.get("/api/new/documents/NEWDOC001").json()
    assert detail["metadata"]["title"] == "Lovell's shipping memo"
    assert detail["metadata"]["title_is_override"] is True
    assert client.get("/api/new/documents").json()[0]["title"] == "Lovell's shipping memo"  # queue listing too

    # Re-typing the title back to the derived value clears the override (title tracks metadata
    # again).
    client.post("/api/new/documents/NEWDOC001/review", json={**base_review, "title": derived})
    detail = client.get("/api/new/documents/NEWDOC001").json()
    assert detail["metadata"]["title"] == derived
    assert detail["metadata"]["title_is_override"] is False


def test_post_new_document_rename_updates_id_everywhere_including_export(tmp_path: Path, monkeypatch):
    # Own create_app call, same reasoning as test_post_new_export_writes_the_zip_to_the_export_dir:
    # keeps export output scoped to tmp_path instead of the repo's real data/export.
    db_path = str(tmp_path / "extraction.db")
    bucket_uri = f"file://{(tmp_path / 'bucket').as_posix()}"
    settings = Settings(bucket_uri=bucket_uri, db_path=db_path)
    app = review_server.create_app(settings)
    client = TestClient(app)

    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    page_a = tiny_image_bytes()
    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", page_a, "image/jpeg"))])
    batch_id = resp.json()["batch_id"]

    def fake_draft(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            for doc_id in db.get_new_pending_document_ids(conn):
                seed_draft(
                    conn, doc_id, model="m", provider="p",
                    result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
                    ai_metadata={"title": "", "document_type": "Letter", "date": "", "author": "", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
                    ocr_draft_text="d", output_json={}, cost_estimate=0.0001,
                )

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", fake_draft)
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    resp = client.post("/api/new/documents/NEWDOC001/rename", json={"new_display_id": "NEWDOC002"})
    assert resp.status_code == 200
    assert resp.json() == {"document_id": "NEWDOC001", "display_id": "NEWDOC002"}

    renamed = client.get("/api/new/documents/NEWDOC001").json()
    assert renamed["display_id"] == "NEWDOC002"
    img_resp = client.get("/api/new/documents/NEWDOC001/pages/0/image")
    assert img_resp.status_code == 200
    assert img_resp.content == page_a

    # Nothing in the bucket is named after the display id, so nothing moved.
    storage = BucketStorage(bucket_uri)
    with db.connect(db_path) as conn:
        page_key = db.get_new_document_pages(conn, "NEWDOC001")[0]["bucket_key"]
        folder_id = db.folder_of_document(conn, "NEWDOC001")
    assert storage.exists(page_key)
    record = json.loads(storage.read_record(folder_id, "NEWDOC001"))
    assert record["display_id"] == "NEWDOC002"

    client.post(
        "/api/new/documents/NEWDOC001/review",
        json={
            "document_type": "Letter", "date": "", "author": "", "recipient": "", "place": "",
            "mentioned": "", "subjects": "", "reviewed_text": "t", "marginal_notes": [], "places": [],
            "names": [], "challenges": [], "review_status": "accepted", "reviewer": "jdoe", "editors_notes": "",
        },
    )
    from incipit.export import export_new_csv
    _, csv_body, _ = export_new_csv(settings)
    csv_text = csv_body.decode("utf-8-sig")
    assert "NEWDOC002" in csv_text
    assert "NEWDOC001" not in csv_text




def test_post_new_document_rename_rejects_blank_id(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )
    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    resp = client.post("/api/new/documents/NEWDOC001/rename", json={"new_display_id": "   "})
    assert resp.status_code == 400


def test_post_new_document_rename_rejects_id_collision(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )
    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [
        {"display_id": "NEWDOC001", "sequence_indices": [0]}]})
    second = client.post("/api/new/batches", files=[("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg"))])
    client.post(f"/api/new/batches/{second.json()['batch_id']}/confirm", json={"groups": [
        {"display_id": "NEWDOC002", "sequence_indices": [0]}]})

    resp = client.post("/api/new/documents/NEWDOC001/rename", json={"new_display_id": "NEWDOC002"})
    assert resp.status_code == 400


def test_post_new_document_rename_404_for_unknown_document(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/new/documents/NOSUCHDOC/rename", json={"new_display_id": "NEWDOC002"})
    assert resp.status_code == 404


def test_post_new_review_rejects_invalid_status(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    # Invalid review_status is rejected before the document lookup even happens.
    resp = client.post(
        "/api/new/documents/NEWDOC001/review",
        json={"reviewed_text": "x", "review_status": "bogus", "reviewer": "", "editors_notes": ""},
    )
    assert resp.status_code == 400


def test_post_new_review_404_for_undrafted_document(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    resp = client.post(
        "/api/new/documents/NEWDOC001/review",
        json={"reviewed_text": "x", "review_status": "accepted", "reviewer": "", "editors_notes": ""},
    )
    assert resp.status_code == 404  # get_new_document_for_review only matches status='drafted'


def test_post_draft_pending_retries_undrafted_documents(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    def fake_draft_now(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            seed_draft(
                conn, "NEWDOC001", model="m", provider="p",
                result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
                ai_metadata={"title": "", "document_type": "", "date": "", "author": "", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
                ocr_draft_text="d", output_json={}, cost_estimate=0.0001,
            )

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", fake_draft_now)

    resp = client.post("/api/new/draft-pending")
    assert resp.status_code == 200
    assert resp.json() == {"drafted": 1, "error": None}


def test_post_draft_pending_surfaces_a_draft_error(tmp_path: Path, monkeypatch):
    """transcribe_new_document isolates per-document failures -- without surfacing the error here,
    a retry that fails on every still-pending document (e.g."""
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None
    )

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]})

    def fake_draft_fails(settings_arg, options_arg=None, **_):
        with db.connect(db_path) as conn:
            db.upsert_new_document_error(conn, "NEWDOC001", "OpenRouter rejected the request (402)")

    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", fake_draft_fails)

    resp = client.post("/api/new/draft-pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["drafted"] == 0
    assert "402" in body["error"]


def test_post_draft_pending_surfaces_an_account_error(tmp_path: Path, monkeypatch):
    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 1)
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda *a, **kw: (_ for _ in ()).throw(openrouter.AccountError("insufficient credits")),
    )

    resp = client.post("/api/new/draft-pending")

    assert resp.status_code == 200
    assert resp.json() == {"drafted": 0, "error": "insufficient credits"}


def test_get_pending_count_reflects_undrafted_documents(tmp_path: Path, monkeypatch):
    client, db_path = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", lambda settings_arg, options_arg=None, **_: None)

    resp = client.get("/api/new/pending-count")
    assert resp.json() == {"pending": 0}

    resp = client.post("/api/new/batches", files=[("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(
        f"/api/new/batches/{batch_id}/confirm",
        json={"groups": [{"display_id": "NEWDOC001", "sequence_indices": [0]}]},
    )

    resp = client.get("/api/new/pending-count")
    assert resp.json() == {"pending": 1}

    with db.connect(db_path) as conn:
        seed_draft(
            conn, "NEWDOC001", model="m", provider="p",
            result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
            ai_metadata={"title": "", "document_type": "", "date": "", "author": "", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
            ocr_draft_text="d", output_json={}, cost_estimate=0.0001,
        )

    resp = client.get("/api/new/pending-count")
    assert resp.json() == {"pending": 0}


def test_post_suggest_more_doc_ids_returns_empty_without_folder_id(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/new/suggest-doc-ids", json={"folder_id": "", "count": 3, "exclude": []})
    assert resp.json() == {"suggested_document_ids": []}


def _seed_folder_app(tmp_path, monkeypatch, docs):
    """Builds an app and seeds each (document_id, review_status) in `docs` as a drafted document,
    one batch per folder, so folder listing and export can be exercised end to end."""
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    client = TestClient(app)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", lambda s, options_arg=None, **_: None)

    # One batch per folder, the way a folder is uploaded, confirmed into its documents.
    folders = {}
    for doc_id, _ in docs:
        folders.setdefault(doc_id[:-3], []).append(doc_id)
    for folder, members in folders.items():
        resp = client.post("/api/new/batches", data={"folder_display_id": folder}, files=[
            ("files", (f"{i}.jpg", tiny_image_bytes(), "image/jpeg")) for i in range(len(members))])
        client.post(f"/api/new/batches/{resp.json()['batch_id']}/confirm", json={"groups": [
            {"display_id": doc_id, "sequence_indices": [i]} for i, doc_id in enumerate(members)]})
    for doc_id, review_status in docs:
        with db.connect(db_path) as conn:
            seed_draft(
                conn, doc_id, model="m", provider="p",
                result={"text": "t", "challenges": [], "uncertain_words": [], "marginal_notes": [], "places": [], "names": []},
                ai_metadata={"title": "", "document_type": "", "date": "", "author": "", "recipient": "", "place": "", "mentioned": "", "subjects": ""},
                ocr_draft_text="d", output_json={}, cost_estimate=0.0001,
            )
        if review_status == "accepted":
            client.post(f"/api/new/documents/{doc_id}/review", json={
                "document_type": "", "date": "", "author": "", "recipient": "", "place": "",
                "mentioned": "", "subjects": "", "reviewed_text": "t", "marginal_notes": [], "places": [],
                "names": [], "challenges": [], "review_status": "accepted", "reviewer": "jd", "editors_notes": "",
            })
    return client


def test_get_new_folders_lists_folders_with_approval(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [
        ("E2231AAA", "accepted"), ("E2231AAB", "accepted"),
        ("E2232AAA", "accepted"), ("E2232AAB", "unreviewed"),
    ])
    folders = {f["folder_id"]: f for f in client.get("/api/new/folders").json()}
    assert folders["E2231"]["all_approved"] is True
    assert folders["E2232"]["all_approved"] is False       # one doc still unreviewed
    assert folders["E2232"]["accepted_count"] == 1
    assert folders["E2232"]["document_count"] == 2


# ---------- folder-scoped drafting ----------

# What the model answers; each fake hands it to the extractor the caller passed.
FAKE_NEW_DOC_ANSWER = {
    "text": "Dear Sir, I enclose the report.", "uncertain_words": [],
    "document_type": "Letter", "date": "1900-01-01",
}


def _fake_draft(extract_result):
    return extract_result(dict(FAKE_NEW_DOC_ANSWER), {"cost": 0.0})


_TEST_SUFFIXES = [docid._index_to_suffix(i) for i in range(40)]


def _seed_pending_folder(tmp_path, monkeypatch, folder_id, count):
    """Builds an app with `count` confirmed-but-undrafted new_documents in one folder, created
    directly (not through the confirm-split API), so no auto-draft side effect fires."""
    db_path = str(tmp_path / "extraction.db")
    bucket_uri = f"file://{(tmp_path / 'bucket').as_posix()}"
    settings = Settings(bucket_uri=bucket_uri, db_path=db_path)
    db.init_db(db_path)
    storage = BucketStorage(bucket_uri)
    with db.connect(db_path) as conn:
        seed_batch(conn, f"BATCH-{folder_id}", count, folder=folder_id)
        for i in range(count):
            doc_id = f"{folder_id}{_TEST_SUFFIXES[i]}"
            bucket_key, bucket_uri_full = storage.upload_document(folder_id, doc_id, "0001.jpg",
                                                                  b"fake-jpeg-bytes")
            seed_document(conn, doc_id, f"BATCH-{folder_id}", [
                {"sequence_index": 0, "bucket_key": bucket_key, "bucket_uri": bucket_uri_full}])

    monkeypatch.setattr(
        "incipit.new_document.openrouter.call_openrouter",
        lambda messages, settings_arg, schema=None, extract_result=None: _fake_draft(extract_result),
    )
    app = review_server.create_app(settings)
    return TestClient(app), db_path


def test_get_pending_count_scoped_to_folder(tmp_path: Path, monkeypatch):
    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 3)
    _seed_pending_folder(tmp_path, monkeypatch, "E2232", 2)  # a second folder, must not be counted

    assert client.get("/api/new/pending-count").json()["pending"] == 5  # global
    assert client.get("/api/new/pending-count?folder_id=E2231").json()["pending"] == 3
    assert client.get("/api/new/pending-count?folder_id=E2232").json()["pending"] == 2
    assert client.get("/api/new/pending-count?folder_id=NOPE").json()["pending"] == 0


def test_post_new_folder_draft_drafts_only_the_requested_count(tmp_path: Path, monkeypatch):
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 5)

    resp = client.post("/api/new/folders/draft?folder_id=E2231&count=2")
    assert resp.status_code == 200
    assert resp.json() == {"drafted": 2, "attempted": 2, "remaining": 3, "error": None, "terminal": ""}

    with db.connect(db_path) as conn:
        statuses = [db.get_new_document_status(conn, f"E2231{suffix}") for suffix in _TEST_SUFFIXES[:5]]
    # Drafted in DocID order, exactly `count` of them -- the rest untouched.
    assert statuses == ["drafted", "drafted", "split_confirmed", "split_confirmed", "split_confirmed"]


def test_post_new_folder_draft_all_drafts_every_pending_document(tmp_path: Path, monkeypatch):
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 4)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")  # no count = "Draft All"
    assert resp.status_code == 200
    assert resp.json() == {"drafted": 4, "attempted": 4, "remaining": 0, "error": None, "terminal": ""}

    with db.connect(db_path) as conn:
        assert db.get_new_pending_document_ids_in_folder(conn, "E2231") == []


def test_folder_draft_caps_one_request_and_reports_what_is_left(tmp_path: Path, monkeypatch):
    """The fix for the 40-minute request."""
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 12)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")  # no count = "Draft All"

    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == review_server.MAX_DOCUMENTS_PER_DRAFT_REQUEST
    assert body["drafted"] == review_server.MAX_DOCUMENTS_PER_DRAFT_REQUEST
    assert body["remaining"] == 12 - review_server.MAX_DOCUMENTS_PER_DRAFT_REQUEST
    with db.connect(db_path) as conn:
        assert len(db.get_new_pending_document_ids_in_folder(conn, "E2231")) == body["remaining"]


def test_repeated_folder_draft_requests_finish_the_folder(tmp_path: Path, monkeypatch):
    """What the client's loop does."""
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 12)

    drafted, calls = 0, 0
    while True:
        resp = client.post("/api/new/folders/draft?folder_id=E2231")
        if resp.status_code == 404:  # nothing left pending
            break
        assert resp.status_code == 200
        body = resp.json()
        drafted += body["drafted"]
        calls += 1
        if body["remaining"] == 0 or body["drafted"] == 0:
            break

    assert drafted == 12
    assert calls == 3  # 12 documents at 5 per request
    with db.connect(db_path) as conn:
        assert db.get_new_pending_document_ids_in_folder(conn, "E2231") == []


def test_folder_draft_reports_an_account_failure_as_terminal(tmp_path: Path, monkeypatch):
    """No key / bad key / no credits is the only OpenRouter failure worth giving up on -- every
    remaining document would fail identically."""
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 6)

    def no_credits(messages, settings_arg, schema=None, extract_result=None):
        raise openrouter.AccountError("insufficient OpenRouter credits")

    monkeypatch.setattr("incipit.new_document.openrouter.call_openrouter", no_credits)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")

    assert resp.status_code == 200
    body = resp.json()
    assert "credits" in body["terminal"]
    assert body["drafted"] == 0
    assert body["remaining"] == 6  # nothing consumed
    with db.connect(db_path) as conn:
        # Still awaiting drafting, not marked errored -- the account is the problem, not the
        # folder.
        statuses = {db.get_new_document_status(conn, d) for d in db.get_new_pending_document_ids_in_folder(conn, "E2231")}
    assert statuses == {"split_confirmed"}


def test_folder_draft_retries_a_transient_failure_instead_of_erroring(tmp_path: Path, monkeypatch):
    """The central promise: OpenRouter is the only arbiter, and even it only condemns a document
    when the failure is one retrying cannot fix."""
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 2)
    attempts = []

    def flaky(messages, settings_arg, schema=None, extract_result=None):
        attempts.append(1)
        if len(attempts) <= 2:  # first document fails twice, then succeeds
            raise openrouter.TransientModelError("rate limited")
        return _fake_draft(extract_result)

    monkeypatch.setattr("incipit.new_document.openrouter.call_openrouter", flaky)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")

    assert resp.status_code == 200
    assert resp.json()["drafted"] == 2
    assert resp.json()["error"] is None
    with db.connect(db_path) as conn:
        assert db.get_new_pending_document_ids_in_folder(conn, "E2231") == []


def test_a_document_is_only_errored_after_every_retry_is_spent(tmp_path: Path, monkeypatch):
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 1)
    attempts = []

    def always_down(messages, settings_arg, schema=None, extract_result=None):
        attempts.append(1)
        raise openrouter.TransientModelError("upstream exhausted")

    monkeypatch.setattr("incipit.new_document.openrouter.call_openrouter", always_down)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")

    assert resp.status_code == 200
    # Four goes at the primary model, then one at the fallback -- which is down here too.
    assert len(attempts) == modeling.MAX_DOCUMENT_ATTEMPTS + 1
    assert resp.json()["drafted"] == 0
    with db.connect(db_path) as conn:
        assert db.get_new_document_status(conn, "E2231AAA") == "error"


def test_post_new_folder_draft_does_not_touch_other_folders(tmp_path: Path, monkeypatch):
    client, db_path = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 2)
    _seed_pending_folder(tmp_path, monkeypatch, "E2232", 2)

    resp = client.post("/api/new/folders/draft?folder_id=E2231")
    assert resp.status_code == 200
    assert resp.json()["drafted"] == 2

    with db.connect(db_path) as conn:
        # E2232 is untouched -- both of its documents are still pending.
        assert len(db.get_new_pending_document_ids_in_folder(conn, "E2232")) == 2


def test_post_new_folder_draft_404_when_folder_has_nothing_pending(tmp_path: Path, monkeypatch):
    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 1)
    resp = client.post("/api/new/folders/draft?folder_id=NOPE")
    assert resp.status_code == 404


def test_post_new_folder_draft_rejects_a_count_below_one(tmp_path: Path, monkeypatch):
    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 1)
    resp = client.post("/api/new/folders/draft?folder_id=E2231&count=0")
    assert resp.status_code == 400


# ---------- delete: existing-document workflow ----------

DEL_Q = "reviewer=Test+Reviewer&reason=duplicate+scan"


# ---------- delete: New Documents workflow ----------


def test_delete_new_document_removes_db_rows_and_bucket_images(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted"), ("E2231AAB", "accepted")])
    db_path = str(tmp_path / "extraction.db")
    storage = BucketStorage(f"file://{(tmp_path / 'bucket').as_posix()}")
    with db.connect(db_path) as conn:
        bucket_key = db.get_new_document_pages(conn, "E2231AAA")[0]["bucket_key"]
    assert storage.exists(bucket_key)

    resp = client.delete(f"/api/new/documents/E2231AAA?{DEL_Q}")
    assert resp.status_code == 200

    assert not storage.exists(bucket_key)
    # The sibling is shown under the freed id rather than leaving a hole.
    assert client.get("/api/new/documents/E2231AAA").status_code == 404
    assert client.get("/api/new/documents/E2231AAB").json()["display_id"] == "E2231AAA"
    folders = {f["folder_id"]: f for f in client.get("/api/new/folders").json()}
    assert folders["E2231"]["document_count"] == 1


def test_delete_new_document_404_for_unknown_id(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])
    resp = client.delete(f"/api/new/documents/NOPE?{DEL_Q}")
    assert resp.status_code == 404


def test_delete_new_document_frees_the_docid_for_reuse(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])
    assert client.delete(f"/api/new/documents/E2231AAA?{DEL_Q}").status_code == 200

    resp = client.post("/api/new/suggest-doc-ids", json={"folder_id": "E2231", "count": 1, "exclude": []})
    assert resp.status_code == 200
    # AAA is free again -- the deleted document's own slot is the very next suggestion.
    assert resp.json()["suggested_document_ids"] == ["E2231AAA"]


def test_delete_new_folder_removes_every_document_and_their_images(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [
        ("E2231AAA", "accepted"), ("E2231AAB", "accepted"), ("E2232AAA", "accepted"),
    ])
    db_path = str(tmp_path / "extraction.db")
    storage = BucketStorage(f"file://{(tmp_path / 'bucket').as_posix()}")
    with db.connect(db_path) as conn:
        e2231_keys = [
            db.get_new_document_pages(conn, doc_id)[0]["bucket_key"] for doc_id in ("E2231AAA", "E2231AAB")
        ]

    resp = client.delete(f"/api/new/folders?folder_id=E2231&{DEL_Q}")
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 2

    assert client.get("/api/new/documents/E2231AAA").status_code == 404
    assert client.get("/api/new/documents/E2231AAB").status_code == 404
    for key in e2231_keys:
        assert not storage.exists(key)
    # The other folder is untouched, and the deleted folder no longer appears at all.
    folders = {f["folder_id"]: f for f in client.get("/api/new/folders").json()}
    assert "E2231" not in folders
    assert folders["E2232"]["document_count"] == 1


def test_delete_new_folder_404_when_empty_or_unknown(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])
    resp = client.delete(f"/api/new/folders?folder_id=NOPE&{DEL_Q}")
    assert resp.status_code == 404


def test_upload_rejects_too_many_files(tmp_path: Path, monkeypatch):
    """A page-count ceiling still guards against a mis-picked folder (the picker aimed at a whole
    drive)."""
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr(review_server, "MAX_UPLOAD_FILES", 3)
    files = [("files", (f"{i}.jpg", tiny_image_bytes(), "image/jpeg")) for i in range(4)]

    resp = client.post("/api/new/batches", files=files)
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]
    # Nothing was ingested -- the batch is rejected whole, not partially created.
    assert client.get("/api/new/batches").json() == []


def test_upload_default_ceiling_allows_a_real_scanning_session(tmp_path: Path):
    """Real archival folders run 70-91 scans; the default must not sit anywhere near that."""
    assert review_server.MAX_UPLOAD_FILES >= 200


def test_upload_byte_ceiling_is_generous_but_finite(tmp_path: Path, monkeypatch):
    """Ordinary full-resolution folders fit, while one request cannot consume unlimited disk."""
    assert review_server.MAX_UPLOAD_TOTAL_BYTES > 500 * 1024 * 1024
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    monkeypatch.setattr(review_server, "MAX_UPLOAD_TOTAL_BYTES", 1024)
    resp = client.post("/api/new/batches", files=[
        ("files", (f"{i}.jpg", tiny_image_bytes(), "image/jpeg")) for i in range(2)
    ])
    assert resp.status_code == 413
    assert "upload limit" in resp.json()["detail"]
    assert client.get("/api/new/batches").json() == []


def test_upload_ignores_a_folder_compilation_pdf(tmp_path: Path, monkeypatch):
    """Scan folders routinely also contain one PDF of every page bound together."""
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    resp = client.post("/api/new/batches", files=[
        ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
        ("files", ("E2241_all_scans.pdf", b"%PDF-1.7" + b"\x00" * (512 * 1024), "application/pdf")),
        ("files", ("Thumbs.db", b"\x00" * 4096, "application/octet-stream")),
        ("files", ("0002.jpg", tiny_image_bytes(), "image/jpeg")),
    ])
    assert resp.status_code == 200
    pages = resp.json()["pages"]
    assert [p["original_filename"] for p in pages] == ["0001.jpg", "0002.jpg"]


def test_upload_never_reads_the_bytes_of_a_skipped_file(tmp_path: Path, monkeypatch):
    """The whole point of skipping by extension is to avoid pulling a multi-hundred-MB compilation
    PDF into memory just to discard it."""
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    seen: list[str] = []
    real_init = review_server._LazyUploads.__init__

    def tracking_init(self, uploads):
        for u in uploads:
            original_read = u.file.read
            name = u.filename

            def read(*a, _n=name, _r=original_read, **k):
                seen.append(_n)
                return _r(*a, **k)

            u.file.read = read
        real_init(self, uploads)

    monkeypatch.setattr(review_server._LazyUploads, "__init__", tracking_init)
    resp = client.post("/api/new/batches", files=[
        ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg")),
        ("files", ("compilation.pdf", b"%PDF-1.7" + b"\x00" * 4096, "application/pdf")),
    ])
    assert resp.status_code == 200
    assert "0001.jpg" in seen
    assert "compilation.pdf" not in seen


def test_upload_of_only_skippable_files_fails_cleanly(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/new/batches", files=[
        ("files", ("all_scans.pdf", b"%PDF-1.7" + b"\x00" * 1024, "application/pdf")),
    ])
    assert resp.status_code == 400
    # Must not read as "your upload never arrived" -- it says what was skipped and why.
    detail = resp.json()["detail"]
    assert "were scan images" in detail
    assert "all_scans.pdf" in detail
    assert client.get("/api/new/batches").json() == []


def test_upload_reads_one_file_at_a_time(tmp_path: Path, monkeypatch):
    """Peak memory must not scale with folder size: the bytes are pulled as run_ingest_batch
    consumes them, so at no point are all N files resident at once (the old comprehension did
    exactly that, which is what the byte cap was wrongly compensating for)."""
    client, _ = _build_app(tmp_path)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, settings_arg: None)

    live = 0
    peak = 0
    real_normalize = review_server.run_ingest_batch.__globals__["imaging"].normalize_image_bytes

    def counting_normalize(content, filename):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            return real_normalize(content, filename)
        finally:
            live -= 1

    monkeypatch.setattr(
        review_server.run_ingest_batch.__globals__["imaging"], "normalize_image_bytes", counting_normalize
    )
    resp = client.post("/api/new/batches", files=[
        ("files", (f"{i}.jpg", tiny_image_bytes(), "image/jpeg")) for i in range(10)
    ])
    assert resp.status_code == 200
    assert len(resp.json()["pages"]) == 10
    # Images are handled strictly one at a time as they stream in.
    assert peak == 1


# ---------- deletion audit log ----------


def test_delete_refuses_without_a_real_reason(tmp_path: Path, monkeypatch):
    """The session supplies the actor, but a useful explanation is still mandatory."""
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])

    assert client.delete("/api/new/documents/E2231AAA").status_code == 400
    assert client.delete("/api/new/documents/E2231AAA?reviewer=Jo").status_code == 400
    # A token reason ("x") is refused too -- the log is only useful if the reason says something.
    assert client.delete("/api/new/documents/E2231AAA?reviewer=Jo&reason=x").status_code == 400
    # Nothing was actually deleted by any of those, and nothing was logged.
    assert client.get("/api/new/documents/E2231AAA").status_code == 200
    assert client.get("/api/deletion-log").json() == []


def test_delete_records_who_deleted_what_and_why(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])

    resp = client.delete("/api/new/documents/E2231AAA?reviewer=Ada+L&reason=scanned+twice+by+mistake")
    assert resp.status_code == 200

    entries = client.get("/api/deletion-log").json()
    assert len(entries) == 1
    entry = entries[0]
    # The forged query value is ignored; attribution comes from the authenticated account.
    assert entry["reviewer"] == "Test Admin"
    assert entry["reason"] == "scanned twice by mistake"
    assert entry["target_kind"] == "new_document"
    assert entry["target_id"] == "E2231AAA"
    assert entry["workflow"] == "new"
    assert entry["deleted_at"]
    # The log outlives the document it describes -- that's the whole point.
    assert client.get("/api/new/documents/E2231AAA").status_code == 404


def test_delete_folder_records_one_entry_naming_every_member(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [
        ("E2231AAA", "accepted"), ("E2231AAB", "accepted"), ("E2232AAA", "accepted"),
    ])

    resp = client.delete("/api/new/folders?folder_id=E2231&reviewer=Ada+L&reason=folder+rescanned")
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 2

    entries = client.get("/api/deletion-log").json()
    # One readable entry for the whole folder, not one per document...
    assert len(entries) == 1
    assert entries[0]["target_kind"] == "new_folder"
    assert entries[0]["target_id"] == "E2231"
    assert "2 documents" in entries[0]["summary"]
    # ...but the members are still recorded, so the entry says exactly what went.
    assert entries[0]["details"]["document_ids"] == ["E2231AAA", "E2231AAB"]


def test_deleting_a_document_does_not_refund_the_cost_cap(tmp_path: Path, monkeypatch):
    """Spend already paid to OpenRouter must keep counting against the all-time cap after the row
    it was recorded on is gone -- otherwise tidying the archive quietly buys back budget."""
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted")])
    bill(tmp_path / "extraction.db", 0.004)
    before = client.get("/api/settings").json()["total_cost"]
    assert before > 0  # the drafting was billed

    assert client.delete(f"/api/new/documents/E2231AAA?{DEL_Q}").status_code == 200

    after = client.get("/api/settings").json()["total_cost"]
    assert after == pytest.approx(before)


def test_deletion_log_is_newest_first(tmp_path: Path, monkeypatch):
    client = _seed_folder_app(tmp_path, monkeypatch, [("E2231AAA", "accepted"), ("E2231AAB", "accepted")])

    client.delete("/api/new/documents/E2231AAA?reviewer=First&reason=first+deletion")
    client.delete("/api/new/documents/E2231AAB?reviewer=Second&reason=second+deletion")

    entries = client.get("/api/deletion-log").json()
    assert [e["reviewer"] for e in entries] == ["Test Admin", "Test Admin"]
    assert [e["reason"] for e in entries] == ["second deletion", "first deletion"]


def _settings_payload(**overrides):
    payload = {"max_total_cost": 2.0, "new_document_batch_size": 5}
    payload.update(overrides)
    return payload


def test_post_settings_rejects_a_non_positive_max_total_cost(tmp_path: Path):
    client, _ = _build_app(tmp_path)
    resp = client.post("/api/settings", json=_settings_payload(max_total_cost=0))
    assert resp.status_code == 400


# ---------- fixing a split after drafting ----------


def _confirmed_folder(tmp_path, monkeypatch):
    """A confirmed folder -- the state a reviewer is in when they notice a bad boundary."""
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings)
    client = TestClient(app)
    monkeypatch.setattr("incipit.review_server.run_propose_split", lambda batch_id, s: None)
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents", lambda s, options_arg=None, **_: None)

    resp = client.post("/api/new/batches", files=[
        ("files", (f"{i}.jpg", tiny_image_bytes(), "image/jpeg")) for i in range(4)
    ], data={"folder_display_id": "E2241"})
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/confirm", json={"groups": [
        {"display_id": "E2241AAA", "sequence_indices": [0]},
        {"display_id": "E2241AAB", "sequence_indices": [1]},
        {"display_id": "E2241AAC", "sequence_indices": [2, 3]},
    ]})
    return client, db_path


def test_get_resplit_seeds_the_screen_with_current_grouping(tmp_path, monkeypatch):
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)

    resp = client.get("/api/new/resplit?document_ids=E2241AAA,E2241AAB")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["sequence_index"] for p in body["pages"]] == [0, 1]
    assert [p["starts_new_document"] for p in body["pages"]] == [True, True]
    # The ids being replaced are offered back first, so a merge keeps the earlier name by default.
    assert body["suggested_document_ids"][:2] == ["E2241AAA", "E2241AAB"]


def test_post_resplit_redrafts_only_what_it_rebuilt(tmp_path, monkeypatch):
    """Same leak as post_confirm_split: the rebuilt documents, not everyone's pending queue."""
    client, _ = _confirmed_folder(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda settings_arg, options_arg=None, document_ids=None, **_: asked.append(document_ids),
    )

    client.post("/api/new/resplit", json={
        "document_ids": ["E2241AAA", "E2241AAB"],
        "groups": [{"display_id": "E2241AAA", "sequence_indices": [0, 1]}],
    })

    assert asked == [["E2241AAA"]]


def test_post_resplit_merges_and_redrafts(tmp_path, monkeypatch):
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)

    resp = client.post("/api/new/resplit", json={
        "document_ids": ["E2241AAA", "E2241AAB"],
        "groups": [{"display_id": "E2241AAA", "sequence_indices": [0, 1]}],
    })
    assert resp.status_code == 200
    assert resp.json()["created"] == ["E2241AAA"]
    assert resp.json()["replaced"] == ["E2241AAA", "E2241AAB"]
    # Absorbing E2241AAB frees its suffix, so the folder closes up behind it -- a gap in the
    # sequence would export as a lost document rather than a correction.
    assert resp.json()["renumbered"] == {"E2241AAC": "E2241AAB"}

    with db.connect(db_path) as conn:
        # The rebuilt document holds both pages...
        assert len(db.get_new_document_pages(conn, "E2241AAA")) == 2
        # ...and the untouched third document, with its own two pages, is now shown as E2241AAB.
        assert len(db.get_new_document_pages(conn, "E2241AAC")) == 2
        assert db.display_id_of(conn, "E2241AAC") == "E2241AAB"
        assert db.get_new_document(conn, "E2241AAB") is None
    # The folder still holds the untouched document alongside the merged one.
    folders = {f["folder_id"]: f for f in client.get("/api/new/folders").json()}
    assert folders["E2241"]["document_count"] == 2


def _drafted_folder(tmp_path, monkeypatch):
    """A confirmed folder whose documents also carry a transcription -- the state merging needs,
    since merging keeps the existing text rather than producing new text."""
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)
    with db.connect(db_path) as conn:
        for document_id in ("E2241AAA", "E2241AAB", "E2241AAC"):
            seed_draft(
                conn,
                document_id,
                model="qwen",
                provider="openrouter",
                result={
                    "text": f"text of {document_id}",
                    "challenges": [],
                    "uncertain_words": [],
                    "marginal_notes": [],
                    "places": [],
                    "names": [],
                },
                ai_metadata={"title": document_id, "document_type": "Letter", "date": "", "author": "",
                             "recipient": "", "place": "", "mentioned": "", "subjects": ""},
                ocr_draft_text=None,
                output_json={},
                cost_estimate=0.0,
            )
    return client, db_path


def _stub_merge_model(monkeypatch):
    """Replaces the single text-only metadata call a merge makes."""
    def fake_call(messages, settings, schema=None, extract_result=None):
        return extract_result(
            {"document_type": "Letter", "date": "1889-03-14", "author": "Hayes, Howard W",
             "recipient": "Lovell, Ada Mary", "place": "Newark, N.J.", "mentioned": [], "subjects": []},
            {"cost": 0.0002},
        )

    monkeypatch.setattr("incipit.merge.openrouter.call_openrouter", fake_call)


def test_get_merge_previews_without_changing_anything(tmp_path, monkeypatch):
    client, db_path = _drafted_folder(tmp_path, monkeypatch)

    resp = client.get("/api/new/merge?document_ids=E2241AAA,E2241AAB")

    assert resp.status_code == 200
    body = resp.json()
    assert body["target_document_id"] == "E2241AAA"
    assert body["page_count"] == 2
    assert body["all_drafted"] is True
    # The reviewer is told up front which other documents the merge will rename.
    assert body["renumber"] == {"E2241AAC": "E2241AAB"}
    with db.connect(db_path) as conn:
        assert db.get_new_document(conn, "E2241AAB") is not None


def test_post_merge_keeps_the_text_and_renumbers_the_folder(tmp_path, monkeypatch):
    client, db_path = _drafted_folder(tmp_path, monkeypatch)
    _stub_merge_model(monkeypatch)

    resp = client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAB"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == "E2241AAA"
    assert body["page_count"] == 2
    assert body["renumbered"] == {"E2241AAC": "E2241AAB"}
    with db.connect(db_path) as conn:
        row = db.get_new_document(conn, "E2241AAA")
    # Kept, not redrafted -- the endpoint never calls run_transcribe_new_documents.
    assert "text of E2241AAA" in row["text"] and "text of E2241AAB" in row["text"]
    assert fields.drafted(row)["author"] == "Hayes, Howard W"
    assert row["review_status"] == "unreviewed"


def test_post_merge_never_redrafts(tmp_path, monkeypatch):
    """The distinction that makes merging cheap."""
    client, db_path = _drafted_folder(tmp_path, monkeypatch)
    _stub_merge_model(monkeypatch)
    drafted = []
    monkeypatch.setattr(
        "incipit.review_server.run_transcribe_new_documents",
        lambda s, options_arg=None: drafted.append(True),
    )

    assert client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAB"]}).status_code == 200
    assert drafted == []


def test_post_merge_accepts_non_adjacent_documents(tmp_path, monkeypatch):
    client, db_path = _drafted_folder(tmp_path, monkeypatch)
    _stub_merge_model(monkeypatch)

    resp = client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAC"]})

    assert resp.status_code == 200
    assert resp.json()["document_id"] == "E2241AAA"
    with db.connect(db_path) as conn:
        merged = db.get_new_document(conn, "E2241AAA")
        assert db.get_new_document(conn, "E2241AAB") is not None
        assert db.get_new_document(conn, "E2241AAC") is None
        assert "text of E2241AAA" in merged["text"]
        assert "text of E2241AAC" in merged["text"]
        assert "text of E2241AAB" not in merged["text"]


def test_post_merge_rejects_an_undrafted_document(tmp_path, monkeypatch):
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)  # confirmed but never drafted
    _stub_merge_model(monkeypatch)

    resp = client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAB"]})

    assert resp.status_code == 400
    assert "drafted first" in resp.json()["detail"]


def test_post_merge_reports_the_spend_cap_as_busy_not_as_a_crash(tmp_path, monkeypatch):
    client, db_path = _drafted_folder(tmp_path, monkeypatch)
    _stub_merge_model(monkeypatch)
    with db.connect(db_path) as conn:
        conn.execute("UPDATE app_settings SET max_total_cost = 0.0 WHERE id = 1")

    resp = client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAB"]})

    assert resp.status_code == 409
    assert "cap" in resp.json()["detail"]
    with db.connect(db_path) as conn:
        assert db.get_new_document(conn, "E2241AAB") is not None


def test_post_merge_is_serialized_against_other_costed_work(tmp_path, monkeypatch):
    """Merging spends money, so it takes the same single-flight slot every other costed path does."""
    client, db_path = _drafted_folder(tmp_path, monkeypatch)
    _stub_merge_model(monkeypatch)

    with activity.exclusive("confirming", "something else already running"):
        resp = client.post("/api/new/merge", json={"document_ids": ["E2241AAA", "E2241AAB"]})

    assert resp.status_code == 409


def test_post_resplit_rejects_a_split_that_loses_a_page(tmp_path, monkeypatch):
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)

    resp = client.post("/api/new/resplit", json={
        "document_ids": ["E2241AAC"],
        "groups": [{"display_id": "E2241AAC", "sequence_indices": [2]}],  # page 3 dropped
    })
    assert resp.status_code == 400
    assert "exactly the pages" in resp.json()["detail"]
    with db.connect(db_path) as conn:
        assert len(db.get_new_document_pages(conn, "E2241AAC")) == 2  # unchanged


def test_get_resplit_404s_for_an_unknown_document(tmp_path, monkeypatch):
    client, db_path = _confirmed_folder(tmp_path, monkeypatch)
    resp = client.get("/api/new/resplit?document_ids=NOPE")
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


# ---------- existing documents: archival folders + choosing what to draft ----------

# ---------- Wave 1: folder-scoped pending batches ----------


def _batch_in_folder(client, folder_id: str, name: str):
    """Uploads a one-image batch and tags it with an archival folder id."""
    resp = client.post("/api/new/batches", files=[("files", (name, tiny_image_bytes(), "image/jpeg"))])
    batch_id = resp.json()["batch_id"]
    client.post(f"/api/new/batches/{batch_id}/folder-id", json={"folder_display_id": folder_id})
    return batch_id


def _batches_app(tmp_path, monkeypatch):
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
                        db_path=str(tmp_path / "extraction.db"))
    monkeypatch.setattr("incipit.review_server.run_propose_split",
                        lambda batch_id, settings_arg: None)
    return TestClient(review_server.create_app(settings, str(tmp_path / "export")))


def test_pending_batches_scope_to_one_folder(tmp_path: Path, monkeypatch):
    """Inside a folder, another folder's 'awaiting split confirmation' is noise -- and worse, it
    invites resuming a split belonging to work the reviewer isn't doing."""
    client = _batches_app(tmp_path, monkeypatch)
    a = _batch_in_folder(client, "E2231", "a.jpg")
    b = _batch_in_folder(client, "E2232", "b.jpg")

    assert {x["batch_id"] for x in client.get("/api/new/batches").json()} == {a, b}
    assert [x["batch_id"] for x in client.get("/api/new/batches?folder_id=E2231").json()] == [a]
    assert [x["batch_id"] for x in client.get("/api/new/batches?folder_id=E2232").json()] == [b]
    assert client.get("/api/new/batches?folder_id=NOPE").json() == []


def test_pending_batches_scope_finds_untagged_batches(tmp_path: Path, monkeypatch):
    """A batch uploaded before a Folder ID was set has folder_id "" -- scoping to "" must find it
    rather than silently hiding it from every view."""
    client = _batches_app(tmp_path, monkeypatch)
    resp = client.post("/api/new/batches", files=[("files", ("x.jpg", tiny_image_bytes(), "image/jpeg"))])
    untagged = resp.json()["batch_id"]

    assert [x["batch_id"] for x in client.get("/api/new/batches?folder_id=").json()] == [untagged]


# ---------- Wave 2: concurrent costed operations ----------


def test_new_document_drafting_shares_the_lock_with_existing_drafting(tmp_path: Path, monkeypatch):
    """Both workflows drive the same OCR and the same spend cap, so they must contend for one lock
    -- otherwise two 'different' drafts still overshoot the cap together."""
    from incipit import activity

    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 1)
    activity.clear()
    try:
        with activity.exclusive("drafting", "Transcribing existing documents"):
            resp = client.post("/api/new/draft-pending")
        assert resp.status_code == 409
    finally:
        activity.clear()


