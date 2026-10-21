from pathlib import Path

from incipit import backup, db, export_archive


def test_backup_pass_retries_exports_and_removes_stale_mirror_names(tmp_path: Path):
    db_path = tmp_path / "data" / "extraction.db"
    db_path.parent.mkdir(parents=True)
    db.init_db(str(db_path))
    source = db_path.parent / "export" / "new" / "E2013_Business Propositions"
    source.mkdir(parents=True)
    current = source / "E2013_Business_Propositions_transcriptions.zip"
    current.write_bytes(b"current")
    local_backup, mirror = tmp_path / "backup", tmp_path / "mirror"
    stale = mirror / "exports" / "new" / "old" / "old.zip"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    summary = backup.run_backup(db_path, local_backup, data_dir=db_path.parent, mirror_dir=mirror)

    relative = current.relative_to(db_path.parent / "export")
    assert summary["exports_copied"] == 1
    assert (local_backup / "exports" / relative).read_bytes() == b"current"
    assert (mirror / "exports" / relative).read_bytes() == b"current"
    assert not stale.exists()


def test_an_archive_larger_than_one_chunk_is_still_recognised_as_unchanged(tmp_path: Path):
    """The comparison reads in chunks now -- two whole export archives will not fit in memory."""
    source, destination = tmp_path / "source", tmp_path / "destination"
    for root in (source, destination):
        (root / "new").mkdir(parents=True)
        (root / "new" / "big.zip").write_bytes(b"x" * (3 * 1024 * 1024))

    assert export_archive.sync_tree(source, destination) == 0


def test_an_archive_that_differs_late_in_the_file_is_copied(tmp_path: Path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    for root, tail in ((source, b"a"), (destination, b"b")):
        (root / "new").mkdir(parents=True)
        (root / "new" / "big.zip").write_bytes(b"x" * (3 * 1024 * 1024) + tail)

    assert export_archive.sync_tree(source, destination) == 1
    assert (destination / "new" / "big.zip").read_bytes()[-1:] == b"a"


def archived(root: Path, workflow: str, folder_id: str, name: str, body: bytes = b"zip") -> Path:
    path = root / export_archive.relative_path(workflow, folder_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_forget_is_content_with_a_folder_that_never_exported(tmp_path: Path):
    assert export_archive.forget(tmp_path / "export", "E2002") == []


def test_an_archive_is_filed_under_the_folders_internal_id(tmp_path: Path):
    """A rename changes the title and nothing else, so the directory never moves."""
    assert archived(tmp_path, "new", "f0001", "E2002_Advice").parent.name == "f0001"


def test_an_archive_is_named_for_the_folders_title(tmp_path: Path):
    assert archived(tmp_path, "new", "f0001", "E2002_Advice").name == "E2002_Advice_transcriptions.zip"


def test_a_folder_id_that_is_not_a_safe_token_is_refused(tmp_path: Path):
    import pytest
    with pytest.raises(ValueError):
        export_archive.relative_path("new", "../escape", "Advice")


def test_failed_copy_cleans_its_partial_file(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        export_archive._atomic_copy(tmp_path / "missing", tmp_path / "backup/copy.zip")
    assert not list(tmp_path.rglob("*.partial"))


def test_backup_uses_explicit_export_directory_without_falling_back(tmp_path):
    db_path = tmp_path / "data/extraction.db"
    db_path.parent.mkdir()
    db.init_db(str(db_path))
    root = tmp_path / "custom-exports"
    archive = archived(root, "new", "E2002", "Advice", b"current")
    summary = backup.run_backup(db_path, tmp_path / "backup", export_dir=root)
    assert summary["exports_copied"] == 1
    assert (tmp_path / "backup/exports" / archive.relative_to(root)).read_bytes() == b"current"


def test_ownership_manifest_prevents_deleting_another_folder_with_similar_name(tmp_path):
    import json
    root = tmp_path / "exports"
    mine = archived(root, "new", "E2002", "Advice")
    other = archived(root, "new", "E2002_Notes", "Notebook")
    for path, owner in ((mine, "E2002"), (other, "E2002_Notes")):
        (path.parent / "export.json").write_text(json.dumps({"folder_id": owner, "workflow": "new"}))
    export_archive.forget(root, "E2002")
    assert not mine.exists() and other.exists()
