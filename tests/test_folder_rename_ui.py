"""The rename editor belongs to one folder, and opening it moves nothing else."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def run(call, state, extra="", functions=("closeFolderRename", "endRenameIfLeftTheFolder")):
    """Runs the functions out of app.js against a made-up state and stub elements."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = []
    for name in functions:
        start = source.index(f"  function {name}(")
        blocks.append(source[start:source.index("\n  }", start) + 4])
    harness = (
        "const classes = { edit: new Set(), button: new Set() };"
        "const fakeClassList = (set) => ({"
        "  add: (c) => set.add(c), remove: (c) => set.delete(c),"
        "  contains: (c) => set.has(c),"
        "  toggle: (c, on) => (on ? set.add(c) : set.delete(c)) });"
        "const folderRenameEdit = { classList: fakeClassList(classes.edit) };"
        "const folderRenameButton = { classList: fakeClassList(classes.button) };"
        "let barHidden = null;"
        "const renderResplitBar = () => { barHidden = !state.resplitMode; };"
        f"const state = {json.dumps(state)};"
        + "".join(blocks) + extra
        + f"console.log(JSON.stringify({call}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


OPEN_IN_E2002 = {"renamingFolder": True, "renameFolder": "E2002", "activeFolder": "E2002"}


# ---------- leaving the folder closes it ----------


def test_going_back_to_the_folder_grid_closes_the_editor():
    state = {**OPEN_IN_E2002, "activeFolder": None}

    result = run("({open: state.renamingFolder, hidden: folderRenameEdit.classList"
                 ".contains('hidden')})", state, extra="endRenameIfLeftTheFolder();")

    assert result == {"open": False, "hidden": True}


def test_opening_a_different_folder_closes_it_too():
    """It used to arrive at the next folder still holding the previous folder's name."""
    state = {**OPEN_IN_E2002, "activeFolder": "E2080"}

    result = run("state.renamingFolder", state, extra="endRenameIfLeftTheFolder();")

    assert result is False


def test_the_folder_it_belongs_to_is_forgotten_as_well():
    state = {**OPEN_IN_E2002, "activeFolder": "E2080"}

    result = run("state.renameFolder", state, extra="endRenameIfLeftTheFolder();")

    assert result is None


def test_staying_in_the_folder_leaves_the_editor_open():
    result = run("({open: state.renamingFolder, hidden: folderRenameEdit.classList"
                 ".contains('hidden')})", dict(OPEN_IN_E2002),
                 extra="folderRenameEdit.classList.remove('hidden');"
                       "endRenameIfLeftTheFolder();")

    assert result == {"open": True, "hidden": False}


def test_a_closed_editor_is_left_alone():
    state = {"renamingFolder": False, "renameFolder": None, "activeFolder": "E2080"}

    result = run("state.renamingFolder", state, extra="endRenameIfLeftTheFolder();")

    assert result is False


# ---------- and it cannot move the header's buttons ----------


def _header_children():
    """The ids directly inside #folder-header, in order."""

    class Header(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if attributes.get("id") == "folder-header":
                self.depth = 0
                return
            if self.depth is not None:
                if self.depth == 0 and attributes.get("id"):
                    self.ids.append(attributes["id"])
                self.depth += 1

        def handle_endtag(self, tag):
            if self.depth is None:
                return
            if self.depth == 0:
                self.depth = None
            else:
                self.depth -= 1

    parser = Header()
    parser.feed(INDEX)
    return parser.ids


def test_the_editor_is_not_inside_the_header():
    """This is what keeps Fix Split and its neighbours still: the editor is a row of its own below
    the header, so opening it cannot change the header's layout at all."""
    assert "folder-rename-edit" not in _header_children()


def test_the_editor_row_exists_outside_it():
    assert 'id="folder-rename-edit"' in INDEX
    assert "folder-rename-row" in INDEX


def test_the_header_still_holds_the_controls_it_always_did():
    ids = _header_children()

    assert ids == ["folder-back", "folder-header-name", "folder-rename", "folder-resplit",
                   "folder-delete", "folder-export"]


def test_the_headers_buttons_are_not_allowed_to_shrink():
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert ".folder-header > .btn { flex: 0 0 auto; }" in css


# ---------- Fix Split is scoped to its folder in exactly the same way ----------


OPEN_RESPLIT = {"resplitMode": True, "resplitFolder": "E2002", "activeFolder": "E2002",
                "resplitSelection": []}


def resplit(call, state, extra="endResplitIfLeftTheFolder();"):
    return run(call, state, extra=extra, functions=("endResplitIfLeftTheFolder",))


def test_leaving_the_folder_ends_fix_split():
    """Reported: "the banner that pops up presists across the site unless you press cancel"."""
    state = {**OPEN_RESPLIT, "activeFolder": None}

    assert resplit("state.resplitMode", state) is False


def test_opening_another_folder_ends_it_too():
    state = {**OPEN_RESPLIT, "activeFolder": "E2080"}

    assert resplit("state.resplitMode", state) is False


def test_the_banner_is_hidden_on_the_way_out():
    """The folder grid returns before renderResplitBar is reached, which is how the banner came to
    sit over the rest of the site -- so leaving has to hide it itself."""
    state = {**OPEN_RESPLIT, "activeFolder": None}

    assert resplit("barHidden", state) is True


def test_the_picked_documents_are_forgotten():
    state = {**OPEN_RESPLIT, "activeFolder": None, "resplitSelection": ["E2002AAA"]}

    assert resplit("state.resplitSelection.size", state) == 0


def test_staying_in_the_folder_keeps_fix_split_open():
    assert resplit("state.resplitMode", dict(OPEN_RESPLIT)) is True
