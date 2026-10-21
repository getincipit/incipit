import json
import threading
import time

import pytest
import requests
from conftest import bill, document_named, large_jpeg_bytes, tiny_image_bytes, seed_draft

from incipit import db, split
from incipit.config import Settings
from incipit.storage import BucketStorage


def _settings(tmp_path) -> Settings:
    return Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
        openrouter_api_key="sk-test",
    )


def test_run_ingest_batch_stores_images_in_given_order(tmp_path):
    settings = _settings(tmp_path)
    files = [("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes()), ("0003.jpg", tiny_image_bytes())]

    batch_id = split.run_ingest_batch(files, settings)

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "uploaded"
        assert batch["image_count"] == 3

        images = db.get_batch_images(conn, batch_id)
        assert [img["original_filename"] for img in images] == ["0001.jpg", "0002.jpg", "0003.jpg"]
        assert [img["sequence_index"] for img in images] == [0, 1, 2]


def test_run_ingest_batch_sanitizes_a_path_traversal_filename(tmp_path):
    """A crafted upload filename must not be able to write outside the batch's own folder -- the
    bucket key is built from a bare basename, and the raw name is kept only for display."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("../../../evil.jpg", tiny_image_bytes())], settings)

    with db.connect(settings.db_path) as conn:
        img = db.get_batch_images(conn, batch_id)[0]
    assert ".." not in img["bucket_key"]
    assert img["bucket_key"].endswith("0000_evil.jpg")
    assert img["original_filename"] == "../../../evil.jpg"  # raw name preserved for display only


def test_run_ingest_batch_rejects_an_unsafe_folder_id(tmp_path):
    settings = _settings(tmp_path)
    try:
        split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="../escape")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_confirm_batch_split_rejects_a_path_traversal_document_id(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)
    try:
        split.confirm_batch_split(batch_id, [{"display_id": "../../evil", "sequence_indices": [0]}], settings)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # The batch must not have been confirmed or written anything under a traversed path.
    with db.connect(settings.db_path) as conn:
        assert db.get_batch(conn, batch_id)["status"] == "uploaded"


def test_run_ingest_batch_rejects_empty_upload(tmp_path):
    settings = _settings(tmp_path)
    try:
        split.run_ingest_batch([], settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_ingest_batch_rejects_batches_over_the_upload_sanity_cap(tmp_path):
    settings = _settings(tmp_path)
    files = [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(split.MAX_BATCH_UPLOAD_PAGES + 1)]
    try:
        split.run_ingest_batch(files, settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_ingest_batch_accepts_uploads_larger_than_one_split_window(tmp_path):
    """A batch bigger than MAX_SPLIT_WINDOW_PAGES (the per-Qwen-call limit) must still be accepted
    at ingest time -- run_propose_split is responsible for windowing it across multiple calls,
    not run_ingest_batch rejecting it upfront the way it used to."""
    settings = _settings(tmp_path)
    files = [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(split.MAX_SPLIT_WINDOW_PAGES + 5)]

    batch_id = split.run_ingest_batch(files, settings)

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["image_count"] == split.MAX_SPLIT_WINDOW_PAGES + 5


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeErrorResponse:
    """A response whose raise_for_status() behaves like a real 4xx from `requests` -- unlike
    _FakeResponse above (which never fails)."""

    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        raise ValueError("no body")

    @property
    def text(self):
        return ""


def _mock_split_response(monkeypatch, starts_new_document, cost=0.0003):
    _mock_split_responses(monkeypatch, [(starts_new_document, cost)])


def _mock_split_responses(monkeypatch, responses):
    """`responses` is one (starts_new_document, cost) pair per expected Qwen call, consumed in
    order -- lets a test drive run_propose_split's windowing through more than one call with a
    different (scripted) boundary-detection result each time, the way a real multi-window batch
    would get a different response per window."""
    fake_payloads = []
    for response in responses:
        flags, cost = response[0], response[1]
        dup_flags = response[2] if len(response) > 2 else [False] * len(flags)
        fake_payloads.append({
            "choices": [{"message": {"content": json.dumps({
                "starts_new_document": flags,
                "is_duplicate_of_previous_page": dup_flags,
            })}}],
            "usage": {"cost": cost},
        })
    calls = iter(fake_payloads)
    lock = threading.Lock()

    def fake_post(self, url, headers=None, json=None, timeout=None):
        with lock:
            payload = next(calls)
        return _FakeResponse(payload)

    monkeypatch.setattr("requests.Session.post", fake_post)


def test_run_propose_split_writes_proposal_back_onto_batch_images(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes()), ("0003.jpg", tiny_image_bytes())], settings
    )
    _mock_split_response(monkeypatch, [True, False, True])

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: False, 2: True}
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "split_proposed"
        assert batch["split_cost_estimate"] == 0.0003

        images = db.get_batch_images(conn, batch_id)
        assert [img["proposed_new_document"] for img in images] == [1, 0, 1]


def test_run_propose_split_writes_duplicate_scan_flags_back_onto_batch_images(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes()), ("0003.jpg", tiny_image_bytes())], settings
    )
    # Page 2 (index 2) is a duplicate scan of page 1 (index 1) -- same document, no boundary there.
    _mock_split_responses(monkeypatch, [([True, False, False], 0.0003, [False, False, True])])

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: False, 2: False}
    with db.connect(settings.db_path) as conn:
        images = db.get_batch_images(conn, batch_id)
        assert [img["is_duplicate_scan"] for img in images] == [0, 0, 1]


def test_run_propose_split_never_trusts_a_duplicate_flag_at_window_position_zero(tmp_path, monkeypatch):
    """Position 0 of any window either has no real predecessor (the batch's true first page) or its
    predecessor was already finalized in an earlier window and isn't resent here -- so even if
    the model claims it's a duplicate, there's no in-window evidence to trust that, and it must
    be forced False (see _propose_split_window)."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)
    # Model incorrectly claims the very first page is a duplicate -- impossible, nothing precedes
    # it.
    _mock_split_responses(monkeypatch, [([True, False], 0.0003, [True, False])])

    split.run_propose_split(batch_id, settings)

    with db.connect(settings.db_path) as conn:
        images = db.get_batch_images(conn, batch_id)
        assert [img["is_duplicate_scan"] for img in images] == [0, 0]


def test_run_propose_split_never_treats_a_document_start_as_a_duplicate(tmp_path, monkeypatch):
    """A page can't simultaneously start a new document and be a droppable duplicate of the page
    before it -- if the model flags both for the same page, starts_new_document wins."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes()), ("0003.jpg", tiny_image_bytes())], settings
    )
    _mock_split_responses(monkeypatch, [([True, True, False], 0.0003, [False, True, False])])

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: True, 2: False}
    with db.connect(settings.db_path) as conn:
        images = db.get_batch_images(conn, batch_id)
        assert [img["is_duplicate_scan"] for img in images] == [0, 0, 0]


def test_run_propose_split_windows_a_batch_larger_than_one_call(tmp_path, monkeypatch):
    """A batch bigger than MAX_SPLIT_WINDOW_PAGES must be processed across multiple Qwen calls, and
    a later window's opinion on a page it shares (via overlap) with an earlier window wins -- it
    has seen further ahead."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 5)
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 2)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(8)], settings)

    _mock_split_responses(
        monkeypatch,
        [
            ([True, False, False, True, False], 0.001),          # window 1's initial (revised) guess
            ([False, True, False, True, False], 0.0015),          # window 2 revises pages 3-4
        ],
    )

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {
        0: True, 1: False, 2: False,   # untouched -- outside window 2's overlap reach
        3: False, 4: True,             # revised by window 2 (had been True, False from window 1)
        5: False, 6: True, 7: False,   # window 2's own fresh core
    }
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        # Both calls' costs summed into the one total the $ cap tracks.
        assert batch["split_cost_estimate"] == 0.001 + 0.0015


