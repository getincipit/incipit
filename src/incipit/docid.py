"""Display ids: what people type and see for folders and documents, never what anything is keyed by.

A folder's and a document's internal ids are opaque (see db.new_folder_id / db.new_document_id).
The display id a document carries is unique only within its folder, and follows one of the schemes
below, chosen per tab:

- ``letters``: ``{folder}{AAA}``, a 3-letter base-26 sequence (AAA, AAB, ..., AAZ, ABA, ...).
- ``numbers``: ``{folder}-001``, a zero-padded decimal sequence.
- ``filename``: the stem of the document's first scan, never renumbered.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from pathlib import PurePath

SCHEMES = ("letters", "numbers", "filename")
DEFAULT_SCHEME = "letters"

SUFFIX_LENGTH = 3
ALPHABET_SIZE = 26
NUMBER_WIDTH = 3

# A display id becomes a URL segment, a filename in an export and a CSV cell, so it stays a plain,
# path- and URL-safe token.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def require_safe_id(value: str, field_name: str = "id") -> str:
    """Returns `value` unchanged if it's a safe id token; raises ValueError otherwise."""
    if not _SAFE_ID.match(value or ""):
        raise ValueError(
            f"{field_name} {value!r} is invalid -- use only letters, digits, hyphens, and "
            "underscores (no spaces, slashes, or dots)"
        )
    return value


def require_scheme(scheme: str) -> str:
    if scheme not in SCHEMES:
        raise ValueError(f"unknown id scheme {scheme!r}; expected one of {SCHEMES}")
    return scheme


def _index_to_suffix(index: int) -> str:
    chars = []
    for _ in range(SUFFIX_LENGTH):
        index, rem = divmod(index, ALPHABET_SIZE)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _sequence_id(folder: str, index: int, scheme: str) -> str:
    if scheme == "numbers":
        return f"{folder}-{index + 1:0{NUMBER_WIDTH}d}"
    return f"{folder}{_index_to_suffix(index)}"


def is_conventional_id(folder: str, display_id: str, scheme: str = DEFAULT_SCHEME) -> bool:
    """True when `display_id` is this folder's own sequence under `scheme`, which is what
    renumbering is allowed to move; a hand-typed id is left alone."""
    if not display_id.startswith(folder):
        return False
    tail = display_id[len(folder):]
    if scheme == "letters":
        return len(tail) == SUFFIX_LENGTH and tail.isalpha() and tail.isupper()
    if scheme == "numbers":
        return len(tail) > 1 and tail[0] == "-" and tail[1:].isdigit()
    return False


def _sequence_index(folder: str, display_id: str, scheme: str) -> int | None:
    """Where a conventional id sits in its folder's sequence, or None for any other id."""
    if not is_conventional_id(folder, display_id, scheme):
        return None
    tail = display_id[len(folder):]
    if scheme == "numbers":
        return int(tail[1:]) - 1
    index = 0
    for char in tail:
        index = index * ALPHABET_SIZE + (ord(char) - ord("A"))
    return index


def filename_id(filename: str) -> str:
    """The `filename` scheme's display id for a scan: its stem, made path-safe."""
    stem = PurePath(filename or "").stem
    return re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "document"


def next_document_ids(folder: str, existing: Collection[str], count: int,
                      scheme: str = DEFAULT_SCHEME, filenames: Sequence[str] = ()) -> list[str]:
    """`count` new display ids for a folder, in order, skipping any already in `existing`.

    For the `filename` scheme, `filenames` gives each new document's first scan; a stem already
    taken gets `_2`, `_3`, ... appended.
    """
    require_scheme(scheme)
    taken = set(existing)
    proposed: list[str] = []
    if scheme == "filename":
        for position in range(count):
            base = filename_id(filenames[position] if position < len(filenames) else "")
            candidate, attempt = base, 2
            while candidate in taken:
                candidate, attempt = f"{base}_{attempt}", attempt + 1
            taken.add(candidate)
            proposed.append(candidate)
        return proposed
    index = 0
    while len(proposed) < count:
        candidate = _sequence_id(folder, index, scheme)
        index += 1
        if candidate in taken:
            continue
        proposed.append(candidate)
    return proposed


def renumber_plan(folder: str, ordered_ids: list[str], immovable: Collection[str] = (),
                  scheme: str = DEFAULT_SCHEME) -> dict[str, str]:
    """`{old_display_id: new_display_id}` for the ids that must move so the folder runs without a
    gap. Ids outside the scheme, and `immovable` ones, keep their place; `filename` never moves."""
    require_scheme(scheme)
    if scheme == "filename":
        return {}
    immovable = set(immovable)
    reserved = {
        index for display_id in ordered_ids
        if display_id in immovable and (index := _sequence_index(folder, display_id, scheme)) is not None
    }
    movable = [display_id for display_id in ordered_ids
               if is_conventional_id(folder, display_id, scheme) and display_id not in immovable]

    plan = {}
    index = 0
    for display_id in movable:
        while index in reserved:
            index += 1
        target = _sequence_id(folder, index, scheme)
        index += 1
        if target != display_id:
            plan[display_id] = target
    return plan


# One path component per folder, everywhere a folder is named on disk: exports and backups.
_UNSAFE_IN_FOLDER = re.compile(r'[\\/:*?"<>|]+')


def folder_directory(folder_id: str, folder_name: str = "") -> str:
    """`FolderID_Group`, the way folders are labelled when they are uploaded."""
    folder_id = (folder_id or "").strip()
    group = _UNSAFE_IN_FOLDER.sub("-", folder_name or "")
    group = "".join(c for c in group if c.isprintable()).strip().rstrip(". ")
    if not group:
        return folder_id[:120]
    if folder_id and (group == folder_id or group.startswith(folder_id + "_")
                      or group.startswith(folder_id + " ")):
        return group[:120]
    if not folder_id:
        return group[:120]
    return f"{folder_id}_{group}"[:120]
