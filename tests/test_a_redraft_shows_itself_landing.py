"""A redraft replaces fields in place, so the screen has to say that it happened."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import terms_js

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


# ---------- the fields say so ----------


def test_the_redraft_marks_the_fields_it_replaced():
    """Reported: a toast said a redraft had run, and nothing on the page looked any different, so
    the reviewer was left comparing the screen against their memory of the old draft."""
    start = JS.index("  function showRedrafted(")
    block = JS[start:JS.index("\n  }", start)]

    # The transcription and the page fields, which the model reads off the page (ReviewFields.pageBlocks).
    assert 'textEdit.closest(".block")' in block, "the whole field is marked, not just its input"
    assert "ReviewFields.pageBlocks()" in block


def test_the_metadata_a_redraft_keeps_is_not_marked():
    """Title, date, author and the rest survive a redraft untouched, so flashing them would say
    something untrue."""
    start = JS.index("  function showRedrafted(")
    block = JS[start:JS.index("\n  }", start)]

    for kept in ("meta-input-title", "meta-input-date", "meta-input-author"):
        assert kept not in block, kept


def test_the_mark_stays_until_the_reviewer_has_read_it():
    """A mark that fades after a second is one the reviewer can miss by looking away, which is the
    whole thing they were missing before."""
    start = JS.index("  function showRedrafted(")
    block = JS[start:JS.index("\n  function clearRedrafted(", start)]

    assert "setTimeout" not in block
    assert 'addEventListener("input", () =>' in block


def test_editing_one_redrafted_field_keeps_the_other_field_markers():
    """Each chip describes its own field; touching Notes must not unmark Transcription."""
    start = JS.index("  function showRedrafted(")
    block = JS[start:JS.index("\n  function clearRedrafted(", start)]

    assert 'block.classList.remove("is-redrafted")' in block
    assert 'addEventListener("input", clearRedrafted' not in block
    assert 'document.querySelector(".block.is-redrafted")' in block


def test_leaving_the_document_removes_old_redraft_listeners():
    start = JS.index("  function clearRedrafted(")
    block = JS[start:JS.index("\n  }", start)]

    assert "redraftMarkerListeners?.abort()" in block


def test_opening_another_document_drops_the_mark():
    """The fields belong to whichever document is open now, and the last one's redraft has nothing
    to say about them."""
    start = JS.index("  async function openDocument(")

    assert "clearRedrafted()" in JS[start:start + 900]


def test_the_mark_only_runs_after_the_document_has_been_reloaded():
    """Flashing the old values would say the redraft had landed before it had."""
    start = JS.index("  async function redraftThisDocument(")
    block = JS[start:JS.index("\n  }", start)]

    assert block.index("await openDocument(") < block.index("showRedrafted()")


def test_the_stylesheet_still_shows_the_change_without_motion():
    """The animation is the nicety; which fields changed is the information."""
    assert "@keyframes redrafted" in CSS
    assert ".block.is-redrafted h3::after" in CSS and 'content: "New draft"' in CSS
    reduced = CSS[CSS.index("/* The border and the chip are the information"):]
    assert ".block.is-redrafted { animation: none; }" in reduced


def test_the_stamp_is_announced_rather_than_only_drawn():
    assert 'id="redraft-stamp"' in INDEX
    stamp = INDEX[INDEX.index('id="redraft-stamp"'):]
    assert 'aria-live="polite"' in stamp[:200]


# ---------- the reviewer pill ----------


def pill(doc: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blocks = []
    for name in ("reviewerPill", "pillText"):
        start = JS.index(f"  function {name}(")
        blocks.append(JS[start:JS.index("\n  }", start) + 4])
    verdicts = JS[JS.index("  const REVIEWER_VERDICTS = {"):]
    verdicts = verdicts[:verdicts.index("};") + 2]
    harness = (
        terms_js() +
        "const escapeHtml = (s) => String(s == null ? '' : s);"
        + verdicts + "".join(blocks)
        + f"console.log(reviewerPill({json.dumps(doc)}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return " ".join(done.stdout.split())


def test_the_pill_says_the_verdict_and_not_the_name():
    """A folder is assigned to one reviewer at a time, so the name said nothing the folder had not
    already said, and it made every row longer."""
    html = pill({"reviewer_status": "accepted", "reviewer_name": "John"})

    assert ">Accepted</span>" in html
    assert "pill-name" not in html


def test_the_name_is_still_there_for_anyone_who_looks():
    html = pill({"reviewer_status": "accepted", "reviewer_name": "John"})

    assert 'title="John accepted this document"' in html


def test_a_document_no_reviewer_has_ruled_on_still_shows_nothing():
    assert pill({"reviewer_status": "unreviewed", "reviewer_name": ""}) == ""
