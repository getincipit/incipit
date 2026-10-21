"""HTTP routes for Settings → Prompt: profiles, their versions, the live preview and the sample test."""

from __future__ import annotations

from fastapi import File, Request, UploadFile
from fastapi.responses import JSONResponse

from . import db, fields, imaging, openrouter, profiles, prompting
from .new_document import retitle
from .storage import BucketStorage

MAX_TEST_IMAGES = 3
# A sample row the preview renders the title template with, so the owner sees what it produces.
SAMPLE_VALUES = {"document_type": "Letter", "author": "Smith, Jane", "recipient": "Brown, Thomas",
                 "date": "1901-03-04", "place": "Boston, MA"}


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _readable(template: str) -> str:
    """A stored prompt as the model reads it, with its live placeholders left showing."""
    return template.replace("{{", "{").replace("}}", "}")


def _sample_values(styles: dict) -> dict:
    if styles.get("names") == "last_first":
        return SAMPLE_VALUES
    return {**SAMPLE_VALUES, "author": "Jane Smith", "recipient": "Thomas Brown"}


def _shown(compiled: prompting.Compiled) -> dict:
    """A compiled version as the builder shows it: prompts, form fields, columns and a sample title."""
    return {
        "answers": compiled.answers,
        "document_prompt": _readable(compiled.document_prompt),
        "split_prompt": _readable(compiled.split_prompt),
        "merge_prompt": _readable(compiled.merge_prompt),
        "fields": fields.describe(compiled.registry),
        "export_columns": fields.export_columns(compiled.registry),
        "sample_title": fields.render_title(
            compiled.answers.get("title_template") or fields.DEFAULT_TITLE_TEMPLATE, "A001",
            _sample_values(compiled.styles), compiled.styles, compiled.registry),
        "content_hash": compiled.content_hash,
        "warnings": list(compiled.warnings),
    }


