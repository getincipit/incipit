"""The prompt compiler: a profile's answers become the document, split and merge prompts, their JSON
schemas, the field registry and the index styles.

The fixed paragraphs below are the machinery every archive needs (the page-break rule, the fidelity
rule, structural tags, positioned marginal notes); everything about one archive's period, people,
places, vocabularies and conventions is generated from its answers. `{page_count}` and
`{page_break_count}` (and the merge prompt's `{document_text}`) are the only placeholders a compiled
prompt keeps; every other brace is escaped before it is stored.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from . import fields as registry_mod

# ---------- the questionnaire ----------

STYLE_OPTIONS = {
    "names": ("last_first", "first_last", "as_written"),
    "us_places": ("usps", "full_state", "as_written"),
    "world_places": ("country", "as_written"),
    "dates": ("iso", "as_written"),
    "separator": ("; ", " | "),
    "uncertain": ("bracket_unclear", "bracket_illegible", "question"),
    "marginal_notes": ("positioned", "inline", "ignore"),
    "crossouts": ("final", "marked"),
}
STYLE_SWITCHES = ("structural_tags", "overwritten_hand")

DEFAULT_ANSWERS = {
    "collection": "",
    "material": "letters, telegrams, notes, reports and similar papers",
    "period_from": None,
    "period_to": None,
    "languages": [],
    "region": "",
    "people": [],
    "organisations": [],
    "places": [],
    "vocabularies": {"document_types": {"mode": "free", "terms": []},
                     "subjects": {"mode": "free", "terms": []}},
    "styles": {"names": "last_first", "us_places": "usps", "world_places": "country", "dates": "iso",
               "separator": "; ", "uncertain": "bracket_unclear", "structural_tags": True,
               "marginal_notes": "positioned", "crossouts": "final", "overwritten_hand": True},
    "fields": {},
    "custom_fields": [],
    "title_template": registry_mod.DEFAULT_TITLE_TEMPLATE,
    "special_instructions": "",
}

QUESTIONNAIRE = {
    "sections": [
        {"id": "collection", "title": "The collection",
         "questions": ["collection", "material", "period_from", "period_to", "languages", "region"]},
        {"id": "entities", "title": "People, organisations and places it mentions",
         "questions": ["people", "organisations", "places"]},
        {"id": "vocabularies", "title": "Document types and subjects", "questions": ["vocabularies"]},
        {"id": "styles", "title": "How the index is written", "questions": ["styles"]},
        {"id": "fields", "title": "What is catalogued", "questions": ["fields", "custom_fields", "title_template"]},
        {"id": "special", "title": "Anything else the model should know", "questions": ["special_instructions"]},
    ],
    "style_options": {k: list(v) for k, v in STYLE_OPTIONS.items()},
    "style_switches": list(STYLE_SWITCHES),
    "builtin_fields": [{"key": f["key"], "label": f["label"], "type": f["type"],
                        "export_column": f.get("export_column", "")} for f in registry_mod.BUILTINS],
    "field_types": list(registry_mod.TYPES),
    "defaults": DEFAULT_ANSWERS,
}

UNCERTAIN_MARKERS = {"bracket_unclear": "[unclear: word?]", "bracket_illegible": "[illegible]",
                     "question": "word[?]"}
MARGINAL_POSITIONS = ("Upper left", "Top", "Upper right", "Left margin", "Right margin", "Lower left",
                      "Bottom", "Lower right", "Between lines", "Across the text", "Overleaf")
LIMITS = {"collection": 200, "material": 400, "region": 200, "special_instructions": 6000,
          "title_template": 200, "entity": 120, "note": 300, "term": 80}


class CompileError(ValueError):
    """Answers that cannot become a prompt, said so that the person can fix them."""


@dataclass
class Compiled:
    answers: dict
    registry: list
    styles: dict
    document_prompt: str
    split_prompt: str
    merge_prompt: str
    document_schema: dict
    merge_schema: dict
    content_hash: str = ""
    warnings: list = field(default_factory=list)


# ---------- reading the answers ----------


def _text(value, name: str, limit: int) -> str:
    text = " ".join(str(value or "").split()) if name != "special_instructions" else str(value or "").strip()
    if len(text) > limit:
        raise CompileError(f"{name.replace('_', ' ')} is at most {limit} characters")
    return text


def _year(value, name: str):
    if value in (None, ""):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise CompileError(f"{name.replace('_', ' ')} is a year, such as 1929") from None
    if not 0 < year < 3000:
        raise CompileError(f"{name.replace('_', ' ')} is a year, such as 1929")
    return year


def _entities(value, name: str) -> list[dict]:
    if not isinstance(value, list):
        raise CompileError(f"{name} must be a list")
    out = []
    for entry in value:
        entry = {"name": entry} if isinstance(entry, str) else entry
        if not isinstance(entry, dict):
            raise CompileError(f"each of the {name} is a name with an optional note")
        title = _text(entry.get("name"), "entity", LIMITS["entity"])
        if title:
            out.append({"name": title, "note": _text(entry.get("note"), "note", LIMITS["note"])})
    return out


def normalise(answers: dict) -> dict:
    """The answers over their defaults, every value checked; CompileError names the first bad one."""
    if not isinstance(answers, dict):
        raise CompileError("the answers must be an object")
    out = copy.deepcopy(DEFAULT_ANSWERS)
    for key, value in answers.items():
        if key not in out:
            raise CompileError(f"there is no question {key!r}")
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = {**out[key], **copy.deepcopy(value)}
        else:
            out[key] = copy.deepcopy(value)
    for key in ("collection", "material", "region", "special_instructions", "title_template"):
        out[key] = _text(out[key], key, LIMITS[key])
    out["period_from"] = _year(out["period_from"], "period_from")
    out["period_to"] = _year(out["period_to"], "period_to")
    if out["period_from"] and out["period_to"] and out["period_from"] > out["period_to"]:
        raise CompileError("the period ends before it begins")
    if not isinstance(out["languages"], list):
        raise CompileError("languages must be a list")
    out["languages"] = [_text(x, "entity", LIMITS["entity"]) for x in out["languages"] if str(x).strip()]
    for key in ("people", "organisations", "places"):
        out[key] = _entities(out[key], key)
    for key, vocabulary in out["vocabularies"].items():
        if key not in ("document_types", "subjects") or not isinstance(vocabulary, dict):
            raise CompileError(f"there is no vocabulary {key!r}")
        if vocabulary.get("mode") not in ("free", "controlled"):
            raise CompileError(f"the {key.replace('_', ' ')} vocabulary is free or controlled")
        vocabulary["terms"] = [_text(t, "term", LIMITS["term"]) for t in vocabulary.get("terms") or []
                               if str(t).strip()]
    styles = out["styles"]
    for key, value in styles.items():
        if key in STYLE_OPTIONS:
            if value not in STYLE_OPTIONS[key]:
                raise CompileError(f"{key.replace('_', ' ')} is one of {', '.join(map(repr, STYLE_OPTIONS[key]))}")
        elif key in STYLE_SWITCHES:
            styles[key] = bool(value)
        else:
            raise CompileError(f"there is no index style {key!r}")
    if not isinstance(out["fields"], dict) or not isinstance(out["custom_fields"], list):
        raise CompileError("fields must be an object and custom fields a list")
    if "{display_id}" not in out["title_template"]:
        raise CompileError("the title template must include {display_id}, so every title is unique")
    return out


# ---------- the paragraphs ----------


def _esc(text: str) -> str:
    """Typed or generated text, made safe for str.format: it can never add a placeholder."""
    return text.replace("{", "{{").replace("}", "}}")


def _period(a: dict) -> str:
    if a["period_from"] and a["period_to"]:
        return f"from {a['period_from']} to {a['period_to']}"
    if a["period_from"]:
        return f"from {a['period_from']} onwards"
    if a["period_to"]:
        return f"from before {a['period_to']}"
    return ""


def _role(a: dict) -> str:
    period = _period(a)
    material = a["material"] or "historical documents"
    where = a["collection"] or "an archive"
    line = (f"You are cataloguing a scanned document that is being added to {where} for the first time. "
            f"The collection holds {material}")
    line += f", {period}." if period else "."
    line += (" There is no existing catalogue record for it; everything below must be determined "
             "directly from the document image(s) themselves.")
    return line


def _era(a: dict) -> str:
    parts = ["Preserve the spelling, punctuation and formatting conventions of the document's own "
             "period — do not modernize them."]
    if a["languages"]:
        parts.append(f"Documents are mostly in {', '.join(a['languages'])}; transcribe each in the "
                     f"language it is written in, without translating it.")
    if a["region"]:
        parts.append(f"Most of the material concerns {a['region']}.")
    return " ".join(parts)


def _entities_section(a: dict) -> str:
    groups = [(title, a[key]) for key, title in (("people", "People"), ("organisations", "Organisations"),
                                                 ("places", "Places")) if a[key]]
    if not groups:
        return ""
    lines = ["**Names that recur in this collection.** These are spelling aids for words you can "
             "already see on the page: when a handwritten or faded word could be one of them, use "
             "this spelling. They are never a source of content — do not add a name the page does "
             "not show, and do not assume a document concerns someone because they are listed here."]
    for title, entries in groups:
        lines.append(f"\n{title}:")
        lines.extend(f"- {e['name']}" + (f" — {e['note']}" if e["note"] else "") for e in entries)
    return "\n".join(lines)


_TRANSCRIPTION_RULES = """Using the document image(s), produce the best possible transcription:
- Preserve original spelling, capitalization, and punctuation
{crossouts}
- Include insertions/corrections as part of the final intended text where clear
- Do not attempt to transcribe diagrams, sketches, or charts
- **Preserve the document's paragraph structure.** Start a new paragraph where the writer did, separated by a blank line — one empty line between them, never a single line break. The two mean different things: a single newline continues the same paragraph on a new line (an address block, a signature), a blank line ends one paragraph and begins the next. Writing a new paragraph on the next line instead of after a blank line merges it into the one before it. Do not insert breaks the document does not have, and do not re-wrap lines mid-sentence — a line ending simply because the page ran out of room is not a paragraph break.
- **You are being given {{page_count}} scan(s) of this document, in order, and your `text` must contain exactly {{page_break_count}} page break(s).** Where one scan ends and the next begins, put the break on its own line as `[page 2]` — the number of the scan that is starting. The review screen lines your transcription up against the scans using these, so a missing break leaves the reviewer reading page one's text beside page three's image for the rest of the document. Count the scans before you answer and check your breaks against that number. A scan whose only content is a marginal annotation still gets its `[page N]` marker.
- A scan that repeats a document already transcribed above it — a handwritten draft of a typed letter, a carbon copy — is still its own scan and still gets transcribed, from its own image. The two are not the same text: a typist regularises spelling, punctuation and wording that the draft shows as the writer actually set it down, and the archive wants both.
- Do not add commentary or explanation"""

_CROSSOUTS = {
    "final": "- For crossed-out text, output the final intended readable text",
    "marked": "- Keep crossed-out words that can still be read, marked `[crossed out: words]` where they "
              "stand, followed by what replaced them",
}

_FIDELITY = """**Above all: `text` is a transcription, not a reconstruction. Never write a word the page does not
contain.** Where this rule and any other instruction in this prompt appear to conflict — including
the structural-tagging requirement below — this one wins. Concretely:
- Do not finish a sentence the writer left unfinished, and do not repair grammar, spelling or
  punctuation that is simply wrong on the page. Period usage and plain mistakes are both part of
  the document.
