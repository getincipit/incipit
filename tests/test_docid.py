from incipit import docid


def test_next_document_ids_starts_at_aaa_when_folder_is_unused():
    assert docid.next_document_ids("D8026", set(), 3) == ["D8026AAA", "D8026AAB", "D8026AAC"]


def test_next_document_ids_continues_after_existing_suffix():
    assert docid.next_document_ids("D8026", {"D8026AAA"}, 2) == ["D8026AAB", "D8026AAC"]


def test_next_document_ids_skips_gaps_in_the_middle():
    assert docid.next_document_ids("D8026", {"D8026AAA", "D8026AAC"}, 2) == ["D8026AAB", "D8026AAD"]


def test_next_document_ids_ignores_ids_from_a_different_folder():
    assert docid.next_document_ids("D8026", {"D8027AAA"}, 1) == ["D8026AAA"]


def test_next_document_ids_ignores_ids_of_the_wrong_shape():
    # too short, too long, and a differently-shaped id from another collection (e.g. X018A4-0242)
    assert docid.next_document_ids("D8026", {"D8026AA", "D8026AAAX", "X018A4-0242"}, 1) == ["D8026AAA"]


def test_next_document_ids_rolls_over_from_aaz_to_aba():
    existing = {f"D8026AA{chr(ord('A') + i)}" for i in range(26)}  # AAA..AAZ all used
    assert docid.next_document_ids("D8026", existing, 1) == ["D8026ABA"]


def test_next_document_ids_handles_a_large_count_spanning_a_rollover():
    result = docid.next_document_ids("D8026", set(), 27)
    assert result[0] == "D8026AAA"
    assert result[25] == "D8026AAZ"
    assert result[26] == "D8026ABA"


def test_require_safe_id_accepts_real_document_and_folder_ids():
    for value in ["E2241AAA", "D8026", "CL207AAA", "X018A4-0242", "D0014AAI"]:
        assert docid.require_safe_id(value) == value


def test_require_safe_id_rejects_path_traversal_and_separators():
    import pytest

    for bad in ["../etc", "..", "a/b", "a\b", "a b", "", "foo.bar", "a/../b"]:
        with pytest.raises(ValueError):
            docid.require_safe_id(bad, "document id")


# ---------- the live site's archival folder string ----------


