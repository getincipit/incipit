"""A reviewer can turn or mirror a page in the viewer, save it, and have the export carry it (asked for 15 October)."""
import io
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from incipit import accounts, collab, db, folder_exports, imaging, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows

REPO = Path(__file__).resolve().parents[1]
RED, WHITE = (255, 0, 0), (255, 255, 255)


def marked_scan() -> bytes:
    """A wide page with a red block in its top-left corner, so every orientation is told apart."""
    img = Image.new("RGB", (60, 30), WHITE)
    img.paste(RED, (0, 0, 15, 10))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=100)
    return buffer.getvalue()


def corner_colour(content: bytes, corner: str):
    with Image.open(io.BytesIO(content)) as img:
        x = 2 if "left" in corner else img.width - 3
        y = 2 if "top" in corner else img.height - 3
        return img.convert("RGB").getpixel((x, y))


def is_red(pixel) -> bool:
    return pixel[0] > 200 and pixel[1] < 80 and pixel[2] < 80


@pytest.fixture(params=["new"])
def site(tmp_path, request, monkeypatch):
    workflow = request.param
    document = "E2002AAA"
    settings = Settings(db_path=str(tmp_path / "extraction.db"), bucket_uri=(tmp_path / "bucket").as_uri())
    app = review_server.create_app(settings)
    with db.connect(settings.db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                          "VALUES ('B', 'now', 1, 'E2002')")
        insert_rows(conn,"INSERT INTO new_documents (document_id,batch_id,status,text,reviewed_text,review_status) "
                     "VALUES (?, 'B', 'drafted','original','latest correction','accepted')", (document,))
        conn.execute("INSERT INTO new_document_pages (document_id,sequence_index,bucket_key) VALUES (?,0,'scan.jpg')",
                     (document,))
    BucketStorage(settings.bucket_uri).write_bytes("scan.jpg", marked_scan())
    client = TestClient(app)
    client.post("/login", data={"username": "paul", "password": "paul-password"}, follow_redirects=False)
    base = "/api/new/documents"
    return client, settings, workflow, document, f"{base}/{document}"


def test_a_saved_orientation_comes_back_with_the_document(site):
    client, _, _, _, url = site
    client.put(f"{url}/pages/0/orientation", json={"rotation": 90, "mirrored": True})
    page = client.get(url).json()["pages"][0]
    assert (page["rotation"], page["mirrored"]) == (90, True)


def test_a_page_never_turned_comes_back_upright(site):
    client, _, _, _, url = site
    page = client.get(url).json()["pages"][0]
    assert (page["rotation"], page["mirrored"]) == (0, False)


def test_only_quarter_turns_are_accepted(site):
    client, _, _, _, url = site
    assert client.put(f"{url}/pages/0/orientation", json={"rotation": 45}).status_code == 400


def test_a_page_the_document_does_not_have_cannot_be_turned(site):
    client, _, _, _, url = site
    assert client.put(f"{url}/pages/7/orientation", json={"rotation": 90}).status_code == 404


def test_turning_a_page_back_upright_leaves_nothing_stored(site):
    client, settings, workflow, document, url = site
    client.put(f"{url}/pages/0/orientation", json={"rotation": 180})
    client.put(f"{url}/pages/0/orientation", json={"rotation": 0, "mirrored": False})
    with db.connect(settings.db_path) as conn:
        assert db.page_orientations(conn, workflow, document) == {}


def test_a_reviewer_cannot_turn_a_page_in_an_approved_folder(site):
    client, settings, workflow, _, url = site
    with db.connect(settings.db_path) as conn:
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
        reviewer = accounts.get_user_by_name(conn, "john")["id"]
        conn.execute("INSERT INTO folder_assignments (folder_id,workflow,assigned_to,status,assigned_at,updated_at) "
                     "VALUES ('E2002',?,?,'approved','now','now')", (workflow, reviewer))
    client.cookies.clear()
    client.post("/login", data={"username": "john", "password": "john-password"}, follow_redirects=False)
    assert client.put(f"{url}/pages/0/orientation", json={"rotation": 90}).status_code == 409


