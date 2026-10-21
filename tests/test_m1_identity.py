"""Covers docs/specs/M1-identity-foundations.md: internal ids never change, display ids are scoped."""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from incipit import collab, db, docid, export, folder_exports, review_server, split
from incipit.storage import BucketStorage
from conftest import make_app, signed_in, tiny_image_bytes, seed_draft

ROOT = Path(__file__).resolve().parents[1]
FILES = ("Page_1.jpg", "Page_2.jpg")
INTERNAL_FOLDER = re.compile(r"^f[0-9a-f]{16}$")
INTERNAL_DOCUMENT = re.compile(r"^d[0-9a-f]{16}$")
DRAFT = {"text": "a draft", "challenges": [], "uncertain_words": [], "marginal_notes": [],
         "places": [], "names": []}
NO_METADATA = {"title": "", "document_type": "", "date": "", "author": "", "recipient": "",
               "place": "", "subjects": ""}
ACCEPT = {"reviewed_text": "accepted text", "review_status": "accepted", "title": "",
          "marginal_notes": [], "places": [], "names": [], "challenges": [], "editors_notes": ""}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """An app with paul (admin) and john (reviewer), and no model calls."""
    app, settings = make_app(tmp_path, monkeypatch)
    monkeypatch.setattr(review_server, "run_propose_split", lambda *a, **k: None)
    monkeypatch.setattr(review_server, "run_transcribe_new_documents", lambda *a, **k: None)
    return signed_in(app, "paul"), settings


def upload(client, folder, names, files=FILES):
    """Uploads `files` as `folder` and confirms one document per scan; returns (folder_id, ids)."""
    batch = client.post("/api/new/batches", data={"folder_display_id": folder},
                        files=[("files", (f, tiny_image_bytes(), "image/jpeg")) for f in files]).json()
    confirmed = client.post(f"/api/new/batches/{batch['batch_id']}/confirm", json={"groups": [
        {"display_id": name, "sequence_indices": [i]} for i, name in enumerate(names)]}).json()
    return batch["folder_id"], confirmed["created"]


def drafted(settings, *document_ids):
    with db.connect(settings.db_path) as conn:
        for document_id in document_ids:
            seed_draft(conn, document_id, model="m", provider="p",
                                      result=DRAFT, ai_metadata=NO_METADATA, ocr_draft_text="",
                                      output_json={}, cost_estimate=0.0)


def bucket_keys(settings, prefix):
    root = Path(settings.bucket_uri[len("file://"):])
    return {p.relative_to(root).as_posix() for p in (root / prefix).rglob("*") if p.is_file()}


def page_keys(settings, document_id):
    with db.connect(settings.db_path) as conn:
        return [p["bucket_key"] for p in db.get_new_document_pages(conn, document_id)]


# ---------- AC-1: internal ids never change ----------


def test_ac1_a_documents_internal_id_survives_a_rename_and_a_renumbering(site):
    client, settings = site
    _, (first, second) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    drafted(settings, first, second)
    client.post(f"/api/new/documents/{second}/rename", json={"new_display_id": "E2002AAC"})
    client.delete(f"/api/new/documents/{first}", params={"reason": "a test"})

    assert client.get(f"/api/new/documents/{second}").json()["display_id"] == "E2002AAA"


def test_ac1_a_folders_internal_id_survives_a_rename(site):
    client, _ = site
    folder_id, _ = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    client.post(f"/api/new/folders/{folder_id}/name", json={"folder_name": "Correspondence"})

    titles = {f["folder_id"]: f["folder_title"] for f in client.get("/api/new/folders").json()}
    assert titles == {folder_id: "E2002_Correspondence"}


# ---------- AC-2: two tabs, the same names, nothing shared ----------


@pytest.fixture
def two_tabs(site):
    """Folder "E2002" with documents X1AAA and X1AAB and the same scan names in tab 1 and tab 2."""
    client, settings = site
    first_folder, first = upload(client, "E2002", ["X1AAA", "X1AAB"])
    with db.connect(settings.db_path) as conn:
        conn.execute("INSERT INTO tabs (id, slug, label, created_at) "
                     "VALUES (2, 'second', 'Second', datetime('now'))")
    batch = split.run_ingest_batch([(f, tiny_image_bytes()) for f in FILES], settings,
                                   folder_display_id="E2002", tab_id=2)
    second = split.confirm_batch_split(batch, [{"display_id": "X1AAA", "sequence_indices": [0]},
                                               {"display_id": "X1AAB", "sequence_indices": [1]}],
                                       settings)
    with db.connect(settings.db_path) as conn:
        second_folder = db.find_folder(conn, "E2002", 2)["folder_id"]
    return client, settings, (first_folder, first), (second_folder, second)


def test_ac2_each_tab_holds_its_own_folder_called_e2002(two_tabs):
    _, _, (first_folder, _), (second_folder, _) = two_tabs
    assert first_folder != second_folder


def test_ac2_same_named_scans_in_the_two_tabs_are_stored_apart(two_tabs):
    _, settings, (_, first), (_, second) = two_tabs
    assert set(page_keys(settings, first[0])).isdisjoint(page_keys(settings, second[0]))