def install(app, settings) -> None:

    def run(action):
        """One profile action, mapping a refused answer to 400 and a missing row to 404."""
        try:
            with db.connect(settings.db_path) as conn:
                return action(conn)
        except prompting.CompileError as exc:
            return _error(400, str(exc))
        except LookupError as exc:
            return _error(404, str(exc))

    def actor(request: Request):
        return request.state.user["id"]

    def profile_payload(conn, profile_id: int) -> dict:
        found = profiles.profile(conn, profile_id)
        if found is None:
            raise LookupError("no such prompt profile")
        current = profiles.compiled(conn, found["current_version_id"])
        return {**found, "current": _shown(current), "versions": profiles.versions(conn, profile_id)}

    @app.get("/api/prompt-profiles/questionnaire")
    def questionnaire():
        return prompting.QUESTIONNAIRE

    @app.get("/api/prompt-profiles")
    def list_profiles():
        with db.connect(settings.db_path) as conn:
            return {"profiles": profiles.profiles(conn)}

    @app.post("/api/prompt-profiles")
    async def create_profile(request: Request):
        body = await request.json()
        return run(lambda conn: {"id": profiles.create_profile(
            conn, body.get("name", ""), body.get("answers") or {}, actor(request))})

    @app.post("/api/prompt-profiles/preview")
    async def preview(request: Request):
        body = await request.json()
        try:
            return _shown(prompting.compile(body.get("answers") or {}))
        except prompting.CompileError as exc:
            # The builder shows this beside the answers; nothing was saved.
            return _error(400, str(exc))

    @app.get("/api/prompt-profiles/versions/{version_id}")
    def get_version(version_id: int):
        def read(conn):
            found = profiles.version(conn, version_id)
            if found is None:
                raise LookupError("no such prompt version")
            return {"id": version_id, "profile_id": found["profile_id"], "version_no": found["version_no"],
                    "label": profiles.label(conn, version_id), "created_at": found["created_at"],
                    **_shown(profiles.compiled(conn, version_id))}
        return run(read)

    @app.get("/api/prompt-profiles/{profile_id}")
    def get_profile(profile_id: int):
        return run(lambda conn: profile_payload(conn, profile_id))

    @app.put("/api/prompt-profiles/{profile_id}")
    async def rename_profile(request: Request, profile_id: int):
        body = await request.json()

        def rename(conn):
            if profiles.profile(conn, profile_id) is None:
                raise LookupError("no such prompt profile")
            profiles.rename(conn, profile_id, body.get("name", ""))
            return {"ok": True}
        return run(rename)

    @app.get("/api/prompt-profiles/{profile_id}/versions")
    def list_versions(profile_id: int):
        return run(lambda conn: {"versions": profiles.versions(conn, profile_id)})

    @app.post("/api/prompt-profiles/{profile_id}/versions")
    async def add_version(request: Request, profile_id: int):
        body = await request.json()
        return run(lambda conn: {"id": profiles.add_version(
            conn, profile_id, body.get("answers") or {}, actor(request))})

    @app.post("/api/prompt-profiles/{profile_id}/current")
    async def set_current(request: Request, profile_id: int):
        body = await request.json()
        return run(lambda conn: profiles.set_current(conn, profile_id, int(body.get("version_id") or 0))
                   or {"ok": True})

    @app.post("/api/prompt-profiles/{profile_id}/test")
    async def test_profile(request: Request, profile_id: int, files: list[UploadFile] = File(...)):
        if not 1 <= len(files) <= MAX_TEST_IMAGES:
            return _error(400, f"a prompt test takes 1 to {MAX_TEST_IMAGES} images")
        if not settings.openrouter_api_key:
            return _error(400, "no model key is set: add INCIPIT_OPENROUTER_API_KEY to the server's environment")
        images = []
        for upload in files:
            try:
                content, _ = imaging.normalize_image_bytes(await upload.read(), upload.filename or "")
            except ValueError as exc:
                return _error(400, f"{upload.filename}: {exc}")
            images.append(imaging.downscale_for_model(content))
        with db.connect(settings.db_path) as conn:
            found = profiles.profile(conn, profile_id)
            if found is None:
                return _error(404, "no such prompt profile")
            spent = db.get_total_cost(conn)
            if spent >= settings.max_total_cost:
                return _error(409, f"spend ${spent:.2f} has reached the ${settings.max_total_cost:.2f} cap; "
                                   "raise it in Settings before testing")
            version_id = found["current_version_id"]
            compiled = profiles.compiled(conn, version_id)
        try:
            result = openrouter.call_openrouter(
                openrouter.build_new_document_messages(images, compiled), settings,
                schema=compiled.document_schema, extract_result=openrouter.document_extractor(compiled.registry))
        except Exception as exc:  # noqa: BLE001 - every model failure is the same answer to the owner
            return _error(502, f"the model could not draft the sample: {exc}")
        shown = {"text": result["text"], "uncertain_words": result["uncertain_words"],
                 "fields": result["fields"]}
        with db.connect(settings.db_path) as conn:
            profiles.record_test(conn, version_id, actor(request), len(images), result.get("cost"), shown)
        return {**shown, "version_id": version_id, "cost": result.get("cost"),
                "field_registry": fields.describe(compiled.registry)}

    @app.post("/api/new/folders/{folder_id}/prompt-version")
    async def repin_folder(request: Request, folder_id: str):
        body = await request.json()

        def repin(conn):
            if db.get_folder(conn, folder_id) is None:
                raise LookupError("no such folder")
            moved = profiles.repin_folder(conn, folder_id, int(body.get("version_id") or 0))
            # A different template gives different titles; the values themselves stay by key.
            storage = BucketStorage(settings.bucket_uri)
            for row in db.get_new_documents_in_folder(conn, folder_id):
                retitle(conn, row["document_id"], storage)
            return {"ok": True, "documents": moved}
        return run(repin)
