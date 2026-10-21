# M4b — Installer, data folder and access

## Purpose

We build and publish the software; a client installs it on one computer of their own, the **host**,
keeps everything in a folder they choose, and their team reaches it with a browser over the
network. M4b makes that install: a data folder with one configuration file that the server reads,
a `serve` command that runs from it, an Access step that tells the owner the address to share and
checks the port, start-at-login, a tray program, the application icon set, a packaged build and the
installer script that asks the three questions (folder, port, start at login).

## Scope

- **The data folder.** `incipit.toml` at its root, read by every command; the layout
  `incipit.db`, `bucket/`, `exports/`, `backups/`, `logs/` created on first run.
  `incipit init-folder <path>` makes one; `incipit serve [--data-folder <path>]` runs the site from
  it (the folder from the argument, else `INCIPIT_DATA_DIR`, else the current directory when it holds
  an `incipit.toml`). Environment variables still override single values for development; the old
  `review` command stays for the checkout's own `launch.json`.
- **Access.** `host.py` finds the host's LAN addresses and hostname, builds the link to share, and
  tests whether the port answers on each address from the host itself; `startup.py` registers and
  removes a start-at-login entry (a Windows logon task, a macOS LaunchAgent, an XDG autostart entry)
  through one command runner. Routes: `GET /api/access` (admin), `PUT /api/access` (owner: port,
  start at login), `POST /api/access/test`. A wizard step "access" after "people" and Settings → Access
  show the link, a copy button, the port test's result, whether the router opened the port (below)
  and where the data lives.
- **Icons.** `scripts/build_icons.py` renders `app/static/favicon.svg` with resvg to PNG at 16, 32,
  48, 64, 128, 256 and 512 px and packs a multi-size `.ico`; the build and the installer use them.
- **Build and installer.** `scripts/build_app.py` makes a one-folder PyInstaller build with the
  static assets inside it (`assets.py` finds them in a frozen build); `installer/incipit.iss` is the
  Inno Setup script: data folder, port and start-at-login pages, the folder written through
  `incipit init-folder`, a Start-menu entry, the first start, the wizard opened in the browser;
  upgrading keeps the folder, uninstalling leaves it.
- **Tray.** `incipit tray`: Open Incipit, Open data folder, Start/Stop the server, Back up now,
  Quit; the address to share in its tooltip. Optional (`pip install incipit[tray]`), used by the
  installer's Start-menu entry.