def test_run_propose_split_logs_when_a_later_window_revises_an_earlier_ones_answer(tmp_path, monkeypatch, caplog):
    """The overlap-zone revision in the test above isn't silent -- it's logged so a reviewer (or
    whoever's debugging a weird split) can find out a page's classification actually changed
    mid-run, not just trust whatever the final value happens to be."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 5)
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 2)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(8)], settings)
    _mock_split_responses(
        monkeypatch,
        [
            ([True, False, False, True, False], 0.001),
            ([False, True, False, True, False], 0.0015),
        ],
    )

    with caplog.at_level("WARNING", logger="incipit.split"):
        split.run_propose_split(batch_id, settings)

    assert "revised page" in caplog.text and "sequence_index 3" in caplog.text


def test_run_propose_split_actually_dispatches_windows_concurrently(tmp_path, monkeypatch):
    """The whole point of this design: multiple windows' Qwen calls must genuinely overlap in wall
    time, not just be structurally allowed to."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 2)
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)  # keep windows disjoint -- 3 of them
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 3)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(6)], settings)

    good_payload = {
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.001},
    }
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def fake_post(self, url, headers=None, json=None, timeout=None):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.1)  # long enough for the other concurrently-dispatched calls to also arrive
        with lock:
            in_flight -= 1
        return _FakeResponse(good_payload)

    monkeypatch.setattr("requests.Session.post", fake_post)

    split.run_propose_split(batch_id, settings)

    # All 3 disjoint 2-page windows fit in one wave (MAX_CONCURRENT_SPLIT_CALLS=3) -- if dispatch
    # were still sequential, max_in_flight could never exceed 1.
    assert max_in_flight > 1