- Do not supply a word you cannot actually read. Mark it `{marker}`{guess}, or leave it out. An
  invented reading is worse than an admitted gap, because everything downstream treats it as a word
  that was really there, and no reviewer can tell the difference without going back to the scan.
- Do not infer content from context, from the letterhead, or from what a document of this kind
  usually says. A plausible sentence is not evidence.
- If a page is largely illegible, transcribe what can be read and stop. A short honest
  transcription is a usable record; a long invented one is worse than none, because it silently
  corrupts the archive it joins.

This is about not manufacturing content. It does not license leaving out material that IS on the
page."""

_STRUCTURAL = """**Structural tagging is MANDATORY, not optional.** Tag non-authored structural material inline,
immediately before the material it labels, using a short bracketed tag. A page whose letterhead,
stamps or filing codes go untagged is an incomplete transcription, however accurate its words are:
the tags are what let a reader tell the author's own writing apart from the printed and
administrative material around it.

- `[Letterhead]` — printed stationery header: organisation names, telephone or cable details, the
  sender's printed address block
- `[Stamp]` — a receipt/date stamp or other administrative marking, not written by the author
- `[File notation]` — filing/reference codes or initials
- Use the same bracketed style for any other clearly non-authored structural element you encounter

**Record what is actually there -- never add a tag or a note to satisfy this instruction.** A page
with no letterhead must NOT get a `[Letterhead]` tag. The requirement is to miss nothing that is on
the page, not to produce tags; an invented tag is a worse error than a missing one, because it
describes a document that does not exist.

