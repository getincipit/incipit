"""Normalizes whatever image format a reviewer's upload or the live site's image URL hands us into
real JPEG bytes, once, at ingest time -- so every downstream reader (bucket storage, modeling, and
openrouter.py's `_image_block`, which has always *labeled* every image `image/jpeg` without
verifying it) can safely assume that's true."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .storage import derived_key as storage_derived_key

logger = logging.getLogger(__name__)


def normalize_image_bytes(content: bytes, filename: str) -> tuple[bytes, str]:
    """Returns (jpeg_bytes, filename_with_.jpg_extension)."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format == "JPEG":
                # These bytes are returned untouched, so the decode only ever proved the file is a
                # real image.
                img.draft("L", (1, 1))
                img.load()
                return content, filename
            img.load()
            rgb = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"{filename!r} is not a recognized image file: {exc}") from exc

    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=92)
    stem = Path(filename).stem if filename else "page"
    return buffer.getvalue(), f"{stem}.jpg"


# Clockwise turns as Pillow's lossless transposes, which name the counter-clockwise angle.
_CLOCKWISE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def orient(content: bytes, rotation: int, mirrored: bool) -> bytes:
    """A scan as the reviewer turned it: mirrored left to right first, then rotated clockwise."""
    if rotation == 0 and not mirrored:
        return content
    with Image.open(io.BytesIO(content)) as img:
        img.load()
        turned = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if mirrored else img.copy()
        if rotation:
            turned = turned.transpose(_CLOCKWISE[rotation])
        buffer = io.BytesIO()
        # Re-encoded once for the export, at a quality that keeps faint pencil legible.
        turned.convert("RGB").save(buffer, format="JPEG", quality=95, dpi=img.info.get("dpi", (72, 72)))
    return buffer.getvalue()


# The long edge, in pixels, that a page is reduced to before it is sent to the model.
MODEL_IMAGE_LONG_EDGE = 2400

# Below this, re-encoding costs quality for nothing: a page already small enough to send is left
# exactly as it is, the same principle as normalize_image_bytes leaving JPEG bytes alone.
MODEL_IMAGE_MIN_BYTES = 400_000

# The split call gets a much smaller page than transcription does, because it is asking a much
# smaller question: does this page begin a new document?
SPLIT_IMAGE_LONG_EDGE = 1000


def downscale_for_model(content: bytes, long_edge: int = MODEL_IMAGE_LONG_EDGE) -> bytes:
    """A page reduced to something a model call can actually carry."""
    if not content or len(content) <= MODEL_IMAGE_MIN_BYTES or long_edge <= 0:
        return content
    return _resize_to_long_edge(content, long_edge, "for the model")


def _resize_to_long_edge(content: bytes, long_edge: int, purpose: str) -> bytes:
    """Shared by every caller that needs a page smaller than the archival scan."""
    if not content or long_edge <= 0:
        return content
    try:
        with Image.open(io.BytesIO(content)) as img:
            # From the header, before anything is decoded: computing it after the draft below makes
            # some pages a pixel narrower, which is a different image for no reason.
            width, height = img.size
            if max(width, height) <= long_edge:
                return content
            ratio = long_edge / max(width, height)
            target = (max(round(width * ratio), 1), max(round(height * ratio), 1))
            # The decoder does the coarse reduction itself, reading the scan DCT-scaled instead of
            # building the whole bitmap.
            img.draft("RGB", (long_edge, long_edge))
            img.load()
            resized = img.convert("RGB").resize(target, Image.LANCZOS)
        buffer = io.BytesIO()
        # 90 rather than 92: on a page already reduced to a quarter of its width, the difference is
        # invisible and the file is meaningfully smaller.
        resized.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - see docstring: never fatal
        logger.warning("could not resize a page %s (%s); using it unchanged", purpose, exc)
        return content


# The long edge of each size the review UI asks for.
DISPLAY_SIZES = {"thumb": 400, "view": 1600, "full": 0}
DEFAULT_DISPLAY_SIZE = "full"


def render_for_display(content: bytes, size: str) -> bytes:
    """A page at the size a screen actually draws it."""
    if size not in DISPLAY_SIZES:
        raise ValueError(f"unknown display size {size!r}; expected one of {sorted(DISPLAY_SIZES)}")
    long_edge = DISPLAY_SIZES[size]
    # Deliberately not downscale_for_model: that one leaves any file under 400 KB alone, because
    # for a model call the cost being avoided is bytes on the wire.
    return _resize_to_long_edge(content, long_edge, f"for display at {size}")


def warm_display_cache(storage, bucket_key: str, content: bytes) -> None:
    """Render every display size of a page now, so no screen has to wait for one later."""
    previous = content
    for size in sorted(DISPLAY_SIZES, key=lambda s: DISPLAY_SIZES[s] or 10**9, reverse=True):
        if size == "full":
            continue
        previous = cached_render(storage, bucket_key, size, source=previous)


def cached_render(storage, bucket_key: str, size: str, source: bytes | None = None) -> bytes:
    """A page at `size`, rendered once and then kept in the bucket beside the scan it came from."""
    if size not in DISPLAY_SIZES:
        raise ValueError(f"unknown display size {size!r}; expected one of {sorted(DISPLAY_SIZES)}")

    def original() -> bytes:
        return source if source is not None else storage.read_bytes(bucket_key)

    if size == "full":
        return original()

    cache_key = storage_derived_key(bucket_key, size)
    try:
        if storage.exists(cache_key):
            return storage.read_bytes(cache_key)
    except Exception as exc:  # noqa: BLE001 - see docstring: never fatal
        logger.warning("could not read the cached %s copy of %s (%s); rendering it again",
                       size, bucket_key, exc)

    try:
        content = render_for_display(original(), size)
    except Exception as exc:  # noqa: BLE001 - see docstring: never fatal
        logger.warning("could not render the %s copy of %s (%s); using the page unchanged",
                       size, bucket_key, exc)
        return original()

    try:
        storage.write_bytes(cache_key, content)
    except Exception as exc:  # noqa: BLE001 - a cache that will not write is slow, not broken
        logger.warning("could not cache the %s copy of %s (%s)", size, bucket_key, exc)
    return content
