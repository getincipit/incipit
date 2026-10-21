"""What the review screens download to draw a page."""

import io
import pathlib

import pytest
from PIL import Image
from starlette.testclient import TestClient

from incipit import accounts, collab, db, imaging, review_server, split, storage
from incipit.config import Settings

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

_SCAN: bytes | None = None


def archival_scan() -> bytes:
    """A page the size and shape the client's scanner actually produces."""
    global _SCAN
    if _SCAN is None:
        buffer = io.BytesIO()
        Image.effect_noise((5142, 6656), 60).convert("RGB").save(buffer, format="JPEG", quality=92)
        _SCAN = buffer.getvalue()
    return _SCAN


def dims(content):
    with Image.open(io.BytesIO(content)) as img:
        return img.size


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path,
                        openrouter_api_key="sk-test")
    app = review_server.create_app(settings, str(tmp_path / "export" / "o.csv"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
    batch_id = split.run_ingest_batch(
        [("E2002_Page_01.jpg", archival_scan()), ("E2002_Page_02.jpg", archival_scan())],
        settings, folder_display_id="E2002")
    client = TestClient(app)
    client.cookies.clear()
    client.post("/login", data={"username": "paul", "password": "paul-password"},
                follow_redirects=False)
    return client, settings, batch_id


def image_url(batch_id, index=0, size=None):
    url = f"/api/new/batches/{batch_id}/images/{index}"
    return url if size is None else f"{url}?size={size}"


# ---------- serving a page at the size it will be drawn ----------


def test_a_thumbnail_is_a_thumbnail_not_a_scan(site):
    """The defect."""
    client, _, batch_id = site
    res = client.get(image_url(batch_id, size="thumb"))
    assert res.status_code == 200
    assert max(dims(res.content)) == imaging.DISPLAY_SIZES["thumb"]
    assert len(res.content) < len(archival_scan()) / 20


def test_the_viewer_gets_a_page_a_reviewer_can_actually_read(site):
    """Small is not the goal -- these are handwritten and faded documents, and the viewer pane is
    where someone decides what a page says."""
    client, _, batch_id = site
    res = client.get(image_url(batch_id, size="view"))
    assert max(dims(res.content)) == imaging.DISPLAY_SIZES["view"]
    assert max(dims(res.content)) > imaging.DISPLAY_SIZES["thumb"] * 3


def test_the_full_scan_is_still_served_byte_for_byte(site):
    """Asking for no size, or for "full", must hand back the archival page unchanged: it is what a
    download and a deliberate close look are for."""
    client, _, batch_id = site
    assert client.get(image_url(batch_id)).content == archival_scan()
    assert client.get(image_url(batch_id, size="full")).content == archival_scan()


def test_the_stored_scan_is_never_replaced_by_a_reduced_one(site):
    """The bucket holds the only full-resolution copy of a page."""
    client, settings, batch_id = site
    with db.connect(settings.db_path) as conn:
        key = db.get_batch_images(conn, batch_id)[0]["bucket_key"]
    bucket = storage.BucketStorage(settings.bucket_uri)

    client.get(image_url(batch_id, size="thumb"))
    client.get(image_url(batch_id, size="view"))

    assert bucket.read_bytes(key) == archival_scan()


def test_an_unknown_size_is_refused_rather_than_guessed(site):
    client, _, batch_id = site
    assert client.get(image_url(batch_id, size="enormous")).status_code == 400


def test_a_missing_page_is_still_a_404(site):
    client, _, batch_id = site
    assert client.get(image_url(batch_id, index=99, size="thumb")).status_code == 404


# ---------- letting the browser keep what it already has ----------


@pytest.mark.parametrize("size", ["thumb", "view", "full"])
def test_every_size_comes_back_with_a_validator(site, size):
    client, _, batch_id = site
    res = client.get(image_url(batch_id, size=size))
    assert res.headers.get("etag")
    assert "max-age" in res.headers.get("cache-control", "")


def test_asking_again_with_the_validator_costs_nothing(site):
    """This is what makes the panel's rows survive a re-render: 304, no body."""
    client, _, batch_id = site
    etag = client.get(image_url(batch_id, size="thumb")).headers["etag"]
    again = client.get(image_url(batch_id, size="thumb"), headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


def test_two_sizes_of_one_page_are_not_confused_for_each_other(site):
    """A shared validator would serve a thumbnail into the viewer pane."""
    client, _, batch_id = site
    thumb = client.get(image_url(batch_id, size="thumb")).headers["etag"]
    view = client.get(image_url(batch_id, size="view")).headers["etag"]
    assert thumb != view

    res = client.get(image_url(batch_id, size="view"), headers={"If-None-Match": thumb})
    assert res.status_code == 200
    assert max(dims(res.content)) == imaging.DISPLAY_SIZES["view"]


def test_two_pages_are_not_confused_for_each_other(site):
    client, _, batch_id = site
    first = client.get(image_url(batch_id, 0, "thumb")).headers["etag"]
    second = client.get(image_url(batch_id, 1, "thumb")).headers["etag"]
    assert first != second


# ---------- resizing a 34-megapixel page once, not per view ----------


def test_a_reduced_page_is_kept_so_paging_back_does_not_redo_the_work(site):
    client, settings, batch_id = site
    with db.connect(settings.db_path) as conn:
        key = db.get_batch_images(conn, batch_id)[0]["bucket_key"]
    bucket = storage.BucketStorage(settings.bucket_uri)

    body = client.get(image_url(batch_id, size="thumb")).content
    assert bucket.read_bytes(storage.derived_key(key, "thumb")) == body


def test_the_sizes_are_rendered_at_upload_not_on_the_first_view(site):
    """Reducing a 34-megapixel scan takes about a third of a second, and the split-review screen
    opens on a sidebar of every page at once."""
    _, settings, batch_id = site
    with db.connect(settings.db_path) as conn:
        keys = [i["bucket_key"] for i in db.get_batch_images(conn, batch_id)]
    bucket = storage.BucketStorage(settings.bucket_uri)

    for key in keys:
        for size in ("thumb", "view"):
            assert bucket.exists(storage.derived_key(key, size)), f"{size} of {key} was not warmed"


def test_each_size_is_reduced_from_the_last_not_from_the_scan(tmp_path):
    """Decoding a 5142x6656 JPEG is the expensive part, so 5142 -> 1600 -> 400 pays it once instead
    of once per size."""
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'b').as_posix()}",
                        db_path=str(tmp_path / "x.db"), openrouter_api_key="sk-test")
    bucket = storage.BucketStorage(settings.bucket_uri)
    bucket.upload_bytes("B", "p.jpg", archival_scan(), prefix="new_intake")
    key = "new_intake/B/p.jpg"

    sources = []
    real = imaging.render_for_display

    def watched(content, size):
        sources.append((size, max(dims(content))))
        return real(content, size)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(imaging, "render_for_display", watched)
    try:
        imaging.warm_display_cache(bucket, key, archival_scan())
    finally:
        monkeypatch.undo()

    assert [s for s, _ in sources] == ["view", "thumb"], "largest first, or the chain saves nothing"
    assert sources[0][1] == 6656, "the view is reduced from the scan"
    assert sources[1][1] == imaging.DISPLAY_SIZES["view"], "the thumb is reduced from the view"