Everything else — the document's actual authored body, including its own salutation, dateline, and
signature — stays untagged."""

_MARGINAL_POSITIONED = """**Marginal notes belong in `marginal_notes`, not in `text`.** A marginal annotation is not part of
the document the author wrote -- it is something added alongside or on top of it, often by another
hand and often years later. Putting it in `text`, even as a bare tag, breaks the reading flow of
the transcription for no gain, so:

1. `text` carries the authored document only, with NO `[Marginal note]` tag anywhere in it, and
2. `marginal_notes` carries each annotation, one entry per annotation, **each beginning with
   where on the page it sits and which page it is on**, in one pair of square brackets, followed by
   the note itself:

   `[Upper right, Page 1] Tell him to come up and see me`
   `[Left margin, Page 2] enc/b HWH`

**Use exactly one of these positions, spelled exactly as written here:** {positions}.

**The page number is the scan the annotation is on**, counted the way the `[page N]` markers count
them, written as `Page 1`, `Page 2` and so on. A single-page document still says `Page 1`, so every
entry has the same shape.

Pick the position that best describes where the annotation physically is. `Between lines` is for a
note written into the body's own line spacing rather than in a margin; `Across the text` for one
written over the body itself, crossing its lines; `Overleaf` for one on the back of the sheet, with
the number of the scan it appears on. The position describes the annotation, not what it refers to.

