# Architecture and ownership

Every production file has one primary reason to change. When a change crosses rows in this table,
keep orchestration at the caller and domain rules at the owner; do not copy a rule into both files.

## Application boundaries

| Owner | Responsibility | Must not own |
|---|---|---|
| `cli.py` | Parse command-line input and compose commands | Pipeline rules or SQL |
| `review_server.py` | Compose the web application and the review workflow routes | Authentication policy, operational endpoints, asset discovery |
| `webauth.py` | The gate (public paths, admin capabilities, forced password change, first-run redirect) and sign-in, sign-up and account-self-service routes | Password/session persistence or generic browser security |
| `people_routes.py` | The Team tab's people and join-code routes | Who may do what to whom (`accounts`) |
| `site_routes.py` | Site settings, branding files, `/site.css`, the shell and setup pages, and the setup wizard's routes | Validation of settings or files |
| `bootstrap.py` | Create every schema, and decide whether a server may start (an owner, or a bootstrap token) | Creating accounts |
| `security.py` | Redirect validation and browser transport headers | Users, roles, or workflow authorization |
| `system_routes.py` | Health, live activity, and pending-work operational endpoints | Review workflow mutations |
| `assets.py` | Resolve packaged/static asset locations | Reading or interpreting asset contents |
| `accounts.py` | Users, the one owner, sessions, the permission matrix over accounts, join codes, folder assignment | HTTP responses |
| `site_settings.py` | The site's name, role labels, org id, join rules, public payload, bootstrap token and setup steps | Appearance validation |
| `appearance.py` | Appearance settings, their validation and contrast rule, `/site.css`, the custom-CSS sanitiser | Storing files |
| `site_assets.py` | Branding uploads: type and size checks, SVG sanitising, storage under `site/` | Which file the appearance uses |
| `collab.py` | Comments, revisions, notifications, and review lifecycle | Authentication or document generation |
| `scoping.py` | Determine which domain objects a signed-in role may receive | UI hiding or persistence |
| `db.py` | SQLite schema, migrations, transactions, and document repositories | HTTP or bucket I/O |
| `storage.py` | Bucket paths and object operations | Image transformation or database state |
| `file_locks.py` | Shared OS-backed lock primitive for cross-process resource ownership | Workflow-specific lock paths or policies |
| `imaging.py` | Image normalization and cached renderings | Bucket layout decisions |

## Pipeline stages

| Owner | Responsibility |
|---|---|
| `split.py` | Ingest uploads and propose/confirm document boundaries |
| `new_document.py` | Draft a document's transcription and fields, and keep its stored record current |
| `merge.py` | Merge already-drafted documents, and renumber a folder's display ids (rewriting their records) |
| `gaps.py` | Find display-id gaps and close them on a timer, through `merge.renumber_folder` |
| `modeling.py` | Shared image-model retry, chunking, and response-quality behaviour |
| `openrouter.py` | OpenAI-compatible chat protocol, message building from a compiled version, and response parsing |
| `prompting.py` | The questionnaire, its validation, and compiling answers into prompts, schemas and a registry |
| `profiles.py` | Prompt profiles and their immutable versions; which version a tab, folder or document uses |
| `fields.py` | The field registry: stored values, review validation, chunk and merge pooling, export columns, titles |
| `profile_routes.py` | Settings → Prompt's HTTP routes, including the sample test |
| `tabs.py` | Tabs: slugs, what may change when, and which tab a folder or batch belongs to |
| `tab_routes.py` | The tab strip everyone reads and the settings administrators change |
| `datafolder.py` | The client's data folder: `incipit.toml`, its defaults and checks, the paths and Settings it gives |
| `host.py` | The host's network addresses, the links to share, and a test of the port from the host |
| `startup.py` | Start-at-login entries per platform, made through one command runner |
| `access_routes.py` | Settings → Access and the wizard's access step |
| `keyvault.py` | Encrypting the model service's key with a key kept outside the data folder (DPAPI on Windows) |
| `backends.py` | The model service, its key and models; applying them to the running site; the free connection test |
| `ledger.py` | The insert-only record of every model call, and spend read from it |
| `jobs.py` | Drafting and split proposals as recorded jobs that survive a closed browser and a restart |
| `model_routes.py` | Settings → Models and Usage, and the jobs the page follows |
| `restore.py` | Rebuilding a data folder from a backup into a new folder, with a report of what was missing |
| `backup_routes.py` | Settings → Backups: where backups go, the snapshots kept, back up now, restore |
| `updates.py` | The release feed, version comparison, a checked download, and handing over to the installer |
| `support.py` | The support bundle: versions, settings without keys, status and masked logs, nothing private |
| `update_routes.py` | Settings → Updates and the support bundle download |
| `localname.py` | The site's `{name}.local` name, announced by mDNS while it runs |
| `portmap.py` | Opening the port on the router by UPnP, and what the router said |
| `relocate.py` | The location file naming this computer's data folder, and moving the folder |
| `tls.py` | The site's own HTTPS certificate: made, kept, renewed, and its fingerprint |
| `tunnel.py` | The Cloudflare quick tunnel: cloudflared run, watched, tied to the site, and its address |
| `fixedlink.py` | The team's permanent Telegraph page, rewritten with each new tunnel address |
| `tray.py` | The tray program: runs the site as a child process and offers its menu |
| `export.py` | Produce a folder's CSV content |
| `csv_safety.py` | Protect spreadsheet CSV cells without changing authoritative data |
| `folder_exports.py` | Check completion eligibility, build consistent deliverables, and coordinate bounded export jobs |
| `export_archive.py` | Name, atomically publish, and replicate complete export folder trees |

