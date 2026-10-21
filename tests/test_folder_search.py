"""An editor types a folder title or a person's name and the grid narrows to it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

FOLDER = {"folder_id": "E2003", "folder_name": "E2003_Articles_jpegs",
          "assigned_to_name": "John Halloran", "assigned_by_name": "Paul Israel"}


def matches(folder, term):
    """Runs the page's own matcher, with `state` as the page holds it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blocks = []
    for name in ("folderTitleFor", "folderSearchText", "matchesFolderSearch"):
        start = JS.index(f"  function {name}(")
        blocks.append(JS[start:JS.index("\n  }", start) + 4])
    harness = (f"const state = {{ folderSearch: {json.dumps(term.lower())} }};"
               + "".join(blocks)
               + f"console.log(JSON.stringify(matchesFolderSearch({json.dumps(folder)})));")
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# ---------- what a term is matched against ----------


def test_a_folder_is_found_by_its_title():
    assert matches(FOLDER, "articles") is True


def test_a_folder_is_found_by_its_id():
    assert matches(FOLDER, "e2003") is True


def test_a_folder_is_found_by_the_reviewer_it_is_with():
    assert matches(FOLDER, "halloran") is True


def test_a_folder_is_found_by_the_editor_who_gave_it_out():
    assert matches(FOLDER, "paul") is True


def test_a_folder_that_matches_nothing_is_hidden():
    assert matches(FOLDER, "phonograph") is False


def test_the_search_is_case_insensitive():
    assert matches(FOLDER, "john halloran") is True


def test_an_empty_term_shows_everything():
    assert matches(FOLDER, "") is True


def test_a_folder_with_nobody_on_it_does_not_crash_the_matcher():
    assert matches({"folder_id": "E2080", "folder_name": "E2080_Visitors"}, "visitors") is True


def test_a_folder_held_by_an_admin_is_found_by_their_name():
    held = {**FOLDER, "claim": {"admin_id": 2, "admin_name": "Rachel Weissman"}}

    assert matches(held, "weissman") is True


# ---------- when it fires ----------


def test_it_waits_for_a_pause_rather_than_running_on_each_keystroke():
    assert "const SEARCH_DEBOUNCE_MS = 250;" in JS
    assert "setTimeout(applyFolderSearch, SEARCH_DEBOUNCE_MS)" in JS


def test_the_pending_pass_is_cancelled_by_the_next_keystroke():
    """Without this the debounce is a delay, not a debounce: every character still redraws."""
    start = JS.index('folderSearch?.addEventListener("input"')
    assert "clearTimeout(searchTimer)" in JS[start:start + 220]


def test_one_or_two_characters_are_not_a_term():
    assert "const SEARCH_MIN_CHARS = 3;" in JS


def test_escape_clears_it_without_waiting():
    start = JS.index('folderSearch?.addEventListener("keydown"')
    block = JS[start:start + 300]
    assert "clearTimeout(searchTimer)" in block and "applyFolderSearch()" in block


def test_switching_workflow_clears_the_term():
    """A term typed against New Documents would otherwise empty the Existing grid silently."""
    assert 'state.folderSearch = "";' in JS
    assert "if (folderSearch) folderSearch.value = \"\";" in JS


# ---------- where it lives ----------


def test_the_search_bar_is_in_the_side_panel():
    sidebar = HTML[HTML.index('<nav class="sidebar">'):HTML.index('</nav>')]

    assert 'id="folder-search"' in sidebar


def test_it_sits_above_the_filters_it_narrows():
    assert HTML.index('id="folder-search-wrap"') < HTML.index('id="filter-nav-section"')


def test_a_reviewer_does_not_get_it():
    assert "body.role-reviewer #folder-search-wrap { display: none; }" in CSS


def test_it_is_gone_inside_a_folder_where_there_is_no_folder_list():
    assert 'folderSearchWrap?.classList.toggle("hidden", !onGrid)' in JS


# ---------- and the counts agree with the grid ----------


def test_the_badges_count_within_the_search():
    """Otherwise the sidebar says eleven approved while one is on screen."""
    start = JS.index("function updateFolderCounts(")
    assert "state.folders.filter(matchesFolderSearch)" in JS[start:start + 600]


def test_a_search_that_finds_nothing_says_so():
    assert "No folders match" in JS
