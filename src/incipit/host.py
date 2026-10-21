"""What the host computer is called on the network, the links a team uses to reach the site, and a
test of whether the port answers from the host itself."""

from __future__ import annotations

import socket

PROBE_TIMEOUT = 1.0


def hostname() -> str:
    return socket.gethostname()


def lan_addresses() -> list[str]:
    """The host's own IPv4 addresses on its networks, the one that reaches the internet first."""
    found: list[str] = []
    # The address the host would leave through: no packet is sent for a UDP connect.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(hostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out: list[str] = []
    for address in found:
        if address not in out and not address.startswith("127.") and not address.startswith("169.254."):
            out.append(address)
    return out


def link(address: str, port: int, scheme: str = "http") -> str:
    return f"{scheme}://{address}:{port}/"


def links(port: int, addresses: list[str] | None = None, site_name: str = "") -> list[dict]:
    """The links to share: the site's own name first, then each network address, the computer's name,
    and this computer only."""
    addresses = lan_addresses() if addresses is None else addresses
    out = [{"address": f"{site_name}.local", "link": link(f"{site_name}.local", port), "kind": "site"}] if site_name else []
    out += [{"address": a, "link": link(a, port), "kind": "network"} for a in addresses]
    out.append({"address": hostname(), "link": link(hostname(), port), "kind": "name"})
    out.append({"address": "localhost", "link": link("localhost", port), "kind": "local"})
    return out


def probe(address: str, port: int, timeout: float = PROBE_TIMEOUT) -> dict:
    """Whether something answers on `address:port` from this computer, and why not if not."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return {"address": address, "reachable": True, "detail": "answered"}
    except socket.timeout:
        return {"address": address, "reachable": False,
                "detail": "no answer; a firewall rule on this computer may be blocking the port"}
    except OSError as exc:
        return {"address": address, "reachable": False, "detail": exc.strerror or str(exc)}


def test_port(port: int, addresses: list[str] | None = None) -> list[dict]:
    addresses = lan_addresses() if addresses is None else addresses
    return [probe(a, port) for a in [*addresses, "localhost"]]
