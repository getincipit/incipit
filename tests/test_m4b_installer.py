"""Covers M4b (docs/specs/M4b-installer-data-folder-and-access.md): the client's data folder and its
incipit.toml, the serve and init-folder commands, Settings → Access and its port test, start at login,
the icon set, the packaged build's asset lookup, and the tray's menu."""

import socket
import subprocess
import tomllib
from pathlib import Path

import pytest
from PIL import Image

from incipit import accounts, assets, cli, datafolder, host, review_server, site_settings, startup, tray
from conftest import signed_in

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _site(tmp_path, monkeypatch, *, port=8010):
    """An app run from a fresh data folder, with Olive the owner, Paul an admin and John a reviewer."""
    folder = tmp_path / "Incipit"
    datafolder.initialise(folder, port=port)
    settings = datafolder.settings_for(folder, datafolder.load(folder))
    paths = datafolder.resolved_paths(folder, datafolder.load(folder))
    monkeypatch.setattr(startup, "run", lambda args: subprocess.CompletedProcess(args, 1, "", ""))
    app = review_server.create_app(settings, str(paths["exports"]), str(paths["logs"]))
    from incipit import db
    with db.connect(settings.db_path) as conn:
        accounts.create_owner(conn, "olive", "olive-password", "Olive")
        accounts.create_user(conn, "paul", "paul-password", "Paul", "admin")
        accounts.create_user(conn, "john", "john-password", "John", "reviewer")
    return app, settings, folder


@pytest.fixture
def site(tmp_path, monkeypatch):
    return _site(tmp_path, monkeypatch)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- AC-1: init-folder ----------


def test_ac1_init_folder_makes_the_layout_and_the_file(tmp_path):
    folder = tmp_path / "Incipit"
    assert cli.main(["init-folder", str(folder)]) == 0
    made = sorted(p.name for p in folder.iterdir())
    assert made == sorted(["backups", "bucket", "exports", "incipit.db", "incipit.toml", "logs",
                           site_settings.BOOTSTRAP_FILENAME])


def test_ac1_the_file_holds_the_defaults(tmp_path):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder), "--port", "8123"])
    written = tomllib.loads((folder / "incipit.toml").read_text(encoding="utf-8"))
    assert (written["server"]["port"], written["server"]["host"], written["paths"]["database"]) == (8123, "0.0.0.0", "incipit.db")


def test_ac1_running_init_folder_again_changes_nothing(tmp_path):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder), "--port", "8123"])
    (folder / "incipit.toml").write_text((folder / "incipit.toml").read_text(encoding="utf-8").replace("8123", "8200"),
                                         encoding="utf-8")
    cli.main(["init-folder", str(folder), "--port", "8999"])
    assert datafolder.load(folder)["server"]["port"] == 8200


def test_ac1_an_unwritable_folder_is_refused_with_its_name(tmp_path, monkeypatch):
    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(datafolder.ConfigError, match="cannot be written"):
        datafolder.initialise(tmp_path / "locked")


# ---------- AC-2: serve reads the folder ----------


def test_ac2_serve_runs_the_site_from_the_folders_file(tmp_path, monkeypatch):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder), "--port", "8124"])
    seen = {}
    monkeypatch.setattr(cli, "run_server", lambda settings, export_dir, **kw: seen.update(settings=settings, export_dir=export_dir, **kw))
    assert cli.main(["serve", "--data-folder", str(folder)]) == 0
    assert (seen["port"], seen["host"]) == (8124, "0.0.0.0")
    assert Path(seen["settings"].db_path) == (folder / "incipit.db").resolve()
    assert Path(seen["export_dir"]) == (folder / "exports").resolve() and Path(seen["backup_dir"]) == (folder / "backups").resolve()


def test_ac2_the_folder_can_come_from_the_environment(tmp_path, monkeypatch):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder)])
    monkeypatch.setenv(datafolder.ENV_FOLDER, str(folder))
    assert datafolder.resolve() == folder.resolve()


def test_ac2_an_environment_variable_overrides_one_value(tmp_path, monkeypatch):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder)])
    monkeypatch.setenv("INCIPIT_PORT", "9001")
    assert datafolder.load(folder)["server"]["port"] == 9001


def test_ac2_a_bad_value_is_named_with_its_line(tmp_path):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder)])
    path = folder / "incipit.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("port = 8010", "port = 99999"), encoding="utf-8")
    with pytest.raises(datafolder.ConfigError, match=r"\[server\]\.port"):
        datafolder.load(folder)


def test_ac2_a_folder_without_a_file_is_refused(tmp_path):
    with pytest.raises(datafolder.ConfigError, match="init-folder"):
        datafolder.resolve(str(tmp_path / "nowhere"))


# ---------- AC-3: the wizard ----------


def test_ac3_a_fresh_folder_has_a_setup_token(tmp_path):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder)])
    assert site_settings.bootstrap_path(str(folder / "incipit.db")).is_file()


def test_ac3_the_wizard_ends_with_access(site):
    app, _, _ = site
    steps = [s["id"] for s in signed_in(app, "olive").get("/api/setup").json()["steps"]]
    assert steps[-1] == "access" and steps.index("people") < steps.index("access")


# ---------- AC-4: what the Access page reads ----------


def test_ac4_access_reports_the_addresses_port_links_and_folder(site, monkeypatch):
    app, settings, folder = site
    monkeypatch.setattr(host, "lan_addresses", lambda: ["192.168.1.20"])
    got = signed_in(app).get("/api/access").json()
    assert got["port"] == settings.port and got["addresses"] == ["192.168.1.20"]
    assert got["links"][0] == {"address": "192.168.1.20", "link": "http://192.168.1.20:8010/", "kind": "network"}
    assert Path(got["data_folder"]) == folder.resolve() and got["private"] is False


def test_ac4_a_reviewer_is_refused_the_access_page(site):
    app, _, _ = site
    assert signed_in(app, "john").get("/api/access").status_code == 403


# ---------- AC-5: the port test ----------


def test_ac5_a_port_with_a_listener_is_reported_reachable():
    port = _free_port()
    with socket.socket() as server:
        server.bind(("127.0.0.1", port))
        server.listen()
        assert host.probe("127.0.0.1", port)["reachable"] is True


def test_ac5_a_port_nothing_listens_on_is_reported_as_such():
    result = host.probe("127.0.0.1", _free_port())
    assert result["reachable"] is False and result["detail"]


