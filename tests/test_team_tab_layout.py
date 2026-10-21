"""The Team tab: names without their role, and a sidebar that stays whole however far the page scrolls (15 October)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _workspace_function(name: str, script: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    start = source.index(f"  function {name}(")
    body = source[start:source.index("\n  }\n", start) + 4]
    return subprocess.run([node, "-e", body + script], capture_output=True, text=True)


def test_the_team_tables_name_people_without_their_role():
    """The tables already say who is an editor and who a reviewer, so "Paul (Editor)" read twice."""
    result = _workspace_function("plainName", """
      const assert = require('node:assert/strict');
      assert.equal(plainName('Paul (Editor)'), 'Paul');
      assert.equal(plainName('John (Reviewer)'), 'John');
      assert.equal(plainName('Ada (Lovelace) Byron'), 'Ada (Lovelace) Byron');
      assert.equal(plainName(null), '');""")
    assert result.returncode == 0, result.stderr


def test_the_team_panel_scrolls_inside_the_shell_so_the_sidebar_runs_its_full_height():
    """Reported 15 October: scrolling Team moved the whole page, leaving Collapse and the white bar behind."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.team-panel \{([^}]*)\}", css).group(1)
    assert "overflow-y:auto" in rule.replace(" ", "")


def test_the_team_tab_hides_the_document_queues_filters():
    """Opened from inside a folder, Team kept the queue's All / Unreviewed counts, which count nothing there."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    hidden = re.search(r"const QUEUE_ONLY_SIDEBAR = \[([^\]]*)\]", source).group(1)
    assert {"filter-nav-section", "folder-search-wrap"} <= set(re.findall(r'"([^"]+)"', hidden))


def test_the_avatar_shows_letters_not_the_role_suffixs_bracket():
    """Found in the push review on 15 October: "Paul (Editor)" drew "P(" in the identity bar."""
    result = _workspace_function("avatarInitials", """
      const assert = require('node:assert/strict');
      assert.equal(avatarInitials('Paul (Editor)'), 'P');
      assert.equal(avatarInitials('Mary Emily Miller'), 'ME');
      assert.equal(avatarInitials(''), '?');""")
    assert result.returncode == 0, result.stderr


def test_the_team_column_is_centred_in_a_wide_window():
    """Asked for on 15 October: the cards sat at the left, leaving the right of a wide screen empty."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.team-panel \{([^}]*)\}", css).group(1).replace(" ", "")
    assert "max(40px,calc((100%-1180px)/2))" in rule
