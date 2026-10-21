"""Deleting a folder, and what happens if the same folder is uploaded again."""

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import bill, insert_rows, signed_in

# New folders take their display ids as internal ids, so the tests below can name them.
pytestmark = pytest.mark.usefixtures("readable_ids")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return application, db_path


def scan(name="page1.jpg"):
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), "white").save(buf, format="JPEG")
    return ("files", (name, buf.getvalue(), "image/jpeg"))


def worked_on(db_path, folder_id, batch_id):
    """A folder in the state a real one reaches: a document, assigned, verdicts, exported."""
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
            "VALUES (?, ?, 'drafted', 'accepted')", (f"{folder_id}AAA", batch_id))
        john = accounts.get_user_by_name(conn, "john")["id"]
        paul = accounts.get_user_by_name(conn, "paul")["id"]
        accounts.assign_folder(conn, folder_id, "new", john, paul)
        db.set_document_review(conn, f"{folder_id}AAA", "new", "reviewer", "accepted", "John")
        db.set_document_review(conn, f"{folder_id}AAA", "new", "admin", "accepted", "Paul")
        db.record_folder_export(conn, folder_id, "new", 1, "Paul")


def leftovers(db_path, folder_id):
    with db.connect(db_path) as conn:
        return {
            "assignments": conn.execute(
                "SELECT COUNT(*) FROM folder_assignments WHERE folder_id = ?",
                (folder_id,)).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM folder_events WHERE folder_id = ?",
                (folder_id,)).fetchone()[0],
            "exports": conn.execute(
                "SELECT COUNT(*) FROM folder_exports WHERE folder_id = ?",
                (folder_id,)).fetchone()[0],
            "reviews": conn.execute(
                "SELECT COUNT(*) FROM document_reviews WHERE document_id LIKE ?",
                (folder_id + "%",)).fetchone()[0],
            "documents": conn.execute(
                "SELECT COUNT(*) FROM new_documents WHERE document_id LIKE ?",
                (folder_id + "%",)).fetchone()[0],
        }


def upload(client, folder_id, replace=False):
    data = {"folder_display_id": folder_id}
    if replace:
        data["replace"] = "true"
    return client.post("/api/new/batches", files=[scan()], data=data)


# ---------- deleting a folder takes the work done on it ----------


def test_deleting_a_folder_leaves_nothing_of_it_behind(site):
    """The reported bug: everything below survived, keyed on ids that were free to reuse."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    batch = upload(paul, "E9001").json()["batch_id"]
    worked_on(db_path, "E9001", batch)
    assert leftovers(db_path, "E9001")["events"] > 0

    response = paul.delete("/api/new/folders",
                           params={"folder_id": "E9001", "reviewer": "Paul",
                                   "reason": "uploaded the wrong scans"})

    assert response.status_code == 200
    assert leftovers(db_path, "E9001") == {
        "assignments": 0, "events": 0, "exports": 0, "reviews": 0, "documents": 0}


def test_the_deletion_log_records_what_else_went_with_it(site):
    """The log is the only surviving trace, so it has to say more than "a folder"."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    batch = upload(paul, "E9001").json()["batch_id"]
    worked_on(db_path, "E9001", batch)

    paul.delete("/api/new/folders",
                params={"folder_id": "E9001", "reviewer": "Paul", "reason": "wrong scans"})

    entry = paul.get("/api/deletion-log").json()[0]
    assert entry["target_id"] == "E9001"
    assert entry["details"]["purged"]["folder_events"] > 0
    assert entry["details"]["purged"]["document_reviews"] > 0


def test_another_folder_is_not_touched_by_the_purge(site):
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])
    worked_on(db_path, "E9002", upload(paul, "E9002").json()["batch_id"])

    paul.delete("/api/new/folders",
                params={"folder_id": "E9001", "reviewer": "Paul", "reason": "wrong scans"})

    kept = leftovers(db_path, "E9002")
    assert kept["assignments"] == 1 and kept["events"] > 0 and kept["reviews"] == 2


# ---------- and uploading a folder that already exists is refused ----------


def test_a_folder_that_already_exists_is_not_quietly_uploaded_over(site):
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    response = upload(paul, "E9001")

    assert response.status_code == 409
    assert response.headers["x-folder-exists"] == "E9001"
    assert leftovers(db_path, "E9001")["documents"] == 1, "the upload went through anyway"