def test_ac5_the_route_tests_every_address_and_localhost(site, monkeypatch):
    app, _, _ = site
    monkeypatch.setattr(host, "lan_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(host, "probe", lambda address, port, timeout=1.0: {"address": address, "reachable": address == "localhost", "detail": "x"})
    results = signed_in(app).post("/api/access/test").json()["results"]
    assert [(r["address"], r["reachable"]) for r in results] == [("192.168.1.20", False), ("localhost", True)]


# ---------- AC-6: the owner changes the port ----------


def test_ac6_the_owner_changes_the_port_for_the_next_start(site):
    app, _, folder = site
    got = signed_in(app, "olive").put("/api/access", json={"port": 8300}).json()
    assert (got["restart_needed"], got["pending_port"]) == (True, 8300)
    assert datafolder.load(folder, with_env=False)["server"]["port"] == 8300


def test_ac6_an_admin_who_is_not_the_owner_cannot_change_the_port(site):
    app, _, _ = site
    assert signed_in(app).put("/api/access", json={"port": 8300}).status_code == 403


def test_ac6_a_port_out_of_range_is_refused(site):
    app, _, _ = site
    assert signed_in(app, "olive").put("/api/access", json={"port": 70000}).status_code == 400


# ---------- AC-7: start at login ----------


def test_ac7_windows_registers_a_logon_task_that_runs_the_tray(tmp_path):
    ran = []
    startup.register(tmp_path, system="Windows", runner=lambda args: ran.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    command = ran[0]
    assert command[:5] == ["schtasks", "/Create", "/SC", "ONLOGON", "/TN"] and "tray" in command[command.index("/TR") + 1]


def test_ac7_windows_status_and_removal_go_through_the_runner(tmp_path):
    ran = []

    def runner(args):
        ran.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    registered = startup.status(system="Windows", runner=runner)
    startup.unregister(system="Windows", runner=runner)
    assert registered is True and ran[1][:2] == ["schtasks", "/Delete"]


def test_ac7_linux_writes_and_removes_an_autostart_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    startup.register(tmp_path / "Incipit", system="Linux")
    on = startup.status(system="Linux")
    startup.unregister(system="Linux")
    assert (on, startup.status(system="Linux")) == (True, False)


def test_ac7_the_page_turns_start_at_login_on_and_records_it(site, monkeypatch):
    app, _, folder = site
    ran = []
    monkeypatch.setattr(startup, "run", lambda args: ran.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(startup, "platform", type("P", (), {"system": staticmethod(lambda: "Windows")}))
    signed_in(app, "olive").put("/api/access", json={"start_at_login": True})
    assert any(a[:2] == ["schtasks", "/Create"] for a in ran)
    assert datafolder.load(folder, with_env=False)["server"]["start_at_login"] is True


# ---------- AC-8: the icon set ----------


def test_ac8_the_icon_set_has_every_size_square_and_not_blank(tmp_path):
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import build_icons

    build_icons.build(out_dir=tmp_path)
    assert build_icons.check(tmp_path) == []
    with Image.open(tmp_path / "incipit-512.png") as big:
        assert big.size == (512, 512)


def test_ac8_the_ico_holds_several_sizes(tmp_path):
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import build_icons

    build_icons.build(out_dir=tmp_path)
    with Image.open(tmp_path / "incipit.ico") as ico:
        sizes = ico.info.get("sizes") or set()
    assert {(16, 16), (32, 32), (256, 256)} <= set(sizes)


# ---------- AC-9: a packaged build finds its pages ----------


def test_ac9_a_frozen_build_finds_the_static_files_beside_the_program(tmp_path, monkeypatch):
    (tmp_path / "frozen" / "app" / "static").mkdir(parents=True)
    monkeypatch.setattr(assets, "SOURCE_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(assets, "FROZEN_ROOT", tmp_path / "frozen")
    assert assets.asset_directory("app/static") == tmp_path / "frozen" / "app" / "static"


def test_ac9_a_checkout_still_prefers_its_own_files():
    assert assets.asset_directory("app/static") == ROOT / "app" / "static"


# ---------- AC-11: the tray's menu ----------


def _menu(running):
    class Fake:
        folder = Path("C:/Incipit")
    site = Fake()
    site.running = running
    calls = []
    actions = {k: (lambda *a, k=k: calls.append(k)) for k in ("open", "folder", "start", "stop", "backup", "copy", "quit")}
    return tray.menu_items(site, {"server": {"port": 8010}}, actions), calls


def test_ac11_the_tray_offers_its_entries_in_order(monkeypatch):
    monkeypatch.setattr(host, "lan_addresses", lambda: ["192.168.1.20"])
    items, _ = _menu(running=True)
    assert [label for label, _, _ in items] == ["Open Incipit", "Open data folder", "Stop the site", "Back up now",
                                                 "Share: http://192.168.1.20:8010/", "Last backup: none yet", "Copy the team's link", "Quit"]


def test_ac11_a_stopped_site_offers_start_and_cannot_be_opened(monkeypatch):
    monkeypatch.setattr(host, "lan_addresses", lambda: [])
    items, calls = _menu(running=False)
    labels = {label: (act, enabled) for label, act, enabled in items}
    labels["Start the site"][0]()
    assert labels["Open Incipit"][1] is False and calls == ["start"]


# ---------- regressions ----------


def test_the_packaged_program_starts_without_a_console(monkeypatch):
    """Reported from the first installed run: a windowed build has no stdout, and uvicorn's logging
    set-up failed on `None.isatty()` before the site could start."""
    import uvicorn

    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    cli.ensure_console_streams()
    uvicorn.Config(app="incipit.review_server:create_app")


# ---------- AC-13: the site's local name ----------


def test_ac13_a_typed_name_becomes_a_dns_label():
    from incipit import localname
    assert localname.normalise("  Our Archive!  ") == "our-archive" and localname.normalise("our-archive.local") == "our-archive"


def test_ac13_a_name_without_a_letter_or_digit_is_refused():
    from incipit import localname
    with pytest.raises(ValueError, match="1 to 63"):
        localname.normalise("---")


def test_ac13_init_folder_records_the_name(tmp_path):
    folder = tmp_path / "Incipit"
    cli.main(["init-folder", str(folder), "--site-name", "Our Archive"])
    assert datafolder.load(folder)["server"]["site_name"] == "our-archive"


def test_ac13_the_name_is_announced_for_the_hosts_addresses(monkeypatch):
    from incipit import localname
    registered = []

    class FakeZeroconf:
        def register_service(self, info, allow_name_change=False):
            registered.append(info)

        def unregister_service(self, info):
            pass

        def close(self):
            pass
    monkeypatch.setattr(host, "lan_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(localname, "resolves_elsewhere", lambda name, own: [])
    announcer = localname.Announcer("our-archive", 8010, zeroconf_factory=FakeZeroconf).start()
    info = registered[0]
    assert announcer.announcing and info.server == "our-archive.local." and info.port == 8010
    assert info.parsed_addresses() == ["192.168.1.20"]


def test_ac13_a_name_another_computer_answers_is_not_announced(monkeypatch):
    from incipit import localname
    monkeypatch.setattr(host, "lan_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(localname, "resolves_elsewhere", lambda name, own: ["192.168.1.99"])
    announcer = localname.Announcer("our-archive", 8010, zeroconf_factory=lambda: pytest.fail("must not register")).start()
    assert not announcer.announcing and "192.168.1.99" in announcer.error


def test_ac13_the_name_is_the_first_link_to_share():
    links = host.links(8010, ["192.168.1.20"], "our-archive")
    assert links[0] == {"address": "our-archive.local", "link": "http://our-archive.local:8010/", "kind": "site"}


def test_ac13_the_owner_changes_the_name_at_once(site, monkeypatch):
    from incipit import localname
    app, settings, folder = site
    announced = []
    monkeypatch.setattr(localname, "announce", lambda name, port, title="Incipit": announced.append((name, port)))
    got = signed_in(app, "olive").put("/api/access", json={"site_name": "Our Archive"})
    assert got.status_code == 200 and announced == [("our-archive", settings.port)]
    assert datafolder.load(folder, with_env=False)["server"]["site_name"] == "our-archive"


def test_ac13_a_bad_name_is_refused_by_the_page(site):
    app, _, _ = site
    assert signed_in(app, "olive").put("/api/access", json={"site_name": "***"}).status_code == 400


# ---------- AC-14: the location file ----------


def test_ac14_init_folder_records_the_folder_in_the_location_file(tmp_path):
    from incipit import relocate
    assert cli.main(["init-folder", str(tmp_path / "Data")]) == 0
    assert relocate.remembered() == (tmp_path / "Data").resolve()


def test_ac14_the_program_finds_the_folder_with_no_path_given(tmp_path, monkeypatch):
    from incipit import relocate
    datafolder.initialise(tmp_path / "Data")
    relocate.remember(tmp_path / "Data")
    monkeypatch.delenv(datafolder.ENV_FOLDER, raising=False)
    monkeypatch.chdir(tmp_path)
    assert datafolder.resolve(None) == (tmp_path / "Data").resolve()


def test_ac14_a_remembered_folder_that_is_gone_is_named(tmp_path, monkeypatch):
    from incipit import relocate
    relocate.remember(tmp_path / "Unplugged")
    monkeypatch.delenv(datafolder.ENV_FOLDER, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(datafolder.ConfigError, match="Unplugged"):
        datafolder.resolve(None)


def test_ac14_the_logon_entry_names_no_folder(tmp_path):
    assert "--data-folder" not in startup.serve_command(tmp_path)


def test_ac14_the_installer_shortcuts_run_the_tray_without_a_folder():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    shortcuts = [line for line in script.splitlines() if line.startswith("Name:") and "{#AppExe}" in line]
    assert shortcuts and all('Parameters: "tray"' in line for line in shortcuts)


def test_ac14_the_installer_offers_to_make_a_new_folder():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "False, 'Incipit Data');" in script


# ---------- AC-15: moving the data folder ----------


def _remembered_site(tmp_path, monkeypatch):
    from incipit import relocate
    app, settings, folder = _site(tmp_path, monkeypatch)
    relocate.remember(folder)
    return app, settings, folder


def test_ac15_a_move_to_a_folder_with_files_in_it_is_refused(tmp_path, monkeypatch):
    app, _, _ = _remembered_site(tmp_path, monkeypatch)
    (tmp_path / "Busy").mkdir()
    (tmp_path / "Busy" / "letter.txt").write_text("x")
    got = signed_in(app, "olive").post("/api/access/move", json={"target": str(tmp_path / "Busy")})
    assert got.status_code == 400 and "not empty" in got.json()["detail"]


def test_ac15_a_move_inside_the_current_folder_is_refused(tmp_path, monkeypatch):
    app, _, folder = _remembered_site(tmp_path, monkeypatch)
    assert signed_in(app, "olive").post("/api/access/move", json={"target": str(folder / "inner")}).status_code == 400


def test_ac15_an_admin_who_is_not_the_owner_cannot_move_the_data(tmp_path, monkeypatch):
    app, _, _ = _remembered_site(tmp_path, monkeypatch)
    assert signed_in(app, "paul").post("/api/access/move", json={"target": str(tmp_path / "New")}).status_code == 403


def test_ac15_a_move_is_recorded_and_restarts_the_site(tmp_path, monkeypatch):
    from incipit import access_routes, relocate
    app, _, _ = _remembered_site(tmp_path, monkeypatch)
    restarted = []
    app.state.request_restart = lambda: restarted.append(True)
    monkeypatch.setattr(access_routes.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    got = signed_in(app, "olive").post("/api/access/move", json={"target": str(tmp_path / "New")})
    assert got.json()["restarting"] and restarted and relocate.pending() == (tmp_path / "New").resolve()


def test_ac15_the_next_start_copies_everything_to_the_new_folder(tmp_path, monkeypatch):
    from incipit import relocate
    _, _, folder = _remembered_site(tmp_path, monkeypatch)
    (folder / "bucket" / "scan.jpg").write_bytes(b"\xff\xd8 scan")
    relocate.request(folder, tmp_path / "New")
    served = relocate.apply_pending(folder.resolve())
    assert served == (tmp_path / "New").resolve() and (served / "bucket" / "scan.jpg").read_bytes() == b"\xff\xd8 scan"


def test_ac15_after_a_move_the_location_names_the_new_folder(tmp_path, monkeypatch):
    from incipit import relocate
    _, _, folder = _remembered_site(tmp_path, monkeypatch)
    relocate.request(folder, tmp_path / "New")
    relocate.apply_pending(folder.resolve())
    assert relocate.remembered() == (tmp_path / "New").resolve() and relocate.pending() is None


def test_ac15_the_old_folder_is_kept_but_never_served_again(tmp_path, monkeypatch):
    from incipit import relocate
    _, _, folder = _remembered_site(tmp_path, monkeypatch)
    relocate.request(folder, tmp_path / "New")
    relocate.apply_pending(folder.resolve())
    assert (folder / "incipit.db").is_file() and datafolder.resolve(str(folder)) == (tmp_path / "New").resolve()


def test_ac15_a_copy_that_fails_keeps_the_old_folder_and_says_why(tmp_path, monkeypatch):
    from incipit import relocate
    _, _, folder = _remembered_site(tmp_path, monkeypatch)
    relocate.request(folder, tmp_path / "New")

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(relocate.shutil, "copytree", broken)
    assert relocate.apply_pending(folder.resolve()) == folder.resolve()
    assert relocate.pending() is None and "No space left" in relocate.failure_note(folder)


def test_ac15_the_owner_cancels_a_move_not_yet_made(tmp_path, monkeypatch):
    from incipit import relocate
    app, _, folder = _remembered_site(tmp_path, monkeypatch)
    relocate.request(folder, tmp_path / "New")
    assert signed_in(app, "olive").delete("/api/access/move").json()["move_to"] == "" and relocate.pending() is None


def test_ac15_serve_carries_out_a_move_before_opening_the_database(tmp_path, monkeypatch):
    from incipit import relocate
    folder = tmp_path / "Data"
    cli.main(["init-folder", str(folder)])
    relocate.request(folder, tmp_path / "New")
    served = []
    monkeypatch.setattr(cli, "run_server", lambda settings, *a, **k: served.append(settings.data_folder) or False)
    monkeypatch.delenv(datafolder.ENV_FOLDER, raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serve"]) == 0 and served == [str((tmp_path / "New").resolve())]


def test_ac15_a_supervised_site_hands_its_restart_to_the_tray(tmp_path, monkeypatch):
    folder = tmp_path / "Data"
    cli.main(["init-folder", str(folder)])
    monkeypatch.setattr(cli, "run_server", lambda *a, **k: True)
    monkeypatch.setenv(cli.SUPERVISED, "1")
    assert cli.main(["serve", "--data-folder", str(folder)]) == cli.RESTART_EXIT_CODE


def test_ac15_the_tray_starts_the_site_again_from_the_new_folder(tmp_path, monkeypatch):
    from incipit import relocate
    relocate.remember(tmp_path / "New")
    site = tray.Site(tmp_path / "Old")
    started = []
    monkeypatch.setattr(site, "start", lambda: started.append(site.folder))
    site._watch(type("P", (), {"wait": lambda self: cli.RESTART_EXIT_CODE})())
    assert started == [tmp_path / "New"]


# ---------- AC-16: opening the port on the router ----------


DESCRIPTION_XML = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device>
<deviceList><device><serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/upnp/control/WANIPConn1</controlURL></service></serviceList></device></deviceList></device></deviceList>
</device></root>"""


def _soap_error(url, code, text=""):
    import io
    import urllib.error
    body = f"<errorCode>{code}</errorCode><errorDescription>{text}</errorDescription>".encode()
    return urllib.error.HTTPError(url, 500, "error", {}, io.BytesIO(body))


class FakeRouter:
    """A gateway answering SOAP the way a home router does."""

    def __init__(self, external="93.184.216.34", refuse=None, permanent_only=False):
        self.external, self.refuse, self.permanent_only, self.calls = external, refuse, permanent_only, []

    def __call__(self, url, data=None, headers=None, timeout=5):
        if data is None:
            return DESCRIPTION_XML
        action = headers["SOAPAction"].split("#")[1].rstrip('"')
        body = data.decode()
        self.calls.append((action, body))
        if action == self.refuse:
            raise _soap_error(url, 718, "ConflictInMappingEntry")
        if self.permanent_only and action == "AddPortMapping" and "<NewLeaseDuration>0<" not in body:
            raise _soap_error(url, 725, "OnlyPermanentLeasesSupported")
        if action == "GetExternalIPAddress":
            return f"<r><NewExternalIPAddress>{self.external}</NewExternalIPAddress></r>".encode()
        return b"<r/>"


def _gateway(router):
    from incipit import portmap
    found = portmap.control_url("http://192.168.1.1:5000/desc.xml", DESCRIPTION_XML)
    return portmap.Gateway(found[0], found[1], "192.168.1.20", fetch=router)


def _forwarder(router):
    from incipit import portmap
    forwarder = portmap.Forwarder(8010, finder=lambda: _gateway(router) if router else None)
    forwarder.open_once()
    return forwarder


def test_ac16_the_routers_description_gives_its_control_address():
    from incipit import portmap
    assert portmap.control_url("http://192.168.1.1:5000/desc.xml", DESCRIPTION_XML) == (
        "http://192.168.1.1:5000/upnp/control/WANIPConn1", "urn:schemas-upnp-org:service:WANIPConnection:1")


def test_ac16_the_port_is_forwarded_to_this_computer():
    router = FakeRouter()
    _forwarder(router)
    added = next(body for action, body in router.calls if action == "AddPortMapping")
    assert "<NewExternalPort>8010</NewExternalPort>" in added and "<NewInternalClient>192.168.1.20</NewInternalClient>" in added


def test_ac16_an_open_port_gives_the_address_to_share():
    status = _forwarder(FakeRouter()).status()
    assert status["state"] == "open" and status["link"] == "https://93.184.216.34:8010/"


def test_ac16_a_router_behind_the_providers_shared_address_is_reported():
    status = _forwarder(FakeRouter(external="100.72.4.9")).status()
    assert status["state"] == "double_nat" and status["link"] == ""


def test_ac16_a_router_behind_another_router_is_reported():
    assert _forwarder(FakeRouter(external="192.168.0.4")).state == "double_nat"


def test_ac16_no_router_answering_is_reported():
    assert _forwarder(None).state == "no_router"


def test_ac16_a_refusal_is_reported_with_the_routers_reason():
    forwarder = _forwarder(FakeRouter(refuse="AddPortMapping"))
    assert forwarder.state == "refused" and "ConflictInMappingEntry" in forwarder.error


def test_ac16_a_router_taking_only_permanent_mappings_gets_one():
    assert _gateway(FakeRouter(permanent_only=True)).add(8010) == 0


def test_ac16_stopping_removes_the_forwarding():
    router = FakeRouter()
    _forwarder(router).stop()
    assert router.calls[-1][0] == "DeletePortMapping"


def test_ac16_init_folder_records_forwarding(tmp_path):
    cli.main(["init-folder", str(tmp_path / "Data"), "--forward-port"])
    assert datafolder.load(tmp_path / "Data", with_env=False)["server"]["forward_port"] is True


def test_ac16_the_owner_turns_forwarding_on_from_the_page(site, monkeypatch):
    from incipit import portmap, tls
    app, settings, folder = site
    monkeypatch.setitem(tls.serving, "port", settings.https_port)
    asked = []
    monkeypatch.setattr(portmap, "forward", lambda port, finder=None: asked.append(port))
    got = signed_in(app, "olive").put("/api/access", json={"forward_port": True})
    assert got.json()["internet"]["enabled"] and asked == [settings.https_port]


def test_ac16_turning_forwarding_on_is_kept_for_the_next_start(site, monkeypatch):
    from incipit import portmap
    app, _, folder = site
    monkeypatch.setattr(portmap, "forward", lambda port, finder=None: None)
    signed_in(app, "olive").put("/api/access", json={"forward_port": True})
    assert datafolder.load(folder, with_env=False)["server"]["forward_port"] is True


# ---------- AC-17: the firewall ----------


def test_ac17_the_installer_lets_the_program_through_the_firewall():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "advfirewall firewall add rule" in script.split("[UninstallRun]")[0]


def test_ac17_the_uninstaller_removes_the_firewall_rule():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "advfirewall firewall delete rule" in script.split("[UninstallRun]")[1]


def test_ac17_an_install_without_the_rule_never_asks_to_remove_it():
    """Regression from the 2026-10-20 installer test: the uninstall waited on a UAC prompt for a rule never added."""
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    removal = next(line for line in script.split("[UninstallRun]")[1].splitlines() if "advfirewall" in line)
    assert "Check: WantFirewall" in removal


# ---------- AC-18: HTTPS with the site's own certificate ----------


def _certificate(tmp_path):
    from incipit import tls
    tls.ensure(tmp_path / "tls", common_name="our-archive.local", names=["our-archive.local", "host-pc", "localhost"],
               addresses=["192.168.1.20", "127.0.0.1"])
    return tls.load(tmp_path / "tls")


def test_ac18_the_site_makes_a_certificate_for_its_names_and_addresses(tmp_path):
    from cryptography import x509
    alt = _certificate(tmp_path).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {"our-archive.local", "host-pc", "localhost"} <= set(alt.get_values_for_type(x509.DNSName))
    assert "192.168.1.20" in {str(a) for a in alt.get_values_for_type(x509.IPAddress)}


def test_ac18_the_certificate_is_kept_across_starts(tmp_path):
    from incipit import tls
    _certificate(tmp_path)
    before = tls.fingerprint(tmp_path / "tls")
    _certificate(tmp_path)
    assert tls.fingerprint(tmp_path / "tls") == before


def test_ac18_a_certificate_about_to_end_is_renewed(tmp_path, monkeypatch):
    from incipit import tls
    monkeypatch.setattr(tls, "VALID_DAYS", 10)
    _certificate(tmp_path)
    before = tls.fingerprint(tmp_path / "tls")
    monkeypatch.setattr(tls, "VALID_DAYS", 5 * 365)
    _certificate(tmp_path)
    assert tls.fingerprint(tmp_path / "tls") != before


def test_ac18_the_fingerprint_reads_as_browsers_show_it(tmp_path):
    from incipit import tls
    _certificate(tmp_path)
    pairs = tls.fingerprint(tmp_path / "tls").split(":")
    assert len(pairs) == 32 and all(len(p) == 2 and p == p.upper() for p in pairs)


def test_ac18_a_taken_https_port_leaves_the_site_on_http_and_says_why(site):
    from incipit import review_server as rs, tls
    _, settings, _ = site
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        settings.https_port = busy.getsockname()[1]
        assert rs._secure_listener(settings, "127.0.0.1") is None and "in use" in tls.serving["error"]


def test_ac18_plain_http_is_never_opened_to_the_internet(site, monkeypatch):
    from incipit import access_routes, tls
    _, settings, _ = site
    settings.forward_port = True
    monkeypatch.setitem(tls.serving, "port", 0)
    assert access_routes.internet(settings)["state"] == "no_https"


def test_ac18_anyone_can_download_the_certificate(site):
    from incipit import review_server as rs
    app, settings, _ = site
    rs._secure_listener(settings, "127.0.0.1")
    got = signed_in(app, None).get("/certificate.crt")
    assert got.status_code == 200 and got.content.startswith(b"-----BEGIN CERTIFICATE-----")


def test_ac18_access_shows_the_fingerprint_the_team_checks(site, monkeypatch):
    from incipit import review_server as rs, tls
    app, settings, _ = site
    rs._secure_listener(settings, "127.0.0.1")
    https = signed_in(app, "olive").get("/api/access").json()["https"]
    assert https["serving"] and https["fingerprint"] == tls.fingerprint(settings.tls_dir)


def test_ac18_an_https_port_equal_to_the_port_is_refused(tmp_path):
    datafolder.initialise(tmp_path / "Data")
    path = tmp_path / "Data" / "incipit.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("https_port = 8443", "https_port = 8010"), encoding="utf-8")
    with pytest.raises(datafolder.ConfigError, match="https_port"):
        datafolder.load(tmp_path / "Data")


def test_ac18_the_owner_changes_the_https_port_for_the_next_start(site):
    app, _, folder = site
    got = signed_in(app, "olive").put("/api/access", json={"https_port": 9443})
    assert got.json()["restart_needed"] and datafolder.load(folder, with_env=False)["server"]["https_port"] == 9443


# ---------- AC-19: the Cloudflare quick tunnel ----------


class FakeCloudflared:
    """Stands in for cloudflared: prints what the real one prints, then exits with `code`."""

    def __init__(self, lines, code=0):
        self.stderr = iter(lines)
        self.code = code
        self.pid = None
        self.args = None

    def __call__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        return self

    def wait(self, timeout=None):
        return self.code

    def poll(self):
        return self.code


CLOUDFLARED_OUTPUT = [
    "2026-10-20T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...",
    "2026-10-20T10:00:02Z INF |  https://quiet-river-ledger-notes.trycloudflare.com                    |",
]


def _tunnel(tmp_path, fake):
    from incipit import tunnel
    return tunnel.Tunnel(8010, tmp_path / "tunnel", program="cloudflared", launcher=fake)


def test_ac19_the_tunnel_points_cloudflared_at_the_sites_port(tmp_path):
    fake = FakeCloudflared(CLOUDFLARED_OUTPUT)
    _tunnel(tmp_path, fake).run_once()
    assert fake.args[-2:] == ["--url", "http://127.0.0.1:8010"]


def test_ac19_the_address_cloudflared_prints_is_recorded_for_the_tray(tmp_path):
    from incipit import tunnel
    lines = iter(CLOUDFLARED_OUTPUT)
    seen = []

    class Holding(FakeCloudflared):
        def wait(self, timeout=None):
            seen.append(tunnel.recorded_address(tmp_path / "tunnel"))
            return 0
    _tunnel(tmp_path, Holding(lines)).run_once()
    assert seen == ["https://quiet-river-ledger-notes.trycloudflare.com"]


def test_ac19_the_address_is_forgotten_when_the_tunnel_stops(tmp_path):
    from incipit import tunnel
    _tunnel(tmp_path, FakeCloudflared(CLOUDFLARED_OUTPUT, code=1)).run_once()
    assert tunnel.recorded_address(tmp_path / "tunnel") == ""


def test_ac19_a_tunnel_that_stops_says_why(tmp_path):
    t = _tunnel(tmp_path, FakeCloudflared(["ERR failed to request quick Tunnel: 429 Too Many Requests"], code=1))
    t.run_once()
    assert t.state == "stopped" and "429" in t.error


def test_ac19_a_missing_tunnel_program_is_reported(tmp_path):
    from incipit import tunnel
    t = tunnel.Tunnel(8010, tmp_path / "tunnel", program=None)
    t.run_once()
    assert t.state == "missing"


def test_ac19_cloudflared_gets_a_home_of_its_own(tmp_path):
    fake = FakeCloudflared(CLOUDFLARED_OUTPUT)
    _tunnel(tmp_path, fake).run_once()
    assert fake.kwargs["env"]["USERPROFILE"] == str(tmp_path / "tunnel")


def _outside(site, monkeypatch, *, mode, router_state, forward=True):
    from incipit import access_routes, tunnel
    _, settings, _ = site
    settings.tunnel, settings.forward_port = mode, forward
    opened = []
    monkeypatch.setattr(tunnel, "open_tunnel", lambda port, state_dir="", program=None, on_address=None: opened.append(port))
    monkeypatch.setattr(tunnel, "current", lambda: None)
    monkeypatch.setattr(access_routes, "internet", lambda s: {"state": router_state})
    access_routes.reach_outside(settings, wait=0)
    return opened, settings


def test_ac19_when_needed_no_tunnel_opens_if_the_router_gives_an_address(site, monkeypatch):
    opened, _ = _outside(site, monkeypatch, mode="auto", router_state="open")
    assert opened == [None]


def test_ac19_when_needed_the_tunnel_opens_behind_another_network(site, monkeypatch):
    opened, settings = _outside(site, monkeypatch, mode="auto", router_state="double_nat")
    assert opened == [settings.port]


def test_ac19_when_needed_the_tunnel_opens_if_forwarding_is_off(site, monkeypatch):
    opened, settings = _outside(site, monkeypatch, mode="auto", router_state="off", forward=False)
    assert opened == [settings.port]


def test_ac19_always_opens_the_tunnel_even_with_an_open_router(site, monkeypatch):
    opened, settings = _outside(site, monkeypatch, mode="on", router_state="open")
    assert opened == [settings.port]


def test_ac19_off_never_opens_the_tunnel(site, monkeypatch):
    opened, _ = _outside(site, monkeypatch, mode="off", router_state="double_nat")
    assert opened == [None]


def test_ac19_the_owner_switches_the_tunnel_and_it_is_kept(site, monkeypatch):
    from incipit import access_routes
    app, _, folder = site
    monkeypatch.setattr(access_routes, "reach_outside", lambda settings, wait=0: None)
    got = signed_in(app, "olive").put("/api/access", json={"tunnel": "auto"})
    assert got.json()["tunnel"]["mode"] == "auto" and datafolder.load(folder, with_env=False)["server"]["tunnel"] == "auto"


def test_ac19_an_unknown_tunnel_mode_is_refused(site):
    app, _, _ = site
    assert signed_in(app, "olive").put("/api/access", json={"tunnel": "sometimes"}).status_code == 400


def test_ac19_init_folder_records_the_tunnel_mode(tmp_path):
    cli.main(["init-folder", str(tmp_path / "Data"), "--tunnel", "auto"])
    assert datafolder.load(tmp_path / "Data", with_env=False)["server"]["tunnel"] == "auto"


class _Request:
    def __init__(self, peer, headers, scheme="http"):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers
        self.url = type("U", (), {"scheme": scheme})()


def test_ac19_sign_in_limits_count_each_tunnel_visitor_separately(monkeypatch):
    from incipit import tunnel
    monkeypatch.setattr(tunnel, "_current", object())
    assert tunnel.visitor_address(_Request("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"


def test_ac19_a_visitor_on_the_network_cannot_claim_another_address(monkeypatch):
    from incipit import tunnel
    monkeypatch.setattr(tunnel, "_current", object())
    assert tunnel.visitor_address(_Request("192.168.1.30", {"cf-connecting-ip": "203.0.113.9"})) == "192.168.1.30"


def test_ac19_the_header_is_ignored_while_no_tunnel_runs(monkeypatch):
    from incipit import tunnel
    monkeypatch.setattr(tunnel, "_current", None)
    assert tunnel.visitor_address(_Request("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"})) == "127.0.0.1"


def test_ac19_a_tunnel_visitors_session_cookie_is_marked_secure(monkeypatch):
    from incipit import tunnel
    monkeypatch.setattr(tunnel, "_current", object())
    request = _Request("127.0.0.1", {"cf-connecting-ip": "203.0.113.9", "x-forwarded-proto": "https"})
    assert tunnel.visitor_scheme(request) == "https"


def test_ac19_the_tray_offers_the_current_internet_address(tmp_path, monkeypatch):
    from incipit import tunnel
    (tmp_path / "tunnel").mkdir()
    (tmp_path / "tunnel" / tunnel.ADDRESS_FILE).write_text("https://quiet-river.trycloudflare.com\n")
    monkeypatch.setattr(host, "lan_addresses", lambda: [])
    site = type("S", (), {"folder": tmp_path, "running": True})()
    copied = []
    actions = {k: (lambda *a: None) for k in ("open", "folder", "start", "stop", "backup", "quit")}
    actions["copy"] = copied.append
    entry = next(item for item in tray.menu_items(site, {"server": {"port": 8010}}, actions) if item[0] == "Copy the team's link")
    entry[1]()
    assert entry[2] and copied == ["https://quiet-river.trycloudflare.com/"]


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NO_WINDOW"), reason="Windows job objects")
def test_ac19_cloudflared_dies_with_the_site_even_when_it_is_killed_outright(tmp_path):
    import sys
    script = ("import subprocess, sys, time\n"
              f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
              "from incipit import tunnel\n"
              "child = subprocess.Popen(['ping', '-t', '127.0.0.1'], stdout=subprocess.DEVNULL)\n"
              "tunnel._bind_to_this_process(child)\n"
              "print(child.pid, flush=True)\n"
              "time.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    pid = parent.stdout.readline().strip()
    subprocess.run(["taskkill", "/PID", str(parent.pid), "/F"], capture_output=True)
    parent.wait(timeout=10)
    import time
    time.sleep(1.5)
    listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    assert pid not in listing


def test_ac19_the_build_bundles_cloudflared_with_its_notice():
    script = (SCRIPTS / "build_app.py").read_text(encoding="utf-8")
    assert "shutil.copy(cloudflared, built / tunnel.EXE)" in script and "Apache License" in script


def test_ac19_the_installer_offers_the_tunnel_when_the_router_cannot_open_the_port():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "--tunnel auto" in script and "/NOTUNNEL" in script


# ---------- AC-20: the team's fixed link ----------


class FakeTelegraph:
    """Answers the Telegraph API the way telegra.ph does, keeping pages in memory."""

    def __init__(self, refuse=False):
        self.pages, self.calls, self.refuse = {}, [], refuse

    def __call__(self, url, data=None, timeout=None):
        import json as _json
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, dict(data)))
        if self.refuse:
            body = {"ok": False, "error": "FLOOD_WAIT_5"}
        elif method == "createAccount":
            body = {"ok": True, "result": {"access_token": "token-1", "short_name": data["short_name"]}}
        elif method == "createPage":
            path = f"Our-Archive-sign-in-10-20{'-' + str(len(self.pages)) if self.pages else ''}"
            self.pages[path] = _json.loads(data["content"])
            body = {"ok": True, "result": {"path": path, "url": f"https://telegra.ph/{path}"}}
        else:
            assert data["access_token"] == "token-1"
            self.pages[data["path"]] = _json.loads(data["content"])
            body = {"ok": True, "result": {"path": data["path"], "url": f"https://telegra.ph/{data['path']}"}}
        return type("R", (), {"json": lambda self: body})()

    def text(self, path):
        def flat(nodes):
            return "".join(n if isinstance(n, str) else flat(n.get("children", [])) for n in nodes)
        return flat(self.pages[path])


def _dns(answers):
    """Public DNS that has no answer for the first `answers` asks, then one."""
    asked = []

    def get(url, params=None, headers=None, timeout=None):
        asked.append(params["name"])
        body = {"Status": 0, "Answer": [{"type": 1, "data": "104.16.0.1"}]} if len(asked) > answers else {"Status": 3}
        return type("R", (), {"json": lambda self: body})()
    get.asked = asked
    return get


ADDRESS_ONE = "https://quiet-river-ledger-notes.trycloudflare.com/"
ADDRESS_TWO = "https://amber-field-quarter-maps.trycloudflare.com/"


def _link(tmp_path, telegraph=None, dns=None):
    from incipit import fixedlink
    return fixedlink.FixedLink(tmp_path / "tunnel", title="Our Archive sign-in", post=telegraph or FakeTelegraph(),
                               get=dns or _dns(0), sleep=lambda s: None)


def test_ac20_the_first_address_makes_a_page_that_leads_to_it(tmp_path):
    telegraph = FakeTelegraph()
    link = _link(tmp_path, telegraph)
    link.announce(ADDRESS_ONE)
    assert ADDRESS_ONE in telegraph.text("Our-Archive-sign-in-10-20")


def test_ac20_a_new_address_rewrites_the_same_page(tmp_path):
    telegraph = FakeTelegraph()
    link = _link(tmp_path, telegraph)
    link.announce(ADDRESS_ONE)
    first = link.url
    link.announce(ADDRESS_TWO)
    assert link.url == first and ADDRESS_TWO in telegraph.text("Our-Archive-sign-in-10-20")


def test_ac20_the_page_is_kept_across_restarts_of_the_site(tmp_path):
    telegraph = FakeTelegraph()
    _link(tmp_path, telegraph).announce(ADDRESS_ONE)
    _link(tmp_path, telegraph).announce(ADDRESS_TWO)
    assert [method for method, _ in telegraph.calls].count("createPage") == 1


def test_ac20_an_address_is_announced_only_once_public_dns_knows_it(tmp_path):
    telegraph = FakeTelegraph()
    dns = _dns(3)
    _link(tmp_path, telegraph, dns).announce(ADDRESS_ONE)
    assert len(dns.asked) == 4 and ADDRESS_ONE in telegraph.text("Our-Archive-sign-in-10-20")


def test_ac20_an_address_dns_never_learns_is_not_announced(tmp_path, monkeypatch):
    from incipit import fixedlink
    monkeypatch.setattr(fixedlink, "RESOLVE_SECONDS", -1)
    telegraph = FakeTelegraph()
    link = _link(tmp_path, telegraph, _dns(10 ** 6))
    assert link.announce(ADDRESS_ONE) is False and link.state == "unresolved" and not telegraph.calls


def test_ac20_dns_is_asked_over_https_for_the_tunnels_name():
    from incipit import fixedlink
    dns = _dns(0)
    fixedlink.resolves(ADDRESS_ONE, dns)
    assert dns.asked == ["quiet-river-ledger-notes.trycloudflare.com"]


def test_ac20_a_telegraph_refusal_is_reported(tmp_path):
    link = _link(tmp_path, FakeTelegraph(refuse=True))
    assert link.announce(ADDRESS_ONE) is False and "FLOOD_WAIT" in link.status()["error"]


def test_ac20_stopping_the_site_says_so_on_the_page(tmp_path):
    telegraph = FakeTelegraph()
    link = _link(tmp_path, telegraph)
    link.announce(ADDRESS_ONE)
    link.withdraw()
    text = telegraph.text("Our-Archive-sign-in-10-20")
    assert "not running" in text and ADDRESS_ONE not in text


def test_ac20_a_replaced_link_retires_the_old_page(tmp_path):
    telegraph = FakeTelegraph()
    link = _link(tmp_path, telegraph)
    link.announce(ADDRESS_ONE)
    old = link.url
    link.replace()
    link.announce(ADDRESS_ONE)
    assert link.url != old and "no longer works" in telegraph.text("Our-Archive-sign-in-10-20")


def test_ac20_the_tray_offers_the_permanent_link_over_the_changing_address(tmp_path, monkeypatch):
    from incipit import tunnel
    _link(tmp_path).announce(ADDRESS_ONE)
    (tmp_path / "tunnel" / tunnel.ADDRESS_FILE).write_text(ADDRESS_ONE.rstrip("/") + "\n")
    assert tray.internet_link(tmp_path, {"server": {"port": 8010}}) == "https://telegra.ph/Our-Archive-sign-in-10-20"


def test_ac20_the_tunnel_tells_the_link_each_new_address(tmp_path):
    import time
    told = []
    t = tunnel_with(tmp_path, FakeCloudflared(CLOUDFLARED_OUTPUT), told.append)
    t.run_once()
    for _ in range(50):
        if told:
            break
        time.sleep(0.02)
    assert told == ["https://quiet-river-ledger-notes.trycloudflare.com/"]


def tunnel_with(tmp_path, fake, on_address):
    from incipit import tunnel
    return tunnel.Tunnel(8010, tmp_path / "tunnel", program="cloudflared", launcher=fake, on_address=on_address)


def test_ac20_access_shows_the_permanent_link(site, monkeypatch):
    from incipit import access_routes
    app, settings, _ = site
    settings.tunnel_dir = str(Path(settings.data_folder) / "tunnel")
    access_routes._links.pop(settings.tunnel_dir, None)
    _link(Path(settings.data_folder)).announce(ADDRESS_ONE)
    assert signed_in(app, "olive").get("/api/access").json()["fixed_link"]["url"].startswith("https://telegra.ph/")


def test_ac20_only_the_owner_replaces_the_link(site):
    app, _, _ = site
    assert signed_in(app, "paul").post("/api/access/fixed-link/replace").status_code == 403


def test_ac20_turning_the_link_off_is_kept(site, monkeypatch):
    from incipit import access_routes
    app, settings, folder = site
    monkeypatch.setattr(access_routes, "team_link", lambda s: type("L", (), {"withdraw": lambda self: None,
                                                                             "status": lambda self: {"state": "waiting"}})())
    signed_in(app, "olive").put("/api/access", json={"fixed_link": False})
    assert datafolder.load(folder, with_env=False)["server"]["fixed_link"] is False


# ---------- regressions from the M4b code review (2026-10-21) ----------


def test_a_save_with_a_bad_value_changes_nothing_live(site, monkeypatch):
    """Review finding 1: the name was announced before a later bad value refused the save."""
    from incipit import localname
    app, settings, _ = site
    announced = []
    monkeypatch.setattr(localname, "announce", lambda name, port, title="Incipit": announced.append(name))
    got = signed_in(app, "olive").put("/api/access", json={"site_name": "our-archive", "tunnel": "sometimes"})
    assert got.status_code == 400 and announced == [] and settings.site_name == ""


def test_a_newer_address_supersedes_one_still_waiting_on_dns(tmp_path):
    """Review finding 2: a stop or a new address waited up to 3 minutes behind an old DNS wait."""
    import threading
    telegraph = FakeTelegraph()
    waiting = threading.Event()

    def slow_dns(url, params=None, headers=None, timeout=None):
        found = "amber-field" in params["name"]
        if not found:
            waiting.set()
        body = {"Status": 0, "Answer": [{"type": 1}]} if found else {"Status": 3}
        return type("R", (), {"json": lambda self: body})()
    link = _link(tmp_path, telegraph, slow_dns)
    link._sleep = lambda s: __import__("time").sleep(0.05)
    first = threading.Thread(target=link.announce, args=(ADDRESS_ONE,))
    first.start()
    waiting.wait(5)
    link.announce(ADDRESS_TWO)
    first.join(5)
    assert not first.is_alive() and ADDRESS_ONE not in telegraph.text("Our-Archive-sign-in-10-20")


def test_stopping_from_the_tray_says_so_on_the_team_page(tmp_path, monkeypatch):
    """Review finding 3: the tray kills the site outright, so the page kept a dead address."""
    from incipit import fixedlink
    datafolder.initialise(tmp_path / "Data")
    state_dir = tmp_path / "Data" / "tunnel"
    telegraph = FakeTelegraph()
    _link(tmp_path / "Data", telegraph).announce(ADDRESS_ONE)
    monkeypatch.setattr(fixedlink.requests, "post", telegraph)
    site = tray.Site(tmp_path / "Data")
    site.process = type("P", (), {"poll": lambda self: None, "terminate": lambda self: None,
                                  "wait": lambda self, timeout=None: 0})()
    site.stop()
    assert state_dir.is_dir() and "not running" in telegraph.text("Our-Archive-sign-in-10-20")


def test_the_page_keeps_its_title_when_the_tray_rewrites_it(tmp_path, monkeypatch):
    """Review finding 3: the tray knows no site name and would have retitled the page."""
    from incipit import fixedlink
    telegraph = FakeTelegraph()
    _link(tmp_path, telegraph).announce(ADDRESS_ONE)
    fixedlink.FixedLink(tmp_path / "tunnel", post=telegraph, get=_dns(0)).withdraw()
    assert telegraph.calls[-1][1]["title"] == "Our Archive sign-in"


def test_an_old_tunnel_never_erases_the_new_tunnels_address(tmp_path):
    """Review finding 4: the replaced tunnel cleared the address file its successor had written."""
    from incipit import tunnel
    old = tunnel.Tunnel(8010, tmp_path / "tunnel", program="cloudflared")
    old._record("https://old-words.trycloudflare.com")
    (tmp_path / "tunnel" / tunnel.ADDRESS_FILE).write_text("https://new-words.trycloudflare.com\n")
    old._record("")
    assert tunnel.recorded_address(tmp_path / "tunnel") == "https://new-words.trycloudflare.com"


def test_a_tunnel_stopped_while_cloudflared_starts_ends_it(tmp_path):
    """Review finding 4: a stop during start-up left a second cloudflared running."""
    ended = []

    class Starting(FakeCloudflared):
        def terminate(self):
            ended.append(True)
    fake = Starting(CLOUDFLARED_OUTPUT)
    t = _tunnel(tmp_path, fake)
    t._stop.set()
    t.run_once()
    assert ended == [True]


def test_when_one_listener_ends_both_stop():
    """Review finding 5: the HTTPS listener kept serving after the plain one ended."""
    import asyncio
    from incipit import review_server as rs

    class Ends:
        should_exit = False

        async def serve(self):
            return None

    class Runs:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.05)
    runs = Runs()
    asyncio.run(asyncio.wait_for(rs._serve_together([Ends(), runs]), timeout=5))
    assert runs.should_exit


def test_a_failed_move_leaves_no_partial_copy_behind(tmp_path, monkeypatch):
    """Review finding 6: the partial copy made the same folder "not empty" for the next try."""
    from incipit import relocate
    _, _, folder = _remembered_site(tmp_path, monkeypatch)
    relocate.request(folder, tmp_path / "New")
    real = relocate.shutil.copytree
    calls = []

    def half(src, dst, *args, **kwargs):
        outermost = not calls
        calls.append(src)
        real(src, dst, *args, **kwargs)
        if outermost:
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(relocate.shutil, "copytree", half)
    relocate.apply_pending(folder.resolve())
    assert not (tmp_path / "New").exists()


def test_a_missing_folder_is_said_to_the_person_at_the_host(tmp_path, monkeypatch):
    """Review finding 6: the windowed tray printed to a console nobody sees."""
    from incipit import relocate
    relocate.remember(tmp_path / "Unplugged")
    monkeypatch.delenv(datafolder.ENV_FOLDER, raising=False)
    monkeypatch.chdir(tmp_path)
    told = []
    monkeypatch.setattr(tray, "tell", told.append)
    assert tray.main(None) == 2 and "Unplugged" in told[0]


def test_the_trays_menu_builds_with_the_real_pystray(monkeypatch):
    """Regression (user report 2026-10-21): pystray refused the menu's actions (a lambda with a default
    argument counts as three parameters) and the tray crashed on its first start."""
    pytest.importorskip("pystray")
    monkeypatch.setattr(host, "lan_addresses", lambda: [])
    items, calls = _menu(running=True)
    items[-1:-1] = tray.update_entries({"state": "available", "release": {"version": "9.9.9"}},
                                       {"check": lambda: calls.append("check"), "install": lambda: calls.append("install")})
    menu = tray.pystray_menu(items)
    entries = [e for e in menu.items if e.text != "Quit" and e._action is not None]
    for entry in entries:
        entry(None)
    assert len(list(menu.items)) == len(items) and "install" in calls and "backup" in calls


# Regression (user report 2026-10-21): the first install was pointed at C:\Program Files, which the
# person's own account cannot write to, and nothing explained why it did not work.


def test_the_installer_refuses_a_data_folder_the_account_cannot_write_and_says_what_to_choose():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "function DataFolderProblem" in script and "function Writable" in script and "function ProtectedPlace" in script
    assert "cannot be under Program Files or Windows" in script and r"Documents\Incipit Data, or one on a data drive, such as D:\Incipit" in script
    assert "Problem := DataFolderProblem(FolderPage.Values[0]);" in script


def test_the_installer_checks_the_data_folder_before_a_silent_install_writes_anything():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "function PrepareToInstall" in script and "Result := DataFolderProblem(DataFolder(''));" in script


def test_the_installer_explains_program_files_for_the_program_too():
    script = (ROOT / "installer" / "incipit.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequiredOverridesAllowed=commandline" in script
    assert "Problem := ProgramFolderProblem(WizardDirValue);" in script and "/ALLUSERS" in script


# The team's page carries the site's own name (user direction 2026-10-21: the link should read as
# whatever the owner named the site, not as the product).


def test_the_teams_page_is_titled_with_the_sites_name_alone(tmp_path):
    import dataclasses
    from conftest import make_app
    from incipit import access_routes, fixedlink
    app, settings = make_app(tmp_path, admins=("paul",), reviewers=())
    assert signed_in(app, "paul").put("/api/site/settings", json={"product_name": "Harbour Street Archive"}).status_code == 200
    settings = dataclasses.replace(settings, tunnel_dir=str(tmp_path / "tunnel"))
    access_routes._links.pop(settings.tunnel_dir, None)
    link = access_routes.team_link(settings)
    assert isinstance(link, fixedlink.FixedLink) and link.title == "Harbour Street Archive"
    signed_in(app, "paul").put("/api/site/settings", json={"product_name": "Harbour Street Papers"})
    assert access_routes.team_link(settings).title == "Harbour Street Papers"


def test_renaming_the_site_renames_its_page_at_the_next_write(tmp_path):
    from incipit import fixedlink
    telegraph = FakeTelegraph()
    first = fixedlink.FixedLink(tmp_path / "tunnel", title="Harbour Street Archive", post=telegraph, get=_dns(0), sleep=lambda s: None)
    first.announce(ADDRESS_ONE)
    renamed = fixedlink.FixedLink(tmp_path / "tunnel", title="Harbour Street Papers", post=telegraph, get=_dns(0), sleep=lambda s: None)
    renamed.announce(ADDRESS_TWO)
    assert telegraph.calls[-1][1]["title"] == "Harbour Street Papers"


def test_a_replaced_link_takes_the_sites_current_name(tmp_path):
    from incipit import fixedlink
    telegraph = FakeTelegraph()
    old = fixedlink.FixedLink(tmp_path / "tunnel", title="Old Name", post=telegraph, get=_dns(0), sleep=lambda s: None)
    old.announce(ADDRESS_ONE)
    new = fixedlink.FixedLink(tmp_path / "tunnel", title="New Name", post=telegraph, get=_dns(0), sleep=lambda s: None)
    new.replace()
    new.announce(ADDRESS_TWO)
    created = [c for c in telegraph.calls if c[0] == "createPage"]
    assert created[-1][1]["title"] == "New Name"
