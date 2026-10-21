"""Tab indents the paragraph the reviewer is in, instead of leaving the transcription field."""

import json
import pathlib
import shutil
import subprocess

import pytest

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"

INDENT = "    "


def _edit(value: str, start: int, end: int, outdent: bool = False) -> dict:
    """Run shiftIndent against a stand-in textarea, and report what it left behind."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    harness = """
    const fs = require("fs");
    const src = fs.readFileSync(process.argv[1], "utf8").replace(/\\r\\n/g, "\\n");
    function slice(start, end) {
      const a = src.indexOf(start);
      if (a < 0) throw new Error("start marker missing: " + start);
      const b = src.indexOf(end, a);
      if (b < 0) throw new Error("end marker missing: " + end);
      return src.slice(a, b + end.length);
    }
    const code = slice("const INDENT_SPACES =", "\\n    markDirty();\\n  }");
    const input = JSON.parse(process.argv[2]);

    let dirty = false;
    const textEdit = {
      value: input.value,
      selectionStart: input.start,
      selectionEnd: input.end,
      setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
      setRangeText(text, a, b) {
        this.value = this.value.slice(0, a) + text + this.value.slice(b);
        this.setSelectionRange(a + text.length, a + text.length);
      },
    };
    const markDirty = () => { dirty = true; };
    const document = {};   // no execCommand: take the setRangeText path

    new Function("textEdit", "markDirty", "document", code + "\\n shiftIndent(" +
                 JSON.stringify(input.outdent) + ");")(textEdit, markDirty, document);

    process.stdout.write(JSON.stringify({
      value: textEdit.value,
      selectionStart: textEdit.selectionStart,
      selectionEnd: textEdit.selectionEnd,
      dirty,
    }));
    """
    payload = json.dumps({"value": value, "start": start, "end": end, "outdent": outdent})
    done = subprocess.run(
        [node, "-e", harness, str(APP_JS), payload],
        # Several UI tests launch Node concurrently under xdist on Windows. Process startup can
        # consume most of a minute on a loaded host even though this script itself is instant.
        capture_output=True, encoding="utf-8", timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


LETTER = "Dear Sir:-\n\nYour letter of February 21 was received.\n\nYours very truly,"
PARA_AT = LETTER.index("Your letter")


# ---------- indenting ----------


def test_tab_indents_the_line_the_cursor_is_in():
    out = _edit(LETTER, PARA_AT + 5, PARA_AT + 5)
    assert out["value"] == LETTER[:PARA_AT] + INDENT + LETTER[PARA_AT:]


def test_the_other_paragraphs_are_left_alone():
    """The cursor is in one paragraph."""
    out = _edit(LETTER, PARA_AT + 5, PARA_AT + 5)
    assert out["value"].count(INDENT) == 1
    assert out["value"].startswith("Dear Sir:-") and out["value"].endswith("Yours very truly,")


def test_four_spaces_because_that_is_what_the_model_writes():
    """A paragraph indented by hand has to be the same text as one the model indented -- in the
    preview, in the database and in the export."""
    out = _edit("Your letter of February 21 was received.", 4, 4)
    assert out["value"].startswith("    Your letter")
    assert not out["value"].startswith("\t")


def test_the_caret_keeps_its_place_in_the_words():
    """It sits four characters further along, because four characters went in ahead of it -- not
    back at the start of the line, and not at the end of it."""
    out = _edit(LETTER, PARA_AT + 5, PARA_AT + 5)
    assert out["selectionStart"] == out["selectionEnd"] == PARA_AT + 5 + len(INDENT)
    assert out["value"][out["selectionStart"]:][:6] == "letter"


def test_indenting_marks_the_document_unsaved():
    """It is an edit to the transcription like any other."""
    assert _edit(LETTER, PARA_AT + 5, PARA_AT + 5)["dirty"] is True


def test_a_cursor_at_the_very_start_of_the_field_still_indents():
    out = _edit("Your letter arrived.", 0, 0)
    assert out["value"] == INDENT + "Your letter arrived."


def test_an_empty_field_is_not_a_crash():
    assert _edit("", 0, 0)["value"] == INDENT


# ---------- a selection spanning several lines ----------


TWO = "First paragraph.\nSecond paragraph.\nThird paragraph."


def test_a_selection_indents_every_line_it_covers():
    out = _edit(TWO, 2, TWO.index("Third") + 3)
    assert out["value"] == f"{INDENT}First paragraph.\n{INDENT}Second paragraph.\n{INDENT}Third paragraph."


def test_a_selection_ending_at_a_line_start_does_not_drag_that_line_in():
    """Selecting a paragraph by dragging past its end lands the cursor on the next line."""
    out = _edit(TWO, 0, TWO.index("Second"))
    assert out["value"] == f"{INDENT}First paragraph.\nSecond paragraph.\nThird paragraph."


def test_the_selection_still_covers_what_it_covered():
    """A reviewer indenting twice presses Tab twice."""
    out = _edit(TWO, 0, TWO.index("Third") + 3)
    assert out["value"][out["selectionStart"]:out["selectionEnd"]].count(INDENT) == 3


# ---------- taking it back out ----------


def test_shift_tab_removes_an_indent_it_put_in():
    indented = INDENT + "Your letter of February 21 was received."
    assert _edit(indented, 8, 8, outdent=True)["value"] == "Your letter of February 21 was received."


def test_shift_tab_removes_a_literal_tab_from_a_paste():
    assert _edit("\tYour letter arrived.", 3, 3, outdent=True)["value"] == "Your letter arrived."


def test_shift_tab_removes_only_one_level():
    out = _edit(INDENT + INDENT + "Your letter arrived.", 9, 9, outdent=True)
    assert out["value"] == INDENT + "Your letter arrived."


def test_shift_tab_on_a_flush_line_changes_nothing():
    """Including the dirty flag: an edit that edits nothing must not put the document into a state
    where leaving the page warns about unsaved work."""
    out = _edit("Your letter arrived.", 4, 4, outdent=True)
    assert out["value"] == "Your letter arrived."
    assert out["dirty"] is False


def test_shift_tab_removes_a_partial_indent_left_by_hand():
    """Someone typed two spaces."""
    assert _edit("  Your letter arrived.", 4, 4, outdent=True)["value"] == "Your letter arrived."


def test_indent_then_outdent_returns_the_original_text():
    once = _edit(LETTER, PARA_AT + 5, PARA_AT + 5)
    back = _edit(once["value"], once["selectionStart"], once["selectionStart"], outdent=True)
    assert back["value"] == LETTER


# ---------- the handler that reaches it ----------


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_tab_is_taken_from_the_browser_rather_than_shadowing_it():
    """Without preventDefault the browser moves focus as well, so the indent goes in and the
    reviewer ends up typing into the marginal notes field."""
    handler = _source().split('textEdit.addEventListener("keydown"')[1].split("});")[0]
    assert "preventDefault" in handler
    assert 'event.key !== "Tab"' in handler


def test_escape_gives_back_the_way_out_of_the_field():
    """Tab is how a keyboard-only reviewer leaves a textarea."""
    handler = _source().split('textEdit.addEventListener("keydown"')[1].split("});")[0]
    assert '"Escape"' in handler and "blur()" in handler


def test_a_browser_shortcut_on_tab_is_left_alone():
    """Ctrl+Tab and its friends belong to the browser -- they switch tabs, and a page that swallows
    them is a page the reviewer has to use the mouse to get out of."""
    handler = _source().split('textEdit.addEventListener("keydown"')[1].split("});")[0]
    for modifier in ("ctrlKey", "altKey", "metaKey"):
        assert modifier in handler

    def test_shift_is_not_in_that_list():
        assert "shiftKey" in handler and "event.shiftKey)" in handler

    test_shift_is_not_in_that_list()


def test_the_edit_goes_through_insert_text_so_undo_still_works():
    """An indent the reviewer cannot Ctrl+Z out of is worse than no shortcut at all: assigning to
    .value clears the browser's undo stack for the whole field."""
    source = _source()
    body = source.split("function shiftIndent(")[1].split("\n  }")[0]
    assert 'execCommand("insertText"' in body
    assert "setRangeText" in body, "and a fallback for where execCommand is gone"


def test_only_the_transcription_gets_this():
    """The other fields are one item per line -- a list of names, a list of places."""
    source = _source()
    for field in ("marginalNotes", "places", "names", "challenges", "reviewNotes"):
        assert f'{field}.addEventListener("keydown"' not in source
