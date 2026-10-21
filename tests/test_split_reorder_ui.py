"""The split proposal sidebar exposes stable drag ordering."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parents[1] / "app" / "static" / "app.js"


def test_drag_order_moves_one_document_without_losing_any():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("  function moveSplitOrder(")
    end = source.index("\n  }", start) + len("\n  }")
    block = source[start:end]
    script = f"{block}\nconsole.log(JSON.stringify(moveSplitOrder([0, 4, 9], 9, 0)));"

    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == [9, 0, 4]


def test_proposal_rows_are_draggable_and_reassign_positional_ids():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'draggable="true"' in source
    assert 'await assignPositionalSplitNames()' in source
    assert 'splitState.groupNames[group.firstSeq]' in source


def test_confirmation_uses_visible_order_and_non_adjacent_merges():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    scan = source[source.index("  function scanSplitGroups("):source.index("  function moveSplitOrder(")]
    ordered = source[source.index("  function splitGroups("):source.index("  // Approval is remembered")]
    compute = source[source.index("  function computeSplitGroups("):source.index("  // What confirm actually sends")]
    state = {
        "pages": [
            {"sequence_index": 0, "startsNew": True},
            {"sequence_index": 1, "startsNew": False},
            {"sequence_index": 2, "startsNew": True},
            {"sequence_index": 3, "startsNew": False},
            {"sequence_index": 4, "startsNew": True},
            {"sequence_index": 5, "startsNew": False},
        ],
        "mergeTargets": {"0": 0, "4": 0},
        "groupOrder": [2, 0],
        "groupNames": {"0": "E2241AAB", "2": "E2241AAA"},
        "dropped": [],
    }
    script = (
        f"let splitState = {json.dumps(state)}; splitState.dropped = new Set();\n"
        f"{scan}\n{ordered}\n{compute}\n"
        "console.log(JSON.stringify(computeSplitGroups()));"
    )

    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == [
        {"display_id": "E2241AAA", "firstSeq": 2, "sequence_indices": [2, 3]},
        {"display_id": "E2241AAB", "firstSeq": 0, "sequence_indices": [0, 1, 4, 5]},
    ]


def test_proposal_ui_explains_arbitrary_selection_merge():
    source = APP_JS.read_text(encoding="utf-8")
    html = (APP_JS.parents[0] / "index.html").read_text(encoding="utf-8")
    assert "mergeSelection" in source
    assert "mergeProposedGroups" in source
    assert "tick any two or more to merge" in html
