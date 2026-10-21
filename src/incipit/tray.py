"""The tray (menu-bar) program the installer starts: it runs the site from the data folder and offers
Open Incipit, Open data folder, Start/Stop, Back up now and Quit, with the address to share in its
tooltip. The menu is built by `menu_items` so it can be checked without a display."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import backup, datafolder, fixedlink, host, tunnel


def open_folder(path: str | Path) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(str(path))  # noqa: S606 - opening the client's own folder in Explorer
    elif system == "Darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class Site:
    """The server as a child process the tray starts and stops, so Stop really frees the port. When
    the site asks to be started again (after a data-folder move) the tray does so, from wherever the
    location file now points."""

    def __init__(self, folder: Path, explicit: bool = False, on_update=None):
        self.folder = folder
        # Called when the site stops for an update, so the tray can quit and let the installer work.
        self.on_update = on_update
        # A folder given on the command line is served as it is; otherwise the location file decides.
        self.explicit = explicit
        self.process: subprocess.Popen | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def command(self) -> list[str]:
        base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "incipit.cli"]
        return [*base, "serve", *(["--data-folder", str(self.folder)] if self.explicit else [])]

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = {**os.environ, cli_supervised(): "1"}
            self.process = subprocess.Popen(self.command(), cwd=str(self.folder), creationflags=flags, env=env)
            threading.Thread(target=self._watch, args=(self.process,), daemon=True).start()

    def _watch(self, process: subprocess.Popen) -> None:
        code = process.wait()
        if code == cli_update_code() and not self._stopping:
            self.process = None
            if self.on_update is not None:
                self.on_update()
            return
        if code == cli_restart_code() and not self._stopping:
            if not self.explicit:
                from . import relocate
                self.folder = relocate.remembered() or self.folder
            self.process = None
            self.start()

    def stop(self) -> None:
        self._stopping = True
        was_running = self.running
        if self.running:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if was_running:
            withdraw_team_page(self.folder)


def withdraw_team_page(folder: Path) -> None:
    """Says on the team's page that the site is stopped, which a site killed outright cannot say itself."""
    try:
        config = datafolder.load(folder, with_env=False)
    except datafolder.ConfigError:
        return
    if not config["server"]["fixed_link"]:
        return
    state_dir = datafolder.resolved_paths(folder, config)["tunnel"]
    if fixedlink.recorded_url(state_dir):
        fixedlink.FixedLink(state_dir).withdraw()


def cli_supervised() -> str:
    from . import cli
    return cli.SUPERVISED


def cli_restart_code() -> int:
    from . import cli
    return cli.RESTART_EXIT_CODE


def cli_update_code() -> int:
    from . import cli
    return cli.UPDATE_EXIT_CODE


def update_entries(update: dict, actions: dict) -> list[tuple[str, object, bool]]:
    """"Check for updates", and "Install update X" once a newer release has been found."""
    entries = [("Check for updates", lambda: actions["check"](), True)]
    if update.get("state") == "available" and update.get("release"):
        version = update["release"]["version"]
        entries.append((f"Install update {version}", lambda: actions["install"](), True))
    return entries


def install_update(site: "Site", tray_stop) -> str:
    """Downloads and checks the newer release, starts its installer, and gets out of its way."""
    from . import updates
    # Read fresh: a release remembered from an earlier check may since have been replaced.
    current = updates.check()
    if current["state"] != "available":
        return "Incipit is up to date."
    try:
        installer = updates.download(current["release"])
        updates.start_installer(installer)
    except (updates.UpdateError, OSError) as exc:
        return f"The update could not be installed: {exc}"
    site.stop()
    tray_stop()
    return ""


