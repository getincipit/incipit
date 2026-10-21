"""The field registry: which catalogue fields a profile version drafts, reviews and exports, and how.

A registry is a list of field definitions, in order, built from a profile's answers and stored with
each version. Every place that reads or writes a document's fields (the JSON schema, the model
result, the stored row, the review form and payload, the record, the CSV and the title) asks this
module, so a field exists in all of them or in none.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

# The transcription itself; always drafted, never configurable, stored in columns of its own.
CORE_FIELDS = ("text", "uncertain_words")

CHALLENGE_TAGS = ("handwriting", "typed", "marginal_note", "faded", "crossout", "insertion",
                  "table", "long_document")

TYPES = ("string", "list", "enum")
# page: read off each scan and pooled across a long document's chunks; document: the whole thing.
SCOPES = ("page", "document")
CUSTOM_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# The built-in fields, in the order the form, the prompt's field list and the export use them.
BUILTINS = (
    {"key": "document_type", "label": "Document type", "type": "string", "scope": "document",
     "export_column": "Document Type"},
    {"key": "date", "label": "Date", "type": "string", "scope": "document", "export_column": "Date"},
    {"key": "author", "label": "Author", "type": "string", "scope": "document",
     "export_column": "Author(s)"},
    {"key": "recipient", "label": "Recipient", "type": "string", "scope": "document",
     "export_column": "Recipient(s)"},
    {"key": "place", "label": "Place", "type": "string", "scope": "document", "export_column": ""},
    {"key": "names", "label": "Names mentioned", "type": "list", "scope": "document",
     "export_column": "Name(s) Mentioned"},
    {"key": "subjects", "label": "Subjects", "type": "list", "scope": "document",
     "export_column": "Subjects"},
    {"key": "places", "label": "Places mentioned", "type": "list", "scope": "page",
     "export_column": "Places"},
    {"key": "marginal_notes", "label": "Marginal notes", "type": "list", "scope": "page",
     "export_column": ""},
    {"key": "challenges", "label": "Challenges", "type": "enum", "scope": "page",
     "options": list(CHALLENGE_TAGS), "export_column": ""},
)
BUILTIN_KEYS = tuple(f["key"] for f in BUILTINS)

# Columns every export has, around the registry's own.
LEADING_COLUMNS = ("Document ID", "Title", "Transcription")
TRAILING_COLUMNS = ("Image Filename(s)", "Editors Notes", "Submitted by")

DEFAULT_TITLE_TEMPLATE = "[{display_id}], {document_type} from {author} to {recipient}, {date}"


class RegistryError(ValueError):
    """A field definition the person can see and fix."""


# ---------- building a registry ----------


def _field(base: dict, override: dict) -> dict:
    field = {"key": base["key"], "label": base["label"], "type": base["type"],
             "scope": base.get("scope", "document"), "enabled": True, "instruction": "",
             "export_column": base.get("export_column", ""), "options": list(base.get("options", [])),
             "builtin": base.get("builtin", True)}
    for name in ("label", "instruction", "export_column"):
        if name in override:
            field[name] = " ".join(str(override[name] or "").split()) if name != "instruction" \
                else str(override[name] or "").strip()
    if "enabled" in override:
        field["enabled"] = bool(override["enabled"])
    return field


def build(field_answers: dict | None, custom: list | None, vocabularies: dict | None = None) -> list[dict]:
    """The registry for a set of answers: the built-ins as configured, then the custom fields."""
    field_answers = field_answers or {}
    registry = []
    for base in BUILTINS:
        override = field_answers.get(base["key"]) or {}
        if not isinstance(override, dict):
            raise RegistryError(f"the settings for {base['key']} must be an object")
        registry.append(_field(base, override))
    taken = set(BUILTIN_KEYS) | set(CORE_FIELDS)
    for given in custom or []:
        if not isinstance(given, dict):
            raise RegistryError("each custom field must be an object")
        key = str(given.get("key", "")).strip()
        if not CUSTOM_KEY.match(key):
            raise RegistryError(f"a field key is 2-32 lower-case letters, digits or _, starting with a "
                                f"letter: {key!r}")
        if key in taken:
            raise RegistryError(f"the field key {key!r} is already used")
        taken.add(key)
        kind = given.get("type", "string")
        if kind not in TYPES:
            raise RegistryError(f"{key}: a field's type is one of {', '.join(TYPES)}")
        options = [str(o).strip() for o in given.get("options") or [] if str(o).strip()]
        if kind == "enum" and not options:
            raise RegistryError(f"{key}: a choice field needs its options")
        if not str(given.get("label", "")).strip():
            raise RegistryError(f"{key}: a field needs a label")
        registry.append(_field({"key": key, "label": given["label"], "type": kind,
                                "scope": given.get("scope", "document") if given.get("scope") in SCOPES
                                else "document",
                                "export_column": given.get("export_column", given["label"]),
                                "options": options, "builtin": False},
                               {k: given[k] for k in ("instruction", "enabled") if k in given}))
    # A controlled vocabulary is the field's options; free text leaves it open.
    for key, vocabulary in (vocabularies or {}).items():
        if isinstance(vocabulary, dict) and vocabulary.get("mode") == "controlled":
            terms = [str(t).strip() for t in vocabulary.get("terms") or [] if str(t).strip()]
            if not terms:
                raise RegistryError(f"a controlled {key.replace('_', ' ')} vocabulary needs its terms")
            for field in registry:
                if field["key"] == key:
                    field["options"] = terms
    return registry


def enabled(registry: list[dict]) -> list[dict]:
    return [f for f in registry if f["enabled"]]


def by_key(registry: list[dict]) -> dict:
    return {f["key"]: f for f in registry}


def is_list(field: dict) -> bool:
    return field["type"] in ("list", "enum")


def empty(field: dict):
    return [] if is_list(field) else ""


# ---------- the model's side ----------


def _schema_for(field: dict) -> dict:
    if field["type"] == "string":
        return ({"type": "string", "enum": [""] + field["options"]} if field["options"]
                else {"type": "string"})
    items = {"type": "string", "enum": field["options"]} if field["options"] else {"type": "string"}
    return {"type": "array", "items": items}


def document_schema(registry: list[dict]) -> dict:
    """The strict JSON schema a draft answers with: the transcription, then each enabled field."""
    properties = {"text": {"type": "string"},
                  "uncertain_words": {"type": "array", "items": {"type": "string"}}}
    properties.update({f["key"]: _schema_for(f) for f in enabled(registry)})
    return {"type": "json_schema", "json_schema": {
        "name": "new_document", "strict": True,
        "schema": {"type": "object", "properties": properties, "required": list(properties),
                   "additionalProperties": False}}}


def merge_schema(registry: list[dict]) -> dict:
    """What a merge asks for: the document-scope fields only, since the text is settled."""
    properties = {f["key"]: _schema_for(f) for f in enabled(registry) if f["scope"] == "document"}
    return {"type": "json_schema", "json_schema": {
        "name": "merged_metadata", "strict": True,
        "schema": {"type": "object", "properties": properties, "required": list(properties),
                   "additionalProperties": False}}}


def clean(field: dict, value):
    """A model's or a person's value in the field's own shape; anything else becomes empty."""
    if is_list(field):
        values = value if isinstance(value, list) else []
        values = [str(v).strip() for v in values if isinstance(v, (str, int, float)) and str(v).strip()]
        if field["options"]:
            values = [v for v in values if v in field["options"]]
        return values
    text = value.strip() if isinstance(value, str) else ""
    if field["options"] and text not in field["options"]:
        return ""
    return text


