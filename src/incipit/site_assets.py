"""Files an owner or admin uploads to brand the site: logo, favicon, album photographs, video,
animation and fonts. Each is checked by type and size before anything is written."""

from __future__ import annotations

import sqlite3
import io
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

from PIL import Image, UnidentifiedImageError

SCHEMA = """
-- An uploaded branding file; the bytes live in the bucket under site/, this row says what they are.
CREATE TABLE IF NOT EXISTS site_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('logo', 'favicon', 'album_image', 'video', 'animation', 'font')),
    bucket_key   TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER,
    bytes        INTEGER NOT NULL,
    caption      TEXT NOT NULL DEFAULT '',
    credit       TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL
);
"""

PREFIX = "site"
KINDS = ("logo", "favicon", "album_image", "video", "animation", "font")
MB = 1024 * 1024
LIMITS = {"logo": 15 * MB, "favicon": 2 * MB, "album_image": 15 * MB, "video": 50 * MB,
          "animation": 2 * MB, "font": 2 * MB}
RASTER = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}
# Large enough for a hero photograph at twice a laptop's width, small enough to load quickly.
ALBUM_LONG_EDGE = 3200
MAX_PIXELS = 60_000_000


class AssetError(ValueError):
    """An upload the person can fix: the wrong kind of file, or too large."""


