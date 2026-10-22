"""Publishes site/ (the download page) to GitHub Pages.

The page lives in its own repository, getincipit/getincipit.github.io, which GitHub serves at the
organisation's root address; its main branch holds only site/'s files. Copies the shared drawings in,
builds that branch and force-pushes it. No workflow is involved, so it needs nothing beyond push access.

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

SITE_REPOSITORY = "getincipit/getincipit.github.io"
BRANCH = "main"
PAGE_URL = "https://getincipit.github.io/"


def git(*args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def stamp_assets(page: Path, version: str) -> None:
    """Appends ?v=<commit> to the page's own stylesheet, script, video and poster links: GitHub's cache
    keeps a file for ten minutes, and a new page drawn with an old stylesheet once broke the hero."""
    html = page.read_text(encoding="utf-8")
    for name in ("site.css", "site.js", "hero.webm", "hero.mp4", "screens/hero-poster-dark.webp"):
        html = html.replace(f'"{name}"', f'"{name}?v={version}"')
    page.write_text(html, encoding="utf-8")


def publish() -> str:
    build_site.build()
    with tempfile.TemporaryDirectory(prefix="incipit-pages-") as tmp:
        work = Path(tmp) / "pages"
        shutil.copytree(ROOT / "site", work)
        (work / ".nojekyll").write_text("", encoding="utf-8")
        stamp_assets(work / "index.html", git("rev-parse", "--short", "HEAD", cwd=ROOT))
        git("init", "-q", "-b", BRANCH, cwd=work)
        git("add", "-A", cwd=work)
        git("-c", "user.name=incipit", "-c", "user.email=incipit@users.noreply.github.com", "commit", "-q", "-m",
            f"Publish the download page ({git('rev-parse', '--short', 'HEAD', cwd=ROOT)})", cwd=work)
        git("push", "-q", "--force", f"https://github.com/{SITE_REPOSITORY}.git", f"{BRANCH}:{BRANCH}", cwd=work)
    return PAGE_URL


if __name__ == "__main__":
    print(f"published {publish()}")
