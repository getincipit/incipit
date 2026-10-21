"""What drafting a folder cost and how long it took."""

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


def _new(db_path, drafts, split_cost=0.0):
    with db.connect(db_path) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, "
                     "split_cost_estimate) VALUES ('B', 'now', 1, 'E2002', ?)", (split_cost,))
        for index, (cost, seconds, pages) in enumerate(drafts):
            doc = f"E2002AA{chr(65 + index)}"
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, review_status, text, "
                "cost_estimate, duration_seconds, model, prompt_version_id, run_date) "
                "VALUES (?, 'B', 'drafted', 'unreviewed', 't', ?, ?, 'qwen/qwen3.7-plus', 1, "
                "'2026-09-01T10:00:00')", (doc, cost, seconds))
            for page in range(pages):
                conn.execute("INSERT INTO new_document_pages (document_id, sequence_index, "
                             "bucket_key) VALUES (?, ?, 'k')", (doc, page))


def analytics(client, folder_id, workflow):
    return client.get(f"/api/analytics/folder?folder_id={folder_id}&workflow={workflow}")


# ---------- the numbers ----------


def test_a_new_folder_reports_what_it_cost(site):
    app, db_path = site
    _new(db_path, [(0.0009, 10.0, 1), (0.0011, 10.0, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["cost"] == pytest.approx(0.0020)


def test_the_time_taken_is_the_sum_of_what_was_measured(site):
    app, db_path = site
    _new(db_path, [(0.001, 9.0, 1), (0.001, 11.0, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["seconds"] == pytest.approx(20.0)
    assert body["seconds_per_document"] == pytest.approx(10.0)


def test_the_rate_is_documents_per_minute(site):
    """Two documents in twenty seconds is six a minute -- the number a reviewer sanity-checks
    against how long a folder felt."""
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1), (0.001, 10.0, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["documents_per_minute"] == pytest.approx(6.0)


def test_seconds_per_page_counts_pages_not_documents(site):
    """A folder of long documents and one of short ones draft at very different rates per document
    and similar rates per page, which is what makes this the comparable figure."""
    app, db_path = site
    _new(db_path, [(0.001, 20.0, 4), (0.001, 20.0, 4)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["seconds_per_page"] == pytest.approx(5.0)


def test_a_document_drafted_before_timing_existed_is_excluded_from_the_rates(site):
    """Counting an unmeasured document as instant would report a speed the pipeline has never
    reached, and the rate is the one number here nobody could check by eye."""
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1), (0.001, None, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["seconds"] == pytest.approx(10.0)
    assert body["seconds_per_document"] == pytest.approx(10.0), "the untimed row skewed the average"
    assert body["measured_count"] == 1
    assert body["unmeasured_count"] == 1


def test_an_untimed_document_still_counts_towards_the_cost(site):
    """It really was drafted and really was billed -- only its duration is unknown."""
    app, db_path = site
    _new(db_path, [(0.002, None, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["cost"] == pytest.approx(0.002)
    assert body["seconds"] == 0


def test_the_split_proposal_is_reported_beside_the_drafting_cost_not_inside_it(site):
    """Proposing a split is a real Qwen call billed against the batch, not against any document."""
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1)], split_cost=0.05)
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["cost"] == pytest.approx(0.001)
    assert body["split_cost"] == pytest.approx(0.05)


def test_the_provenance_of_the_numbers_is_reported(site):
    """Which model and prompt produced them -- a folder drafted last month under a different model
    is not comparable with one drafted today, and the report should say so itself."""
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1)])
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["models"] == ["qwen/qwen3.7-plus"]
    assert body["prompt_versions"] == ["General correspondence v1"]
    assert body["first_run"] and body["last_run"]


def test_documents_in_another_folder_are_not_counted(site):
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1)])
    with db.connect(db_path) as conn:
        insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                          "VALUES ('B9', 'now', 1, 'E9999')")
        insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, cost_estimate, "
                     "duration_seconds) VALUES ('E9999AAA', 'B9', 'drafted', 5.0, 999.0)")
    body = analytics(signed_in(app), "E2002", "new").json()
    assert body["cost"] == pytest.approx(0.001)
    assert body["seconds"] == pytest.approx(10.0)


# ---------- who may ask ----------


def test_a_reviewer_cannot_read_a_folder_s_spend(site):
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1)])
    assert analytics(signed_in(app, "john"), "E2002", "new").status_code in (401, 403)


def test_a_reviewer_is_not_shown_the_link_at_all(site):
    """An admin-only endpoint behind a control everyone can see is a control that can only fail."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = source.split("function renderCardMenu", 1)[1].split("\n  }", 1)[0]
    assert 'document.body.dataset.role !== "admin"' in block
    assert "Drafting analytics" in block


def test_an_unknown_workflow_is_refused(site):
    app, db_path = site
    _new(db_path, [(0.001, 10.0, 1)])
    assert analytics(signed_in(app), "E2002", "sideways").status_code == 400


def test_a_folder_with_no_documents_is_a_404(site):
    app, _ = site
    assert analytics(signed_in(app), "NOPE", "new").status_code == 404


# ---------- the popup ----------


def test_the_card_offers_a_way_in_rather_than_a_bare_number():
    """The change asked for: cost alone raised questions it could not answer."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "renderCardMenu(f)" in source
    assert "Drafting analytics" in source
    assert "renderDraftCost" not in source, "the old cost badge is still on the card"


def test_the_way_in_does_not_look_like_the_open_draft_control():
    """Asked for explicitly: it must not read as a primary action."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = css.split(".card-menu-list button {", 1)[1].split("}", 1)[0]
    assert "background: none" in block
    assert "border: 0" in block
    assert ".card-menu-list button:hover" in css


def test_clicking_the_link_does_not_also_open_the_folder():
    """The link sits on a card whose own click handler drills into the folder."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '[data-claim], [data-release], [data-analytics]' in source
    handler = source.split("[data-analytics]", 2)[2]
    assert "e.stopPropagation()" in handler


def test_the_report_is_a_popup_and_not_a_screen():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="analytics-modal"' in markup
    assert "modal-overlay" in markup.split('id="analytics-modal"', 1)[1][:120]
    assert 'aria-modal="true"' in markup.split('id="analytics-modal"', 1)[1][:200]


@pytest.mark.parametrize("way_out", ["analytics-close", "Escape", "scrim"])
def test_it_can_be_left_to_return_to_the_folder_view(way_out):
    """Asked for explicitly."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    if way_out == "analytics-close":
        assert 'el("analytics-close").addEventListener("click", closeAnalytics)' in source
    elif way_out == "Escape":
        assert 'e.key === "Escape"' in source and "closeAnalytics()" in source
    else:
        assert "if (e.target === analyticsModal) closeAnalytics();" in source


def test_a_drag_that_ends_outside_the_dialog_does_not_close_it():
    """Selecting a number and releasing on the scrim is how someone copies one of these figures."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'analyticsModal.addEventListener("mousedown"' in source, \
        "closing on click would fire at the end of a text selection"


def test_sub_cent_spend_is_not_rounded_away_in_the_popup():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    money = source.split("function money(", 1)[1].split("\n  }", 1)[0]
    assert "toFixed(4)" in money


def test_a_long_run_is_shown_in_minutes_rather_than_hundreds_of_seconds():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = source.split("function duration(", 1)[1].split("\n  }", 1)[0]
    assert "3600" in block and "60" in block


# ---------- the duration is really measured, not merely a column ----------


