"""A folder's display ids run without a gap: AAA, AAB, AAC, or -001, -002, -003."""

from __future__ import annotations

import logging
import threading

from . import db, docid
from .config import Settings
from .merge import renumber_folder

logger = logging.getLogger(__name__)

# How often the site looks.
CHECK_INTERVAL_MINUTES = 15


def find(db_path: str) -> dict[str, dict[str, str]]:
    """{folder_id: {old_display_id: new_display_id}} for every folder with a gap in its sequence."""
    plans = {}
    with db.connect(db_path) as conn:
        for folder in conn.execute("SELECT folder_id, display_id FROM folders").fetchall():
            ids = sorted(db.display_ids_in_folder(conn, folder["folder_id"]))
            plan = docid.renumber_plan(folder["display_id"], ids,
                                       scheme=db.folder_id_scheme(conn, folder["folder_id"]))
            if plan:
                plans[folder["folder_id"]] = plan
    return plans


def close(settings: Settings) -> dict[str, str]:
    """Closes every gap, returning the whole {document_id: new_display_id} that was applied."""
    plans = find(settings.db_path)
    if not plans:
        return {}

    applied: dict[str, str] = {}
    for folder_id in sorted(plans):
        plan = renumber_folder(folder_id, settings)
        if not plan:
            continue
        applied.update(plan)
        logger.info("closed %d id gap(s) in folder %s: %s", len(plan), folder_id, plan)
    return applied


def watch(settings: Settings, interval_minutes: float = CHECK_INTERVAL_MINUTES,
          stop: threading.Event | None = None) -> threading.Event:
    """Closes gaps on a timer, on a daemon thread."""
    stop = stop or threading.Event()

    def forever() -> None:
        while not stop.wait(interval_minutes * 60):
            try:
                moved = close(settings)
                if moved:
                    logger.info("the display-id watcher closed %d gap(s)", len(moved))
            except Exception as exc:  # noqa: BLE001 - never fatal to the server
                logger.warning("the display-id watcher failed this round: %s", exc)

    threading.Thread(target=forever, name="incipit-docid-gaps", daemon=True).start()
    logger.info("display-id gap watcher started: every %g minutes", interval_minutes)
    return stop
