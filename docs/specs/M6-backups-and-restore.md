# M6 — Backups and restore in the data folder

## Purpose

The site already backs itself up while it runs: a verified snapshot of the database every ten
minutes when something changed, an additive copy of the scans, records and exports every two
minutes, retention that thins old snapshots, and removals that follow the deletion log. But a
client cannot choose where the backups go (they default to `backups/` inside the data folder, on the
same drive as what they protect), nothing records which files a snapshot needs, and getting data
back means copying folders by hand. M6 lets the owner put backups on another drive from Settings,
records a manifest with every snapshot, restores a backup into a new data folder from Settings or
the command line with a report of anything missing, and has the tray say when backups go stale.

## Scope

- **Settings → Backups** (owner changes, admins see): where backups go and an optional second copy,
  each checked before it is saved (the drive exists and can be written) and applied at once (the
  backup timer restarts on the new place); a warning when backups share the data's drive; the last
  backup's time and result; the snapshots kept, newest first, with their size; "Back up now".
- **Manifest.** Beside every snapshot, `snapshot/<name>.manifest.json`: every bucket key the
  database refers to at that moment (page scans, uploaded images, records, site assets) with its
  size, and the site's `incipit.toml` copied to `config/incipit.toml`. Retention removes a
  manifest with its snapshot.
- **Restore.** `restore.py` builds a new data folder from a backup: the chosen snapshot (newest by
  default) as `incipit.db`, checked; the backed-up bucket and exports; `incipit.toml` from the
  backup with the paths reset to the new folder; then every manifest key checked, and a
  `RESTORE-REPORT.txt` naming anything missing and the documents it belongs to. Never into a folder
  with files in it, never over the running site.
  - `incipit restore --from <backup folder> [--snapshot NAME] [--use] <new folder>`; `--use` makes it
    this computer's data folder.
  - From Settings → Backups, "Restore into a new folder…": the site restarts, builds the new folder
    from the snapshot, and runs from it (the old data folder is left exactly as it was).
- **Health and the tray.** `/health` keeps reporting the backup's age; the tray shows "Last backup:
  N minutes ago" and raises a notification when the last backup failed or is more than a day old.

## Non-goals

Remote or cloud backup targets (S3 and the like, M10), encryption of the backup set, scheduling
choices beyond the existing cadence, restoring a single document.

## Routes and roles

| Route | Owner | Admin | Reviewer |
|---|---|---|---|
| `GET /api/backups` | yes | yes | 403 |
| `PUT /api/backups` `{destination?, second_copy?}` | yes | 403 | 403 |
| `POST /api/backups/run` | yes | yes | 403 |
| `POST /api/backups/restore` `{snapshot, target}` | yes | 403 | 403 |

## Acceptance criteria

- AC-1: The owner sets where backups go and an optional second copy on Settings → Backups; a place that does not exist or cannot be written is refused with the reason, an accepted one is written to `incipit.toml` and the next backup goes there without a restart; an admin can see but not change it.
- AC-2: The page warns when backups are on the same drive as the data, shows the last backup's time and result, and lists the snapshots kept, newest first, with their sizes.
- AC-3: "Back up now" makes a backup at once and the page shows it.
- AC-4: Every snapshot has a manifest listing each bucket key the database refers to, with its size, and the backup holds a copy of `incipit.toml`; retention removes a manifest with its snapshot.
- AC-5: `incipit restore` builds a new data folder from a backup's newest (or a named) snapshot, with its scans, records and exports, and the site serves it: the documents open with their pages, reviews and comments are present, and the folders' exported files are there.
- AC-6: A restore reports, in `RESTORE-REPORT.txt` and on the command line, every file the manifest names that the backup lacks, with the documents it belongs to, and restores everything else.
- AC-7: A restore refuses a target with files in it, and a backup folder without a usable snapshot, saying why.
- AC-8: From Settings, the owner restores a snapshot into a new folder: the site restarts and runs from it, and the old data folder is unchanged.
- AC-9: The tray shows when the last backup happened and warns when it failed or is more than a day old.
- AC-10: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m6.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| The backup drive is unplugged | the tray warns after a day; the page says the destination is unavailable | records the failure, tries again every two minutes | reconnects the drive or picks another |
| The chosen place cannot be written | the page refuses it with the reason | keeps the old destination | picks another |
| Backups on the same drive as the data | a warning on the page | backs up anyway | moves them to another drive |
| The second copy is unreachable | "second copy unreachable" in the last result | the main backup still happens | reconnects it |
| A snapshot fails its check | the last result says so | discards it, keeps the earlier ones | none, unless it repeats |
| A restore target has files in it | the reason, before anything is copied | copies nothing | chooses an empty folder |
| Files missing from the backup at restore | RESTORE-REPORT.txt names them and their documents | restores everything else | finds them in the second copy or the old folder |
| A restore from Settings fails | a note in the old data folder and on the page after the restart | keeps running from the old folder | reads the note |

