"""Backups of the irreplaceable state."""

import sqlite3
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import insert_rows, unreachable_destination
from incipit import backup, db


def _seeded_db(tmp_path: Path, rows: int = 3) -> Path:
    path = tmp_path / "extraction.db"
    db.init_db(str(path))
    with db.connect(str(path)) as conn:
        insert_rows(conn,"INSERT INTO new_batches (batch_id, uploaded_at, image_count) VALUES ('B', 'now', 0)")
        for i in range(rows):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, reviewed_text, review_status) "
                "VALUES (?, 'B', ?, 'accepted')",
                (f"E2241AA{chr(ord('A') + i)}", f"a reviewer wrote this for {i}"),
            )
    return path


# ---------- snapshotting ----------


def test_snapshot_captures_reviewer_edits(tmp_path):
    """The whole point."""
    source = _seeded_db(tmp_path)
    snapshot = backup.snapshot_database(source, tmp_path / "backups")

    with sqlite3.connect(snapshot) as conn:
        texts = [r[0] for r in conn.execute("SELECT reviewed_text FROM new_documents ORDER BY document_id")]
    assert texts == ["a reviewer wrote this for 0", "a reviewer wrote this for 1", "a reviewer wrote this for 2"]


def test_snapshot_of_a_database_with_uncommitted_wal_content(tmp_path):
    """A plain file copy is unsafe here: in WAL mode, committed data can still be sitting in the
    -wal file, so copying only the .db can silently lose it."""
    source = _seeded_db(tmp_path)
    live = sqlite3.connect(source)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute(
        "INSERT INTO new_documents (document_id, batch_id, reviewed_text) VALUES ('E2241AAZ', 'B', 'late edit')"
    )
    live.commit()  # committed, but very likely still only in the -wal
    try:
        snapshot = backup.snapshot_database(source, tmp_path / "backups")
        with sqlite3.connect(snapshot) as conn:
            found = conn.execute(
                "SELECT reviewed_text FROM new_documents WHERE document_id = 'E2241AAZ'"
            ).fetchone()
        assert found == ("late edit",)
    finally:
        live.close()


def test_snapshot_works_while_the_database_is_being_written(tmp_path):
    """This runs from inside the review server every ten minutes, so an open writer must not make
    it fail."""
    source = _seeded_db(tmp_path)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO new_documents (document_id, batch_id) VALUES ('E2241ABA', 'B')")
    writer.commit()
    try:
        snapshot = backup.snapshot_database(source, tmp_path / "backups")
        ok, _ = backup.verify_snapshot(snapshot)
        assert ok
    finally:
        writer.close()


def test_snapshots_are_named_so_they_sort_chronologically(tmp_path):
    source = _seeded_db(tmp_path)
    early = backup.snapshot_database(source, tmp_path / "b", moment=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc))
    late = backup.snapshot_database(source, tmp_path / "b", moment=datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc))

    assert early.name < late.name  # plain string sort is chronological -- `ls` is usable in an incident
    assert backup.list_snapshots(tmp_path / "b")[0][1] == late  # newest first


def test_an_interrupted_snapshot_leaves_no_usable_looking_file(tmp_path):
    """Written under a .partial name and only renamed once verified, so a crash mid-copy can't
    leave something the restore path would pick as newest."""
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup.snapshot_database(source, backup_dir)

    (backup_dir / "extraction-20260801T030000Z.partial").write_bytes(b"half a database")

    assert len(backup.list_snapshots(backup_dir)) == 1  # the partial file is not a snapshot


# ---------- verification: a corrupt backup is worse than none ----------


def test_verification_rejects_a_corrupt_snapshot(tmp_path):
    bad = tmp_path / "extraction-20260801T030000Z.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)  # right magic, garbage inside

    ok, detail = backup.verify_snapshot(bad)

    assert not ok
    assert detail


def test_verification_rejects_a_structurally_valid_but_empty_database(tmp_path):
    """`integrity_check` passes on an empty file -- it is a perfectly valid database with nothing
    in it."""
    empty = tmp_path / "extraction-20260801T030000Z.db"
    sqlite3.connect(empty).close()

    ok, detail = backup.verify_snapshot(empty)

    assert not ok
    assert "new_documents" in detail


def test_a_failed_snapshot_is_discarded_and_does_not_displace_the_good_one(tmp_path, monkeypatch):
    """The rule that makes this trustworthy: a bad copy never sits in the directory pretending to
    be protection, and the previous good snapshot survives."""
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    good = backup.snapshot_database(source, backup_dir)

    monkeypatch.setattr(backup, "verify_snapshot", lambda path: (False, "pretend corruption"))
    with pytest.raises(RuntimeError, match="failed verification"):
        backup.snapshot_database(source, backup_dir)

    remaining = [p for _, p in backup.list_snapshots(backup_dir)]
    assert remaining == [good]
    assert not list(backup_dir.glob("*.partial"))


