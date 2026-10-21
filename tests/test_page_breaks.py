"""Every scan gets a `[page N]` break, and a draft that comes back without them says so."""

import pathlib
import re

import pytest

from incipit import modeling, openrouter, prompting

DEFAULT = prompting.compile({})


# ---------- what the prompts are told ----------


def test_the_scan_count_reaches_the_new_document_prompt():
    """The seam that matters."""
    messages = openrouter.build_new_document_messages([b"a", b"b", b"c"], DEFAULT)
    text = messages[0]["content"][0]["text"]
    assert "3 scan(s)" in text
    assert "exactly 2 page break(s)" in text


def test_no_placeholder_survives_into_the_prompt():
    """An unfilled {page_count} would reach the model as literal braces and read as noise."""
    text = openrouter.build_new_document_messages([b"a", b"b"], DEFAULT)[0]["content"][0]["text"]
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("count,expected", [(1, 0), (2, 1), (7, 6)])
def test_a_single_scan_is_told_to_emit_no_breaks(count, expected):
    """Most documents are one scan."""
    assert openrouter.page_counts([b"x"] * count) == {
        "page_count": count, "page_break_count": expected}


def test_no_images_does_not_produce_a_negative_count():
    assert openrouter.page_counts([])["page_break_count"] == 0


# ---------- the check that stops it going quiet again ----------


@pytest.mark.parametrize("text,pages,found", [
    ("one page only", 1, 0),
    ("a\n\n[page 2]\n\nb", 2, 1),
    ("a\n\n[page 2]\n\nb\n\n[page 3]\n\nc", 3, 2),
    ("a\n\n[PAGE 2]\n\nb", 2, 1),
    ("a\n\n[page  2]\n\nb", 2, 1),
])
def test_a_correct_draft_counts_its_breaks_and_says_nothing(text, pages, found, caplog):
    assert modeling.warn_on_missing_page_breaks({"text": text}, pages) == found
    assert "page break" not in caplog.text


def test_a_draft_missing_every_break_is_reported(caplog):
    """E2002AAZ: three distinct pages, zero breaks."""
    modeling.warn_on_missing_page_breaks({"text": "no breaks at all"}, 3)
    assert "0 page break(s) for 3 scan(s)" in caplog.text


def test_a_draft_missing_some_breaks_is_reported(caplog):
    """E2229AAC: four scans, one break."""
    modeling.warn_on_missing_page_breaks({"text": "a\n[page 2]\nb"}, 4)
    assert "1 page break(s) for 4 scan(s)" in caplog.text


def test_more_breaks_than_scans_is_not_reported(caplog):
    """Over-emission is a different fault and this is not the place to judge it -- warning here
    would train whoever reads the log to ignore the line that matters."""
    modeling.warn_on_missing_page_breaks({"text": "a\n[page 2]\nb\n[page 3]\nc"}, 2)
    assert caplog.text == ""


def test_a_draft_with_no_text_at_all_does_not_raise():
    """A failed extraction reaches here before the caller's error handling does."""
    assert modeling.warn_on_missing_page_breaks({}, 3) == 0
    assert modeling.warn_on_missing_page_breaks({"text": None}, 3) == 0


def test_a_marker_run_into_a_line_still_counts(caplog):
    """Observed on E2002AAZ re-drafted: "the second set[page 2]slightly raised" -- no newlines, but
    the break is in the right place and app.js draws the rule there."""
    assert modeling.warn_on_missing_page_breaks({"text": "the second set[page 2]slightly raised"}, 2) == 1
    assert "page break" not in caplog.text


def test_the_checker_runs_on_the_real_drafting_path(monkeypatch):
    """Asserted through call_image_model rather than by grepping for the call: a warning
    defined and never wired in is the same silence this was written to end."""
    seen = []
    monkeypatch.setattr(modeling, "warn_on_missing_page_breaks",
                        lambda result, count: seen.append((result.get("text"), count)))
    monkeypatch.setattr(modeling, "page_images", lambda pages, storage: [b"img"] * len(pages))
    monkeypatch.setattr(openrouter, "call_openrouter",
                        lambda *a, **kw: {"text": "drafted"})

    modeling.call_image_model(
        [{"bucket_key": "a"}, {"bucket_key": "b"}, {"bucket_key": "c"}],
        storage=None, settings=None,
        build_messages=lambda images: [], schema={}, extractor=None, merge=None,
    )
    assert seen == [("drafted", 3)]


