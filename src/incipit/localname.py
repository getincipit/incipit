"""The site's friendly name on the local network: `{name}.local`, announced by multicast DNS (mDNS),
so the team opens http://our-archive.local:8010/ rather than an IP address that can change.

Windows 10 and later, macOS, iOS and most Linux desktops resolve `.local` names with nothing set up;
the host only has to keep announcing while the site runs, which `Announcer` does.
"""

from __future__ import annotations

import logging
import re
import socket
import threading

from . import host

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SERVICE_TYPE = "_http._tcp.local."


def normalise(value) -> str:
    """A name as people type it ("Our Archive") made a valid label ("our-archive"), or ValueError."""
    name = str(value or "").strip().lower()
    # Before the clean-up, which would turn the dot into a hyphen.
    if name.endswith(".local"):
        name = name[: -len(".local")]
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not NAME.fullmatch(name):
        raise ValueError("a site name is 1 to 63 letters, digits or hyphens, such as our-archive")
    return name


def hostname(name: str) -> str:
    return f"{name}.local"


def link(name: str, port: int) -> str:
    return host.link(hostname(name), port)


def resolves_elsewhere(name: str, own: list[str]) -> list[str]:
    """Addresses another computer already answers this name with, so two hosts do not share one."""
    try:
        found = {info[4][0] for info in socket.getaddrinfo(hostname(name), None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(found - set(own))


class Announcer:
    """Keeps `{name}.local` and an HTTP service record answered for the host's addresses while the site runs."""

    def __init__(self, name: str, port: int, title: str = "Incipit", zeroconf_factory=None):
        self.name, self.port, self.title = name, port, title
        self._factory = zeroconf_factory
        self._zeroconf = None
        self._info = None
        self.error = ""
        self.addresses: list[str] = []

    @property
    def announcing(self) -> bool:
        return self._info is not None and not self.error

    def start(self) -> "Announcer":
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            self.error = "the local-name service is not installed (pip install zeroconf)"
            logger.warning("%s", self.error)
            return self
        self.addresses = host.lan_addresses()
        if not self.addresses:
            self.error = "no network address to announce; the site is reachable from this computer only"
            logger.warning("%s", self.error)
            return self
        clash = resolves_elsewhere(self.name, self.addresses)
        if clash:
            self.error = f"{hostname(self.name)} already answers from {', '.join(clash)}; choose another name"
            logger.warning("%s", self.error)
            return self
        try:
            self._zeroconf = (self._factory or Zeroconf)()
            self._info = ServiceInfo(
                SERVICE_TYPE, f"{self.title} ({self.name}).{SERVICE_TYPE}",
                addresses=[socket.inet_aton(a) for a in self.addresses], port=self.port,
                server=f"{hostname(self.name)}.", properties={"path": "/"})
            self._zeroconf.register_service(self._info, allow_name_change=True)
            logger.info("announcing http://%s:%d/ for %s", hostname(self.name), self.port, ", ".join(self.addresses))
        except Exception as exc:  # noqa: BLE001 - a missing name must never stop the site itself
            self.error = f"the name could not be announced: {exc}"
            self._info = None
            logger.warning("%s", self.error)
        return self

    def stop(self) -> None:
        if self._zeroconf is not None:
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
            finally:
                self._zeroconf.close()
        self._zeroconf, self._info = None, None


# The running site's announcer, which Settings -> Access reads and replaces.
_lock = threading.Lock()
_current: Announcer | None = None


def current() -> Announcer | None:
    return _current


def announce(name: str, port: int, title: str = "Incipit") -> Announcer | None:
    """Starts announcing `name` (stopping any earlier name); an empty name stops announcing."""
    global _current
    with _lock:
        if _current is not None:
            _current.stop()
            _current = None
        if name:
            _current = Announcer(name, port, title).start()
        return _current
