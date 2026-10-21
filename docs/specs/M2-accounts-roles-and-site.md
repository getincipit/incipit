# M2 — Accounts, roles, bootstrap, appearance and the setup shell

## Purpose

An organisation running Incipit needs one accountable person (the owner), administrators the owner
appoints, and reviewers the administrators bring in, each with a written list of what they may do.
The first account must be created securely on a fresh install, nobody may join without the site's
permission, and the owner must be able to make the site theirs: its name, its logo, its colours,
and a sign-in page with its own photographs or motion. A first-run wizard walks the owner through
the parts that exist so far.

## Scope

- Roles `owner`, `admin`, `reviewer` replacing `admin`, `student` (M0 AC-7, deferred here). The
  role *keys* are fixed; what people read ("Editor", "Student", "Volunteer") is a site setting.
- A permission matrix enforced on the server, with fail-closed defaults.
- Bootstrap: `incipit init`, a single-use bootstrap token, atomic owner creation,
  `incipit create-owner --recover`, and a server that will not start without an owner or a token.
- Account management by owner and admins; join codes; optional reviewer self-signup.
- The organisation id (`org_id`) and the `/login?join=1` entry point.
- Site settings and appearance: product and company name, role labels, logo, favicon, accent
  colours, surface tone, fonts, corner radius, custom CSS, and the sign-in backdrop (colour,
  photo album, video, animation, built-in presets), in light and dark, with a reduced-motion
  fallback.
- A resumable `/setup` wizard with the steps that exist now: owner, site and appearance, people.

## Non-goals

Prompt profiles (M3), tabs (M4), model backends and spend (M5), S3 (M6), packaging (M7), the
hub's org-id lookup and "Join an organisation" page (M8, M9), e-mail (M10). Bundled font *files*
are not shipped: the font menu offers system stacks, and an owner may upload a WOFF2.

## Roles and permissions

| Action | Owner | Admin | Reviewer |
|---|---|---|---|
| Edit site settings and appearance | yes | yes, except custom CSS and fonts | no |
| Create reviewer accounts and reviewer join codes | yes | yes | no |
| Create admin accounts and admin join codes | yes | no | no |
| Change a user's role; transfer ownership | yes (never demotes self; transfer is atomic) | no | no |
| Deactivate / reactivate | yes (not themself) | reviewers only | no |
| Reset another person's password | admins and reviewers | reviewers only | no |
| Change own password; close own account | yes (owner cannot close) | yes | yes |
| Regenerate the org id; turn the join path on or off | yes | no | no |
| Delete data, export, see cost, Team tab | yes | yes | no |
| Review documents, comment | yes (on the admin track) | yes (admin track) | yes (reviewer track) |

Review tracks stay two: `collab.ROLES = ("reviewer", "admin")`; the owner reviews on the admin
track. `scoping.is_admin(user)` is true for owner and admin. Deactivation and an administrative
password reset end every session of that account; a reset also sets `must_change_password`, and
until it is changed the account can reach only its own password change, `/api/me` and sign-out.
Nobody can reset an owner's password through the site; `incipit create-owner --recover` on the
host does it.

## Data model

- `users.role` CHECK `owner|admin|reviewer`; a partial unique index `users(role) WHERE role =
  'owner'` makes a second owner impossible. New columns `created_by`, `last_login_at`,
  `must_change_password`. Existing `student` rows become `reviewer`; `document_reviews.role`
  values likewise.
- `site_settings` (one row, `id = 1`): `product_name`, `company_name`, `role_label_owner`,
  `role_label_admin`, `role_label_reviewer`, `time_zone`, `reviewer_self_signup`, `org_id`,
  `join_enabled`, `appearance_json`, `setup_steps_done_json`, `setup_completed_at`.
- `join_codes(id, code_hash UNIQUE, role CHECK admin|reviewer, label, created_by, created_at,
  expires_at, max_uses, uses, active)`. The code is shown once, at creation; only its SHA-256 is
  stored.
