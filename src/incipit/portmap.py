"""Opening the site's port on the router by UPnP, so the team can reach it from outside the network
with nothing set up by hand.

The host asks the router (an Internet Gateway Device) to forward its port to this computer, renews
the lease while the site runs, and removes the forwarding when it stops. Routers with UPnP turned
off, and networks behind a second router or the provider's shared address (CGNAT), cannot be opened
this way; the status says which, so Settings → Access can tell the owner what to do.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urljoin
from xml.etree import ElementTree

from . import host

logger = logging.getLogger(__name__)

SSDP_ADDRESS = ("239.255.255.250", 1900)
SEARCH_TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                  "urn:schemas-upnp-org:device:InternetGatewayDevice:2")
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")
DESCRIPTION = "Incipit"
LEASE_SECONDS = 3600
# Renewed well before the lease ends, so a slow router never lets it lapse.
RENEW_SECONDS = 1200
# The provider's shared range: an address here means another network sits between router and internet.
CGNAT = ipaddress.ip_network("100.64.0.0/10")


class PortMapError(RuntimeError):
    """The router refused or could not be reached; the message says which."""


def search(timeout: float = 2.5) -> list[str]:
    """The description URLs of the gateways that answer an SSDP search on this network."""
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        for target in SEARCH_TARGETS:
            message = (f"M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
                       f"MAN: \"ssdp:discover\"\r\nMX: 2\r\nST: {target}\r\n\r\n")
            sock.sendto(message.encode("ascii"), SSDP_ADDRESS)
        try:
            while True:
                data, _ = sock.recvfrom(4096)
                match = re.search(r"^location:\s*(\S+)", data.decode("latin-1"), re.I | re.M)
                if match and match.group(1) not in found:
                    found.append(match.group(1))
        except (socket.timeout, OSError):
            pass
    return found


def _fetch(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 5) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - the router on this network
        return response.read()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def control_url(description_url: str, description: bytes) -> tuple[str, str] | None:
    """(control URL, service type) of the gateway's WAN connection service, from its description XML."""
    root = ElementTree.fromstring(description)
    base = next((el.text for el in root.iter() if _local(el.tag) == "URLBase" and el.text), description_url)
    services = {}
    for service in (el for el in root.iter() if _local(el.tag) == "service"):
        fields = {_local(child.tag): (child.text or "").strip() for child in service}
        if fields.get("serviceType") in SERVICES and fields.get("controlURL"):
            services[fields["serviceType"]] = urljoin(base, fields["controlURL"])
    for kind in SERVICES:
        if kind in services:
            return services[kind], kind
    return None


