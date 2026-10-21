"""A transcription keeps the document's paragraph structure, on screen and in the export."""

import csv
import io
import json
import pathlib
import shutil
import subprocess

import pytest

from incipit import db, export
from incipit.config import Settings
from conftest import insert_rows

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
STYLE_CSS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"

# A real drafted letter, shaped the way both prompts now ask for: a dateline, a hard-wrapped
# address block, a salutation, prose paragraphs, and a page boundary.
LETTER = (
    "May 10, 1922\n"
    "\n"
    "Mr. Fred. A. Mett, Gen. Mgr.,\n"
    "Powhatan Mining Co.,\n"
    "Woodlawn, Baltimore, Md.\n"
    "\n"
    "Dear Sir:\n"
    "\n"
    "Your letter of May 1st was brought to Mr. Lovell's personal attention. He has no report to "
    "make at this time in regard to Pow-minco Gooch Filter Fibre.\n"
    "\n"
    "[page 2]\n"
    "\n"
    "He wishes me to say [unclear: however?] that we will begin to use a large quantity of thin "
    "asbestos paper about the size of a visiting card next Winter."
)


# ---------- the renderer, executed ----------


def _render(text: str) -> str:
    """Pull renderHighlighted and its helpers out of app.js and run them under node."""
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
    const code = slice("function escapeHtml(str) {", "\\n  }")
      + "\\n" + slice("const PAGE_MARKER =", '}).join("");\\n  }');
    const render = new Function(code + "\\n return renderHighlighted;")();
    process.stdout.write(render(JSON.parse(process.argv[2])));
    """
    # encoding rather than text=True: Windows decodes a child's output as cp1252 by default, and
    # these documents carry the typographic quotes a letterhead motto is set in.
    done = subprocess.run(
        [node, "-e", harness, str(APP_JS), json.dumps(text)],
        capture_output=True, encoding="utf-8", timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_each_paragraph_becomes_its_own_block():
    """One <p> per paragraph, not one blob relying on the container's white-space."""
    html = _render(LETTER)
    assert html.count("<p class=") == 5, html


def test_a_hard_wrapped_address_block_keeps_its_own_line_breaks():
    """The writer's line breaks inside one paragraph are the document's too -- an address written
    over three lines is three lines."""
    html = _render(LETTER)
    assert "Powhatan Mining Co.,\nWoodlawn, Baltimore, Md." in html


def test_prose_paragraphs_are_indented_and_short_lines_are_not():
    """The indent has to distinguish, or it is just a margin."""
    html = _render(LETTER)
    indented = [b for b in html.split("<p class=") if b.startswith('"tp-para tp-indent"')]
    flush = [b for b in html.split("<p class=") if b.startswith('"tp-para"')]
    assert len(indented) == 2, "expected the two body paragraphs to indent"
    assert len(flush) == 3, "expected dateline, address block and salutation to stay flush"
    assert "May 10, 1922" in "".join(flush)
    assert "Dear Sir:" in "".join(flush)


def test_a_page_marker_becomes_a_page_break_not_literal_text():
    """`[page 2]` is a boundary the prompts emit, not content."""
    html = _render(LETTER)
    assert '<div class="tp-page-break"><span>Page 2</span></div>' in html
    assert "[page 2]" not in html


def test_a_page_marker_run_into_a_paragraph_is_still_pulled_out():
    """Models do not reliably put a blank line either side of the marker."""
    html = _render("First page ends here.\n[page 2]\nSecond page starts here.")
    assert '<div class="tp-page-break">' in html
    assert "[page" not in html
    assert html.index("First page") < html.index("tp-page-break") < html.index("Second page")


def test_uncertain_words_are_still_highlighted():
    """The highlight predates this change and is what a reviewer scans for -- restructuring the
    renderer around paragraphs must not cost it."""
    assert "<mark>[unclear: however?]</mark>" in _render(LETTER)


def test_transcribed_text_is_still_escaped():
    """The transcription is model output written into innerHTML."""
    html = _render("<script>alert(1)</script>\n\n<b>second</b>")
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;" in html