- `site_assets(id, kind CHECK logo|favicon|album_image|video|animation|font, bucket_key,
  content_type, width, height, bytes, caption, credit, position, created_by, created_at)`; files
  live in the bucket under `site/` and are backed up with it.

## Routes

| Route | Who |
|---|---|
| `GET /setup`, `POST /api/setup/owner` | anyone holding the bootstrap token, only while no owner exists; 404 afterwards |
| `GET /api/setup`, `POST /api/setup/steps/{id}`, `POST /api/setup/finish` | owner |
| `GET /api/site`, `GET /site.css`, `GET /site/assets/{id}` | public (no e-mail, key, org id or setting beyond what the sign-in page draws) |
| `GET /api/site/settings`, `PUT /api/site/settings` | owner, admin (owner-only fields refused for admins) |
| `POST /api/site/assets`, `PATCH /api/site/assets/{id}`, `DELETE /api/site/assets/{id}`, `POST /api/site/assets/order` | owner, admin (fonts owner-only) |
| `POST /api/site/org-id` | owner |
| `GET/POST /api/team/users`, `POST /api/team/users/{id}/(deactivate|reactivate|reset-password)` | owner, admin within the matrix |
| `POST /api/team/users/{id}/role`, `POST /api/team/owner/transfer` | owner |
| `GET/POST /api/team/join-codes`, `DELETE /api/team/join-codes/{id}` | owner, admin within the matrix |
| `POST /signup` | anyone with a valid join code, or anyone when reviewer self-signup is on |

## Bootstrap contract

`incipit init --db … --bucket-uri …` creates the database, the site row (with a fresh org id) and
the default tab. With `--owner NAME [--display-name …]` it creates the owner, reading the password
from `INCIPIT_OWNER_PASSWORD_FILE` or a prompt; without it, it writes a single-use bootstrap
token to `bootstrap.token` beside the database (mode 0600 where the OS supports it) and prints it.
`incipit review` refuses to start when the database has no owner and no token file exists. The
owner is created under `BEGIN IMMEDIATE` with the owner count re-checked inside the transaction;
the token file is deleted on success.

## Appearance

