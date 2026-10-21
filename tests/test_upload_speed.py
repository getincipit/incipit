"""What an upload is allowed to spend."""

import io

import pytest
from PIL import Image

from incipit import db, imaging, split
from incipit.config import Settings
from incipit.storage import BucketStorage


def a_jpeg(width=1400, height=1000, colour=(120, 90, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


@pytest.fixture
def site(tmp_path):
    return Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
                    db_path=str(tmp_path / "extraction.db"))


# ---------- the decode that was thrown away ----------


def test_a_jpeg_comes_back_byte_for_byte(site):
    """The whole reason the decode was pointless: these bytes are the caller's own."""
    content = a_jpeg()

    out, name = imaging.normalize_image_bytes(content, "E2013_Page_1.jpg")

    assert out == content
    assert name == "E2013_Page_1.jpg"


def test_a_jpeg_is_not_decoded_at_full_size(monkeypatch):
    """draft() is what makes it cheap."""
    from PIL import JpegImagePlugin

    drafted = []
    original = JpegImagePlugin.JpegImageFile.draft

    def record(self, mode, size):
        drafted.append((mode, size))
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", record)

    imaging.normalize_image_bytes(a_jpeg(), "page.jpg")

    assert drafted, "the JPEG was decoded at full resolution to be handed straight back"


def test_a_truncated_scan_is_still_caught():
    """The check has to survive the speed-up: draft still reads every byte of the stream."""
    content = a_jpeg()

    with pytest.raises(ValueError, match="not a recognized image"):
        imaging.normalize_image_bytes(content[: len(content) // 3], "half.jpg")


def test_a_file_that_is_not_an_image_is_still_caught():
    """A folder picker sweeps up Thumbs.db and .DS_Store; they must not sink the batch."""
    with pytest.raises(ValueError, match="not a recognized image"):
        imaging.normalize_image_bytes(b"not an image at all", "Thumbs.db")


def test_a_png_is_still_converted_at_its_own_size():
    """draft() is on the JPEG branch only -- a drafted PNG would be converted at an eighth."""
    buffer = io.BytesIO()
    Image.new("RGB", (900, 700), (30, 90, 140)).save(buffer, format="PNG")

    out, name = imaging.normalize_image_bytes(buffer.getvalue(), "scan.png")

    assert name == "scan.jpg"
    assert Image.open(io.BytesIO(out)).size == (900, 700)


# ---------- the name check that was one round trip per page ----------


def test_the_folder_is_listed_once_however_many_pages(site, monkeypatch):
    storage = BucketStorage(site.bucket_uri)
    stats = []
    monkeypatch.setattr(BucketStorage, "exists",
                        lambda self, key: stats.append(key) or storage.exists(key))

    split.run_ingest_batch([(f"p{i}.jpg", a_jpeg()) for i in range(6)], site, folder_display_id="E2013")

    # derived/ lookups belong to the display cache, which is a different step.
    asked = [key for key in stats if key.startswith("uploads/")]
    assert not asked, f"still asking per page: {asked[:4]}"


def test_two_uploads_of_one_folder_are_stored_apart(site):
    """Each batch has its own directory, so the second upload cannot overwrite the first."""
    pages = [("E2013_Page_1.jpg", a_jpeg())]
    first = split.run_ingest_batch(pages, site, folder_display_id="E2013")
    second = split.run_ingest_batch(pages, site, folder_display_id="E2013")

    with db.connect(site.db_path) as conn:
        keys = [db.get_batch_images(conn, b)[0]["bucket_key"] for b in (first, second)]
    assert keys[0] != keys[1]
