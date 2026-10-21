"""Money this process has committed to but has not yet recorded.

The all-time cap is read from the database per document, which is exact only while one draft runs
at a time. Drafting several folders at once means several runs read the same total and each decide
there is room -- so each one holds back a reservation for the document it is about to start, and
the check counts those reservations as though they were already spent.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager

_lock = threading.Lock()
_held: dict[str, float] = {}

# A document's cost is only known once it has been drafted, so this is the headroom kept back for
# one that is in flight. Comfortably above the most any single document has cost here (the largest
# of 154 drafts was $0.011), because reserving too little is what lets the cap slip.
PER_DOCUMENT_RESERVE = 0.02


def reserved() -> float:
    """What is currently held back across every run."""
    with _lock:
        return sum(_held.values())


def cap_message(conn, max_total_cost: float, monthly_cap: float | None = None, held: float = 0.0) -> str | None:
    """Which cap ledger spend (plus what is held back) has reached, in words, or None while there is room."""
    from . import ledger
    total = ledger.spent(conn) + held
    if total >= max_total_cost:
        return f"the all-time cap of ${max_total_cost:.2f} is reached (${total - held:.2f} spent)"
    if monthly_cap is not None:
        month = ledger.spent(conn, ledger.month_start()) + held
        if month >= monthly_cap:
            return f"the monthly cap of ${monthly_cap:.2f} is reached (${month - held:.2f} spent this month)"
    return None


@contextmanager
def room_under_caps(conn, max_total_cost: float, monthly_cap: float | None = None,
                    amount: float = PER_DOCUMENT_RESERVE):
    """Reserves headroom for one document under both caps; yields None when granted, else the cap's message."""
    key = uuid.uuid4().hex[:8]
    with _lock:
        message = cap_message(conn, max_total_cost, monthly_cap, held=sum(_held.values()))
        if message is None:
            _held[key] = amount
    try:
        yield message
    finally:
        if message is None:
            with _lock:
                _held.pop(key, None)


@contextmanager
def room_for_one(spent, cap: float, amount: float = PER_DOCUMENT_RESERVE):
    """Reserve headroom for one document, yielding False when the cap has no room left.

    The test is recorded spend plus what *other* runs are holding, and a run's own reserve is not
    counted against itself. A document's cost cannot be known before it is drafted, so the cap has
    always tolerated the one in flight carrying it over -- that is unchanged, and a single run
    behaves exactly as it did before reservations existed. What the reserve stops is a *second*
    run starting on the strength of a total that does not yet include the first one's document.

    `spent` is read inside the lock rather than passed in, so two runs cannot both act on the same
    total; it is the caller's `db.get_total_cost`.
    """
    key = uuid.uuid4().hex[:8]
    with _lock:
        granted = spent() + sum(_held.values()) < cap
        if granted:
            _held[key] = amount
    try:
        yield granted
    finally:
        if granted:
            with _lock:
                _held.pop(key, None)


def clear() -> None:
    """Test helper -- drops every reservation."""
    with _lock:
        _held.clear()
