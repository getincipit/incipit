import json

import pytest
import requests

from conftest import draft_call

from incipit import openrouter, prompting
from incipit.config import Settings

DEFAULT = prompting.compile({})
_call = draft_call


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def test_call_openrouter_parses_structured_response(monkeypatch):
    fake_content = json.dumps(
        {
            "text": "Dear Sir, ...",
            "uncertain_words": ["[unclear: word?]"],
            "marginal_notes": ["see attached"],
            "places": ["Newark, N. J.", "Troy, N. Y."],
            "names": ["Howard W. Hayes", "William Edgar Gilmore"],
            "challenges": ["typed", "not_a_real_tag"],
        }
    )
    fake_payload = {
        "choices": [{"message": {"content": fake_content}}],
        "usage": {"cost": 0.0007, "prompt_tokens": 100, "completion_tokens": 50},
    }

    sent = {}

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.update(json)
        return _FakeResponse(fake_payload)

    monkeypatch.setattr("requests.Session.post", fake_post)

    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test")
    result = _call([{"role": "user", "content": []}], settings)

    assert result["text"] == "Dear Sir, ..."
    assert result["fields"]["marginal_notes"] == ["see attached"]
    assert result["fields"]["places"] == ["Newark, N. J.", "Troy, N. Y."]
    assert result["fields"]["names"] == ["Howard W. Hayes", "William Edgar Gilmore"]
    assert result["fields"]["challenges"] == ["typed"]  # invalid tag filtered out
    assert result["cost"] == 0.0007
    assert sent["response_format"] == DEFAULT.document_schema
    assert sent["provider"] == {"require_parameters": True}


def test_call_openrouter_requires_api_key():
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="")
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_call_openrouter_retries_after_a_malformed_json_response(monkeypatch):
    """A 200 response with content that isn't valid JSON (observed in practice as an occasional
    structured-output glitch) should be retried like a transport-level failure, not fail the
    whole call outright."""
    good_content = json.dumps({"starts_new_document": [True, False]})
    responses = [
        _FakeResponse({"choices": [{"message": {"content": "not json {{"}}], "usage": {}}),
        _FakeResponse({"choices": [{"message": {"content": good_content}}], "usage": {"cost": 0.001}}),
    ]
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr("requests.Session.post", fake_post)

    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=3
    )
    result = _call(
        [{"role": "user", "content": []}],
        settings,
        schema=openrouter.SPLIT_JSON_SCHEMA,
        extract_result=openrouter.extract_split_result,
    )

    assert len(calls) == 2
    assert result["starts_new_document"] == [True, False]
    assert result["cost"] == 0.001


def test_call_openrouter_raises_after_exhausting_retries_on_malformed_json(monkeypatch):
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeResponse({"choices": [{"message": {"content": "still not json"}}], "usage": {}})

    monkeypatch.setattr("requests.Session.post", fake_post)

    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=2
    )
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected a transient model error"
    except openrouter.TransientModelError as exc:
        assert isinstance(exc.__cause__, json.JSONDecodeError)
        assert not isinstance(exc, (openrouter.AccountError, openrouter.RequestTooLargeError))


def test_call_openrouter_retries_a_body_level_upstream_error_then_succeeds(monkeypatch):
    """OpenRouter can return HTTP 200 with a body-level error object (observed live: {"error":
    {"message": "exhausted all available targets to no avail", "code": 502}}) when its upstream
    providers are momentarily unavailable -- a transient failure that must be retried, not fail
    the call, the same as a malformed-JSON glitch."""
    good_content = json.dumps({"starts_new_document": [True]})
    responses = [
        _FakeResponse({"error": {"message": "exhausted all available targets to no avail", "code": 502}}),
        _FakeResponse({"choices": [{"message": {"content": good_content}}], "usage": {"cost": 0.001}}),
    ]
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=3
    )
    result = _call(
        [{"role": "user", "content": []}], settings,
        schema=openrouter.SPLIT_JSON_SCHEMA, extract_result=openrouter.extract_split_result,
    )

    assert len(calls) == 2  # one 502-in-body, one success
    assert result["starts_new_document"] == [True]


def test_call_openrouter_routes_a_size_body_error_into_the_413_path(monkeypatch):
    """A size problem sometimes arrives as a 200-with-body-error rather than a clean HTTP 413."""
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _FakeResponse({"error": {"message": "input is too large for the model", "code": 413}})

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=3
    )
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "413" in str(exc)
    assert len(calls) == 1  # not retried -- retrying an oversized request never helps


