
from incipit import db, new_document, prompting
from incipit.config import Settings
from incipit.new_document import NewDocumentOptions, document_title
from incipit.storage import BucketStorage
from conftest import bill, seed_document

DEFAULT = prompting.compile({})
# What the model answers; fakes hand it to whichever extractor the caller passed.
FAKE_ANSWER = {
    "text": "Dear Sir, I enclose the report.", "uncertain_words": [],
    "places": ["Troy, N. Y."], "names": ["Ada M. Lovell"], "challenges": ["typed"],
    "document_type": "Letter", "date": "1900-01-01", "author": "Hayes, Howard W",
    "recipient": "Gilmore, William Edgar", "place": "Troy, N. Y.", "subjects": ["Phonograph"],
}


def _answer(extract_result):
    return extract_result(dict(FAKE_ANSWER), {"cost": 0.0006})


def build_document_title(display_id, document_type, author, recipient, date):
    """The default profile's title for these values."""
    return document_title(DEFAULT, display_id, {"document_type": document_type, "author": author,
                                                "recipient": recipient, "date": date})


# ---------- build_document_title ----------


def test_build_document_title_matches_site_convention():
    title = build_document_title("D0014AAI", "Letter", "Chapman, Cloyd Mason; Miller, John Vincent", "Lovell, Ada Mary", "1900-06-19")
    assert title == "[D0014AAI], Letter from Cloyd Mason Chapman, John Vincent Miller to Ada Mary Lovell, June 19th, 1900"


def test_build_document_title_single_author_recipient():
    title = build_document_title("CL207AAA", "Letter", "Hayes, Howard W", "Gilmore, William Edgar", "1903-01-12")
    assert title == "[CL207AAA], Letter from Howard W Hayes to William Edgar Gilmore, January 12th, 1903"


def test_build_document_title_degrades_gracefully_when_everything_blank():
    assert build_document_title("NEWDOC001", "", "", "", "") == "[NEWDOC001], Untitled"


def test_build_document_title_omits_missing_recipient():
    title = build_document_title("NEWDOC002", "Letter", "Lovell, Ada Mary", "", "1900-01-01")
    assert title == "[NEWDOC002], Letter from Ada Mary Lovell, January 1st, 1900"


def test_build_document_title_handles_ordinal_suffixes():
    assert "3rd" in build_document_title("X", "Letter", "", "", "1900-01-03")
    assert "11th" in build_document_title("X", "Letter", "", "", "1900-01-11")
    assert "21st" in build_document_title("X", "Letter", "", "", "1900-01-21")


# ---------- transcribe_new_document / run_transcribe_new_documents ----------


def _settings(tmp_path) -> Settings:
    return Settings(
        bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}",
        db_path=str(tmp_path / "extraction.db"),
        openrouter_api_key="sk-test",
    )


def _seed_confirmed_group(tmp_path, settings, document_id="NEWDOC001", page_count=1):
    db.init_db(settings.db_path)
    storage = BucketStorage(settings.bucket_uri)
    batch_id = f"BATCH-{document_id}"

    pages = []
    for index in range(page_count):
        bucket_key, bucket_uri = storage.upload_bytes(
            document_id, f"{index:04d}.jpg", b"fake-jpeg-bytes", prefix="new"
        )
        pages.append({"sequence_index": index, "bucket_key": bucket_key, "bucket_uri": bucket_uri})

    with db.connect(settings.db_path) as conn:
        db.create_batch(conn, batch_id, image_count=page_count)
        seed_document(conn, document_id, batch_id, pages=pages)


def test_transcribe_new_document_records_error_without_crashing(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed_confirmed_group(tmp_path, settings)


    def failing_call(*args, **kwargs):
        raise RuntimeError("openrouter 500")

    monkeypatch.setattr("incipit.new_document.openrouter.call_openrouter", failing_call)

    storage = BucketStorage(settings.bucket_uri)
    options = NewDocumentOptions()
    with db.connect(settings.db_path) as conn:
        new_document.transcribe_new_document("NEWDOC001", settings, storage, conn, options)

    with db.connect(settings.db_path) as conn:
        row = db.get_new_document(conn, "NEWDOC001")
        assert row["status"] == "error"
        assert "openrouter 500" in row["error_message"]


def test_run_transcribe_new_documents_stops_once_cost_cap_reached(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed_confirmed_group(tmp_path, settings, "DOC_A")
    _seed_confirmed_group(tmp_path, settings, "DOC_B")

    calls = []

    def fake_call(messages, settings_arg, schema=None, extract_result=None):
        # Counts drafting calls only.
        if "text" in schema["json_schema"]["schema"]["properties"]:
            calls.append(1)
            bill(settings_arg.db_path, 0.0006)
            return _answer(extract_result)  # cost 0.0006 each
        return {"picks": []}

    monkeypatch.setattr("incipit.new_document.openrouter.call_openrouter", fake_call)

    options = NewDocumentOptions(max_total_cost=0.0006)
    new_document.run_transcribe_new_documents(settings, options)

    assert len(calls) == 1
    with db.connect(settings.db_path) as conn:
        assert db.get_new_document_status(conn, "DOC_A") == "drafted"
        assert db.get_new_document_status(conn, "DOC_B") == "split_confirmed"  # untouched, cap hit first


def test_new_document_drafting_sends_every_page_image(tmp_path, monkeypatch):
    """The seam the other tests here stub past: a new document has no site metadata to fall back
    on, so if the pages don't reach the model there is nothing to catalogue it from -- and a
    stub that ignores `messages` would report success either way."""
    settings = _settings(tmp_path)
    _seed_confirmed_group(tmp_path, settings, "DOC_A", page_count=3)
    sent = []
    monkeypatch.setattr(
        "incipit.new_document.openrouter.call_openrouter",
        lambda messages, settings_arg, **kwargs: sent.append(messages) or _answer(kwargs["extract_result"]),
    )

    new_document.run_transcribe_new_documents(
        settings, NewDocumentOptions()
    )

    content = sent[0][0]["content"]
    assert content[0]["type"] == "text"
    images = [block for block in content if block["type"] == "image_url"]
    assert len(images) == 3
    assert all(img["image_url"]["url"].startswith("data:image/jpeg;base64,") for img in images)
