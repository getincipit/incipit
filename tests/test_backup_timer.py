"""Backups run inside the review server, every ten minutes, and only when something changed."""

import sqlite3
import json
import pathlib
import threading
import time
from pathlib import Path

import pytest

from incipit import backup
from incipit import db as incipit_db
from conftest import insert_rows


@pytest.fixture
def db(tmp_path):
    """The real schema, because verify_snapshot checks the tables that matter are readable."""
    path = tmp_path / "extraction.db"
    incipit_db.init_db(str(path))
    return path


def write_to(db, document_id="E1AAA"):
    conn = sqlite3.connect(db)
    insert_rows(conn,"INSERT INTO new_documents (document_id, status) VALUES (?, 'drafted')", (document_id,))
    conn.commit()
    conn.close()


def snapshots(backup_dir):
    return [p.name for _, p in backup.list_snapshots(Path(backup_dir))]


# ---------- detecting change ----------


def test_an_unchanged_database_writes_no_second_snapshot(db, tmp_path):
    out = tmp_path / "bk"
    assert backup.snapshot_database(db, out, only_if_changed=True) is not None
    assert backup.snapshot_database(db, out, only_if_changed=True) is None
    assert len(snapshots(out)) == 1


def test_a_changed_database_writes_one(db, tmp_path):
    out = tmp_path / "bk"
    backup.snapshot_database(db, out, only_if_changed=True)
    write_to(db)
    assert backup.snapshot_database(db, out, only_if_changed=True) is not None
    assert len(snapshots(out)) == 2


def test_the_first_run_of_an_empty_destination_always_keeps_its_snapshot(db, tmp_path):
    """There is nothing to compare against, and "unchanged" must never mean "no backup exists"."""
    assert backup.snapshot_database(db, tmp_path / "bk", only_if_changed=True) is not None


def test_an_unchanged_run_leaves_nothing_behind_at_all(db, tmp_path):
    """Not even a .partial."""
    out = tmp_path / "bk"
    backup.snapshot_database(db, out, only_if_changed=True)
    before = sorted(p.name for p in out.iterdir())
    backup.snapshot_database(db, out, only_if_changed=True)
    assert sorted(p.name for p in out.iterdir()) == before


def test_without_the_flag_every_call_still_snapshots(db, tmp_path):
    """The manual path and the restore drill both want a snapshot on demand, changed or not."""
    out = tmp_path / "bk"
    backup.snapshot_database(db, out, moment=_moment(1))
    backup.snapshot_database(db, out, moment=_moment(2))
    assert len(snapshots(out)) == 2


def _moment(seconds):
    from datetime import datetime, timezone
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)


def test_an_unreadable_previous_snapshot_does_not_suppress_this_one(db, tmp_path, monkeypatch):
    """A comparison that cannot be made is not evidence of no change."""
    out = tmp_path / "bk"
    backup.snapshot_database(db, out, only_if_changed=True)
    monkeypatch.setattr(backup, "digest_of",
                        lambda p: (_ for _ in ()).throw(OSError("gone")))
    assert backup.snapshot_database(db, out, only_if_changed=True) is not None


def test_a_snapshot_that_fails_verification_still_raises(db, tmp_path, monkeypatch):
    """Unchanged and unusable are different answers."""
    monkeypatch.setattr(backup, "verify_snapshot", lambda p: (False, "malformed"))
    with pytest.raises(RuntimeError):
        backup.snapshot_database(db, tmp_path / "bk", only_if_changed=True)


# ---------- what run_backup reports ----------


def test_a_no_change_run_is_recorded_as_a_success(db, tmp_path):
    """"The backup ran and there was nothing to save" is a different fact from "the backup did not
    run", and the status file is the only place anyone finds out which."""
    out, logs = tmp_path / "bk", tmp_path / "logs"
    backup.run_backup(db, out, status_dir=logs, only_if_changed=True)
    summary = backup.run_backup(db, out, status_dir=logs, only_if_changed=True)

    assert summary["unchanged"] is True
    assert summary["snapshot"] is None
    status = backup.read_status(logs)
    assert status["ok"] is True
    assert "no change" in status["detail"]
    assert status["last_success_at"]


