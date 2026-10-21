"""M0 AC-1..AC-4: nothing from the project this code was forked from survives in the tree.

The checks are targeted at proprietary content and removed functionality rather than ordinary words,
so a sentence that happens to say "existing" or "discover" does not fail the build.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).name

# Content that identifies the original project: its name, host, people, places and products.
PROPRIETARY = re.compile(
    r"edison|rutgers|edisondigital|amarel|tailscale|omeka|microfilm|\bgloc\b|\btaep\b|encyclo-technes|"
    r"Menlo Park|Glenmont|Llewellyn|Meadowcroft|Batchelor|Insull|Kennelly|Mambert|Gouraud|Kruesi|"
    r"Mallory|Hutchison|Villard|\bJehl\b|\bUpton\b|Mina Miller|Fred Ott|Ediphone|Diamond Disc|"
    r"Blue Amberol|kinetoscope|pp955|pisrael|scarletmail",
    re.IGNORECASE,
)
# Identifiers of features the fork removed: a reference means a caller was missed.
REMOVED = re.compile(
    r"from \.(notebook|preindexed|authority|discover|advance|migrate_layout|transcribe)\b|"
    r"import (notebook|preindexed|authority|discover|advance|migrate_layout)\b|"
    r"incipit\.(notebook|preindexed|authority|discover|advance|migrate_layout|transcribe)\b|"
    r"/api/(notebooks|preindexed|discover)\b|EDISON_|edison_session|X-Edison|edison-theme|\{edison|"
    r"iiif\.archive\.org|gitlab\.com/edison-papers|login-scene|login-projector|notebooks\.js|"
    r"notebook_editor\.js",
)
BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".db", ".db-wal", ".db-shm", ".bin",
                   ".pdf", ".zip", ".sqlite"}


def tracked_files() -> list[Path]:
    """What git knows about plus what it would add: a fresh fork has not committed yet."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def text_files() -> list[Path]:
    # The M0 spec lists the banned terms by design, so it is the one document allowed to name them.
    return [p for p in tracked_files() if p.suffix.lower() not in BINARY_SUFFIXES
            and p.name != THIS_FILE and "docs/specs/" not in p.as_posix() and p.is_file()]


def test_ac1_no_scan_database_or_binary_is_in_the_repository():
    found = [p.relative_to(ROOT).as_posix() for p in tracked_files()
             if p.suffix.lower() in BINARY_SUFFIXES]
    assert found == [], found


def test_ac1_no_data_record_is_in_the_repository():
    found = [p.relative_to(ROOT).as_posix() for p in tracked_files()
             if "data/" in p.relative_to(ROOT).as_posix() and p.suffix == ".json"]
    assert found == [], found


# Lines that may name the original project on purpose: the installer's publisher, which the user
# set to the organisation publishing this build (2026-10-21).
ALLOWED_LINES = {
    "installer/incipit.iss": ('#define AppPublisher GetEnv("INCIPIT_PUBLISHER") != "" ? GetEnv("INCIPIT_PUBLISHER") : "Thomas Edison Papers"',),
}


@pytest.mark.parametrize("pattern,label", [(PROPRIETARY, "proprietary"), (REMOVED, "removed")])
def test_ac2_ac3_no_tracked_file_carries_the_original_project(pattern, label):
    hits = []
    for path in text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        allowed = ALLOWED_LINES.get(path.relative_to(ROOT).as_posix(), ())
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and line.strip() not in allowed:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:100]}")
    assert hits == [], f"{label} residue:\n" + "\n".join(hits[:40])


def test_ac4_the_original_package_is_not_importable():
    import importlib.util
    assert importlib.util.find_spec("edison_pipeline") is None
