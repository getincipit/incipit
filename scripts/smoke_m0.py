#!/usr/bin/env python
"""M0 smoke: drive the running site over HTTP as admin and reviewer, upload to export.

Serves a real app on a throwaway database and bucket with the model call replaced by a fake, so
the journey runs end to end without a key: upload six drawn pages, split, confirm two documents,
draft, review as the reviewer, send back, resubmit, accept as the admin, export. Every step prints
PASS or FAIL and the exit code says whether all passed.

    .venv/Scripts/python.exe scripts/smoke_m0.py [--port 8097]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
import uvicorn
from PIL import Image, ImageDraw

from incipit import accounts, collab, db, openrouter, review_server
from incipit.config import Settings

ADMIN, REVIEWER = ("paul", "paul-password"), ("john", "john-password")
FOLDER = "SM01"


def drawn_page(seed: int) -> bytes:
    """A page of drawn 'handwriting', different for each seed so duplicate detection has work."""
    image = Image.new("RGB", (600, 800), (250, 247, 240))
    draw = ImageDraw.Draw(image)
    for row in range(12):
        y = 80 + row * 52 + (seed * 7) % 20
        draw.line([(60, y), (60 + 200 + (seed * 37 + row * 11) % 300, y + 3)], fill=(50, 45, 40), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def fake_call_openrouter(messages, settings, schema=None, extract_result=None, extra=None):
    """What the model would say, shaped the way call_openrouter shapes it: a split with two
    documents, or a short draft with one page break per scan."""
    images = sum(1 for part in messages[0]["content"] if part.get("type") == "image_url")
    name = (schema or {}).get("json_schema", {}).get("name", "")
    if name == "split":
        parsed = {"starts_new_document": [i in (0, 3) for i in range(images)],
                  "is_duplicate_of_previous_page": [False] * images}
        usage = {"cost": 0.001}
    elif name == "merged_metadata":
        parsed = {"document_type": "Letter", "date": "1901-03-20", "author": "Lovell, Ada Mary",
                  "recipient": "Marsh, Henry", "place": "Albany, NY", "names": [], "subjects": ["Shipping"]}
        usage = {"cost": 0.0005}
    else:
        text = "\n\n".join(f"Page {i + 1} of the drafted letter." + (f"\n\n[page {i + 2}]" if i < images - 1 else "")
                           for i in range(images))
        parsed = {"text": text, "uncertain_words": [], "marginal_notes": [], "places": ["Albany, NY"],
                  "names": ["Carver, Charles"], "challenges": ["typed"], "document_type": "Letter",
                  "date": "1901-03-20", "author": "Lovell, Ada Mary", "recipient": "Marsh, Henry",
                  "place": "Albany, NY", "subjects": ["Shipping"]}
        usage = {"cost": 0.002}
    return extract_result(parsed, usage)


class Checks:
    def __init__(self):
        self.failed = 0

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not condition else ""))
        if not condition:
            self.failed += 1


def serve(port: int, root: Path) -> Settings:
    settings = Settings(db_path=str(root / "smoke.db"), bucket_uri=(root / "bucket").as_uri(),
                        openrouter_api_key="sk-fake")
    openrouter.call_openrouter = fake_call_openrouter
    app = review_server.create_app(settings, str(root / "exports"), str(root / "logs"))
    with db.connect(settings.db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        accounts.create_user(conn, ADMIN[0], ADMIN[1], "Paul", "admin")
        accounts.create_user(conn, REVIEWER[0], REVIEWER[1], "John", "reviewer")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.1)
    return settings


def session(base: str, user: tuple[str, str]) -> requests.Session:
    s = requests.Session()
    s.post(f"{base}/login", data={"username": user[0], "password": user[1]}, allow_redirects=False)
    return s


def wait_for_export(admin: requests.Session, base: str, folder_id: str) -> dict:
    started = admin.post(f"{base}/api/folder-exports/new/{folder_id}")
    if started.status_code != 202:
        return {"state": f"refused {started.status_code}: {started.text[:120]}"}
    state = {}
    for _ in range(100):
        state = admin.get(f"{base}/api/folder-exports/jobs/{started.json()['job_id']}").json()
        if state.get("state") not in ("queued", "running"):
            break
        time.sleep(0.1)
    return state


def journey(base: str, checks: "Checks", root: Path, label: str = "",
            batch_id: str | None = None, db_name: str = "smoke.db") -> dict:
    """Upload (or take an already-ingested batch), split, confirm, draft, review, send back,
    resubmit, accept and export one folder, as admin and reviewer. Returns the ids it made."""
    tag = f"{label} " if label else ""
    admin, reviewer = session(base, ADMIN), session(base, REVIEWER)
    if batch_id is None:
        files = [("files", (f"{FOLDER}_Page_{i + 1:02d}.jpg", drawn_page(i), "image/jpeg")) for i in range(6)]
        upload = admin.post(f"{base}/api/new/batches", files=files, data={"folder_display_id": FOLDER})
        checks.ok(f"2 {tag}admin uploads six drawn pages", upload.status_code == 200, upload.text[:200])
        batch_id = upload.json()["batch_id"]
    proposal = admin.post(f"{base}/api/new/batches/{batch_id}/retry-proposal").json()
    folder_id = proposal["folder_id"]
    checks.ok(f"2 {tag}the folder's display id comes back with its internal id",
              proposal.get("folder_display_id") == FOLDER and folder_id and folder_id != FOLDER,
              json.dumps({k: proposal.get(k) for k in ("folder_id", "folder_display_id")}))
    starts = [p["proposed_new_document"] for p in proposal["pages"]]
    checks.ok(f"2 {tag}the fake backend proposes two documents", starts == [True, False, False, True, False, False], str(starts))
    groups = [{"display_id": f"{FOLDER}AAA", "sequence_indices": [0, 1, 2]},
              {"display_id": f"{FOLDER}AAB", "sequence_indices": [3, 4, 5]}]
    confirm = admin.post(f"{base}/api/new/batches/{batch_id}/confirm", json={"groups": groups})
    checks.ok(f"2 {tag}admin confirms the split", confirm.status_code == 200, confirm.text[:200])
    first, second = confirm.json().get("created", ["", ""])

    documents = [d for d in admin.get(f"{base}/api/new/documents").json() if d["folder_id"] == folder_id]
    checks.ok(f"3 {tag}both documents are drafted, shown by display id",
              sorted(d["display_id"] for d in documents) == [f"{FOLDER}AAA", f"{FOLDER}AAB"],
              str([d.get("display_id") for d in documents]))
    detail = admin.get(f"{base}/api/new/documents/{first}").json()
    checks.ok(f"3 {tag}the review payload carries text, metadata, pages and both ids",
              "[page 2]" in detail["transcription"]["text"] and detail["fields"]["author"]
              and len(detail["pages"]) == 3 and detail["display_id"] == f"{FOLDER}AAA"
              and detail["metadata"]["title"].startswith(f"[{FOLDER}AAA]"))
    checks.ok(f"3 {tag}the first page image is served",
              admin.get(f"{base}/api/new/documents/{first}/pages/0/image").status_code == 200)
    keys = _bucket_keys(root, first)
    # Not vacuous: an empty bucket once passed this while the scans sat outside the data folder.
    checks.ok(f"3 {tag}scans are stored under internal ids only",
              bool(keys) and all(f"documents/{folder_id}/{first}/" in key.replace("\\", "/") for key in keys))

    with db.connect(str(root / db_name)) as conn:
        john_id = accounts.get_user_by_name(conn, REVIEWER[0])["id"]
    assign = admin.post(f"{base}/api/team/assign", json={"folder_id": folder_id, "reviewer_id": john_id, "workflow": "new"})
    checks.ok(f"4 {tag}admin assigns the folder to the reviewer", assign.status_code == 200, assign.text[:200])
    mine = reviewer.get(f"{base}/api/new/folders").json()
    checks.ok(f"4 {tag}the reviewer sees the assigned folder by its display id",
              any(f["folder_id"] == folder_id and f["folder_display_id"] == FOLDER for f in mine), str(mine)[:200])
    leaked = {k for f in mine for k in f if k in ("accepted_count", "all_approved", "export", "claim", "cost_estimate", "assigned_by", "reviewers")}
    checks.ok(f"6 {tag}no admin field reaches the reviewer", not leaked, str(leaked))
    payload = {"reviewed_text": "Corrected text.\n\n[page 2]\n\nMore.\n\n[page 3]\n\nEnd.", "review_status": "accepted",
               "title": "", "fields": {}, "editors_notes": ""}
    for doc_id in (first, second):
        saved = reviewer.post(f"{base}/api/new/documents/{doc_id}/review", json=payload)
        checks.ok(f"4 {tag}the reviewer accepts a document", saved.status_code == 200, saved.text[:200])
    comment = reviewer.post(f"{base}/api/documents/{first}/comments?workflow=new", json={"body": "is page two right?"})
    checks.ok(f"4 {tag}the reviewer leaves a comment", comment.status_code in (200, 201), comment.text[:200])
    submitted = reviewer.post(f"{base}/api/my/submit", json={"folder_id": folder_id, "workflow": "new", "note": "done"})
    checks.ok(f"4 {tag}the reviewer submits the folder", submitted.status_code == 200, submitted.text[:200])

    back = admin.post(f"{base}/api/team/review", json={"folder_id": folder_id, "workflow": "new", "status": "returned",
                                                       "note": "look at page two", "rework_documents": [first]})
    checks.ok(f"5 {tag}admin sends the folder back with a reason", back.status_code == 200, back.text[:200])
    flagged = reviewer.get(f"{base}/api/new/documents").json()
    checks.ok(f"5 {tag}the reviewer sees the sent-back mark", any(d["document_id"] == first and d.get("sent_back") for d in flagged))
    fix = reviewer.post(f"{base}/api/new/documents/{first}/review", json={**payload, "reviewed_text": "Fixed.\n\n[page 2]\n\nx\n\n[page 3]\n\ny"})
    checks.ok(f"5 {tag}the reviewer fixes and accepts again", fix.status_code == 200, fix.text[:200])
    resubmit = reviewer.post(f"{base}/api/my/submit", json={"folder_id": folder_id, "workflow": "new", "note": "fixed"})
    checks.ok(f"5 {tag}the reviewer resubmits", resubmit.status_code == 200, resubmit.text[:200])
    for doc_id in (first, second):
        accepted = admin.post(f"{base}/api/new/documents/{doc_id}/review", json={**payload, "reviewed_text": "Admin accepted."})
        checks.ok(f"5 {tag}admin accepts a document", accepted.status_code == 200, accepted.text[:200])
    folders = admin.get(f"{base}/api/new/folders").json()
    ready = next((f for f in folders if f["folder_id"] == folder_id), {})
    checks.ok(f"5 {tag}the folder reads approved and ready", ready.get("all_approved") is True, json.dumps(ready)[:200])
    state = wait_for_export(admin, base, folder_id)
    checks.ok(f"5 {tag}export completes", state.get("state") == "completed", json.dumps(state)[:200])
    csvs = list((root / "exports").glob(f"*/{folder_id}/*.csv"))
    rows = csvs[0].read_text(encoding="utf-8").strip().splitlines() if csvs else []
    checks.ok(f"5 {tag}the CSV has one row per document, named by display id",
              len(rows) == 3 and rows[1].startswith(f'"{FOLDER}AAA"'), str(rows[:2]))
    return {"folder_id": folder_id, "documents": [first, second], "batch_id": batch_id}


def _bucket_keys(root: Path, document_id: str) -> list[str]:
    return [str(p.relative_to(root / "bucket")) for p in (root / "bucket" / "documents").rglob("*")
            if p.is_file() and document_id in p.parts]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-") as tmp:
        serve(args.port, Path(tmp))
        anon = requests.Session()
        checks.ok("1 /health answers", anon.get(f"{base}/health").status_code in (200, 503))
        checks.ok("1 /login is public and the card renders", "Sign in" in anon.get(f"{base}/login").text)
        checks.ok("1 the API refuses an anonymous call", anon.get(f"{base}/api/new/folders").status_code == 401)
        journey(base, checks, Path(tmp))

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
