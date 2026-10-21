"""Ephemeral, process-local presence for the admin Team view.

Presence is deliberately not editorial data: heartbeats must not churn SQLite, trigger backups, or
survive a server restart. The production review server is one process, so a small locked map is the
right-sized store for "active now" and recent activity.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import accounts

ACTIVE_SECONDS = 60
RECENT_SECONDS = 10 * 60


class Tracker:
    """Last browser heartbeat for each signed-in user."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._people: dict[int, dict] = {}

    def touch(self, user, *, document_id: str = "", workflow: str = "") -> None:
        now = self._clock()
        document_id = (document_id or "").strip()[:200]
        workflow = workflow if workflow in accounts.WORKFLOWS else ""
        with self._lock:
            self._people[int(user["id"])] = {
                "user_id": int(user["id"]),
                "name": user["display_name"] or user["username"],
                "role": user["role"],
                "document_id": document_id,
                "workflow": workflow if document_id else "",
                "seen_monotonic": now,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }

    def snapshot(self) -> list[dict]:
        """Active and briefly-recent people, active first and then by most recent heartbeat."""
        now = self._clock()
        with self._lock:
            # Prune here so an abandoned browser costs no permanent memory.
            stale = [user_id for user_id, item in self._people.items()
                     if now - item["seen_monotonic"] > RECENT_SECONDS]
            for user_id in stale:
                self._people.pop(user_id, None)
            rows = [dict(item) for item in self._people.values()]

        for row in rows:
            age = max(0, round(now - row.pop("seen_monotonic")))
            row["active"] = age <= ACTIVE_SECONDS
            row["seconds_ago"] = age
        rows.sort(key=lambda row: (not row["active"], row["seconds_ago"], row["name"].lower()))
        return rows
