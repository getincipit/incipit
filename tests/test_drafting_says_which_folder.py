"""Drafting a folder says which folder, in the activity bar and on the status strip."""

import shutil
import subprocess
from pathlib import Path

import pytest

from incipit import activity

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_folder_draft_registers_a_job_carrying_the_folder(tmp_path: Path, monkeypatch):
    from tests.test_review_server import _seed_pending_folder

    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 2)
    seen = []
    real = activity.exclusive

    def watched(kind, label, *args, **kwargs):
        seen.append({"kind": kind, "label": label, "folder": kwargs.get("folder", "")})
        return real(kind, label, *args, **kwargs)

    monkeypatch.setattr("incipit.review_server.activity.exclusive", watched)
    assert client.post("/api/new/folders/draft?folder_id=E2231").status_code == 200

    drafting = [job for job in seen if job["kind"] == "drafting"]
    assert drafting, "drafting a folder registered no job, so the activity bar showed nothing"
    assert drafting[0]["folder"] == "E2231"
    assert "E2231" in drafting[0]["label"]


def test_a_second_draft_while_one_runs_is_refused(tmp_path: Path, monkeypatch):
    """Two drafts at once spend twice the budget on the same documents."""
    from tests.test_review_server import _seed_pending_folder

    client, _ = _seed_pending_folder(tmp_path, monkeypatch, "E2231", 2)
    with activity.exclusive("drafting", "something else is drafting"):
        response = client.post("/api/new/folders/draft?folder_id=E2231")

    assert response.status_code == 409


def test_the_status_strip_names_the_folder_it_is_drafting():
    """The once-a-second ticker said only "Drafting...", overwriting the line that named it."""
    start = JS.index("async function runFolderDraft(")
    block = JS[start:JS.index("\n  }", start)]

    assert "`Drafting ${label}`" in block
    assert '"Drafting",' not in block


def test_the_ticker_keeps_whatever_the_caller_last_said():
    start = JS.index("function startProgressIndicator(")
    block = JS[start:JS.index("\n  }\n", start)]

    assert "stop.setLabel" in block
    assert "${said}..." in block


def test_the_strip_counts_down_what_the_reviewer_asked_for():
    """It said "Drafting 5 documents" while drafting 49, because 5 is one request's cap."""
    start = JS.index("async function runFolderDraft(")
    block = JS[start:JS.index("\n  }", start)]

    assert "${target - drafted} document" in block
    assert "remaining" in block
    assert 'params.set("total", String(target))' in block
    assert 'params.set("done", String(drafted))' in block


def test_the_folders_own_pending_total_is_not_polled_over_the_countdown():
    """Drafting 5 of 49 polled 49 and put "49 still pending" beside "5 remaining"."""
    start = JS.index("async function runFolderDraft(")
    block = JS[start:JS.index("\n  }", start)]

    assert "pending-count?folder_id" not in block


def test_the_label_survives_a_round_trip_through_the_real_function():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = JS.index("  function startProgressIndicator(")
    block = JS[start:JS.index("\n  }\n", start) + 4]
    harness = (
        "const PROGRESS_GRACE_MS = 0; const PROGRESS_MIN_VISIBLE_MS = 0;"
        "const activeJobs = []; let last = '';"
        + block
        + "const stop = startProgressIndicator((t) => { last = t; }, 'Drafting E2231');"
        "setTimeout(() => { stop.setLabel('Drafting E2231: 3 of 9 done');"
        "  console.log(last); stop(); }, 30);"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Drafting E2231: 3 of 9 done...")
