"""Starting the site when the host's user logs in: a per-user entry on each platform, made and removed
through one command runner so the suite can watch what would be run without running it."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

TASK_NAME = "Incipit"
AGENT_LABEL = "org.incipit.site"


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def serve_command(data_folder: str | Path | None = None) -> list[str]:
    """How the site is started at login: the tray, which finds the data folder through the location
    file (so a move from Settings needs no new entry); the packaged program, or this interpreter."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "tray"]
    return [sys.executable, "-m", "incipit.cli", "tray"]


def _quote(parts: list[str]) -> str:
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def supported(system: str | None = None) -> bool:
    return (system or platform.system()) in ("Windows", "Darwin", "Linux")


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "incipit.desktop"


def status(system: str | None = None, runner=run) -> bool:
    """Whether a start-at-login entry exists for this user."""
    system = system or platform.system()
    if system == "Windows":
        return runner(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    if system == "Darwin":
        return _agent_path().is_file()
    if system == "Linux":
        return _autostart_path().is_file()
    return False


def register(data_folder: str | Path, system: str | None = None, runner=run) -> str:
    """Makes the entry; returns the command the platform will run, which the page shows."""
    system = system or platform.system()
    command = serve_command(data_folder)
    if system == "Windows":
        done = runner(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", _quote(command), "/F"])
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout or "schtasks refused").strip())
    elif system == "Darwin":
        path = _agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"        <string>{p}</string>\n" for p in command)
        path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>{AGENT_LABEL}</string>
    <key>ProgramArguments</key><array>
{args}    </array>
    <key>RunAtLoad</key><true/>
</dict></plist>
''', encoding="utf-8")
        runner(["launchctl", "load", str(path)])
    elif system == "Linux":
        path = _autostart_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[Desktop Entry]\nType=Application\nName=Incipit\nExec={_quote(command)}\nX-GNOME-Autostart-enabled=true\n",
                        encoding="utf-8")
    else:
        raise RuntimeError(f"starting at login is not supported on {system}")
    return _quote(command)


def unregister(system: str | None = None, runner=run) -> None:
    system = system or platform.system()
    if system == "Windows":
        runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    elif system == "Darwin":
        path = _agent_path()
        if path.is_file():
            runner(["launchctl", "unload", str(path)])
            path.unlink()
    elif system == "Linux":
        _autostart_path().unlink(missing_ok=True)