## Smoke checklist (`scripts/smoke_m6.py`)

From a fresh data folder with a fake model service: upload, split, confirm, draft, accept as a
reviewer and an admin, export; as the owner point backups at a folder outside the data folder (a
missing drive is refused) and press "Back up now"; see the snapshot listed with a manifest; delete
one backed-up scan from the backup; `incipit restore` into a new folder;
serve it and see the documents, their pages, the reviews and the exported files, with the missing
scan named in the report; restore again from Settings into another folder and see the site come back
from it with the old folder untouched; a reviewer is refused Backups.

## Decisions made while building it

- **A restore never overwrites.** It always builds a new folder and, from Settings, switches the
  location file to it at the next start, as a data-folder move does; the old folder is kept whole,
  so a restore of the wrong snapshot loses nothing.
- **The manifest is read from the snapshot, not the live database**, so it lists what that moment
  needed; it is written after the bucket copy in the same run, so the files it names are already in
  the backup when it is written.
- **The restored folder keeps the backed-up `incipit.toml`** (port, site name, tunnel, backups
  place), with any absolute paths for the database, bucket, exports, logs, certificate and tunnel
  reset to the new folder's own. The model key is not in the file and stays with this computer
  (M5); a restore on another computer asks for it again.
- **"Back up now" runs in the background** and the page follows it, because a first backup of a
  large archive copies every scan.
- **The tray reads the status file the backups already write**, so the warning needs no new channel;
  it notifies once per reason, not every ten minutes.

## Defects found while building it

- **Scans outside the data folder.** The bucket's address was built with `Path.as_uri()`, which
  writes a space as `%20`; the file store took it literally, so a data folder named "Incipit Data"
  (the installer's default) kept its scans in a sibling folder named "Incipit%20Data", which neither
  the backups nor a data-folder move ever saw. Found by this module's smoke, whose backup held no
  scans; the M0 journey's check had passed over an empty list. Fixed at both ends (the address is
  no longer encoded, and the store decodes one that is), with regression tests and the M0 check
  made to require scans.
- **Open database files.** `with sqlite3.connect(...)` commits but does not close, and Windows will
  not delete an open file, so a failed restore could not clear its half-built folder; the
  connections in `restore.py` and `relocate.py` are now closed explicitly.

## Redundancy review

- One owner per rule: `backup.py` alone writes backups, manifests and the status file; `restore.py`
  alone reads a backup back into a folder; `relocate.py` alone changes which folder this computer
  serves (a move and a restore share its start-up path); `backup_routes.py` is the only HTTP surface.
- One source per fact: the backup places live only in `incipit.toml`, the last result only in the
  status file, the snapshot list only in the backup folder itself; the page reads each fresh.
- Reused rather than rebuilt: snapshotting, verification, retention, the additive copy and the
  deletion log from the existing backups; the restart path from M4b's move; the M0 journey for the
  smoke (it now takes the database's file name).

## Results (2026-10-21)

- Suite: 2,105 passed (`-p no:randomly -n auto`), including 37 M6 tests
  (`tests/test_m6_backups_and_restore.py`). `pyflakes` and `node --check` are clean.
- Smoke: `scripts/smoke_m6.py` passes end to end: the M0 journey through export on a real data
  folder, backups moved outside it (a missing drive refused), a snapshot with its manifest, one
  scan deleted from the backup and named with its document by `incipit restore`, the restored folder
  serving its documents, pages, reviewed text, comments and exported files, then a restore from
  Settings after which the site ran from the new folder with the old database byte-for-byte
  unchanged. `smoke_m0` to `smoke_m5` still pass.
- Browser: the Backups page drew the last result, the same-drive warning, the places and the
  snapshots with the restore form opening in place, at phone width with no overflow and no console
  errors.

- Release review (2026-10-21): backups now run one at a time across the timer, "Back up now" and
  the tray (a separate process), on a lock file beside the status file; four concurrent runs took
  turns, and without the lock the same test failed. A backup place set by `INCIPIT_BACKUP_DIR` or
  `INCIPIT_BACKUP_MIRROR` is now the one the page shows, "Back up now" uses and a restart keeps;
  the page marks it as set by the environment and refuses to change it.

## Status

Complete.
