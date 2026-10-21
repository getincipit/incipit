# M3 — Versioned prompt profiles, the field registry and index styles

## Purpose

Every archive is different: its period, its people and places, the kinds of documents it holds, the
fields it catalogues and the conventions it indexes them by. Today all of that is one hard-coded
prompt and a fixed set of database columns. M3 lets an administrator answer a guided questionnaire
and have it compiled into a prompt of the same shape and rigour as the hand-written one, with the
fields, the JSON schema, the review form and the export columns all following from the same answers.

A profile *version* is immutable and fully determines everything a draft depended on, so a
document drafted under version 3 still displays and exports correctly after version 4 renames a
field. Nothing archive-specific is a default: the shipped profile is neutral, with an empty
gazetteer.

## Scope

- `prompt_profiles` and immutable `prompt_profile_versions`; documents pin the version that drafted
  them; a tab names the profile whose *current* version new drafts use.
- `incipit/prompting.py`: `compile(answers) -> Compiled` producing the document, split and merge
  prompts, both JSON schemas, the field registry, the index styles and a content hash.
- `incipit/fields.py`: the field registry (built-in and custom fields), the effective value of a
  field, schema/extractor/merge generation, the review payload's validation, the record's field
  block, export columns and the title template.
- Document fields stored as JSON keyed by registry key: `fields_json` (the model's draft) and
  `reviewed_fields_json` (what a reviewer saved; a key present means the reviewer set it). The
  transcription (`text`, `reviewed_text`, `uncertain_words_json`) and the title columns stay
  columns. The old per-field columns are migrated into the JSON and dropped.
- The review payload becomes `{reviewed_text, title, fields, review_status, editors_notes,
  draft_version}`; the review form is rendered from the document's pinned registry.
- Settings → Prompt: the questionnaire, a live preview of the compiled prompt, a sample test on
  1–3 images (counted against the spend cap), the version list with a diff, and making a version
  current; wizard step "prompt".
- `prompts/*.md` are deleted; their text lives in the compiler as the templates it fills.

## Non-goals

Model backends and per-tab keys (M5; the prompt test uses the interim `OPENROUTER_API_KEY`), tab
CRUD (M4; M3 adds `tabs.prompt_profile_id` and a route to set it for an existing tab), authority
matching against an external list (M10).

## The questionnaire (`answers_json`)

| Section | Answers |
|---|---|
| Collection | `collection` (what the archive is), `material` (the kinds of documents, free text), `period_from`, `period_to` (years, optional), `languages` (list), `region` (free text) |
| Known entities | `people`, `organisations`, `places`: lists of `{name, note}`; a spelling aid, never a source of content |
| Vocabularies | `document_types` and `subjects`: `{mode: free|controlled, terms: [...]}` |
| Index styles | `names` (`last_first` · `first_last` · `as_written`), `us_places` (`usps` · `full_state` · `as_written`), `world_places` (`country` · `as_written`), `dates` (`iso` · `as_written`), `separator` (`"; "` · `" | "`), `uncertain` (`bracket_unclear` → `[unclear: word?]` · `bracket_illegible` → `[illegible]` · `question` → `word[?]`), `structural_tags` (on/off), `marginal_notes` (`positioned` → own field with position and page · `inline` → `[Marginal note: …]` in the text · `ignore`), `crossouts` (`final` · `marked` → `[crossed out: …]`), `overwritten_hand` (on/off) |
| Fields | per built-in field `{enabled, label, instruction, export_column}`; custom fields `{key, label, type: string|list|enum, options, instruction, export_column}` |
| Title | `title_template`, default `[{display_id}], {document_type} from {author} to {recipient}, {date}` |
| Special instructions | free text, appended as the project's own section |

Built-in fields: `text` and `uncertain_words` (always on), `marginal_notes`, `places`, `names`,
`challenges` (enum of the challenge tags), `document_type`, `date`, `author`, `recipient`, `place`,
`subjects`. A custom key is `[a-z][a-z0-9_]{1,31}` and may not shadow a built-in.

## Compilation

Section order follows the hand-written prompt: role line (from collection, material, period) ·
era and language note · known entities with the spelling-aid guardrail (omitted when empty) · the
fixed transcription rules including the page-break rule, with `{page_count}` and
`{page_break_count}` the only live placeholders · the fidelity rule with the chosen uncertain
marker · structural tags when enabled · marginal notes in the chosen placement · the
overwritten-hand paragraph when enabled · crossed-out text · special instructions · the JSON field
list generated from the registry, each field's paragraph written for the chosen index styles and
vocabularies. Every piece of generated or typed text is brace-escaped before it is stored, so
`.format` can only fill the two placeholders. The split prompt opens with the material
description; the merge prompt lists the document-scope fields only. The content hash is SHA-256
over the canonical JSON of the answers and the three compiled prompts.

## Data model

- `prompt_profiles(id, name, current_version_id, created_by, created_at, updated_at)`.
- `prompt_profile_versions(id, profile_id, version_no, answers_json, field_registry_json,
  index_styles_json, compiled_document_prompt, compiled_split_prompt, compiled_merge_prompt,
  document_schema_json, merge_schema_json, content_hash, created_by, created_at,
  UNIQUE(profile_id, version_no))`. Triggers refuse `UPDATE` and `DELETE` on it.
- `prompt_tests(id, version_id, created_by, created_at, image_count, cost_estimate, result_json)`;
  its cost counts toward the spend cap until M5's ledger replaces it.
- `new_documents`: `fields_json`, `reviewed_fields_json`, `prompt_version_id`; the per-field
  columns go. `tabs.prompt_profile_id`.
- `init_db` seeds profile 1, "General correspondence", whose version 1 is compiled from the default
  answers, and points tab 1 at it; documents drafted before M3 are pinned to that version.

## Routes (admin and owner)

| Route | Purpose |
|---|---|
| `GET /api/prompt-profiles/questionnaire` | the questionnaire: sections, options, defaults, built-in fields |
| `GET /api/prompt-profiles`, `POST /api/prompt-profiles` | list; create from `{name, answers}` (version 1) |
| `GET /api/prompt-profiles/{id}` | the profile, its current version's answers and compiled prompts |
| `GET /api/prompt-profiles/{id}/versions`, `POST …/versions` | list; a new version from edited answers, made current |
| `GET /api/prompt-profiles/versions/{version_id}` | one version, compiled |
| `POST /api/prompt-profiles/{id}/current` | make an earlier version current again |
| `POST /api/prompt-profiles/preview` | compile `{answers}` without saving |
| `POST /api/prompt-profiles/{id}/test` | draft 1–3 uploaded images with the current version, nothing stored but the test row |
| `POST /api/tabs/{tab_id}/prompt-profile` | which profile a tab's new drafts use |
| `POST /api/new/folders/{folder_id}/prompt-version` | re-pin a folder's documents to a version without redrafting |

## Acceptance criteria

- AC-1: The default answers compile to a document prompt that carries the page-break rule with exactly one `{page_count}` and one `{page_break_count}`, the fidelity rule, structural tagging, positioned marginal notes and a field list matching the registry, and names no person, place or archive.
- AC-2: Each index style choice changes the compiled prompt as documented (names, US and world places, dates, separator, uncertain marker, structural tags, marginal-note placement, crossed-out text, overwritten hand), and the known-entity section appears only when an entity is given.
- AC-3: Text typed into any answer, including `{x}`, survives `.format` unchanged and cannot add a placeholder.
- AC-4: The content hash changes when any answer changes and not otherwise.
- AC-5: The JSON schema, the extractor, the stored fields, the review form and the export columns all follow the registry: a disabled field is absent from all five and a custom field present in all five.
- AC-6: A profile version cannot be changed or deleted, by route or in the database; editing answers makes a new version.
- AC-7: A new draft records the tab's profile's current version id; a document drafted under an earlier version still renders, reviews and exports with that version's fields after a field is renamed or removed.
- AC-8: The review payload is validated against the document's registry: an unknown field is refused (400), a disabled field is ignored, a list field takes a list, an enum field only its options.
- AC-9: Documents drafted before M3 keep every value. *Deferred to M10 (later work): no installation predates M3, so the column-folding migration was retired when the schema was rebuilt on 2026-10-20; a migration returns if one is ever needed.*
- AC-10: The title follows the profile's title template; names are re-ordered for the title only under `last_first`, and dates written long only under `iso`.
- AC-11: A compile error or an invalid answer refuses the new version and keeps the current one; the preview shows the error.
- AC-12: The prompt test drafts the uploaded sample images with the profile's current version, returns the fields, and its cost counts toward the spend cap; a reviewer cannot run it.
- AC-13: Settings → Prompt and the wizard's prompt step build a profile from the questionnaire with a live preview; reviewers do not see the tab or reach the routes.
- AC-14: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m3.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| Answers that do not compile (bad key, enum without options, unknown style) | "Prompt could not be used: …; the current version is kept" in the preview and on save | refuses the version | fixes the answer |
| A custom field key that clashes with a built-in or another field | the clash, by key | refuses the version | renames it |
| The model rejects the compiled schema (400 on `response_format`) | the prompt test shows the provider's message | nothing stored; current version unchanged | simplifies the field, or waits for M5's `json_object` fallback |
| A document's pinned version is missing (database edited by hand) | the document opens with the default version's fields | logs the document id | restores the version |
| A review saves a field the registry no longer has | 400 naming the field | nothing written | reloads the document |
| Prompt test would exceed the spend cap | "the spend cap has been reached" | refuses before calling the model | raises the cap |
| Migration of old columns meets unreadable JSON | the value is kept as text | logs the document id | — |

## Smoke checklist (`scripts/smoke_m3.py`)

As admin: read the questionnaire; preview a profile for a 1950s civil-rights correspondence
collection with three people, two organisations and four places, `first_last` names, `full_state`
places and a custom `reel` field; check the preview names them and lists `reel`; save it and point
tab 1 at it; run the prompt test on two drawn pages (fake model) and see the fields; upload,
split and draft a folder; the document carries the new version id and a `reel` field; review it
with `reel` set; export, and the CSV has a `Reel` column; make version 2 that removes `reel`; the
old document still shows and exports `reel`; a reviewer is refused every profile route.

## Decisions made while building it

- A redraft replaces the transcription and the **page-scope** fields (places, marginal notes,
  challenges), which the model reads off the page; document-scope fields a reviewer edited are kept.
- The export header is the leading columns, then every column of every version present in the
  folder in registry order, then the trailing ones; "Image Filename(s)" moved to the trailing group.
- A review payload that names an unknown field is refused (400) rather than ignored; an older tab
  sending the retired `mentioned` key is told so.
- The builder's preview shows the prompt as the model reads it (`{{` shown as `{`), and the sample
  title follows the chosen name style.
- A choice field shows only its options; a value outside a controlled list stays selectable on a
  text field so an old document loses nothing.

## Redundancy review

- One owner per rule: the field registry (`fields.py`) is the only place that knows a field's type,
  label, export column and scope; the schema, extractor, chunk merge, merge pooling, review
  validation, form, export and title all read it. Prompt text exists only as compiled, stored
  versions (`profiles.compiled`), never recompiled; `prompts/*.md`, `PROMPTS_DIR`, the fixed
  schemas, the per-field columns and `build_document_title` are deleted.
- One source per fact: a document's values are its two JSON columns; titles are derived by
  `new_document.document_title` and cached only for the queue's SQL, re-derived on rename and
  re-pin (`retitle`). History labels come from the registry at edit time (`text_revisions.label`).
- Consolidated: three order-keeping dedupes became `fields.dedupe`; the single-use
  `review_server._effective` / `_effective_list` went; tests share `conftest.seed_draft`,
  `review_payload` and `draft_call` rather than each writing the old shapes.
- Found and fixed in review: `profiles.default_version_id` ran the schema script on every lookup,
  and `executescript` commits an open transaction, which would have broken the review save's
  `BEGIN IMMEDIATE`; it is now a plain read.
- Left for the module that owns it: the per-tab profile picker lives in Settings → Prompt until
  M4's Tabs settings; the prompt test's cost sits in `prompt_tests` until M5's ledger.

## Results (2026-10-20)

- Suite: all green with `-p no:randomly -n auto`, including 66 M3 spec tests
  (`tests/test_m3_prompt_profiles.py`) and the spec-coverage check across M0–M3. `pyflakes` over
  `src`, `scripts` and `tests`, and `node --check` on every script in `app/static`, are clean.
- Smoke: `scripts/smoke_m3.py` 26 of 26; `smoke_m0`, `smoke_m1` and `smoke_m2` still pass.
- Browser, on a throwaway site: the wizard's prompt step compiled live as answers changed (a `{x}`
  in the collection reached the preview as typed), a saved version 2 compared line by line with
  version 1, the review form was drawn from the document's registry (inputs, list blocks, choice
  chips) and an edited author and a toggled chip saved and reloaded, with no console errors. At a
  phone-width pane the builder fell to one column and nothing clipped. This found three bugs, all
  fixed: the wizard's mount point shared the preview's attribute name, the preview showed the
  stored brace escapes, and the sample title ignored the name style.

## Status

Complete.
