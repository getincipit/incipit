"""Split windows must be planned against the size the model is actually sent."""

import io

import pytest
from PIL import Image

from incipit import db, split
from incipit.config import Settings
from incipit.storage import BucketStorage

_ARCHIVAL_SCAN: bytes | None = None


def archival_scan_bytes() -> bytes:
    global _ARCHIVAL_SCAN
    if _ARCHIVAL_SCAN is None:
        buffer = io.BytesIO()
        Image.effect_noise((5100, 6600), 60).convert("RGB").save(buffer, format="JPEG", quality=90)
        _ARCHIVAL_SCAN = buffer.getvalue()
    return _ARCHIVAL_SCAN


@pytest.fixture(scope="module")
def scan() -> bytes:
    content = archival_scan_bytes()
    assert len(content) > split.MAX_SPLIT_WINDOW_BYTES / 2, (
        "this fixture only tests anything if one stored page is a large fraction of the window "
        f"budget; got {len(content)} bytes"
    )
    return content


def _batch(tmp_path, scan, pages=12):
    settings = Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
        openrouter_api_key="sk-test",
    )
    batch_id = split.run_ingest_batch([(f"{i:04d}.jpg", scan) for i in range(pages)], settings)
    with db.connect(settings.db_path) as conn:
        batch_images = db.get_batch_images(conn, batch_id)
    return settings, batch_id, batch_images


def _plan(batch_images, storage):
    overlap = min(split.SPLIT_WINDOW_OVERLAP_PAGES, split.MAX_SPLIT_WINDOW_PAGES // 2)
    return split._plan_split_windows(
        batch_images, 0, storage, split.MAX_SPLIT_WINDOW_PAGES, split.MAX_SPLIT_WINDOW_BYTES, overlap
    )


def test_a_window_of_large_scans_still_holds_pages_to_compare(tmp_path, scan):
    """The defect itself."""
    settings, _, batch_images = _batch(tmp_path, scan)
    windows = _plan(batch_images, split.ModelPageBytes(BucketStorage(settings.bucket_uri)))

    cores = [w["core_end"] - w["core_start"] for w in windows]
    assert min(cores) > 1, f"a window decides a page with no neighbour: {cores}"
    assert len(windows) <= len(batch_images) / 2


def test_planning_against_the_stored_scan_is_what_produced_one_page_windows(tmp_path, scan):
    """Pins the cause, not just the symptom: the same pages planned against bucket sizes -- what
    the planner did before -- collapse to one page per window."""
    settings, _, batch_images = _batch(tmp_path, scan)
    windows = _plan(batch_images, BucketStorage(settings.bucket_uri))

    cores = [w["core_end"] - w["core_start"] for w in windows]
    assert min(cores) == 1


def test_the_planner_measures_exactly_what_the_sender_transmits(tmp_path, scan):
    """Not "smaller than", which a stale scale-factor guess would also satisfy: the planner's
    number for a page must be the length of the bytes the request carries for it."""
    settings, _, batch_images = _batch(tmp_path, scan, pages=3)
    pages = split.ModelPageBytes(BucketStorage(settings.bucket_uri))

    for image in batch_images:
        assert pages.size(image["bucket_key"]) == len(pages.read_bytes(image["bucket_key"]))


def test_a_window_stays_inside_the_budget_it_was_planned_under(tmp_path, scan):
    """The budget exists because OpenRouter's gateway rejected an oversized request with a 413."""
    settings, _, batch_images = _batch(tmp_path, scan)
    pages = split.ModelPageBytes(BucketStorage(settings.bucket_uri))
    windows = _plan(batch_images, pages)

    for window in windows:
        sent = sum(len(pages.read_bytes(img["bucket_key"])) for img in window["images"])
        assert sent <= split.MAX_SPLIT_WINDOW_BYTES + split.SPLIT_WINDOW_OVERLAP_BYTES


def test_a_page_is_decoded_once_however_often_it_is_measured(tmp_path, scan):
    """A batch is replanned from scratch after a 413 shrink, and the look-back means neighbouring
    windows ask about the same page repeatedly."""
    settings, _, batch_images = _batch(tmp_path, scan, pages=3)
    inner = BucketStorage(settings.bucket_uri)
    reads = []
    original = inner.read_bytes
    inner.read_bytes = lambda key: (reads.append(key), original(key))[1]

    pages = split.ModelPageBytes(inner)
    for _ in range(4):
        for image in batch_images:
            pages.size(image["bucket_key"])

    assert sorted(reads) == sorted(img["bucket_key"] for img in batch_images)


def test_the_bytes_a_window_sends_are_the_downscaled_ones(tmp_path, scan, monkeypatch):
    """End to end through the real call path: whatever reaches build_split_messages must be the
    reduced page, not the archival scan."""
    settings, batch_id, _ = _batch(tmp_path, scan, pages=2)
    seen: list[int] = []

    def fake_build(image_bytes, compiled):
        seen.extend(len(b) for b in image_bytes)
        return [{"role": "user", "content": "x"}]

    def fake_call(messages, settings, schema=None, extract_result=None):
        return {"starts_new_document": [True] * len(seen), "is_duplicate_of_previous_page": [False] * len(seen),
                "cost": 0.0}

    monkeypatch.setattr(split.openrouter, "build_split_messages", fake_build)
    monkeypatch.setattr(split.openrouter, "call_openrouter", fake_call)

    split.run_propose_split(batch_id, settings)

    assert seen, "the split call never went out"
    assert max(seen) < len(scan) / 2, f"full-resolution scans went on the wire: {seen}"
