"""Confirming a split while Qwen is still proposing it must not resurrect the batch."""
import pytest

from incipit import db, split
from incipit.config import Settings
from conftest import insert_rows


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
    )


@pytest.fixture
def batch(settings):
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id, status) "
            "VALUES ('B1', 'now', 3, 'E2005', 'uploaded')"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO new_batch_images (batch_id, sequence_index, bucket_key, bucket_uri) "
                "VALUES ('B1', ?, ?, ?)",
                (i, f"new_intake/B1/p{i}.jpg", f"file:///new_intake/B1/p{i}.jpg"),
            )
    return "B1"


def status_of(settings, batch_id="B1"):
    with db.connect(settings.db_path) as conn:
        return conn.execute(
            "SELECT status FROM new_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()["status"]


def pending_ids(settings):
    with db.connect(settings.db_path) as conn:
        return [b["batch_id"] for b in db.get_pending_batches(conn)]


# ---------- the reported defect ----------


def test_a_wave_landing_after_confirmation_does_not_reopen_the_batch(settings, batch):
    """The exact reported sequence: confirm, then a late proposal wave arrives."""
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {0: True, 1: False}, proposal_cursor=2)
    assert status_of(settings) == "split_proposed"

    with db.connect(settings.db_path) as conn:
        db.mark_batch_confirmed(conn, batch)
    assert status_of(settings) == "confirmed"

    # Qwen's next wave finishes and persists, as run_propose_split does between waves.
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {2: True}, proposal_cursor=3)

    assert status_of(settings) == "confirmed", (
        "a late proposal wave dragged the batch back to 'split_proposed'"
    )


def test_a_confirmed_batch_is_not_offered_for_confirmation_again(settings, batch):
    """What the reviewer actually saw: the banner returning beside the real folder."""
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {0: True}, proposal_cursor=1)
    with db.connect(settings.db_path) as conn:
        db.mark_batch_confirmed(conn, batch)
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {1: False}, proposal_cursor=2)

    assert pending_ids(settings) == [], (
        "the confirmed batch is still listed as awaiting split confirmation"
    )


def test_the_proposal_still_records_while_the_batch_is_pending(settings, batch):
    """The guard must not stop the normal path: an unconfirmed batch still accepts its proposal."""
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {0: True, 1: False}, proposal_cursor=2)
        rows = {r["sequence_index"]: r["proposed_new_document"] for r in db.get_batch_images(conn, batch)}

    assert status_of(settings) == "split_proposed"
    assert rows[0] == 1 and rows[1] == 0
    assert pending_ids(settings) == [batch]


def test_the_cursor_is_not_rolled_back_by_a_late_wave(settings, batch):
    """A late wave is ignored wholesale, so it cannot leave the cursor disagreeing with the
    documents that now exist."""
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {0: True}, proposal_cursor=3)
    with db.connect(settings.db_path) as conn:
        db.mark_batch_confirmed(conn, batch)
    with db.connect(settings.db_path) as conn:
        db.set_batch_split_proposal(conn, batch, {1: True}, proposal_cursor=1)
    with db.connect(settings.db_path) as conn:
        cursor = conn.execute(
            "SELECT split_proposal_cursor FROM new_batches WHERE batch_id = ?", (batch,)
        ).fetchone()[0]

    assert cursor == 3


# ---------- confirming twice ----------


def test_confirming_a_second_time_is_refused(settings, batch):
    """A stale banner, a double click, or a second reviewer."""
    with db.connect(settings.db_path) as conn:
        db.mark_batch_confirmed(conn, batch)

    with pytest.raises(ValueError, match="already been confirmed"):
        split.confirm_batch_split(batch, [{"display_id": "E2005AAA", "sequence_indices": [0]}], settings)


def test_confirming_a_batch_that_does_not_exist_is_refused(settings, batch):
    """`batch` is requested only to initialise the schema -- the id below is a different one."""
    with pytest.raises(ValueError):
        split.confirm_batch_split(
            "nope", [{"display_id": "E2005AAA", "sequence_indices": [0]}], settings
        )


# ---------- the propose loop stands down ----------


def test_batch_is_pending_is_what_the_listing_uses(settings, batch):
    """The loop's check and the banner's listing must agree about what "pending" means, or the
    guard protects a different thing than the defect it was written for."""
    with db.connect(settings.db_path) as conn:
        assert db.batch_is_pending(conn, batch) is True
        db.mark_batch_confirmed(conn, batch)
        assert db.batch_is_pending(conn, batch) is False
        assert db.batch_is_pending(conn, "no-such-batch") is False


def test_the_propose_loop_checks_before_every_wave(settings):
    """Between waves, not only at the start: the confirmation arrives mid-run by definition."""
    import pathlib

    source = (pathlib.Path(split.__file__)).read_text(encoding="utf-8")
    loop = source.split("while cursor < len(batch_images):", 1)[1]
    assert "db.batch_is_pending(conn, batch_id)" in loop.split("\n\n", 1)[0], (
        "the pending check is no longer inside the wave loop"
    )
