"""Bringing a site into being: the schema, the site row, and the right to create its owner."""

from __future__ import annotations

from pathlib import Path

from . import accounts, db, site_settings


def initialise(db_path: str | Path) -> None:
    """Every table the site needs, created or migrated in place; safe to run on every start."""
    db.init_db(str(db_path))


def has_owner(db_path: str | Path) -> bool:
    with db.connect(str(db_path)) as conn:
        return accounts.owner_exists(conn)


def can_start(db_path: str | Path) -> tuple[bool, str]:
    """Whether a server may start: it needs an owner, or a bootstrap token to create one with."""
    if has_owner(db_path):
        return True, ""
    if site_settings.bootstrap_path(db_path).is_file():
        return True, "no owner yet: open /setup with the bootstrap token to create one"
    return False, ("this site has no owner and no bootstrap token; run "
                   "`incipit init --db <path>` first")
