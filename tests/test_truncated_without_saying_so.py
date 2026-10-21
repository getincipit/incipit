"""A reply cut off mid-JSON, from a provider that did not admit it."""

from conftest import draft_call
import json

import pytest

from incipit import openrouter
from incipit.config import Settings

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}}


def pages(count: int) -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": "t"}, *([IMAGE] * count)]}]


class Reply:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def cut_off(finish_reason):
    """The reported failure: an object whose first string never closes, after a reply's worth of
    transcription."""
    # Varied text, not a phrase repeated: a repetition loop is a different fault again, and is
    # caught before this one (see test_a_repetition_loop_is_what_bounds_the_cost...).
    body = ('{\n  "text": "Dear Sir, I enclose the report. '
            + " ".join(f"transcribed-word-{n}" for n in range(400)))
    return Reply({"choices": [{"message": {"content": body},
                               "finish_reason": finish_reason}], "usage": {}})


def whole():
    payload = {"text": "Dear Sir, I enclose the report.", "uncertain_words": [],
               "marginal_notes": [], "places": [], "names": [], "challenges": []}
    return Reply({"choices": [{"message": {"content": json.dumps(payload)},
                               "finish_reason": "stop"}], "usage": {}})


def settings(retries=1):
    return Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=retries)


def run(monkeypatch, replies, message_pages=6, retries=1):
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = draft_call(pages(message_pages), settings(retries))
    return result, sent


# ---------- the reported failure ----------


@pytest.mark.parametrize("finish_reason", ["stop", None, "", "end_turn"])
def test_a_body_cut_off_mid_json_is_truncation_whatever_the_provider_calls_it(
        monkeypatch, finish_reason):
    result, sent = run(monkeypatch, [cut_off(finish_reason), whole()])

    assert result["text"] == "Dear Sir, I enclose the report."
    assert sent[1]["max_tokens"] == sent[0]["max_tokens"] * 2, \
        "the retry sent the identical request again, which is the reported bug"


def test_the_error_a_reviewer_sees_is_not_a_json_message(monkeypatch):
    """"Unterminated string starting at line 2 column 3" tells a reviewer nothing they can act on,
    and sends them looking at the model rather than at the length of the document."""
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return cut_off("stop")

    monkeypatch.setattr("requests.Session.post", fake_post)
    with pytest.raises(openrouter.ResponseTruncatedError) as raised:
        draft_call(pages(6), settings())

    assert "too long to transcribe in one reply" in str(raised.value)
    assert "Unterminated string" not in str(raised.value)


def test_it_climbs_before_it_gives_up(monkeypatch):
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return cut_off("stop")

    sent = []
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        (sent.append(dict(json)), cut_off("stop"))[1])
    with pytest.raises(openrouter.ResponseTruncatedError):
        draft_call(pages(12), settings())

    caps = [s["max_tokens"] for s in sent]
    assert caps == [openrouter.MAX_OUTPUT_TOKENS, openrouter.MAX_OUTPUT_TOKENS_CEILING]


# ---------- and a body that was never JSON is still just malformed ----------


def test_prose_is_not_mistaken_for_a_truncated_object(monkeypatch):
    """A refusal, an HTML error page, an empty string: another attempt is the right answer for
    those, and a bigger budget is not."""
    refusal = Reply({"choices": [{"message": {"content": "I cannot read this image."},
                                  "finish_reason": "stop"}], "usage": {}})
    sent = []
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        (sent.append(dict(json)), refusal)[1])

    with pytest.raises(openrouter.TransientModelError):
        draft_call(pages(6), settings(retries=2))

    assert {s["max_tokens"] for s in sent} == {openrouter.MAX_OUTPUT_TOKENS}, \
        "a malformed body was handed a bigger budget"


@pytest.mark.parametrize("content,expected", [
    ('{"text": "half a letter' + " and more of it" * 200, True),
    ('[{"text": "half' + " and more of it" * 200, True),
    ('{"text": "half a letter', False),        # too short to be a reply that ran out of room
    ('{"text": "a whole letter"}', False),
    ('[{"text": "a whole letter"}]', False),
    ("I cannot read this image.", False),
    ("<html><body>502</body></html>", False),
    ("", False),
    ("   ", False),
])
def test_what_counts_as_cut_off_mid_json(content, expected):
    assert openrouter.looks_truncated(content) is expected


# ---------- what a runaway is allowed to cost ----------


