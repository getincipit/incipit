"""CLI entrypoint. A client's install runs from a data folder: `incipit init-folder` makes one,
`incipit serve` serves it and `incipit tray` runs it from the system tray. `incipit init`,
`incipit review` and `incipit create-owner` take explicit paths, as a checkout does; `incipit export`
writes one folder's CSV."""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import accounts, backup, bootstrap, datafolder, db, site_settings
from .config import Settings
from .export import export_new_csv
from .review_server import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incipit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Write the reviewed documents of one folder as a CSV")
    export_parser.add_argument("--db", required=True, help="Path to the SQLite database")
    export_parser.add_argument("--folder", default=None, help="Folder id to export (default: every folder)")
    export_parser.add_argument("--output", required=True, help="Path to write the CSV")
    export_parser.add_argument("-v", "--verbose", action="store_true")

    init_parser = subparsers.add_parser("init", help="Create a new site's database, and its owner or a setup token")
    init_parser.add_argument("--db", required=True, help="Path to the SQLite database to create")
    init_parser.add_argument("--owner", default=None, help="The owner's username; without it a setup token is written")
    init_parser.add_argument("--display-name", default="", help="The owner's full name")

    owner_parser = subparsers.add_parser("create-owner", help="Create the site's owner, or reset their password")
    owner_parser.add_argument("--db", required=True, help="Path to the SQLite database")
    owner_parser.add_argument("--owner", default=None, help="The owner's username (not needed with --recover)")
    owner_parser.add_argument("--display-name", default="")
    owner_parser.add_argument("--recover", action="store_true",
                              help="Reset the existing owner's password instead of creating one")

    folder_parser = subparsers.add_parser("init-folder", help="Create a data folder with its incipit.toml")
    folder_parser.add_argument("path", help="The folder the site keeps everything in")
    folder_parser.add_argument("--port", type=int, default=None, help=f"Port (default {datafolder.DEFAULTS['server']['port']})")
    folder_parser.add_argument("--host", default=None, help="Bind address (default 0.0.0.0, every interface)")
    folder_parser.add_argument("--start-at-login", action="store_true", help="Start the site when this user logs in")
    folder_parser.add_argument("--site-name", default=None,
                               help="Announce the site on the network as NAME.local, e.g. our-archive")
    folder_parser.add_argument("--https-port", type=int, default=None,
                               help=f"HTTPS port (default {datafolder.DEFAULTS['server']['https_port']})")
    folder_parser.add_argument("--tunnel", choices=("off", "auto", "on"), default=None,
                               help="Use a Cloudflare quick tunnel: when the router cannot open the port (auto), always, or never")
    folder_parser.add_argument("--forward-port", action="store_true",
                               help="Ask the router (UPnP) to open the port to the internet while the site runs")

    restore_parser = subparsers.add_parser("restore", help="Rebuild a data folder from a backup, into a new folder")
    restore_parser.add_argument("target", help="The new, empty folder to restore into")
    restore_parser.add_argument("--from", dest="backup", required=True, help="The backup folder (where backups go)")
    restore_parser.add_argument("--snapshot", default=None, help="A snapshot's name; the newest that passes its check by default")
    restore_parser.add_argument("--use", action="store_true", help="Make the restored folder this computer's data folder")

    serve_parser = subparsers.add_parser("serve", help="Run the site from a data folder")
    serve_parser.add_argument("--data-folder", default=None,
                              help=f"The data folder (default: {datafolder.ENV_FOLDER}, or the current directory)")
    serve_parser.add_argument("-v", "--verbose", action="store_true")

    tray_parser = subparsers.add_parser("tray", help="Run the site from the system tray")
    tray_parser.add_argument("--data-folder", default=None)
    tray_parser.add_argument("--quiet", action="store_true", help="Do not open the browser (after an update)")

    bundle_parser = subparsers.add_parser("support-bundle", help="Write a support bundle (no data, keys or passwords)")
    bundle_parser.add_argument("--data-folder", default=None)
    bundle_parser.add_argument("--out", default=None, help="Where to write the zip (default: the current folder)")

    review_parser = subparsers.add_parser("review", help="Run the review site")
    review_parser.add_argument("--db", required=True, help="Path to the SQLite database")
    review_parser.add_argument("--bucket-uri", required=True, help="fsspec URI, e.g. file://./data/bucket")
    review_parser.add_argument(
        "--export-dir", default="data/exports",
        help="Root of export deliverables; each folder gets ZIP, CSV, JSON and images.",
    )
    # Backups run inside this server rather than from a scheduled task, so that they happen
    # whenever the site is up instead of whenever somebody is logged on.
    review_parser.add_argument(
        "--backup-dir", default=None,
        help="Where verified snapshots are written, every --backup-interval-minutes and only when "
        "the database has changed. Defaults to INCIPIT_BACKUP_DIR. Must be a local disk; empty "
        "disables backups and the server says so at startup.",
    )
    review_parser.add_argument(
        "--backup-mirror", default=None,
        help="Offsite copy of the backup set, typically a network share. Best effort: unreachable "
        "costs the offsite copy, never the local snapshot. Defaults to INCIPIT_BACKUP_MIRROR.",
    )
    review_parser.add_argument(
        "--backup-interval-minutes", type=float, default=backup.BACKUP_INTERVAL_MINUTES,
        help=f"How often the reviewers' work is copied to the backup drive "
             f"(default: {backup.BACKUP_INTERVAL_MINUTES:g}).",
    )
    review_parser.add_argument(
        "--snapshot-interval-minutes", type=float, default=backup.SNAPSHOT_INTERVAL_MINUTES,
        help=f"How often the database itself is snapshotted; each one is a full copy "
             f"(default: {backup.SNAPSHOT_INTERVAL_MINUTES:g}).",
    )
    review_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1, localhost only)")
    review_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    review_parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def owner_password() -> str:
    """From INCIPIT_OWNER_PASSWORD_FILE, so no password is typed into a shell history, else a prompt."""
    path = os.environ.get("INCIPIT_OWNER_PASSWORD_FILE", "")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    first = getpass.getpass("Owner password: ")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("the two passwords do not match")
    return first


