"""HTTP routes for Settings → Updates: the version, the check, installing a newer release (the owner's
decision), and the support bundle."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import accounts, support, updates


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def install(app, settings) -> None:

    @app.get("/api/updates")
    def get_updates():
        return updates.state()

    @app.post("/api/updates/check")
    def post_check():
        return updates.check()

    @app.post("/api/updates/install")
    def post_install(request: Request):
        """Downloads and checks the newer release, starts its installer, and stops the site for it."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner installs an update")
        # Read fresh: a release remembered from an earlier check may since have been replaced.
        current = updates.check()
        if current["state"] != "available":
            return _error(409, "there is no newer version to install")
        try:
            installer = updates.download(current["release"])
            updates.start_installer(installer)
        except (updates.UpdateError, OSError) as exc:
            return _error(400, str(exc))
        stop = getattr(request.app.state, "stop_for_update", None)
        if stop is not None:
            # After the reply, so the page knows to wait for the new version.
            threading.Timer(1.0, stop).start()
        return {**updates.state(), "installing": current["release"]["version"]}

    @app.get("/api/support/bundle")
    def get_bundle():
        if not settings.data_folder:
            return _error(409, "this site is not run from a data folder")
        return Response(support.build(Path(settings.data_folder)), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{support.bundle_name()}"'})
