"""Batch intake, model-proposed document boundaries, and confirming them into documents."""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import db, docid, imaging, openrouter, profiles, spend
from . import storage as storage_mod
from .config import Settings
from .storage import BucketStorage

logger = logging.getLogger(__name__)

# One Qwen split-proposal call holds at most this many page images -- a secondary safety cap
# alongside MAX_SPLIT_WINDOW_BYTES below (whichever limit a window hits first ends it).
MAX_SPLIT_WINDOW_PAGES = 16

# The real limiter found by the 413 above: caps a window by total raw image bytes, not just page
# count, since real scan file sizes vary widely (114KB-1.5MB observed in one folder alone) and a
# fixed page count can't account for that.
MAX_SPLIT_WINDOW_BYTES = 8_000_000

# Windows no longer depend on each other's Qwen response (see SPLIT_WINDOW_OVERLAP_PAGES below), so
# they can be sent concurrently instead of one-at-a-time -- a 71-page real folder needed ~7
# sequential calls before this, each easily 15-30s+ under normal conditions.
MAX_CONCURRENT_SPLIT_CALLS = 3

# Look-back into the previous window, so a document spanning a window edge is not mis-split.
SPLIT_WINDOW_OVERLAP_PAGES = 10

# Overlap's own byte budget, entirely separate from (and added on top of, not carved out of)
# MAX_SPLIT_WINDOW_BYTES -- confirmed necessary by testing against a realistic 71-page/~700KB-per-
# scan batch: with only the page-count cap above, 10 pages of overlap alone consumed ~7MB of the
# 8MB core budget, leaving room for barely one *new* page per window and producing 61 windows for a
# batch that should need about 7.
SPLIT_WINDOW_OVERLAP_BYTES = 1_500_000

# Sanity ceiling on a single upload, independent of the per-call window above -- large enough for a
# real bulk-scan session (windowing means there's no longer a hard *processing* reason to cap this
# low), small enough to catch someone accidentally selecting a folder orders of magnitude too big.
MAX_BATCH_UPLOAD_PAGES = 1000


_ALREADY_NUMBERED = re.compile(r"^[0-9]{2,4}[_-]")


# The folder a batch's documents are filed under when nobody named its folder before confirming.
UNFILED_FOLDER = "unfiled"


def bucket_folder_of(batch, batch_id: str) -> str:
    """Where this batch's scans actually sit in the bucket."""
    try:
        stored = batch["bucket_folder"]
    except (KeyError, IndexError, TypeError):
        stored = None
    return stored or batch_id


def page_filename(sequence_index: int, safe_name: str) -> str:
    """The scan's name inside the bucket."""
    if _ALREADY_NUMBERED.match(safe_name):
        return safe_name
    return f"{sequence_index:04d}_{safe_name}"


