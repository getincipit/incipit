"""HTTP routes for the site's tabs: the strip everyone reads, and the settings administrators change."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from . import db, tabs


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _optional_int(value):
    return None if value in (None, "") else int(value)


def install(app, settings) -> None:

    def run(action):
        """One tab action, with a refusal mapped to the status that says why."""
        try:
            with db.connect(settings.db_path) as conn:
                return action(conn)
        except tabs.TabConflict as exc:
            return _error(409, str(exc))
        except (tabs.TabError, ValueError, TypeError) as exc:
            return _error(400, str(exc))
        except LookupError as exc:
            return _error(404, str(exc))

    @app.get("/api/tabs")
    def list_tabs(request: Request):
        with db.connect(settings.db_path) as conn:
            return {"tabs": tabs.for_viewer(conn, request.state.user)}

    @app.post("/api/tabs")
    async def create_tab(request: Request):
        body = await request.json()

        def create(conn):
            tab_id = tabs.create(conn, body.get("label", ""), body.get("id_scheme") or "letters",
                                 _optional_int(body.get("prompt_profile_id")))
            return {"id": tab_id, "tab": tabs.get(conn, tab_id)}
        return run(create)

    @app.post("/api/tabs/reorder")
    async def reorder_tabs(request: Request):
        body = await request.json()
        return run(lambda conn: {"tabs": tabs.reorder(conn, body.get("order") or [])})

    @app.put("/api/tabs/{tab_id}")
    async def update_tab(request: Request, tab_id: int):
        body = await request.json()
        changes = {k: v for k, v in body.items() if k in ("label", "enabled", "id_scheme", "prompt_profile_id")}
        if "prompt_profile_id" in changes:
            changes["prompt_profile_id"] = _optional_int(changes["prompt_profile_id"])
        return run(lambda conn: {"tab": tabs.update(conn, tab_id, changes or body)})

    @app.delete("/api/tabs/{tab_id}")
    def delete_tab(tab_id: int):
        return run(lambda conn: tabs.delete(conn, tab_id) or {"ok": True})

    @app.post("/api/tabs/{tab_id}/prompt-profile")
    async def set_tab_profile(request: Request, tab_id: int):
        body = await request.json()
        return run(lambda conn: {"tab": tabs.update(
            conn, tab_id, {"prompt_profile_id": _optional_int(body.get("profile_id"))})})
