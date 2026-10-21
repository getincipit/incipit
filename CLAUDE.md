# Incipit

Transcription, indexing and review workbench. Scans go in as a folder; the model proposes where
each document starts, drafts the transcription and the catalogue fields; people review; the folder
goes out as a CSV, JSON records and images. Forked from an archive-specific pipeline on 2026-10-19
with a fresh history; nothing from that project's data, names or hosts belongs here (the residue
test `tests/test_fork_residue.py` enforces it).

Work proceeds module by module; the plan and each module's spec live in `docs/specs/`. Every
module ships a spec with acceptance criteria, spec tests, a smoke script, a redundancy review and a
failure table. Never commit or push unless asked.

## Commands

```bash
.venv/Scripts/python.exe -m pytest -q -p no:randomly -n auto      # the suite (~1700 tests, ~1 min)
.venv/Scripts/python.exe -m pytest tests/test_collab.py -q -p no:randomly
.venv/Scripts/python.exe -m incipit.cli --help                    # init-folder / serve / tray / init / create-owner / review / export
.venv/Scripts/python.exe scripts/smoke_m2.py                      # setup, accounts, branding, then the M0 journey
.venv/Scripts/python.exe scripts/build_icons.py                   # the app icon set from favicon.svg
.venv/Scripts/python.exe scripts/build_app.py                     # the packaged app in dist/Incipit (needs .[build])
```

A client's install is one **data folder**: `incipit init-folder <path>` makes it (`incipit.toml`,
`incipit.db`, `bucket/`, `exports/`, `backups/`, `logs/`) and `incipit serve --data-folder <path>`
runs the site from it; `incipit tray` does the same from the system tray, which is what the
installer (`installer/incipit.iss`, Inno Setup) starts. The file decides the port and every path;
an environment variable (`INCIPIT_PORT`, `INCIPIT_BACKUP_DIR`, …) overrides one value for development.
The model service's key is entered on Settings → Models and stored encrypted in the database
(`keyvault.py`, `backends.py`); a key found in `incipit.toml` or `OPENROUTER_API_KEY` at start is
moved there. Every call is recorded in the insert-only ledger (`ledger.py`), which spend and the
caps read; drafting and split proposals run as jobs (`jobs.py`). Backups go where Settings → Backups
says, each snapshot with a manifest of the files it needs; `incipit restore --from <backups>
<new folder>` (or Settings → Backups) rebuilds a data folder from one, never over the running site.
New versions come from a release feed baked into the build (`UPDATE_FEED`, or `INCIPIT_UPDATE_FEED`);
the owner installs from Settings → Updates or the tray, and the installer's `/UPDATE` mode replaces
only the program. A release is published with `scripts/release.py --notes "..."` (bump `VERSION`,
commit and push first): it builds with the GitHub feed baked in and creates the GitHub release.
`docs/OPERATIONS.md` is the client administrator's guide. The checkout's own
commands below still take explicit paths.

A new site is created with `incipit init --db data/incipit.db` (add `--owner NAME`, with the
password in `INCIPIT_OWNER_PASSWORD_FILE` or typed, to skip the wizard; without it a one-time
`/setup?token=…` link is printed). `review` refuses to start a site with no owner and no token.
The review server (the same arguments the desktop preview uses, from `.claude/launch.json`):

```bash
.venv/Scripts/python.exe -m incipit.cli review --db data/incipit.db --bucket-uri file://./data/bucket --host 127.0.0.1 --port 8011
```

`-p no:randomly` matters: the suite is order-independent by design, and pinning the order makes
a failure reproducible.

## Layout

- `src/incipit/` — the application. `review_server.py` is the API, `collab.py` the review
  lifecycle, `scoping.py` what each role may see, `db.py` every query, `storage.py` the bucket,
  `docid.py` the display-id schemes, `tabs.py` the tabs (and `tab_routes.py` their routes).
- `app/static/` — the UI. `app.js` is the queue and document screens, `workspace.js` the Team tab
  and the account menu, `history.js` Back/Forward, `index.html` the shell, `style.css` all of it.
- Prompts are not files: `prompting.compile(answers)` builds them from a profile's questionnaire
  answers, and `profiles.py` stores each result as an immutable version. `tests/test_prompts.py`
  holds the compiled default to the rules every project needs.
