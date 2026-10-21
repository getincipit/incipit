# M4 — Data-driven tabs

## Purpose

A site has had one tab, "Documents", seeded by M0 and reachable only as tab 1. M4 lets an
administrator create, rename, reorder, pause and (while empty) delete upload tabs, each with its own
display-id scheme and prompt profile, so one organisation can keep its correspondence, its ledgers
and its photographs apart. The Team tab stays fixed, last and admin-only.

## Scope

- `incipit/tabs.py`: the rules (slugs, reserved words, what may change when) and the queries.
- `incipit/tab_routes.py`: `GET/POST /api/tabs`, `PUT /api/tabs/{id}`, `POST /api/tabs/reorder`,
  `DELETE /api/tabs/{id}`; `POST /api/tabs/{id}/prompt-profile` moves here from M3.
- Every listing narrows to one tab: folders, documents, batches, the pending count, the Team tab's
  folder list; an upload names the tab it lands in. A folder is reached through its tab
  (`folders.tab_id`); the shared tables keep their `workflow` column, which names the document
  kind (`new`) and is not the tab.
- A paused (disabled) tab: hidden from reviewers' tab list, its folders still open by direct link,
  but nothing in it can be uploaded, drafted or saved until it is resumed.
- Exports are published under the tab's slug: `exports/{tab_slug}/{folder}/`.
- The UI's tab strip is built from `/api/tabs`; `state.activeTab` replaces `activeWorkflow`; the
  hash is `#{slug}/{folder}/{document}`; Settings → Tabs (`tabs_admin.js`) and the wizard's "tabs"
  step manage tabs, including which prompt profile each uses (removed from Settings → Prompt).

## Non-goals

The `iiif` tab kind (M10); per-tab roles or assignments beyond the folder's; moving a folder
between tabs; renaming a slug.

## Data model

`tabs` (M1) is unchanged: `id, slug UNIQUE, label, kind, id_scheme, position, enabled,
prompt_profile_id, created_at`. A slug is made once from the label (`[a-z0-9-]{2,32}`, lower-case,
runs of other characters become one hyphen, a clash gets `-2`, `-3`, …), never from a reserved word
(`team setup api static site login logout signup health admin hub new`), and never changes, so
links and export paths survive a rename. `id_scheme` may change only while the tab has no folder.

## Routes and roles

| Route | Owner | Admin | Reviewer |
|---|---|---|---|
| `GET /api/tabs` | all tabs, every field | all tabs, every field | enabled tabs: `id, slug, label, kind, id_scheme` |
| `POST /api/tabs` `{label, id_scheme?, prompt_profile_id?}` | yes | yes | 403 |
| `PUT /api/tabs/{id}` `{label?, enabled?, id_scheme?, prompt_profile_id?}` | yes | yes | 403 |
| `POST /api/tabs/reorder` `{order: [id, …]}` | yes | yes | 403 |
| `DELETE /api/tabs/{id}` | yes, when empty | yes, when empty | 403 |
| `GET /api/new/folders?tab_id=`, `/documents?tab_id=`, `/batches?tab_id=`, `/pending-count?tab_id=` | scoped | scoped | scoped and filtered as before |
| `POST /api/new/batches` with `tab_id` | yes | yes | 403 (as before) |

## Acceptance criteria

- AC-1: A tab's slug is made once from its label (`[a-z0-9-]{2,32}`), a reserved word or a clash is avoided, and renaming the tab keeps the slug.
- AC-2: `GET /api/tabs` lists tabs in position order; a reviewer receives only enabled tabs and none of the administrative fields.
- AC-3: An admin creates, renames, pauses, resumes and reorders tabs; a reviewer is refused every write (403).
- AC-4: A tab's id scheme can be changed until its first folder exists and is refused (400) afterwards; a new tab's first folder follows the chosen scheme.
- AC-5: A tab with a folder or a batch cannot be deleted (409); an empty one can, and the Team tab cannot be made or deleted through the routes.
- AC-6: Folders, documents, batches, the pending count and the Team tab's folder list each narrow to the tab asked for; a folder in another tab is absent from them.
- AC-7: An upload names its tab; two tabs each holding a folder "E2002" show it only in their own queue, and a draft in each uses that tab's prompt profile.
- AC-8: A paused tab vanishes from a reviewer's tab list; a folder in it still opens by direct link, and an upload, a draft or a review save in it is refused with a message that names the tab (409).
- AC-9: The tab strip is drawn from `/api/tabs` with Team last and admin-only, the browser hash is `#{slug}/…`, and a pasted link to a tab, a folder or a document opens it.
- AC-10: Settings → Tabs and the wizard's tabs step manage tabs and their prompt profile; Settings → Prompt no longer offers a tab picker.
- AC-11: A folder's export is published under its tab's slug, and two tabs' same-named folders export to different directories.
- AC-12: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m4.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| A label that yields no usable slug (empty, or only punctuation) | "a tab needs a name with a letter or digit in it" | refuses the tab | types a name |
| A label whose slug is reserved or taken | the slug gets a numeric suffix, shown in the reply | creates it under the suffixed slug | renames if the suffix is unwanted |
| Changing the id scheme of a tab that has folders | "this tab already has folders; its id scheme is fixed" | refuses (400) | makes a new tab |
| Deleting a tab that has folders or batches | "move or delete its N folders first" | refuses (409) | empties it |
| Tab disabled while a reviewer works in it | the tab leaves the strip; an open folder shows "This tab is paused" and saves are refused | 409 on writes, reads unchanged | resumes the tab |
| An upload posted to a disabled or unknown tab | "that tab is paused" / "no such tab" | refuses (409 / 404) before storing anything | picks a tab |
| A hash naming a slug that no longer exists or is paused for this reader | the first tab opens | ignores the route | — |
| Reorder lists an id that is not a tab | 400 naming it | leaves the order as it was | retries |

