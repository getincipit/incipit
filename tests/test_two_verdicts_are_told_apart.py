"""A document row carries two verdicts, and they must not read as the same thing twice."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import terms_js

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def pill(doc: dict) -> str:
    """reviewerPill out of app.js, on one queue row's data."""
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


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


ACCEPTED = {"reviewer_status": "accepted", "reviewer_name": "John"}


# ---------- the reported collision ----------


def test_a_reviewer_verdict_does_not_read_as_the_document_s_review_status():
    """Reported: a folder view showed two identical chips side by side, one of them sitting where
    the review status belongs. They are different verdicts -- the reviewer's and the editor's --
    and they were the same word in the same colour. The editor's badge reads "Reviewed" and wears
    its own class, so the two no longer collide."""
    html = pill(ACCEPTED)

    assert "pill-reviewer" in html
    assert "badge" not in html


def test_the_reviewer_s_verdict_is_still_attributable():
    """The pill says only the verdict -- a folder is assigned to one reviewer at a time, so the name
    was telling the reviewer something the folder had already told them -- but it is still there
    for anyone who looks."""
    assert 'title="John accepted this document"' in pill(ACCEPTED)


def test_an_unnamed_reviewer_is_still_identified_as_one():
    """Older rows carry a verdict with no name attached."""
    assert "The reviewer accepted this document" in pill(
        {"reviewer_status": "accepted", "reviewer_name": ""})


def test_a_verdict_the_reviewer_never_gave_is_not_invented():
    """`flagged` is no longer a verdict anyone can give, so a row carrying one draws nothing."""
    assert pill({"reviewer_status": "flagged", "reviewer_name": "John"}) == ""


def test_a_document_no_reviewer_has_touched_shows_no_pill_at_all():
    assert pill({"reviewer_status": "unreviewed", "reviewer_name": ""}) == ""


def test_the_two_chips_are_still_different_shapes_in_the_stylesheet():
    """Colour alone was never the distinction, but the classes must stay apart so they can be."""
    assert ".pill-reviewer" in CSS and ".badge-reviewed" in CSS
