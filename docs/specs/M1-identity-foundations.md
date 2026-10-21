# M1 — Identity and schema foundations

## Purpose

A document's identity must never depend on what people call it. Today the Edison-shaped DocID
(`{folder}{AAA}`) is the primary key of `new_documents`, folders are parsed out of it, and bucket
paths are built from it. Two tabs holding a folder with the same name, or two folders holding a
scan with the same filename, would collide; renaming or renumbering a document has to cascade
through eight tables and move files in the bucket.

M1 separates **internal ids** (immutable, opaque, used for every relationship, route and storage
key) from **display ids** (what people type, see and export, unique only within their scope).

## Design

| Thing | Internal id | Display id | Uniqueness of the display id |
|---|---|---|---|
| Tab | `tabs.id` (integer) | `slug`, `label` | slug unique per site |
| Folder | `folders.folder_id` (text, `f` + 16 hex) | `display_id` ("E2002"), `name` | per tab |
| Document | `new_documents.document_id` (text, `d` + 16 hex) | `display_id` ("E2002AAA") | per folder |

- Column names `folder_id` and `document_id` are kept on every table and in every route; they now
  always carry the internal id. Nothing parses a display id to find a folder (`docid.folder_id_of`
  is deleted).
- Every document row carries its `folder_id`; a folder row carries its `tab_id`. One seeded tab
  (`id=1`, slug `documents`, label "Documents", id scheme `letters`) holds every folder until M4.
- Upload takes the folder's **display id**; the server finds or creates the folder in the tab and
  the batch stores the folder's internal id.
- Confirming a split takes each group's **display id**; the server generates the internal id.
- Renaming a document or renumbering a folder changes `display_id` only. No other table changes,
  no file moves. `db.rename_new_document` and `db.renumber_new_documents` are now single-column
  updates (the second in two passes, so a shift onto its own sources never trips the unique
  index); the eight-table cascade and the bucket regroup (`layout.py`) are removed.
  `merge.renumber_folder` is the one place a folder is renumbered, and it rewrites the records of
  the documents it renames.
- Bucket keys: `documents/{folder_id}/{document_id}/{file}`, `records/{folder_id}/{document_id}.json`,
  `uploads/{batch_id}/{file}`, `derived/{size}/...`. Display ids appear only inside records and
  exports.