def run_ingest_batch(
    files: Collection[tuple[str, bytes]], settings: Settings, folder_display_id: str = "",
    folder_name: str = "", uploaded_by: int | None = None, tab_id: int = db.DEFAULT_TAB_ID,
) -> str:
    """Stores each uploaded image -- in the given order, which the caller (the browser's folder
    picker) is responsible for making scan order -- under uploads/{batch_id}/, and records the
    batch and its images. A folder display id finds or creates the folder in the tab."""
    if not files:
        raise ValueError("no files uploaded")
    if len(files) > MAX_BATCH_UPLOAD_PAGES:
        raise ValueError(
            f"batch of {len(files)} images exceeds the {MAX_BATCH_UPLOAD_PAGES}-page sanity limit "
            "for a single upload; split it into smaller batches"
        )
    if folder_display_id:
        docid.require_safe_id(folder_display_id, "Folder ID")

    db.init_db(settings.db_path)
    batch_id = uuid.uuid4().hex[:12]
    uploaded_at = datetime.now(timezone.utc).isoformat()
    bucket_folder = batch_id
    storage = BucketStorage(settings.bucket_uri)

    images = []
    skipped: list[str] = []
    to_warm: list[tuple[str, bytes]] = []
    for filename, content in files:
        # The upload filename becomes part of a bucket path -- reduce it to a bare basename so a
        # crafted name (e.g. "../../x.jpg") can't write outside the batch's folder.
        safe_name = Path(filename).name or f"page{len(images):04d}.jpg"
        try:
            content, safe_name = imaging.normalize_image_bytes(content, safe_name)
        except ValueError as exc:
            # A folder picker sweeps up whatever's in the directory -- OS junk like .DS_Store,
            # Thumbs.db, desktop.ini is common and shouldn't sink an otherwise-good batch upload.
            logger.warning("batch %s: skipping %s -- %s", batch_id, filename, exc)
            skipped.append(filename)
            continue
        sequence_index = len(images)
        # The batch's own directory and the sequence prefix make every key unique.
        bucket_key, bucket_uri = storage.upload_bytes(
            bucket_folder, page_filename(sequence_index, safe_name), content,
            prefix=storage_mod.UPLOADS_PREFIX,
        )
        to_warm.append((bucket_key, content))
        images.append(
            {
                "sequence_index": sequence_index,
                "original_filename": filename,
                "bucket_key": bucket_key,
                "bucket_uri": bucket_uri,
            }
        )

    if not images:
        raise ValueError("no files uploaded" if not skipped else "none of the uploaded files were recognized images")

    _warm_display_cache(to_warm, storage)

    with db.connect(settings.db_path) as conn:
        folder_id = (db.ensure_folder(conn, folder_display_id, folder_name, tab_id)
                     if folder_display_id else None)
        db.create_batch(conn, batch_id, image_count=len(images), folder_id=folder_id, tab_id=tab_id,
                        uploaded_folder_name=folder_name, uploaded_by=uploaded_by,
                        bucket_folder=bucket_folder, uploaded_at=uploaded_at)
        db.add_batch_images(conn, batch_id, images)

    logger.info(
        "ingested batch %s (%d images, %d skipped, folder %r)",
        batch_id, len(images), len(skipped), folder_display_id,
    )
    return batch_id


# Pillow releases the interpreter lock while it decodes and resizes, so warming genuinely runs in
# parallel rather than taking turns.
WARM_WORKERS = 4


def _warm_display_cache(pages: list[tuple[str, bytes]], storage: BucketStorage) -> None:
    """Render the review UI's page sizes for a freshly uploaded batch, before anyone opens it."""
    if not pages:
        return
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(WARM_WORKERS, len(pages))) as pool:
        list(pool.map(lambda page: imaging.warm_display_cache(storage, page[0], page[1]), pages))
    logger.info("rendered display sizes for %d page(s) in %.1fs", len(pages), time.monotonic() - started)


def delete_batch(batch_id: str, settings: Settings) -> None:
    """Discards an uploaded-but-never-confirmed batch entirely: its raw intake images in the bucket
    (`uploads/{batch_id}/`) and its `new_batches`/`new_batch_images` rows."""
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        if batch is None:
            raise ValueError(f"batch {batch_id!r} not found")
        if batch["status"] not in db.PENDING_BATCH_STATUSES:
            raise ValueError(
                f"batch {batch_id!r} has already been confirmed and can't be deleted this way"
            )

    storage = BucketStorage(settings.bucket_uri)
    storage.delete_prefix(storage_mod.UPLOADS_PREFIX, bucket_folder_of(batch, batch_id))

    with db.connect(settings.db_path) as conn:
        db.delete_batch_rows(conn, batch_id)


def set_batch_folder_id(batch_id: str, folder_display_id: str, settings: Settings,
                        folder_name: str = "") -> None:
    """Lets a reviewer name (or correct) a batch's folder after upload -- e.g. they forgot to fill
    it in before uploading -- so the split-review screen can auto-fill every document name."""
    if folder_display_id:
        docid.require_safe_id(folder_display_id, "Folder ID")
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        if batch is None:
            raise ValueError(f"batch {batch_id!r} not found")
        if batch["status"] not in db.PENDING_BATCH_STATUSES:
            raise ValueError(
                f"batch {batch_id!r} has already been confirmed and its folder id can't be changed"
            )
        # The folder is made in the tab the batch was uploaded to.
        folder_id = (db.ensure_folder(conn, folder_display_id, folder_name, batch["tab_id"])
                     if folder_display_id else None)
        if folder_id and folder_name:
            db.set_new_folder_name(conn, folder_id, folder_name)
        db.set_batch_folder_id(conn, batch_id, folder_id)