def test_a_single_page_may_still_climb(monkeypatch):
    """Found on the real E2003_Articles folder: one document of 23 could not be drafted at all."""
    sent = []
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        (sent.append(dict(json)), cut_off("stop"))[1])

    with pytest.raises(openrouter.ResponseTruncatedError):
        draft_call(pages(1), settings())

    assert [s["max_tokens"] for s in sent] == [openrouter.MAX_OUTPUT_TOKENS,
                                               openrouter.MAX_OUTPUT_TOKENS_CEILING]


def test_a_repetition_loop_is_what_bounds_the_cost_not_the_page_count(monkeypatch):
    """Where the runaway guard actually lives: a degenerate answer is refused a bigger budget, so
    the ceiling never has to be small enough to make one cheap."""
    ditto = Reply({"choices": [{"message": {"content": '{"text": "' + ('" " ' * 4000)},
                                "finish_reason": "length"}], "usage": {}})
    sent = []
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        (sent.append(dict(json)), ditto)[1])

    with pytest.raises(openrouter.DegenerateResponseError):
        draft_call(pages(4), settings())

    assert {s["max_tokens"] for s in sent} == {openrouter.MAX_OUTPUT_TOKENS}


def test_the_worst_a_document_can_waste_is_still_cents():
    """Every attempt that gets truncated is spend with nothing to show for it."""
    wasted, room = 0, openrouter.MAX_OUTPUT_TOKENS
    while room < openrouter.MAX_OUTPUT_TOKENS_CEILING:
        wasted += room
        room *= 2

    assert wasted * 1.28 / 1_000_000 < 0.05


# ---------- a provider that will not give the room asked for ----------


def test_a_refused_budget_is_halved_rather_than_failing_the_document(monkeypatch):
    refusal = Reply({"error": {"message": "max_tokens must be <= 8192"}}, status=400)
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return refusal if len(sent) == 1 else whole()

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = draft_call(pages(6), settings())

    assert result["text"] == "Dear Sir, I enclose the report."
    assert sent[1]["max_tokens"] == openrouter.MAX_OUTPUT_TOKENS // 2


# ---------- a provider that gives up is not a document that is too long ----------


def test_a_provider_that_gives_up_early_is_retried_not_given_more_room(monkeypatch):
    """Reported by the run: "returned 9 characters of unfinished JSON without saying so
    (finish_reason=error); retrying with 32768 output tokens"."""
    aborted = Reply({"choices": [{"message": {"content": '{"text":"'},
                                  "finish_reason": "error"}], "usage": {}})
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return aborted if len(sent) == 1 else whole()

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = draft_call(pages(4), settings(retries=3))

    assert result["text"] == "Dear Sir, I enclose the report."
    assert {s["max_tokens"] for s in sent} == {openrouter.MAX_OUTPUT_TOKENS},         "the budget was raised for a provider that had failed, not for a reply that was too long"


def test_a_provider_that_keeps_failing_is_not_reported_as_a_long_document(monkeypatch):
    aborted = Reply({"choices": [{"message": {"content": '{"text":"'},
                                  "finish_reason": "error"}], "usage": {}})
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None: aborted)

    with pytest.raises(openrouter.TransientModelError):
        draft_call(pages(4), settings(retries=2))


def test_a_short_unfinished_body_is_not_truncation():
    assert openrouter.looks_truncated('{"text":"') is False
    assert openrouter.looks_truncated('{"text": "' + "a" * 60000) is True


def test_an_abort_is_retried_beyond_the_transient_budget(monkeypatch):
    """Reported: "I kept clicking draft all despite it failing and somehow the model progressed"."""
    aborted = Reply({"choices": [{"message": {"content": '{"text":"'},
                                  "finish_reason": "error"}], "usage": {}})
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        # More aborts than the one transient attempt this run is allowed.
        return aborted if len(sent) <= 3 else whole()

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = draft_call(pages(4), settings(retries=1))

    assert result["text"] == "Dear Sir, I enclose the report."
    assert len(sent) == 4


def test_a_model_that_only_ever_aborts_still_gives_up(monkeypatch):
    aborted = Reply({"choices": [{"message": {"content": '{"text":"'},
                                  "finish_reason": "error"}], "usage": {}})
    sent = []
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        (sent.append(1), aborted)[1])

    with pytest.raises(openrouter.TransientModelError):
        draft_call(pages(4), settings(retries=1))

    assert len(sent) == 1 + openrouter.PROVIDER_ABORT_EXTRA_ATTEMPTS
