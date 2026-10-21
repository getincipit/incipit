import io

import pytest
from conftest import tiny_image_bytes
from PIL import Image

from incipit.imaging import normalize_image_bytes


def test_normalize_converts_png_to_jpeg():
    png_bytes = tiny_image_bytes("PNG")

    result_bytes, result_filename = normalize_image_bytes(png_bytes, "scan.png")

    assert result_filename == "scan.jpg"
    with Image.open(io.BytesIO(result_bytes)) as img:
        assert img.format == "JPEG"


def test_normalize_converts_tiff_to_jpeg():
    tiff_bytes = tiny_image_bytes("TIFF")

    result_bytes, result_filename = normalize_image_bytes(tiff_bytes, "0001_scan.tif")

    assert result_filename == "0001_scan.jpg"
    with Image.open(io.BytesIO(result_bytes)) as img:
        assert img.format == "JPEG"


def test_normalize_converts_bmp_to_jpeg():
    bmp_bytes = tiny_image_bytes("BMP")

    result_bytes, result_filename = normalize_image_bytes(bmp_bytes, "scan.bmp")

    assert result_filename == "scan.jpg"
    with Image.open(io.BytesIO(result_bytes)) as img:
        assert img.format == "JPEG"


def test_normalize_converts_webp_to_jpeg():
    webp_bytes = tiny_image_bytes("WEBP")

    result_bytes, result_filename = normalize_image_bytes(webp_bytes, "scan.webp")

    assert result_filename == "scan.jpg"
    with Image.open(io.BytesIO(result_bytes)) as img:
        assert img.format == "JPEG"


def test_normalize_leaves_existing_jpeg_bytes_unchanged():
    jpeg_bytes = tiny_image_bytes("JPEG")

    result_bytes, result_filename = normalize_image_bytes(jpeg_bytes, "scan.jpg")

    assert result_bytes == jpeg_bytes
    assert result_filename == "scan.jpg"


def test_normalize_rejects_bytes_that_arent_an_image():
    with pytest.raises(ValueError, match="not a recognized image file"):
        normalize_image_bytes(b"not an image, just some junk bytes", "Thumbs.db")


def test_normalize_keeps_only_the_first_frame_of_a_multi_frame_tiff():
    buffer = io.BytesIO()
    frame_one = Image.new("RGB", (2, 2), color=(255, 0, 0))
    frame_two = Image.new("RGB", (2, 2), color=(0, 255, 0))
    frame_one.save(buffer, format="TIFF", save_all=True, append_images=[frame_two])

    result_bytes, result_filename = normalize_image_bytes(buffer.getvalue(), "multipage.tif")

    assert result_filename == "multipage.jpg"
    with Image.open(io.BytesIO(result_bytes)) as img:
        assert img.format == "JPEG"
        # JPEG is lossy -- allow rounding, just confirm it's red (frame one), not green (frame two)
        r, g, b = img.getpixel((0, 0))
        assert r > 200 and g < 50


def test_normalize_falls_back_to_page_jpg_when_filename_is_empty():
    png_bytes = tiny_image_bytes("PNG")

    _, result_filename = normalize_image_bytes(png_bytes, "")

    assert result_filename == "page.jpg"