def test_plan_split_windows_handles_a_realistic_71_page_batch_efficiently(tmp_path):
    """Regression test for a real bug caught during testing: with only a page-count cap on overlap,
    10 pages of look-back alone consumed ~7MB of the 8MB core budget for realistically-sized
    (~700KB) scans, leaving room for barely one *new* page per window -- 61 windows for a
    71-page batch that should need about 7."""
    settings = _settings(tmp_path)
    files = [(f"{i:04d}.jpg", large_jpeg_bytes()) for i in range(71)]  # realistic average scan size
    batch_id = split.run_ingest_batch(files, settings)
    with db.connect(settings.db_path) as conn:
        batch_images = db.get_batch_images(conn, batch_id)
    storage = BucketStorage(settings.bucket_uri)

    effective_overlap = min(split.SPLIT_WINDOW_OVERLAP_PAGES, split.MAX_SPLIT_WINDOW_PAGES // 2)
    windows = split._plan_split_windows(
        batch_images, 0, storage, split.MAX_SPLIT_WINDOW_PAGES, split.MAX_SPLIT_WINDOW_BYTES, effective_overlap
    )

    # Roughly matches the ~7 sequential calls a 71-page batch needed before any of this windowing
    # work -- the whole point of overlap is added safety, not a *worse* call count than before.
    assert 5 <= len(windows) <= 10
    # And with the default concurrency, that collapses into just a few waves.
    waves_needed = -(-len(windows) // split.MAX_CONCURRENT_SPLIT_CALLS)  # ceiling division
    assert waves_needed <= 4
    # Every page still gets covered exactly once by some window's core -- no gaps, no double-owned
    # cores (overlap is allowed to repeat pages, cores must not).
    core_pages = [img["sequence_index"] for w in windows for img in w["images"][w["core_start"] - w["overlap_start"]:]]
    assert sorted(core_pages) == list(range(71))


def test_run_propose_split_completes_a_realistic_71_page_batch_across_multiple_waves(tmp_path, monkeypatch):
    """End-to-end: a realistic large batch actually completes, covering every page exactly once,
    using real concurrent dispatch (not just planning) -- the actual scenario "split proposal is
    slow" was reported against."""
    settings = _settings(tmp_path)
    files = [(f"{i:04d}.jpg", large_jpeg_bytes()) for i in range(71)]
    batch_id = split.run_ingest_batch(files, settings)

    call_count = 0
    lock = threading.Lock()

    def fake_post(self, url, headers=None, timeout=None, **kwargs):
        nonlocal call_count
        with lock:
            call_count += 1
        image_count = len(kwargs["json"]["messages"][0]["content"]) - 1  # minus the one text block
        return _FakeResponse({
            "choices": [{"message": {"content": json.dumps({
                "starts_new_document": [True] + [False] * (image_count - 1),
                "is_duplicate_of_previous_page": [False] * image_count,
            })}}],
            "usage": {"cost": 0.001},
        })

    monkeypatch.setattr("requests.Session.post", fake_post)

    proposals = split.run_propose_split(batch_id, settings)

    assert sorted(proposals.keys()) == list(range(71))
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "split_proposed"
        assert batch["split_proposal_cursor"] == 71
    # Matches the window count pinned in the planning test above -- confirms the real run took the
    # same number of calls the plan predicted, not more (e.g. from an unwanted re-examination).
    assert 5 <= call_count <= 10


def _raw_batch_images(storage: BucketStorage, sizes: list[int]) -> list[dict]:
    images = []
    for i, size in enumerate(sizes):
        bucket_key, _ = storage.upload_bytes("RAWBATCH", f"{i:04d}.bin", b"x" * size)
        images.append({"sequence_index": i, "bucket_key": bucket_key})
    return images


def test_plan_split_windows_stops_at_byte_budget_before_page_count(tmp_path):
    """A window's core must stop adding fresh images once their total size would exceed
    MAX_SPLIT_WINDOW_BYTES, even when MAX_SPLIT_WINDOW_PAGES would allow more -- validated live
    by a real 413 Request Entity Too Large from OpenRouter's gateway on a 40-image window of
    real archival scans averaging ~640KB each (see split.py's MAX_SPLIT_WINDOW_BYTES docstring)."""
    settings = _settings(tmp_path)
    storage = BucketStorage(settings.bucket_uri)
    batch_images = _raw_batch_images(storage, [10] * 5)

    # 25-byte budget, 10 bytes/image -> a 3rd image (30 bytes total) would exceed it.
    windows = split._plan_split_windows(batch_images, 0, storage, split.MAX_SPLIT_WINDOW_PAGES, 25, 0)

    assert len(windows[0]["images"]) == 2
    assert windows[0]["core_end"] == 2


def test_plan_split_windows_always_includes_a_single_image_that_alone_exceeds_the_byte_budget(tmp_path):
    """A window can't shrink one oversized image into a smaller request -- it still has to be sent
    (and left to _propose_split_window's own error handling) rather than skipped, which would
    hang run_propose_split's while loop (cursor never advancing) or silently drop a page."""
    settings = _settings(tmp_path)
    storage = BucketStorage(settings.bucket_uri)
    batch_images = _raw_batch_images(storage, [50])

    windows = split._plan_split_windows(batch_images, 0, storage, split.MAX_SPLIT_WINDOW_PAGES, 5, 0)

    assert len(windows) == 1
    assert len(windows[0]["images"]) == 1
    assert windows[0]["core_end"] == 1


def test_plan_split_windows_overlaps_each_window_with_the_previous_ones_core(tmp_path):
    """The whole point of a fixed overlap: window k's look-back is the tail of window (k-1)'s own
    core, not just fresh unexamined pages -- that's what gives the model context to correct a
    boundary near the seam."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(6)], settings)
    with db.connect(settings.db_path) as conn:
        batch_images = db.get_batch_images(conn, batch_id)
    from incipit.storage import BucketStorage
    storage = BucketStorage(settings.bucket_uri)

    windows = split._plan_split_windows(batch_images, 0, storage, 3, split.MAX_SPLIT_WINDOW_BYTES, 1)

    assert [(w["overlap_start"], w["core_start"], w["core_end"]) for w in windows] == [(0, 0, 3), (2, 3, 5), (4, 5, 6)]
    assert [img["sequence_index"] for img in windows[1]["images"]] == [2, 3, 4]  # 2 is look-back context


def test_run_propose_split_persists_each_window_before_the_next_one_can_fail(tmp_path, monkeypatch):
    """A later window's failure must not discard an earlier window's already-billed, already-
    successful result -- observed live when a 70-page real batch's later window hit a transient
    gateway error after several earlier windows had already succeeded."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 2)
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(4)], settings)

    # Window 1 (seq 0-1): a clean 2-page document, succeeds and is persisted.
    _mock_split_responses(
        monkeypatch,
        [([True, False], 0.001)] + [([True], 0.0007)] * 3,
    )

    try:
        split.run_propose_split(batch_id, settings)
        assert False, "expected ValueError from window 2's persistent length mismatch"
    except ValueError:
        pass

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        # Window 1's result and cost are still there despite window 2 never completing.
        assert batch["status"] == "split_proposed"
        assert batch["split_cost_estimate"] == 0.001
        images = {img["sequence_index"]: img["proposed_new_document"] for img in db.get_batch_images(conn, batch_id)}
        assert images[0] == 1
        assert images[1] == 0
        # Window 2's pages were never reached, so they keep their pre-proposal default.
        assert images[2] == 0
        assert images[3] == 0


