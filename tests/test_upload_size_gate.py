"""Refusing an oversized upload before its bytes reach the host's disk."""
import pytest
from fastapi.testclient import TestClient

from incipit import db, review_server
from incipit.config import Settings


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    return TestClient(application)


def test_an_oversized_upload_is_refused_on_its_declared_length(client):
    """Reported in review: multipart is spooled to the temp disk before any route body runs, so
    the per-folder check inside the handler cannot be what stops a request from filling it."""
    oversized = str(review_server.MAX_REQUEST_BYTES + 1)

    response = client.post("/api/new/batches", content=b"", headers={
        "content-type": "multipart/form-data; boundary=x", "content-length": oversized})

    assert response.status_code == 413


def test_the_refusal_names_the_limit_a_reviewer_was_told_about(client):
    oversized = str(review_server.MAX_REQUEST_BYTES + 1)

    response = client.post("/api/new/batches", content=b"", headers={
        "content-type": "multipart/form-data; boundary=x", "content-length": oversized})

    megabytes = review_server.MAX_UPLOAD_TOTAL_BYTES // (1024 * 1024)
    assert f"{megabytes} MB" in response.json()["detail"]


def test_an_upload_that_does_not_declare_its_size_is_refused(client):
    """Without a length there is nothing to check before the body arrives, and a browser always
    sends one for a file upload."""
    def chunks():
        yield b""

    response = client.post("/api/new/batches", content=chunks(),
                           headers={"content-type": "multipart/form-data; boundary=x"})

    assert response.status_code == 411


def test_an_ordinary_request_is_not_asked_to_declare_a_length(client):
    """Only the routes that carry a real body; everything else may be chunked."""
    def chunks():
        yield b'{"folder_id": "E2002"}'

    response = client.post("/api/new/suggest-doc-ids", content=chunks(),
                           headers={"content-type": "application/json"})

    assert response.status_code != 411


def test_a_malformed_content_length_is_a_bad_request_not_a_crash(client):
    response = client.post("/api/new/batches", content=b"", headers={
        "content-type": "multipart/form-data; boundary=x", "content-length": "not-a-number"})

    assert response.status_code == 400


def test_an_oversized_presence_heartbeat_is_refused_with_its_small_limit(client):
    response = client.post("/api/presence", content=b"", headers={
        "content-type": "application/json",
        "content-length": str(review_server.MAX_PRESENCE_BYTES + 1),
    })

    assert response.status_code == 413
    assert "8 KB" in response.json()["detail"]


def test_the_gate_leaves_a_normal_sized_upload_alone(client, tmp_path):
    """The ceiling must sit far enough above a real folder to be invisible to one."""
    from conftest import tiny_image_bytes

    response = client.post("/api/new/batches", files=[
        ("files", ("0001.jpg", tiny_image_bytes(), "image/jpeg"))])

    assert response.status_code == 200
    with db.connect(str(tmp_path / "extraction.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM new_batches").fetchone()[0] == 1


# ---------- the lookup-table CSV, which costs far more memory than it weighs ----------


