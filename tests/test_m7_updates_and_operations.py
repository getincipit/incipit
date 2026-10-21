"""Covers M7 (docs/specs/M7-updates-and-operations.md): the release feed and version comparison, a
checked download handed to the installer, the installer's quiet update mode, the tray's log and
update entries, the support bundle, and the administrator's guide."""

import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from incipit import accounts, cli, datafolder, db, review_server, startup, support, tray, updates
from conftest import signed_in

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = b"MZ fake installer bytes" * 100


def _site(tmp_path, monkeypatch):
    folder = tmp_path / "Incipit Data"
    datafolder.initialise(folder)
    config = datafolder.load(folder)
    settings = datafolder.settings_for(folder, config)
    paths = datafolder.resolved_paths(folder, config)
    monkeypatch.setattr(startup, "run", lambda args: subprocess.CompletedProcess(args, 1, "", ""))
    app = review_server.create_app(settings, str(paths["exports"]), str(paths["logs"]))
    with db.connect(settings.db_path) as conn:
        accounts.create_owner(conn, "olive", "olive-password", "Olive")
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, settings, folder


@pytest.fixture
def site(tmp_path, monkeypatch):
    return _site(tmp_path, monkeypatch)


class Reply:
    def __init__(self, body=None, content=b"", status=200):
        self._body, self._content, self.status_code = body, content, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for start in range(0, len(self._content), size):
            yield self._content[start:start + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _feed(version="9.1.0", sha=None, size=None, notes="Faster drafting."):
    return {"version": version, "date": "2026-11-02", "notes": notes,
            "downloads": {updates.platform_key(): {"url": "https://example.org/Incipit-setup.exe",
                                                   "size": len(INSTALLER) if size is None else size,
                                                   "sha256": sha or hashlib.sha256(INSTALLER).hexdigest()}}}


def _web(monkeypatch, feed=None, installer=INSTALLER, fail=False):
    """The release feed and the installer as the internet would serve them."""
    import requests

    def fake_get(url, **kwargs):
        if fail:
            raise requests.ConnectionError("offline")
        return Reply(content=installer) if url.endswith(".exe") else Reply(body=feed)
    monkeypatch.setattr(updates.requests, "get", fake_get)
    monkeypatch.setenv(updates.ENV_FEED, "https://example.org/latest.json")


# ---------- AC-1: reading the feed ----------


def test_ac1_a_newer_release_is_reported_with_its_notes(monkeypatch):
    _web(monkeypatch, _feed())
    found = updates.check()
    assert (found["state"], found["release"]["version"], found["release"]["notes"]) == ("available", "9.1.0",
                                                                                       "Faster drafting.")


def test_ac1_the_same_version_is_up_to_date(monkeypatch):
    _web(monkeypatch, _feed(version=updates.current_version()))
    assert updates.check()["state"] == "up_to_date"


def test_ac1_an_unreachable_feed_is_said_plainly(monkeypatch):
    _web(monkeypatch, fail=True)
    found = updates.check()
    assert found["state"] == "unreachable" and "offline" in found["error"]


def test_ac1_a_build_without_a_feed_says_so(monkeypatch):
    monkeypatch.delenv(updates.ENV_FEED, raising=False)
    monkeypatch.setattr(updates, "asset_directory", lambda name: Path("no-such-file"))
    assert updates.check()["state"] == "not_configured"


def test_ac1_the_site_checks_once_a_day_by_itself(monkeypatch):
    import threading
    checks, stop = [], threading.Event()
    monkeypatch.setattr(updates, "check", lambda http_get=None: checks.append(1) or stop.set())
    updates.check_forever(stop, first_delay=0)
    assert checks == [1] and updates.CHECK_EVERY_HOURS == 24


def test_ac1_the_build_carries_the_feed_address():
    script = (ROOT / "scripts" / "build_app.py").read_text(encoding="utf-8")
    assert 'feed_file.write_text(os.environ.get("INCIPIT_UPDATE_FEED"' in script and "{feed_file}{separator}." in script


# ---------- AC-2: comparing versions ----------


@pytest.mark.parametrize("candidate, current, newer", [
    ("0.10.0", "0.9.3", True), ("0.9.3", "0.10.0", False), ("1.0", "0.99.99", True), ("0.2.0", "0.2.0", False),
    ("v0.3.1", "0.3.0", True), ("0.3.0.1", "0.3.0", True)])
def test_ac2_versions_compare_as_numbers(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


def test_ac2_a_prerelease_is_never_offered():
    assert updates.is_newer("9.0.0-beta.1", "0.1.0") is False


# ---------- AC-3: the download and the installer ----------


def test_ac3_a_matching_download_is_kept(monkeypatch, tmp_path):
    _web(monkeypatch, _feed())
    path = updates.download(updates.check()["release"], folder=tmp_path)
    assert path.read_bytes() == INSTALLER


def test_ac3_a_download_with_the_wrong_checksum_is_deleted_and_refused(monkeypatch, tmp_path):
    _web(monkeypatch, _feed(sha="0" * 64))
    release = updates.check()["release"]
    with pytest.raises(updates.UpdateError, match="did not match"):
        updates.download(release, folder=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ac3_a_download_of_the_wrong_size_is_refused(monkeypatch, tmp_path):
    _web(monkeypatch, _feed(size=12))
    with pytest.raises(updates.UpdateError):
        updates.download(updates.check()["release"], folder=tmp_path)


def test_ac3_the_installer_is_started_silently_in_update_mode(tmp_path):
    started = []
    updates.start_installer(tmp_path / "Incipit-9.1.0-setup.exe", launcher=lambda args, **kw: started.append(args))
    assert started[0][1:3] == ["/SILENT", "/UPDATE"]


def test_ac3_installing_stops_the_site_for_the_installer(site, monkeypatch):
    app, _, _ = site
    _web(monkeypatch, _feed())
    started, stopped = [], []
    monkeypatch.setattr(updates, "start_installer", lambda path, launcher=None: started.append(path))
    app.state.stop_for_update = lambda: stopped.append(True)
    from incipit import update_routes
    monkeypatch.setattr(update_routes.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    got = signed_in(app, "olive").post("/api/updates/install")
    assert got.json()["installing"] == "9.1.0" and started and stopped


def test_ac3_a_site_stopped_for_an_update_is_not_started_again(tmp_path, monkeypatch):
    folder = tmp_path / "Data"
    cli.main(["init-folder", str(folder)])
    monkeypatch.setattr(cli, "run_server", lambda *a, **k: "update")
    monkeypatch.setenv(cli.SUPERVISED, "1")
    assert cli.main(["serve", "--data-folder", str(folder)]) == cli.UPDATE_EXIT_CODE


def test_ac3_the_tray_quits_when_the_site_stops_for_an_update(tmp_path):
    quit_ = []
    site = tray.Site(tmp_path, on_update=lambda: quit_.append(True))
    site._watch(type("P", (), {"wait": lambda self: cli.UPDATE_EXIT_CODE})())
    assert quit_ == [True]


# ---------- AC-4: who may update ----------


def test_ac4_an_admin_sees_the_update_but_cannot_install_it(site, monkeypatch):
    app, _, _ = site
    _web(monkeypatch, _feed())
    paul = signed_in(app, "paul")
    assert paul.post("/api/updates/check").json()["state"] == "available"
    assert paul.post("/api/updates/install").status_code == 403


def test_ac4_a_reviewer_sees_nothing_of_updates(site):
    app, _, _ = site
    assert signed_in(app, "john").get("/api/updates").status_code == 403


# ---------- AC-5: the installer's update mode (the install itself is verified outside the suite) ----------


def _script():
    return (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")


def test_ac5_an_update_makes_no_data_folder():
    line = next(l for l in _script().splitlines() if "init-folder" in l and l.startswith("Filename"))
    assert "Check: not IsUpdate" in line


def test_ac5_an_update_skips_the_pages_and_the_firewall():
    script = _script()
    assert "function ShouldSkipPage" in script and "Result := LoginPage.Values[3] and not IsUpdate;" in script


def test_ac5_an_update_starts_the_tray_again_quietly():
    assert 'Parameters: "tray --quiet"; Flags: nowait; Check: IsUpdate' in _script()


def test_ac5_a_manual_upgrade_offers_the_data_folder_in_use():
    script = _script()
    assert "function RememberedFolder" in script and "location.toml" in script


def test_ac5_the_tray_opens_no_browser_when_quiet(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)
    source = (ROOT / "src" / "incipit" / "tray.py").read_text(encoding="utf-8")
    assert "if not quiet:" in source and opened == []


# ---------- AC-6: the tray's log ----------


def test_ac6_the_tray_keeps_its_own_rolling_log(tmp_path):
    import logging
    datafolder.initialise(tmp_path / "Data")
    before = list(logging.getLogger().handlers)
    try:
        tray.log_to(tmp_path / "Data", datafolder.load(tmp_path / "Data"))
        logging.getLogger("incipit.tray").warning("tray started")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "tray started" in (tmp_path / "Data" / "logs" / "tray.log").read_text(encoding="utf-8")
    finally:
        for handler in list(logging.getLogger().handlers):
            if handler not in before:
                logging.getLogger().removeHandler(handler)
                handler.close()


# ---------- AC-7: the support bundle ----------


def _bundle(folder) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(support.build(folder)))


def test_ac7_the_bundle_holds_versions_settings_status_logs_jobs_and_calls(site):
    _, _, folder = site
    (folder / "logs" / "incipit.log").write_text("site started\n", encoding="utf-8")
    names = set(_bundle(folder).namelist())
    assert {"README.txt", "versions.json", "incipit.toml", "backup-status.json", "logs/incipit.log", "jobs.json",
            "model-calls.json"} <= names


def test_ac7_the_bundle_holds_no_database_and_no_scans(site):
    _, _, folder = site
    (folder / "bucket" / "documents").mkdir(parents=True, exist_ok=True)
    (folder / "bucket" / "documents" / "page.jpg").write_bytes(b"scan")
    names = _bundle(folder).namelist()
    assert not any(n.endswith((".db", ".jpg")) or n.startswith("bucket") for n in names)


def test_ac7_a_key_in_the_settings_file_is_blanked(site):
    _, _, folder = site
    path = folder / "incipit.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('openrouter_api_key = ""',
                                                             'openrouter_api_key = "sk-or-v1-secret123456"'),
                    encoding="utf-8")
    assert "secret123456" not in _bundle(folder).read("incipit.toml").decode()


@pytest.mark.parametrize("secret", ["sk-or-v1-abcdef1234567890", "Authorization: Bearer abcdefgh12345678",
                                    "open /setup?token=Y_gOTkHyghEmIWu91fuHu2N", "olive.archivist@example.org"])
def test_ac7_keys_tokens_and_addresses_in_the_logs_are_masked(site, secret):
    _, _, folder = site
    (folder / "logs" / "incipit.log").write_text(f"something happened: {secret}\n", encoding="utf-8")
    text = _bundle(folder).read("logs/incipit.log").decode()
    assert "masked" in text and secret.split()[-1].split("=")[-1] not in text


def test_ac7_the_owner_and_admins_download_the_bundle_and_reviewers_cannot(site):
    app, _, _ = site
    got = signed_in(app, "paul").get("/api/support/bundle")
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    assert signed_in(app, "john").get("/api/support/bundle").status_code == 403


def test_ac7_the_command_writes_the_bundle(site, tmp_path):
    _, _, folder = site
    out = tmp_path / "bundle.zip"
    assert cli.main(["support-bundle", "--data-folder", str(folder), "--out", str(out)]) == 0
    assert "versions.json" in zipfile.ZipFile(out).namelist()


# ---------- AC-8: the guide ----------


@pytest.mark.parametrize("section", ["## Where everything is", "## Backups", "## A restore drill",
                                     "## Moving to a new computer", "## Changing the port or the address",
                                     "## Updating", "support bundle", "## Uninstalling"])
def test_ac8_the_guide_covers_what_an_administrator_needs(section):
    assert section in (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")


# ---------- AC-9: the tray and the page ----------


def test_ac9_the_tray_offers_a_check_and_an_install_when_one_is_ready():
    actions = {"check": lambda: None, "install": lambda: None}
    labels = [label for label, _, _ in tray.update_entries({"state": "available", "release": {"version": "9.1.0"}}, actions)]
    assert labels == ["Check for updates", "Install update 9.1.0"]


def test_ac9_the_tray_offers_only_the_check_when_up_to_date():
    labels = [label for label, _, _ in tray.update_entries({"state": "up_to_date"}, {"check": None, "install": None})]
    assert labels == ["Check for updates"]


def test_ac9_settings_has_an_updates_tab_mounted_through_its_signal():
    index = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "app" / "static" / "updates.js").read_text(encoding="utf-8")
    assert 'data-settings-tab="updates"' in index and "remount(container)" in source


# ---------- the release script and the updater agree ----------


def test_the_feed_the_release_script_writes_is_the_one_the_updater_reads(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import release
    installer = tmp_path / "Incipit-9.2.0-setup.exe"
    installer.write_bytes(INSTALLER)
    written = release.feed("9.2.0", "Notes.", installer, "parthaped/incipit")
    _web(monkeypatch, written)
    found = updates.check()
    path = updates.download(found["release"], folder=tmp_path / "downloaded")
    assert found["state"] == "available" and path.read_bytes() == INSTALLER


def test_installed_copies_read_the_feed_through_githubs_latest_release_link():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import release
    assert release.feed_address("parthaped/incipit") == \
        "https://github.com/parthaped/incipit/releases/latest/download/latest.json"