class _FakeErrorResponse:
    """A response whose raise_for_status() behaves like a real 4xx from `requests` -- status_code
    set, and raises requests.exceptions.HTTPError, unlike _FakeResponse above (which never
    fails)."""

    def __init__(self, status_code, error_body=None):
        self.status_code = status_code
        self._error_body = error_body

    def raise_for_status(self):
        raise requests.exceptions.HTTPError(f"{self.status_code} Client Error", response=self)

    def json(self):
        if self._error_body is None:
            raise ValueError("no body")
        return self._error_body

    @property
    def text(self):
        return json.dumps(self._error_body or {})


def test_call_openrouter_gives_a_clear_message_for_insufficient_credits(monkeypatch):
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(402, {"error": {"message": "This request requires more credits."}})

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test")

    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        message = str(exc)
        assert "402" in message
        assert "insufficient OpenRouter credits" in message
        assert "This request requires more credits." in message


def test_call_openrouter_gives_a_clear_message_for_an_invalid_key(monkeypatch):
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(401)  # no JSON body -- OpenRouter doesn't always send one

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-bad")

    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "401" in str(exc)
        assert "invalid OpenRouter API key" in str(exc)


def test_call_openrouter_gives_a_clear_message_for_a_request_too_large(monkeypatch):
    """413 is OpenRouter's gateway rejecting an oversized request body -- observed live sending a
    multi-image split-proposal window (see split.py's MAX_SPLIT_WINDOW_BYTES)."""

    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(413)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test")

    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "413" in str(exc)
        assert "too large" in str(exc)


def test_call_openrouter_does_not_retry_a_402(monkeypatch):
    calls = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _FakeErrorResponse(402)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=3
    )

    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert len(calls) == 1  # an account-level problem retrying won't fix -- one attempt only


def test_call_openrouter_treats_an_unclassified_status_as_transient(monkeypatch):
    """A 500 that outlived the session adapter's own retries is still a service problem, not a
    verdict on the document -- so it is transient, and the caller keeps trying."""

    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(500)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=1
    )

    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected a transient model error"
    except openrouter.TransientModelError as exc:
        assert "500" in str(exc)
        assert isinstance(exc.__cause__, requests.exceptions.HTTPError)


def test_terminal_errors_are_distinguishable_from_transient_ones():
    """The whole retry policy keys on this: everything is retried unless it is one of these two."""
    assert issubclass(openrouter.AccountError, openrouter.ModelError)
    assert issubclass(openrouter.RequestTooLargeError, openrouter.ModelError)
    assert issubclass(openrouter.TransientModelError, openrouter.ModelError)
    assert not issubclass(openrouter.TransientModelError, openrouter.AccountError)
    assert not issubclass(openrouter.AccountError, openrouter.RequestTooLargeError)
    # Every one stays a RuntimeError, so existing `except RuntimeError` call sites -- including the
    # ones that sniff the message for "413" to drive chunking -- keep working unchanged.
    for cls in (openrouter.ModelError, openrouter.AccountError,
                openrouter.RequestTooLargeError, openrouter.TransientModelError):
        assert issubclass(cls, RuntimeError)


def test_a_missing_api_key_is_an_account_error(monkeypatch):
    """No key means every document in the run fails identically, so the run should stop rather than
    grind through the queue writing the same error onto each one."""
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="")
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected an account error"
    except openrouter.AccountError:
        pass


def test_a_413_is_terminal_for_the_request_but_not_the_account(monkeypatch):
    """An oversized document must not stop the run -- the next document may be an ordinary size."""

    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(413)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=1
    )
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected a size error"
    except openrouter.RequestTooLargeError as exc:
        assert not isinstance(exc, openrouter.AccountError)
        # The chunking call sites still find "413" in the message.
        assert "413" in str(exc)


def test_a_402_stops_the_run(monkeypatch):
    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeErrorResponse(402)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(
        bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test", openrouter_retries=1
    )
    try:
        _call([{"role": "user", "content": []}], settings)
        assert False, "expected an account error"
    except openrouter.AccountError as exc:
        assert "credits" in str(exc)


def _chunk(text, cost, **values):
    return {"text": text, "uncertain_words": values.pop("uncertain_words", []), "fields": values, "cost": cost}


