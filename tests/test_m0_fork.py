"""Covers M0 AC-4, AC-5 and AC-9 (docs/specs/M0-fork-and-scrub.md): the fork's surface and its tests."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ac4_the_command_line_offers_no_command_of_a_removed_feature():
    shown = subprocess.run([sys.executable, "-m", "incipit.cli", "--help"], capture_output=True,
                           text=True, timeout=60, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    commands = set(re.search(r"\{([\w,-]+)\}", shown.stdout).group(1).split(","))
    assert commands & {"discover", "extract", "transcribe", "advance", "migrate-layout"} == set()


def test_ac5_the_shell_has_one_workflow_tab_called_documents():
    """Since M4 the strip is drawn from /api/tabs; the one seeded tab is still Documents."""
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    schema = (ROOT / "src" / "incipit" / "db.py").read_text(encoding="utf-8")
    assert re.findall(r'class="workflow-tab[^"]*" data-workflow=', html) == []
    assert "'documents', " in schema and "'Documents'" in schema


def test_ac5_the_history_routes_only_the_workflow_and_the_team_tab():
    """Since M4: only the tabs the reader was given (window.__tabs__) and the Team tab are routes."""
    history = (ROOT / "app" / "static" / "history.js").read_text(encoding="utf-8")
    assert "(window.__tabs__ || []).includes(bits[0])" in history and 'bits[0] === "team"' in history


def test_ac9_no_test_file_defines_its_own_signed_in_helper():
    own = [p.name for p in (ROOT / "tests").glob("test_*.py")
           if re.search(r"^def signed_in\(", p.read_text(encoding="utf-8"), re.M)]
    assert own == []