def init_site(args) -> int:
    bootstrap.initialise(args.db)
    if bootstrap.has_owner(args.db):
        print("this site already has an owner; nothing to do")
        return 0
    if args.owner:
        with db.connect(args.db) as conn:
            accounts.create_owner(conn, args.owner, owner_password(), args.display_name)
        site_settings.remove_bootstrap_token(args.db)
        print(f"created the owner {args.owner}")
        return 0
    token = site_settings.write_bootstrap_token(args.db)
    print(f"no owner yet. Open /setup?token={token} on the running site to create one.")
    print(f"(the token is also in {site_settings.bootstrap_path(args.db)})")
    return 0


def create_owner(args) -> int:
    bootstrap.initialise(args.db)
    with db.connect(args.db) as conn:
        current = accounts.owner(conn)
        if args.recover:
            if current is None:
                raise SystemExit("this site has no owner to recover; run without --recover")
            accounts.set_password(conn, current["id"], owner_password())
            # A recovered owner who was deactivated could still not sign in.
            conn.execute("UPDATE users SET active = 1 WHERE id = ?", (current["id"],))
            print(f"reset the password of the owner {current['username']}")
            return 0
        if not args.owner:
            raise SystemExit("name the owner with --owner")
        accounts.create_owner(conn, args.owner, owner_password(), args.display_name)
    site_settings.remove_bootstrap_token(args.db)
    print(f"created the owner {args.owner}")
    return 0


def setup_link(port: int, token: str) -> str:
    return f"http://localhost:{port}/setup?token={token}"


def init_folder(args) -> int:
    """The installer's step: the folder, its file, its empty database and the link to the wizard."""
    try:
        path = datafolder.initialise(args.path, port=args.port, host=args.host,
                                     start_at_login=True if args.start_at_login else None,
                                     site_name=args.site_name,
                                     forward_port=True if args.forward_port else None,
                                     https_port=args.https_port, tunnel=args.tunnel)
        config = datafolder.load(path.parent)
    except datafolder.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    settings = datafolder.settings_for(path.parent, config)
    bootstrap.initialise(settings.db_path)
    # The shortcuts and the start-at-login entry find the folder through this.
    from . import relocate
    relocate.remember(path.parent)
    print(f"data folder: {path.parent}")
    if config["server"]["site_name"]:
        from . import localname
        print(f"the team will open {localname.link(config['server']['site_name'], config['server']['port'])}")
    if not bootstrap.has_owner(settings.db_path):
        token = site_settings.write_bootstrap_token(settings.db_path)
        print(f"no owner yet: once the site runs, open {setup_link(config['server']['port'], token)}")
    if args.start_at_login:
        from . import startup
        try:
            print("starts at login with: " + startup.register(path.parent))
        except RuntimeError as exc:
            print(f"could not register start at login: {exc}", file=sys.stderr)
    return 0