def test_the_chained_thumbnail_is_still_the_right_size(tmp_path):
    """Chaining is only worth it if the result is the same page a direct render would give."""
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'b').as_posix()}",
                        db_path=str(tmp_path / "x.db"), openrouter_api_key="sk-test")
    bucket = storage.BucketStorage(settings.bucket_uri)
    bucket.upload_bytes("B", "p.jpg", archival_scan(), prefix="new_intake")
    key = "new_intake/B/p.jpg"

    imaging.warm_display_cache(bucket, key, archival_scan())

    for size in ("thumb", "view"):
        cached = bucket.read_bytes(storage.derived_key(key, size))
        assert max(dims(cached)) == imaging.DISPLAY_SIZES[size]
        width, height = dims(cached)
        assert abs((width / height) - (5142 / 6656)) < 0.02


def test_warming_a_batch_uses_more_than_one_worker(tmp_path, monkeypatch):
    """Pillow releases the interpreter lock while it decodes and resizes, so this is real
    parallelism rather than threads taking turns."""
    import threading
    import time as time_module

    from incipit import split as split_module

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def slow_warm(storage_, key, content):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time_module.sleep(0.05)
        with lock:
            in_flight -= 1

    monkeypatch.setattr(split_module.imaging, "warm_display_cache", slow_warm)
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'b').as_posix()}",
                        db_path=str(tmp_path / "x.db"), openrouter_api_key="sk-test")
    tiny = archival_scan()
    split.run_ingest_batch([(f"{i}.jpg", tiny) for i in range(6)], settings)

    assert peak > 1, "pages were warmed one at a time"


