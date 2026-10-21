# M7 — Updates and operations

## Purpose

Once a client has installed Incipit, the one thing they rely on us for is new versions. M7 lets the
installed program find out that a newer version exists, download it, check it, and install it over
itself without touching the data folder, from the tray or from Settings, with the owner deciding
when. It also gives the client's own administrator what they need to run the site without us: logs
that roll over in the data folder, a support bundle they can look through and send if they choose,
and a plain guide (`docs/OPERATIONS.md`) to backups, a restore drill, moving to a new computer,
changing the port, updating and uninstalling.

## Scope

- **Release feed.** A small JSON file we publish with each release (`latest.json`: version, date,
  notes, and per platform the installer's URL, size and SHA-256). Its address is baked into the build
  (`UPDATE_FEED` beside `VERSION`; `INCIPIT_UPDATE_FEED` overrides it). An empty address means
  updates are not set up in this build, and the page says so. The site checks once a day while it
  runs and on request; nothing is installed without the owner asking.
- **Update.** `updates.py`: compare versions; download the installer to the user's own settings
  folder; check its size and SHA-256 against the feed (a mismatch is deleted and refused); start it
  with `/SILENT /UPDATE` and let the site stop. The installer, given `/UPDATE`, asks nothing, keeps
  the data folder and every setting, skips making a folder and the firewall prompt, replaces the
  program, and starts the tray again quietly. Owner only; from Settings → Updates or the tray.
- **Installer upgrades by hand.** Run without `/UPDATE` over an existing install, the folder page
  offers the data folder this computer already uses (read from the location file), so a person
  clicking Next keeps their data where it is.
- **Logs.** The site's rolling `logs/incipit.log` (5 × 5 MB) as before; the tray gains its own
  `logs/tray.log`.
- **Support bundle.** `incipit support-bundle [--out FILE]` and Settings → Updates → "Download a
  support bundle" (owner and admin): a zip of versions (Incipit, Python, Windows, cloudflared),
  `incipit.toml` with any key blanked, the backup status, the recent logs with keys, tokens and
  e-mail addresses masked, the jobs of the last week and the model calls' totals. Never the
  database, scans, transcriptions, passwords or keys. A `README.txt` inside says what it holds.
- **`docs/OPERATIONS.md`.** For the client's administrator, in plain words.

## Non-goals

Automatic installation without the owner's say; delta updates; downgrading (an older version is
installed by hand); signing the installer (a certificate we have not bought; open from M4b); a
macOS updater.

## Routes and roles

| Route | Owner | Admin | Reviewer |
|---|---|---|---|
| `GET /api/updates` (current version, feed state, the newer release if any) | yes | yes | 403 |
| `POST /api/updates/check` | yes | yes | 403 |
| `POST /api/updates/install` | yes | 403 | 403 |
| `GET /api/support/bundle` | yes | yes | 403 |

## Acceptance criteria

- AC-1: The site reads the release feed and reports a newer version with its notes, says when it is up to date, and says plainly when the feed cannot be reached or this build has no feed; it checks once a day by itself.
- AC-2: Versions compare as numbers (0.10.0 is newer than 0.9.3) and a pre-release is never offered over a release.
- AC-3: An installer whose size or SHA-256 differs from the feed is deleted and refused with the reason; a matching one is started with `/SILENT /UPDATE` and the site stops so it can be replaced.
- AC-4: Only the owner installs an update; an admin sees the update but cannot install it; a reviewer sees nothing.
- AC-5: Given `/UPDATE`, the installer shows no pages, keeps the data folder, the location file and every setting, skips the firewall step, and starts the tray again without opening a browser; run by hand over an existing install, its folder page offers the data folder already in use. *Verified outside the suite, by installing one version over another on this computer.*
- AC-6: The tray keeps its own rolling log in the data folder's `logs/`.
- AC-7: The support bundle holds versions, the settings file with any key blanked, the backup status, the recent logs with keys, tokens and e-mail addresses masked, the last week's jobs and the call totals, and a README; it holds no database, scan, transcription, password or key; the owner and admins can download it and a reviewer cannot.
- AC-8: `docs/OPERATIONS.md` covers where everything is, backups and a restore drill, moving to a new computer, changing the port and the address, updating, the support bundle and uninstalling.
- AC-9: The tray offers "Check for updates" and, when one is ready, "Install update X"; Settings → Updates shows the version, the check, the newer release's notes and the install button.
- AC-10: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m7.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| The feed cannot be reached | "Could not check for updates (reason); the site carries on" | tries again tomorrow | checks the internet connection |
| This build has no feed | "Updates are not set up in this build" | never checks | installs new versions by hand |
| The download stops part-way | "The download did not finish; try again" | deletes the partial file | tries again |
| The download does not match the feed | "The downloaded installer did not match; it was deleted" | deletes it, installs nothing | reports it to us |
| The installer fails | the old version keeps running (Inno Setup rolls back) | nothing changed in the data folder | runs the installer by hand |
| An update is started while a draft is running | the draft job is queued again after the update | the job resumes at the next start (M5) | nothing |
| The support bundle would include a secret | it is masked (`sk-or-…` keys, bearer tokens, setup and access tokens, e-mail addresses) | masks it before zipping | reads the bundle before sending it |

