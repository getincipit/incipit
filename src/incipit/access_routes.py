"""HTTP routes for Settings → Access and the wizard's access step: the links a team uses to reach the
host, the port, start at login, and a test of the port from the host itself."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import time

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import accounts, datafolder, db, fixedlink, host, localname, portmap, relocate, site_settings, startup, tls, tunnel

PRIVATE_BINDS = ("127.0.0.1", "localhost", "::1")


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def payload(settings) -> dict:
    """What the page shows: how the site is reached now, and what the host can change."""
    folder = settings.data_folder
    addresses = host.lan_addresses()
    announcer = localname.current()
    return {
        "hostname": host.hostname(),
        "addresses": addresses,
        "port": settings.port,
        "bind": settings.host,
        # Bound to this computer only: the links would not work from another machine.
        "private": settings.host in PRIVATE_BINDS,
        "site_name": settings.site_name,
        "local_name": {
            "name": settings.site_name,
            "link": localname.link(settings.site_name, settings.port) if settings.site_name else "",
            "announcing": bool(announcer and announcer.announcing and announcer.name == settings.site_name),
            "error": announcer.error if announcer and announcer.name == settings.site_name else "",
        },
        "links": host.links(settings.port, addresses, settings.site_name if announcer and announcer.announcing else ""),
        "https": secure(settings, addresses),
        "internet": internet(settings),
        "tunnel": tunnel_payload(settings),
        "fixed_link": fixed_link_payload(settings),
        "data_folder": folder,
        "configured": bool(folder),
        "start_at_login": {
            "supported": bool(folder) and startup.supported(),
            "registered": startup.status(runner=startup.run) if folder else False,
            "command": startup._quote(startup.serve_command(folder)) if folder else "",
        },
    }


def secure(settings, addresses: list[str]) -> dict:
    """The HTTPS listener, its certificate's fingerprint, and the https links that reach it."""
    port = tls.serving["port"]
    names = ([localname.hostname(settings.site_name)] if settings.site_name else []) + addresses
    return {
        "port": settings.https_port,
        "serving": bool(port),
        "error": tls.serving["error"],
        "fingerprint": tls.fingerprint(settings.tls_dir) if settings.tls_dir else "",
        "expires": tls.expires(settings.tls_dir) if settings.tls_dir else "",
        "links": [host.link(name, port, "https") for name in names] if port else [],
        "certificate_url": "/certificate.crt",
    }


def internet(settings) -> dict:
    """Whether the router has opened the HTTPS port, and the address that reaches the site from outside."""
    forwarder = portmap.current()
    if not settings.forward_port:
        return {"enabled": False, "state": "off", "error": "", "external_address": "", "link": ""}
    if not tls.serving["port"]:
        # Plain HTTP is never opened to the internet.
        return {"enabled": True, "state": "no_https", "error": tls.serving["error"], "external_address": "", "link": ""}
    if forwarder is None or forwarder.port != tls.serving["port"]:
        return {"enabled": True, "state": "searching", "error": "", "external_address": "", "link": ""}
    return {"enabled": True, **forwarder.status()}


# How long "auto" waits for the router's answer before deciding a tunnel is needed.
ROUTER_WAIT_SECONDS = 30


def reach_outside(settings, wait: float = ROUTER_WAIT_SECONDS) -> None:
    """Opens or closes the quick tunnel for the tunnel setting and what the router managed."""
    if settings.tunnel == "off" or not settings.tunnel_dir:
        tunnel.open_tunnel(None)
        return
    if settings.tunnel == "auto":
        deadline = time.monotonic() + wait
        while settings.forward_port and internet(settings)["state"] == "searching" and time.monotonic() < deadline:
            time.sleep(0.5)
        if internet(settings)["state"] == "open":
            tunnel.open_tunnel(None)
            return
    running = tunnel.current()
    if running is None or running.port != settings.port:
        tunnel.open_tunnel(settings.port, settings.tunnel_dir, on_address=lambda link: _announce(settings, link))


