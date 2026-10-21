import pytest
import threading
import time

from incipit import activity


def teardown_function():
    activity.clear()


def test_a_tracked_job_is_visible_while_it_runs_and_gone_after():
    assert activity.snapshot() == []
    with activity.track("drafting", "Transcribing 3 documents"):
        running = activity.snapshot()
        assert len(running) == 1
        assert running[0]["kind"] == "drafting"
        assert running[0]["label"] == "Transcribing 3 documents"
        assert running[0]["elapsed_seconds"] >= 0
    assert activity.snapshot() == []


def test_a_failed_job_still_clears():
    """A crash must not leave the site claiming work is still in flight -- that would be worse than
    showing nothing, since a reviewer would wait for something that already died."""
    try:
        with activity.track("drafting", "doomed"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert activity.snapshot() == []


def test_concurrent_jobs_are_all_reported_oldest_first():
    """Starlette runs sync handlers in a threadpool, so several of these really are live at once."""
    started = threading.Event()
    release = threading.Event()

    def worker():
        with activity.track("splitting", "second"):
            started.set()
            release.wait(timeout=5)

    with activity.track("drafting", "first"):
        t = threading.Thread(target=worker)
        t.start()
        started.wait(timeout=5)
        labels = [e["label"] for e in activity.snapshot()]
        release.set()
        t.join(timeout=5)

    assert labels == ["first", "second"]
    assert activity.snapshot() == []


def test_detail_can_be_updated_while_running():
    with activity.track("splitting", "Proposing") as job:
        activity.set_detail(job, "12 of 40 pages examined")
        assert activity.snapshot()[0]["detail"] == "12 of 40 pages examined"


def test_elapsed_seconds_grows():
    with activity.track("drafting", "slow"):
        first = activity.snapshot()[0]["elapsed_seconds"]
        time.sleep(0.05)
        assert activity.snapshot()[0]["elapsed_seconds"] >= first


# ---------- single-flight ----------


def test_exclusive_refuses_a_second_job_of_the_same_kind():
    """Two reviewers clicking Draft at the same moment genuinely run at once (Starlette
    threadpool), and the spend cap is read per-document -- so both could see a total under the
    cap and together overshoot it."""
    activity.clear()
    try:
        with activity.exclusive("drafting", "Transcribing 5 documents"):
            with pytest.raises(activity.AlreadyRunning) as exc:
                with activity.exclusive("drafting", "Transcribing 3 more"):
                    pass
        assert exc.value.kind == "drafting"
        assert exc.value.running["label"] == "Transcribing 5 documents"   # names what holds the lock
    finally:
        activity.clear()


def test_exclusive_allows_different_kinds_concurrently():
    """Splitting and drafting don't contend for the same resource, so holding one must not block
    the other -- over-serializing would make the site feel broken."""
    activity.clear()
    try:
        with activity.exclusive("drafting", "Transcribing"):
            with activity.exclusive("splitting", "Proposing boundaries"):
                assert {e["kind"] for e in activity.snapshot()} == {"drafting", "splitting"}
    finally:
        activity.clear()


def test_exclusive_releases_the_lock_when_the_job_raises():
    """A crashed draft must not wedge the site forever -- the next attempt has to be allowed."""
    activity.clear()
    try:
        with pytest.raises(ValueError):
            with activity.exclusive("drafting", "Transcribing"):
                raise ValueError("openrouter blew up")
        assert {e["kind"] for e in activity.snapshot()} == set()
        with activity.exclusive("drafting", "Retrying"):      # must not raise
            pass
    finally:
        activity.clear()


def test_exclusive_is_race_free_under_real_threads():
    """The check and the registration must happen under one lock acquisition -- otherwise two
    threads can both observe 'nothing running' and both proceed, which is the exact bug."""
    activity.clear()
    started, refused = [], []
    barrier = threading.Barrier(8)

    def attempt(i):
        barrier.wait()          # maximize the chance of a genuine collision
        try:
            with activity.exclusive("drafting", f"job {i}"):
                started.append(i)
                time.sleep(0.02)
        except activity.AlreadyRunning:
            refused.append(i)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(started) == 1, f"exactly one job may run, got {started}"
    assert len(refused) == 7
    activity.clear()


def test_a_resource_cannot_be_mutated_twice_at_once():
    activity.clear()
    with activity.resource_lock("new-document:E2002AAA"):
        with pytest.raises(activity.ResourceBusy):
            with activity.resource_lock("new-document:E2002AAA"):
                pass

    # Releasing it must not wedge the document permanently.
    with activity.resource_lock("new-document:E2002AAA"):
        pass


def test_shared_saves_coexist_but_an_exclusive_redraft_does_not():
    activity.clear()
    with activity.resource_lock("new-document:E2002AAA", shared=True):
        with activity.resource_lock("new-document:E2002AAA", shared=True):
            with pytest.raises(activity.ResourceBusy):
                with activity.resource_lock("new-document:E2002AAA"):
                    pass
