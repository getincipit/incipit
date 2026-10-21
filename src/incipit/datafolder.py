"""The client's data folder: one `incipit.toml` and the files beside it, which every command reads.

The folder is what the installer asks for and what a backup copies. Its configuration file holds
the few settings a client changes (the port, whether the site starts at login, where things are);
an environment variable still overrides a single value for development.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

import tomli_w

from .config import Settings

CONFIG_NAME = "incipit.toml"
ENV_FOLDER = "INCIPIT_DATA_DIR"

DEFAULTS = {
    # Every interface, so the team can reach the host; 127.0.0.1 keeps the site to this computer.
    # site_name is announced as {site_name}.local on the network; empty announces nothing.
    # https_port serves the same site over HTTPS with its own certificate; forward_port asks the router
    # (UPnP) to open that port, and only that one, to the internet while the site runs.
    # tunnel: "auto" opens a Cloudflare quick tunnel when the router cannot give an internet address,
    # "on" always, "off" never.
    "server": {"host": "0.0.0.0", "port": 8010, "https_port": 8443, "start_at_login": False, "site_name": "",
               "forward_port": False, "tunnel": "off", "fixed_link": True},
    # Relative to the data folder unless absolute; an empty mirror means no offsite copy.
    "paths": {"database": "incipit.db", "bucket": "bucket", "exports": "exports", "backups": "backups",
              "backup_mirror": "", "logs": "logs", "tls": "tls", "tunnel": "tunnel"},
    "model": {"openrouter_api_key": ""},
}
# The directories a fresh folder gets, so the first start finds every place it writes to.
LAYOUT = ("bucket", "exports", "backups", "logs")
# Single values an environment variable may override, for a developer's shell.
ENV_OVERRIDES = {("server", "host"): "INCIPIT_HOST", ("server", "port"): "INCIPIT_PORT",
                 ("paths", "backups"): "INCIPIT_BACKUP_DIR", ("paths", "backup_mirror"): "INCIPIT_BACKUP_MIRROR",
                 ("model", "openrouter_api_key"): "OPENROUTER_API_KEY"}


class ConfigError(ValueError):
    """A folder or a file the person can see and fix; the message says what and where."""


def config_path(folder: str | Path) -> Path:
    return Path(folder) / CONFIG_NAME


def resolve(argument: str | None = None) -> Path:
    """The data folder in use: the argument, else INCIPIT_DATA_DIR, else a current directory holding the
    file, else the folder this computer's location file names."""
    from . import relocate
    for candidate in (argument, os.environ.get(ENV_FOLDER)):
        if candidate:
            folder = Path(candidate).expanduser()
            if not config_path(folder).is_file():
                raise ConfigError(f"{folder} holds no {CONFIG_NAME}; make it with `incipit init-folder {folder}`")
            return relocate.forwarded(folder.resolve())
    here = Path.cwd()
    if config_path(here).is_file():
        return relocate.forwarded(here)
    # Where the installer (or a move from Settings) said this computer's site lives.
    remembered = relocate.remembered()
    if remembered is not None:
        if not config_path(remembered).is_file():
            raise ConfigError(f"the data folder {remembered} is missing or holds no {CONFIG_NAME}; reconnect the "
                              f"drive it is on, or run `incipit init-folder` to make a new one")
        return remembered
    raise ConfigError(f"no data folder named: pass --data-folder, set {ENV_FOLDER}, or run inside a folder "
                      f"holding {CONFIG_NAME}")


def initialise(folder: str | Path, **values) -> Path:
    """Creates the folder, its layout and its file (with any given [server] values); leaves what exists alone."""
    folder = Path(folder).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in LAYOUT:
            (folder / name).mkdir(exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ConfigError(f"{folder} cannot be written to ({exc.strerror or exc}); choose another folder") from exc
    path = config_path(folder)
    if not path.exists():
        config = copy.deepcopy(DEFAULTS)
        for key, value in values.items():
            if value is not None:
                config["server"][key] = value
        validate(config, path)
        path.write_text(tomli_w.dumps(config), encoding="utf-8")
    return path


def load(folder: str | Path, with_env: bool = True) -> dict:
    """The file's values over the defaults, checked, with the environment's overrides applied last."""
    path = config_path(folder)
    try:
        given = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} does not exist") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    config = copy.deepcopy(DEFAULTS)
    for section, values in given.items():
        if section not in config or not isinstance(values, dict):
            raise ConfigError(f"{path}: there is no section [{section}]")
        for key, value in values.items():
            if key not in config[section]:
                raise ConfigError(f"{path}: there is no setting {key!r} in [{section}]")
            config[section][key] = value
    if with_env:
        for (section, key), variable in ENV_OVERRIDES.items():
            if os.environ.get(variable):
                config[section][key] = os.environ[variable]
    validate(config, path)
    return config


