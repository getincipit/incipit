# Running Incipit: a guide for your administrator

Incipit runs on one computer of yours, the **host**. Everyone else uses a web browser. This guide is
for whoever looks after that computer. Nothing here needs us: apart from new versions, your site
runs entirely on your own machine.

## Where everything is

| What | Where |
|---|---|
| The program | `C:\Program Files\Incipit` (or `%LOCALAPPDATA%\Programs\Incipit`), chosen when it was installed |
| Your **data folder** | The folder you chose at install (often `Documents\Incipit Data`). Everything your project holds is here. |
| ‣ the database | `incipit.db` in the data folder: people, folders, documents, reviews, comments, settings |
| ‣ scans and records | `bucket\` in the data folder |
| ‣ exports | `exports\` in the data folder |
| ‣ logs | `logs\` in the data folder (`incipit.log` for the site, `tray.log` for the tray icon) |
| ‣ the site's settings | `incipit.toml` in the data folder: the port, the site's name, where backups go |
| Backups | Where Settings → Backups says (by default `backups\` inside the data folder; move them to another drive) |
| This computer's own settings | `%LOCALAPPDATA%\Incipit`: which data folder is in use, and the key that unlocks the saved model key |

The **Incipit icon** in the system tray (bottom right of the screen) runs the site. Its menu opens
the site, opens the data folder, starts and stops the site, backs up now, copies the address to
share, checks for updates and shows when the last backup happened. The site only answers while
this computer is on and the icon is running; "Start Incipit when I log in" (Settings → Access) keeps
it that way.

## Backups

The site backs itself up while it runs: the scans, records and exports every two minutes, and the
database every ten minutes when something has changed. Older snapshots are thinned (everything from
the last six hours, then hourly for two days, daily for a month, weekly for half a year).

1. **Put backups on another drive.** Settings → Backups → *Backups folder*: an external disk or a
   second internal drive (for example `E:\Incipit Backups`). Backups on the same drive as the data
   protect against mistakes, not against that drive failing; the page warns while they share one.
2. **Add a second copy** if you can, for example on a network share: *A second copy*.
3. **Watch the tray.** It warns when the last backup failed or there has been no good backup for a
   day (an unplugged disk, most often). Settings → Backups shows the last result too.

## A restore drill (do this once, now)

Practise before you need it. Nothing here touches the site you are running.

1. Settings → Backups → *Snapshots kept*: pick the newest, **Restore…**, and type a new folder such as
   `D:\Incipit Restore Test`. The site restarts, builds that folder and runs from it.
2. Open the site and check a few documents, their pages and their reviews.
3. Read `RESTORE-REPORT.txt` in the new folder: it lists any file the backup lacked.
4. To go back to your real data folder: choose **Quit** on the tray icon, run
   `"C:\Program Files\Incipit\Incipit.exe" init-folder "D:\Incipit Data"` with your real folder (it
   changes nothing inside the folder; it only makes it the one in use again), and start Incipit from
   the Start menu. Your real data folder was left exactly as it was; delete the test folder.

From the command line (the program folder): `Incipit.exe restore --from "E:\Incipit Backups"
"D:\Incipit Restored"`; add `--use` to make it this computer's data folder.

## Moving to a new computer

1. On the old computer: Settings → Backups → **Back up now**, onto a drive you can carry.
2. On the new computer: install Incipit (choose any data folder; it will be replaced), then restore:
   `Incipit.exe restore --from "E:\Incipit Backups" "D:\Incipit Data" --use`, and start Incipit from
   the Start menu.
3. **Enter the model service's key again** (Settings → Models): it is locked to the old computer on
   purpose and does not travel with the data.
4. Settings → Access: check the site's name and the address to share; set up the router or tunnel
   again if you used them. Tell your team the new address if it changed.

## Changing the port or the address

- **The site's name on your network** (`our-archive.local`): Settings → Access → *Site name*. It
  changes at once.
- **The port**: Settings → Access → *Port*, Save, then Stop and Start the site from the tray icon.
- **Reaching the site from outside your network**: Settings → Access. The router can open the port
  automatically; when it cannot (a campus or company network, or a provider that shares one address
  between customers), the free Cloudflare tunnel gives an address, and the team's permanent link
  always leads to the current one.

## Updating

Settings → Updates (or the tray's **Check for updates**) says when a new version is ready and shows
what changed. **Install** (owner only) downloads it, checks it, and replaces the program; the site
stops for a minute or two and comes back by itself. Your data folder and settings are kept. If an
update ever fails, the old version keeps running; download the installer and run it by hand: it
offers the data folder you already use, so clicking Next keeps everything as it is.

## When something is wrong

| You notice | Do this |
|---|---|
| Nobody can open the site | Is the host on, and the Incipit icon in the tray? Use the icon's **Start the site**. |
| The team cannot reach it but it opens on the host | Settings → Access → **Test the port from this computer**; check the firewall allowed Incipit when it was installed. |
| "The saved key cannot be read on this computer" | The data came from another computer or account: enter the key again (Settings → Models). |
| Drafting stopped with a cap message | Settings → Usage: raise the cap, or wait for next month. |
| The tray says backups failed | Reconnect the backup drive, or choose another folder in Settings → Backups. |
| Anything else | Settings → Updates → **Download a support bundle**, read it, and send it to whoever helps you. It masks keys, tokens and e-mail addresses and holds no scans, transcriptions or passwords. From the command line: `Incipit.exe support-bundle`. |

## Uninstalling

Settings → Apps → Incipit → Uninstall. The program, its shortcuts and its firewall rule go; your
data folder and your backups stay where they are, so a later install can use them again.
