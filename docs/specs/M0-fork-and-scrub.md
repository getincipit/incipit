# M0 — Fork and scrub

## Purpose

Create the Incipit repository from the incipit working tree so that nothing Edison-specific
remains in code, prompts, assets, tests or history, while the generic upload → split → draft →
review → export workflow and the Team tab keep working exactly as before.

## Scope

- Exclusion-aware copy of the working tree (uncommitted changes included), inventory, secret and
  data scans, fresh `git init`.
- Package rename `incipit` → `incipit`; CLI `incipit`.
- Removal of notebooks, the Existing Documents workflow (discover/extract/advance/transcribe),
  Edison authority matching, pre-indexed catalogue import, layout migration, the login scene and
  Edison imagery.
- Renaming of every Edison identifier (cookie, headers, storage keys, env vars, schema names,
  thread names, CSV names).
- Neutral interim prompts (replaced by the compiler in M3).
- Test fixtures consolidated into `tests/conftest.py`; tests of removed features deleted.

## Non-goals

Site settings, roles beyond admin/reviewer naming, prompt builder, tabs, S3, packaging, hub: later
modules. The Existing Documents *tables* and the review routes that read them are removed here; the
IIIF import returns as a tab kind in M10.

## Acceptance criteria

- AC-1: The repository contains no file matching `*.jpg *.jpeg *.png *.webp *.db *.db-wal *.bin *.pdf *.zip`, no JSON under `data/`, and no transcription fixture.
- AC-2: No tracked file contains a proprietary term (`edison`, `rutgers`, `edisondigital`, `amarel`, `slurm`, `tailscale`, `omeka`, `microfilm`, `gloc`, `taep`, the Edison gazetteer names and places, the sample DocIDs, `pp955`, `pisrael`, `scarletmail`), except this spec's own list and the residue test that enforces it.
- AC-3: No tracked file imports or routes to removed functionality (`notebook*`, `preindexed*`, `authority`, `discover`, `advance`, `migrate_layout`, `transcribe`), and none uses `EDISON_*` env vars, `edison_session`, `X-Edison-Frameable`, `edison-theme`, `{edison: true}`, `iiif.archive.org` or the old GitLab referer.
- AC-4: `python -m incipit.cli --help` lists no command of a removed feature (it listed only `review` and `export` at M0; M2 adds `init` and `create-owner`); the original package is not importable from the venv.
- AC-5: The browser shell shows one workflow tab ("Documents") and the Team tab for admins; `history.js` accepts `#new/...` and `#team` only.
- AC-6: The sign-in page is the card alone: no canvas, no quotes, no external scene assets; `/login.html`, `/style.css`, `/logo.svg` remain public, `/login-scene.js` and `/login-projector.bin` return 404.
- AC-7: Roles are `admin` and `reviewer` in the database, API payloads and page classes; a reviewer still receives none of `accepted_count`, `all_approved`, `export`, `claim`, `cost_estimate`, `assigned_by`, `reviewers`. *Deferred to M2 (see Deviations).*
- AC-8: The interim prompts contain the page-break rule, the fidelity rule and the structural-tag guardrail, and no proper name, place or product from the Edison gazetteer.
- AC-9: Every test module obtains its app and signed-in clients from `tests/conftest.py`; no test file defines its own `signed_in` helper.
- AC-10: The full suite passes with `-p no:randomly -n auto`; `compileall`, `pyflakes` and `node --check` on every shipped JS file pass. *Verified outside the suite, by the module's checks and smoke run.*
- AC-11: `scripts/smoke_m0.py` completes the upload → split → draft (fake backend) → review → send back → resubmit → export journey as admin and reviewer against a running server and reports every step green. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| Copy destination already exists | script refuses | nothing written | removes or renames the folder |
| Scan finds a secret or data file | script prints the path, exits non-zero | no `git init` | fixes the exclusion list, re-runs |
| A removed route is still called by the UI | 404 in the console, feature absent | — | the residue test names the route; remove the caller |
| `incipit` still importable after rename | AC-4 test fails | — | `pip install -e .` again |

## Smoke checklist (`scripts/smoke_m0.py`)

1. Server answers `/health` and serves `/login`.
2. Admin signs in, uploads six drawn pages as one batch, receives a split proposal from the fake backend, confirms it as two documents.
3. Drafts land for both documents; the review screen payload carries text, fields and history.
4. Reviewer signs in, sees only the assigned folder, accepts both, submits.
5. Admin sends one back with a reason; reviewer sees the flag, fixes, resubmits; admin accepts; folder reads ready; export job completes and the CSV has one row per document.
6. Reviewer payloads contain no admin field (AC-7 list).

## Redundancy review

- Sixteen local `signed_in` helpers replaced by `tests/conftest.signed_in`; `make_app` is the one
  way to build an app (the remaining per-file `app`/`site` fixtures seed test-specific rows and are
  folded in as each file is next touched).
- Dead CSS removed: `style.css` went from 3,015 to 2,447 lines (notebook, discover, pending-picker,
  pre-index and login-scene rules).
- Removed as dead after the scrub: `_transcribe_only`, `_from_preindexed`, `_existing_folder_label`,
  `_maybe_transcribe_next_batch`, `_advance_in_background`, the read-only metadata grid, the
  existing-workflow analytics branch, `db._table_exists` (duplicate of `_has_table`), the
  `transcriptions`/`notebook_openings` branches in `collab`, `scoping`, `folder_exports`,
  `reviewed_by_folder`, `_last_touched_a_document`.
- Known redundancy left for the module that owns it: about thirty `activeWorkflow === "new"`
  branches in `app.js` that are now always true, `_review_table` returning one constant,
  `accounts.WORKFLOWS` with one member, the `workflow` column carrying one value (all M4, tabs);
  the `student` role name and `role-student` class (M2, roles); `docid.py`'s single id scheme and
  `layout.regroup` moving files after renames (M1, identity).

## Deviations from the first draft of this spec

- AC-7 (roles renamed to `reviewer`) is deferred to M2, where the role model changes anyway;
  renaming twice would be the second copy of a change. Roles stay `admin` and `student` in M0.
- `scripts/generate_screenshots.py` was dropped rather than adapted: it seeded the original
  project's data and called removed functions; `incipit demo` (M9) replaces it.
- `manifest.py`/`extract.py` were not parked: they are copied back from the original repository
  when the IIIF tab kind is built (M10), which keeps the fork free of their archive-specific code.

## Results (2026-10-19)

- Suite: 1,738 passed with `-p no:randomly -n auto` (the original had 2,432; 694 tests of removed
  features went with them). `compileall`, `pyflakes` and `node --check` clean.
- Residue: `tests/test_fork_residue.py` passes over every tracked and untracked file.
- Smoke: `scripts/smoke_m0.py` reports 25 PASS, 0 FAIL for the upload → export journey as admin
  and student, including the student-boundary check.
- Browser: sign-in card, shell with one "Documents" tab and the Team tab for the admin, settings
  menu without the removed cards (checked by hand on port 8011).

## Status

Complete.