def validate(config: dict, path: Path) -> None:
    server, paths = config["server"], config["paths"]
    try:
        port = int(server["port"])
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigError(f"{path}: [server].port must be a whole number from 1 to 65535 (got {server['port']!r}); "
                          f"the default is {DEFAULTS['server']['port']}")
    server["port"] = port
    try:
        https_port = int(server["https_port"])
    except (TypeError, ValueError):
        https_port = 0
    if not 1 <= https_port <= 65535 or https_port == port:
        raise ConfigError(f"{path}: [server].https_port must be a whole number from 1 to 65535 other than the port "
                          f"(got {server['https_port']!r}); the default is {DEFAULTS['server']['https_port']}")
    server["https_port"] = https_port
    if not isinstance(server["host"], str) or not server["host"].strip():
        raise ConfigError(f"{path}: [server].host must be an address such as 0.0.0.0 or 127.0.0.1")
    for key in ("start_at_login", "forward_port", "fixed_link"):
        if isinstance(server[key], str):
            server[key] = server[key].strip().lower() in ("1", "true", "yes", "on")
        server[key] = bool(server[key])
    if server["site_name"]:
        from . import localname
        try:
            server["site_name"] = localname.normalise(server["site_name"])
        except ValueError as exc:
            raise ConfigError(f"{path}: [server].site_name: {exc}") from None
    server["tunnel"] = str(server["tunnel"]).strip().lower()
    if server["tunnel"] not in ("off", "auto", "on"):
        raise ConfigError(f"{path}: [server].tunnel must be \"off\", \"auto\" or \"on\" (got {server['tunnel']!r})")
    for key in ("database", "bucket", "exports", "backups", "logs", "tls", "tunnel"):
        if not isinstance(paths[key], str) or not paths[key].strip():
            raise ConfigError(f"{path}: [paths].{key} must be a path")
    if not isinstance(paths["backup_mirror"], str):
        raise ConfigError(f"{path}: [paths].backup_mirror must be a path or empty")


def save(folder: str | Path, config: dict) -> None:
    path = config_path(folder)
    validate(config, path)
    path.write_text(tomli_w.dumps(config), encoding="utf-8")


def update(folder: str | Path, **server_values) -> dict:
    """Changes [server] values in the file (the port, start at login) and returns the new configuration."""
    config = load(folder, with_env=False)
    for key, value in server_values.items():
        if key not in DEFAULTS["server"]:
            raise ConfigError(f"there is no setting {key!r} in [server]")
        config["server"][key] = value
    save(folder, config)
    return config


def resolved_paths(folder: str | Path, config: dict) -> dict[str, Path | None]:
    """Every path the server writes to, absolute, inside the folder unless the file said otherwise."""
    folder = Path(folder)

    def inside(value: str):
        return (folder / value).resolve() if value and not Path(value).is_absolute() else (Path(value) if value else None)
    return {key: inside(value) for key, value in config["paths"].items()}


def settings_for(folder: str | Path, config: dict) -> Settings:
    """The Settings the server runs with, from a folder and its configuration."""
    paths = resolved_paths(folder, config)
    # Not as_uri(): it writes a space as %20, which the file store took literally, putting the scans
    # of a folder such as "Incipit Data" in a sibling folder named "Incipit%20Data".
    return Settings(db_path=str(paths["database"]), bucket_uri=f"file://{paths['bucket'].as_posix()}",
                    openrouter_api_key=config["model"]["openrouter_api_key"],
                    data_folder=str(Path(folder).resolve()), host=config["server"]["host"],
                    port=config["server"]["port"], site_name=config["server"]["site_name"],
                    forward_port=config["server"]["forward_port"], https_port=config["server"]["https_port"],
                    tls_dir=str(paths["tls"]), tunnel=config["server"]["tunnel"],
                    tunnel_dir=str(paths["tunnel"]), fixed_link=config["server"]["fixed_link"])