def extract(registry: list[dict], parsed: dict) -> dict:
    """The fields of one model answer, each cleaned to its shape."""
    return {f["key"]: clean(f, parsed.get(f["key"])) for f in enabled(registry)}


def dedupe(values: list) -> list:
    """Order kept, blanks dropped, and two entries differing only in case or outer spaces are one."""
    seen: set = set()
    out: list = []
    for value in values:
        key = value.strip().casefold() if isinstance(value, str) else repr(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip() if isinstance(value, str) else value)
    return out


def merge_chunks(registry: list[dict], chunks: list[dict]) -> dict:
    """A long document drafted in chunks: page fields pooled, document fields from the first chunk
    that has them."""
    out = {}
    for field in enabled(registry):
        values = [chunk.get(field["key"], empty(field)) for chunk in chunks]
        if is_list(field):
            out[field["key"]] = dedupe([v for value in values for v in value])
        else:
            out[field["key"]] = next((v for v in values if v), "")
    return out


# ---------- the stored document ----------


def load(raw) -> dict:
    """One of the JSON field columns, as a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def drafted(row) -> dict:
    return load(row["fields_json"] if "fields_json" in row.keys() else None)


def reviewed(row) -> dict:
    return load(row["reviewed_fields_json"] if "reviewed_fields_json" in row.keys() else None)


def effective(row, registry: list[dict]) -> dict:
    """{key: value} for every field the registry knows: the reviewer's once saved, else the draft."""
    draft, review = drafted(row), reviewed(row)
    out = {}
    for field in registry:
        key = field["key"]
        out[key] = clean(field, review[key]) if key in review else clean(field, draft.get(key))
    return out