def test_chunks_of_one_document_join_their_text_and_pool_their_page_fields():
    merged = openrouter.document_chunk_merger(DEFAULT.registry)([
        _chunk("page one text", 0.001, uncertain_words=["[a?]"], places=["Newark, N. J."], names=["Lovell"],
               challenges=["typed"]),
        _chunk("page two text", 0.002, uncertain_words=["[b?]"], places=["Newark, N. J.", "Troy, N. Y."],
               names=["Hayes"], challenges=["typed", "faded"]),
    ])
    assert merged["text"] == "page one text\n\npage two text"
    assert merged["fields"]["places"] == ["Newark, N. J.", "Troy, N. Y."]  # unioned, order kept, deduped
    assert merged["fields"]["names"] == ["Lovell", "Hayes"]
    assert merged["fields"]["challenges"] == ["typed", "faded"]
    assert merged["uncertain_words"] == ["[a?]", "[b?]"]
    assert merged["cost"] == 0.003


def test_a_single_chunk_passes_through_the_merge_unchanged():
    merged = openrouter.document_chunk_merger(DEFAULT.registry)([_chunk("t", 0.001, places=["P"])])
    assert (merged["text"], merged["fields"]["places"], merged["cost"]) == ("t", ["P"], 0.001)


def test_a_chunked_documents_text_fields_come_from_the_first_chunk_that_filled_them():
    merged = openrouter.document_chunk_merger(DEFAULT.registry)([
        _chunk("t1", 0.001, document_type="", date="", author="", subjects=["s1"]),
        _chunk("t2", 0.002, document_type="Letter", date="1900-01-01", author="Lovell", subjects=["s2"]),
    ])
    assert (merged["fields"]["document_type"], merged["fields"]["date"], merged["fields"]["author"]) ==         ("Letter", "1900-01-01", "Lovell")
    assert merged["fields"]["subjects"] == ["s1", "s2"]


# ---------- robustness guards found during benchmarking ----------