def test_propose_split_window_retries_after_a_transient_network_error(tmp_path, monkeypatch):
    """A network hiccup (connection error, timeout, etc.) previously propagated straight out of
    _propose_split_window with no retry at this level at all -- only the wrong-length case
    (below) was ever retried here."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)

    good_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.001},
    })
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("connection reset")
        return good_payload

    monkeypatch.setattr("requests.Session.post", fake_post)

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: False}
    assert len(calls) == 2  # one failed attempt, one successful retry


def test_run_propose_split_keeps_earlier_windows_after_a_later_window_exhausts_transient_retries(tmp_path, monkeypatch):
    """Mirrors test_run_propose_split_persists_each_window_before_the_next_one_can_fail, but for a
    transient exception (repeated connection errors) instead of a persistent length mismatch --
    confirms the new window-level retry-on-exception path still correctly gives up after
    openrouter_retries attempts (rather than retrying forever), and that giving up doesn't
    discard an earlier window's already-persisted proposal."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 2)
    # One window per wave -- pins exactly which window ("call #1") gets which scripted response;
    # without this, concurrent dispatch could race window 1 and window 2 into either order.
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(4)], settings)

    good_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.001},
    })
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return good_payload  # window 1 (seq 0-1) succeeds
        raise requests.exceptions.ConnectionError("connection reset")  # window 2 always fails

    monkeypatch.setattr("requests.Session.post", fake_post)

    try:
        split.run_propose_split(batch_id, settings)
        assert False, "expected the transient error to propagate after exhausting retries"
    except requests.exceptions.ConnectionError:
        pass

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "split_proposed"
        assert batch["split_cost_estimate"] == 0.001
        assert batch["split_proposal_cursor"] == 2  # only window 1's 2 pages were ever finalized
        images = {img["sequence_index"]: img["proposed_new_document"] for img in db.get_batch_images(conn, batch_id)}
        assert images[0] == 1
        assert images[1] == 0


def test_run_propose_split_completes_the_cursor_on_full_success(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)
    _mock_split_response(monkeypatch, [True, False])

    split.run_propose_split(batch_id, settings)

    with db.connect(settings.db_path) as conn:
        assert db.get_batch(conn, batch_id)["split_proposal_cursor"] == 2


