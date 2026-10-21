"""Orchestrates the "Add New Document" workflow's draft stage: confirmed split group -> the new-
document prompt (transcription + indices + full metadata) -> structured JSON output +
`new_documents` row."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import config, db, fields, ledger, openrouter, profiles, spend
from .config import DEFAULT_MAX_TOTAL_COST, Settings
from .modeling import call_image_model, has_fallback_model, with_model_retries
from .storage import BucketStorage

logger = logging.getLogger(__name__)

@dataclass
class NewDocumentOptions:
    max_total_cost: float = DEFAULT_MAX_TOTAL_COST
    limit: int | None = None
    monthly_cap: float | None = None


def refresh_output_record(conn, document_id: str, storage: BucketStorage):
    """Rewrites a document's JSON so it says what the reviewer decided, not only what the model
    drafted."""
    try:
        row = db.get_new_document_for_review(conn, document_id)
        if row is None:
            return None
        record = json.loads(row["output_json"] or "{}")
        # The record leads with both identities: the permanent one, and what people call it now.
        folder_id = row["folder_id"]
        record["document_id"] = document_id
        record["display_id"] = row["display_id"] or ""
        record["folder_id"] = folder_id or ""
        record["folder_display_id"] = db.folder_display(conn, folder_id)[0]
        _, values = db.field_values(conn, row)
        version_id = profiles.document_version_id(conn, row)
        # The record leads with what is true now.
        record.setdefault("transcription", {})
        record["transcription"]["text"] = db.effective_text(row)
        record["fields"] = values
        record["metadata"] = {"title": db.row_text(row, "title_override") or db.row_text(row, "reviewed_title")
                              or db.row_text(row, "ai_title")}
        record["prompt"] = {"version_id": version_id, "version": profiles.label(conn, version_id)}
        # What the model produced, kept whatever the reviewer has done since.
        record["ai_draft"] = {"text": db.row_text(row, "text"), "title": db.row_text(row, "ai_title"),
                              "fields": fields.drafted(row)}
        record["review"] = {
            "status": row["review_status"] or "unreviewed",
            "reviewer": db.signed_reviewer(row),
            "reviewed_text": row["reviewed_text"] or "",
            "editors_notes": row["editors_notes"] or "",
            "reviewed_at": row["reviewed_at"] or "",
            "title": db.row_text(row, "title_override") or db.row_text(row, "reviewed_title"),
            "fields": fields.reviewed(row),
            # `status` above is the shared column, which is the administrator's verdict: a reviewer
            # accepting a document deliberately does not write it (see _status_to_store).
            "by_role": {role: db.get_document_review(conn, document_id, "new", role) or ""
                        for role in ("reviewer", "admin")},
        }
        written = storage.write_record(
            folder_id, document_id, json.dumps(record, indent=2, ensure_ascii=False))
        return written
    except Exception as exc:  # noqa: BLE001 - see docstring: never fails the reviewer's save
        logger.error("could not refresh the stored record for %s: %s: %s",
                     document_id, type(exc).__name__, exc)
        return None


def document_title(compiled, display_id: str, values: dict) -> str:
    """The title a version's template gives these field values."""
    template = compiled.answers.get("title_template") or fields.DEFAULT_TITLE_TEMPLATE
    return fields.render_title(template, display_id, values, compiled.styles, compiled.registry)


def title_for_row(conn, row, values: dict | None = None, display_id: str | None = None) -> str:
    """The derived title of a stored document, from its own version and (by default) its effective fields."""
    compiled = profiles.compiled(conn, profiles.document_version_id(conn, row))
    if values is None:
        values = fields.effective(row, compiled.registry)
    return document_title(compiled, row["display_id"] if display_id is None else display_id, values)


def retitle(conn, document_id: str, storage: BucketStorage | None = None) -> None:
    """Re-derives the cached titles after the display id or the pinned version changed; an override stays."""
    row = db.get_new_document(conn, document_id)
    if row is None:
        return
    compiled = profiles.compiled(conn, profiles.document_version_id(conn, row))
    ai_title = document_title(compiled, row["display_id"] or "", fields.drafted(row))
    reviewed_title = (title_for_row(conn, row) if row["reviewed_title"] is not None else None)
    conn.execute("UPDATE new_documents SET ai_title = ?, reviewed_title = ? WHERE document_id = ?",
                 (ai_title, reviewed_title, document_id))
    if storage is not None:
        refresh_output_record(conn, document_id, storage)


def _draft_with_retries(document_id: str, pages, storage: BucketStorage, settings: Settings, compiled):
    """One document's VLM draft under `compiled`, retried through anything transient, then offered
    to the fallback model."""
    def draft(on: Settings):
        return call_image_model(
            pages,
            storage,
            on,
            build_messages=lambda images: openrouter.build_new_document_messages(images, compiled),
            schema=compiled.document_schema,
            extractor=openrouter.document_extractor(compiled.registry),
            merge=openrouter.document_chunk_merger(compiled.registry),
        )

    return with_model_retries(
        f"drafting {document_id}",
        lambda: draft(settings),
        last_resort=((lambda: _on_fallback_model(document_id, settings, draft))
                     if has_fallback_model(settings) else None),
    )