- `data/bucket/` — `documents/{folder_id}/{document_id}/` (confirmed scans), `uploads/{batch_id}/`
  (raw batches), `records/{folder_id}/{document_id}.json`, `derived/` (cached renderings). Keys hold
  internal ids only, so nothing in the bucket moves when something is renamed. Nothing under
  `data/` is tracked.
- `tests/` — one file per area, named for the behaviour. `conftest.make_app` and `signed_in` are
  the one way to build an app and sign in.

## Identity

Every tab, folder and document has an **internal id** (`tabs.id`, `folder_id` = `f` + 16 hex,
`document_id` = `d` + 16 hex) that never changes and is used for every relationship, route and
bucket key. What people type and see is the **display id** (`folders.display_id` "E2002",
`new_documents.display_id` "E2002AAA"), unique per tab and per folder respectively. Renaming and
renumbering change `display_id` only. Nothing derives a folder from a display id; payloads carry
`display_id` and `folder_display_id` beside the internal ids, and the UI shows the display ids.
Tests that want readable ids use the `readable_ids` fixture or the `seed_*` helpers in conftest.

## Tabs

A tab is a row of `tabs`; every folder carries its `tab_id`, and a batch carries the tab it was
uploaded into before it has a folder. The shared tables' `workflow` column names the document kind
(`new`) and is not the tab. Listings take `?tab_id=`; the strip, the hash (`#{slug}/…`) and
Settings → Tabs are drawn from `GET /api/tabs`, which a reviewer may read (enabled tabs, public
fields) while every other `/api/tabs` verb is admin-only. A slug is made once from the label and
never changes; exports are published under it. A paused tab refuses uploads, drafts and saves
(409) but still reads. The Team tab is not a row: `workspace.js` appends it last.

## Fields and prompt versions

A document's catalogue fields live in `fields_json` (the model's draft) and `reviewed_fields_json`
(what a reviewer saved; a key present means they set it), keyed by the field registry of the prompt
version the document is pinned to (`prompt_version_id`). Read them with `db.field_values` or
`fields.effective`, never by column. The registry also generates the JSON schema, the review form
(`review_fields.js`), the export columns and the title, so a field is added in one place: the
questionnaire's answers. A version is never edited (triggers refuse it); a change is a new version,
and old documents keep reading their own.

## How the review lifecycle works

Roles are `owner` (exactly one, enforced by a unique index), `admin` and `reviewer`; what people
read for each comes from `site_settings`, through `term()` in `app/static/terms.js`, never a literal
word. `accounts.is_admin` is true for owner and admin, and the owner reviews on the admin track.
Who may do what to whom is `accounts.require_may_create` / `require_may_manage`; the matrix is in
`docs/specs/M2-accounts-roles-and-site.md`.

A folder is assigned to a reviewer, who accepts each document and submits it; an admin reviews and
either approves it or sends it back. Two rules underpin most of the code:

1. **Verdicts are per role.** `new_documents.review_status` is the admin's verdict; export and the
   approval gate read it. A reviewer's verdict lives in `document_reviews`. Neither overwrites the other.
2. **Progress is computed, never stored.** `collab.folder_progress_map` counts the documents on
   every request, per role.

Sending a folder back lets the admin pick the documents that need work
(`collab.flag_documents_for_rework`): those are flagged, the reviewer's own accept on them is
withdrawn, and the folder cannot come back until the reviewer has looked at each one again.

## What a reviewer must never receive

Enforced in `scoping.strip_admin_fields` and covered by `tests/test_reviewer_admin_boundary.py`:
the admin's `accepted_count` / `all_approved`, export state, folder claims, and cost. Hiding a
field in the page is not enough; it must not be in the payload.

## Conventions

- Comments say **why**, briefly, one or two lines.
- Tests are named as sentences about behaviour and each asserts one thing; a spec test carries its
  acceptance-criterion id in its name.
- Schema changes go in the `CREATE TABLE IF NOT EXISTS` block *and* `db._migrate()`.
- Scripts that run app code in node (tests' harnesses) prepend `conftest.terms_js()` for `term()`.
