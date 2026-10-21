"""Rejoins documents a split cut apart, keeping the transcription and regenerating only the
metadata."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import collab, db, docid, fields, openrouter, profiles
from .config import Settings
from .new_document import document_title, refresh_output_record
from .split import carry_intake_orientations, resolve_intake_context, restore_intake_orientations
from .storage import BucketStorage

logger = logging.getLogger(__name__)

def _uncertain_words(row) -> list:
    try:
        value = json.loads(row["uncertain_words_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _merged_values(registry: list[dict], sources: list[dict], answer: dict) -> dict:
    """Page fields pooled from the sources; document fields from the model, lists also pooling the
    sources'."""
    out = {}
    for field in registry:
        key = field["key"]
        if field["scope"] == "page" or not field.get("enabled", True):
            out[key] = (fields.dedupe([v for values in sources for v in values.get(key) or []])
                        if fields.is_list(field) else next((v[key] for v in sources if v.get(key)), ""))
        elif fields.is_list(field):
            out[key] = fields.dedupe([*(v for values in sources for v in values.get(key) or []),
                                *(answer.get(key) or [])])
        else:
            out[key] = answer.get(key) or ""
    # A merge is where a mentioned name becomes the author or recipient, which have their own fields.
    if "names" in out:
        principals = {name.strip().lower() for key in ("author", "recipient")
                      for name in str(out.get(key) or "").split(";") if name.strip()}
        out["names"] = [n for n in out["names"] if n.strip().lower() not in principals]
    return out


def _document_text(row) -> str:
    """The best available transcription: a reviewer's corrected text where one exists, otherwise
    the model's."""
    reviewed = (row["reviewed_text"] or "").strip()
    return reviewed if reviewed else (row["text"] or "")


def plan_merge(document_ids: list[str], settings: Settings) -> dict:
    """Validates a proposed merge and describes what it will do, without changing anything."""
    if len(set(document_ids)) < 2:
        raise ValueError("select two or more documents to merge them")

    with db.connect(settings.db_path) as conn:
        batch_id, images = resolve_intake_context(conn, document_ids)
        rows = {document_id: db.get_new_document(conn, document_id) for document_id in document_ids}

        # Scan order, not selection order: the reviewer may have ticked the boxes in any order, and
        # a merged document's pages have to read in the order they were scanned.
        ordered_ids: list[str] = []
        for image in images:
            assigned = image["assigned_document_id"]
            if assigned in rows and assigned not in ordered_ids:
                ordered_ids.append(assigned)

        folder_id = rows[ordered_ids[0]]["folder_id"]
        if any(rows[d]["folder_id"] != folder_id for d in ordered_ids):
            raise ValueError("all the documents being merged must be in the same folder")

        target_id = ordered_ids[0]
        folder_display_id = db.folder_display(conn, folder_id)[0]
        scheme = db.folder_id_scheme(conn, folder_id)
        merged_away = {rows[d]["display_id"] for d in ordered_ids[1:]}
        remaining = sorted(d for d in db.display_ids_in_folder(conn, folder_id) if d not in merged_away)

    return {
        "batch_id": batch_id,
        "folder_id": folder_id,
        "document_ids": ordered_ids,
        "display_ids": [rows[d]["display_id"] for d in ordered_ids],
        "target_document_id": target_id,
        "target_display_id": rows[target_id]["display_id"],
        "page_count": len(images),
        # A merge regenerates metadata from the joined transcription, so every source has to
        # actually have one -- an undrafted document has no text to join.
        "all_drafted": all(rows[d]["status"] == "drafted" for d in ordered_ids),
        # Display ids only: what each remaining document will be called once the gap closes.
        "renumber": docid.renumber_plan(folder_display_id, remaining, scheme=scheme),
    }


