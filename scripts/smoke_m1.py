#!/usr/bin/env python
"""M1 smoke: two tabs each holding a folder "SM01" with the same scan names, and neither touches the other.

Runs the M0 journey (upload, split, draft, review, send back, resubmit, accept, export) in the
first tab over HTTP, creates a second tab in the database, ingests the same filenames into a
folder of the same name there, runs the journey again on it, then renames and deletes documents
in the second tab and checks the first tab's documents, comments, scans and export are unchanged.

    .venv/Scripts/python.exe scripts/smoke_m1.py [--port 8098]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_m0 import (ADMIN, FOLDER, Checks, drawn_page, journey, serve,  # noqa: E402
                      session)

from incipit import db, split  # noqa: E402


def snapshot(base: str, settings, root: Path, folder_id: str, documents: list[str]) -> dict:
    """What a person in the first tab can see of their folder, plus where its files are."""
    admin = session(base, ADMIN)
    listed = sorted((d["document_id"], d["display_id"]) for d in admin.get(f"{base}/api/new/documents").json()
                    if d["folder_id"] == folder_id)
    comments = [len(admin.get(f"{base}/api/documents/{d}/comments").json()["comments"]) for d in documents]
    with db.connect(settings.db_path) as conn:
        keys = sorted(p["bucket_key"] for d in documents for p in db.get_new_document_pages(conn, d))
    exported = sorted(p.read_bytes() for p in (root / "exports").glob(f"*/{folder_id}/*.csv"))
    return {"documents": listed, "comments": comments, "keys": keys, "export": exported}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="incipit-smoke-m1-") as tmp:
        root = Path(tmp)
        settings = serve(args.port, root)
        first = journey(base, checks, root, label="tab 1")
        before = snapshot(base, settings, root, first["folder_id"], first["documents"])

        with db.connect(settings.db_path) as conn:
            conn.execute("INSERT INTO tabs (id, slug, label, created_at) "
                         "VALUES (2, 'second', 'Second', datetime('now'))")
        pages = [(f"{FOLDER}_Page_{i + 1:02d}.jpg", drawn_page(i)) for i in range(6)]
        batch_id = split.run_ingest_batch(pages, settings, folder_display_id=FOLDER, tab_id=2)
        second = journey(base, checks, root, label="tab 2", batch_id=batch_id)

        checks.ok("7 the two tabs hold two folders, both shown as SM01",
                  first["folder_id"] != second["folder_id"])
        with db.connect(settings.db_path) as conn:
            keys = {tab: {p["bucket_key"] for d in run["documents"] for p in db.get_new_document_pages(conn, d)}
                    for tab, run in (("first", first), ("second", second))}
        checks.ok("7 same-named scans in the two tabs are stored apart",
                  keys["first"] and keys["second"] and keys["first"].isdisjoint(keys["second"]))

        admin = session(base, ADMIN)
        renamed_id = second["documents"][1]
        renamed = admin.post(f"{base}/api/new/documents/{renamed_id}/rename", json={"new_display_id": "SM01ZZZ"})
        checks.ok("7 a document in the second tab is renamed", renamed.status_code == 200, renamed.text[:200])
        detail = admin.get(f"{base}/api/new/documents/{renamed_id}").json()
        checks.ok("7 the renamed document keeps its internal id and shows its new name",
                  detail.get("document_id") == renamed_id and detail.get("display_id") == "SM01ZZZ")
        deleted = admin.delete(f"{base}/api/new/documents/{second['documents'][0]}",
                               params={"reason": "smoke test"})
        checks.ok("7 a document in the second tab is deleted", deleted.status_code == 200, deleted.text[:200])

        after = snapshot(base, settings, root, first["folder_id"], first["documents"])
        for part, label in (("documents", "documents"), ("comments", "comments"),
                            ("keys", "scan keys"), ("export", "exported CSVs")):
            checks.ok(f"7 the first tab's {label} are unchanged", after[part] == before[part],
                      f"{before[part]!r:.80} -> {after[part]!r:.80}")

    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
