"""HTTP routes for the Team tab's People and Invite cards: accounts, roles and join codes."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from . import accounts, db


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _optional_int(value):
    return None if value in (None, "") else int(value)


def install(app, settings) -> None:

    def act(request: Request, action):
        """Runs one account action as the signed-in person, mapping its refusals to HTTP."""
        try:
            with db.connect(settings.db_path) as conn:
                return action(conn, request.state.user)
        except accounts.PermissionDenied as exc:
            return _error(403, str(exc))
        except (accounts.AuthError, ValueError, TypeError) as exc:
            return _error(400, str(exc))

    @app.get("/api/team/users")
    def list_people(request: Request):
        user = request.state.user
        with db.connect(settings.db_path) as conn:
            return {"people": accounts.people(conn), "me": user["id"],
                    "may_add": ["admin", "reviewer"] if accounts.is_owner(user) else ["reviewer"]}

    @app.post("/api/team/users")
    async def add_person(request: Request):
        body = await request.json()

        def create(conn, actor):
            role = body.get("role", "reviewer")
            accounts.require_may_create(actor, role)
            user_id = accounts.create_user(conn, body.get("username", ""), body.get("password", ""),
                                           body.get("display_name", ""), role, created_by=actor["id"])
            # A password someone else chose is temporary by nature.
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (user_id,))
            return {"id": user_id}
        return act(request, create)

    @app.post("/api/team/users/{user_id}/deactivate")
    def deactivate(request: Request, user_id: int):
        return act(request, lambda conn, actor: accounts.deactivate(conn, actor, user_id) or {"ok": True})

    @app.post("/api/team/users/{user_id}/reactivate")
    def reactivate(request: Request, user_id: int):
        return act(request, lambda conn, actor: accounts.reactivate(conn, actor, user_id) or {"ok": True})

    @app.post("/api/team/users/{user_id}/reset-password")
    async def reset_password(request: Request, user_id: int):
        body = await request.json()
        return act(request, lambda conn, actor: accounts.reset_password(
            conn, actor, user_id, body.get("password", "")) or {"ok": True})

    @app.post("/api/team/users/{user_id}/role")
    async def change_role(request: Request, user_id: int):
        body = await request.json()
        return act(request, lambda conn, actor: accounts.change_role(
            conn, actor, user_id, body.get("role", "")) or {"ok": True})

    @app.post("/api/team/owner/transfer")
    async def transfer(request: Request):
        body = await request.json()
        return act(request, lambda conn, actor: accounts.transfer_ownership(
            conn, actor, int(body.get("user_id", 0))) or {"ok": True})

    @app.get("/api/team/join-codes")
    def list_codes(request: Request):
        with db.connect(settings.db_path) as conn:
            return {"codes": accounts.join_codes(conn, request.state.user)}

    @app.post("/api/team/join-codes")
    async def add_code(request: Request):
        body = await request.json()

        def create(conn, actor):
            code = accounts.create_join_code(
                conn, actor, body.get("role", "reviewer"), body.get("label", ""),
                _optional_int(body.get("expires_days", 14)), _optional_int(body.get("max_uses")))
            # Shown this once: only its hash is kept.
            return {"code": code, "codes": accounts.join_codes(conn, actor)}
        return act(request, create)

    @app.delete("/api/team/join-codes/{code_id}")
    def revoke_code(request: Request, code_id: int):
        return act(request, lambda conn, actor: accounts.revoke_join_code(conn, actor, code_id) or {"ok": True})