## Smoke checklist (`scripts/smoke_m7.py`)

Serve a release feed and a fake installer from a local server; start a site from a fresh data folder
with that feed; as the owner check for updates and see the newer version and its notes; corrupt the
feed's checksum and see the install refused and the download deleted; restore it and see the
install start the (fake) installer with `/SILENT /UPDATE` and the site stop; as an admin see the
update but be refused the install; download a support bundle and find the versions, the settings
without the key, the logs with a planted key masked, and no database; a reviewer is refused both.

## Decisions made while building it

- **Our own small feed rather than a code host's API.** A `latest.json` we write, wherever we host
  it (a release asset, a static page), keeps the program independent of any one service's API; the
  build bakes its address in, so changing hosts is a rebuild, not a code change.
- **The checksum comes from the feed over HTTPS.** Without a signed installer (open since M4b), the
  feed is what vouches for the download; a signed installer would let Windows vouch for it too.
- **Installing reads the feed fresh.** A release remembered from an earlier check may since have
  been replaced; found by the suite, where a cached bad checksum leaked into the next install.
- **The site stops with its own exit code (76)** so the tray quits rather than restarting it, and
  the installer, with `CloseApplications=force`, closes whatever is left; it starts the tray again
  itself with `--quiet`, so no browser opens at a host nobody may be sitting at.
- **An update asks nothing and changes nothing but the program.** `/UPDATE` skips every page, the
  data-folder step and the firewall step (which would otherwise prompt for administrator approval
  on every update, or register a second removal); a by-hand upgrade reads the location file so its
  folder page offers the folder already in use.
- **The support bundle is built, never stored**, and masks rather than drops lines, so the person
  helping still sees what happened around a secret.

## Redundancy review

- One owner per rule: `updates.py` alone reads the feed, compares versions, downloads and starts the
  installer; `support.py` alone decides what a bundle holds and masks it; the installer alone knows
  how an update differs from a first install.
- One source per fact: the running version is `VERSION` (served by `/health` and named on the page);
  the feed address is the build's `UPDATE_FEED` or the environment; the update state lives in
  memory and is re-read from the feed whenever an install is asked for.
- Reused: the tray's restart path from M4b (now with a second exit code), M4b's location file for
  the by-hand upgrade, the existing rolling log for the site.

## Results (2026-10-21)

- Suite: 2,175 passed (`-p no:randomly -n auto`), including 48 M7 tests
  (`tests/test_m7_updates_and_operations.py`). `pyflakes` and `node --check` are clean.
- Smoke: `scripts/smoke_m7.py` 15 of 15: the check against a served feed, a wrong checksum refused
  with nothing left on disk, the roles, a support bundle with a planted key masked and no database,
  and an install that downloaded and checked the installer, started it, and stopped the site with
  exit code 76.
- Upgrade on this computer (AC-5): 0.1.0 installed silently into a throwaway folder with its own
  data folder and port; with that site running, a 0.1.1 installer run with `/VERYSILENT /UPDATE`
  closed it, replaced the program, and started the tray again with `--quiet`; the site came back
  serving 0.1.1 by itself, with the data folder, a marker file, `incipit.toml` and the location
  file untouched and no new data folder made. The same installer run by hand (silently, no folder
  named) used the folder already in use. Uninstalling removed the program and kept the data.
- Browser: Settings → Updates showed the version, the check, a feed's error in plain words, and a
  newer release's notes with the owner's install button; no overflow or console errors.
- Found while testing: a `secret.key` from an earlier test run had been left in this computer's
  real settings folder; it protected nothing (no real install had stored a key) and was removed.

## Status

Complete. Releases are published to GitHub (`getincipit/incipit`, user decision 2026-10-21) by
`scripts/release.py`, which bakes the feed address
`https://github.com/getincipit/incipit/releases/latest/download/latest.json` into the build, compiles
the installer, writes `latest.json` with its size and SHA-256, and creates the release with both as
assets; GitHub's "latest release" link means installed copies always read the newest feed.
v0.1.0 was published this way on 2026-10-21; read through the live feed it reported "up to date"
against itself, and the published installer downloaded and passed the size and SHA-256 check,
byte-identical to the local build.