def test_run_propose_split_resumes_from_the_persisted_cursor_after_a_failure(tmp_path, monkeypatch):
    """The actual recovery path for a batch that failed outright (not just a 413 shrinking can fix
    -- a real outage, or the $ cap since raised in the Settings menu): calling run_propose_split
    again must pick up exactly where it left off, never re-examining -- or re-billing -- pages
    already proposed, and must add to (not overwrite) the batch's already- recorded cost."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 2)
    # One window per wave -- pins exactly which window ("call #1") gets which scripted response;
    # without this, concurrent dispatch could race window 1 and window 2 into either order.
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(4)], settings)

    window1_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.001},
    })

    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return window1_payload  # window 1 (seq 0-1) succeeds
        raise requests.exceptions.ConnectionError("connection reset")  # window 2 always fails

    monkeypatch.setattr("requests.Session.post", fake_post)
    try:
        split.run_propose_split(batch_id, settings)
        assert False, "expected the first run to fail"
    except requests.exceptions.ConnectionError:
        pass

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["split_proposal_cursor"] == 2
        assert batch["split_cost_estimate"] == 0.001

    # Resume: window 2 (seq 2-3) now succeeds.
    window2_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.002},
    })
    resume_calls = []

    def fake_post_resume(self, url, headers=None, json=None, timeout=None):
        resume_calls.append(1)
        return window2_payload

    monkeypatch.setattr("requests.Session.post", fake_post_resume)

    proposals = split.run_propose_split(batch_id, settings)

    # Only the newly-processed window is in this call's own return value -- window 1's pages
    # (already correctly in the database) aren't re-examined, so they're not re-billed either.
    assert proposals == {2: True, 3: False}
    assert len(resume_calls) == 1

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "split_proposed"
        assert batch["split_proposal_cursor"] == 4
        assert batch["split_cost_estimate"] == 0.003  # 0.001 (run 1) + 0.002 (run 2), not overwritten
        images = {img["sequence_index"]: img["proposed_new_document"] for img in db.get_batch_images(conn, batch_id)}
        assert images == {0: 1, 1: 0, 2: 1, 3: 0}


def test_run_propose_split_shrinks_a_window_and_retries_after_a_413(tmp_path, monkeypatch):
    """OpenRouter's gateway rejecting a window as too large (413) is not a hard failure -- the
    window is halved and the wave retried from the same point, converging to whatever size
    actually works, rather than aborting the whole remaining batch on the first oversized window
    it hits."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 4)
    # No overlap -- keeps this test about 413 shrinking specifically, not also entangled with
    # overlap reconciliation (which has its own dedicated test).
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(4)], settings)

    good_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True, False],
            "is_duplicate_of_previous_page": [False, False],
        })}}],
        "usage": {"cost": 0.001},
    })
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeErrorResponse(413)  # the initial 4-page window is rejected as too large
        return good_payload  # the shrunk 2-page windows both succeed

    monkeypatch.setattr("requests.Session.post", fake_post)

    proposals = split.run_propose_split(batch_id, settings)

    # After the shrink (4 -> 2 pages), the whole 4-page batch is proposed as two 2-page windows.
    assert proposals == {0: True, 1: False, 2: True, 3: False}
    assert len(calls) == 3  # 1 rejected attempt + 2 successful shrunk-window calls

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "split_proposed"
        assert batch["split_cost_estimate"] == 0.002  # only the 2 successful calls billed