def test_ac2_a_comment_in_one_tab_is_not_on_the_same_named_document_in_the_other(two_tabs):
    client, _, (_, first), (_, second) = two_tabs
    client.post(f"/api/documents/{first[0]}/comments", json={"body": "page two is torn"})

    assert client.get(f"/api/documents/{second[0]}/comments").json()["comments"] == []


def test_ac2_deleting_one_tabs_folder_leaves_the_others_scans(two_tabs):
    client, settings, (first_folder, _), (_, second) = two_tabs
    client.delete("/api/new/folders", params={"folder_id": first_folder, "reason": "a test"})

    storage = BucketStorage(settings.bucket_uri)
    assert all(storage.exists(key) for document_id in second for key in page_keys(settings, document_id))


def test_ac2_exporting_one_tabs_folder_includes_none_of_the_other(two_tabs):
    _, settings, (first_folder, first), (_, second) = two_tabs
    drafted(settings, *first, *second)
    with db.connect(settings.db_path) as conn:
        exported = {r["document_id"] for r in
                    db.get_new_documents_for_export(conn, first_folder, include_unapproved=True)}
    assert exported == set(first)


# ---------- AC-3: display ids are unique in their scope ----------


def test_ac3_a_second_document_cannot_take_a_display_id_its_folder_already_uses(site):
    client, _ = site
    _, (_, second) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])

    assert client.post(f"/api/new/documents/{second}/rename",
                       json={"new_display_id": "E2002AAA"}).status_code == 400


def test_ac3_a_second_folder_cannot_take_a_display_id_its_tab_already_uses_in_any_case(site):
    client, _ = site
    upload(client, "E2002", ["E2002AAA", "E2002AAB"])

    response = client.post("/api/new/batches", data={"folder_display_id": "e2002"},
                           files=[("files", ("p.jpg", tiny_image_bytes(), "image/jpeg"))])
    assert response.status_code == 409


def test_ac3_the_database_itself_refuses_a_repeated_display_id_in_a_folder(site):
    client, settings = site
    folder_id, (first, _) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])

    with pytest.raises(db.IntegrityError), db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET display_id = 'E2002AAB' WHERE document_id = ?", (first,))


# ---------- AC-4: a rename touches the display id and nothing else ----------


def _everything_keyed_by(settings, document_id):
    with db.connect(settings.db_path) as conn:
        def count(table):
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE document_id = ?",
                                (document_id,)).fetchone()[0]
        return {table: count(table) for table in
                ("document_comments", "text_revisions", "document_reviews", "page_orientations",
                 "new_document_pages")} | {"keys": page_keys(settings, document_id)}


def test_ac4_a_rename_leaves_comments_revisions_verdicts_orientations_and_pages_in_place(site):
    client, settings = site
    _, (document_id, _) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    drafted(settings, document_id)
    with db.connect(settings.db_path) as conn:
        paul = conn.execute("SELECT id FROM users WHERE username = 'paul'").fetchone()[0]
        collab.add_comment(conn, document_id, "new", paul, "check the date")
        collab.record_revision(conn, document_id, "new", "reviewed_text", "a", "b", paul)
        db.set_document_review(conn, document_id, "new", "admin", "accepted", "Paul")
        db.set_page_orientation(conn, "new", document_id, 0, 90, False, "Paul")
    before = _everything_keyed_by(settings, document_id)

    client.post(f"/api/new/documents/{document_id}/rename", json={"new_display_id": "E2002ZZZ"})

    assert _everything_keyed_by(settings, document_id) == before


def test_ac4_a_renamed_documents_scan_is_still_served(site):
    client, _ = site
    _, (document_id, _) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    client.post(f"/api/new/documents/{document_id}/rename", json={"new_display_id": "E2002ZZZ"})

    assert client.get(f"/api/new/documents/{document_id}/pages/0/image").status_code == 200


# ---------- AC-5: renumbering moves no file ----------


def test_ac5_closing_a_gap_moves_no_scan(site):
    client, settings = site
    _, (first, second) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    kept = set(page_keys(settings, second))

    client.delete(f"/api/new/documents/{first}", params={"reason": "a test"})

    assert bucket_keys(settings, "documents") == kept


# ---------- AC-6: nothing parses a display id ----------


def test_ac6_no_code_derives_a_folder_from_a_display_id():
    sources = (ROOT / "src" / "incipit").rglob("*.py")
    assert [p.name for p in sources if "folder_id_of" in p.read_text(encoding="utf-8")] == []


# ---------- AC-7: bucket keys are built from internal ids ----------


def test_ac7_scan_and_record_keys_name_internal_ids_only(site):
    client, settings = site
    _, documents = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    for document_id in documents:
        client.post(f"/api/new/documents/{document_id}/rename", json={"new_display_id": f"N{document_id}"})

    keys = bucket_keys(settings, "documents") | bucket_keys(settings, "records")
    parts = [key.split("/") for key in keys]
    assert keys and all(INTERNAL_FOLDER.match(p[1]) and INTERNAL_DOCUMENT.match(p[2].removesuffix(".json"))
                        for p in parts)