def _on_fallback_model(document_id: str, settings: Settings, draft):
    """The same scans, the other Qwen."""
    fallback = settings.openrouter_fallback_model
    logger.warning("drafting %s on the fallback model %s", document_id, fallback)
    result = draft(replace(settings, openrouter_model=fallback))
    result["model"] = fallback
    return result


def transcribe_new_document(
    document_id: str, settings: Settings, storage: BucketStorage, conn, options: NewDocumentOptions
) -> None:
    # Measured around the work itself, not inferred from run_date -- drafting runs in batches a
    # reviewer kicks off by hand, so the span between run_dates is mostly time nobody waited on.
    started = time.monotonic()
    try:
        pages = db.get_new_document_pages(conn, document_id)
        if not pages:
            raise ValueError(f"no pages found for {document_id}; run split-confirm first")

        document = db.get_new_document(conn, document_id)
        folder_id, display_id = document["folder_id"], document["display_id"] or ""
        # Pinned before the call: the document says what it was told, whatever the profile does next.
        version_id = profiles.version_for_folder(conn, folder_id)
        compiled = profiles.compiled(conn, version_id)
        with ledger.context(prompt_version_id=version_id):
            result = _draft_with_retries(document_id, pages, storage, settings, compiled)
        title = document_title(compiled, display_id, result["fields"])
        model = result.get("model") or settings.openrouter_model

        output_record = {
            "document_id": document_id,
            "display_id": display_id,
            "folder_id": folder_id,
            "folder_display_id": db.folder_display(conn, folder_id)[0],
            "source": {"page_refs": [p["bucket_key"] for p in pages]},
            "metadata": {"title": title},
            "transcription": {"text": result["text"], "uncertain_words": result["uncertain_words"]},
            "fields": result["fields"],
            "pipeline_info": {
                "method": "new_document",
                "path": config.PATH_SINGLE_PASS,
                # The model that actually produced this draft: a document the primary model could
                # not do is drafted by the fallback, and the row must not claim otherwise.
                "model": model,
                "provider": "openrouter",
                "metadata_source": "model",
                "prompt_version_id": version_id,
                "prompt_version": profiles.label(conn, version_id),
                "run_date": datetime.now(timezone.utc).isoformat(),
                "cost_estimate": result.get("cost"),
            },
            "review": {"status": "unreviewed", "reviewer": "", "reviewed_text": "", "editors_notes": ""},
        }

        storage.write_record(
            folder_id, document_id, json.dumps(output_record, indent=2, ensure_ascii=False))

        db.upsert_new_document_ok(
            conn,
            document_id,
            model=model,
            provider="openrouter",
            prompt_version_id=version_id,
            result=result,
            title=title,
            output_json=output_record,
            cost_estimate=result.get("cost"),
            duration_seconds=time.monotonic() - started,
        )
        logger.info("drafted new document %s (%d chars, cost=%s)", document_id, len(result["text"]), result.get("cost"))

    except openrouter.AccountError:
        # Not this document's fault and not survivable: the key or the balance is the problem, and
        # every remaining document would fail identically.
        raise
    except Exception as exc:  # noqa: BLE001 - isolate per-document failures
        logger.exception("failed to draft new document %s", document_id)
        db.upsert_new_document_error(conn, document_id, str(exc))


def run_transcribe_new_documents(
    settings: Settings, options: NewDocumentOptions | None = None,
    document_ids: list[str] | None = None, progress=None
) -> dict:
    """Drafts confirmed-but-undrafted document groups, called right after a split is confirmed;
    says which cap stopped it, if one did."""
    stopped_by_cap = None
    options = options or NewDocumentOptions()
    with db.connect(settings.db_path) as conn:
        document_ids = document_ids if document_ids is not None else db.get_new_pending_document_ids(conn)
        if options.limit is not None:
            document_ids = document_ids[: options.limit]
        storage = BucketStorage(settings.bucket_uri)
        for index, document_id in enumerate(document_ids):
            # What the status bar counts down.
            if progress is not None:
                progress(len(document_ids) - index, document_id)
            # A reservation rather than a bare comparison: several folders can be drafted at
            # once, and each run would otherwise read the same total and each conclude there was
            # room for one more. It is held until this document's real cost has been committed.
            with spend.room_under_caps(conn, options.max_total_cost, options.monthly_cap) as refused:
                if refused:
                    stopped_by_cap = refused
                    logger.warning("stopping: %s, counting what the runs alongside this one have "
                                   "reserved (%d new document(s) left undrafted this run)",
                                   refused, len(document_ids) - index)
                    break
                try:
                    with ledger.context(document_id=document_id):
                        transcribe_new_document(document_id, settings, storage, conn, options)
                except openrouter.AccountError as exc:
                    # Everything drafted so far is already committed below, so stopping here loses
                    # nothing -- the reviewer fixes the key or tops up the balance and clicks Draft
                    # again, and the run picks up from exactly where it stopped.
                    conn.commit()
                    logger.error("stopping the run: %s", exc)
                    raise
                # Commit after every document, same reasoning as run_transcribe/run_extract: a
                # batch can take minutes (OpenRouter calls per document), and holding one write
                # transaction open for all of them locks out concurrent writers for the whole
                # batch. Inside the reservation, so it is released against a recorded cost.
                conn.commit()
    return {"stopped_by_cap": stopped_by_cap}
