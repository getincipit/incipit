"""New versions of Incipit: the release feed, a checked download, and handing over to the installer.

We publish `latest.json` with each release: its version, date and notes, and for each platform the
installer's address, size and SHA-256. The site reads it once a day (and when asked), and when the
owner says so downloads the installer into the user's own settings folder, checks it against the
feed, and starts it with `/SILENT /UPDATE`; the installer replaces the program and leaves the data
folder alone (installer/incipit.iss). Nothing is installed without the owner asking.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import relocate
from .assets import asset_directory

logger = logging.getLogger(__name__)

ENV_FEED = "INCIPIT_UPDATE_FEED"
FEED_FILE = "UPDATE_FEED"
CHECK_EVERY_HOURS = 24
DOWNLOAD_CHUNK = 1 << 20


class UpdateError(RuntimeError):
    """An update that cannot go ahead; the message says why and what to do."""


def current_version() -> str:
    path = asset_directory("VERSION")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def feed_url() -> str:
    """Where this build looks for new versions: the environment, else the address baked into the build."""
    if os.environ.get(ENV_FEED):
        return os.environ[ENV_FEED].strip()
    try:
        return asset_directory(FEED_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def parse_version(text: str) -> tuple[tuple[int, ...], bool]:
    """(numbers, is_prerelease): "0.10.0" → ((0, 10, 0), False); "0.11.0-beta.1" → ((0, 11, 0), True)."""
    text = str(text or "").strip().lstrip("vV")
    core, _, suffix = text.partition("-")
    numbers = tuple(int(part) for part in re.findall(r"\d+", core)) or (0,)
    return numbers, bool(suffix)


def is_newer(candidate: str, current: str) -> bool:
    """A release newer than what runs; a pre-release is never offered over a release."""
    wanted, pre = parse_version(candidate)
    have, _ = parse_version(current)
    if pre:
        return False
    width = max(len(wanted), len(have))
    return wanted + (0,) * (width - len(wanted)) > have + (0,) * (width - len(have))


def platform_key() -> str:
    system = platform.system()
    return "windows" if system == "Windows" else "macos" if system == "Darwin" else "linux"


_state: dict = {"state": "unchecked", "checked_at": None, "release": None, "error": "", "downloaded": None}
_lock = threading.Lock()


def state() -> dict:
    with _lock:
        return {**_state, "current": current_version(), "feed": bool(feed_url())}


def check(http_get=None) -> dict:
    """Reads the feed and records what it says: up to date, a newer release, or why it could not tell."""
    http_get = http_get or (lambda *a, **k: requests.get(*a, **k))
    url = feed_url()
    now = datetime.now(timezone.utc).isoformat()
    if not url:
        result = {"state": "not_configured", "release": None, "error": ""}
    else:
        try:
            reply = http_get(url, timeout=20)
            reply.raise_for_status()
            feed = reply.json()
            download = (feed.get("downloads") or {}).get(platform_key())
            release = {"version": str(feed["version"]), "date": feed.get("date", ""), "notes": feed.get("notes", ""),
                       "download": download}
            if is_newer(release["version"], current_version()) and download:
                result = {"state": "available", "release": release, "error": ""}
            else:
                result = {"state": "up_to_date", "release": release, "error": ""}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            result = {"state": "unreachable", "release": None, "error": f"{exc.__class__.__name__}: {exc}"[:240]}
    with _lock:
        _state.update(result, checked_at=now)
    logger.info("update check: %s%s", result["state"],
                f" ({result['release']['version']})" if result.get("release") else "")
    return state()


def download(release: dict, http_get=None, folder: Path | None = None) -> Path:
    """The release's installer, checked against the feed's size and SHA-256; anything else is deleted."""
    http_get = http_get or (lambda *a, **k: requests.get(*a, **k))
    meta = release.get("download") or {}
    if not meta.get("url") or not meta.get("sha256"):
        raise UpdateError("the release feed names no installer for this computer")
    folder = folder or relocate.settings_dir() / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"Incipit-{release['version']}-setup.exe")
    final, partial = folder / name, folder / (name + ".partial")
    digest, size = hashlib.sha256(), 0
    try:
        with http_get(meta["url"], stream=True, timeout=60) as reply:
            reply.raise_for_status()
            with partial.open("wb") as out:
                for chunk in reply.iter_content(DOWNLOAD_CHUNK):
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    except (requests.RequestException, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"the download did not finish ({exc.__class__.__name__}); try again") from exc
    expected_size = meta.get("size")
    if (expected_size is not None and size != int(expected_size)) or digest.hexdigest().lower() != str(meta["sha256"]).lower():
        partial.unlink(missing_ok=True)
        raise UpdateError("the downloaded installer did not match the release feed, so it was deleted; "
                          "nothing was installed")
    partial.replace(final)
    with _lock:
        _state["downloaded"] = str(final)
    return final


def installer_command(installer: Path) -> list[str]:
    """Silent, keeping the data folder and every setting, then the tray starts again quietly."""
    return [str(installer), "/SILENT", "/UPDATE", "/SUPPRESSMSGBOXES", "/NORESTART"]


def start_installer(installer: Path, launcher=subprocess.Popen) -> None:
    """Starts the installer on its own, so it outlives the program it is about to replace."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    launcher(installer_command(installer), creationflags=flags, close_fds=True)
    logger.info("started %s; the site stops so it can be replaced", installer.name)


def check_forever(stop: threading.Event, first_delay: float = 120, http_get=None) -> None:
    """Once a day while the site runs; a failure waits for the next day like a success."""
    if stop.wait(first_delay):
        return
    while True:
        try:
            check(http_get)
        except Exception as exc:  # noqa: BLE001 - a check must never touch the site
            logger.warning("update check failed: %s", exc)
        if stop.wait(CHECK_EVERY_HOURS * 3600):
            return
