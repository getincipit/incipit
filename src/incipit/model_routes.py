"""HTTP routes for Settings → Models, Settings → Usage and the jobs the page follows."""

from __future__ import annotations

import math
import threading

from fastapi import Request
from fastapi.responses import JSONResponse

from . import accounts, backends, db, jobs, ledger, scoping, site_settings


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _actor(request: Request) -> str:
    user = request.state.user
    return user["username"] if user is not None else ""


def job_payload(job: dict) -> dict:
    """What anyone allowed to see a job is told: never cost, which lives in the ledger."""
    keys = ("id", "kind", "folder_id", "batch_id", "label", "state", "total", "done", "current_document",
            "created_at", "started_at", "finished_at", "cancel_requested", "error_kind", "error_text")
    shown = {k: job.get(k) for k in keys}
    shown["stopped_by_cap"] = (job.get("result") or {}).get("stopped_by_cap")
    return shown


def install(app, settings, handlers: dict) -> None:

    @app.get("/api/models")
    def get_models():
        with db.connect(settings.db_path) as conn:
            return backends.public(conn)

    @app.put("/api/models")
    async def put_models(request: Request):
        """The owner's choice of service, key and models, applied to the next call."""
        if not accounts.is_owner(request.state.user):
            return _error(403, "only the owner changes the model service and its key")
        body = await request.json()
        changes = {k: body[k] for k in ("kind", "base_url", "api_key", "model", "fallback_model") if k in body}
        try:
            with db.connect(settings.db_path) as conn:
                backends.save(conn, _actor(request), **changes)
                backends.apply(conn, settings)
        except ValueError as exc:
            return _error(400, str(exc))
        with db.connect(settings.db_path) as conn:
            return backends.public(conn)

    @app.post("/api/models/test")
    def test_models():
        """Checks the key and the server without spending anything; a pass completes the wizard's step."""
        with db.connect(settings.db_path) as conn:
            result = backends.test(conn)
            if result["ok"]:
                site_settings.mark_step(conn, "model")
            return {**result, "backend": backends.public(conn)}

    @app.get("/api/models/catalogue")
    def get_catalogue():
        try:
            return {"models": backends.catalogue()}
        except Exception as exc:  # noqa: BLE001 - the picker falls back to typing a model id
            return _error(502, f"OpenRouter's model list could not be fetched ({exc.__class__.__name__}); "
                               "type the model's id instead")

    @app.get("/api/usage")
    def get_usage():
        with db.connect(settings.db_path) as conn:
            caps = db.get_app_settings(conn)
            return {**ledger.usage(conn), "max_total_cost": caps["max_total_cost"], "monthly_cap": caps["monthly_cap"]}

    @app.put("/api/usage/caps")
    async def put_caps(request: Request):
        body = await request.json()
        values = {}
        try:
            if "max_total_cost" in body:
                values["max_total_cost"] = float(body["max_total_cost"])
                if not math.isfinite(values["max_total_cost"]) or values["max_total_cost"] <= 0:
                    raise ValueError
            if "monthly_cap" in body:
                values["monthly_cap"] = None if body["monthly_cap"] in (None, "") else float(body["monthly_cap"])
                if values["monthly_cap"] is not None and (not math.isfinite(values["monthly_cap"])
                                                          or values["monthly_cap"] <= 0):
                    raise ValueError
        except (TypeError, ValueError):
            return _error(400, "a cap is an amount of dollars above zero; leave the monthly cap empty for none")
        with db.connect(settings.db_path) as conn:
            for key, value in values.items():
                conn.execute(f"UPDATE app_settings SET {key} = ? WHERE id = 1", (value,))
            caps = db.get_app_settings(conn)
        settings.max_total_cost, settings.monthly_cap = caps["max_total_cost"], caps["monthly_cap"]
        return caps

    def _visible(request: Request, job: dict) -> bool:
        user = request.state.user
        if scoping.is_admin(user):
            return True
        with db.connect(settings.db_path) as conn:
            visible = scoping.visible_folder_ids(conn, user, "new")
            folder_id = job.get("folder_id")
            if folder_id is None and job.get("batch_id"):
                # A job recorded before it carried its folder: the batch says which.
                batch = db.get_batch(conn, job["batch_id"])
                folder_id = db.row_text(batch, "folder_id") if batch is not None else None
        return visible is None or folder_id in visible

    @app.get("/api/jobs")
    def list_jobs(request: Request, folder_id: str | None = None):
        shown = [job_payload(j) for j in jobs.listing(settings.db_path, folder_id) if _visible(request, j)]
        return {"jobs": shown}

    @app.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: int):
        job = jobs.get(settings.db_path, job_id)
        if job is None or not _visible(request, job):
            return _error(404, "no such job")
        return job_payload(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: int):
        job = jobs.cancel(settings.db_path, job_id)
        if job is None:
            return _error(404, "no such job")
        return job_payload(job)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(request: Request, job_id: int):
        """Queues a failed job again and starts it; the page follows it as before."""
        job = jobs.retry(settings.db_path, job_id)
        if job is None:
            return _error(404, "no such job")
        if job["state"] == "queued":
            threading.Thread(target=jobs.run_and_wait, args=(settings.db_path, handlers, job), daemon=True,
                             name=f"incipit-job-{job_id}").start()
        return job_payload(job)
