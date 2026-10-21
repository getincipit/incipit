"""Contract tests for the compiled default prompt, which replaced the prompt files in M3. Covers M0 AC-8."""

import re

from incipit import openrouter, prompting

DEFAULT = prompting.compile({})
PROMPT = DEFAULT.document_prompt
MERGE = DEFAULT.merge_prompt
SPLIT = DEFAULT.split_prompt


def _flat(text: str) -> str:
    return " ".join(text.split())


def _field_rule(prompt: str, field: str) -> str:
    """The text of one `- `field`:` bullet, up to the next bullet."""
    start = prompt.index(f"- `{field}`:")
    rest = prompt[start + 1:]
    end = rest.find("\n- `")
    return rest[:end if end != -1 else len(rest)]


def test_ac8_no_prompt_names_a_particular_archive_person_or_place():
    """The fork ships neutral prompts; a project's own names arrive through its prompt profile."""
    from test_fork_residue import PROPRIETARY

    for name, text in (("document", PROMPT), ("split", SPLIT), ("merge", MERGE)):
        assert not PROPRIETARY.search(text), f"the {name} prompt names the original project"


# ---------- the machinery every project needs ----------


def test_ac8_the_document_prompt_has_exactly_one_slot_for_each_page_count():
    assert PROMPT.count("{page_count}") == 1
    assert PROMPT.count("{page_break_count}") == 1
    rendered = openrouter.build_new_document_messages([b"x", b"y", b"z"], DEFAULT)[0]["content"][0]["text"]
    assert "3 scan(s)" in rendered and "exactly 2 page break(s)" in rendered


def test_the_split_prompt_renders_its_page_count_everywhere():
    rendered = openrouter.build_split_messages([b"x", b"y"], DEFAULT)[0]["content"][0]["text"]
    assert "{page_count}" not in rendered and "2 scanned page images" in rendered


def test_ac8_the_transcription_may_not_contain_words_the_page_does_not():
    """The counterweight to every "you must include" instruction."""
    assert "Never write a word the page does not" in _flat(PROMPT)
    assert "this one wins" in PROMPT


def test_an_unreadable_word_is_marked_rather_than_guessed():
    assert "Do not supply a word you cannot actually read" in _flat(PROMPT)
    assert "`[unclear: word?]`" in PROMPT


def test_structural_tagging_is_stated_as_mandatory():
    assert "MANDATORY, not optional" in PROMPT
    tags = PROMPT[PROMPT.index("Structural tagging is MANDATORY"):PROMPT.index("Respond with a JSON object")]
    assert "- `[Marginal note]`" not in tags


def test_ac8_tags_must_not_be_invented_to_satisfy_the_requirement():
    assert "never add a tag or a note to satisfy this instruction" in _flat(PROMPT).lower()
    assert "must NOT get a `[Letterhead]` tag" in _flat(PROMPT)


def test_fidelity_does_not_become_an_excuse_to_omit_what_is_there():
    assert "It does not license leaving out material that IS on the page" in _flat(PROMPT)


def test_marginal_notes_are_asked_for_in_the_metadata_only():
    flat = _flat(PROMPT)
    assert "Marginal notes belong in `marginal_notes`, not in `text`" in flat
    assert "with NO `[Marginal note]` tag anywhere in it" in flat
    assert "MUST have them in `marginal_notes`" in flat


def test_a_marginal_note_says_where_and_on_which_page():
    flat = _flat(PROMPT)
    assert "The page number is the scan the annotation is on" in flat
    assert "A single-page document still says `Page 1`" in flat
    for position in prompting.MARGINAL_POSITIONS:
        assert f"`{position}`" in PROMPT, position
    assert "spelled exactly as written here" in flat


def test_a_page_holding_only_a_marginal_note_still_gets_its_page_marker():
    assert "only content is a marginal annotation still gets its `[page N]` marker" in _flat(PROMPT)


def test_handwriting_written_across_a_typed_page_is_asked_for():
    flat = _flat(PROMPT)
    assert "Handwriting written over a typed page is the marginalia that matters most" in flat
    assert "a partly legible reply is recorded, never omitted" in flat
    assert "**Each mark is recorded once.**" in flat


# ---------- the default index conventions ----------


def test_every_indexed_name_field_asks_for_last_first():
    for prompt, keys in ((PROMPT, ("names", "author", "recipient")), (MERGE, ("author", "recipient", "names"))):
        for key in keys:
            assert "`Last, First Middle`" in _field_rule(prompt, key), key


def test_no_name_field_inverts_an_organisation():
    for prompt, keys in ((PROMPT, ("names", "author", "recipient")), (MERGE, ("author", "recipient", "names"))):
        for key in keys:
            rule = _flat(_field_rule(prompt, key)).lower()
            states_it = any(p in rule for p in ("never inverted", "not inverted", "exactly as written"))
            defers = "same" in rule and ("as `author`" in rule or "as `names`" in rule)
            assert states_it or defers, key


def test_places_use_usps_two_letter_state_codes_in_the_index_only():
    rule = _flat(_field_rule(PROMPT, "places"))
    assert "USPS two-letter state code" in rule
    assert "normalises the index field only" in rule
    assert "`text` and the other verbatim fields keep what the page wrote" in rule


def test_the_json_field_list_is_the_one_the_code_reads():
    listed = set(re.findall(r"^- `([a-z_]+)`:", PROMPT, flags=re.MULTILINE))
    assert listed == set(DEFAULT.document_schema["json_schema"]["schema"]["required"])


# ---------- the merge prompt ----------


def test_the_merge_prompt_never_asks_for_transcription():
    assert "do not re-transcribe" in MERGE.lower()
    listed = set(re.findall(r"^- `([a-z_]+)`:", MERGE, flags=re.MULTILINE))
    assert listed == {"document_type", "date", "author", "recipient", "place", "names", "subjects"}


def test_the_merge_prompt_names_why_the_fragments_were_wrong():
    assert "signature" in MERGE and "salutation" in MERGE


def test_the_merge_prompt_has_exactly_one_substitution_slot():
    assert MERGE.count("{document_text}") == 1
    rendered = openrouter.build_merge_metadata_messages("SOME TEXT", DEFAULT)[0]["content"][0]["text"]
    assert "SOME TEXT" in rendered