# ---------- retention ----------


def _fake(moments: list[datetime]) -> list[tuple[datetime, Path]]:
    return [(m, Path(backup.snapshot_name(m))) for m in moments]


def test_retention_keeps_everything_recent(tmp_path):
    """The window where someone notices a mistake and wants to undo it -- nothing is thinned here."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    recent = [now - timedelta(minutes=15 * i) for i in range(20)]  # 5 hours at the real cadence

    keep, delete = backup.plan_retention(_fake(recent), now=now)

    assert len(keep) == 20 and delete == []


def test_retention_thins_older_history_to_one_an_hour_then_one_a_day(tmp_path):
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    moments = [now - timedelta(minutes=15 * i) for i in range(4 * 24 * 10)]  # 10 days, every 15 min

    keep, delete = backup.plan_retention(_fake(moments), now=now)

    assert len(delete) > len(keep)          # most of 960 snapshots go
    assert 40 <= len(keep) <= 90            # recent-in-full + hourly + daily, not hundreds
    kept = {p.name for p in keep}
    assert backup.snapshot_name(moments[0]) in kept   # newest always survives


def test_retention_keeps_the_newest_in_each_bucket_not_the_oldest(tmp_path):
    """What survives from a given hour should be that hour's end state, which is what someone
    restoring almost always wants."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    old_hour = now - timedelta(hours=20)
    moments = [now, old_hour.replace(minute=0), old_hour.replace(minute=45)]

    keep, _ = backup.plan_retention(_fake(moments), now=now)

    assert backup.snapshot_name(old_hour.replace(minute=45)) in {p.name for p in keep}
    assert backup.snapshot_name(old_hour.replace(minute=0)) not in {p.name for p in keep}


def test_prune_never_deletes_the_only_snapshot(tmp_path):
    """A clock jumping forward would age everything out at once; this stops a retention pass
    turning into a wipe."""
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    only = backup.snapshot_database(source, backup_dir, moment=datetime(2020, 1, 1, tzinfo=timezone.utc))

    deleted = backup.prune(backup_dir)  # ancient by any policy

    assert deleted == []
    assert only.exists()


def test_prune_never_deletes_the_newest_even_when_policy_says_so(tmp_path):
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    for year in (2019, 2020):
        backup.snapshot_database(source, backup_dir, moment=datetime(year, 1, 1, tzinfo=timezone.utc))

    backup.prune(backup_dir)

    assert len(backup.list_snapshots(backup_dir)) >= 1


def test_prune_ignores_files_that_are_not_ours(tmp_path):
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup.snapshot_database(source, backup_dir)
    bystander = backup_dir / "notes.txt"
    bystander.write_text("do not delete me", encoding="utf-8")

    backup.prune(backup_dir, now=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert bystander.exists()


# ---------- additive file copying ----------


def test_file_copy_never_deletes_at_the_destination(tmp_path):
    """Mirroring would propagate a bad delete from the live bucket into the backup -- turning one
    mistake into two."""
    source, dest = tmp_path / "src", tmp_path / "dst"
    (source / "a").mkdir(parents=True)
    (source / "a" / "keep.json").write_text("new", encoding="utf-8")
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "already-deleted-upstream.json").write_text("precious", encoding="utf-8")

    backup.copy_tree_additive(source, dest)

    assert (dest / "a" / "keep.json").read_text(encoding="utf-8") == "new"
    assert (dest / "a" / "already-deleted-upstream.json").exists()


def test_file_copy_skips_files_already_backed_up(tmp_path):
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    (source / "one.json").write_text("x", encoding="utf-8")

    assert backup.copy_tree_additive(source, dest) == 1
    assert backup.copy_tree_additive(source, dest) == 0  # unchanged, so not recopied


def test_file_copy_tolerates_a_missing_source(tmp_path):
    assert backup.copy_tree_additive(tmp_path / "nope", tmp_path / "dst") == 0


# ---------- status: silent failure is the real enemy ----------


def test_a_successful_run_records_when_it_happened(tmp_path):
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"

    summary = backup.run_backup(source, backup_dir)

    status = backup.read_status(backup_dir)
    assert status["ok"] is True
    assert status["snapshot"] == summary["snapshot"]
    assert status["last_success_at"]