_links: dict[str, fixedlink.FixedLink] = {}


def site_title(settings) -> str:
    """The site's name as the owner set it, which the team's page is titled after."""
    try:
        with db.connect(settings.db_path) as conn:
            return site_settings.public(conn)["product_name"] or "Incipit"
    except Exception:  # noqa: BLE001 - a missing name must not stop the link
        return "Incipit"


def team_link(settings) -> fixedlink.FixedLink:
    """The site's fixed link, one per tunnel folder; the name is read afresh, so a renamed site renames its page."""
    if settings.tunnel_dir not in _links:
        _links[settings.tunnel_dir] = fixedlink.FixedLink(settings.tunnel_dir)
    _links[settings.tunnel_dir].title = site_title(settings)
    return _links[settings.tunnel_dir]


def _announce(settings, link: str) -> None:
    if settings.fixed_link:
        team_link(settings).announce(link)


def leave_outside(settings) -> None:
    """Closes the tunnel when the site stops, and tells the team's page the site is not running."""
    was_open = tunnel.current() is not None
    tunnel.open_tunnel(None)
    if was_open and settings.fixed_link and settings.tunnel_dir:
        team_link(settings).withdraw()


def fixed_link_payload(settings) -> dict:
    if not settings.tunnel_dir:
        return {"enabled": False, "state": "off", "url": "", "address": "", "updated": "", "error": ""}
    return {"enabled": settings.fixed_link, **team_link(settings).status()}


def tunnel_payload(settings) -> dict:
    running = tunnel.current()
    if settings.tunnel == "off":
        return {"mode": "off", "state": "off", "link": "", "error": ""}
    if running is None:
        # "auto" while the router is asked, or because it gave a working address.
        return {"mode": settings.tunnel, "state": "standby", "link": "", "error": ""}
    return {"mode": settings.tunnel, **running.status()}


def storage_payload(settings) -> dict:
    folder = Path(settings.data_folder) if settings.data_folder else None
    if folder is None:
        return {"data_folder": "", "configured": False}
    pending = relocate.pending()
    return {
        "data_folder": str(folder),
        "configured": True,
        "size_bytes": relocate.folder_size(folder),
        "free_bytes": shutil.disk_usage(folder).free,
        "move_to": str(pending) if pending else "",
        "move_failed": relocate.failure_note(folder),
    }


def apply_live(settings, changes: dict) -> None:
    """What takes effect while the site runs, once the file holds it: the name, forwarding, the tunnel
    and the team's link (the ports wait for the next start)."""
    public = settings.host not in PRIVATE_BINDS
    if "site_name" in changes and changes["site_name"] != settings.site_name:
        settings.site_name = changes["site_name"]
        if public:
            localname.announce(settings.site_name, settings.port)
    if "forward_port" in changes and changes["forward_port"] != settings.forward_port:
        settings.forward_port = changes["forward_port"]
        if public:
            portmap.forward(tls.serving["port"] if settings.forward_port and tls.serving["port"] else None)
    if "fixed_link" in changes and changes["fixed_link"] != settings.fixed_link:
        settings.fixed_link = changes["fixed_link"]
        running = tunnel.current()
        if settings.fixed_link and running is not None and running.address:
            threading.Thread(target=team_link(settings).announce, args=(running.address + "/",), daemon=True).start()
        elif not settings.fixed_link:
            threading.Thread(target=team_link(settings).withdraw, daemon=True).start()
    if "tunnel" in changes and changes["tunnel"] != settings.tunnel:
        settings.tunnel = changes["tunnel"]
        threading.Thread(target=reach_outside, args=(settings,), daemon=True, name="incipit-outside").start()