def suggest_document_ids(batch_id: str, settings: Settings) -> list[str]:
    """Proposes one display id per currently-proposed group in the batch, under the folder's id
    scheme (see docid.py) -- for pre-filling the split-review screen's name inputs."""
    with db.connect(settings.db_path) as conn:
        batch = db.get_batch(conn, batch_id)
        if batch is None:
            raise ValueError(f"batch {batch_id!r} not found")
        folder_id = batch["folder_id"]
        if not folder_id:
            return []

        batch_images = db.get_batch_images(conn, batch_id)
        if not batch_images:
            return []
        folder_display_id, _ = db.folder_display(conn, folder_id)
        existing_ids = db.display_ids_in_folder(conn, folder_id)
        scheme = db.folder_id_scheme(conn, folder_id)

    # The first page always starts a document by convention (see the split-review UI), even if the
    # split proposal itself failed and left every flag at its default False.
    starts = [img for index, img in enumerate(batch_images)
              if index == 0 or img["proposed_new_document"]]
    return docid.next_document_ids(folder_display_id, existing_ids, len(starts), scheme,
                                   [img["original_filename"] or "" for img in starts])


def suggest_more_document_ids(
    folder_id: str, count: int, exclude: set[str], settings: Settings
) -> list[str]:
    """Like `suggest_document_ids`, but for the split-review screen asking for more names *after*
    the reviewer's own toggling creates additional groups beyond what was originally proposed."""
    if not folder_id:
        return []
    with db.connect(settings.db_path) as conn:
        if db.get_folder(conn, folder_id) is None:
            return []
        folder_display_id, _ = db.folder_display(conn, folder_id)
        existing_ids = db.display_ids_in_folder(conn, folder_id) | exclude
        scheme = db.folder_id_scheme(conn, folder_id)
    return docid.next_document_ids(folder_display_id, existing_ids, count, scheme)


class ModelPageBytes:
    """A page's size and content *as the model will actually receive it*, not as it sits in the
    bucket."""

    def __init__(self, storage: BucketStorage, long_edge: int = imaging.SPLIT_IMAGE_LONG_EDGE):
        self._storage = storage
        self._long_edge = long_edge
        self._sizes: dict[str, int] = {}

    def read_bytes(self, bucket_key: str) -> bytes:
        content = imaging._resize_to_long_edge(
            self._storage.read_bytes(bucket_key), self._long_edge, "for the split")
        self._sizes[bucket_key] = len(content)
        return content

    def size(self, bucket_key: str) -> int:
        if bucket_key not in self._sizes:
            self.read_bytes(bucket_key)
        return self._sizes[bucket_key]


def _propose_split_window(
    window_images: list, storage: ModelPageBytes, settings: Settings, compiled
) -> tuple[list[bool], list[bool], float]:
    """One Qwen call for a single window of up to MAX_SPLIT_WINDOW_PAGES page images."""
    # Already reduced for the wire, same as a transcription's pages -- see ModelPageBytes, which is
    # also what _plan_split_windows measured this window against.
    image_bytes = [storage.read_bytes(img["bucket_key"]) for img in window_images]
    messages = openrouter.build_split_messages(image_bytes, compiled)

    attempts = max(settings.openrouter_retries, 1)
    total_cost = 0.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = openrouter.call_openrouter(
                messages, settings, schema=openrouter.SPLIT_JSON_SCHEMA, extract_result=openrouter.extract_split_result
            )
        except (requests.exceptions.RequestException, ValueError) as exc:
            # Observed live against a real 70-page batch: a transient failure on one window (a
            # network hiccup, or call_openrouter itself exhausting its own retries on a malformed-
            # JSON response) previously propagated straight out of this function with no retry at
            # this level at all -- only the wrong-length case below was ever retried here.
            last_error = exc
            logger.warning("split proposal call failed: %s (attempt %d/%d); retrying", exc, attempt, attempts)
            continue
        total_cost += result.get("cost") or 0.0
        flags = result["starts_new_document"]
        dup_flags = result["is_duplicate_of_previous_page"]
        if len(flags) != len(window_images) or len(dup_flags) != len(window_images):
            last_error = ValueError(
                f"split proposal length ({len(flags)} boundary, {len(dup_flags)} duplicate) does "
                f"not match window size {len(window_images)}"
            )
            logger.warning("%s (attempt %d/%d); retrying", last_error, attempt, attempts)
            continue
        starts_new = [bool(f) for f in flags]
        is_duplicate = [bool(f) and not starts_new[i] for i, f in enumerate(dup_flags)]
        if is_duplicate:
            is_duplicate[0] = False
        return starts_new, is_duplicate, total_cost

    raise last_error


