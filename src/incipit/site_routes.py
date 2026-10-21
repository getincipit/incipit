"""HTTP routes for the site's settings, appearance, branding files and first-run setup."""

from __future__ import annotations

import json
from html import escape as escape_html

from fastapi import Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import accounts, appearance, db, site_assets, site_settings, tunnel
from .assets import APP_STATIC_DIR
from .storage import BucketStorage

# Served to a browser that may cache it forever: an asset's id never names different bytes.
_ASSET_CACHE = "public, max-age=31536000, immutable"
# An SVG opened on its own cannot run anything, whatever the sanitiser missed.
_SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _font_urls(conn) -> dict:
    return {a["id"]: a["url"] for a in site_assets.listing(conn, "font")}


def install(app, settings) -> None:
    storage = BucketStorage(settings.bucket_uri)

    def viewer(request: Request):
        return getattr(request.state, "user", None)

    # ---------- public: what the sign-in page and the shell draw ----------

    @app.get("/api/site")
    def public_site():
        with db.connect(settings.db_path) as conn:
            return site_settings.public(conn)

    @app.get("/site.css")
    def site_css():
        with db.connect(settings.db_path) as conn:
            look = site_settings.settings(conn)["appearance"]
            css = appearance.stylesheet(look, _font_urls(conn))
        return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico")
    def favicon_ico():
        """A browser that asks by this name gets the site's own mark, as every page's <link> does."""
        with db.connect(settings.db_path) as conn:
            look = site_settings.settings(conn)["appearance"]
        target = f"/site/assets/{look['favicon_asset']}" if look.get("favicon_asset") else "/favicon.svg"
        return RedirectResponse(target, status_code=302)

    @app.get("/site/assets/{asset_id}")
    def site_asset(asset_id: int):
        with db.connect(settings.db_path) as conn:
            found = site_assets.row(conn, asset_id)
        if found is None:
            return _error(404, "no such file")
        try:
            content = storage.read_bytes(found["bucket_key"])
        except OSError:
            return _error(404, "that file is missing from storage")
        headers = {"Cache-Control": _ASSET_CACHE}
        if found["content_type"] == "image/svg+xml":
            headers["Content-Security-Policy"] = _SVG_POLICY
        return Response(content, media_type=found["content_type"], headers=headers)

    def _page(name: str) -> HTMLResponse:
        """A page named and labelled from the site's settings before anything paints."""
        with db.connect(settings.db_path) as conn:
            site = site_settings.public(conn)
        look = site["appearance"]
        # "</" would let a value close the inline <script>; JSON reads "<\/" as the same text.
        payload = json.dumps(site).replace("</", "<\\/")
        fill = {"{{PRODUCT}}": escape_html(site["product_name"], quote=True),
                "{{LOGO}}": f"/site/assets/{look['logo_asset']}" if look.get("logo_asset") else "logo.svg",
                "{{FAVICON}}": f"/site/assets/{look['favicon_asset']}" if look.get("favicon_asset")
                else "favicon.svg",
                "{{SITE_JSON}}": payload}
        html = (APP_STATIC_DIR / name).read_text(encoding="utf-8")
        for key, value in fill.items():
            html = html.replace(key, value)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def shell():
        return _page("index.html")

    @app.get("/index.html")
    def shell_by_name():
        return _page("index.html")

    # ---------- settings: owner and admins ----------

    def _full(conn) -> dict:
        return {**site_settings.settings(conn), "assets": site_assets.listing(conn),
                "setup": site_settings.setup_state(conn)}

    @app.get("/api/site/settings")
    def get_site_settings():
        with db.connect(settings.db_path) as conn:
            return _full(conn)

    @app.put("/api/site/settings")
    async def put_site_settings(request: Request):
        changes = await request.json()
        if not isinstance(changes, dict):
            return _error(400, "send the settings as an object")
        removed: list[str] = []
        look = changes.get("appearance")
        if isinstance(look, dict) and "custom_css" in look:
            look["custom_css"], removed = appearance.sanitize_css(str(look["custom_css"] or ""))
        try:
            with db.connect(settings.db_path) as conn:
                site_settings.update(conn, viewer(request), changes)
                return {**_full(conn), "removed": removed}
        except PermissionError as exc:
            return _error(403, str(exc))
        except (site_settings.SettingsError, appearance.AppearanceError) as exc:
            return _error(400, str(exc))

    @app.post("/api/site/org-id")
    def post_org_id(request: Request):
        try:
            with db.connect(settings.db_path) as conn:
                return {"org_id": site_settings.regenerate_org_id(conn, viewer(request))}
        except PermissionError as exc:
            return _error(403, str(exc))

    # ---------- branding files ----------

    @app.post("/api/site/assets")
    async def post_site_asset(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
        user = viewer(request)
        if kind == "font" and not accounts.is_owner(user):
            return _error(403, "only the owner can upload a font")
        # One byte past the limit is enough to know it is too large, without reading the rest.
        content = await file.read(site_assets.LIMITS.get(kind, 0) + 1)
        try:
            with db.connect(settings.db_path) as conn:
                return site_assets.store(conn, storage, kind, file.filename or "", content, user["id"])
        except site_assets.AssetError as exc:
            return _error(400, str(exc))

    @app.patch("/api/site/assets/{asset_id}")
    async def patch_site_asset(request: Request, asset_id: int):
        body = await request.json()
        try:
            with db.connect(settings.db_path) as conn:
                return site_assets.describe(conn, asset_id, body.get("caption"), body.get("credit"))
        except site_assets.AssetError as exc:
            return _error(400, str(exc))

    @app.post("/api/site/assets/order")
    async def post_asset_order(request: Request):
        body = await request.json()
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return _error(400, "send the files' ids in their new order")
        with db.connect(settings.db_path) as conn:
            site_assets.reorder(conn, ids)
            return {"assets": site_assets.listing(conn)}

    @app.delete("/api/site/assets/{asset_id}")
    def delete_site_asset(request: Request, asset_id: int):
        try:
            with db.connect(settings.db_path) as conn:
                found = site_assets.row(conn, asset_id)
                if found is not None and found["kind"] == "font" and not accounts.is_owner(viewer(request)):
                    return _error(403, "only the owner can remove a font")
                site_settings.forget_asset(conn, asset_id)
                site_assets.remove(conn, storage, asset_id)
        except site_assets.AssetError as exc:
            return _error(404, str(exc))
        return {"ok": True}

    # ---------- first-run setup ----------

    @app.get("/setup")
    def setup_page(request: Request, token: str = ""):
        with db.connect(settings.db_path) as conn:
            has_owner = accounts.owner_exists(conn)
        if not has_owner:
            # Nothing to say to someone without the token, not even that setup is waiting.
            if not site_settings.bootstrap_token_matches(settings.db_path, token):
                return _error(404, "Not Found")
            return _page("setup.html")
        user = viewer(request)
        if user is None:
            return RedirectResponse("/login?next=/setup", status_code=303)
        if not accounts.is_owner(user):
            return RedirectResponse("/", status_code=303)
        return _page("setup.html")

    @app.post("/api/setup/owner")
    async def post_setup_owner(request: Request):
        body = await request.json()
        with db.connect(settings.db_path) as conn:
            if accounts.owner_exists(conn):
                return _error(404, "Not Found")
        if not site_settings.bootstrap_token_matches(settings.db_path, body.get("token")):
            return _error(404, "Not Found")
        try:
            with db.connect(settings.db_path) as conn:
                owner_id = accounts.create_owner(conn, body.get("username", ""), body.get("password", ""),
                                                 body.get("display_name", ""))
        except accounts.AuthError as exc:
            return _error(400, str(exc))
        # Spent: the token proved the right to create one owner, and that owner now exists.
        site_settings.remove_bootstrap_token(settings.db_path)
        with db.connect(settings.db_path) as conn:
            token = accounts.start_session(conn, owner_id)
        from .webauth import session_cookie
        response = JSONResponse({"ok": True, "next": "/setup"})
        session_cookie(response, token, secure=tunnel.visitor_scheme(request) == "https")
        return response

    def _owner_only(request: Request):
        if not accounts.is_owner(viewer(request)):
            return _error(403, "only the owner runs the setup")
        return None

    @app.get("/api/setup")
    def get_setup(request: Request):
        refused = _owner_only(request)
        if refused:
            return refused
        with db.connect(settings.db_path) as conn:
            return site_settings.setup_state(conn)

    @app.post("/api/setup/steps/{step}")
    async def post_setup_step(request: Request, step: str):
        refused = _owner_only(request)
        if refused:
            return refused
        body = await request.json()
        try:
            with db.connect(settings.db_path) as conn:
                return site_settings.mark_step(conn, step, bool(body.get("done", True)))
        except site_settings.SettingsError as exc:
            return _error(400, str(exc))

    @app.post("/api/setup/finish")
    def post_setup_finish(request: Request):
        refused = _owner_only(request)
        if refused:
            return refused
        try:
            with db.connect(settings.db_path) as conn:
                return site_settings.finish_setup(conn)
        except site_settings.SettingsError as exc:
            return _error(409, str(exc))
