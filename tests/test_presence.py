"""The lightweight active-user registry behind the Team tab."""

from incipit import presence


def user(user_id=1, name="John", role="reviewer"):
    return {"id": user_id, "username": name.lower(), "display_name": name, "role": role}


def test_a_recent_heartbeat_is_active_and_names_the_document():
    now = [100.0]
    tracker = presence.Tracker(clock=lambda: now[0])

    tracker.touch(user(), document_id="E2002AAA", workflow="new")

    assert tracker.snapshot() == [{
        "user_id": 1, "name": "John", "role": "reviewer",
        "document_id": "E2002AAA", "workflow": "new",
        "last_seen": tracker.snapshot()[0]["last_seen"],
        "active": True, "seconds_ago": 0,
    }]


def test_presence_becomes_recent_then_expires_without_writes_or_cleanup_jobs():
    now = [100.0]
    tracker = presence.Tracker(clock=lambda: now[0])
    tracker.touch(user())

    now[0] += presence.ACTIVE_SECONDS + 1
    assert tracker.snapshot()[0]["active"] is False

    now[0] += presence.RECENT_SECONDS
    assert tracker.snapshot() == []


