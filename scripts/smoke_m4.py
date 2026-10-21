#!/usr/bin/env python
"""M4 smoke: tabs made, renamed, reordered, paused and deleted by an admin, and every listing kept apart by tab.

Serves a real app on a throwaway database with a fake model and walks the M4 checklist over HTTP:
the one seeded tab; Ledgers (numbered documents) and Photographs added; Documents renamed with its
slug kept; a reorder; a folder "E2002" uploaded into two tabs and each tab's lists holding only its
own; the ledger's documents numbered; Photographs paused, hidden from a reviewer and refusing a save;
its deletion refused until it is emptied; exports of both E2002s under their tabs' slugs; and a
reviewer refused every write.

    .venv/Scripts/python.exe scripts/smoke_m4.py [--port 8104]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_m0 import ADMIN, REVIEWER, Checks, drawn_page, serve, session, wait_for_export  # noqa: E402

from incipit import accounts, db  # noqa: E402

REVIEW = {"reviewed_text": "Reviewed.", "title": "", "fields": {}, "review_status": "accepted", "editors_notes": ""}


def upload(client, base, tab_id, display_id, display_ids):
    """One page into a tab, split-confirmed into the named documents; returns (folder_id, document_ids)."""
    sent = client.post(f"{base}/api/new/batches", data={"folder_display_id": display_id, "tab_id": str(tab_id)},
                       files=[("files", (f"{display_id}_01.jpg", drawn_page(0), "image/jpeg"))])
    if sent.status_code != 200:
        return None, sent
    batch_id = sent.json()["batch_id"]
    proposal = client.post(f"{base}/api/new/batches/{batch_id}/retry-proposal").json()
    groups = [{"display_id": d, "sequence_indices": [0]} for d in display_ids]
    confirmed = client.post(f"{base}/api/new/batches/{batch_id}/confirm", json={"groups": groups})
    return proposal["folder_id"], confirmed.json().get("created", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8104)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m4-") as tmp:
        root = Path(tmp)
        settings = serve(args.port, root)
        admin, reviewer = session(base, ADMIN), session(base, REVIEWER)

        # ---------- 1 the tabs themselves ----------
        tabs = admin.get(f"{base}/api/tabs").json()["tabs"]
        checks.ok("1 the site starts with its one seeded tab", [t["slug"] for t in tabs] == ["documents"], str(tabs)[:120])
        ledgers = admin.post(f"{base}/api/tabs", json={"label": "Ledgers", "id_scheme": "numbers"}).json()["tab"]
        photos = admin.post(f"{base}/api/tabs", json={"label": "Photographs"}).json()["tab"]
        checks.ok("1 Ledgers and Photographs are made with slugs from their names",
                  (ledgers["slug"], photos["slug"], ledgers["id_scheme"]) == ("ledgers", "photographs", "numbers"))
        renamed = admin.put(f"{base}/api/tabs/1", json={"label": "Correspondence"}).json()["tab"]
        checks.ok("1 renaming Documents keeps its slug", (renamed["label"], renamed["slug"]) == ("Correspondence", "documents"))
        order = admin.post(f"{base}/api/tabs/reorder", json={"order": [ledgers["id"], 1, photos["id"]]}).json()["tabs"]
        checks.ok("1 the tabs take the order given", [t["slug"] for t in order] == ["ledgers", "documents", "photographs"])

        # ---------- 2 the same folder name in two tabs ----------
        folder_1, docs_1 = upload(admin, base, 1, "E2002", ["E2002AAA"])
        folder_2, docs_2 = upload(admin, base, ledgers["id"], "E2002", ["E2002-001"])
        checks.ok("2 E2002 is uploaded into both Correspondence and Ledgers", bool(folder_1 and folder_2 and docs_1 and docs_2),
                  f"{folder_1} {docs_1} {folder_2} {docs_2}")
        mine = [[f["folder_id"] for f in admin.get(f"{base}/api/new/folders?tab_id={t}").json()] for t in (1, ledgers["id"])]
        checks.ok("2 each tab's folder list holds only its own E2002", mine == [[folder_1], [folder_2]], str(mine))
        numbered = [d["display_id"] for d in admin.get(f"{base}/api/new/documents?tab_id={ledgers['id']}").json()]
        checks.ok("2 the ledger's documents are numbered", numbered == ["E2002-001"], str(numbered))
        team = admin.get(f"{base}/api/team/folders").json()["folders"]
        checks.ok("2 the Team list names each folder's tab",
                  sorted(f["tab_slug"] for f in team if f["folder_id"] in (folder_1, folder_2)) == ["documents", "ledgers"])

        # ---------- 3 a paused tab ----------
        folder_3, docs_3 = upload(admin, base, photos["id"], "P001", ["P001AAA"])
        with db.connect(settings.db_path) as conn:
            john = accounts.get_user_by_name(conn, REVIEWER[0])["id"]
        admin.post(f"{base}/api/team/assign", json={"folder_id": folder_3, "reviewer_id": john, "workflow": "new"})
        paused = admin.put(f"{base}/api/tabs/{photos['id']}", json={"enabled": False}).json()["tab"]
        checks.ok("3 Photographs is paused", paused["enabled"] is False)
        seen = [t["slug"] for t in reviewer.get(f"{base}/api/tabs").json()["tabs"]]
        checks.ok("3 the reviewer sees the two open tabs only", seen == ["ledgers", "documents"], str(seen))
        opened = reviewer.get(f"{base}/api/new/documents/{docs_3[0]}")
        saved = reviewer.post(f"{base}/api/new/documents/{docs_3[0]}/review", json=REVIEW)
        checks.ok("3 the reviewer's folder in the paused tab still opens but takes no save",
                  opened.status_code == 200 and saved.status_code == 409 and "Photographs" in saved.text, saved.text[:160])
        refused = admin.post(f"{base}/api/new/batches", data={"folder_display_id": "P002", "tab_id": str(photos["id"])},
                             files=[("files", ("p.jpg", drawn_page(1), "image/jpeg"))])
        checks.ok("3 an upload into the paused tab is refused", refused.status_code == 409, refused.text[:120])

        # ---------- 4 deleting a tab ----------
        held = admin.delete(f"{base}/api/tabs/{photos['id']}")
        checks.ok("4 a tab holding a folder cannot be deleted", held.status_code == 409 and "folder" in held.text, held.text[:120])
        admin.put(f"{base}/api/tabs/{photos['id']}", json={"enabled": True})
        emptied = admin.delete(f"{base}/api/new/folders?folder_id={folder_3}&reason=smoke+test+cleanup")
        checks.ok("4 the folder is deleted once the tab is resumed", emptied.status_code == 200, emptied.text[:120])
        gone = admin.delete(f"{base}/api/tabs/{photos['id']}")
        checks.ok("4 the empty tab is deleted", gone.status_code == 200, gone.text[:120])
        checks.ok("4 two tabs remain", [t["slug"] for t in admin.get(f"{base}/api/tabs").json()["tabs"]] == ["ledgers", "documents"])

        # ---------- 5 exports grouped by tab ----------
        for document_id in (*docs_1, *docs_2):
            admin.post(f"{base}/api/new/documents/{document_id}/review", json=REVIEW)
        states = [wait_for_export(admin, base, f).get("state") for f in (folder_1, folder_2)]
        checks.ok("5 both E2002 folders export", states == ["completed", "completed"], str(states))
        checks.ok("5 the exports sit under their tabs' slugs",
                  (root / "exports" / "documents" / folder_1).is_dir() and (root / "exports" / "ledgers" / folder_2).is_dir(),
                  str(sorted(p.name for p in (root / "exports").iterdir())))

        # ---------- 6 the reviewer's boundary ----------
        for method, path in (("POST", "/api/tabs"), ("PUT", "/api/tabs/1"), ("POST", "/api/tabs/reorder"),
                             ("DELETE", f"/api/tabs/{ledgers['id']}"), ("POST", "/api/tabs/1/prompt-profile")):
            got = reviewer.request(method, f"{base}{path}", json={"label": "x", "order": [1]})
            checks.ok(f"6 a reviewer is refused {method} {path}", got.status_code == 403, str(got.status_code))
        listed = reviewer.get(f"{base}/api/tabs").json()["tabs"]
        checks.ok("6 a reviewer's tab list carries no administrative field",
                  all(set(t) <= {"id", "slug", "label", "kind", "id_scheme"} for t in listed))

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
