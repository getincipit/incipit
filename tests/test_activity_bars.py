"""One status bar per running job, and a clock that is actually running."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import insert_rows, seed_batch

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def run(call, jobs, received_ms_ago=0, functions=("spellElapsed", "spellStep", "liveElapsed",
                                                  "renderActivityBars"), real_escaping=False):
    """Runs the activity renderers out of app.js against a made-up set of jobs."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = []
    for name in functions:
        start = source.index(f"  function {name}(")
        blocks.append(source[start:source.index("\n  }", start) + 4])
    if real_escaping:
        start = source.index("  function escapeHtml(")
        blocks.insert(0, source[start:source.index("\n  }", start) + 4])
    harness = (
        ("" if real_escaping else "const escapeHtml = (s) => String(s == null ? '' : s);")
        + "const elements = { 'activity-bars': { innerHTML: '' },"
        "                   'pipeline-status': { classList: { add: () => { hidden = true; },"
        "                                                      toggle: (c, on) => { hidden = on; } } },"
        "                   'settings-panel-running': { classList: { contains: () => true } } };"
        "let hidden = null;"
        "const el = (id) => elements[id];"
        f"const activeJobs = {json.dumps(jobs)};"
        f"const activeJobsReceivedAt = Date.now() - {received_ms_ago};"
        + "".join(blocks)
        + f"console.log(JSON.stringify({call}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def job(**overrides):
    base = {"id": "a1", "kind": "drafting", "label": "Transcribing new documents", "detail": "",
            "folder": "E2003_Articles_jpegs", "remaining": 7, "elapsed_seconds": 12.0}
    base.update(overrides)
    return base


# ---------- the clock is live ----------


def test_elapsed_counts_on_from_when_the_poll_arrived():
    """The frozen timestamp: elapsed_seconds was rendered exactly as the server sent it."""
    assert run("liveElapsed(activeJobs[0])", [job(elapsed_seconds=12)], received_ms_ago=5000) == 17


def test_a_fresh_poll_reads_as_the_server_sent_it():
    assert run("liveElapsed(activeJobs[0])", [job(elapsed_seconds=12)]) == 12


def test_a_job_the_server_says_just_started_is_not_negative():
    assert run("liveElapsed(activeJobs[0])", [job(elapsed_seconds=0)]) == 0


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (45, "45s"), (60, "1m 0s"), (134, "2m 14s"), (3600, "1h 0m"), (3725, "1h 2m"),
])
def test_how_elapsed_time_is_spelled(seconds, expected):
    assert run(f"spellElapsed({seconds})", []) == expected


# ---------- one bar per job ----------


def test_two_jobs_get_two_bars():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(kind="uploading", folder="E2006_Aviation", remaining=40),
                  job(id="b2", kind="drafting", folder="E2003_Articles_jpegs", remaining=7)])

    assert markup.count('class="activity-bar"') == 2


def test_each_bar_names_its_own_folder():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(kind="uploading", folder="E2006_Aviation"),
                  job(id="b2", kind="drafting", folder="E2003_Articles_jpegs")])

    assert "E2006_Aviation" in markup and "E2003_Articles_jpegs" in markup


def test_each_bar_names_the_step_it_is_on():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(kind="uploading"), job(id="b2", kind="drafting")])

    assert "Uploading" in markup and "Drafting" in markup


def test_each_bar_says_what_is_left_of_its_own_work():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(kind="uploading", remaining=40), job(id="b2", remaining=7)])

    assert "40 documents remaining" in markup
    assert "7 documents remaining" in markup


def test_one_document_left_is_not_pluralised():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)", [job(remaining=1)])

    assert "1 document remaining" in markup


def test_a_job_that_cannot_count_its_work_says_nothing_about_it():
    """Better silent than wrong: not every step knows how many documents it has left."""
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)", [job(remaining=None)])

    assert "remaining" not in markup


def test_each_bar_carries_its_own_elapsed_time():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(elapsed_seconds=134), job(id="b2", elapsed_seconds=12)])

    assert "2m 14s elapsed" in markup and "12s elapsed" in markup


def test_nothing_running_draws_no_bars():
    assert run("(renderActivityBars(), el('activity-bars').innerHTML)", []) == ""


def test_the_single_status_line_steps_aside_while_the_bars_are_up():
    """Otherwise the same work is reported twice, once well and once badly."""
    assert run("(renderActivityBars(), hidden)", [job()]) is True


def test_an_idle_screen_is_not_given_an_empty_status_strip():
    """Hiding it while the bars are up is right; revealing it when they are not put an empty
    22-pixel strip on the queue screen, which is what a stale component looks like."""
    assert run("(renderActivityBars(), hidden)", []) is None


def test_a_folder_name_cannot_smuggle_markup_into_the_bar():
    """A folder is named by whoever uploaded it, and the bar is built by string concatenation."""
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(folder="<img onerror=alert(1)>")], real_escaping=True)

    assert "<img" not in markup
    assert "&lt;img onerror=alert(1)&gt;" in markup


def test_a_detail_from_the_server_is_escaped_too():
    markup = run("(renderActivityBars(), el('activity-bars').innerHTML)",
                 [job(detail="<script>x</script>")], real_escaping=True)

    assert "<script>" not in markup


# ---------- what the server puts in each bar ----------


def test_a_job_carries_the_folder_it_is_working_on():
    from incipit import activity

    with activity.track("drafting", "Transcribing", folder="E2003_Articles_jpegs", remaining=7):
        running = activity.snapshot()

    assert running[0]["folder"] == "E2003_Articles_jpegs"
    assert running[0]["remaining"] == 7


def test_two_jobs_are_reported_separately():
    from incipit import activity

    with activity.track("uploading", "Storing scans", folder="E2006_Aviation", remaining=40):
        with activity.track("drafting", "Transcribing", folder="E2003_Articles", remaining=7):
            running = activity.snapshot()

    assert [(j["kind"], j["folder"], j["remaining"]) for j in running] == [
        ("uploading", "E2006_Aviation", 40), ("drafting", "E2003_Articles", 7)]


def test_what_is_left_counts_down_while_the_job_runs():
    from incipit import activity

    with activity.track("drafting", "Transcribing", folder="E2003", remaining=7) as job:
        activity.set_remaining(job, 3)
        running = activity.snapshot()

    assert running[0]["remaining"] == 3


def test_a_job_that_names_no_folder_still_reports():
    from incipit import activity

    with activity.track("discovering", "Searching"):
        running = activity.snapshot()

    assert running[0]["folder"] == ""
    assert running[0]["remaining"] is None


def test_the_drafting_runner_reports_what_is_left(tmp_path):
    """What keeps a bar's count honest: the runner says so, one document at a time."""
    from incipit import db, new_document
    from incipit.config import Settings

    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
                        db_path=str(tmp_path / "extraction.db"))
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        seed_batch(conn, "B", 3, folder="E2003", name="E2003_Articles")
        for document_id in ("E2003AAA", "E2003AAB", "E2003AAC"):
            insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status) "
                         "VALUES (?, 'B', 'split_confirmed')", (document_id,))

    seen = []
    new_document.run_transcribe_new_documents(
        settings,
        new_document.NewDocumentOptions(max_total_cost=0.0),   # stops before spending anything
        progress=lambda remaining, document_id: seen.append(remaining),
    )

    assert seen[0] == 3, "the first report must be everything still to do"