## Operations and data evolution

| Owner | Responsibility |
|---|---|
| `backup.py` | Snapshots and retention; an additive copy of the bucket at its own keys; removals only from the deletion log's internal ids; mirroring |
| `docid.py` | Display-id schemes (`letters`, `numbers`, `filename`): validate, suggest, and plan renumbering |
| `activity.py` | In-process exclusive-job and progress registry |
| `spend.py` | Reserve in-flight model spend before drafts persist their actual cost |
| `presence.py` | Ephemeral active-user and current-document heartbeats for administrators |
| `config.py` | Typed runtime settings and defaults |

`scripts/` contains explicit operator tools and the smoke scripts, not importable application
modules. `app/static/` contains browser assets; `tests/` mirrors behaviour rather than implementation modules; `docs/specs/` holds one
spec per module.

Within `app/static/`, `terms.js` turns role keys into the site's words (`term("reviewer")`);
`page-kit.js` holds the escape and JSON-API helpers the newer scripts share; `appearance.js` is the
Site and Appearance editor (Settings and the wizard); `people.js` the Team tab's People and Invite
cards; `setup.js` the wizard; `login-backdrop.js` draws the sign-in backdrop, and the editor's
preview of it; `folder_exports.js` owns the shared completion prompt and export progress;
`history.js` owns the browser's Back and Forward; `review_fields.js` draws the review form from a
document's registry; `prompt_builder.js` is Settings → Prompt and the wizard's prompt step; `tabs_admin.js` is
Settings → Tabs and the wizard's tabs step; `access.js` is Settings → Access and the wizard's access step; `autosave.js` drives the document screen's saves
through the hook `app.js` publishes. Export generation, sign-in policy, and authorization remain
server responsibilities.

## Dependency direction

HTTP/CLI composition may call domain modules. Domain modules may call repositories and external
service adapters. Repositories and adapters must not import HTTP/CLI composition. Browser code
must treat server authorization as authoritative; hiding a control is never access control.

## Browser and repository file ownership

Ownership here means responsibility in the code, not a guessed person or GitHub team. The
backend tables above cover `src/incipit/*.py`; `__init__.py` owns only package identity.
The following paths complete the map. A file stays with its feature when it changes.

| Files | Primary responsibility and boundary |
|---|---|
| `app/static/app.js` | Queue, document and split state; navigation request generations; review saves; Settings shell. Publishes hooks rather than sharing mutable state. |
| `app/static/workspace.js` | Identity chrome, Team, comments, notifications, presence, account dialogs. Calls queue hooks; must not assign queue/document state. Owns cleanup of listeners for dialogs it creates. |
| `app/static/history.js` | Browser route history and leave/restore coordination; screen owners implement restoration. |
| `app/static/autosave.js` | Save scheduling only; `app.js` owns save payloads and navigation safety. |
| `app/static/review_fields.js` | Registry-driven field widgets; no independent save pipeline. |
| `app/static/folder_exports.js` | Shared export confirmation and progress UI. |
| `app/static/page-kit.js`, `app/static/terms.js` | Shared browser API/escaping helpers and configured terminology respectively. No feature state. |
| `app/static/people.js` | People and invitation cards, reused by Team. |
| `app/static/appearance.js`, `prompt_builder.js`, `tabs_admin.js`, `access.js`, `models.js`, `backups.js`, `updates.js` | Each owns its named settings panel and any reuse in setup; backend modules own validation and authorization. All paths in this row are under `app/static/`. |
| `app/static/setup.js` | Setup-step orchestration, using feature panel owners. |
| `app/static/login-backdrop.js`, `app/static/backdrop.css` | Shared sign-in backdrop rendering and styling. |
| `app/static/index.html`, `login.html`, `setup.html` | Static shells and script ordering for their respective pages (all under `app/static/`). |
| `app/static/style.css` | Application layout, component styling and stacking; scope feature selectors to their containers. |
| `app/static/*.svg` | Application brand assets. |
| `site/**` | Public marketing site, estimator, and published media; independent of authenticated application state. |
| `scripts/**` | Explicit operator/build/release/smoke entry points; domain rules stay in `src/incipit/`. |
| `tests/**` | Behaviour verification; `conftest.py` owns shared app, sign-in and seed fixtures. |
| `docs/**`, `README.md`, `ARCHITECTURE.md`, `CLAUDE.md` | Specifications, operations, onboarding and development boundaries. |
| `installer/**`, `MANIFEST.in`, `pyproject.toml`, `VERSION` | Packaging, installer lifecycle and release identity. |
| `requirements.txt`, `constraints.txt` | Dependency entry point and resolved pins. |
| `.gitlab-ci.yml`, `pytest.ini`, `.gitignore`, `.gitattributes`, `.env.example`, `.claude/**` | CI, test policy and checkout/development configuration. |
| `data/**` | Runtime data; never a source-code responsibility or a test output destination. |

Async ownership rules: a screen owns the generation token for requests that can change its
state; leaving a document invalidates pending opens. A queue load commits its documents and
folders together only if it is still the newest request. Modal owners remove document-level
listeners on every close path, including replacement. Polling routes obtain visibility from
`scoping.py` and aggregate through `db.py`, never one repository lookup per document.