def test_the_checker_also_runs_when_the_document_had_to_be_chunked(monkeypatch):
    """A 413 splits the document and merges the parts."""
    seen = []
    monkeypatch.setattr(modeling, "warn_on_missing_page_breaks",
                        lambda result, count: seen.append((result.get("text"), count)))
    monkeypatch.setattr(modeling, "page_images", lambda pages, storage: [b"img"] * len(pages))
    monkeypatch.setattr(modeling, "_page_byte_chunks", lambda pages, storage, limit: [pages[:1], pages[1:]])

    calls = []

    def call(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("413 payload too large")
        return {"text": f"chunk{len(calls)}"}

    monkeypatch.setattr(openrouter, "call_openrouter", call)

    modeling.call_image_model(
        [{"bucket_key": "a"}, {"bucket_key": "b"}],
        storage=None, settings=None,
        build_messages=lambda images: [],
        schema={}, extractor=None,
        merge=lambda results: {"text": " ".join(r["text"] for r in results)},
    )
    assert seen and seen[-1][1] == 2, "the merged result was not checked"


def test_the_checker_and_the_renderer_agree_on_the_spelling():
    """app.js parses the same marker to draw the page rules."""
    app_js = (pathlib.Path(__file__).resolve().parents[1]
              / "app" / "static" / "app.js").read_text(encoding="utf-8")
    in_app = re.search(r"const PAGE_MARKER = /(.+?)/i;", app_js)
    assert in_app, "app.js no longer declares PAGE_MARKER the way this test reads it"
    # app.js anchors its own, because it tests a line it has already split out, and it captures the
    # number to render it.
    assert in_app.group(1).strip("^$").replace("(\\d+)", "\\d+") == modeling.PAGE_BREAK.pattern


# ---------- a document too long to come back in one reply ----------


def test_a_truncated_reply_falls_back_to_chunking(monkeypatch):
    """Same remedy as a 413, opposite cause: too much coming back rather than too much going in."""
    monkeypatch.setattr(modeling, "page_images", lambda pages, storage: [b"img"] * len(pages))
    monkeypatch.setattr(modeling, "_page_byte_chunks", lambda pages, storage, limit: [pages[:1], pages[1:]])

    calls = []

    def call(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise openrouter.ResponseTruncatedError("ran out of room at 32768 output tokens")
        return {"text": f"chunk{len(calls)}"}

    monkeypatch.setattr(openrouter, "call_openrouter", call)

    result = modeling.call_image_model(
        [{"bucket_key": "a"}, {"bucket_key": "b"}],
        storage=None, settings=None,
        build_messages=lambda images: [],
        schema={}, extractor=None,
        merge=lambda results: {"text": " ".join(r["text"] for r in results)},
    )

    assert result["text"] == "chunk2 chunk3", "the document was not re-sent in pieces"


def test_a_single_page_that_still_overruns_is_not_chunked(monkeypatch):
    """There is nothing smaller to send, so the failure is the answer rather than a loop."""
    monkeypatch.setattr(modeling, "page_images", lambda pages, storage: [b"img"])
    monkeypatch.setattr(openrouter, "call_openrouter", lambda *a, **kw: (_ for _ in ()).throw(
        openrouter.ResponseTruncatedError("ran out of room")))

    with pytest.raises(openrouter.ResponseTruncatedError):
        modeling.call_image_model(
            [{"bucket_key": "only"}], storage=None, settings=None,
            build_messages=lambda images: [], schema={}, extractor=None, merge=None,
        )


def test_running_out_of_room_is_terminal_for_the_document_retries(monkeypatch):
    """By the time it reaches here the cap has been raised as far as it goes and chunking has been
    tried."""
    calls = []

    def call():
        calls.append(1)
        raise openrouter.ResponseTruncatedError("ran out of room")

    monkeypatch.setattr(modeling.time, "sleep", lambda seconds: None)
    with pytest.raises(openrouter.ResponseTruncatedError):
        modeling.with_model_retries("E2080AAJ", call)

    assert len(calls) == 1, f"the document was retried {len(calls)} times over a fixed limit"
