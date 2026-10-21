"""A document the primary model will not draft goes to the fallback model, not to nothing."""

import pytest

from incipit import config, db, modeling
from incipit.config import Settings
from conftest import insert_rows, seed_batch, draft_call


def settings(**overrides):
    base = dict(db_path=":memory:", openrouter_api_key="k")
    base.update(overrides)
    return Settings(**base)


# ---------- when it is reached ----------


def test_the_fallback_is_only_tried_after_the_retries_are_spent():
    """Four goes at the first question before asking the second: the retries are for a service that
    misbehaved, the fallback for a model that will not do this document at all."""
    attempts, fallbacks = [], []

    def call():
        attempts.append(1)
        raise RuntimeError("no usable response")

    def last_resort():
        fallbacks.append(1)
        return "the fallback's draft"

    result = modeling.with_model_retries("a document", call, last_resort=last_resort)

    assert result == "the fallback's draft"
    assert len(attempts) == modeling.MAX_DOCUMENT_ATTEMPTS
    assert len(fallbacks) == 1


def test_a_document_that_drafts_never_reaches_the_fallback():
    fallbacks = []
    result = modeling.with_model_retries("a document", lambda: "a draft",
                                    last_resort=lambda: fallbacks.append(1))

    assert result == "a draft"
    assert fallbacks == []


def test_the_first_failure_is_what_is_reported_when_the_fallback_fails_too():
    """The primary model's failure is the one a reviewer needs to see."""
    def call():
        raise RuntimeError("qwen3.7-plus abandoned the answer")

    def last_resort():
        raise RuntimeError("the fallback was busy")

    with pytest.raises(RuntimeError, match="abandoned the answer"):
        modeling.with_model_retries("a document", call, last_resort=last_resort)


def test_a_run_with_no_fallback_configured_still_raises():
    def call():
        raise RuntimeError("no usable response")

    with pytest.raises(RuntimeError):
        modeling.with_model_retries("a document", call)


# ---------- what counts as having one ----------


def test_a_fallback_is_configured_by_default():
    assert modeling.has_fallback_model(settings()) is True
    assert config.DEFAULT_OPENROUTER_FALLBACK_MODEL.startswith("qwen/")


def test_the_same_model_twice_is_not_a_fallback():
    same = settings(openrouter_model="qwen/x", openrouter_fallback_model="qwen/x")

    assert modeling.has_fallback_model(same) is False


def test_an_empty_fallback_is_not_a_fallback():
    assert modeling.has_fallback_model(settings(openrouter_fallback_model="")) is False


# ---------- and the analytics say which model did the work ----------


def test_the_analytics_count_the_documents_each_model_drafted(tmp_path):
    """"We will run qwen3.7plus where it works and if it doesnt use the fall back" -- how often
    that happened is a thing to be able to see."""
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    with db.connect(path) as conn:
        seed_batch(conn, "B", 3, folder="E2003", name="E2003_Articles")
        for document_id, model in (("E2003AAA", "qwen/qwen3.7-plus"),
                                   ("E2003AAB", "qwen/qwen3.7-plus"),
                                   ("E2003AAC", "qwen/qwen3.6-35b-a3b")):
            insert_rows(conn,
                "INSERT INTO new_documents (document_id, batch_id, status, model) "
                "VALUES (?, 'B', 'drafted', ?)", (document_id, model))

        analytics = db.get_new_folder_analytics(conn, "E2003")

    assert analytics["documents_by_model"] == {
        "qwen/qwen3.6-35b-a3b": 1, "qwen/qwen3.7-plus": 2}


def test_a_folder_nothing_has_drafted_reports_no_models(tmp_path):
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    with db.connect(path) as conn:
        analytics = db.get_new_folder_analytics(conn, "E2003")

    assert analytics["documents_by_model"] == {}


# ---------- the failures that used to step over the fallback ----------


@pytest.mark.parametrize("failure", [
    "ResponseTruncatedError",
    "DegenerateResponseError",
])
def test_a_failure_the_same_model_cannot_fix_still_reaches_the_fallback(failure):
    """Reported: a document failed with the fallback configured."""
    from incipit import openrouter

    attempts, fallbacks = [], []

    def call():
        attempts.append(1)
        raise getattr(openrouter, failure)("the model will not do this document")

    result = modeling.with_model_retries("a document", call,
                                    last_resort=lambda: (fallbacks.append(1), "a draft")[1])

    assert result == "a draft"
    assert len(attempts) == 1, "the same model was asked again, which buys the same answer"
    assert len(fallbacks) == 1


