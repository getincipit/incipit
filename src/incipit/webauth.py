"""Sign-in, and the single place authorization is decided."""

from __future__ import annotations

import csv
from html import escape as escape_html
import io
import re
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import accounts, bootstrap, collab, db, people_routes, presence, security, site_routes, site_settings, tunnel, updates
from .assets import APP_STATIC_DIR

SESSION_COOKIE = "incipit_session"

# Reachable without signing in.
PUBLIC_PATHS = ("/login", "/signup", "/health", "/setup", "/api/setup/owner", "/api/site", "/site.css",
                "/certificate.crt")
PUBLIC_PREFIXES = ("/static/", "/favicon", "/site/assets/")
PUBLIC_FILES = ("/login.html", "/style.css", "/logo.svg", "/favicon.svg", "/favicon.ico",
                "/backdrop.css", "/terms.js", "/page-kit.js", "/login-backdrop.js", "/appearance.js", "/people.js",
                "/setup.js")

# Admin-only.
ADMIN_ONLY = (
    (re.compile(r"^/api/folder-exports/"), "export"),
    (re.compile(r"^/api/settings"), "settings"),
    (re.compile(r"^/api/team"), "team"),
    # Both spellings, because the route is /api/deletion-log and this rule used to say only
    # /api/deletions -- which matches nothing, so the log of who deleted what, and why, was
    # readable by any signed-in account.
    (re.compile(r"^/api/deletion"), "destruction"),
    # What a folder cost and how long it took is budget information -- the same reason spend does
    # not appear on a reviewer's screen anywhere else.
    (re.compile(r"^/api/analytics"), "drafting analytics"),
    (re.compile(r"^/api/reviewer-colours"), "reviewer colours"),
    (re.compile(r"^/api/site/"), "site settings"),
    # The owner's alone; the routes check that, the gate keeps everyone else below admin out.
    (re.compile(r"^/api/setup(/|$)"), "setup"),
    # What the model is told, and which version a tab or folder uses.
    (re.compile(r"^/api/prompt-profiles(/|$)"), "prompt profiles"),
    (re.compile(r"^/api/new/folders/[^/]+/prompt-version$"), "prompt profiles"),
    # How the host is reached and whether it starts at login: the administrators' business.
    (re.compile(r"^/api/access(/|$)"), "access"),
    # The key, the models and what they cost; the owner-only parts are checked in the routes.
    (re.compile(r"^/api/models(/|$)"), "models"),
    (re.compile(r"^/api/usage(/|$)"), "usage"),
    (re.compile(r"^/api/backups(/|$)"), "backups"),
    (re.compile(r"^/api/updates(/|$)"), "updates"),
    (re.compile(r"^/api/support(/|$)"), "support"),
    (re.compile(r"^/api/jobs/[^/]+/(cancel|retry)$"), "jobs"),
)

# Admin-only to change, readable by anyone signed in: the tab strip is drawn from GET /api/tabs.
ADMIN_ONLY_WRITES = (
    (re.compile(r"^/api/tabs(/|$)"), "tabs"),
)

# What an account told to change its password may still reach: the change itself, and the way out.
PASSWORD_CHANGE_ALLOWED = ("/api/me", "/api/me/password", "/logout", "/api/site")

# Admin-only by verb rather than path: a reviewer may read a document but must never delete one.
ADMIN_ONLY_METHODS = ("DELETE",)


def is_public(path: str) -> bool:
    return (
        path in PUBLIC_PATHS
        or path in PUBLIC_FILES
        or any(path.startswith(p) for p in PUBLIC_PREFIXES)
    )


def admin_capability(path: str, method: str) -> str | None:
    """The capability a path needs, or None if any signed-in user may use it."""
    for pattern, capability in ADMIN_ONLY:
        if pattern.match(path):
            return capability
    if method != "GET":
        for pattern, capability in ADMIN_ONLY_WRITES:
            if pattern.match(path):
                return capability
    if method in ADMIN_ONLY_METHODS:
        return "destruction"
    return None