def test_the_refusal_says_what_is_in_the_way(site):
    """"Already exists" is not enough to decide on: it has to name what would be lost."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    detail = upload(paul, "E9001").json()["detail"]

    assert "1 document(s)" in detail
    assert "John" in detail          # who holds it
    assert "exported" in detail      # and that it has already gone out
    assert "cannot be undone" in detail


def test_a_reviewer_cannot_replace_a_folder_at_all(site):
    """Replacing destroys an editor's finished work, so it is not a reviewer's call."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])
    john = signed_in(application, "john", "john-password")

    refused = upload(john, "E9001")
    forced = upload(john, "E9001", replace=True)

    assert refused.status_code == 409
    assert "Ask an editor" in refused.json()["detail"]
    assert forced.status_code == 409, "a reviewer got through by asking twice"
    assert leftovers(db_path, "E9001")["documents"] == 1


def test_an_admin_who_says_so_replaces_the_folder_outright(site):
    """Nothing of the old folder may survive into the new one -- that is the whole point."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    response = upload(paul, "E9001", replace=True)

    assert response.status_code == 200
    assert leftovers(db_path, "E9001") == {
        "assignments": 0, "events": 0, "exports": 0, "reviews": 0, "documents": 0}


def test_a_folder_nobody_has_uploaded_before_is_not_in_anyone_s_way(site):
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")

    assert upload(paul, "E9001").status_code == 200


def test_a_reviewer_may_still_upload_a_folder_that_is_new(site):
    """The guardrail is about replacing work, not about reviewers uploading."""
    application, _ = site
    john = signed_in(application, "john", "john-password")

    assert upload(john, "E9003").status_code == 200


def test_an_upload_with_no_folder_id_is_unaffected(site):
    """Ungrouped uploads have no folder to collide with."""
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")

    assert paul.post("/api/new/batches", files=[scan()], data={}).status_code == 200
    assert paul.post("/api/new/batches", files=[scan()], data={}).status_code == 200


# ---------- and the replacement has to arrive before the old folder goes ----------


def junk(name="notes.txt"):
    return ("files", (name, b"not an image at all", "text/plain"))


def batch_ids(db_path, folder_id):
    with db.connect(db_path) as conn:
        return {row["batch_id"] for row in conn.execute(
            "SELECT batch_id FROM new_batches WHERE folder_id = ?", (folder_id,))}


def test_a_replacement_that_cannot_be_ingested_leaves_the_existing_folder_alone(site):
    """Reported in review: the purge ran first, so an unreadable scan left neither folder."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    response = paul.post("/api/new/batches", files=[junk()],
                         data={"folder_display_id": "E9001", "replace": "true"})

    assert response.status_code == 400
    assert leftovers(db_path, "E9001")["documents"] == 1


def test_a_failed_replacement_keeps_the_verdicts_and_the_assignment_too(site):
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    paul.post("/api/new/batches", files=[junk()],
              data={"folder_display_id": "E9001", "replace": "true"})

    kept = leftovers(db_path, "E9001")
    assert kept["assignments"] == 1 and kept["reviews"] == 2 and kept["exports"] == 1


def test_a_replacement_discards_the_old_folder_s_scans_and_keeps_its_own(site):
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])
    before = batch_ids(db_path, "E9001")

    new_batch = upload(paul, "E9001", replace=True).json()["batch_id"]

    assert batch_ids(db_path, "E9001") == {new_batch}
    assert new_batch not in before


def test_what_the_replaced_folder_spent_stays_in_the_total(site):
    """Nothing is refunded by replacing a folder, so the all-time cap must not move."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])
    with db.connect(db_path) as conn:
        bill(site[1], 0.25)
        conn.execute("UPDATE new_documents SET cost_estimate = 0.25 WHERE document_id = ?",
                     ("E9001AAA",))
        spent = db.get_total_cost(conn)

    upload(paul, "E9001", replace=True)

    with db.connect(db_path) as conn:
        assert db.get_total_cost(conn) == pytest.approx(spent)


def test_what_the_replaced_folder_s_split_proposal_cost_stays_in_the_total_too(site):
    """Reported in review: the snapshot totalled the drafts and not the batches, so replacing a
    folder handed its split-proposal spend back."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    batch = upload(paul, "E9001").json()["batch_id"]
    worked_on(db_path, "E9001", batch)
    with db.connect(db_path) as conn:
        bill(site[1], 0.11, "split")
        conn.execute("UPDATE new_batches SET split_cost_estimate = 0.11 WHERE batch_id = ?",
                     (batch,))
        spent = db.get_total_cost(conn)

    upload(paul, "E9001", replace=True)

    with db.connect(db_path) as conn:
        assert db.get_total_cost(conn) == pytest.approx(spent)