@pytest.mark.parametrize("empty", ["", "   ", "\n\n\n"])
def test_a_document_with_no_text_renders_the_empty_marker(empty):
    """Whitespace-only text used to fall through the old `|| "(empty)"` because it was truthy."""
    assert _render(empty) == "<em>(empty)</em>"


def test_the_stylesheet_actually_spaces_and_indents_the_blocks():
    """The renderer only emits class names; without these rules the markup is correct and the
    screen is unchanged, which is the failure mode that would go unnoticed."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".text-preview p.tp-para" in css
    assert ".text-preview p.tp-indent" in css
    assert ".text-preview .tp-page-break" in css


# ---------- the export ----------


def _accepted_new_document(db_path: str, document_id: str, text: str) -> None:
    with db.connect(db_path) as conn:
        insert_rows(conn,
            "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
            "VALUES ('B', 'now', 1, 'E1')"
        )
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status, text) "
            "VALUES (?, 'B', 'drafted', 'accepted', ?)",
            (document_id, text),
        )


def test_the_new_document_export_keeps_paragraph_breaks(tmp_path):
    """csv.QUOTE_ALL keeps a newline inside its quoted field, so the structure survives the file --
    but only as long as nothing upstream flattens the text first."""
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    _accepted_new_document(db_path, "E1AAA", LETTER)

    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    count, csv_bytes, _ = export.export_new_csv(settings)

    assert count == 1
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    transcription = rows[1][rows[0].index("Transcription")]
    assert transcription == LETTER, "the export changed the transcription's shape"
    assert transcription.count("\n\n") == 5
    assert "[page 2]" in transcription, "the page boundary has to reach the deliverable too"


def test_the_export_round_trips_through_a_real_csv_reader(tmp_path):
    """A multi-line field is only correct if a reader puts it back together as one field."""
    db_path = str(tmp_path / "extraction.db")
    db.init_db(db_path)
    _accepted_new_document(db_path, "E1AAA", LETTER)

    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    _, csv_bytes, _ = export.export_new_csv(settings)

    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert len(rows) == 2, "the paragraph breaks were read back as extra rows"


# ---------- which blocks take an indent ----------

REPLY = (
    "February 24, 1920.\n"
    "\n"
    "Mr. R. H. Williams,\n"
    "Publicity Department,\n"
    "Wm.L.Gilbert Clock Co.,\n"
    "Winsted, Connecticut.\n"
    "\n"
    "Dear Sir:-\n"
    "\n"
    "Your letter of February 21 was received and submitted to Mr. Lovell for his personal "
    "consideration.\n"
    "\n"
    "He wishes me to suggest that you increase the area of the rubber pieces and put a stop so "
    "that the twist, say 1/32\", will come against the stop.\n"
    "\n"
    "Yours very truly,\n"
    "\n"
    "Asst. to Mr. Lovell."
)


def _indent_map(text: str) -> list[tuple[bool, str]]:
    """(indented?, first few words) for each rendered paragraph, in order."""
    out = []
    for block in _render(text).split("<p class=")[1:]:
        classes, body = block.split(">", 1)
        out.append(("tp-indent" in classes, body.split("</p>")[0][:34]))
    return out


def test_the_ninety_nine_character_paragraph_indents():
    """The regression this rule was rewritten for."""
    indented = [text for is_indented, text in _indent_map(REPLY) if is_indented]
    assert any(t.startswith("Your letter of February 21") for t in indented), \
        "the first body paragraph is 99 characters and still lost its indent"
    assert len(indented) == 2, "both body paragraphs indent, and only those two"


def test_the_reply_s_labels_all_stay_flush():
    """Everything around the prose: a dateline, a hard-wrapped address, a salutation, a closing and
    a signature."""
    flush = [text for is_indented, text in _indent_map(REPLY) if not is_indented]
    assert len(flush) == 5
    for label in ("February 24, 1920.", "Mr. R. H. Williams,", "Dear Sir:-",
                  "Yours very truly,", "Asst. to Mr. Lovell."):
        assert any(t.startswith(label[:18]) for t in flush), f"{label!r} should sit flush"


@pytest.mark.parametrize("paragraph", [
    "Thanking you for your interest already shown in this lubricating problem and any "
    "further suggestions which you may have, I am",
    "At October 31, 1910, the following journal entry appears in our General Journal, "
    "posted to the proper General Ledger Accounts:-",
    "It is quite possible that the right theory has not been evolved so far -",
])
def test_a_paragraph_that_does_not_end_in_a_full_stop_is_still_prose(paragraph):
    assert _indent_map(paragraph)[0][0], "a body paragraph lost its indent over its punctuation"


@pytest.mark.parametrize("label", [
    "598                Assistant to Mr. Lovell.",          # a file number beside a signature
    "____________________________________________ (L.S.)",  # the rule a signature is written on
    "[Copies to notation] COPIES TO MR. STEPHEN B. MARSHALL (2)",
    "[Marginal note] Explain that I am so overworked that I did not have time to go over it",
    "“Help the Blind to Help Themselves, by Giving Them Work”",
    "Winsted, Connecticut, Thursday, the twenty-fourth of February, 1920",
])
def test_a_label_is_never_indented_however_long_it_runs(label):
    """Word count separates prose from labels, but four shapes reach ten words without being prose:
    a structural tag, a letterhead motto, a long dateline, and a signature line."""
    assert not _indent_map(label)[0][0], f"{label!r} was set as a paragraph"


def test_a_paragraph_the_reviewer_indented_by_hand_is_not_indented_twice():
    """Tab writes four leading spaces (see shiftIndent)."""
    by_hand = "    Will you kindly look into this idea and see what you think of it, and let me know?"
    assert not _indent_map(by_hand)[0][0]


# ---------- a paragraph the model ran on from the line before ----------
# The model is asked to separate paragraphs with a blank line and sometimes uses a single newline.


def test_a_salutation_run_into_the_first_paragraph_is_split_off():
    """E2080BAH."""
    html = _render("Dear Mr. Lovell:\nI am taking an opportunity of sending you a letter by my son, "
                   "Howard M. Edmunds, who is on a visit to the States. I should much like him to "
                   "have the pleasure of meeting you, and presenting this letter personally.")
    blocks = html.split("<p class=")[1:]
    assert len(blocks) == 2
    assert blocks[0].startswith('"tp-para"'), "the salutation should stay flush"
    assert blocks[1].startswith('"tp-para tp-indent"'), "the first paragraph should indent"


def test_many_paragraphs_run_together_are_pulled_apart():
    """E2080BAJ: one block, 34 lines, nothing indented, no spacing anywhere on the page."""
    para = ("It was through an introduction from Mr. Ada Mary Lovell that I first became "
            "acquainted with the gentleman whose work I am about to describe, and the acquaintance "
            "ripened into a friendship lasting many years afterwards. ")
    html = _render("\n".join(para + str(i) for i in range(5)))
    blocks = html.split("<p class=")[1:]
    assert len(blocks) == 5
    assert all(b.startswith('"tp-para tp-indent"') for b in blocks)


def test_a_hard_wrapped_address_is_still_left_alone():
    """The rule this must not break."""
    html = _render("To\nA. M. Lovell, Esq.\nTroy, N.J.")
    assert html.count("<p class=") == 1
    assert "A. M. Lovell, Esq.\nTroy, N.J." in html


def test_a_letterhead_is_still_left_alone():
    html = _render("[Letterhead] HENRY EDMUNDS.\nTELEGRAMS,\nCYLINDROID,VIC. LONDON.\n"
                   "TELEPHONE\n5266 VICTORIA.")
    assert html.count("<p class=") == 1


def test_a_paragraph_wrapped_over_typewriter_lines_is_not_broken_up():
    """The conservative direction."""
    html = _render("I hope my son may have an opportunity of seeing you.\n"
                   "Any attention you can show him will be much appreciated by,\n"
                   "Yours sincerely,")
    assert html.count("<p class=") == 1


@pytest.mark.parametrize("text,expected", [("", 0), ("   ", 0), ("one short line", 1)])
def test_short_input_is_unaffected(text, expected):
    """It asserted nothing at all before -- calling _render and letting it pass so long as it did
    not raise, which is not what "unaffected" means."""
    html = _render(text)

    assert html.count("<p class=") == expected
    if text.strip():
        assert text in html