**Each mark is recorded once.** A circled or handwritten number or code is a `[File notation]` in
`text` or an entry in `marginal_notes`, never both. A page with nothing in the margins leaves
`marginal_notes` an empty array.

Recall still matters: a page with marginal annotations MUST have them in `marginal_notes`, each one
saying where it was and on which page."""

_MARGINAL_INLINE = """**Marginal notes stay in `text`, where they appear.** Transcribe each annotation in another hand,
or added beside or over the body, as `[Marginal note: the words]` at the point in the text nearest
to it. Do not invent one: a page with nothing in its margins has no marginal note tags."""

_MARGINAL_IGNORE = """**Leave marginal annotations out.** Notes added beside or over the body by another hand are not
transcribed for this archive; transcribe the authored document only."""

_OVERWRITTEN = """**Handwriting written over a typed page is the marginalia that matters most, and the easiest to
miss.** A reply or note written on the document itself — running across the typed lines, filling
the space around the signature, or climbing up the sheet — is often the reason the document was
kept. Before you answer, look at every scan for writing in a different hand, instrument or ink from
the body. When you find it, record it even though it overlaps the typing: transcribe the whole note
as one entry, in reading order, and mark a word you cannot read rather than dropping the note: a
partly legible reply is recorded, never omitted."""


def _marginal(a: dict) -> str:
    placement = a["styles"]["marginal_notes"]
    if placement == "positioned":
        return _MARGINAL_POSITIONED.format(positions=", ".join(f"`{p}`" for p in MARGINAL_POSITIONS))
    return _MARGINAL_INLINE if placement == "inline" else _MARGINAL_IGNORE


# ---------- each field's line in the JSON list ----------


def _names_rule(styles: dict) -> str:
    sep = styles["separator"]
    return {
        "last_first": (f"Write every personal name as `Last, First Middle` (e.g. `\"Hayes, Howard W\"`), "
                       f"inverting names the document writes first-last. Organisations are never "
                       f"inverted. Several names are joined with `\"{sep}\"`, not a bare comma."),
        "first_last": (f"Write every personal name as `First Middle Last` (e.g. `\"Howard W Hayes\"`), in "
                       f"natural order. Several names are joined with `\"{sep}\"`."),
        "as_written": (f"Write each name exactly as the document writes it. Several names are joined "
                       f"with `\"{sep}\"`."),
    }[styles["names"]] + " If only part of a name is legible, give that part; never invent an initial."


def _place_rule(styles: dict) -> str:
    us = {
        "usps": ("Qualify a US place with its USPS two-letter state code when the document establishes "
                 "which state is meant (`\"Miami, FL\"`, `\"Newark, NJ\"`): two capital letters, no periods."),
        "full_state": ("Qualify a US place with its state written in full when the document establishes "
                       "which state is meant (`\"Miami, Florida\"`)."),
        "as_written": "Write US places as the document writes them.",
    }[styles["us_places"]]
    world = {"country": " Give a place outside the United States with its country (`\"Lyon, France\"`).",
             "as_written": " Write places outside the United States as the document writes them."}[
        styles["world_places"]]
    verbatim = ("" if styles["us_places"] == "as_written" and styles["world_places"] == "as_written" else
                " This normalises the index field only: `text` and the other verbatim fields keep what the "
                "page wrote. If the document gives no evidence of which place is meant, write the bare "
                "name — an invented qualifier is worse than none.")
    return us + world + verbatim


def _date_rule(styles: dict) -> str:
    if styles["dates"] == "iso":
        return ("the document's date in ISO form `YYYY-MM-DD` if a full date is legible; `YYYY-MM` or "
                "`YYYY` if only part is; empty string if none is discernible. Do not guess a date that "
                "isn't written or clearly given by the letterhead or postmark.")
    return ("the document's date exactly as the document writes it; empty string if none is "
            "discernible. Do not guess a date that isn't written.")


def _vocabulary_rule(a: dict, key: str, free: str) -> str:
    vocabulary = a["vocabularies"][key]
    if vocabulary["mode"] == "controlled":
        return "exactly one of these terms: " + ", ".join(f"`{t}`" for t in vocabulary["terms"])
    hint = f" Terms this archive already uses: {', '.join(f'`{t}`' for t in vocabulary['terms'])}." \
        if vocabulary["terms"] else ""
    return free + hint


def _field_line(f: dict, a: dict) -> str:
    styles, key = a["styles"], f["key"]
    if key == "marginal_notes":
        text = ("array of marginal notes, each opening with its position and page from the fixed "
                "list above (`[Upper right, Page 2] ...`); empty array if none.")
    elif key == "places":
        text = ("array of distinct real-world places the document is actually about, one normalised entry "
                "each, no fragmented addresses. " + _place_rule(styles) + " Empty array if none.")
    elif key == "names":
        text = ("the document's Mentioned index: every distinct person and organisation named in the "
                "document except its author and recipient, who have fields of their own. "
                + _names_rule(styles) + " List each once, in its fullest form. Empty array if none.")
    elif key == "challenges":
        text = ("array of tags from this fixed list that genuinely apply: "
                + ", ".join(f"`{o}`" for o in f["options"]) + ".")
    elif key == "document_type":
        text = _vocabulary_rule(a, "document_types",
                                "the kind of document, as a short conventional noun phrase such as "
                                "`Letter`, `Telegram`, `Report`, `Memorandum`, `Minutes`, `Clipping`. "
                                "Prefer the term a catalogue would use over a description.")
    elif key == "date":
        text = _date_rule(styles)
    elif key == "author":
        text = ("the document's author or sender. " + _names_rule(styles)
                + " If the document is on an organisation's letterhead, that organisation is an author "
                f"too, after the person (`\"{'Smith, Jane' if styles['names'] == 'last_first' else 'Jane Smith'}"
                f"{styles['separator']}Acme Manufacturing Company\"`); "
                "letterhead with no signature makes it the sole author. Empty string if no author is "
                "identifiable.")
    elif key == "recipient":
        form = {"last_first": "`Last, First Middle`", "first_last": "`First Middle Last`",
                "as_written": "as-written"}[styles["names"]]
        text = (f"the document's recipient or addressee, in the same {form} form and with the same "
                "join as `author`. Empty string if none.")
    elif key == "place":
        text = ("the single place most associated with the document's origin, such as the sender's "
                "location, in the same form as `places`. Empty string if not discernible.")
    elif key == "subjects":
        text = _vocabulary_rule(a, "subjects",
                                "array of subject terms: what the document is about, at the level someone "
                                "browsing the archive would search for, not a summary of its particulars. "
                                "Never a person's name; people are indexed in `names`. Empty array if none.")
        if a["vocabularies"]["subjects"]["mode"] == "controlled":
            text = "array of " + text + "; empty array if none apply."
    else:
        shape = {"string": "text", "list": "array of text entries", "enum": "array of terms from: "
                 + ", ".join(f"`{o}`" for o in f["options"])}[f["type"]]
        if f["type"] == "string" and f["options"]:
            shape = "exactly one of: " + ", ".join(f"`{o}`" for o in f["options"])
        empty = "empty array" if registry_mod.is_list(f) else "empty string"
        text = f"{f['label']} ({shape}). {f['instruction'] or ''} Use an {empty} if the document does not say."
        return f"- `{key}`: " + " ".join(text.split())
    if f["instruction"]:
        text += " " + f["instruction"]
    return f"- `{key}`: {text}"


def _field_list(a: dict, registry: list, scope: str | None = None) -> str:
    marker = UNCERTAIN_MARKERS[a["styles"]["uncertain"]]
    lines = []
    if scope is None:
        lines = ["- `text`: the full transcription (string)" + (", tagged as described above" if
                                                                a["styles"]["structural_tags"] else "")
                 + f". Mark any uncertain word inline as `{marker}`.",
                 "- `uncertain_words`: array of the same uncertain words or phrases listed separately "
                 "(empty array if none)."]
    lines += [_field_line(f, a) for f in registry_mod.enabled(registry)
              if scope is None or f["scope"] == scope]
    return "\n".join(lines)


# ---------- the three prompts ----------


def _document_prompt(a: dict, registry: list) -> str:
    styles = a["styles"]
    marker = UNCERTAIN_MARKERS[styles["uncertain"]]
    guess = " with your best guess" if "word" in marker else ""
    fields_enabled = registry_mod.by_key(registry)
    sections = [_esc(_role(a)) + " " + _esc(_era(a))]
    entities = _entities_section(a)
    if entities:
        sections.append(_esc(entities))
    sections.append(_TRANSCRIPTION_RULES.format(crossouts=_esc(_CROSSOUTS[styles["crossouts"]])))
    sections.append(_esc(_FIDELITY.format(marker=marker, guess=guess)))
    if styles["structural_tags"]:
        sections.append(_esc(_STRUCTURAL))
    if fields_enabled["marginal_notes"]["enabled"] or styles["marginal_notes"] != "positioned":
        sections.append(_esc(_marginal(a)))
    if styles["overwritten_hand"] and styles["marginal_notes"] != "ignore":
        sections.append(_esc(_OVERWRITTEN))
    if a["special_instructions"]:
        sections.append("**This archive's own instructions.**\n\n" + _esc(a["special_instructions"]))
    sections.append("Respond with a JSON object with exactly these fields:\n" + _esc(_field_list(a, registry)))
    return "\n\n".join(sections)


_SPLIT = """You are looking at {{page_count}} scanned page images, in the exact order they were scanned, from an
archive of {material}. Multiple distinct documents may be present in this sequence, scanned
back-to-back with no separator. Your job is only to decide which pages start a new document — do
not transcribe anything.

