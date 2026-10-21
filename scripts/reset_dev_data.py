#!/usr/bin/env python
"""Developer utility: wipes every document -- both workflows -- so the site starts fresh with 0
files."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from incipit import db
from incipit.storage import BucketStorage


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/extraction.db", help="Path to the SQLite extraction table")
    parser.add_argument("--bucket-uri", default="file://./data/bucket", help="Bucket URI to wipe")
    parser.add_argument(
        "--csv", default="data/documents.csv",
        help="Discover/extract work-queue CSV (deleted; recreated fresh on the next Discover+Extract)",
    )
    parser.add_argument(
        "--export-dir", default="data/exports",
        help="Host export deliverables dir -- wiped and recreated empty",
    )
    parser.add_argument(
        "--outputs-dir", default="data/outputs",
        help="Existing-document workflow's per-document raw AI-output JSON dir -- wiped and recreated empty",
    )
    parser.add_argument(
        "--outputs-new-dir", default="data/outputs_new",
        help="New Documents workflow's per-document raw AI-output JSON dir -- wiped and recreated empty",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return parser.parse_args()


def _wipe_dir(path_str: str) -> None:
    path = Path(path_str)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    print(f"  wiped {path}")


def main() -> None:
    args = _parse_args()

    print("This will PERMANENTLY delete:")
    print(f"  - every document/page/transcription row in {args.db}")
    print(f"  - every image under {args.bucket_uri}")
    print(f"  - {args.csv}")
    print(f"  - everything under {args.export_dir}, {args.outputs_dir}, {args.outputs_new_dir}")
    print("Settings-menu pacing/cost knobs are left alone.")
    if not args.yes:
        answer = input("Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing was deleted.")
            sys.exit(1)

    db.init_db(args.db)
    with db.connect(args.db) as conn:
        counts = db.reset_all_documents(conn)
    for table, count in counts.items():
        print(f"  deleted {count} row(s) from {table}")

    BucketStorage(args.bucket_uri).delete_all()
    print(f"  wiped all images under {args.bucket_uri}")

    csv_path = Path(args.csv)
    if csv_path.exists():
        csv_path.unlink()
        print(f"  removed {csv_path}")

    _wipe_dir(args.export_dir)
    _wipe_dir(args.outputs_dir)
    _wipe_dir(args.outputs_new_dir)

    print("Done -- the site now starts fresh with 0 files.")


if __name__ == "__main__":
    main()