def test_a_failed_run_is_recorded_and_raised(tmp_path):
    """It must be both: recorded so a person can see it, raised so the scheduled task reports non-
    zero rather than looking like it worked."""
    backup_dir = tmp_path / "backups"

    with pytest.raises(Exception):
        backup.run_backup(tmp_path / "there-is-no-database.db", backup_dir)

    status = backup.read_status(backup_dir)
    assert status["ok"] is False
    assert status["detail"]


def test_a_failure_preserves_when_protection_was_last_real(tmp_path):
    """A run of failures must not erase the last-success time -- that timestamp is the number
    someone needs during an incident, and it is exactly when it would otherwise be overwritten."""
    source = _seeded_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup.run_backup(source, backup_dir)
    first_success = backup.read_status(backup_dir)["last_success_at"]

    with pytest.raises(Exception):
        backup.run_backup(tmp_path / "gone.db", backup_dir)

    status = backup.read_status(backup_dir)
    assert status["ok"] is False
    assert status["last_success_at"] == first_success


def test_status_survives_a_corrupted_status_file(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / backup.STATUS_FILENAME).write_text("{ not json", encoding="utf-8")

    assert backup.read_status(backup_dir) is None
    backup.write_status(backup_dir, True, "fine")
    assert backup.read_status(backup_dir)["ok"] is True


# ---------- the whole pass ----------


def test_run_backup_snapshots_prunes_and_copies_in_one_pass(tmp_path):
    source = _seeded_db(tmp_path)
    data_dir = tmp_path / "data"
    (data_dir / "bucket" / "records" / "f1").mkdir(parents=True, exist_ok=True)
    (data_dir / "bucket" / "records" / "f1" / "d1.json").write_text("{}", encoding="utf-8")

    summary = backup.run_backup(source, tmp_path / "backups", data_dir=data_dir)

    assert summary["snapshots_kept"] == 1
    assert summary["files_copied"] == 1
    assert (tmp_path / "backups" / "bucket" / "records" / "f1" / "d1.json").exists()