Signals that a page starts a **new** document (not a continuation of the previous page):
- A new letterhead, printed stationery header, or address block
- A new dateline and/or salutation with no continuation cue
- A different handwriting style, ink, or typewriter face than the immediately preceding page
- A signature block followed by a page that clearly begins a fresh document rather than continuing
- A form, envelope, or other document type entirely distinct from the previous page

Signals a page **continues** the previous document:
- Mid-sentence text continuing from the bottom of the previous page
- Same handwriting/ink/typeface and no new letterhead or dateline
- Page numbering or explicit continuation marks

When genuinely uncertain, prefer treating a page as a continuation rather than inventing a split —
a reviewer will see every boundary you propose and can add a missing one, but a spurious extra
split creates more manual cleanup than a missed one.

Separately, flag **duplicate scans**: the same physical page fed through the scanner twice in a row.
A duplicate is never a document boundary (`starts_new_document` is `false` for it); flag it only if
it is essentially the same page as the one before it, and when uncertain, do not flag it.

Respond with a JSON object with exactly two fields, each an array of exactly {{page_count}}
booleans, one per input page in the exact order given:
- `starts_new_document`: `true` means that page begins a new document; `false` means it continues
  the previous page. The first element must always be `true`.
- `is_duplicate_of_previous_page`: `true` means this page is a duplicate scan of the page
  immediately before it. The first element must always be `false`."""

_MERGE = """Several documents from an archive of {material} that were catalogued separately have turned out to
be **one document**. Their pages have been joined back together in scan order and their
transcriptions concatenated below.

