"""Renumber every folder so its display ids run without gaps (AAA, AAB, ... or -001, -002, ...)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from incipit import db, gaps  # noqa: E402
from incipit.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/extraction.db")
    parser.add_argument("--bucket-uri", default="file://./data/bucket")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plans = gaps.find(args.db)
    if not plans:
        print("every folder already runs without gaps.")
        return 0

    with db.connect(args.db) as conn:
        labels = {folder_id: db.folder_display(conn, folder_id)[0] for folder_id in plans}
    for folder_id, plan in sorted(plans.items(), key=lambda item: labels[item[0]]):
        print(f"{labels[folder_id]}: {len(plan)} document(s) renumbered")
        for old_id, new_id in sorted(plan.items())[:8]:
            print(f"    {old_id} -> {new_id}")
        if len(plan) > 8:
            print(f"    ... and {len(plan) - 8} more")

    if not args.apply:
        print("\nNothing was changed -- re-run with --apply.")
        return 0

    moved = gaps.close(Settings(bucket_uri=args.bucket_uri, db_path=args.db))
    print(f"\n{len(moved)} document(s) renumbered; only their display ids and records changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