def _plan_split_windows(
    batch_images: list, start_cursor: int, storage: ModelPageBytes, max_pages: int, max_bytes: int,
    overlap_pages: int, overlap_bytes: int = SPLIT_WINDOW_OVERLAP_BYTES,
) -> list[dict]:
    """Partitions batch_images[start_cursor:] into windows whose CORE ranges tile the remaining
    sequence with no gaps or overlaps, from position and byte size alone."""
    windows = []
    core_start = start_cursor
    while core_start < len(batch_images):
        # Look-back scans backwards from core_start, bounded by page count AND its own byte budget
        # (whichever is hit first) -- entirely separate from the core's own max_bytes below.
        overlap_images: list = []
        overlap_bytes_used = 0
        i = core_start - 1
        while i >= start_cursor and len(overlap_images) < overlap_pages:
            candidate = batch_images[i]
            candidate_bytes = storage.size(candidate["bucket_key"])
            if overlap_images and overlap_bytes_used + candidate_bytes > overlap_bytes:
                break
            overlap_images.insert(0, candidate)
            overlap_bytes_used += candidate_bytes
            i -= 1
        overlap_start = core_start - len(overlap_images)
        core_budget = max(1, max_pages - len(overlap_images))

        images = list(overlap_images)
        window_bytes = 0  # the core's own budget, independent of overlap_bytes_used above
        core_end = core_start
        core_count = 0
        while core_end < len(batch_images) and core_count < core_budget:
            candidate = batch_images[core_end]
            candidate_bytes = storage.size(candidate["bucket_key"])
            if images and window_bytes + candidate_bytes > max_bytes:
                break
            images.append(candidate)
            window_bytes += candidate_bytes
            core_end += 1
            core_count += 1
        if core_end == core_start:
            # A single fresh image alone exceeds the byte budget -- still force it through rather
            # than leaving the cursor stuck with no progress (see docstring).
            images.append(batch_images[core_end])
            core_end += 1

        windows.append({"images": images, "overlap_start": overlap_start, "core_start": core_start, "core_end": core_end})
        core_start = core_end
    return windows


def _reconcile_window_results(
    window: dict, flags: list[bool], dup_flags: list[bool],
    proposals: dict[int, bool], duplicates: dict[int, bool], batch_id: str,
) -> None:
    """Commits one window's result into the shared `proposals`/`duplicates` dicts."""
    for img, flag, is_dup in zip(window["images"], flags, dup_flags):
        existing = proposals.get(img["sequence_index"])
        if existing is not None and bool(flag) != existing:
            logger.warning(
                "batch %s: a later window revised page (sequence_index %s) from %s to %s after "
                "seeing more context -- worth a second look on the split-review screen",
                batch_id, img["sequence_index"], existing, bool(flag),
            )
        proposals[img["sequence_index"]] = bool(flag)
        duplicates[img["sequence_index"]] = bool(is_dup)


