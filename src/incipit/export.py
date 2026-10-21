"""Builds the CSV half of a folder's export deliverable from the reviewed documents."""

from __future__ import annotations

import csv
import io
import posixpath

from . import db, fields, profiles
from .config import Settings
from .csv_safety import safe_row

def effective_rows(conn, rows) -> list[dict]:
    """Export rows with their effective field values, each read with its own pinned registry."""
    out = []
    for row in rows:
        registry, values = db.field_values(conn, row)
        out.append({"document_id": row["document_id"], "display_id": row["display_id"] or "",
                    "title": row["title"] or "", "text": row["text"] or "",
                    "editors_notes": row["editors_notes"] or "", "reviewer": row["reviewer"] or "",
                    "prompt_version": profiles.label(conn, profiles.document_version_id(conn, row)),
                    "fields": values, "_registry": registry})
    return out


def _columns(registries: list[list[dict]]) -> list[str]:
    """One header for a folder whose documents may be pinned to different versions: their columns, in order."""
    middle: list[str] = []
    for registry in registries:
        for column in fields.export_columns(registry)[len(fields.LEADING_COLUMNS):-len(fields.TRAILING_COLUMNS)]:
            if column not in middle:
                middle.append(column)
    return [*fields.LEADING_COLUMNS, *middle, *fields.TRAILING_COLUMNS]


def _new_image_filenames(conn, document_id: str) -> str:
    """Semicolon-joined image filenames for the CSV's "Image Filename(s)" column."""
    pages = db.get_new_document_pages(conn, document_id)
    filenames = [posixpath.basename(p["bucket_key"]) for p in pages if p["bucket_key"]]
    return "; ".join(filenames)


def export_new_csv(
    settings: Settings,
    folder_id: str | None = None,
    include_unapproved: bool = False,
) -> tuple[int, bytes, list[str]]:
    """One CSV row per document; the columns follow each document's pinned registry."""
    document_ids = []
    buf = io.StringIO()
    with db.connect(settings.db_path) as conn:
        rows = effective_rows(conn, db.get_new_documents_for_export(
            conn, folder_id=folder_id, include_unapproved=include_unapproved))
        columns = _columns([row["_registry"] for row in rows] or [profiles.registry_for(conn, {})])
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        writer.writerow(columns)
        for row in rows:
            document_ids.append(row["document_id"])
            registry = row["_registry"]
            by_column = dict(zip(
                [f["export_column"] for f in fields.enabled(registry) if f["export_column"]],
                fields.export_values(registry, row["fields"])))
            by_column.update({
                "Document ID": row["display_id"] or row["document_id"], "Title": row["title"],
                "Transcription": row["text"],
                "Image Filename(s)": _new_image_filenames(conn, row["document_id"]),
                "Editors Notes": row["editors_notes"], "Submitted by": row["reviewer"]})
            writer.writerow(safe_row([by_column.get(column, "") for column in columns]))

    return len(rows), buf.getvalue().encode("utf-8"), document_ids