def install(app, settings) -> None:
    """Adds the gate and the sign-in routes to an existing app."""

    auth_attempts = security.AttemptLimiter()
    presence_tracker = presence.Tracker()
    app.state.presence_tracker = presence_tracker

    bootstrap.initialise(settings.db_path)
    with db.connect(settings.db_path) as conn:
        # Folders submitted with every document already accepted, before submit settled approval.
        collab.approve_ready_submissions(conn)

    def current_user(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        with db.connect(settings.db_path) as conn:
            return accounts.user_for_token(conn, token)

    app.state.current_user = current_user
    # Exposed so a test client can create a real account and sign in against this app's own
    # database.
    app.state.auth_settings = settings

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if (path in ("/", "/login") and security.at_the_host(request) and not bootstrap.has_owner(settings.db_path)
                and site_settings.bootstrap_path(settings.db_path).is_file()):
            # Setup is waiting and this is the host's own browser: straight to the wizard.
            return security.add_browser_headers(RedirectResponse("/setup", status_code=303))
        if is_public(path):
            # Public pages may still differ for someone signed in (the setup wizard does).
            request.state.user = current_user(request)
            response = await call_next(request)
            return security.add_browser_headers(response)

        user = current_user(request)
        if user is None:
            # An API call gets 401 so the frontend can react; a page gets a redirect so a person
            # lands on the login form instead of a bare error.
            if path.startswith("/api/"):
                return security.add_browser_headers(
                    JSONResponse({"detail": "sign in to continue"}, status_code=401)
                )
            return security.add_browser_headers(
                RedirectResponse(f"/login?next={quote(path)}", status_code=303)
            )

        if user["must_change_password"] and path.startswith("/api/") and path not in PASSWORD_CHANGE_ALLOWED:
            return security.add_browser_headers(JSONResponse(
                {"detail": "choose a new password before anything else", "code": "must_change_password"},
                status_code=403))

        capability = admin_capability(path, request.method)
        if capability and not accounts.is_admin(user):
            return security.add_browser_headers(
                JSONResponse(
                    {"detail": f"that action ({capability}) is for admins only"}, status_code=403
                )
            )

        # The owner's first visit goes to the setup wizard until it is finished.
        if path == "/" and accounts.is_owner(user):
            with db.connect(settings.db_path) as conn:
                unfinished = not site_settings.setup_finished(conn)
            if unfinished:
                return security.add_browser_headers(RedirectResponse("/setup", status_code=303))

        request.state.user = user
        response = await call_next(request)
        return security.add_browser_headers(response)

    # ---------- signing in ----------

    def login_html(**kwargs) -> str:
        with db.connect(settings.db_path) as conn:
            site = site_settings.public(conn)
        return _login_html(site=site, **kwargs)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(next: str = "/", join: str = ""):
        return login_html(next=security.safe_local_redirect(next), tab="signup" if join else "login")

    @app.post("/login")
    async def do_login(request: Request):
        form = await request.form()
        identity = f"login:{tunnel.visitor_address(request)}"
        if not auth_attempts.allowed(identity):
            return JSONResponse(
                {"detail": "too many sign-in attempts; try again in a few minutes"},
                status_code=429,
                headers={"Retry-After": str(auth_attempts.window_seconds)},
            )
        try:
            with db.connect(settings.db_path) as conn:
                token = accounts.login(conn, form.get("username", ""), form.get("password", ""))
        except accounts.AuthError as exc:
            auth_attempts.failed(identity)
            return HTMLResponse(
                login_html(error=str(exc), next=security.safe_local_redirect(form.get("next"))),
                status_code=401,
            )
        auth_attempts.succeeded(identity)
        return _redirect_with_session(
            security.safe_local_redirect(form.get("next")), token,
            secure=tunnel.visitor_scheme(request) == "https"
        )

    @app.post("/signup")
    async def do_signup(request: Request):
        form = await request.form()
        identity = f"signup:{tunnel.visitor_address(request)}"
        if not auth_attempts.consume(identity):
            return JSONResponse(
                {"detail": "too many account-creation attempts; try again in a few minutes"},
                status_code=429,
                headers={"Retry-After": str(auth_attempts.window_seconds)},
            )
        code = (form.get("join_code") or "").strip()
        try:
            with db.connect(settings.db_path) as conn:
                site = site_settings.settings(conn)
                if not site["join_enabled"]:
                    raise accounts.AuthError("joining this site is closed; ask an administrator")
                if code:
                    _, role = accounts.redeem_join_code(conn, code)
                elif site["reviewer_self_signup"]:
                    role = "reviewer"
                else:
                    raise accounts.AuthError("ask an administrator for a join code")
                user_id = accounts.create_user(
                    conn,
                    username=form.get("username", ""),
                    password=form.get("password", ""),
                    display_name=form.get("display_name", ""),
                    role=role,
                )
                token = accounts.start_session(conn, user_id)
        except accounts.AuthError as exc:
            # A wrong code counts against the same allowance as any other attempt.
            return HTMLResponse(login_html(error=str(exc), tab="signup"), status_code=400)
        return _redirect_with_session("/", token, secure=tunnel.visitor_scheme(request) == "https")

    @app.post("/logout")
    def do_logout(request: Request):
        with db.connect(settings.db_path) as conn:
            accounts.logout(conn, request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/api/me")
    def me(request: Request):
        user = current_user(request)
        return {
            # The id is here so the page can tell "you have this" from "someone else has this" on a
            # folder claim.
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "must_change_password": bool(user["must_change_password"]),
        }

    site_routes.install(app, settings)
    people_routes.install(app, settings)

    @app.post("/api/me/password")
    async def change_own_password(request: Request):
        """Requires the current password, so a browser left open cannot be used to lock its owner
        out of their own account."""
        user = current_user(request)
        form = await request.json()
        try:
            with db.connect(settings.db_path) as conn:
                accounts.login(conn, user["username"], form.get("current", ""))
                accounts.set_password(conn, user["id"], form.get("new", ""))
        except accounts.AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.post("/api/me/close")
    def close_account(request: Request):
        """POST, not DELETE: DELETE is admin-only by verb, and this is the one destructive act a
        reviewer is entitled to perform on their own behalf."""
        user = current_user(request)
        try:
            with db.connect(settings.db_path) as conn:
                accounts.close_own_account(conn, user["id"])
        except accounts.PermissionDenied as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        except accounts.AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ---------- the admin's team view ----------

    # ---------- comments on a document ----------

    def _may_touch(request, document_id: str, workflow: str) -> bool:
        """Comments follow the same visibility rule as everything else: an admin anywhere, a
        reviewer only inside a folder they hold."""
        if workflow not in accounts.WORKFLOWS:
            return False
        from . import scoping
        user = request.state.user
        with db.connect(settings.db_path) as conn:
            return scoping.may_access_document(conn, user, document_id, workflow)

    @app.get("/api/documents/{document_id}/comments")
    def get_comments(request: Request, document_id: str, workflow: str = "new"):
        if not _may_touch(request, document_id, workflow):
            return JSONResponse({"detail": "that document is not yours"}, status_code=403)
        with db.connect(settings.db_path) as conn:
            return {"comments": collab.comments_for(conn, document_id, workflow)}

    @app.post("/api/documents/{document_id}/comments")
    async def post_comment(request: Request, document_id: str):
        body = await request.json()
        workflow = body.get("workflow", "new")
        if not _may_touch(request, document_id, workflow):
            return JSONResponse({"detail": "that document is not yours"}, status_code=403)
        try:
            with db.connect(settings.db_path) as conn:
                collab.add_comment(conn, document_id, workflow, request.state.user["id"], body.get("body", ""))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return {"ok": True}

    @app.post("/api/comments/{comment_id}/resolve")
    def resolve_comment(request: Request, comment_id: int):
        with db.connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT document_id, workflow FROM document_comments WHERE id = ?", (comment_id,)
            ).fetchone()
            if row is None:
                return JSONResponse({"detail": "no such comment"}, status_code=404)
            if not _may_touch(request, row["document_id"], row["workflow"]):
                return JSONResponse({"detail": "that comment is not yours"}, status_code=403)
            collab.resolve_comment(conn, comment_id, request.state.user["id"])
        return {"ok": True}

    @app.get("/api/documents/{document_id}/revisions")
    def get_revisions(request: Request, document_id: str, workflow: str = "new"):
        if not _may_touch(request, document_id, workflow):
            return JSONResponse({"detail": "that document is not yours"}, status_code=403)
        with db.connect(settings.db_path) as conn:
            return {"revisions": collab.revisions_for(conn, document_id, workflow)}

    # ---------- statistics and notifications ----------

    @app.post("/api/presence")
    async def update_presence(request: Request):
        """A tiny heartbeat from any signed-in browser; no database write and no backup churn."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        presence_tracker.touch(
            request.state.user,
            document_id=str(body.get("document_id") or ""),
            workflow=str(body.get("workflow") or ""),
        )
        return {"ok": True}

    @app.get("/api/team/presence")
    def team_presence():
        """Admin-only through the existing /api/team authorization boundary."""
        return {"people": presence_tracker.snapshot(),
                "active_seconds": presence.ACTIVE_SECONDS}

    @app.get("/api/team/stats")
    def team_stats():
        with db.connect(settings.db_path) as conn:
            return {"contributors": collab.contributor_stats(conn)}

    @app.get("/api/notifications")
    def get_notifications(request: Request):
        """Carries the queue fingerprint too, so the poll the bell already runs can tell the page
        when to redraw."""
        with db.connect(settings.db_path) as conn:
            payload = collab.notifications_for(conn, request.state.user)
            payload["version"] = collab.workspace_version(conn, request.state.user)
            return payload

    @app.post("/api/notifications/read")
    def read_notifications(request: Request):
        with db.connect(settings.db_path) as conn:
            collab.mark_notifications_read(conn, request.state.user["id"])
        return {"ok": True}

    @app.get("/api/team/folders")
    def team_folders():
        """Every folder that actually exists, with who holds it."""
        out = []
        with db.connect(settings.db_path) as conn:
            counts = {row["folder_id"]: row["n"] for row in conn.execute(
                "SELECT folder_id, COUNT(*) AS n FROM new_documents GROUP BY folder_id")}
            uploaded = {row["folder_id"] for row in conn.execute(
                "SELECT DISTINCT folder_id FROM new_batches WHERE folder_id IS NOT NULL")}
            labels = db.folder_labels(conn)
            # Folders uploaded but not yet split count too: they are exactly the ones an admin
            # most wants to hand out.
            for folder_id in sorted(set(counts) | uploaded,
                                    key=lambda f: labels.get(f, {}).get("folder_display_id", f)):
                label = labels.get(folder_id)
                if label is None:
                    continue
                out.append({
                    "folder_id": folder_id,
                    "folder_display_id": label["folder_display_id"],
                    "label": label["folder_title"],
                    "workflow": "new",
                    "tab_id": label["tab_id"],
                    "tab_slug": label["tab_slug"],
                    "tab_label": label["tab_label"],
                    "documents": counts.get(folder_id, 0),
                })
            for folder in out:
                assignment = accounts.get_assignment(conn, folder["folder_id"], folder["workflow"])
                holder = accounts.get_user(conn, assignment["assigned_to"]) if assignment and assignment["assigned_to"] else None
                folder["assigned_to_name"] = holder["display_name"] if holder else ""
                folder["assignment_status"] = assignment["status"] if assignment else "unassigned"
        return {"folders": out}

    @app.get("/api/team/roster")
    def team_roster():
        with db.connect(settings.db_path) as conn:
            return {"roster": db.with_folder_labels(conn, accounts.roster(conn)), "reviewers": [
                {"id": u["id"], "username": u["username"], "display_name": u["display_name"]}
                for u in accounts.list_users(conn, "reviewer")
            ]}

    @app.get("/api/team/roster.csv")
    def team_roster_csv():
        """The roster as a clean CSV: who did which folder, who gave it to them, and when."""
        with db.connect(settings.db_path) as conn:
            rows = db.with_folder_labels(conn, accounts.roster(conn))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Name", "Folder", "Assigned By", "Date"])
        for r in rows:
            writer.writerow([
                r["reviewer"], r["folder_display_id"] or r["folder_id"], r["assigned_by"],
                (r["assigned_at"] or "")[:10],
            ])
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="roster.csv"'},
        )

    @app.post("/api/team/assign")
    async def team_assign(request: Request):
        """Assigning is validated input from a person, so every way it can be wrong has to come
        back as a sentence rather than a stack trace."""
        try:
            form = await request.json()
            folder_id = str(form["folder_id"]).strip()
            workflow = form.get("workflow", "new")
            with db.connect(settings.db_path) as conn:
                # Refuse a folder that does not exist.
                known = {(f["folder_id"], f["workflow"]) for f in team_folders()["folders"]}
                if known and (folder_id, workflow) not in known:
                    return JSONResponse(
                        {"detail": "there is no such folder -- pick one from the list rather "
                                   "than typing it"},
                        status_code=400,
                    )
                accounts.assign_folder(
                    conn, folder_id, workflow,
                    int(form["reviewer_id"]), request.state.user["id"], form.get("note", ""),
                )
        except accounts.AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except (KeyError, TypeError, ValueError):
            return JSONResponse(
                {"detail": "folder_id and reviewer_id are required, and reviewer_id must be a number"},
                status_code=400,
            )
        return {"ok": True}

    @app.post("/api/team/unassign")
    async def team_unassign(request: Request):
        """Takes a folder back from the reviewer holding it."""
        try:
            form = await request.json()
            folder_id = str(form["folder_id"]).strip()
            workflow = form.get("workflow", "new")
            with db.connect(settings.db_path) as conn:
                reviewer = accounts.unassign_folder(
                    conn, folder_id, workflow, request.state.user["id"], form.get("note", ""))
        except accounts.AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"detail": "folder_id is required"}, status_code=400)
        return {"ok": True, "reviewer": reviewer}

    @app.get("/api/team/claims")
    def team_claims():
        with db.connect(settings.db_path) as conn:
            return {"claims": db.get_folder_claims(conn)}

    @app.post("/api/team/claim")
    async def team_claim(request: Request):
        """Claim or release a folder."""
        body = await request.json()
        folder_id = (body.get("folder_id") or "").strip()
        workflow = body.get("workflow", "new")
        if not folder_id:
            return JSONResponse({"detail": "which folder?"}, status_code=400)
        if workflow not in accounts.WORKFLOWS:
            return JSONResponse({"detail": "workflow must be one of " + ", ".join(accounts.WORKFLOWS)}, status_code=400)
        with db.connect(settings.db_path) as conn:
            if body.get("release"):
                db.release_folder(conn, folder_id, workflow)
            else:
                db.claim_folder(conn, folder_id, workflow, request.state.user["id"])
        return {"ok": True}

    @app.get("/api/team/history")
    def team_history(folder_id: str, workflow: str = "new"):
        if workflow not in accounts.WORKFLOWS:
            return JSONResponse({"detail": "workflow must be one of " + ", ".join(accounts.WORKFLOWS)}, status_code=400)
        with db.connect(settings.db_path) as conn:
            return {"history": accounts.folder_history(conn, folder_id, workflow)}

    # ---------- the reviewer's own view ----------

    @app.get("/api/my/folders")
    def my_folders(request: Request):
        """Only what this person holds."""
        user = request.state.user
        with db.connect(settings.db_path) as conn:
            rows = accounts.folders_for_reviewer(conn, user["id"])
            labels = db.folder_labels(conn)
            return {"folders": [
                {
                    "folder_id": r["folder_id"],
                    **labels.get(r["folder_id"], {"folder_display_id": "", "folder_title": ""}),
                    "workflow": r["workflow"],
                    "status": r["status"],
                    "assigned_at": r["assigned_at"],
                    "updated_at": r["updated_at"],
                    "history": accounts.folder_history(conn, r["folder_id"], r["workflow"]),
                }
                for r in rows
            ]}

    @app.post("/api/my/submit")
    async def my_submit(request: Request):
        body = await request.json()
        workflow = body.get("workflow", "new")
        # Validated rather than indexed: a body without folder_id raised KeyError, which reaches
        # the client as a 500 -- a malformed request reported as a server fault.
        folder_id = (body.get("folder_id") or "").strip()
        if not folder_id:
            return JSONResponse({"detail": "which folder?"}, status_code=400)
        if workflow not in accounts.WORKFLOWS:
            return JSONResponse({"detail": "workflow must be one of " + ", ".join(accounts.WORKFLOWS)}, status_code=400)
        try:
            with db.connect(settings.db_path) as conn:
                # Authorize BEFORE checking readiness.
                assignment = accounts.get_assignment(conn, folder_id, workflow)
                if assignment is None:
                    return JSONResponse({"detail": "that folder is not assigned"}, status_code=403)
                if (not accounts.is_admin(request.state.user)
                        and assignment["assigned_to"] != request.state.user["id"]):
                    return JSONResponse(
                        {"detail": "that folder is not assigned to you"}, status_code=403
                    )
                # Gated on the documents, not on the button.
                collab.check_can_submit(conn, folder_id, workflow)
                accounts.set_folder_status(
                    conn, folder_id, workflow, "submitted",
                    request.state.user, body.get("note", ""),
                )
                # Accepted before it was handed in: no later verdict would ever approve it.
                collab.approve_when_every_document_is_accepted(conn, folder_id, workflow)
        except collab.NotReady as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except accounts.PermissionDenied as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        except accounts.AuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return {"ok": True}

    @app.post("/api/team/review")
    async def team_review(request: Request):
        """An admin returning a folder with notes, or approving it."""
        body = await request.json()
        status = body.get("status")
        if status not in ("returned", "approved"):
            return JSONResponse({"detail": "status must be 'returned' or 'approved'"}, status_code=400)
        workflow = body.get("workflow", "new")
        folder_id = (body.get("folder_id") or "").strip()
        if not folder_id:
            return JSONResponse({"detail": "which folder?"}, status_code=400)
        if workflow not in accounts.WORKFLOWS:
            return JSONResponse({"detail": "workflow must be one of " + ", ".join(accounts.WORKFLOWS)}, status_code=400)
        # A reviewer sent work back with no reason has nothing to act on.
        if status == "returned" and not (body.get("note") or "").strip():
            return JSONResponse({"detail": "You must enter a reason to send this back to the reviewer."},
                                status_code=400)
        try:
            with db.connect(settings.db_path) as conn:
                if status == "approved":
                    # Approval is what makes a folder exportable.
                    collab.check_can_approve(conn, folder_id, workflow)
                # Returning a folder can name the documents that need more work.
                rework = [d for d in (body.get("rework_documents") or []) if isinstance(d, str)]
                if status == "returned" and rework:
                    collab.mark_documents_for_rework(conn, folder_id, workflow, rework,
                                                     request.state.user["id"])
                accounts.set_folder_status(
                    conn, folder_id, workflow, status,
                    request.state.user, body.get("note", ""),
                )
        except collab.NotReady as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except (accounts.PermissionDenied, accounts.AuthError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return {"ok": True}


def _redirect_with_session(target: str, token: str, *, secure: bool = False) -> RedirectResponse:
    response = RedirectResponse(security.safe_local_redirect(target), status_code=303)
    return session_cookie(response, token, secure=secure)


def session_cookie(response, token: str, *, secure: bool = False):
    """Sets the session cookie on any response."""
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,      # unreadable from JavaScript, so an XSS bug cannot lift the session
        samesite="lax",     # not sent on cross-site POSTs, which is the CSRF case that matters here
        secure=secure,
        max_age=int(accounts.SESSION_LIFETIME.total_seconds()),
        path="/",
    )
    return response


def _login_html(site: dict, error: str = "", next: str = "/", tab: str = "login") -> str:
    """The sign-in card, named and branded from the site's public settings."""
    look = site["appearance"]
    joining = site["join_enabled"]
    if tab == "signup" and not joining:
        error = error or "Joining this site is closed. Ask an administrator for an account."
        tab = "login"
    logo = f"/site/assets/{look['logo_asset']}" if look.get("logo_asset") else "/logo.svg"
    # Versioned, so a browser that cached another program's icon at this port fetches this one.
    favicon = (f"/site/assets/{look['favicon_asset']}" if look.get("favicon_asset")
               else f"/favicon.svg?v={updates.current_version()}")
    hint = ("Leave the join code blank to join as a {reviewer}.".format(
        reviewer=site["labels"]["reviewer"].lower()) if site["self_signup"]
            else "An administrator gives you a join code.")
    fill = {
        "{{PRODUCT}}": site["product_name"],
        "{{TAGLINE}}": look.get("tagline") or "",
        "{{WELCOME}}": look.get("welcome") or "",
        "{{FOOTER}}": look.get("footer") or "",
        "{{LOGO}}": logo,
        "{{MARK}}": "" if look.get("logo_asset") else "stock",
        "{{BACKDROP}}": (look.get("backdrop") or {}).get("kind") or "none",
        "{{FAVICON}}": favicon,
        "{{SIGNUP_HINT}}": hint,
    }
    html = (APP_STATIC_DIR / "login.html").read_text(encoding="utf-8")
    for key, value in fill.items():
        html = html.replace(key, escape_html(value, quote=True))
    return (
        html.replace("{{ERROR}}",
                     f'<div class="auth-error">{escape_html(error, quote=True)}</div>' if error else "")
        .replace("{{NEXT}}", escape_html(security.safe_local_redirect(next), quote=True))
        .replace("{{TAB}}", "signup" if tab == "signup" else "login")
        .replace("{{SIGNUP_HIDDEN}}", "" if joining else "hidden")
    )
