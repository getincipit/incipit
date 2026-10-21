"""Publishes a release: builds the program with the release feed's address baked in, compiles the
installer, writes latest.json with the installer's size and SHA-256, and creates the GitHub release
with both as assets. Installed copies read the feed through GitHub's stable "latest release" link,
so each new release becomes the one they are offered.

    .venv/Scripts/python.exe scripts/release.py --notes "What changed"   [--dry-run]

The version is the VERSION file's; bump it (and commit and push) first. Needs Inno Setup and the
GitHub CLI (`gh`), signed in to an account that can publish to the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

REPOSITORY = "getincipit/incipit"


def feed_address(repository: str) -> str:
    """GitHub serves the newest (non-pre-release) release's asset at this one address."""
    return f"https://github.com/{repository}/releases/latest/download/latest.json"


def installer_address(repository: str, version: str) -> str:
    return f"https://github.com/{repository}/releases/download/v{version}/Incipit-{version}-setup.exe"


def feed(version: str, notes: str, installer: Path, repository: str) -> dict:
    """The release feed updates.py reads."""
    data = installer.read_bytes()
    return {"version": version, "date": datetime.now(timezone.utc).date().isoformat(), "notes": notes,
            "downloads": {"windows": {"url": installer_address(repository, version), "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest()}}}


def iscc() -> str:
    for candidate in (shutil.which("ISCC"), Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
                      Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise SystemExit("Inno Setup is not installed: winget install JRSoftware.InnoSetup")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", required=True, help="What changed, as the Updates page will show it")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--dry-run", action="store_true", help="Build and write latest.json, but publish nothing")
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    os.environ["INCIPIT_UPDATE_FEED"] = feed_address(args.repository)
    import build_app
    build_app.build(ROOT / "dist", ROOT / "build" / "icons")
    subprocess.run([iscc(), "/Q", str(ROOT / "installer" / "incipit.iss")], check=True)
    installer = ROOT / "dist" / f"Incipit-{version}-setup.exe"
    latest = ROOT / "dist" / "latest.json"
    latest.write_text(json.dumps(feed(version, args.notes, installer, args.repository), indent=2), encoding="utf-8")
    print(f"built {installer.name} ({installer.stat().st_size:,} bytes) and latest.json for {args.repository}")
    if args.dry_run:
        return 0
    subprocess.run(["gh", "release", "create", f"v{version}", str(installer), str(latest), "--repo", args.repository,
                    "--title", f"Incipit {version}", "--notes", args.notes], check=True)
    print(f"published v{version}; installed copies will be offered it at their next check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
