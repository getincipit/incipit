"""Removing a proposed document on the split-review screen, before anything is confirmed."""

import json
import shutil
import subprocess

import pytest

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def run(call, pages, dropped=(), names=None):
    """Runs computeSplitGroups/keptSplitGroups out of app.js against a made-up splitState."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = []
    for name in ("scanSplitGroups", "splitGroups", "computeSplitGroups", "keptSplitGroups"):
        start = source.index(f"  function {name}(")
        blocks.append(source[start:source.index("\n  }", start) + 4])
    first_seqs = [page["sequence_index"] for page in pages if page["startsNew"]]
    group_names = {str(seq): name for seq, name in zip(first_seqs, names or [])}
    state = {
        "pages": pages,
        "groupNames": group_names,
        "groupOrder": first_seqs,
        "mergeTargets": {},
        "dropped": list(dropped),
    }
    harness = (
        f"const raw = {json.dumps(state)};"
        "const splitState = { pages: raw.pages, groupNames: raw.groupNames,"
        " groupOrder: raw.groupOrder, mergeTargets: raw.mergeTargets,"
        " dropped: new Set(raw.dropped) };"
        + "".join(blocks) + f"console.log(JSON.stringify({call}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def page(index, starts_new=False, duplicate=False):
    return {"sequence_index": index, "startsNew": starts_new, "isDuplicate": duplicate,
            "keepDuplicate": False}


THREE = [page(0, True), page(1), page(2, True), page(3), page(4, True)]
NAMES = ["E2002AAA", "E2002AAB", "E2002AAC"]


# ---------- what confirm sends ----------


def test_nothing_removed_sends_every_document():
    groups = run("keptSplitGroups()", THREE, names=NAMES)

    assert [g["display_id"] for g in groups] == NAMES


def test_a_removed_document_is_not_sent():
    groups = run("keptSplitGroups()", THREE, dropped=[2], names=NAMES)

    assert [g["display_id"] for g in groups] == ["E2002AAA", "E2002AAC"]


def test_a_removed_documents_pages_are_not_sent_either():
    """They stay in the batch; they simply belong to no document."""
    groups = run("keptSplitGroups()", THREE, dropped=[2], names=NAMES)

    sent = [index for g in groups for index in g["sequence_indices"]]
    assert sent == [0, 1, 4]


def test_removing_the_first_document_keeps_the_others_intact():
    groups = run("keptSplitGroups()", THREE, dropped=[0], names=NAMES)

    assert [g["sequence_indices"] for g in groups] == [[2, 3], [4]]


def test_removing_every_document_but_one_still_sends_that_one():
    groups = run("keptSplitGroups()", THREE, dropped=[0, 2], names=NAMES)

    assert len(groups) == 1 and groups[0]["sequence_indices"] == [4]


def test_the_payload_carries_no_bookkeeping_of_its_own():
    """firstSeq is how the screen tracks a group; the server is sent what it has always been."""
    groups = run("keptSplitGroups()", THREE, names=NAMES)

    assert sorted(groups[0]) == ["display_id", "sequence_indices"]


# ---------- and the grouping itself is unchanged ----------


def test_every_group_still_knows_the_page_it_starts_at():
    groups = run("computeSplitGroups()", THREE, names=NAMES)

    assert [g["firstSeq"] for g in groups] == [0, 2, 4]


def test_a_duplicate_scan_is_still_left_out_of_its_document():
    pages = [page(0, True), page(1, duplicate=True), page(2, True)]

    groups = run("keptSplitGroups()", pages, names=["A", "B"])

    assert [g["sequence_indices"] for g in groups] == [[0], [2]]


def test_a_removed_group_that_is_restored_comes_back():
    groups = run("keptSplitGroups()", THREE, dropped=[], names=NAMES)

    assert len(groups) == 3