@dataclass
class Prepared:
    content: bytes
    content_type: str
    extension: str
    width: int | None = None
    height: int | None = None
    removed: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_assets(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------- checking a file ----------


def _raster(content: bytes, kind: str) -> Prepared:
    """Re-encoded, so nothing but pixels survives: no metadata, no trailing payload."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format not in RASTER:
                raise AssetError("images must be PNG, JPEG, WebP or SVG")
            if img.width * img.height > MAX_PIXELS:
                raise AssetError("that image has too many pixels; keep it under 60 megapixels")
            img.load()
            fmt = img.format
            if kind == "album_image" and max(img.size) > ALBUM_LONG_EDGE:
                img.thumbnail((ALBUM_LONG_EDGE, ALBUM_LONG_EDGE))
            keep_alpha = fmt in ("PNG", "WEBP") and img.mode in ("RGBA", "LA", "P")
            clean = img.convert("RGBA" if keep_alpha else "RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise AssetError(f"that file is not an image this site can use ({exc})") from None
    buffer = io.BytesIO()
    save = {"PNG": {"format": "PNG", "optimize": True}, "JPEG": {"format": "JPEG", "quality": 88},
            "WEBP": {"format": "WEBP", "quality": 88}}[fmt]
    clean.save(buffer, **save)
    content_type, extension = RASTER[fmt]
    return Prepared(buffer.getvalue(), content_type, extension, clean.width, clean.height)


_SVG = "{http://www.w3.org/2000/svg}"
_XLINK = "{http://www.w3.org/1999/xlink}href"
_FORBIDDEN_TAGS = {"script", "foreignObject", "iframe", "embed", "object", "handler", "listener"}


def sanitize_svg(content: bytes) -> tuple[bytes, list[str]]:
    """The SVG without scripts, event handlers, foreign content or references off the page."""
    text = content.decode("utf-8", errors="strict") if isinstance(content, bytes) else content
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise AssetError("SVG files with a DOCTYPE or entities are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AssetError(f"that SVG could not be read ({exc})") from None
    if root.tag not in (f"{_SVG}svg", "svg"):
        raise AssetError("that file is not an SVG image")
    removed: list[str] = []
    for parent in list(root.iter()):
        for child in list(parent):
            local = child.tag.split("}")[-1]
            if local in _FORBIDDEN_TAGS or not child.tag.startswith(_SVG):
                parent.remove(child)
                removed.append(f"<{local}>")
    for element in root.iter():
        for name in list(element.attrib):
            local = name.split("}")[-1]
            value = element.attrib[name].strip()
            if local.lower().startswith("on"):
                del element.attrib[name]
                removed.append(f"{local}=")
            elif (name == _XLINK or local == "href") and not value.startswith("#"):
                del element.attrib[name]
                removed.append(f"href to {value[:40]}")
            elif "url(" in value and not re.fullmatch(r"url\(#[\w-]+\)", value):
                del element.attrib[name]
                removed.append(f"{local}= url")
    ET.register_namespace("", _SVG[1:-1])
    return ET.tostring(root, encoding="utf-8"), removed


def _svg(content: bytes) -> Prepared:
    clean, removed = sanitize_svg(content)
    return Prepared(clean, "image/svg+xml", "svg", removed=tuple(removed))


def _video(content: bytes) -> Prepared:
    if content[4:8] == b"ftyp":
        return Prepared(content, "video/mp4", "mp4")
    if content[:4] == b"\x1a\x45\xdf\xa3":
        return Prepared(content, "video/webm", "webm")
    raise AssetError("videos must be MP4 or WebM")


def _animation(content: bytes, filename: str) -> Prepared:
    """An animated SVG: its CSS and SMIL motion play inside an <img>, where no script can run."""
    if not (filename.lower().endswith(".svg") or content.lstrip()[:5] in (b"<?xml", b"<svg ", b"<svg>")):
        raise AssetError("animations must be animated SVG files")
    return _svg(content)


def _font(content: bytes) -> Prepared:
    if content[:4] != b"wOF2":
        raise AssetError("fonts must be WOFF2 files")
    return Prepared(content, "font/woff2", "woff2")


def prepare(kind: str, filename: str, content: bytes) -> Prepared:
    """The checked, cleaned file, or AssetError saying what is wrong; nothing is written here."""
    if kind not in KINDS:
        raise AssetError(f"unknown kind of file {kind!r}")
    if not content:
        raise AssetError("that file is empty")
    if len(content) > LIMITS[kind]:
        raise AssetError(f"that file is {len(content) / MB:.1f} MB; the limit for a "
                         f"{kind.replace('_', ' ')} is {LIMITS[kind] // MB} MB")
    if kind == "video":
        return _video(content)
    if kind == "animation":
        return _animation(content, filename)
    if kind == "font":
        return _font(content)
    if (filename or "").lower().endswith(".svg") or content.lstrip()[:4] in (b"<?xm", b"<svg"):
        if kind == "album_image":
            raise AssetError("album photographs must be PNG, JPEG or WebP")
        return _svg(content)
    return _raster(content, kind)


# ---------- storing and listing ----------


def store(conn: sqlite3.Connection, storage, kind: str, filename: str, content: bytes,
          created_by: int | None) -> dict:
    """Checks, writes the bytes, then records the row; a failed write leaves no row behind."""
    prepared = prepare(kind, filename, content)
    key = f"{PREFIX}/{kind}/{secrets.token_hex(8)}.{prepared.extension}"
    try:
        storage.write_bytes(key, prepared.content)
    except OSError as exc:
        raise AssetError(f"the file could not be stored; nothing changed ({exc})") from None
    position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM site_assets WHERE kind = ?",
                            (kind,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO site_assets (kind, bucket_key, content_type, width, height, bytes, position, "
        "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, key, prepared.content_type, prepared.width, prepared.height, len(prepared.content),
         position, created_by, _now()))
    return {**get(conn, cur.lastrowid), "removed": list(prepared.removed)}


def _public(row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "url": f"/site/assets/{row['id']}",
            "content_type": row["content_type"], "width": row["width"], "height": row["height"],
            "bytes": row["bytes"], "caption": row["caption"], "credit": row["credit"],
            "position": row["position"]}


def get(conn: sqlite3.Connection, asset_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM site_assets WHERE id = ?", (asset_id,)).fetchone()
    return _public(row) if row is not None else None


def row(conn: sqlite3.Connection, asset_id: int):
    return conn.execute("SELECT * FROM site_assets WHERE id = ?", (asset_id,)).fetchone()


def listing(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM site_assets" + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, position, id",
        (kind,) if kind else ()).fetchall()
    return [_public(r) for r in rows]


def describe(conn: sqlite3.Connection, asset_id: int, caption: str | None, credit: str | None) -> dict:
    if get(conn, asset_id) is None:
        raise AssetError("no such file")
    for column, value in (("caption", caption), ("credit", credit)):
        if value is not None:
            text = " ".join(str(value).split())
            if len(text) > 200:
                raise AssetError(f"a {column} is at most 200 characters")
            conn.execute(f"UPDATE site_assets SET {column} = ? WHERE id = ?", (text, asset_id))
    return get(conn, asset_id)


def reorder(conn: sqlite3.Connection, ordered_ids: list[int]) -> None:
    for position, asset_id in enumerate(ordered_ids):
        conn.execute("UPDATE site_assets SET position = ? WHERE id = ?", (position, int(asset_id)))


def remove(conn: sqlite3.Connection, storage, asset_id: int) -> None:
    found = row(conn, asset_id)
    if found is None:
        raise AssetError("no such file")
    conn.execute("DELETE FROM site_assets WHERE id = ?", (asset_id,))
    storage.delete_keys([found["bucket_key"]])
