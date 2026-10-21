"""Covers M6 (docs/specs/M6-backups-and-restore.md): choosing where backups go, the manifest beside
every snapshot, restoring a backup into a new folder from the command line or from Settings, and
the tray's word on stale backups."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from incipit import (accounts, backup, cli, datafolder, db, relocate, restore, review_server, split, startup, tray)
from conftest import next_document_ids, signed_in, tiny_image_bytes


def _site(tmp_path, monkeypatch, name="Incipit"):
    folder = tmp_path / name
    datafolder.initialise(folder)
    config = datafolder.load(folder)
    settings = datafolder.settings_for(folder, config)
    paths = datafolder.resolved_paths(folder, config)
    monkeypatch.setattr(startup, "run", lambda args: subprocess.CompletedProcess(args, 1, "", ""))
    app = review_server.create_app(settings, str(paths["exports"]), str(paths["logs"]))
    with db.connect(settings.db_path) as conn:
        if not conn.execute("SELECT 1 FROM users WHERE username = 'olive'").fetchone():
            accounts.create_owner(conn, "olive", "olive-password", "Olive")
            accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
            accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, settings, folder


@pytest.fixture
def site(tmp_path, monkeypatch):
    return _site(tmp_path, monkeypatch)


def _drafted(settings, display_id="F001AAA", folder="F001"):
    """One confirmed document with a drafted record, without a model call."""
    batch = split.run_ingest_batch([("0001.jpg", tiny_image_bytes())], settings, folder_display_id=folder)
    with next_document_ids(display_id):
        [document_id] = split.confirm_batch_split(batch, [{"display_id": display_id, "sequence_indices": [0]}],
                                                  settings)
    from incipit import storage
    with db.connect(settings.db_path) as conn:
        folder_id = db.folder_of_document(conn, document_id)
        conn.execute("UPDATE new_documents SET status = 'drafted', text = 'Dear Sir.' WHERE document_id = ?",
                     (document_id,))
    storage.BucketStorage(settings.bucket_uri).write_record(folder_id, document_id, json.dumps({"text": "Dear Sir."}))
    return document_id, folder_id


def _back_up(settings, folder, destination):
    paths = datafolder.resolved_paths(folder, datafolder.load(folder, with_env=False))
    return backup.run_backup(settings.db_path, destination, data_dir=folder, status_dir=paths["logs"],
                             export_dir=paths["exports"])


# ---------- AC-1: where backups go ----------


def test_ac1_the_owner_points_backups_at_another_folder(site, tmp_path):
    app, _, folder = site
    got = signed_in(app, "olive").put("/api/backups", json={"destination": str(tmp_path / "Elsewhere")})
    assert got.status_code == 200 and datafolder.load(folder, with_env=False)["paths"]["backups"] == str(tmp_path / "Elsewhere")


def test_ac1_a_drive_that_is_not_there_is_refused(site):
    app, _, _ = site
    got = signed_in(app, "olive").put("/api/backups", json={"destination": "Q:\\Nowhere\\Backups"})
    assert got.status_code == 400 and "not available" in got.json()["detail"]


def test_ac1_the_second_copy_cannot_sit_inside_the_data_folder(site):
    app, _, folder = site
    got = signed_in(app, "olive").put("/api/backups", json={"second_copy": str(folder / "copy")})
    assert got.status_code == 400


def test_ac1_an_admin_sees_but_cannot_change_where_backups_go(site, tmp_path):
    app, _, _ = site
    paul = signed_in(app, "paul")
    assert paul.get("/api/backups").status_code == 200
    assert paul.put("/api/backups", json={"destination": str(tmp_path / "x")}).status_code == 403


def test_ac1_a_new_destination_restarts_the_backups_there(site, tmp_path):
    app, _, _ = site
    restarted = []
    app.state.restart_backups = lambda: restarted.append(True)
    signed_in(app, "olive").put("/api/backups", json={"destination": str(tmp_path / "Elsewhere")})
    assert restarted == [True]


def test_ac1_a_reviewer_is_refused_backups(site):
    app, _, _ = site
    assert signed_in(app, "john").get("/api/backups").status_code == 403


# ---------- AC-2: what the page shows ----------


def test_ac2_backups_on_the_datas_drive_are_flagged(site):
    app, _, _ = site
    assert signed_in(app, "olive").get("/api/backups").json()["same_drive"] is True


def test_ac2_the_page_lists_snapshots_newest_first_with_sizes(site, tmp_path):
    app, settings, folder = site
    destination = tmp_path / "Elsewhere"
    _back_up(settings, folder, destination)
    backup.snapshot_database(settings.db_path, destination, moment=datetime.now(timezone.utc) + timedelta(hours=1))
    signed_in(app, "olive").put("/api/backups", json={"destination": str(destination)})
    listed = signed_in(app, "olive").get("/api/backups").json()["snapshots"]
    assert len(listed) == 2 and listed[0]["at"] > listed[1]["at"] and all(s["bytes"] > 0 for s in listed)


def test_ac2_the_page_shows_the_last_backups_result(site, tmp_path):
    app, settings, folder = site
    _back_up(settings, folder, folder / "backups")
    last = signed_in(app, "olive").get("/api/backups").json()["last"]
    assert last["ok"] is True and last["last_success_at"]


# ---------- AC-3: back up now ----------


def test_ac3_back_up_now_makes_a_snapshot(site):
    app, settings, folder = site
    client = signed_in(app, "olive")
    client.post("/api/backups/run")
    for _ in range(100):
        state = client.get("/api/backups").json()
        if not state["running"] and state["snapshots"]:
            break
        import time
        time.sleep(0.05)
    assert state["snapshots"] and state["last"]["ok"]


# ---------- AC-4: the manifest ----------


def test_ac4_every_snapshot_has_a_manifest_of_the_files_it_needs(site, tmp_path):
    _, settings, folder = site
    document_id, folder_id = _drafted(settings)
    summary = _back_up(settings, folder, tmp_path / "B")
    manifest = json.loads(backup.manifest_path(tmp_path / "B" / "snapshot" / summary["snapshot"]).read_text())
    kinds = {(e["kind"], e.get("document_id")) for e in manifest["files"]}
    assert ("page", document_id) in kinds and ("record", document_id) in kinds


def test_ac4_manifest_entries_carry_their_sizes(site, tmp_path):
    _, settings, folder = site
    _drafted(settings)
    summary = _back_up(settings, folder, tmp_path / "B")
    manifest = json.loads(backup.manifest_path(tmp_path / "B" / "snapshot" / summary["snapshot"]).read_text())
    assert all(isinstance(e["size"], int) and e["size"] > 0 for e in manifest["files"])


def test_ac4_the_backup_keeps_a_copy_of_the_sites_settings(site, tmp_path):
    _, settings, folder = site
    _back_up(settings, folder, tmp_path / "B")
    assert (tmp_path / "B" / "config" / "incipit.toml").read_text() == (folder / "incipit.toml").read_text()


def test_ac4_retention_removes_a_manifest_with_its_snapshot(site, tmp_path):
    _, settings, folder = site
    destination = tmp_path / "B"
    old = datetime.now(timezone.utc) - timedelta(days=400)
    first = backup.snapshot_database(settings.db_path, destination, moment=old)
    backup.write_manifest(first, folder)
    backup.snapshot_database(settings.db_path, destination, moment=old + timedelta(days=1))
    backup.snapshot_database(settings.db_path, destination)
    backup.prune(destination)
    assert not first.exists() and not backup.manifest_path(first).exists()


# ---------- AC-5: restore ----------


def _restored(tmp_path, monkeypatch, settings, folder):
    _back_up(settings, folder, tmp_path / "B")
    report = restore.restore(tmp_path / "B", tmp_path / "Restored")
    app, restored_settings, _ = _site(tmp_path, monkeypatch, "Restored")
    return report, app, restored_settings


def test_ac5_a_restored_folder_serves_its_documents_with_their_pages(site, tmp_path, monkeypatch):
    _, settings, folder = site
    document_id, _ = _drafted(settings)
    _, app, _ = _restored(tmp_path, monkeypatch, settings, folder)
    paul = signed_in(app, "paul")
    assert paul.get(f"/api/new/documents/{document_id}").status_code == 200
    assert paul.get(f"/api/new/documents/{document_id}/pages/0/image").status_code == 200


def test_ac5_reviews_and_comments_come_back(site, tmp_path, monkeypatch):
    app, settings, folder = site
    document_id, _ = _drafted(settings)
    paul = signed_in(app, "paul")
    document = paul.get(f"/api/new/documents/{document_id}").json()
    from conftest import review_payload
    paul.post(f"/api/new/documents/{document_id}/review", json=review_payload(document, reviewed_text="Checked."))
    paul.post(f"/api/documents/{document_id}/comments?workflow=new", json={"body": "see the margin"})
    _, restored_app, _ = _restored(tmp_path, monkeypatch, settings, folder)
    again = signed_in(restored_app, "paul")
    shown = again.get(f"/api/new/documents/{document_id}").json()
    comments = again.get(f"/api/documents/{document_id}/comments?workflow=new").json()
    assert (shown["transcription"]["text"], shown["review"]["status"]) == ("Checked.", "accepted")
    assert "see the margin" in json.dumps(comments)


def test_ac5_exports_come_back(site, tmp_path, monkeypatch):
    _, settings, folder = site
    exported = folder / "exports" / "documents" / "f1" / "folder.zip"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_bytes(b"PK zip")
    _restored(tmp_path, monkeypatch, settings, folder)
    assert (tmp_path / "Restored" / "exports" / "documents" / "f1" / "folder.zip").read_bytes() == b"PK zip"


def test_ac5_the_restored_folder_keeps_the_sites_settings(site, tmp_path, monkeypatch):
    _, settings, folder = site
    datafolder.update(folder, port=8123, site_name="our-archive")
    _restored(tmp_path, monkeypatch, settings, folder)
    restored = datafolder.load(tmp_path / "Restored", with_env=False)["server"]
    assert (restored["port"], restored["site_name"]) == (8123, "our-archive")


def test_ac5_the_command_restores_and_can_make_it_this_computers_folder(site, tmp_path, capsys):
    _, settings, folder = site
    _back_up(settings, folder, tmp_path / "B")
    code = cli.main(["restore", "--from", str(tmp_path / "B"), "--use", str(tmp_path / "Restored")])
    assert code == 0 and relocate.remembered() == (tmp_path / "Restored").resolve()


def test_ac5_a_named_snapshot_is_restored_rather_than_the_newest(site, tmp_path):
    _, settings, folder = site
    first = _back_up(settings, folder, tmp_path / "B")["snapshot"]
    _drafted(settings)
    _back_up(settings, folder, tmp_path / "B")
    report = restore.restore(tmp_path / "B", tmp_path / "Restored", snapshot=first)
    with db.connect(str(tmp_path / "Restored" / "incipit.db")) as conn:
        documents = conn.execute("SELECT COUNT(*) FROM new_documents").fetchone()[0]
    assert report["snapshot"] == first and documents == 0


# ---------- AC-6: what a backup lacks ----------


def test_ac6_a_scan_missing_from_the_backup_is_named_with_its_document(site, tmp_path):
    _, settings, folder = site
    document_id, folder_id = _drafted(settings, "F001AAA")
    _back_up(settings, folder, tmp_path / "B")
    [page] = list((tmp_path / "B" / "bucket" / "documents" / folder_id / document_id).glob("*"))
    page.unlink()
    report = restore.restore(tmp_path / "B", tmp_path / "Restored")
    text = (tmp_path / "Restored" / restore.REPORT_NAME).read_text()
    assert [m["document_id"] for m in report["missing"]] == [document_id] and "F001AAA" in text


def test_ac6_everything_else_is_restored(site, tmp_path):
    _, settings, folder = site
    first, folder_id = _drafted(settings, "F001AAA")
    second, second_folder = _drafted(settings, "F002AAA", "F002")
    _back_up(settings, folder, tmp_path / "B")
    for page in (tmp_path / "B" / "bucket" / "documents" / folder_id / first).glob("*"):
        page.unlink()
    restore.restore(tmp_path / "B", tmp_path / "Restored")
    assert list((tmp_path / "Restored" / "bucket" / "documents" / second_folder / second).glob("*"))


def test_ac6_a_complete_backup_reports_nothing_missing(site, tmp_path):
    _, settings, folder = site
    _drafted(settings)
    _back_up(settings, folder, tmp_path / "B")
    report = restore.restore(tmp_path / "B", tmp_path / "Restored")
    assert report["missing"] == [] and "Every file" in (tmp_path / "Restored" / restore.REPORT_NAME).read_text()


# ---------- AC-7: refusals ----------


def test_ac7_a_target_with_files_in_it_is_refused(site, tmp_path):
    _, settings, folder = site
    _back_up(settings, folder, tmp_path / "B")
    (tmp_path / "Busy").mkdir()
    (tmp_path / "Busy" / "note.txt").write_text("x")
    with pytest.raises(restore.RestoreError, match="not empty"):
        restore.restore(tmp_path / "B", tmp_path / "Busy")


def test_ac7_a_folder_without_snapshots_is_refused(tmp_path):
    (tmp_path / "Empty").mkdir()
    with pytest.raises(restore.RestoreError, match="no snapshot"):
        restore.restore(tmp_path / "Empty", tmp_path / "Restored")


def test_ac7_the_command_says_why_it_refused(tmp_path, capsys):
    (tmp_path / "Empty").mkdir()
    assert cli.main(["restore", "--from", str(tmp_path / "Empty"), str(tmp_path / "Restored")]) == 2
    assert "no snapshot" in capsys.readouterr().err


# ---------- AC-8: restoring from Settings ----------


def test_ac8_the_owner_asks_for_a_restore_and_the_site_restarts(site, tmp_path, monkeypatch):
    from incipit import backup_routes
    app, settings, folder = site
    relocate.remember(folder)
    summary = _back_up(settings, folder, folder / "backups")
    restarted = []
    app.state.request_restart = lambda: restarted.append(True)
    monkeypatch.setattr(backup_routes.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    got = signed_in(app, "olive").post("/api/backups/restore", json={"snapshot": summary["snapshot"],
                                                                     "target": str(tmp_path / "Restored")})
    assert got.json()["restarting"] and restarted and relocate.restore_pending()["target"] == str(tmp_path / "Restored")


def test_ac8_the_next_start_runs_from_the_restored_folder_and_leaves_the_old_one(site, tmp_path):
    _, settings, folder = site
    relocate.remember(folder)
    document_id, _ = _drafted(settings)
    summary = _back_up(settings, folder, folder / "backups")
    relocate.request_restore(folder, folder / "backups", summary["snapshot"], tmp_path / "Restored")
    before = (folder / "incipit.db").read_bytes()
    served = relocate.apply_pending(folder.resolve())
    assert served == (tmp_path / "Restored").resolve() and relocate.remembered() == served
    assert (folder / "incipit.db").read_bytes() == before


def test_ac8_a_failed_restore_keeps_the_site_where_it_was_and_says_why(site, tmp_path):
    _, settings, folder = site
    relocate.remember(folder)
    summary = _back_up(settings, folder, folder / "backups")
    relocate.request_restore(folder, folder / "backups", summary["snapshot"], tmp_path / "Restored")
    (tmp_path / "Restored").mkdir()
    (tmp_path / "Restored" / "stray.txt").write_text("x")
    assert relocate.apply_pending(folder.resolve()) == folder.resolve()
    assert "not empty" in relocate.failure_note(folder)


def test_ac8_an_admin_cannot_restore(site, tmp_path):
    app, _, _ = site
    assert signed_in(app, "paul").post("/api/backups/restore", json={"target": str(tmp_path / "R")}).status_code == 403


# ---------- AC-9: the tray ----------


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def test_ac9_the_tray_says_when_the_last_backup_happened():
    status = {"ok": True, "last_success_at": (NOW - timedelta(minutes=5)).isoformat()}
    assert tray.backup_label(status, NOW) == "Last backup: 5 min ago"


def test_ac9_the_tray_warns_when_the_last_backup_failed():
    status = {"ok": False, "detail": "drive E: is not mounted", "last_success_at": NOW.isoformat()}
    assert "drive E: is not mounted" in tray.backup_warning(status, NOW)


def test_ac9_the_tray_warns_after_a_day_without_a_good_backup():
    status = {"ok": True, "last_success_at": (NOW - timedelta(hours=30)).isoformat()}
    assert "over a day" in tray.backup_warning(status, NOW)


def test_ac9_a_recent_good_backup_raises_no_warning():
    status = {"ok": True, "last_success_at": (NOW - timedelta(hours=2)).isoformat()}
    assert tray.backup_warning(status, NOW) == ""


# ---------- regressions ----------


def test_a_data_folder_with_a_space_keeps_its_scans_inside_it(tmp_path):
    """Found by the M6 smoke: "Incipit Data" stored its scans in a sibling "Incipit%20Data", unbacked-up."""
    from incipit import storage
    folder = tmp_path / "Incipit Data"
    datafolder.initialise(folder)
    settings = datafolder.settings_for(folder, datafolder.load(folder))
    storage.BucketStorage(settings.bucket_uri).write_bytes("documents/f1/d1/0.jpg", b"scan")
    assert (folder / "bucket" / "documents" / "f1" / "d1" / "0.jpg").is_file()


def test_a_percent_encoded_file_address_reaches_the_real_folder(tmp_path):
    """Found by the M6 smoke: a file:// address with %20 was taken literally."""
    from incipit import storage
    bucket = tmp_path / "Incipit Data" / "bucket"
    bucket.mkdir(parents=True)
    storage.BucketStorage(bucket.as_uri()).write_bytes("records/f1/d1.json", b"{}")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Incipit Data"]


