"""Drafting more than one folder at a time, without letting the spend cap slip."""
import concurrent.futures
import contextlib
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, activity, db, review_server
from incipit.config import Settings
from conftest import insert_rows

APP_JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
    encoding="utf-8")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
    activity.clear()
    client = TestClient(application)
    client.cookies.clear()
    client.post("/login", data={"username": "paul", "password": "paul-password"},
                follow_redirects=False)
    return client, db_path


def pending_folder(db_path, folder_id, count=2):
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES (?, 'now', 1, ?)", (f"B-{folder_id}", folder_id))
        for index in range(count):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status) "
                "VALUES (?, ?, 'split_confirmed')",
                (f"{folder_id}AA{chr(ord('A') + index)}", f"B-{folder_id}"))


def held_runner(db_path, entered, release, seen=None):
    """Stands in for the runner: says it started, holds until let go, then marks its own done."""
    def run(settings_arg, options_arg=None, document_ids=None, progress=None, **_):
        if seen is not None:
            seen.append(list(document_ids or []))
        entered.set()
        release.wait(timeout=5)
        with db.connect(db_path) as conn:
            for document_id in document_ids or []:
                conn.execute("UPDATE new_documents SET status = 'drafted' WHERE document_id = ?",
                             (document_id,))
    return run


def race(client, db_path, monkeypatch, first, second, seen=None):
    """Sends the second request while the first is provably still inside the runner."""
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr("incipit.review_server.run_transcribe_new_documents",
                        held_runner(db_path, entered, release, seen))

    def draft(folder_id):
        return client.post("/api/new/folders/draft", params={"folder_id": folder_id})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(draft, first)
        assert entered.wait(timeout=5), "the first request never reached the runner"
        two = pool.submit(draft, second)
        # It is either refused straight away or parked in the runner beside the first.
        concurrent.futures.wait([two], timeout=1.0)
        release.set()
        return one.result(), two.result()


def test_two_folders_can_be_drafted_at_the_same_time(site, monkeypatch):
    """Reported by hand: the second folder was refused with "drafting is already running", because
    the drafting lock was one lock for the whole site rather than one per folder."""
    client, db_path = site
    pending_folder(db_path, "E2231")
    pending_folder(db_path, "E2232")

    one, two = race(client, db_path, monkeypatch, "E2231", "E2232")

    assert [one.status_code, two.status_code] == [200, 200]


def test_each_folder_drafts_only_its_own_documents(site, monkeypatch):
    client, db_path = site
    pending_folder(db_path, "E2231")
    pending_folder(db_path, "E2232")
    seen = []

    race(client, db_path, monkeypatch, "E2231", "E2232", seen=seen)

    assert sorted(sorted(batch) for batch in seen) == [
        ["E2231AAA", "E2231AAB"], ["E2232AAA", "E2232AAB"]]


def test_the_same_folder_twice_over_is_still_refused(site, monkeypatch):
    """One folder drafting itself twice would draft each of its documents twice."""
    client, db_path = site
    pending_folder(db_path, "E2231")

    one, two = race(client, db_path, monkeypatch, "E2231", "E2231")

    assert [one.status_code, two.status_code] == [200, 409]


def test_the_refusal_names_the_folder_that_is_holding_it(site, monkeypatch):
    """"drafting is already running" says nothing a reviewer can act on when it is their own
    second folder that is being refused."""
    client, db_path = site
    pending_folder(db_path, "E2231")

    _, two = race(client, db_path, monkeypatch, "E2231", "E2231")

    assert "E2231" in two.json()["detail"]


def test_the_browser_tracks_drafting_per_folder_instead_of_disabling_every_folder():
    """Backend concurrency is not usable if the one set of controls stays globally disabled while
    the first request runs. Navigating to a different folder must expose enabled controls there."""
    assert "folderDraftRuns: new Set()" in APP_JS
    assert "state.folderDraftRuns.has(folderDraftKey(state.activeFolder))" in APP_JS
    start = APP_JS.index("async function runFolderDraft(")
    body = APP_JS[start:APP_JS.index("folderDraftCountBtn.addEventListener", start)]
    assert "state.folderDraftRuns.add(runKey)" in body
    assert "state.folderDraftRuns.delete(runKey)" in body


def test_fix_split_does_not_turn_the_comment_button_into_a_selection_button():
    """Fix Split makes the whole row a picker. The comment control must not remain visible without
    its own click handler, because clicking it would silently select the document instead."""
    assert '${state.resplitMode ? "" : commentChip(doc)}' in APP_JS


# ---------- the cap the single lock used to protect ----------


def test_two_runs_together_cannot_spend_past_the_cap():
    """The reason drafting was one lock for the whole site: both runs read the same total, both
    concluded there was room, and together they overshot. The reservation is what replaces it."""
    from incipit import spend
    spend.clear()
    cap = 1.00
    # Under the cap, so one run may start; not by a whole reserve, so the next may not. Every run
    # reads this same total, which is the situation the reservation exists for.
    spent = lambda: cap - spend.PER_DOCUMENT_RESERVE / 2

    def ask(stack):
        return stack.enter_context(spend.room_for_one(spent, cap))

    with contextlib.ExitStack() as stack:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            granted = list(pool.map(lambda _: ask(stack), range(4)))

    assert granted.count(True) == 1


def test_a_reservation_is_given_back_once_the_document_is_recorded(tmp_path):
    from incipit import spend
    spend.clear()

    with spend.room_for_one(lambda: 0.0, 1.00) as granted:
        assert granted
        assert spend.reserved() == pytest.approx(spend.PER_DOCUMENT_RESERVE)

    assert spend.reserved() == 0.0


def test_the_reserve_is_larger_than_any_document_has_cost():
    """It is headroom for a cost that is not known until the document has been drafted, so it has
    to sit above what one actually costs -- the largest of 154 real drafts was $0.011."""
    from incipit import spend

    assert spend.PER_DOCUMENT_RESERVE > 0.011


# ---------- and what the grey bar says while it runs ----------