def _log_to(logs_dir: Path, verbose: bool) -> None:
    """Console and a rolling file in the data folder, so a client's problem has a record."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs_dir / "incipit.log", maxBytes=5_000_000,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)


def serve(args) -> int:
    """The site from its data folder: the file decides the port, the bind and every path."""
    from . import relocate
    try:
        folder = datafolder.resolve(args.data_folder)
        # A move asked for in Settings is carried out now, before anything opens the database.
        here = relocate.remembered()
        if relocate.pending() is not None and here is not None and here.resolve() == folder:
            folder = relocate.apply_pending(folder)
        config = datafolder.load(folder)
    except datafolder.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    paths = datafolder.resolved_paths(folder, config)
    _log_to(paths["logs"], args.verbose)
    settings = datafolder.settings_for(folder, config)
    bootstrap.initialise(settings.db_path)
    adopt_model_key(folder, settings)
    if not bootstrap.has_owner(settings.db_path) and not site_settings.bootstrap_path(settings.db_path).is_file():
        token = site_settings.write_bootstrap_token(settings.db_path)
        logging.getLogger(__name__).warning("no owner yet: open %s", setup_link(settings.port, token))
    restart = run_server(
        settings, str(paths["exports"]), host=settings.host, port=settings.port,
        backup_dir=str(paths["backups"]) if paths["backups"] else "",
        backup_mirror=str(paths["backup_mirror"]) if paths["backup_mirror"] else "",
        backup_status_dir=str(paths["logs"]),
    )
    if not restart:
        return 0
    if restart == "update":
        # The installer replaces the program and starts the tray again; nothing restarts here.
        return UPDATE_EXIT_CODE if os.environ.get(SUPERVISED) else 0
    # The tray starts the site again itself; run on its own, the site starts its successor.
    if os.environ.get(SUPERVISED):
        return RESTART_EXIT_CODE
    subprocess.Popen(serve_command(args), cwd=os.getcwd())
    return 0


def support_bundle(args) -> int:
    """Writes the support bundle for this computer's data folder and says where it went."""
    from . import support
    try:
        folder = datafolder.resolve(args.data_folder)
    except datafolder.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path.cwd() / support.bundle_name()
    out.write_bytes(support.build(folder))
    print(f"wrote {out}; read it before sending it to anyone")
    return 0


def restore_command(args) -> int:
    """Builds a new data folder from a backup and prints the report it wrote there."""
    from . import relocate, restore
    try:
        report = restore.restore(args.backup, args.target, args.snapshot)
    except restore.RestoreError as exc:
        print(f"restore refused: {exc}", file=sys.stderr)
        return 2
    print((Path(report["target"]) / restore.REPORT_NAME).read_text(encoding="utf-8"), end="")
    if args.use:
        relocate.remember(report["target"])
        print(f"this computer now runs the site from {report['target']}")
    return 1 if report["missing"] else 0


def adopt_model_key(folder: Path, settings) -> None:
    """A key typed into incipit.toml (or the environment) moves into the database, encrypted, and
    leaves the file, so the plain key is not copied with every backup of the folder."""
    from . import backends, db
    written = datafolder.load(folder, with_env=False)
    configured = written["model"]["openrouter_api_key"] or os.environ.get("OPENROUTER_API_KEY", "")
    with db.connect(settings.db_path) as conn:
        backends.adopt_configured_key(conn, configured)
    if written["model"]["openrouter_api_key"]:
        written["model"]["openrouter_api_key"] = ""
        datafolder.save(folder, written)
        logging.getLogger(__name__).info("moved the API key from %s into the database", datafolder.CONFIG_NAME)


# Set by the tray for the site it runs, which then hands restarts back to the tray.
SUPERVISED = "INCIPIT_SUPERVISED"
RESTART_EXIT_CODE = 75
# The site stopped for an update: the tray quits too, so the installer can replace both.
UPDATE_EXIT_CODE = 76


def serve_command(args) -> list[str]:
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "incipit.cli"]
    return [*base, "serve", *(["--data-folder", args.data_folder] if args.data_folder else [])]


def ensure_console_streams() -> None:
    """Gives a windowed program the stdout and stderr Windows withholds, which uvicorn's logging needs."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    # Before anything logs: the packaged program has no console (incipit_entry.py, PyInstaller --windowed).
    ensure_console_streams()
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-folder":
        return init_folder(args)
    if args.command == "restore":
        return restore_command(args)
    if args.command == "serve":
        return serve(args)
    if args.command == "tray":
        from . import tray
        return tray.main(args.data_folder, quiet=args.quiet)
    if args.command == "support-bundle":
        return support_bundle(args)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "export":
        count, body, _ = export_new_csv(Settings(db_path=args.db), args.folder)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(body)
        logging.getLogger(__name__).info("exported %d documents to %s", count, args.output)
        return 0

    if args.command == "init":
        return init_site(args)

    if args.command == "create-owner":
        try:
            return create_owner(args)
        except accounts.AuthError as exc:
            raise SystemExit(str(exc)) from None

    if args.command == "review":
        bootstrap.initialise(args.db)
        ready, message = bootstrap.can_start(args.db)
        if not ready:
            print(message, file=sys.stderr)
            return 2
        if message:
            logging.getLogger(__name__).warning("%s", message)
        settings = Settings(
            bucket_uri=args.bucket_uri,
            db_path=args.db,
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )
        run_server(
            settings,
            args.export_dir,
            host=args.host,
            port=args.port,
            # Resolved here rather than as an argparse default, which would be evaluated while the
            # parser is built, before load_dotenv().
            backup_dir=args.backup_dir or os.environ.get("INCIPIT_BACKUP_DIR", ""),
            backup_mirror=args.backup_mirror or os.environ.get("INCIPIT_BACKUP_MIRROR", ""),
            backup_interval_minutes=args.backup_interval_minutes,
            snapshot_interval_minutes=args.snapshot_interval_minutes,
        )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
