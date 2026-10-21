"""Shared image-model retry, chunking, and response-quality behavior."""

from __future__ import annotations

import logging
import re
import time

from . import imaging, openrouter
from .config import Settings
from .storage import BucketStorage

logger = logging.getLogger(__name__)

# The most raw image bytes a single OpenRouter refine call carries before its gateway is at risk of
# rejecting the request as too large (413).
MAX_MODEL_CHUNK_BYTES = 8_000_000


def page_images(pages, storage: BucketStorage) -> list[bytes]:
    """One document's pages, sized for a model call rather than for the archive."""
    return [
        imaging.downscale_for_model(storage.read_bytes(page["bucket_key"]))
        for page in pages if page["bucket_key"]
    ]


# How many times one document's model call is attempted before it is recorded as failed, and how
# long to wait between attempts.
MAX_DOCUMENT_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = (10, 30, 90)


def with_model_retries(label: str, call, last_resort=None):
    """Runs `call()`, retrying anything transient, and returns its result."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_DOCUMENT_ATTEMPTS + 1):
        try:
            return call()
        except (openrouter.AccountError, openrouter.RequestTooLargeError):
            # A bad key, no credits, or a request the gateway will not accept at any size: no model
            # fixes those.
            raise
        except FileNotFoundError:
            # A scan missing from the bucket is missing on every attempt and for every model.
            raise
        except (openrouter.ResponseTruncatedError, openrouter.DegenerateResponseError,
                openrouter.MalformedResponseError) as exc:
            # Asking the same model again buys the same answer -- but the other model is still
            # worth a try, so this stops retrying without stepping over the fallback below.
            last_error = exc
            break
        except Exception as exc:  # noqa: BLE001 - anything else is worth another attempt
            last_error = exc
            if attempt == MAX_DOCUMENT_ATTEMPTS:
                break
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "%s failed on attempt %d/%d (%s); retrying in %ds",
                label, attempt, MAX_DOCUMENT_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    if last_resort is not None:
        try:
            return last_resort()
        except Exception as exc:  # noqa: BLE001 - the first failure is the one worth reporting
            logger.warning("%s: the fallback model could not do it either (%s)", label, exc)
            # Still the primary's failure, but saying the fallback ran: without this a reviewer
            # reads the message as the fallback never having been tried.
            if len(last_error.args) == 1 and isinstance(last_error.args[0], str):
                last_error.args = (f"{last_error.args[0]} -- the fallback model could not do it "
                                   f"either: {exc}",)
    raise last_error


def _page_byte_chunks(pages, storage: BucketStorage, max_bytes: int):
    """Yields consecutive sublists of `pages` in order, each whose total raw image bytes stay
    within `max_bytes` -- except a single page larger than max_bytes on its own, which still
    gets its own chunk rather than being dropped (page order and completeness both matter)."""
    chunk, chunk_bytes = [], 0
    for page in pages:
        size = storage.size(page["bucket_key"])
        if chunk and chunk_bytes + size > max_bytes:
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(page)
        chunk_bytes += size
    if chunk:
        yield chunk


# `[page N]` wherever it lands, matching what the review screen actually honours.
PAGE_BREAK = re.compile(r"\[page\s+\d+\]", re.IGNORECASE)


def warn_on_missing_page_breaks(result: dict, page_count: int) -> int:
    """Says so when a draft came back with fewer page breaks than it had scans."""
    found = len(PAGE_BREAK.findall(result.get("text") or ""))
    if found < max(page_count - 1, 0):
        logger.warning(
            "transcription came back with %d page break(s) for %d scan(s); the review screen will "
            "not line the text up against the images past the last break",
            found, page_count,
        )
    return found


def has_fallback_model(settings: Settings) -> bool:
    """Whether a document the primary model refuses has somewhere else to go."""
    fallback = (getattr(settings, "openrouter_fallback_model", "") or "").strip()
    return bool(fallback) and fallback != settings.openrouter_model


def call_image_model(
    pages,
    storage: BucketStorage,
    settings: Settings,
    *,
    build_messages,
    schema,
    extractor,
    merge,
):
    """Runs the champion image model for one document's pages, so a document of any size
    eventually produces a draft rather than failing on the request-size limit."""
    usable = [p for p in pages if p["bucket_key"]]

    try:
        images = page_images(usable, storage)
        result = openrouter.call_openrouter(
            build_messages(images), settings, schema=schema, extract_result=extractor
        )
        result.setdefault("model", getattr(settings, "openrouter_model", ""))
        warn_on_missing_page_breaks(result, len(usable))
        return result
    except (openrouter.AccountError, openrouter.RequestTooLargeError):
        raise
    except RuntimeError as exc:
        # Two failures are recoverable by sending fewer pages per call: the request being too big
        # to accept (413), and the reply being too big to fit in one answer.
        truncated = isinstance(exc, openrouter.ResponseTruncatedError)
        if not (truncated or "413" in str(exc)) or len(usable) <= 1:
            raise
        reason = ("the transcription was longer than one reply can hold" if truncated
                  else "the request was too large to send (413)")

    logger.warning(
        "%s; transcribing this document in size-bounded chunks instead", reason
    )
    chunk_results = []
    for chunk_pages in _page_byte_chunks(usable, storage, MAX_MODEL_CHUNK_BYTES):
        chunk_images = page_images(chunk_pages, storage)
        try:
            chunk_results.append(
                openrouter.call_openrouter(
                    build_messages(chunk_images), settings, schema=schema, extract_result=extractor
                )
            )
        except RuntimeError as exc:
            # A lone page still too large even by itself -- nothing left to shrink.
            if "413" not in str(exc) or len(chunk_pages) > 1:
                raise
            raise

    merged = merge(chunk_results)
    merged.setdefault("model", getattr(settings, "openrouter_model", ""))
    warn_on_missing_page_breaks(merged, len(usable))
    return merged
