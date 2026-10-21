"""Two things a reviewer sees: the way into what a folder cost, and the way to load a pre-indexed
CSV."""

import pathlib

import pytest

from incipit import accounts, collab, db, review_server
from incipit.config import Settings
from conftest import insert_rows, signed_in

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
ARCHIVAL_FOLDER = "[D7519-F] Document File Series -- 1875"


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    app = review_server.create_app(settings, str(tmp_path / "export" / "o.csv"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, db_path


# ---------- what the folder cost to draft ----------


def _new(db_path, costs):
    with db.connect(db_path) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                     "VALUES ('B', 'now', 1, 'E2002')")
        for index, cost in enumerate(costs):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
                "cost_estimate) VALUES (?, 'B', 'drafted', 'unreviewed', 't', ?)",
                (f"E2002A{chr(65 + index)}", cost))


def test_a_new_folder_reports_what_it_cost(site):
    """Same field name in both workflows, because one folder card renders both."""
    app, db_path = site
    _new(db_path, [0.0009, 0.0011])
    folder = signed_in(app).get("/api/new/folders").json()[0]
    assert folder["cost_estimate"] == pytest.approx(0.0020)


def test_a_folder_costing_under_a_cent_is_not_rounded_to_nothing():
    """Drafting one short document really does cost less than a cent, and $0.00 would report real
    spend as none."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    money = source.split("function money(", 1)[1].split("\n  }", 1)[0]
    assert "toFixed(4)" in money, "sub-cent spend would be shown as $0.00"


def test_the_way_into_the_numbers_sits_on_the_card_beside_the_export_badge():
    """Still on the card itself, in the same footer as the export state -- one line further in,
    behind More, rather than on a screen of its own."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    foot = source.split("folder-card-foot", 1)[1][:400]
    assert "renderCardMenu(f)" in foot and "renderExportState(f)" in foot


# ---------- the pre-indexed CSV control ----------