def merge_documents(document_ids: list[str], settings: Settings,
                    author_id: int | None = None) -> dict:
    """Joins several already-drafted documents into one, keeping their transcription and
    regenerating only the document-level metadata, then closes the DocID gap the merge leaves
    behind in the folder."""
    plan = plan_merge(document_ids, settings)
    if not plan["all_drafted"]:
        raise ValueError(
            "every document being merged has to be drafted first -- merging keeps their "
            "transcriptions, so there has to be one to keep"
        )

    ordered_ids = plan["document_ids"]
    target_id = plan["target_document_id"]
    batch_id = plan["batch_id"]

    with db.connect(settings.db_path) as conn:
        total_cost = db.get_total_cost(conn)
        if total_cost >= settings.max_total_cost:
            raise RuntimeError(
                f"total OpenRouter spend ${total_cost:.4f} has reached the "
                f"${settings.max_total_cost:.2f} cap; not merging"
            )
        sources = [db.get_new_document(conn, d) for d in ordered_ids]
        _, images = resolve_intake_context(conn, ordered_ids)

        # The merged document keeps the first source's pinned version, so its fields read the same way.
        version_id = profiles.document_version_id(conn, sources[0])
        compiled = profiles.compiled(conn, version_id)
        source_values = [fields.effective(row, compiled.registry) for row in sources]
        version_label = profiles.label(conn, version_id)

    joined_text = "\n\n".join(text for text in (_document_text(row) for row in sources) if text)
    uncertain = fields.dedupe([w for row in sources for w in _uncertain_words(row)])

    # The one model call a merge makes: text in, document-scope fields out, no images.
    metadata = openrouter.call_openrouter(
        openrouter.build_merge_metadata_messages(joined_text, compiled),
        settings,
        schema=compiled.merge_schema,
        extract_result=openrouter.merge_extractor(compiled.registry),
    )
    values = _merged_values(compiled.registry, source_values, metadata["fields"])

    storage = BucketStorage(settings.bucket_uri)
    by_index = {image["sequence_index"]: image for image in images}

    # Checked before anything is removed, because a missing scan used to surface after the delete.
    missing = [image["bucket_key"] for image in images if not storage.exists(image["bucket_key"])]
    if missing:
        raise ValueError(f"cannot merge: {len(missing)} intake scan(s) are missing, e.g. {missing[0]}")

    folder_id = plan["folder_id"]
    target_display_id = plan["target_display_id"]
    title = document_title(compiled, target_display_id, values)
    result = {"text": joined_text, "uncertain_words": uncertain, "fields": values}
    # One transaction from the first delete to the restored threads, so a failure rolls the sources
    # and their comments back rather than losing them.
    with db.connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Not init_collab: its schema needs users(id), which a plain pipeline database lacks.
        threads = collab.carry_threads(conn, ordered_ids, "new")
        orientations = carry_intake_orientations(conn, images)
        old_page_keys = {page["bucket_key"] for document_id in ordered_ids
                         for page in db.get_new_document_pages(conn, document_id) if page["bucket_key"]}

        pages = []
        for new_index, seq in enumerate(sorted(by_index)):
            image = by_index[seq]
            filename = Path(image["bucket_key"]).name
            bucket_key, bucket_uri = storage.copy(image["bucket_key"], folder_id, target_id, filename)
            pages.append({"sequence_index": new_index, "bucket_key": bucket_key, "bucket_uri": bucket_uri})

        # Copy before removing old rows or files. SQLite can roll back this transaction, but a
        # bucket delete cannot; a failed copy must leave the original document readable.
        for document_id in ordered_ids:
            db.delete_new_document(conn, document_id)

        output_record = {
            "document_id": target_id,
            "display_id": target_display_id,
            "folder_id": folder_id,
            "source": {"page_refs": [p["bucket_key"] for p in pages], "merged_from": list(ordered_ids)},
            "metadata": {"title": title},
            "transcription": {"text": joined_text, "uncertain_words": uncertain},
            "fields": values,
            "pipeline_info": {
                "method": "merge_documents",
                "model": settings.openrouter_model,
                "provider": "openrouter",
                "prompt_version_id": version_id,
                "prompt_version": version_label,
                "run_date": datetime.now(timezone.utc).isoformat(),
                "cost_estimate": metadata.get("cost"),
            },
            "review": {"status": "unreviewed", "reviewer": "", "reviewed_text": "", "editors_notes": ""},
        }

        db.create_new_document(conn, folder_id, target_display_id, batch_id, pages,
                               document_id=target_id)
        restore_intake_orientations(conn, target_id, list(by_index), orientations)
        db.assign_batch_images(conn, batch_id, {seq: target_id for seq in by_index})
        db.upsert_new_document_ok(
            conn,
            target_id,
            model=settings.openrouter_model,
            provider="openrouter",
            prompt_version_id=version_id,
            result=result,
            title=title,
            ocr_draft_text=None,
            output_json=output_record,
            cost_estimate=metadata.get("cost"),
        )
        collab.restore_threads(conn, target_id, "new", threads)
        # Last, so it reads as the most recent thing that happened to this document -- which it is.
        collab.record_merge_note(conn, target_id, "new", ordered_ids, author_id)

    storage.delete_keys(old_page_keys - {page["bucket_key"] for page in pages})
    for document_id in ordered_ids:
        if document_id != target_id:
            storage.delete_document(folder_id, document_id)
            storage.delete_record(folder_id, document_id)
    renumbered = renumber_folder(folder_id, settings)

    with db.connect(settings.db_path) as conn:
        refresh_output_record(conn, target_id, storage)

    logger.info(
        "merged %d document(s) %s into %s (%d pages, cost=%s); renumbered %d document(s)",
        len(ordered_ids), ordered_ids, target_id, len(pages), metadata.get("cost"), len(renumbered),
    )
    return {
        "document_id": target_id,
        "display_id": target_display_id,
        "replaced": list(ordered_ids),
        "renumbered": renumbered,
        "cost": metadata.get("cost"),
        "page_count": len(pages),
    }


def renumber_folder(folder_id: str, settings: Settings) -> dict[str, str]:
    """Closes any gaps in a folder's display-id sequence, returning `{document_id: new_display_id}`
    for the documents whose display id changed. Their records are rewritten; no file moves."""
    if not folder_id:
        return {}
    with db.connect(settings.db_path) as conn:
        documents = db.get_new_documents_in_folder(conn, folder_id)
        by_display = {row["display_id"]: row["document_id"] for row in documents}
        folder_display_id = db.folder_display(conn, folder_id)[0]
        display_plan = docid.renumber_plan(folder_display_id, sorted(by_display),
                                           scheme=db.folder_id_scheme(conn, folder_id))
        plan = {by_display[old]: new for old, new in display_plan.items()}
        db.renumber_new_documents(conn, plan)
        storage = BucketStorage(settings.bucket_uri)
        for document_id in plan:
            refresh_output_record(conn, document_id, storage)
    if plan:
        logger.info("renumbered %d document(s) in folder %s: %s", len(plan), folder_id, plan)
    return plan