class Gateway:
    """One router's WAN connection service, spoken to over SOAP."""

    def __init__(self, url: str, service: str, local_address: str, fetch=_fetch):
        self.url, self.service, self.local_address = url, service, local_address
        self._fetch = fetch

    def call(self, action: str, **arguments) -> dict:
        body = "".join(f"<{k}>{v}</{k}>" for k, v in arguments.items())
        envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    f'<u:{action} xmlns:u="{self.service}">{body}</u:{action}></s:Body></s:Envelope>')
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{self.service}#{action}"'}
        try:
            reply = self._fetch(self.url, envelope.encode("utf-8"), headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read() if hasattr(exc, "read") else b""
            code = re.search(rb"<errorCode>(\d+)</errorCode>", detail or b"")
            text = re.search(rb"<errorDescription>([^<]*)</errorDescription>", detail or b"")
            raise PortMapError(f"the router refused {action}"
                               + (f" ({code.group(1).decode()}: {text.group(1).decode() if text else ''})" if code else "")
                               ) from None
        except OSError as exc:
            raise PortMapError(f"the router did not answer ({exc})") from None
        root = ElementTree.fromstring(reply)
        return {_local(el.tag): (el.text or "") for el in root.iter() if len(el) == 0}

    def external_address(self) -> str:
        return self.call("GetExternalIPAddress").get("NewExternalIPAddress", "")

    def add(self, port: int, lease: int = LEASE_SECONDS) -> int:
        """Forwards `port` to this computer; returns the lease the router accepted (0: until removed)."""
        arguments = dict(NewRemoteHost="", NewExternalPort=port, NewProtocol="TCP", NewInternalPort=port,
                         NewInternalClient=self.local_address, NewEnabled=1, NewPortMappingDescription=DESCRIPTION)
        try:
            self.call("AddPortMapping", **arguments, NewLeaseDuration=lease)
            return lease
        except PortMapError as exc:
            # 725: the router takes only permanent mappings, which the site removes when it stops.
            if lease and "725" in str(exc):
                self.call("AddPortMapping", **arguments, NewLeaseDuration=0)
                return 0
            raise

    def remove(self, port: int) -> None:
        self.call("DeletePortMapping", NewRemoteHost="", NewExternalPort=port, NewProtocol="TCP")


def _route_address(router_url: str) -> str:
    """This computer's address on the network the router is on."""
    hostname = urllib.parse.urlparse(router_url).hostname or "192.168.1.1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((hostname, 1900))
        return sock.getsockname()[0]


def find_gateway(fetch=_fetch, searcher=search) -> Gateway | None:
    for location in searcher():
        try:
            found = control_url(location, fetch(location))
        except (OSError, ElementTree.ParseError):
            continue
        if found:
            return Gateway(found[0], found[1], _route_address(location), fetch=fetch)
    return None


def classify(address: str) -> str:
    """"public", "shared" (CGNAT) or "private" (a second router) for the address the router reports."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if ip in CGNAT:
        return "shared"
    return "private" if ip.is_private else "public"


class Forwarder:
    """Keeps the port forwarded while the site runs; `status` is what Settings → Access shows."""

    def __init__(self, port: int, finder=find_gateway):
        self.port = port
        self._finder = finder
        self.gateway: Gateway | None = None
        self.state = "searching"
        self.external_address = ""
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def open_once(self) -> None:
        try:
            self.gateway = self.gateway or self._finder()
            if self.gateway is None:
                self.state, self.error = "no_router", "no router on this network answered (UPnP may be turned off)"
                return
            self.gateway.add(self.port)
            self.external_address = self.gateway.external_address()
            kind = classify(self.external_address)
            if kind == "public":
                self.state, self.error = "open", ""
            else:
                self.state = "double_nat"
                self.error = ("the internet provider shares one address between customers (CGNAT)" if kind == "shared"
                              else "another router sits between this one and the internet")
        except PortMapError as exc:
            self.state, self.error = "refused", str(exc)
        logger.info("port forwarding: %s %s", self.state, self.error or self.external_address)

    def _run(self) -> None:
        self.open_once()
        # Renewing also retries a router that was busy or rebooting at the first attempt.
        while not self._stop.wait(RENEW_SECONDS):
            self.open_once()

    def start(self) -> "Forwarder":
        self._thread = threading.Thread(target=self._run, name="incipit-portmap", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.gateway is not None and self.state in ("open", "double_nat"):
            try:
                self.gateway.remove(self.port)
            except PortMapError as exc:
                logger.warning("the forwarding was not removed: %s", exc)
        self.state = "off"

    def status(self) -> dict:
        public = self.state == "open"
        return {"state": self.state, "error": self.error, "external_address": self.external_address,
                # Only the HTTPS port is ever forwarded (review_server.run_server).
                "link": host.link(self.external_address, self.port, "https") if public else ""}


# The running site's forwarder, which Settings → Access reads and replaces.
_lock = threading.Lock()
_current: Forwarder | None = None


def current() -> Forwarder | None:
    return _current


def forward(port: int | None, finder=find_gateway) -> Forwarder | None:
    """Starts forwarding `port` (stopping any earlier forwarding); None stops forwarding."""
    global _current
    with _lock:
        if _current is not None:
            _current.stop()
            _current = None
        if port:
            _current = Forwarder(port, finder).start()
        return _current
