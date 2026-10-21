"""Pages are reduced before they go on the wire, and never at the cost of a document."""

import io

import pytest
from PIL import Image

from incipit import imaging, split


def jpeg(width, height, colour=(180, 170, 160)):
    """A real JPEG big enough not to be waved through by the small-file shortcut."""
    buffer = io.BytesIO()
    # Noise rather than a flat colour: a flat image compresses to almost nothing and would fall
    # under MODEL_IMAGE_MIN_BYTES however large its dimensions are.
    Image.effect_noise((width, height), 60).convert("RGB").save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def dims(content):
    with Image.open(io.BytesIO(content)) as img:
        return img.size


def test_an_oversized_scan_is_reduced_to_the_long_edge():
    out = imaging.downscale_for_model(jpeg(5142, 6656))
    assert max(dims(out)) == imaging.MODEL_IMAGE_LONG_EDGE


def test_the_aspect_ratio_survives():
    """A stretched page is a harder page to read, and the model sees only what we send."""
    width, height = dims(imaging.downscale_for_model(jpeg(5142, 6656)))
    assert abs((width / height) - (5142 / 6656)) < 0.01


def test_a_real_scan_shrinks_by_about_the_amount_that_made_it_sendable():
    """The gateway rejected 6.6 MB."""
    original = jpeg(5142, 6656)
    out = imaging.downscale_for_model(original)
    assert len(out) < len(original) / 3


def test_a_page_already_small_enough_is_returned_untouched():
    """Re-encoding costs quality for nothing -- the same reasoning that makes normalize_image_bytes
    pass JPEG through."""
    small = jpeg(800, 1000)
    assert imaging.downscale_for_model(small) is small


def test_a_small_file_is_never_re_encoded_whatever_its_dimensions():
    tiny = jpeg(40, 40)
    assert len(tiny) <= imaging.MODEL_IMAGE_MIN_BYTES
    assert imaging.downscale_for_model(tiny) is tiny


@pytest.mark.parametrize("junk", [b"", b"not an image at all", b"\xff\xd8\xff truncated"])
def test_something_that_is_not_an_image_comes_back_unchanged(junk):
    """This runs on the way to the expensive step."""
    assert imaging.downscale_for_model(junk) == junk


def test_a_pillow_failure_mid_resize_is_not_fatal(monkeypatch):
    monkeypatch.setattr(imaging.Image, "open",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("decoder exploded")))
    original = jpeg(5142, 6656)
    assert imaging.downscale_for_model(original) is original


def test_the_long_edge_is_configurable_per_call():
    assert max(dims(imaging.downscale_for_model(jpeg(5142, 6656), long_edge=1200))) == 1200


def test_a_long_edge_of_zero_disables_it():
    original = jpeg(5142, 6656)
    assert imaging.downscale_for_model(original, long_edge=0) is original


# ---------- where it has to be applied ----------


def test_pages_sent_for_transcription_are_reduced(tmp_path, monkeypatch):
    """The seam that matters."""
    from incipit import modeling

    big = jpeg(5142, 6656)

    class Storage:
        def read_bytes(self, key):
            return big

    sent = modeling.page_images([{"bucket_key": "a.jpg"}], Storage())
    assert max(dims(sent[0])) == imaging.MODEL_IMAGE_LONG_EDGE


def test_split_windows_are_reduced_further_than_transcription():
    """A split window carries many pages at once, and it is asking a smaller question than
    transcription: where does a document begin, not what does it say."""

    big = jpeg(5142, 6656)

    class Bucket:
        def read_bytes(self, key):
            return big

    page = split.ModelPageBytes(Bucket())
    assert max(dims(page.read_bytes("k"))) == imaging.SPLIT_IMAGE_LONG_EDGE
    assert page.size("k") == len(page.read_bytes("k")) < len(big)


def test_the_split_page_is_smaller_than_the_transcription_page():
    """The two sizes must not drift back together: transcription needs legibility on faded
    handwriting, the split does not."""
    assert imaging.SPLIT_IMAGE_LONG_EDGE < imaging.MODEL_IMAGE_LONG_EDGE
    big = jpeg(5142, 6656)
    assert len(imaging._resize_to_long_edge(big, imaging.SPLIT_IMAGE_LONG_EDGE, "split")) \
        < len(imaging.downscale_for_model(big))


def test_the_stored_scan_is_not_what_gets_reduced():
    """The bucket keeps the archival page."""
    big = jpeg(5142, 6656)
    kept, name = imaging.normalize_image_bytes(big, "E2002_Page_01.jpg")
    assert kept is big and name == "E2002_Page_01.jpg"


# ---------- the decoder does the coarse reduction, and changes nothing else ----------


def _full_decode(content: bytes, long_edge: int) -> bytes:
    """What _resize_to_long_edge did before it asked the JPEG decoder for help."""
    with Image.open(io.BytesIO(content)) as img:
        img.load()
        width, height = img.size
        if max(width, height) <= long_edge:
            return content
        ratio = long_edge / max(width, height)
        resized = img.convert("RGB").resize(
            (max(round(width * ratio), 1), max(round(height * ratio), 1)), Image.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@pytest.mark.parametrize("long_edge", [2400, 1600, 400])
def test_the_faster_decode_returns_exactly_the_same_size(long_edge):
    """The target is computed from the scan's own dimensions before anything is decoded."""
    scan = jpeg(3000, 4000)

    fast = imaging._resize_to_long_edge(scan, long_edge, "a test")
    full = _full_decode(scan, long_edge)

    with Image.open(io.BytesIO(fast)) as a, Image.open(io.BytesIO(full)) as b:
        assert a.size == b.size


def test_the_faster_decode_is_visually_the_same_page():
    """Measured on a real 5116x6666 scan: 0.4% of a grey level, against the model's own run to run
    variation of 0.5% of the transcription."""
    from PIL import ImageChops, ImageStat

    scan = jpeg(3000, 4000)

    fast = imaging._resize_to_long_edge(scan, 2400, "a test")
    full = _full_decode(scan, 2400)

    with Image.open(io.BytesIO(fast)) as a, Image.open(io.BytesIO(full)) as b:
        difference = ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB")))
    assert sum(difference.mean) / 3 < 5.0, "the reduced page no longer matches a full decode"


def test_a_page_small_enough_already_is_returned_untouched():
    scan = jpeg(800, 600)

    assert imaging._resize_to_long_edge(scan, 2400, "a test") is scan