def test_a_page_that_cannot_be_resized_still_uploads(site, monkeypatch, tmp_path):
    """An upload is the reviewer's work."""
    monkeypatch.setattr(imaging, "render_for_display",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("decoder exploded")))
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'b2').as_posix()}",
                        db_path=str(tmp_path / "x.db"), openrouter_api_key="sk-test")
    with pytest.raises(RuntimeError):
        imaging.render_for_display(b"", "thumb")  # the stub really is in place

    batch_id = split.run_ingest_batch([("p.jpg", archival_scan())], settings)
    with db.connect(settings.db_path) as conn:
        assert len(db.get_batch_images(conn, batch_id)) == 1


def test_a_cached_copy_is_reused_rather_than_rendered_again(site, monkeypatch):
    client, _, batch_id = site
    client.get(image_url(batch_id, size="thumb"))

    monkeypatch.setattr(imaging, "render_for_display",
                        lambda *a, **kw: pytest.fail("re-rendered a page already cached"))
    assert client.get(image_url(batch_id, size="thumb")).status_code == 200


def test_a_cache_that_cannot_be_written_still_serves_the_page(site, monkeypatch):
    """A cache is a speed-up."""
    client, _, batch_id = site
    monkeypatch.setattr(storage.BucketStorage, "write_bytes",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")))
    res = client.get(image_url(batch_id, size="thumb"))
    assert res.status_code == 200
    assert max(dims(res.content)) == imaging.DISPLAY_SIZES["thumb"]


def test_discarding_a_batch_takes_its_reduced_copies_with_it(site):
    """A derived image is a cache of a scan and must not outlive it -- both because it would be
    unreachable storage, and because a batch id reused later would show the old batch's pages."""
    client, settings, batch_id = site
    with db.connect(settings.db_path) as conn:
        key = db.get_batch_images(conn, batch_id)[0]["bucket_key"]
    client.get(image_url(batch_id, size="thumb"))
    bucket = storage.BucketStorage(settings.bucket_uri)
    assert bucket.exists(storage.derived_key(key, "thumb"))

    split.delete_batch(batch_id, settings)

    assert not bucket.exists(storage.derived_key(key, "thumb"))
    assert not bucket.exists(key)


# ---------- the screens ask for the size they draw ----------


def test_the_split_sidebar_asks_for_thumbnails(site):
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    panel = source.split("function renderSplitDocPanel", 1)[1].split("\n  }", 1)[0]
    assert 'pageImageUrl(g.pages[0].image_url, "thumb")' in panel


def test_the_split_viewer_asks_for_a_readable_page(site):
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'el("split-viewer-img").src = pageImageUrl(page.image_url, "view");' in source


def test_the_sidebar_is_not_rebuilt_on_every_interaction():
    """renderSplitReview runs on every page turn and every click."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    panel = source.split("function renderSplitDocPanel", 1)[1].split("\n  }", 1)[0]
    assert "if (list.children.length !== groups.length)" in panel, \
        "the row list is rebuilt unconditionally"