def test_a_failed_restore_leaves_no_half_built_folder(site, tmp_path, monkeypatch):
    """From the M6 review: a restore that failed partway left a folder that blocked the retry."""
    _, settings, folder = site
    _back_up(settings, folder, tmp_path / "B")

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(restore.backup, "copy_tree_additive", broken)
    with pytest.raises(OSError):
        restore.restore(tmp_path / "B", tmp_path / "Restored")
    assert not (tmp_path / "Restored").exists()


def test_two_backups_at_once_take_turns(site, tmp_path):
    """Release review P1: Back up now and the timer collided on one snapshot file in the same second."""
    import threading
    _, settings, folder = site
    _drafted(settings)
    errors = []

    def run():
        try:
            _back_up(settings, folder, tmp_path / "B")
        except Exception as exc:  # noqa: BLE001 - the test reports what went wrong
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == [] and all(backup.verify_snapshot(p)[0] for _, p in backup.list_snapshots(tmp_path / "B"))


def test_an_environment_set_destination_is_the_one_shown_and_used(site, tmp_path, monkeypatch):
    """Release review P2: the page and Back up now reloaded the settings without the environment's override."""
    app, _, _ = site
    monkeypatch.setenv("INCIPIT_BACKUP_DIR", str(tmp_path / "FromEnvironment"))
    shown = signed_in(app, "olive").get("/api/backups").json()
    assert shown["destination"] == str(tmp_path / "FromEnvironment") and "destination" in shown["set_by_environment"]


def test_an_environment_set_destination_cannot_be_changed_from_the_page(site, tmp_path, monkeypatch):
    app, _, _ = site
    monkeypatch.setenv("INCIPIT_BACKUP_DIR", str(tmp_path / "FromEnvironment"))
    got = signed_in(app, "olive").put("/api/backups", json={"destination": str(tmp_path / "Other")})
    assert got.status_code == 409 and "INCIPIT_BACKUP_DIR" in got.json()["detail"]