@pytest.mark.parametrize("failure", ["AccountError", "RequestTooLargeError"])
def test_the_failures_no_model_can_fix_are_raised_at_once(failure):
    """A bad key, no credits, or a request the gateway refuses at any size: another model is not
    the answer and trying one wastes a reviewer's time."""
    from incipit import openrouter

    fallbacks = []

    def call():
        raise getattr(openrouter, failure)("hopeless")

    with pytest.raises(getattr(openrouter, failure)):
        modeling.with_model_retries("a document", call,
                               last_resort=lambda: fallbacks.append(1))

    assert fallbacks == []


# ---------- what is not worth another attempt ----------


def test_a_missing_scan_is_not_retried():
    """Reported: a redraft of a document whose scan had left the bucket spent over two minutes
    retrying a file that could never appear."""
    attempts = []

    def call():
        attempts.append(1)
        raise FileNotFoundError("documents/E2229/E2229AAB/page.jpg")

    with pytest.raises(FileNotFoundError):
        modeling.with_model_retries("a document", call)
    assert len(attempts) == 1


def test_a_missing_scan_does_not_reach_the_fallback():
    """The fallback model reads the same missing file."""
    fallbacks = []

    def call():
        raise FileNotFoundError("documents/E2229/E2229AAB/page.jpg")

    with pytest.raises(FileNotFoundError):
        modeling.with_model_retries("a document", call, last_resort=lambda: fallbacks.append(1))
    assert fallbacks == []


def test_a_dropped_connection_is_still_retried():
    """requests' connection errors are OSErrors too; only a missing file is excused from retrying."""
    import requests

    attempts = []

    def call():
        attempts.append(1)
        if len(attempts) < 2:
            raise requests.ConnectionError("connection reset")
        return "a draft"

    assert modeling.with_model_retries("a document", call) == "a draft"


# ---------- reported 2026-09-30: qwen3.7-plus kept returning broken JSON and nothing took over ----------


class _Reply:
    status_code = 200

    def __init__(self, content):
        self._payload = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                         "usage": {"cost": 0.0}}
        self.text = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_a_model_that_keeps_sending_broken_json_is_reported_as_malformed(monkeypatch):
    """Malformed every time is this model's answer to this document, not an outage."""
    from incipit import openrouter
    monkeypatch.setattr("requests.Session.post",
                        lambda self, url, headers=None, json=None, timeout=None:
                        _Reply('{"text": "a" "b"}'))

    with pytest.raises(openrouter.MalformedResponseError):
        draft_call([{"role": "user", "content": []}], settings())


def test_broken_json_goes_to_the_fallback_without_another_round():
    from incipit import openrouter
    attempts = []

    def call():
        attempts.append(1)
        raise openrouter.MalformedResponseError("qwen3.7-plus did not return a usable response")

    result = modeling.with_model_retries("a document", call, last_resort=lambda: "the fallback's draft")

    assert result == "the fallback's draft"
    assert len(attempts) == 1


def test_a_failure_of_both_models_says_the_fallback_was_tried():
    def call():
        raise RuntimeError("qwen3.7-plus abandoned the answer")

    def last_resort():
        raise RuntimeError("qwen3.6 was busy")

    with pytest.raises(RuntimeError, match="fallback model could not do it either: qwen3.6 was busy"):
        modeling.with_model_retries("a document", call, last_resort=last_resort)


def _models_asked(monkeypatch, fail_on):
    """Every model call_image_model is asked with, failing each one named in `fail_on`."""
    from incipit import new_document, openrouter
    asked = []

    def fake(pages, storage, on, **kwargs):
        asked.append(on.openrouter_model)
        if on.openrouter_model in fail_on:
            raise openrouter.MalformedResponseError(f"{on.openrouter_model} sent broken JSON")
        return {"text": f"drafted by {on.openrouter_model}"}

    monkeypatch.setattr(new_document, "call_image_model", fake)
    return asked


def test_the_next_document_goes_back_to_the_primary_model(monkeypatch):
    """A fallback is for the document the primary failed, not for the rest of the run."""
    from incipit import new_document, prompting
    s = settings()
    asked = _models_asked(monkeypatch, fail_on=set())
    new_document._draft_with_retries("E2023AAB", [], None, s, prompting.compile({}))

    assert asked == [s.openrouter_model]