def test_a_no_change_run_does_not_prune(db, tmp_path):
    """Pruning on a tick that added nothing would age the history on wall-clock alone -- at a ten-
    minute cadence that deletes real states during a quiet week."""
    out = tmp_path / "bk"
    backup.run_backup(db, out, only_if_changed=True)
    kept = snapshots(out)
    assert backup.run_backup(db, out, only_if_changed=True)["pruned"] == 0
    assert snapshots(out) == kept


def test_a_no_change_run_still_copies_files_and_mirrors(db, tmp_path):
    """An image can land before the row that references it, and a share that was unreachable last
    time may be back."""
    data = tmp_path / "data"
    (data / "bucket" / "records").mkdir(parents=True, exist_ok=True)
    (data / "bucket" / "records" / "E1AAA.json").write_text("{}", encoding="utf-8")
    out, mirror = tmp_path / "bk", tmp_path / "mirror"

    backup.run_backup(db, out, data_dir=data, mirror_dir=mirror, only_if_changed=True)
    (data / "bucket" / "records" / "E1AAA-second.json").write_text("{}", encoding="utf-8")
    summary = backup.run_backup(db, out, data_dir=data, mirror_dir=mirror, only_if_changed=True)

    assert summary["unchanged"] is True
    assert summary["files_copied"] == 1, "a new file was skipped because the database had not moved"
    assert summary["mirror"]["state"] == "ok"


def test_the_changed_run_still_reports_everything_it_used_to(db, tmp_path):
    summary = backup.run_backup(db, tmp_path / "bk", only_if_changed=True)
    assert summary["unchanged"] is False
    assert summary["snapshot"].startswith("extraction-")
    assert summary["bytes"] > 0
    assert summary["snapshots_kept"] == 1


# ---------- the timer ----------


def test_the_timer_backs_up_on_a_tick(db, tmp_path):
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop,
        interval_minutes=0.001, first_delay_seconds=0))
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "no snapshot was written")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_the_timer_keeps_one_snapshot_while_nothing_changes(db, tmp_path):
    """The whole point of the cadence: ticking often is cheap only if a quiet tick writes nothing."""
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop,
        interval_minutes=0.001, first_delay_seconds=0))
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "no snapshot was written")
        time.sleep(0.5)
        assert len(snapshots(out)) == 1, "an unchanged database was snapshotted repeatedly"
    finally:
        stop.set()
        thread.join(timeout=5)


def test_the_timer_notices_a_write(db, tmp_path):
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop,
        interval_minutes=0.001, snapshot_interval_minutes=0, first_delay_seconds=0))
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "no first snapshot")
        write_to(db, "E1AAB")
        _until(lambda: len(snapshots(out)) == 2, "the write was not picked up")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_failing_backup_does_not_stop_the_timer(db, tmp_path, monkeypatch):
    """The server staying up is what lets somebody read the status file and find out why backups
    stopped."""
    out = tmp_path / "bk"
    calls = []

    def sometimes(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("the drive went away")
        return {"unchanged": False, "snapshot": "x.db", "snapshots_kept": 1}

    monkeypatch.setattr(backup, "run_backup", sometimes)
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop,
        interval_minutes=0.001, first_delay_seconds=0))
    thread.start()
    try:
        _until(lambda: len(calls) >= 3, "the timer stopped after a failure")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_stopping_is_prompt_rather_than_at_the_next_tick(db, tmp_path):
    """A ten-minute sleep that ignores the stop event is a ten-minute shutdown."""
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=tmp_path / "bk", stop=stop,
        interval_minutes=600, first_delay_seconds=0))
    thread.start()
    _until(lambda: len(snapshots(tmp_path / "bk")) == 1, "no snapshot was written")
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "the timer ignored its stop event"


def test_the_first_delay_is_honoured_and_interruptible(db, tmp_path):
    """It exists so a restart loop cannot become a backup loop -- and it must not make shutdown
    wait for it either."""
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop,
        interval_minutes=0.001, first_delay_seconds=30))
    thread.start()
    time.sleep(0.2)
    assert snapshots(out) == [], "the first delay was skipped"
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "stopping during the first delay had to wait it out"


