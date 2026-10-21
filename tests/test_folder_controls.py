"""Folder-level controls: deleting, exporting, and claiming."""
import pathlib

import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from conftest import signed_in

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
ARCHIVAL_FOLDER = "[D7519-F] Document File Series -- 1875"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export" / "out.csv"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "mary", "mary-password", "Mary", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, db_path


# ---------- "I should be able to delete any folders from Existing documents as well" ----------


# ---------- "export tag on the folder should change in real time" ----------


def test_the_page_redraws_the_folders_after_an_export():
    """Server state is only half of it: without the redraw the card still says "Not exported" on a
    folder the reviewer just watched download."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'window.addEventListener("folder-export-completed", () => loadQueue())' in source


def test_the_export_badge_keeps_its_place_beside_the_status():
    """It sat hard right on a folder with nothing to draft and slid to the middle as soon as "Open
    to draft" appeared, so the same badge moved depending on the folder's state."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    foot = css.split(".folder-card-foot {", 1)[1].split("}", 1)[0]
    assert "justify-content: flex-start" in foot
    assert ".folder-card-cta { margin-left: auto; }" in css


# ---------- "admins should be able to claim folders ... make claiming optional" ----------


def test_a_claim_needs_a_folder_and_a_real_workflow(site):
    paul = signed_in(site[0])
    assert paul.post("/api/team/claim", json={"workflow": "new"}).status_code == 400
    assert paul.post("/api/team/claim", json={
        "folder_id": "X", "workflow": "sideways"}).status_code == 400


def test_the_page_can_tell_whose_claim_is_whose(site):
    """"You have this" versus "Mary has this" needs the viewer's own id, which /api/me did not
    carry."""
    app, _ = site
    assert "id" in signed_in(app).get("/api/me").json()


# ---------- "the reviewer still has to scroll to see Save Draft / Flag / Accept" ----------


def test_the_shell_gives_back_the_height_the_identity_bar_takes():
    """The bar is a sibling of the shell, not part of it."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "calc(100vh - var(--who-bar-h" in css
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    assert "--who-bar-h" in source, "nothing measures the bar, so the variable is never set"


def test_the_main_column_does_not_re_derive_the_viewport_height():
    """A second 100vh here made .main taller than its own parent by exactly the identity strip, and
    that overflow -- not the metadata scroll -- is what hid the action bar."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    main = css.split(".main { flex: 1;", 1)[1].split("}", 1)[0]
    assert "100vh" not in main
    assert "min-height: 0" in main


