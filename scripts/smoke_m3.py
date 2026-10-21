#!/usr/bin/env python
"""M3 smoke: a prompt profile built from the questionnaire drives drafting, review and export.

Serves a real app on a throwaway database with a fake model and walks the M3 checklist over HTTP:
the questionnaire, a preview for a civil-rights correspondence collection with its own people,
places, index styles and a custom `reel` field, saving it for the tab, the prompt test, a folder
drafted, reviewed and exported under it, a second version that drops `reel` while the old document
keeps it, and a reviewer refused every profile route.

    .venv/Scripts/python.exe scripts/smoke_m3.py [--port 8103]
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_m0 import ADMIN, REVIEWER, Checks, drawn_page, serve, session, wait_for_export  # noqa: E402

FOLDER = "R014"
ANSWERS = {
    "collection": "the correspondence of a civil rights organisation",
    "material": "letters, telegrams, memoranda and press releases",
    "period_from": 1954, "period_to": 1968, "region": "the American South",
    "people": [{"name": "Okafor, Grace", "note": "office secretary"}, {"name": "Reyes, Daniel"},
               {"name": "Whitfield, Amos", "note": "field organiser"}],
    "organisations": [{"name": "Southern Freedom Council"}, {"name": "Citizens' Bus League"}],
    "places": [{"name": "Montgomery, Alabama"}, {"name": "Selma, Alabama"}, {"name": "Atlanta, Georgia"},
               {"name": "Jackson, Mississippi"}],
    "styles": {"names": "first_last", "us_places": "full_state"},
    "custom_fields": [{"key": "reel", "label": "Reel", "type": "string", "export_column": "Reel",
                       "instruction": "the reel number stamped in the margin, if any"}],
}


def csv_rows(root: Path, folder_id: str) -> list[dict]:
    found = list((root / "exports").glob(f"*/{folder_id}/*.csv"))
    return list(csv.DictReader(io.StringIO(found[0].read_text(encoding="utf-8")))) if found else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8103)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m3-") as tmp:
        root = Path(tmp)
        serve(args.port, root)
        admin, reviewer = session(base, ADMIN), session(base, REVIEWER)

        # ---------- 1 the questionnaire and its preview ----------
        questionnaire = admin.get(f"{base}/api/prompt-profiles/questionnaire").json()
        checks.ok("1 the questionnaire lists its sections and the built-in fields",
                  len(questionnaire.get("sections", [])) == 6 and len(questionnaire.get("builtin_fields", [])) == 10)
        preview = admin.post(f"{base}/api/prompt-profiles/preview", json={"answers": ANSWERS}).json()
        prompt = preview.get("document_prompt", "")
        checks.ok("1 the preview names the collection's people, organisations and places",
                  all(name in prompt for name in ("Okafor, Grace", "Southern Freedom Council", "Selma, Alabama")))
        checks.ok("1 the preview asks for first-last names and full state names",
                  "`First Middle Last`" in prompt and "state written in full" in prompt)
        checks.ok("1 the preview lists the custom reel field in the prompt and the export",
                  "- `reel`:" in prompt and "Reel" in preview.get("export_columns", []))
        refused = admin.post(f"{base}/api/prompt-profiles/preview", json={"answers": {"title_template": "{date}"}})
        checks.ok("1 a template without {display_id} is refused with the reason", refused.status_code == 400
                  and "display_id" in refused.json().get("detail", ""))

        # ---------- 2 saved, given to the tab, and tested ----------
        made = admin.post(f"{base}/api/prompt-profiles", json={"name": "Civil rights correspondence", "answers": ANSWERS})
        checks.ok("2 the profile is saved as version 1", made.status_code == 200, made.text[:200])
        profile_id = made.json().get("id")
        pointed = admin.post(f"{base}/api/tabs/1/prompt-profile", json={"profile_id": profile_id})
        checks.ok("2 tab 1 is pointed at it", pointed.status_code == 200, pointed.text[:200])
        listed = admin.get(f"{base}/api/prompt-profiles").json()
        checks.ok("2 the listing says which tab uses it",
                  any(p["id"] == profile_id and p.get("used_by") for p in listed["profiles"]))
        test = admin.post(f"{base}/api/prompt-profiles/{profile_id}/test",
                          files=[("files", (f"p{i}.jpg", drawn_page(i), "image/jpeg")) for i in range(2)])
        checks.ok("2 the prompt test drafts two drawn pages and returns the fields",
                  test.status_code == 200 and test.json()["fields"].get("author") and "reel" in test.json()["fields"],
                  test.text[:200])
        version_1 = admin.get(f"{base}/api/prompt-profiles/{profile_id}").json()["current_version_id"]

        # ---------- 3 a folder drafted, reviewed and exported under it ----------
        files = [("files", (f"{FOLDER}_{i + 1:02d}.jpg", drawn_page(i), "image/jpeg")) for i in range(6)]
        batch_id = admin.post(f"{base}/api/new/batches", files=files, data={"folder_display_id": FOLDER}).json()["batch_id"]
        folder_id = admin.post(f"{base}/api/new/batches/{batch_id}/retry-proposal").json()["folder_id"]
        groups = [{"display_id": f"{FOLDER}AAA", "sequence_indices": [0, 1, 2]},
                  {"display_id": f"{FOLDER}AAB", "sequence_indices": [3, 4, 5]}]
        created = admin.post(f"{base}/api/new/batches/{batch_id}/confirm", json={"groups": groups}).json().get("created", [])
        detail = admin.get(f"{base}/api/new/documents/{created[0]}").json()
        checks.ok("3 the draft carries the new version and a reel field",
                  detail["pipeline_info"].get("prompt_version_id") == version_1
                  and "reel" in {f["key"] for f in detail["field_registry"]}, str(detail.get("pipeline_info")))
        checks.ok("3 the form's labels come from the profile",
                  {f["key"]: f["label"] for f in detail["field_registry"]}.get("reel") == "Reel")
        for document_id in created:
            saved = admin.post(f"{base}/api/new/documents/{document_id}/review", json={
                "reviewed_text": "Reviewed.", "title": "", "review_status": "accepted", "editors_notes": "",
                "fields": {"reel": "Reel 14"}})
            checks.ok("3 admin reviews a document with reel set", saved.status_code == 200, saved.text[:200])
        state = wait_for_export(admin, base, folder_id)
        checks.ok("3 the folder exports", state.get("state") == "completed", str(state)[:200])
        rows = csv_rows(root, folder_id)
        checks.ok("3 the CSV has a Reel column with the reviewer's value",
                  rows and all(r.get("Reel") == "Reel 14" for r in rows), str(rows[:1])[:200])

        # ---------- 4 a second version drops reel; the old documents keep it ----------
        answers_2 = {**ANSWERS, "custom_fields": []}
        version_2 = admin.post(f"{base}/api/prompt-profiles/{profile_id}/versions", json={"answers": answers_2}).json().get("id")
        checks.ok("4 version 2 is saved and current",
                  admin.get(f"{base}/api/prompt-profiles/{profile_id}").json()["current_version_id"] == version_2)
        old = admin.get(f"{base}/api/new/documents/{created[0]}").json()
        checks.ok("4 the old document still shows reel", old["fields"].get("reel") == "Reel 14"
                  and "reel" in {f["key"] for f in old["field_registry"]})
        changed = admin.put(f"{base}/api/prompt-profiles/versions/{version_1}", json={})
        checks.ok("4 a version cannot be edited", changed.status_code == 405)
        admin.post(f"{base}/api/folder-exports/new/{folder_id}")
        state = wait_for_export(admin, base, folder_id)
        checks.ok("4 the old documents still export reel",
                  all(r.get("Reel") == "Reel 14" for r in csv_rows(root, folder_id)), str(state)[:120])

        # ---------- 5 the reviewer's boundary ----------
        for method, path in (("GET", "/api/prompt-profiles"), ("GET", "/api/prompt-profiles/questionnaire"),
                             ("POST", "/api/prompt-profiles/preview"), ("POST", f"/api/prompt-profiles/{profile_id}/versions"),
                             ("POST", "/api/tabs/1/prompt-profile"), ("POST", f"/api/new/folders/{folder_id}/prompt-version")):
            got = reviewer.request(method, f"{base}{path}", json={})
            checks.ok(f"5 a reviewer is refused {method} {path.split('/api/')[1].split('/')[0]}", got.status_code == 403,
                      str(got.status_code))
        mine = reviewer.get(f"{base}/api/new/documents").json()
        checks.ok("5 the reviewer's listing carries no cost", not any("cost_estimate" in d for d in mine))

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