`appearance_json` holds the tagline, welcome text and footer of the sign-in card, the logo and
favicon asset ids, `accent_light`/`accent_dark`, `surface` (`warm|neutral|cool`), `heading_font`
and `body_font` (`system|serif|humanist|mono` or an uploaded font's id), `radius` (0–24 px),
`custom_css` (≤ 50 KB, owner only) and `backdrop`:
`{kind: none|colour|album|video|animation|preset, colour_light, colour_dark, album: {assets,
transition: crossfade|pan, interval 4–30 s, captions, shuffle}, video: {asset, poster},
animation: {asset}, preset: paper|ink|lines}`. `GET /site.css` emits the tokens for both themes
and appends the sanitised custom CSS last. `login-backdrop.js` draws the backdrop, pauses when the
page is hidden, and shows the first still (or nothing for motion-only kinds) under
`prefers-reduced-motion`. Uploads: PNG/JPEG/WebP re-encoded through `imaging` (≤ 15 MB), SVG
sanitised (no DOCTYPE, scripts, event attributes, foreign objects or external references),
MP4/WebM by signature (≤ 50 MB), Lottie JSON or animated SVG (≤ 2 MB), WOFF2 by signature
(≤ 2 MB). An accent that fails 3:1 contrast against its theme's background is refused.

## Acceptance criteria

- AC-1: Roles are `owner`, `admin` and `reviewer` in the database, API payloads and page classes; no `student` role remains, and the labels people read come from site settings.
- AC-2: Every row of the permission matrix holds over HTTP for owner, admin, reviewer and an anonymous visitor.
- AC-3: Code that is asked about a missing viewer refuses: `scoping.visible_folder_ids` with no user is the empty set and a review route with no viewer answers 401.
- AC-4: With no owner, `/setup` and `/api/setup/owner` require the bootstrap token and answer 404 without it; the token works once; afterwards `/api/setup/owner` answers 404, `/setup` is the owner's alone, and the token file is gone.
- AC-5: Two simultaneous owner creations produce exactly one owner, and the database refuses a second owner row.
- AC-6: `incipit review` will not start without an owner or a bootstrap token; `incipit init --owner` creates the owner from a password file; `incipit create-owner --recover` resets the owner's password.
- AC-7: Deactivating an account, or resetting its password, ends its open sessions on the next request; a reset account can only change its password until it does.
- AC-8: An admin can create reviewers and reviewer join codes but not admins or admin codes; the owner can do both, change roles and transfer ownership; an owner cannot demote or deactivate themself.
- AC-9: `/signup` creates an account only with a valid, active, unexpired, not-exhausted join code (whose role it takes) or, for a reviewer, when self-signup is on; a used-up code is refused.
- AC-10: The site has an org id of the form `ORG-XXXX-XXXX` that the owner can regenerate; `/login?join=1` opens the create-account form when joining is on and says joining is closed when it is off.
- AC-11: `GET /api/site` returns only what the sign-in page and shell draw: no org id, e-mail, key, join code, custom CSS source or settings beyond that list.
- AC-12: Site assets are validated by type and size before anything is written, SVG and custom CSS are sanitised, an accent without 3:1 contrast is refused, and `/site/assets/{id}` serves only registered assets.
- AC-13: `/site.css` carries the chosen tokens for light and dark, and the sign-in page and shell use the site's name, logo and backdrop; each backdrop kind renders, and reduced motion shows a still.
- AC-14: The setup wizard is resumable, shows exactly the registered steps, and can finish only when each is done; a signed-in owner who has not finished is sent to it.
- AC-15: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m2.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| Server started with no owner and no token | — | exits with "run `incipit init`" | runs `incipit init` |
| Bootstrap token lost before use | — | — | `incipit init` again writes a new token while no owner exists |
| Owner forgets their password | "that username and password do not match" | — | `incipit create-owner --recover` |
| Wrong, expired or used-up join code | "that join code is not valid" | counts the attempt against the rate limit | issues a new code |
| Signup with no code while self-signup is off | "ask an administrator for a join code" | creates nothing | — |
| Asset of the wrong type or too large | the limit and the accepted types | rejects before writing to the bucket | — |
| SVG or custom CSS with active content | "removed: …" listing what was stripped | stores the sanitised version | — |
| Accent fails contrast | "not enough contrast against the light/dark background" | keeps the previous colour | — |
| Bucket unreachable while uploading an asset | "could not store the file; nothing changed" | no row is written | checks storage |
| Admin attempts an owner-only action | "only the owner can …" (403) | nothing changes | — |
| Last owner tries to leave | "transfer ownership first" | nothing changes | — |

## Smoke checklist (`scripts/smoke_m2.py`)

Fresh data directory: `/setup` without a token is 404, with the printed token creates the owner
once; the owner names the site, uploads a logo and a three-image album, picks the album backdrop
and an accent, and the public sign-in page and `/site.css` reflect them; the owner creates an
admin join code and the admin joins with it; the admin creates a reviewer join code but is refused
an admin code; a reviewer joins with the code; `/login?join=1` opens the form; the admin resets the
reviewer's password and the reviewer's old session stops working; the reviewer receives no admin
or owner field; the owner regenerates the org id and turns joining off; the wizard finishes; the
M0 journey still passes with the new roles.

## Deviations from the first draft of this spec

- **The host's own browser needs no token** (added 2026-10-21 after the first install opened on the
  login form instead of the wizard). While setup is waiting (no owner, token file present), a request
  to `/` or `/login` from loopback, asked for by a local name and not relayed by the tunnel (which also
  arrives from loopback, under its public hostname and with Cloudflare's headers), goes to `/setup`,
  and `/setup` hands that browser the token from disk, which only this computer can read. Every other
  visitor still gets 404 at `/setup` and the login form elsewhere; the token remains the only key.


- **Animations are animated SVG only, not Lottie.** Playing Lottie JSON needs a player library in
  the page, and bundling one means vendoring third-party code, which is the user's call. An SVG's
  own CSS and SMIL motion play inside an `<img>`, where nothing in it can run a script. Adding
  Lottie is a small follow-up once a player is approved.
- **A video's poster is stored as an `album_image` asset** rather than a kind of its own; it is
  an ordinary photograph, and the album list says which photographs are in the album.
- **The wizard's site step is the same editor as Settings → Site and Appearance**
  (`app/static/appearance.js`), so the form exists once.
- **`/setup` after the owner exists** belongs to the owner (others are sent to sign in) rather
  than answering 404; `/api/setup/owner` answers 404 as specified. AC-4 says so.
- **The admin signup secret is gone**, not kept beside join codes: `create_user` takes no secret,
  and `INCIPIT_ADMIN_SIGNUP_SECRET` is no longer read.
- **The product name lives only in `site_settings`**; `Settings.product_name` was removed so the
  name has one source.
- **Signing in is recorded whenever a session starts** (password, wizard or new account), so an
  owner created by the wizard does not read as never having signed in.
- **M0 AC-4 was restated**: the CLI lists no command of a removed feature; M2 adds `init` and
  `create-owner`.
- **Known limitation:** plural role labels are formed by rule (`-s`, `-es`, `-ies`), so an
  irregular word such as "Alumnus" reads "Alumnuses"; per-label plurals can be added if a site
  needs one.

## Redundancy review

- One owner per rule: who may do what to whom is `accounts.require_may_create` /
  `require_may_manage` (the HTTP routes and the join-code functions call them); `accounts.is_admin`
  is the one admin predicate, and `scoping.is_admin` is that same function. The case-variant and
  owner-uniqueness checks each live in one place (`db.folder_spelled_otherwise`, the partial unique
  index). Every schema is created by `bootstrap.initialise`, which the CLI and the app both call.
- One source per fact: role words come from `site_settings` through `terms.js`; the page-kit holds
  the escape and API helpers the three new scripts share; the backdrop's styles are one file
  (`backdrop.css`) used by the sign-in page and the editor's preview; `login-backdrop.js` draws
  both.
- Deleted: the admin signup secret and its tests, `accounts.admin_signup_secret` and
  `active_admin_count`, the "last administrator" rule (the owner rule replaces it),
  `Settings.product_name`, the sign-in page's admin-code field.
- Left for the module that owns it: role-specific wording inside the long "How It Works" guide is
  labelled through `data-term` spans but its sentences are still written for the default labels
  (M9's demo copy pass); `activity.report_problem` still awaits M5's job table.

## Results (2026-10-20)

- Suite: 1,806 passed with `-p no:randomly -n auto`, including 91 M2 spec tests
  (`tests/test_m2_accounts_and_site.py`) and the spec-coverage check across M0, M1 and M2.
  `pyflakes` over `src`, `scripts` and `tests`, `compileall`, and `node --check` on every script in
  `app/static` are clean; the residue test passes.
- Smoke: `scripts/smoke_m2.py` 51 of 51; `scripts/smoke_m1.py` 56 of 56; `scripts/smoke_m0.py`
  27 of 27.
- Browser, on a fresh site at port 8099: `incipit review` refused to start before `incipit init`;
  `/setup` was 404 without the token and created the owner with it; the wizard saved a name, a
  "Volunteer" label, a three-photograph album with a caption and a warm surface, with the preview
  showing each change before saving; the shell then carried the new name and label and the owner's
  role classes; the Team tab's People and Invite cards made a join code; `/login?join=1` showed the
  album, the name and the heading with the create-account form open, and a volunteer joined with
  the code and was refused the site settings and people routes. No script errors in the console.
  This found three bugs, all fixed: the gate's security headers overwrote an uploaded SVG's sandbox
  policy, saved labels did not reach the open page until a reload, and an owner made by the wizard
  read as never having signed in.

## Status

Complete.
