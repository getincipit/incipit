"""What the screen does while a folder is uploaded and split."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def body_of(name):
    """The source of one function, async or not."""
    for opener in (f"\n  function {name}(", f"\n  async function {name}("):
        if opener in JS:
            start = JS.index(opener)
            return JS[start:JS.index("\n  }", start)]
    raise AssertionError(f"no function called {name}")


# ---------- the resume banner, while the upload it describes is still in hand ----------


def test_the_batch_being_set_up_is_not_offered_back_as_abandoned_work():
    assert "b.batch_id !== state.batchInFlight" in body_of("loadPendingBatches")


def test_the_upload_claims_the_batch_as_soon_as_it_exists():
    """It exists from the POST, which is before the pipeline dialog is even asked."""
    assert "state.batchInFlight = batch.batch_id;" in JS


def test_the_claim_is_released_however_the_flow_ends():
    assert "state.batchInFlight = null;" in body_of("runInitialSplitProposal")


def test_the_banner_stays_down_on_the_split_screen():
    """It would be offering to resume the very batch on screen."""
    assert '!splitReviewView.classList.contains("hidden")' in body_of("loadPendingBatches")


# ---------- work too fast to be worth a status strip ----------


def test_the_strip_waits_before_showing_itself():
    assert "const PROGRESS_GRACE_MS = 400;" in JS
    assert "}, PROGRESS_GRACE_MS);" in JS


def test_nothing_is_written_before_it_is_shown():
    assert "if (!shown || (yieldsToActivityBars && activeJobs.length)) return;" in JS


def test_a_strip_that_did_show_stays_long_enough_to_be_read():
    assert "const PROGRESS_MIN_VISIBLE_MS = 600;" in JS
    assert "PROGRESS_MIN_VISIBLE_MS - (Date.now() - shownAt)" in JS


def test_work_that_finished_inside_the_grace_period_hides_at_once():
    assert "if (!milliseconds) { pipelineStatus.classList.add(\"hidden\"); return; }" in JS


def test_the_success_path_hides_through_that_delay():
    assert "hidePipelineStatusIn(stopProgress());" in JS


def test_stopping_twice_does_not_ask_for_a_second_delay():
    """The finally block stops it again after the success path already did."""
    assert "if (stopped || !shown) { stopped = true; return 0; }" in JS


# ---------- one owner for the strip ----------


def test_the_ticker_stands_down_while_the_activity_bars_are_up():
    """They say the same thing with more in them, and renderActivityBars hides the strip whenever
    they are showing -- so writing it anyway is what made the two take turns.

    Only that strip, though: a caller with a line of its own passes yieldsToActivityBars=false and
    keeps counting (see test_the_split_clock_keeps_running.py).
    """
    assert "yieldsToActivityBars && activeJobs.length)) return;" in JS
    assert "yieldsToActivityBars = true" in JS, "the shared strip must still stand down by default"


def test_the_bars_still_only_ever_hide_it():
    """Revealing it from there put an empty strip on the queue screen; that stays fixed."""
    bars = body_of("renderActivityBars")

    assert 'classList.add("hidden")' in bars
    assert 'classList.remove("hidden")' not in bars


# ---------- and the note that counts them says it properly ----------