def log_to(folder: Path, config: dict) -> None:
    """The tray's own rolling log beside the site's, so a problem starting the site has a record."""
    import logging
    import logging.handlers
    logs = datafolder.resolved_paths(folder, config)["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "tray.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def copy_text(text: str) -> None:
    """Puts `text` on the clipboard with the system's own tool, so the tray needs no extra library."""
    system = platform.system()
    tool = ["clip"] if system == "Windows" else ["pbcopy"] if system == "Darwin" else ["xclip", "-selection", "clipboard"]
    try:
        subprocess.run(tool, input=text.encode("utf-16" if system == "Windows" else "utf-8"), check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        webbrowser.open(text)


def internet_link(folder: Path, config: dict) -> str:
    """The team's permanent link when there is one, else the tunnel's current address, or empty."""
    state_dir = Path(folder) / config.get("paths", {}).get("tunnel", "tunnel")
    address = tunnel.recorded_address(state_dir)
    if not address:
        return ""
    return fixedlink.recorded_url(state_dir) or address + "/"


def tell(message: str) -> None:
    """Says something to the person at the host: a message box on Windows, where the tray has no console."""
    print(message, file=sys.stderr)
    if platform.system() == "Windows" and getattr(sys, "frozen", False):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Incipit", 0x10)


# Past this, a backup is overdue: the site backs up every few minutes while it runs.
STALE_BACKUP_HOURS = 24


def backup_status(folder: Path, config: dict) -> dict:
    paths = datafolder.resolved_paths(folder, config) if config.get("paths") else {}
    return backup.read_status(paths.get("logs")) or {}


def _hours_since(iso: str | None, now: datetime | None = None) -> float | None:
    try:
        return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def backup_label(status: dict, now: datetime | None = None) -> str:
    """What the tray's menu says about backups."""
    hours = _hours_since(status.get("last_success_at"), now)
    if hours is None:
        return "Last backup: none yet"
    if hours < 1:
        return f"Last backup: {max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"Last backup: {round(hours)} h ago"
    return f"Last backup: {round(hours / 24)} days ago"


def backup_warning(status: dict, now: datetime | None = None) -> str:
    """Why the person at the host should look at Settings → Backups, or empty while all is well."""
    if not status:
        return ""
    if status.get("ok") is False:
        return f"The last backup failed: {status.get('detail', 'no reason given')[:160]}"
    hours = _hours_since(status.get("last_success_at"), now)
    if hours is None or hours > STALE_BACKUP_HOURS:
        return "There has been no good backup for over a day. Open Settings → Backups."
    return ""


def share_link(config: dict) -> str:
    addresses = host.lan_addresses()
    return host.link(addresses[0] if addresses else "localhost", config["server"]["port"])


def menu_items(site: Site, config: dict, actions: dict) -> list[tuple[str, object, bool]]:
    """(label, action, enabled) for each entry, in order; the tray turns these into its menu."""
    local = host.link("localhost", config["server"]["port"])
    return [
        ("Open Incipit", lambda: actions["open"](local), site.running),
        ("Open data folder", lambda: actions["folder"](site.folder), True),
        ("Stop the site" if site.running else "Start the site",
         (lambda: actions["stop"]()) if site.running else (lambda: actions["start"]()), True),
        ("Back up now", lambda: actions["backup"](), True),
        (f"Share: {share_link(config)}", None, False),
        (backup_label(backup_status(site.folder, config)), None, False),
        # New at every start, so the tray always offers the current one.
        ("Copy the team's link", lambda: actions["copy"](internet_link(site.folder, config)),
         bool(site.running and internet_link(site.folder, config))),
        ("Quit", lambda: actions["quit"](), True),
    ]


def back_up_now(folder: Path, config: dict) -> dict:
    paths = datafolder.resolved_paths(folder, config)
    return backup.run_backup(paths["database"], paths["backups"], data_dir=folder, status_dir=paths["logs"],
                             mirror_dir=paths["backup_mirror"], export_dir=paths["exports"])


def main(folder_argument: str | None = None, quiet: bool = False) -> int:
    try:
        folder = datafolder.resolve(folder_argument)
        config = datafolder.load(folder)
    except datafolder.ConfigError as exc:
        tell(f"Incipit could not start: {exc}")
        return 2
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("the tray needs pystray (pip install incipit[tray]); serving without it", file=sys.stderr)
        from . import cli
        return cli.main(["serve", "--data-folder", str(folder)])

    log_to(folder, config)
    stopping = {"tray": None}
    site = Site(folder, explicit=folder_argument is not None,
                on_update=lambda: stopping["tray"] and stopping["tray"].stop())
    site.start()
    local = host.link("localhost", config["server"]["port"])
    # The wizard, on the first start of a fresh install; afterwards the site itself. Not after an
    # update, which nobody may be sitting at the host to see.
    if not quiet:
        threading.Timer(2.5, lambda: webbrowser.open(local)).start()

    icon_file = Path(__file__).with_name("tray.png")
    if not icon_file.is_file():
        from .assets import APP_STATIC_DIR
        icon_file = APP_STATIC_DIR / "favicon.svg"
    image = Image.open(icon_file) if icon_file.suffix == ".png" else Image.new("RGBA", (64, 64), (17, 17, 17, 255))

    def quit_all(tray):
        site.stop()
        tray.stop()

    def check_for_updates(tray):
        from . import updates
        found = updates.check()
        words = {"available": f"Incipit {found['release']['version']} is ready to install." if found.get("release") else "",
                 "up_to_date": "Incipit is up to date.", "not_configured": "Updates are not set up in this build.",
                 "unreachable": "Could not check for updates; the site carries on."}
        try:
            tray.notify(words.get(found["state"], found["state"]), "Incipit")
        except Exception:  # noqa: BLE001 - a notification must never stop the tray
            pass

    def build_menu(tray):
        from . import updates
        actions = {"open": webbrowser.open, "folder": open_folder, "start": site.start, "stop": site.stop,
                   "backup": lambda: threading.Thread(target=back_up_now, args=(folder, config), daemon=True).start(),
                   "copy": copy_text, "quit": lambda: quit_all(tray),
                   "check": lambda: threading.Thread(target=check_for_updates, args=(tray,), daemon=True).start(),
                   "install": lambda: threading.Thread(target=lambda: tell_if(install_update(site, tray.stop)),
                                                       daemon=True).start()}
        items = menu_items(site, config, actions)
        # Before Quit, which stays last.
        items[-1:-1] = update_entries(updates.state(), actions)
        return pystray.Menu(*[pystray.MenuItem(label, (lambda _icon, _item, act=act: act()) if act else None,
                                               enabled=enabled)
                              for label, act, enabled in items])

    def tell_if(message: str) -> None:
        if message:
            tell(message)

    tray = pystray.Icon("incipit", image, f"Incipit — {share_link(config)}")
    tray.menu = pystray.Menu(lambda: build_menu(tray).items)
    stopping["tray"] = tray

    def watch_backups():
        """Says so once when backups fail or go stale, and again only if the reason changes."""
        told = ""
        while True:
            warning = backup_warning(backup_status(site.folder, config)) if site.running else ""
            if warning and warning != told:
                try:
                    tray.notify(warning, "Incipit backups")
                except Exception:  # noqa: BLE001 - a notification must never stop the tray
                    pass
            told = warning
            time.sleep(600)
    threading.Thread(target=watch_backups, name="incipit-backup-watch", daemon=True).start()
    tray.run()
    return 0