def test_start_backup_timer_runs_in_the_background_and_never_holds_the_process_open(db, tmp_path):
    stop = backup.start_backup_timer(db, tmp_path / "bk", interval_minutes=0.001)
    try:
        thread = next(t for t in threading.enumerate() if t.name == "incipit-backup")
        assert thread.daemon, "a non-daemon backup thread would hang shutdown"
    finally:
        stop.set()


def test_the_work_reaches_the_drive_every_couple_of_minutes():
    assert backup.BACKUP_INTERVAL_MINUTES == 2


def test_the_database_is_snapshotted_less_often_than_that():
    """Each snapshot is a full copy of extraction.db and the policy keeps six hours of them, so the
    fast cadence is for the files and not for these."""
    assert backup.SNAPSHOT_INTERVAL_MINUTES > backup.BACKUP_INTERVAL_MINUTES


def test_a_fast_tick_copies_the_work_without_snapshotting(db, tmp_path):
    """Reported: "can we make the back up timer quicker" -- quicker for the reviewer's work."""
    out = tmp_path / "bk"
    data = tmp_path / "data"
    record = data / "bucket" / "records" / "f1" / "d1.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"document_id": "d1"}', encoding="utf-8")

    backup.run_backup(db, out, data_dir=data, snapshot_database_too=False)

    assert (out / "bucket" / "records" / "f1" / "d1.json").is_file()
    assert snapshots(out) == []


def test_a_later_tick_still_takes_the_snapshot(db, tmp_path):
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop, interval_minutes=0.001,
        snapshot_interval_minutes=0, first_delay_seconds=0))
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "the database was never snapshotted")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_the_first_tick_snapshots_on_a_machine_that_has_just_booted(db, tmp_path):
    """Found by CI, not here: the timer asked whether `clock() - last_snapshot` had reached the
    cadence, starting last_snapshot at zero."""
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop, interval_minutes=0.001,
        snapshot_interval_minutes=10, first_delay_seconds=0,
        clock=lambda: 12.0))   # a box that booted twelve seconds ago
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "the first tick did not snapshot")
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_quiet_machine_does_not_snapshot_again_before_the_cadence(db, tmp_path):
    """The other half: having taken the first one, it waits."""
    out = tmp_path / "bk"
    stop = threading.Event()
    thread = threading.Thread(target=backup.backup_forever, kwargs=dict(
        db_path=db, backup_dir=out, stop=stop, interval_minutes=0.001,
        snapshot_interval_minutes=10, first_delay_seconds=0,
        clock=lambda: 12.0))
    thread.start()
    try:
        _until(lambda: len(snapshots(out)) == 1, "the first tick did not snapshot")
        write_to(db, "E1AAB")
        time.sleep(0.5)          # many ticks at 0.06s apiece
        assert len(snapshots(out)) == 1
    finally:
        stop.set()
        thread.join(timeout=5)