def test_the_replacement_is_written_to_the_deletion_log(site):
    """A backup only learns these copies are stale from the log."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    upload(paul, "E9001", replace=True)

    entry = paul.get("/api/deletion-log").json()[0]
    assert entry["target_id"] == "E9001"
    assert "replaced" in entry["reason"]
    # The folder lives on under the same id, so only these say which copies are stale.
    assert entry["details"]["document_ids"] == ["E9001AAA"]


def stored_pages(db_path, tmp_bucket, batch_id):
    """Which of a batch's recorded scans are actually still in the bucket."""
    from incipit.storage import BucketStorage
    storage = BucketStorage(tmp_bucket)
    with db.connect(db_path) as conn:
        keys = [row["bucket_key"] for row in conn.execute(
            "SELECT bucket_key FROM new_batch_images WHERE batch_id = ?", (batch_id,))]
    return {key: storage.exists(key) for key in keys}


def test_a_replacement_keeps_the_scans_it_just_uploaded(site, tmp_path):
    """Reported in review: batches of one folder share a bucket directory, so purging the old
    folder by prefix deleted the replacement's own images and left rows pointing at nothing."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E9001", upload(paul, "E9001").json()["batch_id"])

    new_batch = upload(paul, "E9001", replace=True).json()["batch_id"]

    present = stored_pages(db_path, f"file://{(tmp_path / 'bucket').as_posix()}", new_batch)
    assert present and all(present.values()), f"missing from the bucket: {present}"


def test_a_replacement_still_takes_the_old_folder_s_scans(site, tmp_path):
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    old_batch = upload(paul, "E9001").json()["batch_id"]
    worked_on(db_path, "E9001", old_batch)
    old_keys = list(stored_pages(db_path, f"file://{(tmp_path / 'bucket').as_posix()}", old_batch))

    upload(paul, "E9001", replace=True)

    from incipit.storage import BucketStorage
    storage = BucketStorage(f"file://{(tmp_path / 'bucket').as_posix()}")
    assert old_keys and not any(storage.exists(key) for key in old_keys)






def test_a_folder_in_another_case_is_refused_as_the_same_folder(site):
    """Found checking duplicates on 15 October: "e2003" went in beside an exported E2003."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E2003", upload(paul, "E2003").json()["batch_id"])
    assert upload(paul, "e2003").status_code == 409


@pytest.mark.parametrize("separate_apps", [False, True])
def test_concurrent_case_variants_cannot_both_create_a_folder(site, monkeypatch, separate_apps):
    application, db_path = site
    first = signed_in(application, "paul", "paul-password")
    if separate_apps:
        root = Path(db_path).parent
        settings = Settings(bucket_uri=(root / "bucket").as_uri(), db_path=db_path)
        application = review_server.create_app(
            settings, str(root / "documents.csv"), str(root / "export"))
    second = signed_in(application, "paul", "paul-password")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    original_ingest = review_server.run_ingest_batch

    def delayed_ingest(*args, **kwargs):
        if kwargs["folder_display_id"] == "E2003":
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        return original_ingest(*args, **kwargs)

    monkeypatch.setattr(review_server, "run_ingest_batch", delayed_ingest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(upload, first, "E2003")
        assert first_entered.wait(5)
        second_result = pool.submit(upload, second, "e2003")
        # If the second ingestion starts before the first exits, the two spellings have
        # acquired different locks and can each create a folder.
        try:
            assert not second_entered.wait(0.2)
        finally:
            release_first.set()
        assert first_result.result().status_code == 200
        assert second_result.result().status_code == 409
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM new_batches").fetchone()[0] == 1


def test_replacing_cannot_slip_a_second_spelling_of_a_folder_in(site):
    """A replacement purges only its exact ID, so the old spelling would have stayed live beside it."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    worked_on(db_path, "E2003", upload(paul, "E2003").json()["batch_id"])
    upload(paul, "e2003", replace=True)
    assert batch_ids(db_path, "e2003") == set()


def test_a_folder_uploaded_in_parts_still_takes_its_second_part(site):
    """The upload limit tells editors to send a large folder in parts, so an unsplit folder is not a duplicate."""
    application, db_path = site
    paul = signed_in(application, "paul", "paul-password")
    upload(paul, "E2004")
    assert upload(paul, "E2004").status_code == 200
