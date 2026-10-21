"""The bucket: keys built from internal ids, atomic writes, and deletes that stay inside the root."""

from pathlib import Path

import pytest

from incipit.storage import BucketStorage, derived_key


def _storage(tmp_path: Path) -> BucketStorage:
    return BucketStorage(f"file://{tmp_path.as_posix()}")


def test_a_raw_upload_is_stored_under_its_batch_directory(tmp_path: Path):
    storage = _storage(tmp_path)

    key, uri = storage.upload_bytes("b1234", "0000_scan.jpg", b"fake-image-bytes")

    assert key == "uploads/b1234/0000_scan.jpg"
    assert uri == f"file://{tmp_path.as_posix()}/uploads/b1234/0000_scan.jpg"
    assert (tmp_path / "uploads" / "b1234" / "0000_scan.jpg").read_bytes() == b"fake-image-bytes"


@pytest.mark.parametrize("write_method", ["upload_bytes", "write_bytes"])
def test_failed_overwrite_preserves_the_live_scan(tmp_path: Path, monkeypatch, write_method):
    storage = BucketStorage((tmp_path / "bucket").as_uri())
    key, _ = storage.upload_bytes("b1", "scan.jpg", b"original")
    original_open = storage.fs.open

    def partial_write(path, mode="rb", *args, **kwargs):
        if mode == "wb":
            with original_open(path, mode, *args, **kwargs) as fh:
                fh.write(b"partial")
            raise OSError("simulated disk failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(storage.fs, "open", partial_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        if write_method == "upload_bytes":
            storage.upload_bytes("b1", "scan.jpg", b"replacement")
        else:
            storage.write_bytes(key, b"replacement")
    assert storage.read_bytes(key) == b"original"
    assert list((tmp_path / "bucket").rglob("*.tmp-*")) == []


def test_a_documents_scans_are_keyed_by_its_folder_and_document_ids(tmp_path: Path):
    storage = _storage(tmp_path)

    key, _ = storage.upload_document("f0001", "d0001", "page.jpg", b"x")

    assert key == "documents/f0001/d0001/page.jpg"


def test_a_documents_record_is_keyed_by_its_folder_and_document_ids(tmp_path: Path):
    assert _storage(tmp_path).record_key("f0001", "d0001") == "records/f0001/d0001.json"


def test_a_record_needs_both_ids(tmp_path: Path):
    with pytest.raises(ValueError):
        _storage(tmp_path).record_key("", "d0001")


def test_a_record_round_trips(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.write_record("f0001", "d0001", '{"a": 1}')

    assert storage.read_record("f0001", "d0001") == '{"a": 1}'


def test_a_record_that_is_not_there_reads_as_none(tmp_path: Path):
    assert _storage(tmp_path).read_record("f0001", "d9999") is None


def test_delete_record_removes_the_record(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.write_record("f0001", "d0001", "{}")

    storage.delete_record("f0001", "d0001")

    assert storage.read_record("f0001", "d0001") is None


def test_copy_duplicates_bytes_into_the_documents_directory(tmp_path: Path):
    storage = _storage(tmp_path)
    source_key, _ = storage.upload_bytes("b1", "0001_scan.jpg", b"raw-scan-bytes")

    new_key, _ = storage.copy(source_key, "f0001", "d0001", "scan.jpg")

    assert new_key == "documents/f0001/d0001/scan.jpg"
    assert storage.read_bytes(new_key) == b"raw-scan-bytes"
    # The intake copy is left in place, as an audit trail.
    assert storage.exists(source_key)


def test_delete_prefix_removes_every_object_under_that_batch(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.upload_bytes("b1", "0001.jpg", b"a")
    storage.upload_bytes("b1", "0002.jpg", b"b")
    storage.upload_bytes("b2", "0001.jpg", b"c")

    storage.delete_prefix("uploads", "b1")

    assert not storage.exists("uploads/b1/0001.jpg")
    assert not storage.exists("uploads/b1/0002.jpg")
    assert storage.exists("uploads/b2/0001.jpg")


def test_delete_prefix_is_a_no_op_when_nothing_exists(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.write_bytes("uploads/b1/0000.jpg", b"a scan")

    storage.delete_prefix("uploads", "never-uploaded")

    assert storage.exists("uploads/b1/0000.jpg")


def test_delete_document_removes_only_that_document(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.upload_document("f0001", "d0001", "page.jpg", b"x")
    storage.upload_document("f0001", "d0002", "page.jpg", b"y")

    storage.delete_document("f0001", "d0001")

    assert not storage.exists("documents/f0001/d0001/page.jpg")
    assert storage.exists("documents/f0001/d0002/page.jpg")


def test_full_path_rejects_a_key_that_would_escape_the_bucket_root(tmp_path: Path):
    storage = _storage(tmp_path)

    for traversal in ["../../../etc", "a/../../../b", "../.."]:
        with pytest.raises(ValueError):
            storage._full_path(f"documents/{traversal}")


def test_full_path_allows_dots_that_stay_within_the_root(tmp_path: Path):
    """This is about escaping the root, not about rejecting ".." inside a name."""
    storage = _storage(tmp_path)
    assert storage._full_path("documents/a..b/c.jpg").endswith("documents/a..b/c.jpg")


def test_delete_prefix_raises_rather_than_deleting_outside_the_bucket_root(tmp_path: Path):
    with pytest.raises(ValueError):
        _storage(tmp_path).delete_prefix("documents", "../../escaped")


def test_delete_all_removes_every_object_under_every_prefix(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.upload_bytes("b1", "0001.jpg", b"b")
    storage.upload_document("f0001", "d0001", "0001.jpg", b"c")

    storage.delete_all()

    assert not storage.exists("uploads/b1/0001.jpg")
    assert not storage.exists("documents/f0001/d0001/0001.jpg")


def test_delete_all_leaves_the_bucket_usable_for_a_fresh_upload(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.upload_bytes("b1", "0001.jpg", b"a")

    storage.delete_all()
    key, _ = storage.upload_bytes("b1", "0001.jpg", b"fresh")

    assert storage.read_bytes(key) == b"fresh"


def test_delete_all_is_a_no_op_on_an_empty_bucket(tmp_path: Path):
    storage = _storage(tmp_path)

    storage.delete_all()

    assert list(tmp_path.iterdir()) == []


# ---------- a copied scan brings its rendered sizes with it ----------

SOURCE = "uploads/b1/page.jpg"


def _with_derived(tmp_path: Path) -> BucketStorage:
    storage = _storage(tmp_path)
    storage.upload_bytes("b1", "page.jpg", b"the scan")
    storage.write_bytes(derived_key(SOURCE, "view"), b"the view copy")
    storage.write_bytes(derived_key(SOURCE, "thumb"), b"the thumb copy")
    return storage


def test_a_copy_carries_every_rendered_size_to_the_new_key(tmp_path: Path):
    storage = _with_derived(tmp_path)
    key, _ = storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")

    assert storage.read_bytes(derived_key(key, "view")) == b"the view copy"
    assert storage.read_bytes(derived_key(key, "thumb")) == b"the thumb copy"


def test_the_original_s_rendered_sizes_are_left_where_they_were(tmp_path: Path):
    """The intake batch stays as an audit trail, and the split-review screen still reads it."""
    storage = _with_derived(tmp_path)
    storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")

    assert storage.exists(derived_key(SOURCE, "view"))


def test_a_scan_with_nothing_rendered_yet_copies_anyway(tmp_path: Path):
    """Warming is best-effort at upload time, so a page can arrive with no derived copies."""
    storage = _storage(tmp_path)
    storage.upload_bytes("b1", "page.jpg", b"the scan")

    key, _ = storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")
    assert storage.read_bytes(key) == b"the scan"
    assert not storage.exists(derived_key(key, "view"))


def test_a_derived_copy_that_will_not_copy_does_not_fail_the_confirmation(tmp_path: Path, monkeypatch):
    """The scan is already stored by the time this runs."""
    storage = _with_derived(tmp_path)
    monkeypatch.setattr(storage.fs, "copy",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    key, _ = storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")
    assert storage.read_bytes(key) == b"the scan"
    assert not storage.exists(derived_key(key, "view"))


def test_the_copied_sizes_are_the_ones_the_bucket_holds_not_the_ones_in_code(tmp_path: Path):
    """Discovered from disk, so a size renamed in code still travels with the scan."""
    storage = _with_derived(tmp_path)
    storage.write_bytes(derived_key(SOURCE, "retired_size"), b"an old rendering")

    key, _ = storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")
    assert storage.read_bytes(derived_key(key, "retired_size")) == b"an old rendering"


def test_deleting_the_confirmed_document_takes_its_copied_renderings_too(tmp_path: Path):
    """Copying them in means there are now two caches of the same scan."""
    storage = _with_derived(tmp_path)
    key, _ = storage.copy(SOURCE, "f0001", "d0001", "0000_page.jpg")

    storage.delete_document("f0001", "d0001")
    assert not storage.exists(derived_key(key, "view"))
    assert not storage.exists(derived_key(key, "thumb"))


def test_a_record_held_open_for_a_moment_is_still_replaced(tmp_path: Path, monkeypatch):
    """Found in the push review: on Windows a backup copying the old record made the replace, and so the save, fail."""
    from incipit import storage as storage_module
    storage = BucketStorage((tmp_path / "bucket").as_uri())
    key, _ = storage.upload_bytes("b1", "record.json", b"old")
    real_replace, refusals = storage_module.os.replace, []

    def busy_once(source, destination):
        if not refusals:
            refusals.append(destination)
            raise PermissionError(13, "in use by another process")
        real_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", busy_once)
    monkeypatch.setattr(storage_module.time, "sleep", lambda seconds: None)
    storage.write_bytes(key, b"new")
    assert storage.read_bytes(key) == b"new"