def run_propose_split(batch_id: str, settings: Settings) -> dict[int, bool]:
    """Asks Qwen where document boundaries fall across the batch, writing the proposal onto
    `new_batch_images`."""
    with db.connect(settings.db_path) as conn:
        refused = spend.cap_message(conn, settings.max_total_cost, getattr(settings, "monthly_cap", None))
        batch = db.get_batch(conn, batch_id)
        batch_images = db.get_batch_images(conn, batch_id)
        # The split prompt of the tab the batch is filed in, or the default before it has a folder.
        compiled = profiles.compiled(conn, profiles.version_for_folder(conn, batch["folder_id"])
                                     if batch is not None else profiles.default_version_id(conn))
    if not batch_images or batch is None:
        raise ValueError(f"batch {batch_id!r} not found or has no images")

    if refused:
        raise RuntimeError(f"{refused}; not proposing a split")

    # Every size and every payload in this run goes through the same view of a page: what the model
    # receives.
    storage = ModelPageBytes(BucketStorage(settings.bucket_uri))
    proposals: dict[int, bool] = {}
    duplicates: dict[int, bool] = {}
    # Resumes from the batch's own already-billed cost, not 0 -- otherwise persisting this run's
    # total_split_cost (see below) would overwrite, not add to, whatever an earlier failed run
    # already spent and recorded.
    total_split_cost = batch["split_cost_estimate"] or 0.0
    call_count = 0

    # Local, per-run copies -- shrunk on a 413 (see below), never mutating the module constants
    # themselves.
    effective_max_pages = MAX_SPLIT_WINDOW_PAGES
    effective_max_bytes = MAX_SPLIT_WINDOW_BYTES

    cursor = batch["split_proposal_cursor"] or 0
    # The whole remaining sequence is planned ONCE per "epoch" (a fresh start, or right after a 413
    # shrink changes the budget) -- not once per wave -- and consecutive waves are just slices of
    # that same plan.
    windows: list[dict] | None = None
    wave_start = 0
    while cursor < len(batch_images):
        with db.connect(settings.db_path) as conn:
            # The reviewer can confirm a partial proposal from the split-review screen while this
            # is still running, which is legitimate -- they can see what they are confirming.
            if not db.batch_is_pending(conn, batch_id):
                logger.info(
                    "batch %s was confirmed while its split was being proposed -- stopping with "
                    "%d page(s) unexamined", batch_id, len(batch_images) - cursor,
                )
                break
            current_total = db.get_total_cost(conn)
        if current_total >= settings.max_total_cost:
            logger.warning(
                "stopping: total OpenRouter spend $%.4f has reached the $%.2f cap (%d page(s) left unexamined)",
                current_total, settings.max_total_cost, len(batch_images) - cursor,
            )
            break

        if windows is None:
            # Overlap is capped at half of whatever the *current* (possibly already-shrunk) page
            # budget is, so a window shrunk after a 413 doesn't spend most of its now-smaller
            # budget on look-back context alone, leaving little or no room for real progress.
            effective_overlap = min(SPLIT_WINDOW_OVERLAP_PAGES, effective_max_pages // 2)
            windows = _plan_split_windows(
                batch_images, cursor, storage, effective_max_pages, effective_max_bytes, effective_overlap
            )
            wave_start = 0

        wave = windows[wave_start : wave_start + MAX_CONCURRENT_SPLIT_CALLS]

        results: dict[int, tuple[str, object]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as executor:
            future_to_index = {
                executor.submit(contextvars.copy_context().run, _propose_split_window, w["images"], storage,
                                settings, compiled): i
                for i, w in enumerate(wave)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                i = future_to_index[future]
                try:
                    results[i] = ("ok", future.result())
                except RuntimeError as exc:
                    results[i] = ("413" if "413" in str(exc) else "error", exc)
                except Exception as exc:  # noqa: BLE001 -- surfaced the same way a sequential failure would be
                    results[i] = ("error", exc)

        # A single image alone rejected as too large has nothing left to shrink -- force-finalize
        # it as its own one-page document (no real Qwen answer for it) rather than blocking every
        # remaining page in the batch behind it, same fallback the old sequential version used.
        for i, w in enumerate(wave):
            if results[i][0] == "413" and len(w["images"]) == 1:
                logger.warning(
                    "batch %s: page (sequence_index %s) alone is still too large for OpenRouter's "
                    "gateway; force-finalizing it as its own one-page document",
                    batch_id, w["images"][0]["sequence_index"],
                )
                results[i] = ("ok", ([True], [False], 0.0))

        # Any remaining (multi-image) 413 means the current window size is too large for
        # OpenRouter's gateway -- shrink and replan the *whole remaining sequence* from the current
        # cursor (not just this wave): a smaller budget changes every not-yet-committed window's
        # boundaries, not only the oversized one.
        oversized = next((w for i, w in enumerate(wave) if results[i][0] == "413"), None)
        if oversized is not None:
            effective_max_pages = max(1, len(oversized["images"]) // 2)
            effective_max_bytes = max(1, sum(storage.size(img["bucket_key"]) for img in oversized["images"]) // 2)
            logger.warning(
                "batch %s: a %d-page window was rejected as too large by OpenRouter's gateway; "
                "shrinking to %d page(s)/~%d bytes per call and replanning from the current point",
                batch_id, len(oversized["images"]), effective_max_pages, effective_max_bytes,
            )
            windows = None  # force a fresh plan at the top of the loop, with the smaller budget
            continue

        # Commit in planned (wave) order, not completion order, stopping at the first failure -- a
        # later window's failure must not discard an earlier one's already-billed, already-correct
        # result (see this function's docstring).
        first_failure = None
        committed_through = cursor
        committed_count = 0
        for offset, w in enumerate(wave):
            status, payload = results[offset]
            if status != "ok":
                first_failure = payload
                break
            starts_new, is_duplicate, cost = payload
            total_split_cost += cost
            call_count += 1
            _reconcile_window_results(w, starts_new, is_duplicate, proposals, duplicates, batch_id)
            committed_through = w["core_end"]
            committed_count += 1

        if committed_through > cursor:
            # batch_images are contiguous by sequence_index, so the last committed one marks
            # exactly how far a resume can safely skip to without re-examining anything not yet
            # committed.
            committed_cursor = batch_images[committed_through - 1]["sequence_index"] + 1
            with db.connect(settings.db_path) as conn:
                db.set_batch_split_proposal(
                    conn, batch_id, proposals, cost_estimate=total_split_cost, duplicates=duplicates,
                    proposal_cursor=committed_cursor,
                )
            cursor = committed_through
            wave_start += committed_count

        if first_failure is not None:
            raise first_failure

    logger.info(
        "proposed split for batch %s (%d pages, %d Qwen call(s), %d duplicate scan(s) flagged, total cost=%s)",
        batch_id, len(batch_images), call_count, sum(duplicates.values()), total_split_cost,
    )
    return proposals


def _group_display_id(group: dict) -> str:
    return (group.get("display_id") or "").strip()


def confirm_batch_split(batch_id: str, groups: list[dict], settings: Settings) -> list[str]:
    """`groups` is `[{"display_id": str, "sequence_indices": [int, ...]}, ...]` -- the reviewer's
    finalized boundaries and names from the split-review screen. Returns the new documents'
    internal ids, in group order."""
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        # Confirmed once.
        if not db.batch_is_pending(conn, batch_id):
            raise ValueError(
                f"batch {batch_id!r} has already been confirmed -- reload the page to see the "
                f"documents it produced"
            )
        batch_images = {img["sequence_index"]: img for img in db.get_batch_images(conn, batch_id)}
        batch_row = db.get_batch(conn, batch_id)
        folder_id = db.row_text(batch_row, "folder_id") if batch_row is not None else ""
    if not batch_images:
        raise ValueError(f"batch {batch_id!r} not found or has no images")

    # Two documents cannot share a display id in one folder: nobody could tell them apart.
    named = [_group_display_id(g) for g in groups]
    repeated = sorted(name for name, n in Counter(named).items() if name and n > 1)
    if repeated:
        raise ValueError(
            f"two groups are both named {', '.join(repeated)}. A document id belongs to one "
            "document, so join those groups into one, or give each its own name.")
    for display_id in named:
        if not display_id:
            raise ValueError("every document group needs a name")
        docid.require_safe_id(display_id, "document name")

    with db.connect(settings.db_path) as conn:
        if not folder_id:
            # Nobody named the folder: its documents still need one, so they share "unfiled".
            folder_id = db.ensure_folder(conn, UNFILED_FOLDER)
            db.set_batch_folder_id(conn, batch_id, folder_id)
        taken = sorted(set(named) & db.display_ids_in_folder(conn, folder_id))
    if taken:
        raise ValueError(f"document id {taken[0]!r} is already in use in this folder")

    created_ids = []
    for group in groups:
        display_id = _group_display_id(group)
        with db.connect(settings.db_path) as conn:
            document_id = db.new_document_id(conn)
        sequence_indices = sorted(group["sequence_indices"])

        pages = []
        for new_index, seq in enumerate(sequence_indices):
            image = batch_images.get(seq)
            if image is None:
                raise ValueError(f"batch {batch_id!r} has no image at sequence_index {seq}")
            filename = Path(image["bucket_key"]).name
            bucket_key, bucket_uri = storage.copy(image["bucket_key"], folder_id, document_id,
                                                  filename)
            pages.append({"sequence_index": new_index, "bucket_key": bucket_key, "bucket_uri": bucket_uri})

        with db.connect(settings.db_path) as conn:
            db.create_new_document(conn, folder_id, display_id, batch_id, pages,
                                   document_id=document_id)
            db.assign_batch_images(conn, batch_id, {seq: document_id for seq in sequence_indices})
        created_ids.append(document_id)

    with db.connect(settings.db_path) as conn:
        db.mark_batch_confirmed(conn, batch_id)

    logger.info("confirmed batch %s into %d document(s): %s", batch_id, len(created_ids), created_ids)
    return created_ids


def resolve_intake_context(conn, document_ids: list[str]) -> tuple[str, list]:
    """Shared validation for both halves of the re-split flow: the documents must all exist and all
    come from the same intake batch, and that batch must still have their images."""
    if not document_ids:
        raise ValueError("select at least one document to fix")

    batch_ids = set()
    for document_id in document_ids:
        row = db.get_new_document(conn, document_id)
        if row is None:
            raise ValueError(f"document {document_id!r} not found")
        batch_ids.add(row["batch_id"])
    if len(batch_ids) > 1:
        raise ValueError(
            "these documents came from different scan batches and can't be re-split together"
        )

    images = db.get_batch_images_for_documents(conn, document_ids)
    if not images:
        # Pre-dates assign_batch_images, or the intake batch was removed -- either way the original
        # scans this rebuild depends on aren't there, and silently rebuilding from the documents'
        # own copies would quietly lose any page not currently in one of them.
        raise ValueError(
            "the original scans for these documents are no longer available, so their split can't be changed"
        )
    return batch_ids.pop(), images


def carry_intake_orientations(conn, images: list) -> dict[int, dict]:
    """Keep orientation attached to its scan, not its replaceable document ID/page number.

    Document copies retain the intake filename. Pair it with the owner so identically named
    scans in different documents cannot acquire each other's orientation. Work is linear in
    the selected pages, independent of how many other folders the database holds.
    """
    intake = {(image["assigned_document_id"], Path(image["bucket_key"]).name): image["sequence_index"]
              for image in images}
    carried = {}
    for document_id in dict.fromkeys(image["assigned_document_id"] for image in images):
        pages = {page["sequence_index"]: page for page in db.get_new_document_pages(conn, document_id)}
        for row in conn.execute(
            "SELECT sequence_index, rotation, mirrored, set_by, set_at FROM page_orientations "
            "WHERE workflow = 'new' AND document_id = ?", (document_id,)
        ):
            page = pages.get(row["sequence_index"])
            if page is not None:
                seq = intake.get((document_id, Path(page["bucket_key"]).name))
                if seq is not None:
                    carried[seq] = dict(row)
    return carried


def restore_intake_orientations(conn, document_id: str, sequence_indices: list[int], carried: dict) -> None:
    """Restore saved turns at the rebuilt pages' indexes, keeping their original attribution."""
    for new_index, seq in enumerate(sorted(sequence_indices)):
        if seq in carried:
            row = carried[seq]
            conn.execute(
                "INSERT INTO page_orientations "
                "(workflow, document_id, sequence_index, rotation, mirrored, set_by, set_at) "
                "VALUES ('new', ?, ?, ?, ?, ?, ?)",
                (document_id, new_index, row["rotation"], row["mirrored"], row["set_by"], row["set_at"]))


def collect_resplit_pages(document_ids: list[str], settings: Settings) -> dict:
    """Everything the re-split screen needs to open: the pooled intake pages of the selected
    documents, in scan order, each tagged with the document it currently belongs to and whether
    it currently begins one."""
    with db.connect(settings.db_path) as conn:
        batch_id, images = resolve_intake_context(conn, document_ids)

        display = {document_id: db.display_id_of(conn, document_id)
                   for document_id in dict.fromkeys(img["assigned_document_id"] for img in images)}
        folder_id = db.folder_of_document(conn, document_ids[0]) or ""
        folder_display_id = db.folder_display(conn, folder_id)[0]

    pages = []
    seen: set[str] = set()
    for image in images:
        current = image["assigned_document_id"]
        pages.append(
            {
                "sequence_index": image["sequence_index"],
                "original_filename": image["original_filename"],
                "current_document_id": current,
                "current_display_id": display.get(current, ""),
                # A page starts a document exactly when it's the first page of its current one --
                # so the screen opens showing today's grouping, and every change is the reviewer's.
                "starts_new_document": current not in seen,
                "image_url": f"/api/new/batches/{batch_id}/images/{image['sequence_index']}",
            }
        )
        seen.add(current)

    return {
        "batch_id": batch_id,
        "folder_id": folder_id,
        "folder_display_id": folder_display_id,
        "document_ids": list(document_ids),
        "display_ids": [display.get(document_id, "") for document_id in document_ids],
        "pages": pages,
    }


def resplit_documents(document_ids: list[str], groups: list[dict], settings: Settings) -> list[str]:
    """Redraws the boundaries across a set of already-confirmed (and usually already-drafted)
    documents: merging several into one, cutting one into several, or both at once."""
    storage = BucketStorage(settings.bucket_uri)
    with db.connect(settings.db_path) as conn:
        batch_id, images = resolve_intake_context(conn, document_ids)
        by_index = {img["sequence_index"]: img for img in images}
        folder_id = db.folder_of_document(conn, document_ids[0])

        available = set(by_index)
        requested: set[int] = set()
        for group in groups:
            new_id = _group_display_id(group)
            if not new_id:
                raise ValueError("every document needs a name")
            docid.require_safe_id(new_id, "document name")
            indices = group.get("sequence_indices") or []
            if not indices:
                raise ValueError(f"document {new_id!r} has no pages")
            for seq in indices:
                if seq in requested:
                    raise ValueError(f"page {seq} was put in more than one document")
                requested.add(seq)

        if requested != available:
            missing = sorted(available - requested)
            extra = sorted(requested - available)
            detail = f"missing page(s) {missing}" if missing else f"unknown page(s) {extra}"
            raise ValueError(f"the new split must use exactly the pages being fixed -- {detail}")

        # A new name may reuse one of the names being replaced (merging 54+55 into 54 is the common
        # case), but must not collide with any *other* document in the folder -- checked before
        # anything is deleted, so a bad name fails harmlessly instead of halfway through.
        replaced = {db.display_id_of(conn, document_id) for document_id in document_ids}
        in_use = db.display_ids_in_folder(conn, folder_id) - replaced
        names = [_group_display_id(group) for group in groups]
        if len(set(names)) != len(names):
            raise ValueError("two of the new documents have the same name")
        for new_id in names:
            if new_id in in_use:
                raise ValueError(f"document id {new_id!r} is already in use in this folder")

    created_ids = []
    with db.connect(settings.db_path) as conn:
        # Capture and rebuild under the same write lock, so a concurrent orientation save
        # cannot succeed between capture and deletion and then quietly be discarded.
        conn.execute("BEGIN IMMEDIATE")
        _, images = resolve_intake_context(conn, document_ids)
        by_index = {image["sequence_index"]: image for image in images}
        orientations = carry_intake_orientations(conn, images)
        missing = [image["bucket_key"] for image in images if not storage.exists(image["bucket_key"])]
        if missing:
            raise ValueError(f"cannot re-split: {len(missing)} intake scan(s) are missing, e.g. {missing[0]}")
        old_page_keys = {page["bucket_key"] for document_id in document_ids
                         for page in db.get_new_document_pages(conn, document_id) if page["bucket_key"]}

        rebuilt = []
        for group in groups:
            display_id = _group_display_id(group)
            new_id = db.new_document_id(conn)
            pages = []
            for new_index, seq in enumerate(sorted(group["sequence_indices"])):
                image = by_index[seq]
                filename = Path(image["bucket_key"]).name
                bucket_key, bucket_uri = storage.copy(image["bucket_key"], folder_id, new_id,
                                                      filename)
                pages.append({"sequence_index": new_index, "bucket_key": bucket_key, "bucket_uri": bucket_uri})
            rebuilt.append((group, display_id, new_id, pages))

        # Copies are ready before old rows change. A later copy failure therefore rolls SQLite
        # back without leaving existing rows pointing to files already removed from the bucket.
        for document_id in document_ids:
            db.delete_new_document(conn, document_id)

        for group, display_id, new_id, pages in rebuilt:
            db.create_new_document(conn, folder_id, display_id, batch_id, pages, document_id=new_id)
            restore_intake_orientations(conn, new_id, group["sequence_indices"], orientations)
            db.assign_batch_images(conn, batch_id, {seq: new_id for seq in group["sequence_indices"]})
            created_ids.append(new_id)

    storage.delete_keys(old_page_keys - {page["bucket_key"] for *_, pages in rebuilt for page in pages})
    for document_id in set(document_ids) - set(created_ids):
        storage.delete_document(folder_id, document_id)
        storage.delete_record(folder_id, document_id)

    logger.info(
        "re-split %d document(s) %s into %d: %s", len(document_ids), document_ids, len(created_ids), created_ids
    )
    return created_ids