## Smoke checklist (`scripts/smoke_m4.py`)

As admin: list the one tab; create "Ledgers" with the `numbers` scheme and "Photographs"; rename
"Documents" to "Correspondence" and see its slug stay `documents`; reorder; upload a folder "E2002"
into Documents and another "E2002" into Ledgers; see each tab's folder list hold one; see the
ledger's documents numbered `E2002-001`; pause Photographs; as reviewer see two tabs and be refused
a save in the paused tab's folder (seeded by the admin before pausing); delete Photographs after
emptying it, refused while it holds a batch; export a folder from each tab and find them under
`exports/documents/` and `exports/ledgers/`; a reviewer is refused every write route.

## Decisions made while building it

- `state.activeWorkflow` stays in `app.js` as the document kind the API routes name (`/api/new/…`);
  the tab is `state.activeTab`. The plan's "replace activeWorkflow" assumed the workflow would
  vanish; it names a kind of document, not a section of the site.
- Exports are grouped by the tab's **slug**, not its id: the slug is as fixed and far easier for
  an operator to read in the tree. `export_archive.forget` now removes a folder's archives under
  every group, since a folder id is unique across tabs.
- A locked id scheme answers 400 (a request that cannot be made right by retrying), a held tab's
  deletion 409 (true until the tab is emptied).
- M0's AC-5 tests, which pinned the single static tab and the `new`-only hash, are re-pinned to
  the tab list: the shell carries no static tab and the router accepts the slugs it was given.
- A batch carries `tab_id` from upload, so an upload that has not been given a folder name yet
  still belongs to the tab it was made in, and naming it later creates the folder there.

## Redundancy review

- One owner per rule: `tabs.py` is the only place that knows a slug's shape, the reserved words,
  when an id scheme may change and when a tab may go; `tabs.require_open` / `require_folder_open`
  are the one paused-tab rule, called from the review server's guards. The per-tab profile choice
  moved from `profiles.set_tab_profile` (deleted) and the prompt builder to `tabs.update` and
  Settings → Tabs.
- One source per fact: a folder's tab is `folders.tab_id`, read through `db.folder_labels`,
  `get_new_document_folders` and `tabs.of_folder`; nothing stores a tab's folder count. The strip,
  the router's slugs and the wizard all read `GET /api/tabs`.
- Consolidated: the three smokes that knew the export path now find a folder's export by its id
  under any group; the Team list's optgroups and the auto-switch key on the folder's tab rather
  than a `WORKFLOW_NAMES` table (kept only for payloads that predate tabs).
- Left for the module that owns it: moving a folder between tabs and the `iiif` kind (M10);
  per-tab limits (M8).

## Results (2026-10-20)

- Suite: all green with `-p no:randomly -n auto`, including 35 M4 spec tests
  (`tests/test_m4_data_driven_tabs.py`) and the spec-coverage check across M0–M4. `pyflakes` over
  `src`, `scripts` and `tests`, `compileall`, and `node --check` on every script in `app/static`
  are clean.
- Smoke: `scripts/smoke_m4.py` 24 of 24; `smoke_m0` 27, `smoke_m1` 56, `smoke_m2` 51 and
  `smoke_m3` 26 still pass.
- Browser, on a throwaway site: the strip was drawn from `/api/tabs` with Team last and the hash
  `#documents`; Settings → Tabs added "Ledgers" (numbered ids), the strip grew at once, and
  clicking it routed to `#ledgers` with an empty folder grid; at phone width the panel's rows
  stacked with nothing clipped. No script errors in the console. The sign-in and setup pages now
  take the site's own favicon and mark like the shell, and `/favicon.ico` is sent to it.

## Status

Complete.