def test_the_exported_scan_is_turned_as_the_reviewer_saved_it(site, tmp_path):
    client, settings, workflow, _, url = site
    client.put(f"{url}/pages/0/orientation", json={"rotation": 90, "mirrored": False})
    with db.connect(settings.db_path) as conn:
        conn.execute("INSERT INTO folder_assignments (folder_id,workflow,status,assigned_at,updated_at) "
                     "VALUES ('E2002',?,'approved','now','now')", (workflow,))
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    job = jobs.start(workflow, "E2002", "Admin")
    deadline = time.monotonic() + 10
    while jobs.status(job)["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.01)
    with zipfile.ZipFile(next((tmp_path / "exports").rglob("*.zip"))) as bundle:
        image = bundle.read(next(n for n in bundle.namelist() if "/images/" in n))
    with Image.open(io.BytesIO(image)) as exported:
        assert exported.size == (30, 60)


def test_the_export_records_each_pages_orientation(site, tmp_path):
    client, settings, workflow, _, url = site
    client.put(f"{url}/pages/0/orientation", json={"rotation": 270, "mirrored": True})
    with db.connect(settings.db_path) as conn:
        page = folder_exports.records(conn, workflow, "E2002")[0]["pages"][0]
    assert (page["rotation"], page["mirrored"]) == (270, True)


def test_turning_a_page_changes_the_exports_version(site):
    """A folder exported before a page was turned must read as changed, or the old images stay published."""
    client, settings, workflow, _, url = site
    with db.connect(settings.db_path) as conn:
        before = folder_exports.fingerprint(conn, workflow, "E2002")
    client.put(f"{url}/pages/0/orientation", json={"rotation": 90})
    with db.connect(settings.db_path) as conn:
        assert folder_exports.fingerprint(conn, workflow, "E2002") != before


def test_the_bucket_keeps_the_scan_as_it_was_digitized(site, tmp_path):
    client, settings, workflow, _, url = site
    client.put(f"{url}/pages/0/orientation", json={"rotation": 180})
    assert BucketStorage(settings.bucket_uri).read_bytes("scan.jpg") == marked_scan()


@pytest.mark.parametrize("rotation, mirrored, red_corner", [
    (0, True, "top-right"),
    (90, False, "top-right"),
    (180, False, "bottom-right"),
    (270, False, "bottom-left"),
    (90, True, "bottom-right"),
])
def test_an_exported_scan_is_mirrored_first_then_turned_clockwise(rotation, mirrored, red_corner):
    """The same order the viewer's CSS applies, so the export looks like the screen."""
    assert is_red(corner_colour(imaging.orient(marked_scan(), rotation, mirrored), red_corner))


def test_an_upright_scan_is_exported_byte_for_byte():
    assert imaging.orient(marked_scan(), 0, False) == marked_scan()


def test_a_renamed_document_keeps_its_turned_pages(tmp_path):
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    with db.connect(path) as conn:
        insert_rows(conn,"INSERT INTO new_documents (document_id,status) VALUES ('E2002AAA','drafted')")
        db.set_page_orientation(conn, "new", "E2002AAA", 0, 90, False, "Paul")
        db.rename_new_document(conn, "E2002AAA", "E2002AAB")
        assert db.page_orientations(conn, "new", "E2002AAA") == {0: {"rotation": 90, "mirrored": False}}


def test_a_deleted_document_takes_its_orientations_with_it(tmp_path):
    """Otherwise a later document given the same ID would open already turned."""
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    with db.connect(path) as conn:
        insert_rows(conn,"INSERT INTO new_documents (document_id,status) VALUES ('E2002AAA','drafted')")
        db.set_page_orientation(conn, "new", "E2002AAA", 0, 90, False, "Paul")
        db.delete_new_document(conn, "E2002AAA")
        assert db.page_orientations(conn, "new", "E2002AAA") == {}


def _turns(script: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = (REPO / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("  function turnOrientation(o, action)")
    turn = source[start:source.index("\n  }\n", start) + 4]
    start = source.index("  function orientationTransform(")
    transform = source[start:source.index("\n  }\n", start) + 4]
    return subprocess.run([node, "-e", turn + transform + script], capture_output=True, text=True)


def test_the_viewers_buttons_compose_into_one_of_the_eight_orientations():
    result = _turns("""
      const assert = require('node:assert/strict');
      const up = {rotation: 0, mirrored: false};
      const run = (...actions) => actions.reduce(turnOrientation, up);
      assert.deepEqual(run('right', 'right', 'right', 'right'), up);
      assert.deepEqual(run('left', 'right'), up);
      assert.deepEqual(run('flip-h', 'flip-h'), up);
      assert.deepEqual(run('flip-v', 'flip-v'), up);
      assert.deepEqual(run('flip-v'), run('flip-h', 'right', 'right'));
      assert.deepEqual(run('right', 'flip-h'), {rotation: 270, mirrored: true});""")
    assert result.returncode == 0, result.stderr


def test_a_sideways_page_is_fitted_into_the_viewer():
    """A portrait scan turned a quarter has its height across the viewer, so it is scaled to fit."""
    result = _turns("""
      const assert = require('node:assert/strict');
      const t = orientationTransform({rotation: 90, mirrored: false}, 400, 600, 500, 600);
      assert.match(t, /rotate\\(90deg\\) scale\\(0\\.8333\\d*, 0\\.8333\\d*\\)/);
      assert.equal(orientationTransform({rotation: 0, mirrored: false}, 400, 600, 500, 600), '');""")
    assert result.returncode == 0, result.stderr