# ---------- AC-8: what people read uses display ids ----------


@pytest.fixture
def accepted_folder(site):
    client, settings = site
    folder_id, documents = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    drafted(settings, *documents)
    for document_id in documents:
        client.post(f"/api/new/documents/{document_id}/review", json=ACCEPT)
    return client, settings, folder_id, documents


def test_ac8_the_export_csv_names_each_document_by_its_display_id(accepted_folder):
    _, settings, folder_id, _ = accepted_folder
    _, body, _ = export.export_new_csv(settings, folder_id=folder_id)

    rows = body.decode("utf-8-sig").splitlines()[1:]
    assert [row.split(",")[0].strip('"') for row in rows] == ["E2002AAA", "E2002AAB"]


def test_ac8_the_record_carries_the_display_id(accepted_folder):
    _, settings, folder_id, documents = accepted_folder

    record = json.loads(BucketStorage(settings.bucket_uri).read_record(folder_id, documents[0]))
    assert record["display_id"] == "E2002AAA"


def test_ac8_the_exports_document_directories_are_named_by_display_id(accepted_folder, tmp_path):
    _, settings, folder_id, _ = accepted_folder
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    job_id = jobs.start("new", folder_id, "Paul")
    deadline = time.monotonic() + 10
    while jobs.status(job_id)["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.02)

    published = tmp_path / "exports" / "documents" / folder_id / "documents"
    assert sorted(p.name for p in published.iterdir()) == ["E2002AAA", "E2002AAB"]


# ---------- AC-9: the three display-id schemes ----------


def test_ac9_the_letters_scheme_counts_aaa_aab():
    assert docid.next_document_ids("E2002", set(), 2, "letters") == ["E2002AAA", "E2002AAB"]


def test_ac9_the_numbers_scheme_counts_001_002():
    assert docid.next_document_ids("E2002", set(), 2, "numbers") == ["E2002-001", "E2002-002"]


def test_ac9_the_filename_scheme_uses_each_documents_first_scan():
    assert docid.next_document_ids("E2002", set(), 2, "filename",
                                   filenames=["Letter to Hayes.jpg", "Reply.tif"]) == \
        ["Letter-to-Hayes", "Reply"]


def test_ac9_renumbering_closes_a_gap_in_the_numbers_scheme():
    assert docid.renumber_plan("E2002", ["E2002-002"], scheme="numbers") == {"E2002-002": "E2002-001"}


def test_ac9_renumbering_never_moves_a_filename_scheme_id():
    assert docid.renumber_plan("E2002", ["Reply", "Letter"], scheme="filename") == {}


# ---------- AC-10: the browser receives and shows display ids ----------


def test_ac10_every_queue_item_carries_both_display_ids(site):
    client, settings = site
    drafted(settings, *upload(client, "E2002", ["E2002AAA", "E2002AAB"])[1])

    items = client.get("/api/new/documents").json()
    assert items and all(i["display_id"] and i["folder_display_id"] == "E2002" for i in items)


def test_ac10_the_document_payload_carries_both_display_ids(site):
    client, settings = site
    _, (document_id, _) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    drafted(settings, document_id)

    detail = client.get(f"/api/new/documents/{document_id}").json()
    assert (detail["display_id"], detail["folder_display_id"]) == ("E2002AAA", "E2002")


def test_ac10_the_folder_payload_carries_the_folder_display_id(site):
    client, _ = site
    upload(client, "E2002", ["E2002AAA", "E2002AAB"])

    assert [f["folder_display_id"] for f in client.get("/api/new/folders").json()] == ["E2002"]


def test_ac10_the_page_shows_a_documents_display_id_not_its_internal_id():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("  function shownId(")
    function = source[start:source.index("\n  }", start) + 4]
    harness = function + 'console.log(shownId({document_id: "d1", display_id: "E2002AAA"}));'
    shown = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert shown.stdout.strip() == "E2002AAA"


# ---------- AC-11: the module's own smoke ----------


def test_ac11_the_m1_smoke_script_compiles():
    compile((ROOT / "scripts" / "smoke_m1.py").read_text(encoding="utf-8"), "smoke_m1.py", "exec")


def test_ac10_the_comment_panel_asks_for_the_open_documents_internal_id():
    """Found checking M1 in the browser: comments were fetched by the header's display id, and 403'd."""
    workspace = (ROOT / "app" / "static" / "workspace.js").read_text(encoding="utf-8")
    assert '$("doc-id")?.textContent' not in workspace and '$("doc-id")?.dataset.documentId' in workspace


def test_a_new_document_id_is_never_one_already_in_use(site, monkeypatch):
    """Scans are copied under the id before its row exists, so a repeat would overwrite them."""
    client, settings = site
    _, (taken, _) = upload(client, "E2002", ["E2002AAA", "E2002AAB"])
    draws = iter([taken[1:], "0" * 16])
    monkeypatch.setattr(db.secrets, "token_hex", lambda n: next(draws))

    with db.connect(settings.db_path) as conn:
        assert db.new_document_id(conn) == "d" + "0" * 16
