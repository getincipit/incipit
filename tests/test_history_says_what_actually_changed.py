"""An edit's history entry describes the edit, not a rewrite of the whole field."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "workspace.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def diff(field, previous, current):
    """revisionDiff out of workspace.js, on one recorded revision."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blocks = []
    for name in ("revisionDiff", "revisionValue", "isListField", "revisionList", "listDiff", "truncate",
                 "words", "wordDiff"):
        start = JS.index(f"  function {name}(")
        blocks.append(JS[start:JS.index("\n  }", start) + 4])
    revision = {"field": field, "previous": previous, "current": current, "has_current": True}
    harness = (
        "const esc = (s) => String(s == null ? '' : s);"
        + f"const DIFF_CONTEXT_WORDS = {_context_words()};"
        + "".join(blocks)
        + f"console.log(revisionDiff({json.dumps(revision)}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return " ".join(done.stdout.split())


def _context_words():
    """The constant revisionDiff's helpers close over, read from the source it lives in."""
    line = next(l for l in JS.splitlines() if "DIFF_CONTEXT_WORDS =" in l)
    return int(line.split("=")[1].strip().rstrip(";"))


NOTES = "reviewed_marginal_notes_json"


def test_adding_a_note_is_reported_as_an_addition():
    """The report: adding "[Bottom] 8508" showed the whole field struck through and retyped."""
    html = diff(NOTES, json.dumps(["[Upper left] Queme"]),
                json.dumps(["[Upper left] Queme", "[Bottom] 8508"]))

    assert "added" in html
    assert "<ins>[Bottom] 8508</ins>" in html
    assert "[Upper left] Queme</del>" not in html, "the note that was left alone was struck through"


def test_the_entries_left_alone_are_counted_rather_than_repeated():
    html = diff(NOTES, json.dumps(["one", "two", "three"]),
                json.dumps(["one", "two", "three", "four"]))

    assert "3 other entries unchanged" in html


def test_one_untouched_entry_reads_as_one():
    html = diff(NOTES, json.dumps(["one"]), json.dumps(["one", "two"]))

    assert "1 other entry unchanged" in html


def test_removing_a_note_is_reported_as_a_removal():
    html = diff(NOTES, json.dumps(["[Upper left] Queme", "[Bottom] 8508"]),
                json.dumps(["[Upper left] Queme"]))

    assert "removed" in html and "<del>[Bottom] 8508</del>" in html
    assert "<ins>" not in html, "nothing was added"


def test_a_swap_says_both_halves():
    html = diff(NOTES, json.dumps(["kept", "gone"]), json.dumps(["kept", "new"]))

    assert "<ins>new</ins>" in html and "<del>gone</del>" in html
    assert "1 other entry unchanged" in html


def test_reordering_is_not_a_rewrite():
    html = diff(NOTES, json.dumps(["a", "b"]), json.dumps(["b", "a"]))

    assert "reordered" in html
    assert "<del>" not in html


def test_appending_to_text_shows_only_what_was_appended():
    """The same complaint in the transcription field: a sentence added is not a retyped page."""
    html = diff("reviewed_text", "The first line.", "The first line. And a second.")

    assert "added" in html
    assert "<ins>And a second.</ins>" in html
    assert "The first line" not in html.split("<ins>")[0].replace("rev-diff", "")


def test_an_unrelated_rewrite_still_shows_both_sides():
    """Nothing shared at either end, so there is no smaller true thing to say than both sides."""
    html = diff("reviewed_text", "one thing entirely", "something else altogether")

    assert "<del>one thing entirely</del>" in html and "<ins>something else altogether</ins>" in html


def test_a_rewrite_that_keeps_the_last_word_is_trimmed_to_what_changed():
    """It shares an ending, so the ending is context rather than something that was retyped."""
    html = diff("reviewed_text", "one thing entirely", "something else entirely")

    assert "<del>one thing</del>" in html and "<ins>something else</ins>" in html
    assert "<del>one thing entirely</del>" not in html


def test_a_field_that_never_held_a_list_is_untouched_by_the_list_path():
    """An older row storing plain text under a _json field must not be parsed as one."""
    html = diff(NOTES, "plain text, not json", "plain text, not json and more")

    assert "added" in html


# ---------- a word changed in the middle of a paragraph ----------

LETTER_BEFORE = ("[Letterhead] Mina darling? Why this silence- Are you so under the matter that "
                 "you cannot smile? I hope not. Do let me know where you are stopping and how "
                 "your leg is. It looked so angry that I am afraid you are actually...")
LETTER_AFTER = LETTER_BEFORE.replace("cannot smile?", "cannot write?")


def test_one_word_changed_mid_paragraph_shows_only_that_word():
    """Reported on 1021X018C9: "smile?" was changed to "write?" and the history struck the whole
    paragraph through and retyped it, so nothing on screen said what the edit was."""
    html = diff("reviewed_text", LETTER_BEFORE, LETTER_AFTER)

    assert "<del>smile?</del>" in html
    assert "<ins>write?</ins>" in html


def test_the_rest_of_the_paragraph_is_not_struck_through():
    html = diff("reviewed_text", LETTER_BEFORE, LETTER_AFTER)

    assert "Mina darling" not in html.split("<del>")[0] or "<del>Mina" not in html
    assert "I hope not" not in html[html.index("<del>"):html.index("</del>")]


def test_the_changed_word_is_shown_in_its_own_context():
    """A word on its own says nothing; the reviewer needs to see where in the page it sits."""
    html = diff("reviewed_text", LETTER_BEFORE, LETTER_AFTER)

    assert "cannot" in html
    assert "I hope" in html


def test_a_word_inserted_mid_paragraph_is_not_shown_as_a_deletion():
    html = diff("reviewed_text", "the quick fox jumped over", "the quick brown fox jumped over")

    assert "<ins>brown" in html
    assert "<del></del>" not in html


def test_a_word_deleted_mid_paragraph_is_not_shown_as_an_addition():
    html = diff("reviewed_text", "the quick brown fox jumped", "the quick fox jumped")

    assert "<del>brown" in html
    assert "<ins></ins>" not in html


# ---------- what the entry says, and when it is re-read ----------

APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_a_changed_word_is_not_also_labelled_changed():
    """Asked for: the red and the green already say which side is which, so the word only
    competes with them for the reader's attention."""
    html = diff("reviewed_text", LETTER_BEFORE, LETTER_AFTER)

    assert "changed" not in html.lower()


def test_a_one_sided_edit_still_says_which_side_it_was():
    """Colour alone cannot: one green block could be an addition or a replacement."""
    added = diff("reviewed_text", "The first line.", "The first line. And a second.")
    removed = diff("reviewed_text", "The first line. And a second.", "The first line.")

    assert "added" in added and "removed" in removed


def test_saving_re_reads_the_history_panel():
    """An edit belongs in the list that is describing it, not on the reviewer's next reopen."""
    assert "window.__refreshHistory__" in JS, "workspace.js no longer offers the refresh"
    assert "__refreshHistory__" in APP_JS, "saving no longer asks the history panel to catch up"


def test_the_refresh_only_touches_a_panel_that_is_open():
    """Reloading a closed panel would fetch on every save for a reviewer who never opened it."""
    body = JS[JS.index("function refreshHistory()"):]
    body = body[:body.index("\n  }") + 4]

    assert 'classList.contains("hidden")' in body