def test_run_propose_split_force_finalizes_a_single_page_still_too_large_after_shrinking(tmp_path, monkeypatch):
    """A page that's too large for OpenRouter's gateway even entirely on its own (nothing left to
    shrink) must not block every remaining page in the batch behind it -- force-finalized as its
    own one-page document instead, and the rest of the batch still completes normally."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)

    good_payload = _FakeResponse({
        "choices": [{"message": {"content": json.dumps({
            "starts_new_document": [True],
            "is_duplicate_of_previous_page": [False],
        })}}],
        "usage": {"cost": 0.0005},
    })
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) <= 2:
            return _FakeErrorResponse(413)
        return good_payload

    monkeypatch.setattr("requests.Session.post", fake_post)

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: True}
    assert len(calls) == 3


def test_run_propose_split_commits_a_window_with_no_internal_boundary_as_one_document(tmp_path, monkeypatch):
    """A window whose response has no boundary beyond the forced first position (the whole window
    reads as one document) must still commit its full core and let the next window continue --
    _plan_split_windows's core ranges always advance regardless of what a window's response
    says, so there's no "stuck re-examining the same images forever" failure mode here the way
    the old dynamic-pending design had to specifically guard against."""
    monkeypatch.setattr(split, "MAX_SPLIT_WINDOW_PAGES", 3)
    monkeypatch.setattr(split, "SPLIT_WINDOW_OVERLAP_PAGES", 0)
    monkeypatch.setattr(split, "MAX_CONCURRENT_SPLIT_CALLS", 1)
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(5)], settings)

    # Window 1 (seq 0-2): no boundary beyond position 0 -- commits as one 3-page document anyway.
    _mock_split_responses(
        monkeypatch,
        [
            ([True, False, False], 0.001),
            ([True, False], 0.0007),
        ],
    )

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: False, 2: False, 3: True, 4: False}
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["split_cost_estimate"] == 0.001 + 0.0007


def test_run_propose_split_rejects_mismatched_proposal_length(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)
    # A length mismatch is retried (settings.openrouter_retries times, default 3) before giving up
    # -- see _propose_split_window's docstring -- so every attempt needs its own mocked response.
    _mock_split_responses(monkeypatch, [([True], 0.0003)] * 3)  # only 1 flag for 2 pages, every time

    try:
        split.run_propose_split(batch_id, settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_propose_split_retries_a_mismatched_proposal_length_then_succeeds(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)
    _mock_split_responses(
        monkeypatch,
        [([True], 0.0003), ([True, False], 0.0004)],  # first attempt wrong length, second correct
    )

    proposals = split.run_propose_split(batch_id, settings)

    assert proposals == {0: True, 1: False}
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert batch["split_cost_estimate"] == 0.0003 + 0.0004  # both attempts' costs accumulated


def test_delete_batch_removes_intake_images_and_db_rows(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        images = db.get_batch_images(conn, batch_id)
    keys = [img["bucket_key"] for img in images]
    assert all(storage.exists(k) for k in keys)

    split.delete_batch(batch_id, settings)

    with db.connect(settings.db_path) as conn:
        assert db.get_batch(conn, batch_id) is None
        assert db.get_batch_images(conn, batch_id) == []
    assert not any(storage.exists(k) for k in keys)


def test_delete_batch_allowed_after_split_proposal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)
    _mock_split_response(monkeypatch, [True])
    split.run_propose_split(batch_id, settings)

    split.delete_batch(batch_id, settings)  # must not raise -- split_proposed is still deletable

    with db.connect(settings.db_path) as conn:
        assert db.get_batch(conn, batch_id) is None


def test_delete_batch_rejects_a_confirmed_batch(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)
    split.confirm_batch_split(batch_id, [{"display_id": "NEWDOC001", "sequence_indices": [0]}], settings)

    try:
        split.delete_batch(batch_id, settings)
        assert False, "expected ValueError"
    except ValueError:
        pass

    # the confirmed batch and its document must both survive the rejected delete attempt
    with db.connect(settings.db_path) as conn:
        assert db.get_batch(conn, batch_id) is not None
        assert db.get_new_document(conn, document_named(conn, "NEWDOC001")) is not None


def test_delete_batch_raises_for_unknown_batch(tmp_path):
    settings = _settings(tmp_path)
    db.init_db(settings.db_path)
    try:
        split.delete_batch("NONEXISTENT", settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_batch_folder_id_updates_a_pending_batch(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)  # no folder_id at upload time

    split.set_batch_folder_id(batch_id, "E2241", settings)

    with db.connect(settings.db_path) as conn:
        assert db.folder_display(conn, db.get_batch(conn, batch_id)["folder_id"])[0] == "E2241"


def test_set_batch_folder_id_can_also_set_the_folder_name(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)  # no folder_id/name at upload

    split.set_batch_folder_id(batch_id, "E2241", settings, folder_name="E2241_Equipment & Supplies")

    with db.connect(settings.db_path) as conn:
        folder = db.folder_display(conn, db.get_batch(conn, batch_id)["folder_id"])
        assert folder == ("E2241", "E2241_Equipment & Supplies")


def test_set_batch_folder_id_allowed_after_split_proposal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)
    _mock_split_response(monkeypatch, [True])
    split.run_propose_split(batch_id, settings)

    split.set_batch_folder_id(batch_id, "E2241", settings)  # must not raise

    with db.connect(settings.db_path) as conn:
        assert db.folder_display(conn, db.get_batch(conn, batch_id)["folder_id"])[0] == "E2241"


def test_set_batch_folder_id_rejects_a_confirmed_batch(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)
    split.confirm_batch_split(batch_id, [{"display_id": "NEWDOC001", "sequence_indices": [0]}], settings)

    try:
        split.set_batch_folder_id(batch_id, "E2241", settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_batch_folder_id_raises_for_unknown_batch(tmp_path):
    settings = _settings(tmp_path)
    db.init_db(settings.db_path)
    try:
        split.set_batch_folder_id("NONEXISTENT", "E2241", settings)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_confirm_batch_split_creates_documents_and_copies_images(tmp_path):
    settings = _settings(tmp_path)
    page_a = tiny_image_bytes(color=(255, 0, 0))
    page_b = tiny_image_bytes(color=(0, 255, 0))
    page_c = tiny_image_bytes(color=(0, 0, 255))
    batch_id = split.run_ingest_batch(
        [("0001.jpg", page_a), ("0002.jpg", page_b), ("0003.jpg", page_c)], settings
    )

    created = split.confirm_batch_split(
        batch_id,
        [
            {"display_id": "NEWDOC001", "sequence_indices": [0, 1]},
            {"display_id": "NEWDOC002", "sequence_indices": [2]},
        ],
        settings,
    )

    with db.connect(settings.db_path) as conn:
        assert [db.display_id_of(conn, d) for d in created] == ["NEWDOC001", "NEWDOC002"]
        batch = db.get_batch(conn, batch_id)
        assert batch["status"] == "confirmed"

        doc1_pages = db.get_new_document_pages(conn, created[0])
        assert len(doc1_pages) == 2
        assert [p["sequence_index"] for p in doc1_pages] == [0, 1]

        doc2_pages = db.get_new_document_pages(conn, created[1])
        assert len(doc2_pages) == 1

        images = {img["sequence_index"]: img for img in db.get_batch_images(conn, batch_id)}
        assert images[0]["assigned_document_id"] == created[0]
        assert images[2]["assigned_document_id"] == created[1]

    from incipit.storage import BucketStorage
    storage = BucketStorage(settings.bucket_uri)
    assert storage.read_bytes(doc1_pages[0]["bucket_key"]) == page_a
    assert storage.read_bytes(doc2_pages[0]["bucket_key"]) == page_c


def test_confirm_batch_split_rejects_blank_document_name(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)

    try:
        split.confirm_batch_split(batch_id, [{"display_id": "  ", "sequence_indices": [0]}], settings)
        assert False, "expected ValueError"
    except ValueError:
        pass




def test_confirm_batch_split_rejects_id_collision(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes())], settings)

    split.confirm_batch_split(batch_id, [{"display_id": "NEWDOC001", "sequence_indices": [0]}], settings)
    try:
        split.confirm_batch_split(batch_id, [{"display_id": "NEWDOC001", "sequence_indices": [1]}], settings)
        assert False, "expected ValueError for the collision"
    except ValueError:
        pass


def test_run_ingest_batch_stores_folder_id(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="D8026")

    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        assert db.folder_display(conn, batch["folder_id"])[0] == "D8026"


def test_suggest_document_ids_returns_empty_when_no_folder_id_given(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)  # no folder_id
    _mock_split_response(monkeypatch, [True])
    split.run_propose_split(batch_id, settings)

    assert split.suggest_document_ids(batch_id, settings) == []


def test_suggest_document_ids_proposes_one_id_per_proposed_group(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [("0001.jpg", tiny_image_bytes()), ("0002.jpg", tiny_image_bytes()), ("0003.jpg", tiny_image_bytes())], settings, folder_display_id="D8026"
    )
    _mock_split_response(monkeypatch, [True, False, True])  # 2 groups proposed
    split.run_propose_split(batch_id, settings)

    assert split.suggest_document_ids(batch_id, settings) == ["D8026AAA", "D8026AAB"]


def test_suggest_document_ids_defaults_to_one_group_when_proposal_never_ran(tmp_path):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id="D8026")

    assert split.suggest_document_ids(batch_id, settings) == ["D8026AAA"]


def test_suggest_more_document_ids_returns_empty_when_no_folder_id(tmp_path):
    settings = _settings(tmp_path)
    assert split.suggest_more_document_ids("", count=2, exclude=set(), settings=settings) == []


def test_suggest_more_document_ids_respects_exclude_for_names_not_yet_saved(tmp_path):
    settings = _settings(tmp_path)
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        folder_id = db.ensure_folder(conn, "D8026")

    # "D8026AAA" is only typed into another group's input on the split-review screen right now --
    # not saved anywhere the database could see -- so it must be passed via exclude explicitly.
    result = split.suggest_more_document_ids(folder_id, count=1, exclude={"D8026AAA"}, settings=settings)

    assert result == ["D8026AAB"]


def test_run_propose_split_stops_once_cost_cap_reached(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings)

    bill(settings.db_path, 100.0, "split")

    _mock_split_response(monkeypatch, [True])
    try:
        split.run_propose_split(batch_id, settings)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


# ---------- re-splitting already-confirmed documents ----------


def _confirmed_batch(tmp_path, pages=6, groups=None):
    """A batch confirmed into documents, i.e. the state a folder is in once drafting has started."""
    settings = _settings(tmp_path)
    batch_id = split.run_ingest_batch(
        [(f"{i:04d}.jpg", tiny_image_bytes()) for i in range(pages)], settings, folder_display_id="E2241"
    )
    groups = groups or [
        {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
        {"display_id": "E2241AAB", "sequence_indices": [2]},
        {"display_id": "E2241AAC", "sequence_indices": [3, 4, 5]},
    ]
    split.confirm_batch_split(batch_id, groups, settings)
    return settings, batch_id


def _ids(settings, *shown):
    """Internal ids of the documents shown by these display ids."""
    with db.connect(settings.db_path) as conn:
        return [document_named(conn, s) for s in shown]


def _shown(settings, document_ids):
    with db.connect(settings.db_path) as conn:
        return [db.display_id_of(conn, d) for d in document_ids]


def _all_shown(settings):
    with db.connect(settings.db_path) as conn:
        return {row[0] for row in conn.execute("SELECT display_id FROM new_documents")}


def test_collect_resplit_pages_opens_on_todays_grouping(tmp_path):
    settings, batch_id = _confirmed_batch(tmp_path)
    aaa, aab = _ids(settings, "E2241AAA", "E2241AAB")

    payload = split.collect_resplit_pages([aaa, aab], settings)

    assert payload["batch_id"] == batch_id
    assert payload["folder_display_id"] == "E2241"
    # Only the selected documents' own pages, in scan order, showing where documents start now.
    assert [p["sequence_index"] for p in payload["pages"]] == [0, 1, 2]
    assert [p["current_display_id"] for p in payload["pages"]] == ["E2241AAA", "E2241AAA", "E2241AAB"]
    assert [p["starts_new_document"] for p in payload["pages"]] == [True, False, True]


def test_resplit_merges_two_documents_into_one(tmp_path):
    """The '54 and 55 are actually one document' case."""
    settings, _ = _confirmed_batch(tmp_path)

    created = split.resplit_documents(
        _ids(settings, "E2241AAA", "E2241AAB"),
        [{"display_id": "E2241AAA", "sequence_indices": [0, 1, 2]}],
        settings,
    )

    assert _shown(settings, created) == ["E2241AAA"]
    assert "E2241AAB" not in _all_shown(settings)                  # absorbed, its name now free
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        pages = db.get_new_document_pages(conn, created[0])
        assert [p["sequence_index"] for p in pages] == [0, 1, 2]  # renumbered from 0 within the doc
        for page in pages:
            assert storage.exists(page["bucket_key"])
        # The untouched document keeps its pages exactly as they were.
        assert len(db.get_new_document_pages(conn, document_named(conn, "E2241AAC"))) == 3


def test_resplit_cuts_one_document_into_two(tmp_path):
    """The '58 contains two separate documents' case."""
    settings, _ = _confirmed_batch(tmp_path)

    created = split.resplit_documents(
        _ids(settings, "E2241AAC"),
        [
            {"display_id": "E2241AAC", "sequence_indices": [3]},
            {"display_id": "E2241AAD", "sequence_indices": [4, 5]},
        ],
        settings,
    )

    assert _shown(settings, created) == ["E2241AAC", "E2241AAD"]
    with db.connect(settings.db_path) as conn:
        assert [len(db.get_new_document_pages(conn, d)) for d in created] == [1, 2]
        # The rebuilt documents are still tied to their batch, so they can be fixed again later.
        assert db.get_new_document(conn, created[1])["batch_id"] == db.get_new_document(conn, created[0])["batch_id"]


def test_resplit_can_merge_and_cut_in_one_pass(tmp_path):
    settings, _ = _confirmed_batch(tmp_path)

    created = split.resplit_documents(
        _ids(settings, "E2241AAA", "E2241AAB", "E2241AAC"),
        [
            {"display_id": "E2241AAA", "sequence_indices": [0, 1, 2]},  # merge AAA+AAB
            {"display_id": "E2241AAB", "sequence_indices": [3, 4]},     # AAB's name reused downstream
            {"display_id": "E2241AAC", "sequence_indices": [5]},
        ],
        settings,
    )

    assert _shown(settings, created) == ["E2241AAA", "E2241AAB", "E2241AAC"]
    with db.connect(settings.db_path) as conn:
        assert [len(db.get_new_document_pages(conn, d)) for d in created] == [3, 2, 1]


def test_resplit_replaces_the_old_drafted_content(tmp_path):
    """A fixed document must not keep a transcription describing pages it no longer has."""
    settings, _ = _confirmed_batch(tmp_path)
    aaa, aab = _ids(settings, "E2241AAA", "E2241AAB")
    with db.connect(settings.db_path) as conn:
        seed_draft(
            conn, aaa, model="m", provider="p",
            result={"text": "stale text", "challenges": [], "uncertain_words": [],
                    "marginal_notes": [], "places": [], "names": []},
            ai_metadata={"title": "", "document_type": "", "date": "", "author": "",
                         "recipient": "", "place": "", "mentioned": "", "subjects": ""},
            ocr_draft_text="d", output_json={}, cost_estimate=0.001,
        )

    created = split.resplit_documents(
        [aaa, aab],
        [{"display_id": "E2241AAA", "sequence_indices": [0, 1, 2]}],
        settings,
    )

    with db.connect(settings.db_path) as conn:
        rebuilt = db.get_new_document(conn, created[0])
        assert rebuilt["status"] == "split_confirmed"   # back to undrafted, awaiting a fresh draft
        assert not rebuilt["text"]
        assert not rebuilt["reviewed_text"]


def test_resplit_refuses_to_drop_or_invent_pages(tmp_path):
    settings, _ = _confirmed_batch(tmp_path)
    aaa, aab = _ids(settings, "E2241AAA", "E2241AAB")

    with pytest.raises(ValueError, match="exactly the pages being fixed"):
        split.resplit_documents(
            [aaa, aab],
            [{"display_id": "E2241AAA", "sequence_indices": [0, 1]}],  # page 2 silently dropped
            settings,
        )
    with pytest.raises(ValueError, match="exactly the pages being fixed"):
        split.resplit_documents(
            [aaa],
            [{"display_id": "E2241AAA", "sequence_indices": [0, 1, 5]}],  # page 5 isn't AAA's
            settings,
        )
    # Nothing was touched by either rejected attempt.
    with db.connect(settings.db_path) as conn:
        assert len(db.get_new_document_pages(conn, aaa)) == 2
        assert db.get_new_document(conn, aab) is not None


def test_resplit_refuses_a_page_in_two_documents(tmp_path):
    settings, _ = _confirmed_batch(tmp_path)
    with pytest.raises(ValueError, match="more than one document"):
        split.resplit_documents(
            _ids(settings, "E2241AAA", "E2241AAB"),
            [
                {"display_id": "E2241AAA", "sequence_indices": [0, 1]},
                {"display_id": "E2241AAD", "sequence_indices": [1, 2]},
            ],
            settings,
        )


def test_resplit_refuses_a_name_belonging_to_another_document(tmp_path):
    """Reusing a name being replaced is fine; taking an untouched document's is not."""
    settings, _ = _confirmed_batch(tmp_path)
    with pytest.raises(ValueError, match="already in use"):
        split.resplit_documents(
            _ids(settings, "E2241AAA"),
            [{"display_id": "E2241AAC", "sequence_indices": [0, 1]}],  # AAC is not being replaced
            settings,
        )
    with db.connect(settings.db_path) as conn:
        assert len(db.get_new_document_pages(conn, document_named(conn, "E2241AAC"))) == 3   # untouched


def test_resplit_refuses_documents_from_different_batches(tmp_path):
    settings, _ = _confirmed_batch(tmp_path)
    other_batch = split.run_ingest_batch([("z.jpg", tiny_image_bytes())], settings, folder_display_id="E2241")
    split.confirm_batch_split(other_batch, [{"display_id": "E2241AAZ", "sequence_indices": [0]}], settings)

    with pytest.raises(ValueError, match="different scan batches"):
        split.resplit_documents(
            _ids(settings, "E2241AAA", "E2241AAZ"),
            [{"display_id": "E2241AAA", "sequence_indices": [0, 1]}],
            settings,
        )