class _StatusResponse:
    """A response with a real HTTP status, for the paths that key on one."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def _ok(content):
    return _StatusResponse(200, {"choices": [{"message": {"content": content}}], "usage": {}})


def _transcription(text):
    return json.dumps({
        "text": text, "uncertain_words": [], "marginal_notes": [],
        "places": [], "names": [], "challenges": [],
    })


def test_a_long_answer_of_one_repeated_token_is_recognised_as_degenerate():
    """The observed shape: 12,301 characters of ditto marks, unique-word ratio 0.003, completion
    stopped exactly on the cap."""
    assert openrouter.looks_degenerate(",, " * 4000)


def test_an_ordinary_transcription_is_not_degenerate():
    """The guard has to leave real documents alone."""
    prose = ("Your letter of the 6th was brought to Mr. Lovell's personal attention, and he has no "
             "report to make at this time in regard to the filter fibre you enclosed. " * 6)
    assert not openrouter.looks_degenerate(prose)


def test_a_short_repetitive_answer_is_left_alone():
    """A telegram or a file notation is legitimately short and repetitive."""
    assert not openrouter.looks_degenerate("yes yes yes")
    assert not openrouter.looks_degenerate("ditto " * 40)          # under the character floor


def test_a_degenerate_response_is_retried_with_a_frequency_penalty(monkeypatch):
    """The retry is the recovery."""
    sent, responses = [], [_ok(",, " * 4000), _ok(_transcription("Dear Sir, I enclose the report."))]

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return responses[len(sent) - 1]

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = _call(
        [{"role": "user", "content": []}],
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )

    assert result["text"] == "Dear Sir, I enclose the report."
    assert "frequency_penalty" not in sent[0], "the first attempt must not penalise repetition"
    assert sent[1]["frequency_penalty"] == openrouter.DEGENERATE_RETRY_FREQUENCY_PENALTY


def test_a_response_that_stays_degenerate_raises_rather_than_being_stored(monkeypatch):
    """It parses as valid JSON, so nothing else in this module would reject it."""
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None: _ok(",, " * 4000))

    with pytest.raises(openrouter.DegenerateResponseError):
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=2),
        )


def test_an_endpoint_that_requires_reasoning_is_retried_without_the_flag(monkeypatch):
    """Some endpoints reject `reasoning: {"enabled": false}` with a 400."""
    sent = []
    refusal = _StatusResponse(400, {"error": {
        "message": "Reasoning is mandatory for this endpoint and cannot be disabled."}})

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return refusal if len(sent) == 1 else _ok(_transcription("Dear Sir."))

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = _call(
        [{"role": "user", "content": []}],
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )

    assert result["text"] == "Dear Sir."
    assert sent[0]["reasoning"] == {"enabled": False}
    assert "reasoning" not in sent[1], "the retry must not resend the key the endpoint rejected"


def test_an_unrelated_400_is_not_treated_as_a_reasoning_problem(monkeypatch):
    """Detected by the message, not the status: stripping reasoning from every 400 would silently
    change the request shape for faults that have nothing to do with it."""
    monkeypatch.setattr(
        "requests.Session.post",
        lambda self, url, headers=None, json=None, timeout=None: _StatusResponse(
            400, {"error": {"message": "invalid response_format schema"}}),
    )

    with pytest.raises(openrouter.ModelError) as caught:
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=1),
        )
    assert not isinstance(caught.value, openrouter.DegenerateResponseError)


def test_a_result_wrapped_in_a_single_element_array_is_unwrapped(monkeypatch):
    """Observed live from qwen3.7-plus on the Alibaba provider: the complete, correct result inside
    a one-element array."""
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(1)
        return _ok("[" + _transcription("Dear Sir, I enclose the report.") + "]")

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = _call(
        [{"role": "user", "content": []}],
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )
    assert result["text"] == "Dear Sir, I enclose the report."
    assert len(sent) == 1, "the wrapped result was usable; it should not have cost a retry"


@pytest.mark.parametrize("body", [
    '[{"text": "one"}, {"text": "two"}]',   # two candidates, no obvious payload
    '["just a string"]',                    # not a result object at all
    '[]',
    '"a bare string"',
])
def test_any_other_non_object_body_is_raised_as_transient(monkeypatch, body):
    """Only the one evidenced shape is unwrapped."""
    monkeypatch.setattr(
        "requests.Session.post",
        lambda self, url, headers=None, json=None, timeout=None: _ok(body),
    )
    with pytest.raises(openrouter.TransientModelError):
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=2),
        )


def _no_provider(code=502):
    return _StatusResponse(200, {"error": {
        "message": "exhausted all available targets to no avail", "code": code}})


def test_a_momentary_routing_gap_is_retried_not_declared_an_outage(monkeypatch):
    """OpenRouter reports "no targets" for a two-second gap in routing as readily as for a real
    outage."""
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(1)
        return _no_provider() if len(sent) == 1 else _ok(_transcription("Dear Sir."))

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = _call(
        [{"role": "user", "content": []}],
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )
    assert result["text"] == "Dear Sir."


def test_no_provider_across_every_attempt_is_raised_as_unavailable(monkeypatch):
    """Only once it outlasts every attempt is it the model being gone -- the one condition the
    caller answers by changing transcription path rather than by trying again."""
    monkeypatch.setattr(
        "requests.Session.post",
        lambda self, url, headers=None, json=None, timeout=None: _no_provider(),
    )
    with pytest.raises(openrouter.ModelUnavailableError):
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=2),
        )


def test_a_provider_failure_that_is_not_an_outage_stays_transient(monkeypatch):
    """A 502 from one provider is not the same as no provider."""
    monkeypatch.setattr(
        "requests.Session.post",
        lambda self, url, headers=None, json=None, timeout=None: _StatusResponse(
            200, {"error": {"message": "upstream provider returned 502", "code": 502}}),
    )
    with pytest.raises(openrouter.TransientModelError) as caught:
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=2),
        )
    assert not isinstance(caught.value, openrouter.ModelUnavailableError)


def test_a_model_id_no_provider_serves_is_unavailable_immediately(monkeypatch):
    """A 404 is not a routing gap -- no amount of retrying conjures a provider for an id that
    resolves to nothing."""
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(1)
        return _StatusResponse(404, {"error": {"message": "No endpoints found for model"}})

    monkeypatch.setattr("requests.Session.post", fake_post)
    with pytest.raises(openrouter.ModelUnavailableError):
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
        )
    assert len(sent) == 1, "a 404 should not be retried"


# ---------- a reply that ran out of room ----------


IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}}


def pages(count: int) -> list[dict]:
    """A request carrying `count` page images."""
    return [{"role": "user", "content": [{"type": "text", "text": "t"}, *([IMAGE] * count)]}]


def _truncated(text):
    """A reply cut off at the cap: the JSON stops mid-string, exactly as the model left it."""
    whole = _transcription(text)
    return _StatusResponse(
        200,
        {"choices": [{"message": {"content": whole[: len(whole) // 2]},
                      "finish_reason": "length"}], "usage": {}},
    )


def _complete(text):
    return _StatusResponse(
        200,
        {"choices": [{"message": {"content": _transcription(text)}, "finish_reason": "stop"}],
         "usage": {}},
    )


def test_a_reply_cut_off_at_the_cap_is_retried_with_more_room(monkeypatch):
    """The reported failure."""
    sent, responses = [], [_truncated("a very long letter"), _complete("a very long letter")]

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return responses[len(sent) - 1]

    monkeypatch.setattr("requests.Session.post", fake_post)
    # Six pages: the room a request may climb to is derived from the pages it carries.
    result = _call(
        pages(6),
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )

    assert result["text"] == "a very long letter"
    assert sent[0]["max_tokens"] == openrouter.MAX_OUTPUT_TOKENS
    assert sent[1]["max_tokens"] == openrouter.MAX_OUTPUT_TOKENS * 2, \
        "the retry sent the identical request again"


def test_the_cap_keeps_doubling_until_the_ceiling(monkeypatch):
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return _truncated("x")

    monkeypatch.setattr("requests.Session.post", fake_post)
    with pytest.raises(openrouter.ResponseTruncatedError):
        _call(
            pages(12),
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=1),
        )

    caps = [s["max_tokens"] for s in sent]
    assert caps[0] == openrouter.MAX_OUTPUT_TOKENS
    assert caps[-1] == openrouter.MAX_OUTPUT_TOKENS_CEILING
    assert caps == sorted(caps) and len(set(caps)) == len(caps), f"the cap did not climb: {caps}"


def test_running_out_of_room_is_not_reported_as_a_bad_response(monkeypatch):
    """A reviewer reading "did not return a usable response" goes looking at the model."""
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None: _truncated("x"))

    with pytest.raises(openrouter.ResponseTruncatedError) as caught:
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=1),
        )

    message = str(caught.value)
    assert "cut off" in message and "smaller pieces" in message
    assert "did not return a usable response" not in message


def test_a_truncation_retry_does_not_spend_the_transient_attempts(monkeypatch):
    """Those attempts are for a service that misbehaved."""
    sent, responses = [], [_truncated("x"), _complete("the whole letter")]

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return responses[min(len(sent) - 1, len(responses) - 1)]

    monkeypatch.setattr("requests.Session.post", fake_post)
    result = _call(
        pages(6),
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=1),
    )

    assert result["text"] == "the whole letter"
    assert len(sent) == 2, "the truncation retry never happened"


def test_a_repetition_loop_is_not_handed_a_bigger_cap(monkeypatch):
    """A degenerate answer also stops at the cap."""
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return _StatusResponse(200, {"choices": [
            {"message": {"content": ",, " * 4000}, "finish_reason": "length"}], "usage": {}})

    monkeypatch.setattr("requests.Session.post", fake_post)
    with pytest.raises(openrouter.DegenerateResponseError):
        _call(
            [{"role": "user", "content": []}],
            Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=2),
        )

    assert {s["max_tokens"] for s in sent} == {openrouter.MAX_OUTPUT_TOKENS}


def test_an_ordinary_reply_never_pays_for_the_bigger_cap(monkeypatch):
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return _complete("Dear Sir, I enclose the report.")

    monkeypatch.setattr("requests.Session.post", fake_post)
    _call(
        [{"role": "user", "content": []}],
        Settings(db_path=":memory:", openrouter_api_key="k", openrouter_retries=3),
    )

    assert [s["max_tokens"] for s in sent] == [openrouter.MAX_OUTPUT_TOKENS]


def test_the_ceiling_is_a_spending_limit_not_a_model_limit():
    """Qwen3.7-plus permits 131,072 completion tokens, so nothing here is bounded by the model."""
    wasted, room = 0, openrouter.MAX_OUTPUT_TOKENS
    while room < openrouter.MAX_OUTPUT_TOKENS_CEILING:
        wasted += room
        room *= 2

    assert wasted * 1.28 / 1_000_000 < 0.05, (
        f"a single document can now waste ${wasted * 1.28 / 1_000_000:.3f} of output on truncated "
        "attempts before it falls back to chunking"
    )
    assert openrouter.MAX_OUTPUT_TOKENS > 6_100, (
        "the default must still clear the largest real document (E2080AAJ, about 6,000 tokens), "
        "or ordinary documents start paying for the climb"
    )


def test_every_request_may_double_once_however_few_pages_it_carries():
    """Scaling the ceiling by page count starved the chunk recovery: a single dense page got less
    room than the call that had just failed for being too long."""
    assert openrouter.output_ceiling(pages(1)) == openrouter.MAX_OUTPUT_TOKENS_CEILING
    assert openrouter.output_ceiling(pages(12)) == openrouter.MAX_OUTPUT_TOKENS_CEILING
    assert openrouter.MAX_OUTPUT_TOKENS_CEILING == openrouter.MAX_OUTPUT_TOKENS * 2


def test_the_default_room_costs_nothing_when_it_is_not_used():
    """max_tokens is a cap, not a reservation -- which is why the default can be generous."""
    assert openrouter.MAX_OUTPUT_TOKENS <= openrouter.MAX_OUTPUT_TOKENS_CEILING


class _PaddedResponse:
    """A reply held open with keep-alive padding, which only the raw text can be read through."""

    def __init__(self, payload, padding):
        self.text = padding + json.dumps(payload) + padding

    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("Expecting value")


def test_a_reply_wrapped_in_keep_alive_padding_is_still_read(monkeypatch):
    """From the free-tier notebook drafting runs, where a queued model pads while it waits."""
    payload = {"choices": [{"message": {"content": json.dumps({"text": "Dear Sir"})}}],
               "usage": {"cost": 0.0, "prompt_tokens": 10, "completion_tokens": 5}}

    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _PaddedResponse(payload, "\n   \n")

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite",
                        openrouter_api_key="sk-test")
    result = _call([{"role": "user", "content": []}], settings)

    assert result["text"] == "Dear Sir"


def test_a_reply_wrapped_in_a_markdown_code_fence_is_still_read(monkeypatch):
    """From the notebook drafting runs: a call with no schema is answered in a ```json fence, and
    every such opening burned all three attempts on both models before being given up on."""
    fence = "`" * 3
    payload = {"choices": [{"message": {
        "content": fence + '\njson\n{"text": "Dear Sir"}\n'.replace("\njson", "json") + fence}}],
        "usage": {"cost": 0.0, "prompt_tokens": 10, "completion_tokens": 5}}

    def fake_post(self, url, headers=None, json=None, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite",
                        openrouter_api_key="sk-test")
    result = _call([{"role": "user", "content": []}], settings)

    assert result["text"] == "Dear Sir"


