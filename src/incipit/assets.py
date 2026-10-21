"""Locations of non-Python application assets in source and installed layouts."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
INSTALLED_ROOT = Path(sys.prefix) / "share" / "incipit"
# A packaged build (PyInstaller) unpacks its data files beside the program and says where.
FROZEN_ROOT = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None


def asset_directory(relative: str) -> Path:
    """Prefer checkout assets, then a packaged build's, then the wheel's platform data directory."""
    for root in (SOURCE_ROOT, FROZEN_ROOT):
        if root is not None and (root / relative).exists():
            return root / relative
    return INSTALLED_ROOT / relative


APP_STATIC_DIR = asset_directory("app/static")
