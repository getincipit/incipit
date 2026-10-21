"""Renaming a folder after its split has been confirmed."""

import json

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, db, docid, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows, seed_batch, signed_in


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "sam", "sam-password", "Sam", "reviewer")
        seed_batch(conn, "B", 1, folder="E2002", name="Advice_jpegs")
        conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = 'B'")
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
            "VALUES ('E2002AAA', 'B', 'drafted', 'unreviewed')")
        conn.execute(
            "INSERT INTO new_document_pages (document_id, sequence_index, bucket_key) "
            "VALUES ('E2002AAA', 0, 'documents/E2002/E2002AAA/page.jpg')")
    storage = BucketStorage(settings.bucket_uri)
    storage.write_bytes("documents/E2002/E2002AAA/page.jpg", b"a scan")
    storage.write_bytes("records/E2002/E2002AAA.json",
                        json.dumps({"document_id": "E2002AAA"}).encode())
    return application, settings


def rename(client, name, folder_id="E2002"):
    return client.post(f"/api/new/folders/{folder_id}/name", json={"folder_name": name})


def folder_payload(client, folder_id="E2002"):
    body = client.get("/api/new/folders").json()
    folders = body["folders"] if isinstance(body, dict) else body
    return next(f for f in folders if f["folder_id"] == folder_id)


def stored_name(settings, folder_id="E2002"):
    with db.connect(settings.db_path) as conn:
        return db.new_folder_names(conn, folder_id)[0]


# ---------- the database ----------


def test_a_rename_reaches_the_database(site):
    application, settings = site

    assert rename(signed_in(application), "Correspondence").status_code == 200

    assert stored_name(settings) == "Correspondence"


def test_the_new_title_follows_the_convention(site):
    application, settings = site

    result = rename(signed_in(application), "Correspondence").json()

    assert result["folder_title"] == "E2002_Correspondence"


def test_the_folder_list_shows_the_new_title(site):
    application, settings = site
    client = signed_in(application)

    rename(client, "Correspondence")

    folder = folder_payload(client)
    assert folder["folder_title"] == "E2002_Correspondence"


# ---------- and nothing in the bucket moves ----------


def test_a_rename_leaves_the_scans_where_they_are(site):
    application, settings = site

    rename(signed_in(application), "Correspondence")

    assert BucketStorage(settings.bucket_uri).exists("documents/E2002/E2002AAA/page.jpg")


def test_a_rename_leaves_the_page_row_alone(site):
    application, settings = site

    rename(signed_in(application), "Correspondence")

    with db.connect(settings.db_path) as conn:
        key = conn.execute("SELECT bucket_key FROM new_document_pages").fetchone()["bucket_key"]
    assert key == "documents/E2002/E2002AAA/page.jpg"


def test_a_rename_leaves_the_record_where_it_is(site):
    application, settings = site

    rename(signed_in(application), "Correspondence")

    assert BucketStorage(settings.bucket_uri).exists("records/E2002/E2002AAA.json")


# ---------- use default title ----------


def test_the_default_is_the_name_it_was_uploaded_under(site):
    application, settings = site
    client = signed_in(application)

    rename(client, "Something Else")
    result = rename(client, "").json()

    assert result["folder_name"] == "Advice_jpegs"
    assert result["folder_title"] == "E2002_Advice_jpegs"


def test_the_default_is_offered_before_it_is_used(site):
    """The button has to know what it would reset to, so the folder payload carries it."""
    application, settings = site
    client = signed_in(application)

    rename(client, "Something Else")

    folder = folder_payload(client)
    assert folder["default_folder_name"] == "Advice_jpegs"


# ---------- who may do it ----------


def test_a_reviewer_cannot_rename_a_folder(site):
    """The name is what every export is titled with, so it is the editor's to choose."""
    application, settings = site

    assert rename(signed_in(application, "sam"), "Whatever").status_code == 403
    assert stored_name(settings) == "Advice_jpegs"


def test_a_signed_out_visitor_cannot_rename_a_folder(site):
    application, settings = site
    client = TestClient(application)
    client.cookies.clear()

    assert client.post("/api/new/folders/E2002/name",
                       json={"folder_name": "Whatever"}).status_code in (401, 403)


def test_renaming_a_folder_that_does_not_exist_is_a_404(site):
    application, settings = site

    assert rename(signed_in(application), "Whatever", folder_id="E9999").status_code == 404


# ---------- the rule itself ----------


def test_a_name_that_already_carries_the_id_is_not_doubled(site):
    """Most folders are uploaded as "E2002_Advice" already."""
    application, settings = site

    result = rename(signed_in(application), "E2002_Advice").json()

    assert result["folder_title"] == "E2002_Advice"


def test_a_name_with_characters_a_directory_cannot_hold_is_made_safe(site):
    application, settings = site

    result = rename(signed_in(application), "bad/name:here").json()

    assert result["folder_title"] == "E2002_bad-name-here"
    assert docid.folder_directory("E2002", "bad/name:here") == "E2002_bad-name-here"


# ---------- the title the browser draws, and who is offered the editor ----------


def run_js(function_names, call, role="admin"):
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
        encoding="utf-8").replace("\r\n", "\n")
    blocks = []
    for name in function_names:
        start = source.index(f"  function {name}(")
        blocks.append(source[start:source.index("\n  }", start) + 4])
    harness = (
        f"const classes = new Set({_json.dumps(['role-' + role])});"
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + "".join(blocks) + f"console.log(JSON.stringify({call}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


@pytest.mark.parametrize("folder_id,name", [
    ("E2002", "E2002_Advice_jpegs"),
    ("E2006", "Aviation 1920"),
    ("E2241", ""),
    ("E2005", "bad/name:here"),
    ("E2002", "E2002"),
    ("", "Loose Papers"),
])
def test_the_hint_says_what_the_server_will_write(folder_id, name):
    """The hint under the input is only useful if it is the same rule -- so it is tested against
    docid.folder_directory itself rather than against a copy of the expected answer."""
    assert run_js(["folderTitleFor"], f"folderTitleFor({json.dumps(folder_id)}, "
                                      f"{json.dumps(name)})") == \
        docid.folder_directory(folder_id, name)


def test_a_reviewer_is_not_offered_the_rename_button():
    assert run_js(["canRenameFolder"], "canRenameFolder(true)", role="reviewer") is False


def test_an_editor_is_offered_it_in_the_new_workflow():
    assert run_js(["canRenameFolder"], "canRenameFolder(true)") is True


def test_nobody_is_offered_it_for_a_folder_that_lives_on_the_live_site():
    """Existing-document folders are named by the catalogue, not here."""
    assert run_js(["canRenameFolder"], "canRenameFolder(false)") is False


# ---------- a reset puts the name back ----------


def test_resetting_puts_the_database_back_too(site):
    """Reported: a restored default name has to reach the database, so an export matches."""
    application, settings = site
    client = signed_in(application)

    rename(client, "Something Else")
    rename(client, "")

    assert stored_name(settings) == "Advice_jpegs"
