"""Rewrite every drafted document's JSON record from the database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from incipit import db, new_document  # noqa: E402
from incipit.storage import BucketStorage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/extraction.db")
    parser.add_argument("--bucket-uri", default="file://./data/bucket")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    storage = BucketStorage(args.bucket_uri)
    failed = []
    with db.connect(args.db) as conn:
        document_ids = [row["document_id"] for row in conn.execute(
            "SELECT document_id FROM new_documents WHERE status = 'drafted' ORDER BY document_id")]
        if args.apply:
            for document_id in document_ids:
                if new_document.refresh_output_record(conn, document_id, storage) is None:
                    failed.append(document_id)

    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"records {verb}: {len(document_ids) - len(failed)}")
    for document_id in failed[:20]:
        print(f"  FAILED {document_id}")
    if not args.apply:
        print("\nNothing was written -- re-run with --apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
