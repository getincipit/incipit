"""Copies the drawings the landing page shares with the program (the pen and the favicon) into site/,
so there is one source for each; run after changing app/static/logo.svg or favicon.svg.

    .venv/Scripts/python.exe scripts/build_site.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ("logo.svg", "favicon.svg")


def build() -> list[Path]:
    copied = []
    for name in SHARED:
        target = ROOT / "site" / name
        shutil.copyfile(ROOT / "app" / "static" / name, target)
        copied.append(target)
    return copied


if __name__ == "__main__":
    for path in build():
        print(f"copied {path.name}")
