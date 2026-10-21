"""A Cloudflare quick tunnel: an internet address for a site the router cannot open.

When the host is behind a network nobody here controls (a campus, a company, a provider's shared
address), `cloudflared` makes one outbound connection to Cloudflare and is handed a temporary
`https://<words>.trycloudflare.com` address that reaches the site. No account, domain or payment is
needed. The address changes each time the tunnel starts, so the site records the current one in the
data folder (`tunnel/address.txt`) for the tray and Settings → Access to show. Cloudflare describes
quick tunnels as meant for testing, with no uptime promise and at most 200 requests in progress.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ADDRESS = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ADDRESS_FILE = "address.txt"
MODES = ("off", "auto", "on")
# Waits between restarts after cloudflared stops, so a network outage is not a busy loop.
BACKOFF_SECONDS = (5, 15, 60, 300)
EXE = "cloudflared.exe" if os.name == "nt" else "cloudflared"


def find_program() -> str | None:
    """The bundled cloudflared beside the packaged program, else one installed on this computer."""
    beside = Path(sys.executable).with_name(EXE)
    if getattr(sys, "frozen", False) and beside.is_file():
        return str(beside)
    override = os.environ.get("INCIPIT_CLOUDFLARED")
    if override:
        return override
    found = shutil.which("cloudflared")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            candidate = Path(base or "") / "cloudflared" / EXE
            if base and candidate.is_file():
                return str(candidate)
    return None


PID_FILE = "cloudflared.pid"


def _bind_to_this_process(process) -> None:
    """On Windows, cloudflared dies with the site even when the site is killed outright (the tray's Stop)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE

    class LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", LIMITS), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    global _job
    if _job is None:
        job = kernel32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        _job = job
    handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)  # SET_QUOTA | TERMINATE
    if handle:
        kernel32.AssignProcessToJobObject(_job, handle)
        kernel32.CloseHandle(handle)


# The job holding cloudflared; Windows closes it, and kills what is in it, when this process ends.
_job = None


def _end_leftover(state_dir: Path) -> None:
    """Stops a cloudflared an earlier, abruptly ended run left behind, so two tunnels never compete."""
    try:
        pid = int((state_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/FI", "IMAGENAME eq cloudflared.exe"],
                           capture_output=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, 15)
    except OSError:
        pass
    (state_dir / PID_FILE).unlink(missing_ok=True)


def command(program: str, port: int) -> list[str]:
    # The local hop is loopback, so plain HTTP; Cloudflare gives visitors HTTPS.
    return [program, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]


class Tunnel:
    """Keeps a quick tunnel to the site's port running, restarting cloudflared if it stops."""

    def __init__(self, port: int, state_dir: str | Path, program: str | None = None, launcher=subprocess.Popen,
                 on_address=None):
        self.port = port
        # Told each new address in the background, so the team's fixed link follows it.
        self.on_address = on_address
        self.state_dir = Path(state_dir)
        self.program = program
        self._launch = launcher
        self.process = None
        self.address = ""
        self.state = "starting"
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _env(self) -> dict:
        # A config.yaml in the user's own ~/.cloudflared stops quick tunnels, so the child gets its own home.
        home = str(self.state_dir)
        return {**os.environ, "HOME": home, "USERPROFILE": home}

    def _record(self, address: str) -> None:
        mine = self.address
        self.address = address
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / ADDRESS_FILE
        if address:
            path.write_text(address + "\n", encoding="utf-8")
        elif mine and recorded_address(self.state_dir) == mine:
            # Only its own: the tunnel that replaced this one may already have written its address.
            path.unlink(missing_ok=True)

    def _forget_pid(self, pid) -> None:
        path = self.state_dir / PID_FILE
        try:
            if path.read_text(encoding="utf-8").strip() == str(pid):
                path.unlink()
        except OSError:
            pass

    def run_once(self) -> int | None:
        """Starts cloudflared, reads its output for the address, and waits for it to stop."""
        if not self.program:
            self.state, self.error = "missing", "the tunnel program (cloudflared) is not installed"
            return None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _end_leftover(self.state_dir)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = self._launch(command(self.program, self.port), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                        env=self._env(), cwd=str(self.state_dir), creationflags=flags)
        except OSError as exc:
            self.state, self.error = "missing", f"the tunnel program could not start ({exc})"
            return None
        try:
            _bind_to_this_process(self.process)
        except (OSError, AttributeError) as exc:
            logger.warning("the tunnel is not tied to the site's process: %s", exc)
        if getattr(self.process, "pid", None):
            (self.state_dir / PID_FILE).write_text(str(self.process.pid), encoding="utf-8")
        if self._stop.is_set():
            # Stopped while cloudflared was starting: stop() saw no process to end, so this does.
            self.process.terminate()
        self.state, self.error = "starting", ""
        last = ""
        for line in self.process.stderr:
            line = line.strip()
            if line:
                last = line
            found = ADDRESS.search(line)
            if found and found.group(0) != self.address:
                self._record(found.group(0))
                self.state = "open"
                logger.info("quick tunnel open at %s", self.address)
                if self.on_address is not None:
                    threading.Thread(target=self.on_address, args=(self.address + "/",), daemon=True,
                                     name="incipit-fixed-link").start()
        code = self.process.wait()
        self._forget_pid(getattr(self.process, "pid", None))
        self._record("")
        if not self._stop.is_set():
            self.state = "stopped"
            self.error = f"the tunnel stopped (exit {code}): {last[-200:]}" if last else f"the tunnel stopped (exit {code})"
            logger.warning("%s", self.error)
        return code

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once()
            if self.state == "missing":
                return
            # A tunnel that ran a while before stopping starts the backoff again.
            failures = 0 if time.monotonic() - started > 600 else failures + 1
            if self._stop.wait(BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1] if failures else 1):
                return

    def start(self) -> "Tunnel":
        self._thread = threading.Thread(target=self._run, name="incipit-tunnel", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._record("")
        self.state = "off"

    def status(self) -> dict:
        return {"state": self.state, "link": self.address + "/" if self.address else "", "error": self.error}


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _through_tunnel(request) -> bool:
    """A request cloudflared delivered: from this computer, while this site's tunnel runs."""
    peer = request.client.host if request.client else ""
    return peer in LOOPBACK and _current is not None and bool(request.headers.get("cf-connecting-ip"))


def visitor_address(request) -> str:
    """Who is asking: through the tunnel every visitor arrives from 127.0.0.1, so Cloudflare's header says."""
    if _through_tunnel(request):
        return request.headers["cf-connecting-ip"].strip()
    return request.client.host if request.client else "unknown"


def visitor_scheme(request) -> str:
    """How the visitor reached the site; the tunnel's own hop is HTTP but the visitor's is HTTPS."""
    if _through_tunnel(request) and request.headers.get("x-forwarded-proto") == "https":
        return "https"
    return request.url.scheme


def recorded_address(state_dir: str | Path) -> str:
    """The tunnel's current address as the running site recorded it, for the tray."""
    try:
        return (Path(state_dir) / ADDRESS_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# The running site's tunnel, which Settings → Access reads and replaces.
_lock = threading.Lock()
_current: Tunnel | None = None


def current() -> Tunnel | None:
    return _current


def open_tunnel(port: int | None, state_dir: str | Path = "", program: str | None = None,
                on_address=None) -> Tunnel | None:
    """Starts a tunnel to `port` (stopping any earlier one); None stops it."""
    global _current
    with _lock:
        if _current is not None:
            _current.stop()
            _current = None
        if port:
            _current = Tunnel(port, state_dir, program or find_program(), on_address=on_address).start()
        return _current