- **Done for the client, not asked of them** (added 2026-10-21 at the user's request: "everything
  should be done for the user without being intrusive", no tunnel service).
  - *The data folder* is chosen or created in the installer (Browse → Make New Folder, default
    name "Incipit Data"); a folder holding other files is used only after a confirmation. The
    folder is recorded in a **location file** (`%LOCALAPPDATA%\Incipit\location.toml`, `relocate.py`),
    so the shortcuts and the logon task run `Incipit.exe tray` with no path.
  - *Moving it later*: Settings → Access → "Where the data lives" (owner only) shows the folder, its
    size and the free space, and takes a new or empty folder. The site restarts, copies everything,
    checks every file's size and the database's integrity, and only then points the location file at
    the copy; the old folder is left whole with `MOVED-TO.txt` and is never served again. A failed
    copy leaves the site on the old folder with `MOVE-FAILED.txt`, shown on the page.
  - *Port forwarding* by UPnP (`portmap.py`): with `[server] forward_port = true` the site asks the
    router to forward its port to this computer while it runs, renews the lease, and removes it on
    stop. Access shows the internet address to share, or says plainly that no router answered, that
    it refused, or that another network (a second router or the provider's CGNAT) sits in front of it.
    On by default in the installer, switched at once from Access.
  - *HTTPS with the site's own certificate* (added 2026-10-21, user's choice of the options for
    securing the internet address): `tls.py` makes a self-signed certificate on first start
    (names: `{site_name}.local`, the computer's name, `localhost`; addresses: the LAN addresses and
    127.0.0.1), keeps it in `tls/` for five years, and the site serves HTTPS on `https_port` (default
    8443) beside HTTP. **Only the HTTPS port is forwarded**; if HTTPS cannot start, nothing is opened.
    Access shows the encrypted links, the SHA-256 fingerprint, the expiry, a certificate download
    (`/certificate.crt`, public), and "Copy a message for the team" explaining the browser's warning,
    how to check the fingerprint, how to continue in each browser, and how to install the certificate.
  - *A Cloudflare quick tunnel for networks the router cannot open* (added 2026-10-20: the user
    chose quick tunnels after ruling out anything we operate or anything with a cost beyond
    OpenRouter). `tunnel.py` runs the bundled `cloudflared`, which connects out to Cloudflare and is
    given a temporary `https://….trycloudflare.com` address; no account, domain or payment. With
    `tunnel = "auto"` (the installer's default) it opens only when the router gives no working
    internet address; `"on"` always, `"off"` never. Access shows the current address and a
    three-way switch; the tray copies it.
  - *One permanent link for the team* (added 2026-10-20; chosen after comparing free, no-account
    services): `fixedlink.py` keeps a public Telegraph page (telegra.ph; its API hands out an access
    token with no sign-up) whose text links to the tunnel's current address. The page's URL never
    changes; only the token, kept in `tunnel/fixed-link.json`, can change it. A new address goes
    on the page only once public DNS (asked over HTTPS, so this computer's cache is untouched)
    answers for it; when the site stops, the page says it is not running. Access shows the link,
    its state and "Replace the link"; the tray's "Copy the team's link" copies it.
  - *The Windows firewall* rule for the program is added by the installer (one administrator
    prompt) and removed by the uninstaller.

## Non-goals

A macOS or Linux installer (the same scripts build on those hosts later); automatic updates (M7);
the model key and spend (M5); HTTPS for the internet address (see Decisions).

- **A local name** (added 2026-10-21 at the user's request). `localname.py` announces the site as
  `{site_name}.local` by multicast DNS (zeroconf), so the team opens `http://our-archive.local:8010/`
  instead of an IP address that can change; Windows 10 and later, macOS and iOS resolve it with no
  setup. The installer asks for it beside the port.

## Configuration (`incipit.toml`)

```toml
[server]
host = "0.0.0.0"        # every interface, so the team can reach it; 127.0.0.1 keeps it private
port = 8010
start_at_login = true
site_name = "our-archive" # announced as our-archive.local; empty announces nothing
https_port = 8443        # the same site over HTTPS, with the certificate the site makes itself
forward_port = true      # ask the router (UPnP) to open the HTTPS port while the site runs
tunnel = "auto"          # a Cloudflare quick tunnel when the router cannot: "off", "auto" or "on"
fixed_link = true        # a Telegraph page that always leads to the tunnel's current address

[paths]                 # relative to the data folder unless absolute
database = "incipit.db"
bucket = "bucket"
exports = "exports"
backups = "backups"
backup_mirror = ""
logs = "logs"
tls = "tls"             # the site's certificate and key
tunnel = "tunnel"       # cloudflared's own home and the current tunnel address

[model]
openrouter_api_key = "" # moved into the database, encrypted, at start (M5); entered on Settings → Models
```

## Acceptance criteria

- AC-1: `incipit init-folder <path>` creates the folder layout and an `incipit.toml` with the defaults; running it again changes nothing already there.
- AC-2: `incipit serve` reads `incipit.toml` from the data folder (argument, `INCIPIT_DATA_DIR`, or the current directory) and serves the site on the configured host and port with every path resolved inside the folder; an environment variable overrides a single value.
- AC-3: A data folder without an owner opens the setup wizard with its token, as `incipit init` did; the wizard's steps now end with "access".
- AC-4: `GET /api/access` reports the host's LAN addresses and hostname, the port, the links to share and whether start-at-login is registered; a reviewer is refused.
- AC-5: `POST /api/access/test` tries each address on the port from the host and reports which answered; a port nothing listens on is reported as such.
- AC-6: The owner can change the port and start-at-login from Settings → Access; the port is written to `incipit.toml` and takes effect on the next start, which the page says.
- AC-7: Start-at-login registers and removes a per-user entry through one command runner, and the page reflects its state; the runner is mocked in the suite.
- AC-8: `scripts/build_icons.py` writes PNGs at every size and a multi-size ICO from `favicon.svg`; each PNG is square, the right size and not blank.
- AC-9: `assets.py` finds the static files in a frozen build (`sys._MEIPASS`) as it finds them in a checkout.
- AC-10: `scripts/build_app.py` produces a one-folder build whose executable serves a data folder, and `installer/incipit.iss` installs it with the three questions; verified on the build host. *Verified outside the suite, on the build host.*
- AC-11: The tray program offers Open, Open data folder, Start/Stop, Back up now and Quit, and shows the shared link; its menu is tested without a display.
- AC-13: The site can be given a local name (installer question and `/SITENAME=`, `init-folder --site-name`, Settings → Access): it is normalised to a DNS label, announced by mDNS as `{name}.local` for the host's addresses while the site runs, listed first among the links to share, changed at once from Access, and a name another computer already answers is reported rather than announced.
- AC-14: The installer lets the client choose an existing folder or make a new one for the data; `init-folder` records it in the location file, and the program, its shortcuts and its logon task find it there with no path given; a recorded folder that is missing is named in the error.
- AC-15: The owner can move the data folder from Settings → Access to a new or empty folder (a folder with files, the current folder or one inside it is refused); the site restarts, copies and checks everything, and runs from the new folder, which the location file then names; the old folder is kept and never served again; a failed copy leaves the site on the old folder and the page says why; a move not yet made can be cancelled; admins and reviewers cannot move it.
- AC-16: With forwarding on (installer, `init-folder --forward-port`, or Settings → Access at once), the site asks the router by UPnP to forward its port to this computer, shows the internet address to share when the router has a public address, reports "no router", a refusal with the router's reason, or a second network in front (private or CGNAT address), falls back to a permanent mapping on routers that take only those, and removes the forwarding when it stops.
- AC-17: The installer adds a Windows firewall rule for the program and the uninstaller removes it.
- AC-18: The site makes its own certificate for its names and addresses on first start, keeps it across starts (renewing it only when it nears its end), and serves the same site over HTTPS on `https_port`; the router is asked to forward the HTTPS port only, and plain HTTP is never opened to the internet; Access shows the fingerprint (as browsers show it) of the certificate actually served, a public certificate download, and a message for the team about the warning; a taken HTTPS port leaves the site on HTTP with the reason shown; an HTTPS port equal to the port is refused; the owner can change it for the next start.
- AC-19: With the tunnel on, or on "when needed" and the router giving no working internet address (refused, no router, another network in front, or forwarding off), the site runs the bundled cloudflared against its port, records the address cloudflared is given for Access and the tray, forgets it when the tunnel stops, restarts a stopped tunnel with a growing wait, and reports a missing program; "when needed" opens no tunnel when the router gives an address; the owner switches the mode at once and it is kept; sign-in limits count each tunnel visitor by Cloudflare's visitor address, trusted only from this computer while a tunnel runs; a tunnel visitor's session cookie is marked secure; cloudflared dies with the site even when the site is killed outright; the build bundles cloudflared with its licence notice and the installer offers the tunnel.
- AC-20: While the tunnel runs and the fixed link is on, the first address makes a Telegraph page leading to it and every later address rewrites the same page (kept across restarts of the site); an address goes on the page only once public DNS answers for it, and one it never learns is not announced; a Telegraph refusal is reported; when the site stops the page says it is not running; the owner can replace the link, which retires the old page; Access shows the link and the tray copies it in preference to the changing address; only the owner replaces it, and turning it off is kept.
- AC-12: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m4b.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| The chosen data folder is not writable | the installer (or `init-folder`) says so and asks again | writes nothing | picks another folder |
| The port is taken | `serve` stops naming the port and the program holding it when it can; Settings → Access shows the same | nothing starts | changes the port in Access or the file |
| `incipit.toml` is missing a key or has a bad value | the key and the line, with the default it would take | refuses to start | fixes the file (the Access page rewrites it) |
| No network address is found | "only this computer can reach the site" with `localhost` | serves on localhost | connects the host to the network |
| The port test fails from the host | "nothing answered on …; a firewall rule on this computer may be blocking it" | reports per address | allows the port in the firewall |
| The site name is already answered by another computer | "our-archive.local already answers from …; choose another name" on Access | does not announce it | picks another name |
| A network blocks multicast (some guest or corporate Wi-Fi) | the name does not open; the IP links still do | nothing | shares the IP link, or asks IT to allow mDNS |
| Start-at-login cannot be registered | the command's own message | leaves the setting off | registers by hand (the page shows the command) |
| The data folder's drive is missing at start (an unplugged disk) | the tray says the folder is missing and names it | does not start | reconnects the drive, or moves the data |
| The new folder for a move has files, is inside the old one, or lacks room | the page names the reason before anything is copied | records nothing | picks another folder |
| A move's copy fails or does not match | "Moving the data … failed and was undone: …" on Access | keeps serving the old folder, removes the request | frees space or picks another drive |
| No router answers UPnP | "No router answered. Turn on UPnP …, or forward the port by hand", with the port and address | retries at every renewal | turns UPnP on, or forwards by hand |
| The router refuses the mapping | the refusal with the router's reason | retries at every renewal | frees the port on the router or picks another |
| Another network sits in front (second router or CGNAT) | "another network sits in front of it" with what to ask the provider | keeps the mapping, shows no internet link | asks for a public address or bridge mode |
| The internet address changes (a router restart) | the new address on Access | renews and rereads it every 20 minutes | shares the new address |
| The administrator prompt for the firewall is declined | the team cannot connect; the port test from another computer fails | installs everything else | allows the program in Windows Security, or reinstalls |
| The HTTPS port is taken | "HTTPS is not running: port … is in use by another program" on Access | serves HTTP, opens nothing on the router | picks another HTTPS port in Access and restarts |
| The certificate is unreadable or nearly expired | nothing, or one new browser warning | makes a new one at the start | sends the team the new fingerprint (the message button) |
| A team member sees a fingerprint that does not match | the message tells them not to sign in | nothing | checks the address; someone may be intercepting |
| Cloudflare refuses or limits a quick tunnel (429, outage) | "The tunnel stopped; it is started again on its own" with Cloudflare's message | restarts after 5 s, 15 s, 1 min, then every 5 min | waits, or uses the router or IT instead |
| The tunnel's address changes (every start) | the new address on Access and in the tray | records the new one | shares the new address (until the stable link exists) |
| More than 200 requests at once through the tunnel | some requests fail with 429 | nothing | fewer people at once, or direct access |
| The site is killed outright (the tray's Stop, a crash) | nothing | Windows ends cloudflared with it; a leftover from an earlier run is ended at the next start | nothing |
| The tray itself fails while starting (a bad menu, a missing library) | the error, in the terminal or tray.log | stops the site it had started, so the port is not left held by a headless server | reinstalls or reports it |
| The chosen data folder (or program folder) is one the account cannot write to, such as C:\Program Files | the installer says why and what to choose instead, before Next; a silent install stops before writing anything | nothing is created | picks a folder the account can write to (a data drive is fine), or runs the installer as an administrator with /ALLUSERS for the program alone |
| cloudflared is missing from the install | "The tunnel program is missing; reinstall Incipit" | opens no tunnel | reinstalls |
| Telegraph is unreachable or refuses | "The page could not be updated" with Telegraph's reason | tries again at the next address | shares the tunnel address directly meanwhile |
| Public DNS is slow to learn a new address | "Waiting for the new address to be known on the internet…" | asks every 3 s for 3 minutes, then keeps the last address on the page | waits; the next start tries again |
| The site is stopped from the tray, or crashes | from the tray: "not running" on the page; after a crash: the last address, which no longer answers | the tray rewrites the page itself, since it kills the site outright on Windows | restarts the site; the page is rewritten |
| The link was shared too widely | — | — | "Replace the link": a new page; the old one says it no longer works |
| Telegraph is blocked on a team member's network | the link does not open for them | nothing | gives them the tunnel address directly |
| The build host lacks Inno Setup or resvg | the build script names the missing tool | stops before packaging | installs it |

## Smoke checklist (`scripts/smoke_m4b.py`)

Create a data folder with `init-folder`; check the layout and the file; start `serve` from it on a
free port; see the wizard demand the token; create the owner; finish the earlier steps; open Access:
the link names the port, the port test reports the host answered, start-at-login flips on and off
through a mocked runner and is written to the file; change the port in Access and see it in the
file; a reviewer is refused `/api/access`; generate the icon set into a temp dir and check the sizes.

## Decisions made while building it

- **One host, browsers for everyone else.** Only the host computer installs Incipit; the team
  uses a browser. The installer is not for every computer: the database and the files live in one
  place, so there is one copy of the truth and one thing to back up.
- The tray runs the server as a **child process**, so Stop frees the port and Start rereads
  `incipit.toml` (which is how a port changed in Settings → Access takes effect).
- The installer's publisher comes from `INCIPIT_PUBLISHER` at build time (default "Incipit"),
  so no organisation's name is baked into the source.
- `/health` reports the bundled `VERSION` rather than a git commit, which a client's install
  does not have.
- **No tunnel service** (user decision 2026-10-21); the guidance mentioning one is gone, replaced by
  UPnP forwarding and plain instructions for forwarding by hand.
- **The location file lives in the user's own settings folder**, not in the program folder, so an
  upgrade or reinstall keeps it; `INCIPIT_SETTINGS_DIR` overrides it, and the suite points it at a
  temporary folder for every test.
- **A move happens at start, not while serving**: the running site cannot copy a database it holds
  open, so it records the move, exits with code 75, and the tray (or, unsupervised, the site itself)
  starts it again; the copy is checked before the location changes, so nothing is ever half-moved.
- **The firewall rule names the program, on every network profile**: a port rule would break when
  the port changes, and Windows labels a new network Public, where a Private-only rule would leave
  the team locked out with no message.
- **A self-signed certificate, not a bought one** (user decision 2026-10-21): no domain or service of
  ours is needed and nothing leaves the client's computer; the cost is one warning per browser,
  which the team message explains and the fingerprint makes checkable. The certificate is kept for
  five years because a new one means a new warning for everyone; the internet address is not in it
  (it can change and the warning appears for a self-signed certificate either way).
- **Measured, not assumed** (2026-10-20, through a real quick tunnel from this host): 50 MB,
  120 MB and 400 MB request bodies all reached the site at about 13 MB/s, so Cloudflare's 100 MB
  free-plan body cap did not apply to quick tunnels, and splitting uploads was not needed. Quick
  tunnels have no published data cap; Cloudflare calls them best-effort, for testing, with at most
  200 requests in progress.
- **Telegraph for the fixed link** (compared 2026-10-20): ntfy.sh (no account, but messages are
  cached only 12 hours, 250 a day, and anyone who knows the topic could post a fake address),
  jsonblob.com (no account, but its terms could not be confirmed) and Telegraph (no sign-up, edits
  need the token, a permanent page URL, readable by a browser script for a later auto-forward
  page). Telegraph is a free Telegram service with no promise behind it, so the tunnel address is
  always on Access and in the tray as well.
- **The tunnel's local hop is plain HTTP to 127.0.0.1**; Cloudflare gives visitors HTTPS with a real
  certificate, so no warning, but Cloudflare can read the traffic on the way (the page says so).
- **HTTPS on a second port rather than replacing HTTP**: the team on the network keeps the plain
  `.local` link with no warning, and only the encrypted port is opened to the internet.

## Redundancy review

- One owner per rule: `datafolder.py` is the only reader and writer of `incipit.toml` and the only
  place that knows the folder layout and the defaults; `cli.serve`, the tray, the Access route and
  `init-folder` all go through it. `startup.py` is the only code that knows how each platform starts
  a program at login, with one command runner the suite replaces. `host.py` is the one source of the
  addresses and links the Access page, the tray tooltip and the smoke all show.
- One source per fact: the port is in `incipit.toml` alone; the page shows the running port from
  Settings and the pending one from the file's answer, never a stored copy.
- Consolidated: the build icons come from `favicon.svg`, so the browser tab, the program and the
  installer share one drawing.
- `relocate.py` is the only reader and writer of the location file, `portmap.py` the only code that
  speaks to the router; `localname` and `portmap` share one shape (a module-level `current()` and a
  replace-on-change starter) so the Access route treats both alike.
- Left for the module that owns it: updates (M7), the model key moving into the database (M5),
  backups to a second drive chosen in Settings (M6), a macOS build (on a Mac build host).

## Results (2026-10-21)

- Suite: 1,927 passed with `-p no:randomly -n auto`, including 29 M4b spec tests
  (`tests/test_m4b_installer.py`) and the spec-coverage check across M0–M4b. `pyflakes`,
  `compileall` and `node --check` are clean.
- Smoke: `scripts/smoke_m4b.py` 16 of 16; `smoke_m0` to `smoke_m4` still pass.
- Build: `scripts/build_app.py` produced `dist/Incipit` (51 MB, PyInstaller one-folder). The
  packaged `Incipit.exe` made a data folder, served it, and `/health` reported version 0.1.0 with
  the logo and favicon served.
- Installer (2026-10-20, Inno Setup 6.7.3): `Incipit-0.1.0-setup.exe` (22 MB) installed silently
  into a throwaway program folder with `/DATAFOLDER`, `/PORT=8019` and `/NOLOGIN`: the data folder
  and `incipit.toml` were made with the chosen port, both Start-menu entries pointed at the right
  places, Apps & features listed it, and no logon task was made. The first installed run crashed:
  a windowed build has no stdout and uvicorn's logging called `None.isatty()` (fixed in
  `cli.ensure_console_streams`, regression test added). Rebuilt and installed again over the first
  install, the data folder and database were untouched, the installed program served the site on
  8019 with the wizard behind its token and logged into the data folder; uninstalling removed the
  program, the shortcuts and the Apps & features entry and left the data folder whole.
- Browser, from a throwaway data folder: the wizard's new last step listed this machine's real
  network addresses with copy buttons, and the port test reported every address answering. At
  phone width it found a 2-pixel overflow in the wizard's step list, now fixed. No console errors.

- Done for the client (2026-10-20): suite 1,968 passed (`-n auto`), 71 M4b spec tests;
  `scripts/smoke_m4b.py` 28 of 28, including a move of the data folder from Access with the site
  restarted and serving from the new folder with its team intact. On this network's real router,
  UPnP discovery found the gateway and read its WAN service; the router reports a private outside
  address, which `portmap` classifies as a second network in front (double NAT), so no internet
  link would be offered here. No mapping was added to the router during testing.
- Installer, rebuilt: installed silently with `/NOFORWARD /NOFIREWALL`; `init-folder` wrote the
  location file and the shortcuts run `Incipit.exe tray` with no path; the installed program,
  started with no folder named, found and served it; a move requested over HTTP restarted the
  packaged program and it came back serving from the new folder, the old one kept with
  `MOVED-TO.txt`. The uninstall then waited on an administrator prompt to remove a firewall rule
  that was never added; the removal now runs only when the install added the rule (regression
  test added). Uninstalling removed the program and shortcuts and left the data.

- HTTPS (2026-10-20): suite 1,979 passed, 82 M4b spec tests; `scripts/smoke_m4b.py` passes
  (twice in a row), including the sign-in page over HTTPS and the served certificate's fingerprint
  matching the one Access shows. Rebuilt and installed silently: the installed program made its
  certificate in the data folder's `tls/`, served the site on HTTP 8019 and HTTPS 8443, and the
  certificate it served was the one on disk; the uninstall finished with no administrator prompt.
  In the browser the HTTPS card showed the encrypted links, the fingerprint (wrapped at its
  colons) and the two buttons; a first draft let the buttons spill past the card at a narrow width,
  now wrapped. No console errors. The router was not asked to forward anything.

- Quick tunnel (2026-10-20): suite 2,001 passed, 103 M4b spec tests; `scripts/smoke_m4b.py`
  passes. From a checkout, a real quick tunnel served the sign-in page, a sign-in and the Access
  page, and carried 50, 120 and 400 MB requests at about 13 MB/s. Rebuilt with cloudflared
  bundled and installed silently: with forwarding off and the tunnel on "when needed", the
  installed program opened a tunnel by itself and its sign-in page answered through it; killed
  outright, it took cloudflared with it; uninstalling removed both and kept the data. The first
  look-ups of the brand-new address failed until Windows' DNS cache was cleared: a name asked for
  before Cloudflare publishes it is remembered as missing for a while, which the stable link has
  to allow for (announce an address only once it resolves). The Access page showed the switch, the
  state and the address with no overflow or console errors.

- Fixed link, live (2026-10-21, with the user's go-ahead to publish a test page): a throwaway
  site with a real quick tunnel made `https://telegra.ph/Incipit-sign-in-10-21` about 2 seconds
  after the tunnel opened; the page's one link was the tunnel address and following it reached the
  sign-in page. Switching the tunnel off and on gave a new address, and 8 seconds later the same
  page led to it. Killed outright, the site left the page on its last address (as the failure
  table says); the page was then retired with the site's own token and now reads "This link no
  longer works". Telegraph offers no deletion, so the retired page remains. Suite 2,015 passed.

- Code review (2026-10-21) of the M4b modules found and fixed six faults, each with a regression
  test: a save that failed on a later value had already changed the name, forwarding or tunnel
  live (now everything is checked and written first); a stop or a new address could wait up to
  3 minutes behind an old DNS wait (a newer update now supersedes it); stopping from the tray left
  the team page on a dead address (the tray now rewrites it, keeping its title); a stop during
  cloudflared's start-up left a second tunnel, and a replaced tunnel could erase its successor's
  address (each now ends and clears only its own); the HTTPS listener outlived a failed HTTP one
  (both now stop together); a failed move left a partial copy that blocked a retry, and a missing
  data folder was reported only to a console the windowed tray does not have (now a message box).

## Status

Complete.
- **Regression, 2026-10-21 (user report from the first install of v0.1.0):** the tray crashed at start,
  before any click, because the menu's actions were lambdas with a defaulted third parameter and
  pystray 0.19.5 allows two at most; the site it had spawned kept running headless on port 8010 and the
  setup wizard, which the tray opens, never appeared. The menu is now built by `tray.pystray_menu`
  from two-parameter closures (tested against the real pystray), a tray that fails to start stops its
  site, and the host's own browser reaches `/setup` without the token (see M2). Fixed in v0.1.2.
- **Regression, 2026-10-21 (same report):** the data folder was pointed at `C:\Program Files`, which the
  account cannot write to under a per-user install, and nothing said so; the data went to the default
  on a retry. The installer now checks both folders (`ProtectedPlace`, `Writable`) at Next and again in
  `PrepareToInstall` for silent runs, and explains what to choose; `/ALLUSERS` is allowed for the
  program's folder. The guide gained "Choosing the data folder". Shipped in v0.1.3.