def test_a_refused_output_budget_is_never_asked_for_again_after_a_cut_off_reply(monkeypatch):
    """From 6 October: NP017 canvas 21 bounced between 16384 (cut off) and 32768 (refused) over 50 times."""
    sent = []

    def fake_post(self, url, headers=None, json=None, timeout=None):
        sent.append(json["max_tokens"])
        if json["max_tokens"] > openrouter.MAX_OUTPUT_TOKENS:
            return _FakeErrorResponse(400, {"error": {"message": "you requested 32768 output tokens; reduce max_tokens"}})
        return _FakeResponse({"choices": [{"message": {"content": '{"text": "partial'}, "finish_reason": "length"}]})

    monkeypatch.setattr("requests.Session.post", fake_post)
    settings = Settings(bucket_uri="file:///tmp/bucket", db_path="/tmp/db.sqlite", openrouter_api_key="sk-test")
    with pytest.raises(openrouter.ResponseTruncatedError):
        _call([{"role": "user", "content": []}], settings, extra={"reasoning": {"enabled": False}})
    assert len(sent) <= 4


VLLM_REFUSAL = ("This model's maximum context length is 32768 tokens. However, you requested 32768 output tokens "
                "and your prompt contains at least 11000 input tokens, for a total of at least 43768 tokens.")


def test_a_refusal_naming_the_context_window_gives_the_budget_that_fits_it():
    assert openrouter.fitted_budget(VLLM_REFUSAL) == 32768 - 11000 - openrouter.CONTEXT_FIT_MARGIN


def test_a_refusal_that_does_not_count_the_prompt_in_tokens_fits_nothing():
    assert openrouter.fitted_budget("maximum context length is 32768 tokens; at least 40000 input characters") is None


