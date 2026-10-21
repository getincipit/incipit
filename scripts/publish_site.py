"""Publishes site/ (the download page) to GitHub Pages, which serves the gh-pages branch.

Copies the shared drawings in, builds a branch holding only site/'s files at its root, and pushes it.
No workflow is involved, so it needs nothing beyond push access to the repository.

    .venv/Scripts/python.exe scripts/publish_site.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_site  # noqa: E402

BRANCH = "gh-pages"
PAGE_URL = "https://parthaped.github.io/incipit/"


def git(*args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish() -> str:
    build_site.build()
    remote = git("remote", "get-url", "origin", cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="incipit-pages-") as tmp:
        work = Path(tmp) / "pages"
        shutil.copytree(ROOT / "site", work)
        (work / ".nojekyll").write_text("", encoding="utf-8")
        git("init", "-q", "-b", BRANCH, cwd=work)
        git("add", "-A", cwd=work)
        git("-c", "user.name=incipit", "-c", "user.email=incipit@users.noreply.github.com", "commit", "-q", "-m",
            f"Publish the download page ({git('rev-parse', '--short', 'HEAD', cwd=ROOT)})", cwd=work)
        git("push", "-q", "--force", remote, f"{BRANCH}:{BRANCH}", cwd=work)
    return PAGE_URL


if __name__ == "__main__":
    print(f"published {publish()}")