Your job is **only** to describe the joined document as a whole. Do not re-transcribe anything, do
not correct the text, and do not comment on it — the transcription is already settled. A letter's
signature usually falls on its last page and its salutation on its first, so read the whole thing
before answering. If the joined text genuinely does not state something, return the empty value for
it; never invent a value to fill a field.

## Transcription

```
{{document_text}}
```

## Response

Respond with a JSON object with exactly these fields:
{fields}"""


def compile(answers: dict) -> Compiled:
    """Answers in, everything a draft depends on out; CompileError if they cannot be used."""
    a = normalise(answers)
    try:
        registry = registry_mod.build(a["fields"], a["custom_fields"],
                                      {"document_type": a["vocabularies"]["document_types"],
                                       "subjects": a["vocabularies"]["subjects"]})
    except registry_mod.RegistryError as exc:
        raise CompileError(str(exc)) from None
    # The positioned style needs its own field; the inline and ignore styles have none.
    for f in registry:
        if f["key"] == "marginal_notes" and a["styles"]["marginal_notes"] != "positioned":
            f["enabled"] = False
    document = _document_prompt(a, registry)
    material = _esc(a["material"] or "historical documents")
    split = _SPLIT.format(material=material)
    merge = _MERGE.format(material=material, fields=_esc(_field_list(a, registry, scope="document")))
    # Proof that only the documented placeholders survive.
    try:
        document.format(page_count=1, page_break_count=0)
        split.format(page_count=1)
        merge.format(document_text="")
    except (KeyError, IndexError, ValueError) as exc:
        raise CompileError(f"the compiled prompt has a stray placeholder ({exc})") from None
    compiled = Compiled(answers=a, registry=registry, styles=a["styles"], document_prompt=document,
                        split_prompt=split, merge_prompt=merge,
                        document_schema=registry_mod.document_schema(registry),
                        merge_schema=registry_mod.merge_schema(registry))
    compiled.content_hash = content_hash(compiled)
    return compiled


def content_hash(compiled: Compiled) -> str:
    canonical = json.dumps({"answers": compiled.answers, "document": compiled.document_prompt,
                            "split": compiled.split_prompt, "merge": compiled.merge_prompt},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