- Display-id schemes live in `docid.py` as strategies: `letters` (`{folder}AAA`, today's),
  `numbers` (`{folder}-001`), `filename` (the first scan's stem, no renumbering). M1 implements
  the three; the tab chooses one (only `letters` is reachable until M4 exposes the setting).
- Records carry `document_id`, `display_id`, `folder_id`, `folder_display_id`; the CSV's
  "Document ID" column and the export's directory names use display ids, with a `_2` suffix if two
  documents in one folder ever share a filename stem.
- Payloads to the browser carry `display_id` and `folder_display_id` beside the internal ids; the
  UI shows the display ids and routes with the internal ones.

## Acceptance criteria

- AC-1: A folder's internal id and a document's internal id never change, whatever is renamed or renumbered.
- AC-2: Two tabs may each hold a folder with display id "E2002"; two folders may each hold a document with display id "X1AAA" and scans with the same filename; uploads, splits, drafts, comments, reviews, exports and deletes in one never touch the other.
- AC-3: Within one folder a second document with an existing display id is refused (409/400); within one tab a second folder with an existing display id (case-insensitively) is refused.
- AC-4: Renaming a document changes only its `display_id`; its comments, revisions, verdicts, orientations, pages and bucket files are untouched and still found.
- AC-5: Renumbering a folder (gap closing, merge) changes display ids only; no bucket file moves.
- AC-6: No code path derives a folder from a document's display id (`docid.folder_id_of` no longer exists).
- AC-7: Bucket keys for scans and records contain internal ids only.
- AC-8: The export CSV's "Document ID" column, the record JSON's `display_id`, and the export's per-document directory names use display ids.
- AC-9: The three display-id schemes produce `E2002AAA…`, `E2002-001…`, and the scan stem, and renumbering applies only to the `letters` and `numbers` schemes.
- AC-10: Every list and detail payload the browser receives carries `display_id` and `folder_display_id`, and the UI shows them rather than internal ids.
- AC-11: The suite, residue test, `node --check`, and `scripts/smoke_m1.py` (the M0 journey plus two same-named folders in two tabs) pass.

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| Upload names a folder that exists in the tab under different case | "E2002 is already on the site as e2002" | refuses before writing anything | — |
| Confirm/rename gives a display id already in the folder | "That id is already used in this folder" | refuses, nothing changes | — |
| Generated internal id collides with an existing one | nothing | a new document id is drawn until it is unused, before any scan is copied under it; a folder id collision is refused by the primary key before anything is written | — |
| A record write fails after a rename | rename succeeds; record refreshed on next save | logs with document id | — |

## Smoke checklist (`scripts/smoke_m1.py`)

The M0 journey, then: create a second tab directly in the database, upload a folder "SM01" with
the same filenames into it, confirm documents "SM01AAA"/"SM01AAB" there too, rename one, delete
one, export the first tab's folder, and check the first tab's documents, comments and export are
unchanged.

## Deviations from the first draft of this spec

- **Ids are 64 random bits, not 48.** At 48 bits the chance of one collision reaches about 0.2% by
  a million documents; at 64 it is negligible at any size an instance will hold.
- **API field names changed** so a payload says which kind of id it carries: upload takes
  `folder_display_id`, split groups and re-split groups take `display_id`, rename takes
  `new_display_id`, the batch folder route takes `folder_display_id`. Responses gained
  `display_id`, `folder_display_id`, `assigned_display_id`, `display_ids`, `target_display_id`.
- **Backup was simplified in this module, not M6.** The bucket keys no longer change, so the
  backup copies `records/`, `documents/` and `uploads/` at their own keys under `bucket/`, removes
  only what the deletion log names by internal id, and the `backup_refresh` table and the
  folder-labelling copy code are gone. `run_backup` lost `include_images`, which nothing passed.
- **Uploads keep their own directory per batch** (`uploads/{batch_id}/`), so the step-aside
  naming for two uploads of one folder (`split.free_upload_name`, `storage.names_under`) went.
- **No display-id convention is enforced on confirm or rename.** A display id must be a safe
  token and unique in its folder; it no longer has to start with the folder's id, because
  nothing reads the folder out of it. Renumbering still moves only ids that follow the scheme.
- **Known limitation until M4:** the queue and the Team tab list folders from every tab together,
  so two tabs' "SM01" folders appear side by side; the routes and every check keep them apart.

## Redundancy review

- One owner per rule: renumbering and the record rewrite it implies live in
  `merge.renumber_folder`; the three copies in `gaps.close`, the delete route and the re-split
  route are gone. The case-variant folder check lives in `db.folder_spelled_otherwise` (folders
  table) and the upload route calls it before the "folder exists" check, as the original did.
- Deleted: `layout.py`, `docid.folder_id_of` / `parse_archival_folder` / `require_id_in_folder`,
  `storage.folder_of` / `_find_record` / `_document_directories` / `names_under`,
  `split.free_upload_name` / `upload_folder_name`, `export_archive.rename` / `under_folder`,
  `db.tab_id_scheme` / `document_id_exists` / the folder-directory and backup-refresh helpers,
  the `backup_refresh` table, and the tests of all of these.
- Tests: hand-written inserts go through `conftest.insert_rows`, seeding through `seed_folder`,
  `seed_batch`, `seed_document`, and readable ids through `readable_ids` / `next_document_ids`;
  `tests/test_specs_are_covered.py` checks every AC in `docs/specs/` has a test named for it.
- Left for the module that owns it: `activity.report_problem` / `problems()` have no caller since
  M0 removed the existing-document workflow; M5's job table replaces them. Folder listings are
  not yet filtered by tab (M4).

## Results (2026-10-20)

- Suite: 1,714 passed with `-p no:randomly -n auto`, including 30 M1 tests
  (`tests/test_m1_identity.py`) and the spec-coverage check. `pyflakes` over `src`, `scripts` and
  `tests` and `node --check` on every shipped JS file are clean; the residue test passes.
- Smoke: `scripts/smoke_m0.py` 25 of 25; `scripts/smoke_m1.py` 56 of 56 (both journeys, two tabs
  holding "SM01" with the same scan names, a rename and a delete in the second tab, and the first
  tab's documents, comments, scan keys and export unchanged).
- Browser, on the smoke state: folder cards, queue rows, the document header, the Team picker and
  roster show display ids, routes carry internal ids, and no internal id appears in the page. This
  found one bug, fixed and covered by a regression test: the comment panel fetched comments by the
  header's display id and was refused.

## Status

Complete.