def joined(field: dict, value, separator: str = "; ") -> str:
    return separator.join(value) if is_list(field) else (value or "")


# ---------- the person's side ----------


class PayloadError(ValueError):
    """A review payload that does not fit the document's registry."""


def validate_review(registry: list[dict], given: dict) -> dict:
    """The fields a review may save: unknown keys refused, disabled ones dropped, shapes checked."""
    if not isinstance(given, dict):
        raise PayloadError("fields must be an object")
    known = by_key(registry)
    unknown = sorted(k for k in given if k not in known)
    if unknown:
        raise PayloadError(f"this document has no field {', '.join(unknown)}")
    out = {}
    for key, value in given.items():
        field = known[key]
        if not field["enabled"]:
            continue
        if is_list(field):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise PayloadError(f"{field['label']} takes a list of text")
            if field["options"] and any(v.strip() not in field["options"] for v in value if v.strip()):
                raise PayloadError(f"{field['label']} takes only: {', '.join(field['options'])}")
        elif not isinstance(value, str):
            raise PayloadError(f"{field['label']} takes text")
        out[key] = clean(field, value)
    return out


# ---------- exports and titles ----------


def export_columns(registry: list[dict]) -> list[str]:
    return list(LEADING_COLUMNS) + [f["export_column"] for f in enabled(registry) if f["export_column"]] \
        + list(TRAILING_COLUMNS)


def export_values(registry: list[dict], values: dict, separator: str = "; ") -> list[str]:
    return [joined(f, values.get(f["key"], empty(f)), separator) for f in enabled(registry)
            if f["export_column"]]


_ORDINAL = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    return f"{n}th" if 11 <= n % 100 <= 13 else f"{n}{_ORDINAL.get(n % 10, 'th')}"


def long_date(iso: str) -> str:
    """"1901-03-20" -> "March 20th, 1901"; anything else stays as written."""
    try:
        parsed = datetime.strptime(iso, "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso or ""
    return f"{parsed.strftime('%B')} {_ordinal(parsed.day)}, {parsed.year}"


def first_last(names: str, separator: str = "; ") -> str:
    """"Hayes, Howard W; Lovell, Ada" -> "Howard W Hayes, Ada Lovell", the way titles read."""
    out = []
    for part in (p.strip() for p in (names or "").split(separator.strip() or ";")):
        if not part:
            continue
        last, comma, rest = part.partition(",")
        out.append(f"{rest.strip()} {last.strip()}".strip() if comma else part)
    return ", ".join(out)


_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render_title(template: str, display_id: str, values: dict, styles: dict, registry: list[dict]) -> str:
    """The derived title. A clause whose field is empty is dropped with the words that introduce it,
    so a letter with no recipient does not read "to ,"; nothing filled at all reads "Untitled"."""
    separator = styles.get("separator", "; ")
    known = by_key(registry)

    def value_of(key: str) -> str:
        if key == "display_id":
            return display_id
        field = known.get(key)
        if field is None:
            return ""
        value = values.get(key, empty(field))
        if is_list(field):
            return ", ".join(value)
        if key in ("author", "recipient") and styles.get("names") == "last_first":
            return first_last(value, separator)
        if key == "date" and styles.get("dates") == "iso":
            return long_date(value)
        return value or ""

    keys = [k for k in _PLACEHOLDER.findall(template) if k != "display_id"]
    if not any(value_of(k) for k in keys):
        return f"[{display_id}], Untitled" if "{display_id}" in template else "Untitled"
    # Each placeholder takes the text before it (since the previous one) as its lead-in.
    pieces, last, filled = [], 0, True
    for match in _PLACEHOLDER.finditer(template):
        lead, value = template[last:match.start()], value_of(match.group(1))
        if match.group(1) == "document_type" and not value:
            value = "Document"
        filled = bool(value)
        if value:
            pieces.append(lead + value)
        elif not pieces:
            pieces.append(lead)
        last = match.end()
    # The text after the last placeholder closes its clause, so it goes when that clause does.
    return "".join(pieces) + (template[last:] if filled else "")


def describe(registry: list[dict]) -> list[dict]:
    """The registry as the review form wants it."""
    return [{"key": f["key"], "label": f["label"], "type": f["type"], "options": f["options"],
             "scope": f["scope"]} for f in enabled(registry)]