def install(app, settings) -> None:

    @app.get("/certificate.crt")
    def get_certificate():
        """The site's own certificate, public, so a team member can install it and stop the warning."""
        cert_path, _ = tls.paths(settings.tls_dir) if settings.tls_dir else (None, None)
        if cert_path is None or not cert_path.is_file():
            return _error(404, "this site has no certificate yet")
        return Response(cert_path.read_bytes(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="incipit-site.crt"'})

    @app.post("/api/access/fixed-link/replace")
    def replace_fixed_link(request: Request):
        """A new team link, for one shared too widely; the old page says it no longer works."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner replaces the team's link")
        if not settings.tunnel_dir:
            return _error(409, "this site is not run from a data folder")
        link = team_link(settings)
        link.replace()
        running = tunnel.current()
        if running is not None and running.address and settings.fixed_link:
            threading.Thread(target=link.announce, args=(running.address + "/",), daemon=True).start()
        return payload(settings)

    @app.get("/api/access")
    def get_access():
        return payload(settings)

    @app.post("/api/access/test")
    def test_access():
        return {"results": host.test_port(settings.port)}

    @app.get("/api/access/storage")
    def get_storage(request: Request):
        """Where the data lives, how much there is, and any move asked for (owner only)."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner sees where the data lives")
        return storage_payload(settings)

    @app.post("/api/access/move")
    async def post_move(request: Request):
        """Checks the new folder, records the move and restarts the site, which carries it out."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner moves the data")
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder")
        body = await request.json()
        try:
            target = relocate.request(settings.data_folder, body.get("target"))
        except relocate.MoveError as exc:
            return _error(400, str(exc))
        restart = getattr(request.app.state, "request_restart", None)
        if restart is not None:
            # After the reply is sent, so the page learns the move is under way.
            threading.Timer(0.5, restart).start()
        return {**storage_payload(settings), "move_to": str(target), "restarting": restart is not None}

    @app.delete("/api/access/move")
    def delete_move(request: Request):
        """Cancels a move not yet carried out and dismisses the note of one that failed."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner moves the data")
        relocate.cancel()
        if settings.data_folder:
            relocate.dismiss_failure(settings.data_folder)
        return storage_payload(settings)

    @app.put("/api/access")
    async def put_access(request: Request):
        """The owner's changes: the port (for the next start) and whether the site starts at login."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner changes how the site is reached")
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder, so the port is set where it is started")
        body = await request.json()
        changes, restart_needed = {}, False
        if "port" in body:
            try:
                port = int(body["port"])
            except (TypeError, ValueError):
                return _error(400, "the port is a whole number from 1 to 65535")
            if not 1 <= port <= 65535:
                return _error(400, "the port is a whole number from 1 to 65535")
            changes["port"] = port
            restart_needed = port != settings.port
        if "https_port" in body:
            try:
                https_port = int(body["https_port"])
            except (TypeError, ValueError):
                return _error(400, "the HTTPS port is a whole number from 1 to 65535")
            if not 1 <= https_port <= 65535 or https_port == changes.get("port", settings.port):
                return _error(400, "the HTTPS port is a whole number from 1 to 65535, other than the port")
            changes["https_port"] = https_port
            restart_needed = restart_needed or https_port != settings.https_port
        if "site_name" in body:
            try:
                changes["site_name"] = localname.normalise(body["site_name"]) if str(body["site_name"] or "").strip() else ""
            except ValueError as exc:
                return _error(400, str(exc))
        for key in ("forward_port", "fixed_link", "start_at_login"):
            if key in body:
                changes[key] = bool(body[key])
        if "tunnel" in body:
            changes["tunnel"] = str(body["tunnel"] or "").strip().lower()
            if changes["tunnel"] not in tunnel.MODES:
                return _error(400, "the tunnel is off, auto or on")
        if "start_at_login" in changes:
            try:
                if changes["start_at_login"]:
                    startup.register(settings.data_folder, runner=startup.run)
                else:
                    startup.unregister(runner=startup.run)
            except RuntimeError as exc:
                return _error(400, f"start at login could not be changed: {exc}")
        try:
            datafolder.update(settings.data_folder, **changes)
        except datafolder.ConfigError as exc:
            return _error(400, str(exc))
        apply_live(settings, changes)
        return {**payload(settings), "pending_port": changes.get("port"), "restart_needed": restart_needed}