def _until(condition, message, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


# ---------- the bucket, at the keys it uses ----------
# Keys are built from internal ids that never change, so the backup copies them as they are.


def _live_tree(tmp_path):
    """A data directory shaped like the real bucket: one record, one scan, one raw upload."""
    bucket = tmp_path / "data" / "bucket"
    (bucket / "records" / "f1").mkdir(parents=True)
    (bucket / "records" / "f1" / "d1.json").write_text("{}", encoding="utf-8")
    (bucket / "documents" / "f1" / "d1").mkdir(parents=True)
    (bucket / "documents" / "f1" / "d1" / "0000_page.jpg").write_bytes(b"scan")
    (bucket / "uploads" / "b1").mkdir(parents=True)
    (bucket / "uploads" / "b1" / "0000_page.jpg").write_bytes(b"upload")
    (bucket / "derived" / "view" / "documents" / "f1" / "d1").mkdir(parents=True)
    (bucket / "derived" / "view" / "documents" / "f1" / "d1" / "0000_page.jpg").write_bytes(b"cache")
    return tmp_path / "data"


def test_a_documents_record_and_scans_are_copied_at_their_bucket_keys(db, tmp_path):
    out = tmp_path / "bk"
    backup.run_backup(db, out, data_dir=_live_tree(tmp_path), only_if_changed=True)

    assert (out / "bucket" / "records" / "f1" / "d1.json").is_file()
    assert (out / "bucket" / "documents" / "f1" / "d1" / "0000_page.jpg").is_file()


def test_uploads_that_are_not_yet_documents_are_backed_up_under_their_batch(db, tmp_path):
    """Raw scans are the only copy until a reviewer confirms the split."""
    out = tmp_path / "bk"
    backup.run_backup(db, out, data_dir=_live_tree(tmp_path), only_if_changed=True)

    assert (out / "bucket" / "uploads" / "b1" / "0000_page.jpg").is_file()


def test_rendered_copies_are_not_backed_up(db, tmp_path):
    """derived/ is a cache the server rebuilds from the scans."""
    out = tmp_path / "bk"
    backup.run_backup(db, out, data_dir=_live_tree(tmp_path), only_if_changed=True)

    assert not (out / "bucket" / "derived").exists()


def test_a_second_run_recopies_nothing(db, tmp_path):
    """Ticking every couple of minutes is only affordable if a quiet tick is a directory walk."""
    data = _live_tree(tmp_path)
    out = tmp_path / "bk"
    backup.run_backup(db, out, data_dir=data, only_if_changed=True)
    again = backup.run_backup(db, out, data_dir=data, only_if_changed=True)

    assert again["files_copied"] == 0


# ---------- two snapshots in the same second ----------


def test_a_second_snapshot_in_the_same_second_does_not_replace_the_first(db, tmp_path):
    """Names carry the timestamp to the second, so two snapshots in one second resolve to one name
    and Path.replace would silently destroy the earlier."""
    out = tmp_path / "bk"
    moment = _moment(0)
    first = backup.snapshot_database(db, out, moment=moment)
    write_to(db)
    second = backup.snapshot_database(db, out, moment=moment)

    assert first.exists() and second.exists()
    assert first != second
    assert len(snapshots(out)) == 2


def test_the_walked_forward_name_is_still_a_readable_snapshot_name(db, tmp_path):
    """It has to keep sorting chronologically and stay parseable, or retention stops seeing it."""
    out = tmp_path / "bk"
    backup.snapshot_database(db, out, moment=_moment(0))
    write_to(db)
    second = backup.snapshot_database(db, out, moment=_moment(0))

    assert backup.parse_snapshot_time(second) is not None
    assert [p.name for _, p in backup.list_snapshots(out)] == sorted(snapshots(out), reverse=True)


# ---------- reaching the server ----------


def _review_args(tmp_path, *extra):
    from incipit import accounts, bootstrap
    from incipit import db as database

    # The review command will not start a site that has no owner.
    bootstrap.initialise(tmp_path / "x.db")
    with database.connect(str(tmp_path / "x.db")) as conn:
        if not accounts.owner_exists(conn):
            accounts.create_owner(conn, "owner", "owner-password")
    return ["review", "--db", str(tmp_path / "x.db"), "--bucket-uri", "file://./b", *extra]


def test_the_review_command_takes_its_destinations_from_the_environment(monkeypatch, tmp_path):
    """The host passes no backup flags -- run-review.ps1 starts the server with --db and --port and
    nothing else -- so `.env` is the only thing that turns backups on there."""
    from incipit import cli

    monkeypatch.setenv("INCIPIT_BACKUP_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("INCIPIT_BACKUP_MIRROR", r"W:\Editing\AI Backups")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    seen = {}
    monkeypatch.setattr(cli, "run_server", lambda *a, **kw: seen.update(kw))

    cli.main(_review_args(tmp_path))

    assert seen["backup_dir"] == str(tmp_path / "local")
    assert seen["backup_mirror"] == r"W:\Editing\AI Backups"
    assert seen["backup_interval_minutes"] == backup.BACKUP_INTERVAL_MINUTES


def test_an_explicit_flag_beats_the_environment(monkeypatch, tmp_path):
    from incipit import cli

    monkeypatch.setenv("INCIPIT_BACKUP_DIR", str(tmp_path / "from-env"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    seen = {}
    monkeypatch.setattr(cli, "run_server", lambda *a, **kw: seen.update(kw))

    cli.main(_review_args(tmp_path, "--backup-dir", str(tmp_path / "from-flag")))

    assert seen["backup_dir"] == str(tmp_path / "from-flag")


def test_no_destination_configured_leaves_backups_off_rather_than_guessing(monkeypatch, tmp_path):
    """Writing snapshots into an unasked-for default is worse than none: it looks like protection
    while sitting on the same disk as the database."""
    from incipit import cli

    monkeypatch.delenv("INCIPIT_BACKUP_DIR", raising=False)
    monkeypatch.delenv("INCIPIT_BACKUP_MIRROR", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    seen = {}
    monkeypatch.setattr(cli, "run_server", lambda *a, **kw: seen.update(kw))

    cli.main(_review_args(tmp_path))

    assert seen["backup_dir"] == ""


# ---------- snapshots out of the way ----------


def test_snapshots_go_in_their_own_folder(db, tmp_path):
    """Retention keeps dozens of them."""
    out = tmp_path / "bk"
    snapshot = backup.snapshot_database(db, out)

    assert snapshot.parent.name == backup.SNAPSHOT_DIRNAME
    assert [p.name for p in out.iterdir() if p.is_file()] == []


def test_snapshots_taken_before_the_move_are_still_found(db, tmp_path):
    """A backup directory in use has snapshots at its root."""
    out = tmp_path / "bk"
    out.mkdir()
    legacy = out / backup.snapshot_name(_moment(0))
    legacy.write_bytes((tmp_path / "extraction.db").read_bytes())
    write_to(db)
    current = backup.snapshot_database(db, out)

    found = {p.name for _, p in backup.list_snapshots(out)}
    assert legacy.name in found and current.name in found


def test_a_legacy_snapshot_is_still_thinned_by_retention(db, tmp_path):
    """Found is not enough -- an old snapshot at the root has to stay inside the policy, or the
    directory grows for ever now that nothing writes there."""
    from datetime import datetime, timedelta, timezone

    out = tmp_path / "bk"
    out.mkdir()
    body = (tmp_path / "extraction.db").read_bytes()
    now = datetime.now(timezone.utc)
    for days in (200, 199):
        (out / backup.snapshot_name(now - timedelta(days=days))).write_bytes(body)
    backup.snapshot_database(db, out)

    backup.prune(out)
    assert len(backup.list_snapshots(out)) < 3, "old root snapshots were never thinned"


def test_a_name_taken_at_the_root_is_not_reused_in_the_new_folder(db, tmp_path):
    """Both places are one namespace for collision purposes -- otherwise a snapshot in `snapshots/`
    could share a name with one at the root and the two would be indistinguishable in a listing."""
    out = tmp_path / "bk"
    out.mkdir()
    (out / backup.snapshot_name(_moment(0))).write_bytes(b"a legacy snapshot")
    taken = backup.snapshot_database(db, out, moment=_moment(0))

    assert taken.name != backup.snapshot_name(_moment(0))
    assert taken.parent.name == backup.SNAPSHOT_DIRNAME


def test_a_snapshot_left_at_the_root_is_moved_in(db, tmp_path):
    out = tmp_path / "bk"
    out.mkdir()
    legacy = out / backup.snapshot_name(_moment(0))
    legacy.write_bytes((tmp_path / "extraction.db").read_bytes())

    assert backup.migrate_snapshots(out) == 1
    assert not legacy.exists()
    assert (out / backup.SNAPSHOT_DIRNAME / legacy.name).is_file()


def test_the_migration_runs_on_every_backup(db, tmp_path):
    """Recent, so retention keeps it -- an old one would be thinned and hide whether it moved."""
    from datetime import datetime, timezone

    out = tmp_path / "bk"
    out.mkdir()
    legacy = out / backup.snapshot_name(datetime.now(timezone.utc))
    legacy.write_bytes((tmp_path / "extraction.db").read_bytes())
    write_to(db)

    backup.run_backup(db, out, only_if_changed=True)

    assert [p.name for p in out.iterdir() if p.is_file()] == ["last-status.json"]
    assert len(snapshots(out)) == 2


def test_the_migration_leaves_the_document_folders_alone(db, tmp_path):
    out = tmp_path / "bk"
    backup.run_backup(db, out, data_dir=_live_tree(tmp_path), only_if_changed=True)
    before = sorted(p.name for p in out.iterdir())

    backup.migrate_snapshots(out)
    assert sorted(p.name for p in out.iterdir()) == before


def test_a_name_already_in_the_folder_is_not_duplicated(db, tmp_path):
    out = tmp_path / "bk"
    kept = backup.snapshot_database(db, out)
    (out / kept.name).write_bytes(b"an older copy under the same name")

    backup.migrate_snapshots(out)
    assert not (out / kept.name).exists()
    assert len(snapshots(out)) == 1


# ---------- a reviewer's edit has to reach the backup ----------
# Reported from the host: snapshot/ was updating every ten minutes while new/ and existing/ sat a
# day stale.


def _bucket(root):
    """A BucketStorage rooted under the test's own directory."""
    from incipit.storage import BucketStorage
    return BucketStorage("file://" + (pathlib.Path(root) / "bucket").as_posix())


def _drafted(db_path, document_id="E2002AAA", text="the model's draft"):
    from incipit import db as incipit_db
    with incipit_db.connect(str(db_path)) as conn:
        insert_rows(conn,"INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, "
                     "folder_id, uploaded_folder_name) VALUES ('B','now',1,'E2002','E2002_Advice_jpegs')")
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, text, output_json) "
            "VALUES (?, 'B', 'drafted', ?, ?)",
            (document_id, text, json.dumps({"transcription": {"text": text},
                                            "review": {"status": "unreviewed",
                                                       "reviewed_text": ""}})))
    return document_id


def test_a_rewritten_record_is_copied_even_when_its_size_is_unchanged(tmp_path):
    """The second cause."""
    import os, time

    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    f = source / "new_document.json"
    f.write_text("AAAA", encoding="utf-8")
    assert backup.copy_tree_additive(source, dest) == 1

    f.write_text("BBBB", encoding="utf-8")          # same size, different content
    later = time.time() + 60
    os.utime(f, (later, later))
    assert backup.copy_tree_additive(source, dest) == 1
    assert (dest / "new_document.json").read_text(encoding="utf-8") == "BBBB"


def test_an_edit_made_seconds_before_the_tick_is_still_copied(tmp_path):
    """The trap inside the fix."""
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    f = source / "new_document.json"
    f.write_text("favour", encoding="utf-8")
    assert backup.copy_tree_additive(source, dest) == 1

    f.write_text("favors", encoding="utf-8")        # same size, same second
    assert backup.copy_tree_additive(source, dest) == 1
    assert (dest / "new_document.json").read_text(encoding="utf-8") == "favors"


def test_a_large_file_is_not_read_on_every_tick(tmp_path):
    """Scans are megabytes and write-once."""
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    scan = source / "page.jpg"
    scan.write_bytes(b"x" * (backup.DIGEST_MAX_BYTES + 1))
    assert backup.copy_tree_additive(source, dest) == 1

    reads = []
    original = pathlib.Path.read_bytes
    try:
        pathlib.Path.read_bytes = lambda self: (reads.append(self), original(self))[1]
        assert backup.copy_tree_additive(source, dest) == 0
    finally:
        pathlib.Path.read_bytes = original
    assert reads == [], "the backup read a scan it did not need to"


def test_an_unchanged_file_is_still_skipped(tmp_path):
    """Ticking every ten minutes is only affordable if a quiet run copies nothing."""
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    (source / "page.jpg").write_bytes(b"scan")
    assert backup.copy_tree_additive(source, dest) == 1
    assert backup.copy_tree_additive(source, dest) == 0