def test_a_snapshot_can_actually_be_restored(tmp_path):
    """The test that makes the rest mean anything: a backup nobody has restored is a hypothesis."""
    source = _seeded_db(tmp_path)
    snapshot = backup.snapshot_database(source, tmp_path / "backups")

    source.unlink()  # disaster
    restored = tmp_path / "extraction.db"
    restored.write_bytes(snapshot.read_bytes())

    with db.connect(str(restored)) as conn:
        rows = conn.execute(
            "SELECT document_id, reviewed_text FROM new_documents ORDER BY document_id"
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["reviewed_text"] == "a reviewer wrote this for 0"


# ---------- the dedicated backup drive ----------


def _unused_drive() -> str:
    """A drive letter this machine does not have."""
    import string
    for letter in reversed(string.ascii_uppercase):
        if not Path(f"{letter}:\\").exists():
            return letter
    raise RuntimeError("every drive letter is in use")


@pytest.mark.skipif(os.name != "nt", reason="an unmounted drive letter is a Windows concept; on "
                                            "POSIX the anchor is always / and always exists")
def test_an_unmounted_drive_is_reported_as_such_and_nothing_is_written():
    """The likeliest thing to go wrong with an external target."""
    missing = Path(f"{_unused_drive()}:/Editing/AI Backup")

    with pytest.raises(backup.DestinationUnavailable) as exc:
        backup.check_destination(missing)

    assert "not available" in str(exc.value)
    assert "not mounted" in str(exc.value)


def test_a_failure_is_still_recorded_when_the_backup_drive_is_gone(tmp_path):
    """The bug this closes: the status file used to live only in the backup directory, so an
    unmounted drive meant the failure could not be written down anywhere -- invisible in exactly
    the case the file exists for."""
    source = _seeded_db(tmp_path)
    gone = unreachable_destination(tmp_path)
    local = tmp_path / "logs"

    with pytest.raises(Exception):
        backup.run_backup(source, gone, status_dir=local)

    status = backup.read_status(local)
    assert status is not None, "the failure must be recorded somewhere reachable"
    assert status["ok"] is False
    assert status["detail"], "the record has to say what went wrong"


def test_writing_status_never_raises_even_with_nowhere_to_write(tmp_path):
    """write_status runs inside the except block of a failing backup."""
    gone = unreachable_destination(tmp_path)

    assert backup.write_status(gone, False, "drive went away") is None  # no raise


def test_reading_status_from_an_unreachable_drive_reads_as_no_status(tmp_path):
    gone = unreachable_destination(tmp_path)
    assert backup.read_status(gone) is None
    assert backup.read_status(None) is None


def test_a_backup_on_the_data_drive_is_allowed_but_warned_about(tmp_path):
    """It still protects against corruption and bad deletes, so it is not an error -- but it is not
    the thing a second drive was bought for, and quietly accepting it is how a folder ends up
    back on C: after a drive letter changes."""
    source = _seeded_db(tmp_path)

    warnings = backup.check_destination(tmp_path / "backups", source)

    assert len(warnings) == 1
    assert "same volume" in warnings[0]
    assert "NOT against that drive failing" in warnings[0]


def test_a_destination_with_spaces_in_the_path_works(tmp_path):
    r"""`W:\Editing\AI Backup` has a space in it, and an unquoted path is the classic way that
    breaks somewhere between the scheduled task and the shell."""
    source = _seeded_db(tmp_path)
    spaced = tmp_path / "Editing" / "AI Backup"

    summary = backup.run_backup(source, spaced, status_dir=tmp_path / "logs")

    assert (spaced / backup.SNAPSHOT_DIRNAME / summary["snapshot"]).is_file()
    assert backup.read_status(spaced)["ok"] is True


def test_a_successful_run_records_status_in_both_places(tmp_path):
    """Local copy for when the drive is gone, destination copy so the snapshots carry their own
    provenance if the folder is ever inspected on its own."""
    source = _seeded_db(tmp_path)
    dest, local = tmp_path / "dest", tmp_path / "logs"

    backup.run_backup(source, dest, status_dir=local)

    assert backup.read_status(dest)["ok"] is True
    assert backup.read_status(local)["ok"] is True


def test_check_destination_creates_the_folder_when_the_drive_is_there(tmp_path):
    """First run against a brand-new drive should not need the folder made by hand."""
    target = tmp_path / "Editing" / "AI Backup" / "nested"
    assert not target.exists()

    backup.check_destination(target)

    assert target.is_dir()
    assert not (target / ".write-probe").exists()  # the probe cleans up after itself


# ---------- which images are worth keeping ----------


def _bucket(tmp_path):
    """A data dir shaped like the real one: raw uploads and one document's own scans."""
    data = tmp_path / "data"
    for directory, count in (("uploads/b1", 3), ("documents/f1/d1", 2)):
        d = data / "bucket" / directory
        d.mkdir(parents=True)
        for i in range(count):
            (d / f"{i:04d}.jpg").write_bytes(b"scan-bytes")
    return data


def test_uploaded_scans_and_document_scans_are_backed_up(tmp_path):
    """Uploads have no upstream to refetch from, and they are what makes a split fixable later."""
    source = _seeded_db(tmp_path)
    data = _bucket(tmp_path)
    dest = tmp_path / "dest"

    summary = backup.run_backup(source, dest, data_dir=data, status_dir=tmp_path / "logs")

    assert summary["files_copied"] == 5
    assert (dest / "bucket" / "uploads" / "b1" / "0000.jpg").is_file()
    assert (dest / "bucket" / "documents" / "f1" / "d1" / "0000.jpg").is_file()


def test_a_second_run_recopies_nothing(tmp_path):
    """Scans are write-once, so in steady state this has to be a directory walk and nothing more."""
    source = _seeded_db(tmp_path)
    data = _bucket(tmp_path)
    dest = tmp_path / "dest"

    first = backup.run_backup(source, dest, data_dir=data, status_dir=tmp_path / "logs")
    second = backup.run_backup(source, dest, data_dir=data, status_dir=tmp_path / "logs")

    assert first["files_copied"] == 5
    assert second["files_copied"] == 0


def test_a_newly_uploaded_batch_is_picked_up_on_the_next_run(tmp_path):
    source = _seeded_db(tmp_path)
    data = _bucket(tmp_path)
    dest = tmp_path / "dest"
    backup.run_backup(source, dest, data_dir=data, status_dir=tmp_path / "logs")

    later = data / "bucket" / "uploads" / "b2"
    later.mkdir(parents=True)
    (later / "0000.jpg").write_bytes(b"a batch uploaded after the last backup")

    summary = backup.run_backup(source, dest, data_dir=data, status_dir=tmp_path / "logs")

    assert summary["files_copied"] == 1
    assert (dest / "bucket" / "uploads" / "b2" / "0000.jpg").is_file()


def test_a_database_only_run_copies_no_files(tmp_path):
    """Without a data directory a run snapshots the database and nothing else."""
    source = _seeded_db(tmp_path)
    dest = tmp_path / "dest"

    summary = backup.run_backup(source, dest, status_dir=tmp_path / "logs")

    assert summary["files_copied"] == 0
    assert not (dest / "bucket").exists()
    assert (dest / backup.SNAPSHOT_DIRNAME / summary["snapshot"]).is_file()
