"""The model service the site drafts with: OpenRouter, or an OpenAI-compatible server of the client's own.

One backend is active. Its API key is stored encrypted (keyvault) and never leaves the server; the
running Settings object is updated from it at start and whenever the owner changes it, so the next
call uses the new key or model without a restart.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

import requests

from . import keyvault
from .config import DEFAULT_OPENROUTER_FALLBACK_MODEL, DEFAULT_OPENROUTER_MODEL, OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

KINDS = ("openrouter", "openai_compatible")

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_backends (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kind TEXT NOT NULL DEFAULT 'openrouter' CHECK (kind IN ('openrouter', 'openai_compatible')),
    base_url TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    key_hint TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    fallback_model TEXT NOT NULL DEFAULT '',
    last_tested_at TEXT,
    last_test_ok INTEGER,
    last_test_detail TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""


def init_backends(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO model_backends (id, model, fallback_model) VALUES (1, ?, ?) ON CONFLICT DO NOTHING",
                 (DEFAULT_OPENROUTER_MODEL, DEFAULT_OPENROUTER_FALLBACK_MODEL))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT * FROM model_backends WHERE id = 1").fetchone())


def base_url(backend: dict) -> str:
    if backend["kind"] == "openrouter":
        return OPENROUTER_BASE_URL
    return backend["base_url"].rstrip("/")


def api_key(backend: dict) -> tuple[str, str]:
    """The decrypted key and, when it cannot be read on this computer, why."""
    if not backend["api_key_enc"]:
        return "", ""
    try:
        return keyvault.decrypt(backend["api_key_enc"]), ""
    except keyvault.Unreadable as exc:
        return "", f"The saved key cannot be read on this computer ({exc}); enter it again."


def public(conn: sqlite3.Connection) -> dict:
    """The backend as the Models page shows it: never the key, only its hint and whether it can be read."""
    backend = get(conn)
    key, problem = api_key(backend)
    return {
        "kind": backend["kind"],
        "base_url": backend["base_url"],
        "model": backend["model"],
        "fallback_model": backend["fallback_model"],
        "has_key": bool(key),
        "key_hint": backend["key_hint"] if key else "",
        "key_problem": problem,
        "last_test": {"at": backend["last_tested_at"], "ok": None if backend["last_test_ok"] is None
                      else bool(backend["last_test_ok"]), "detail": backend["last_test_detail"]},
    }


def save(conn: sqlite3.Connection, actor: str, **changes) -> None:
    """Applies the owner's changes; an api_key is encrypted on the way in, an empty one removes the key."""
    current = get(conn)
    if "kind" in changes and changes["kind"] not in KINDS:
        raise ValueError("the service is openrouter or openai_compatible")
    kind = changes.get("kind", current["kind"])
    url = changes.get("base_url", current["base_url"]).strip()
    if kind == "openai_compatible" and not url.startswith(("http://", "https://")):
        raise ValueError("give the server's address, such as http://192.168.1.50:8000/v1")
    values = {"kind": kind, "base_url": url}
    for key in ("model", "fallback_model"):
        if key in changes:
            values[key] = str(changes[key] or "").strip()
    if "model" in values and not values["model"]:
        raise ValueError("choose a model to draft with")
    # A key belongs to the service it was made for: a new service or address without a new key
    # drops the old one, so it is never sent to a server it was not meant for.
    moved = kind != current["kind"] or (kind == "openai_compatible" and url != current["base_url"])
    if moved and "api_key" not in changes:
        changes = {**changes, "api_key": ""}
    if "api_key" in changes:
        secret = str(changes["api_key"] or "").strip()
        values["api_key_enc"] = keyvault.encrypt(secret) if secret else ""
        values["key_hint"] = keyvault.hint(secret) if secret else ""
        # A new key has not been tested yet.
        values.update(last_tested_at=None, last_test_ok=None, last_test_detail="")
    values.update(updated_by=actor, updated_at=_now())
    conn.execute(f"UPDATE model_backends SET {', '.join(f'{k} = ?' for k in values)} WHERE id = 1",
                 list(values.values()))


def apply(conn: sqlite3.Connection, settings) -> None:
    """Points the running Settings at the active backend."""
    backend = get(conn)
    key, _ = api_key(backend)
    settings.openrouter_api_key = key
    settings.openrouter_base_url = base_url(backend)
    settings.openrouter_model = backend["model"] or DEFAULT_OPENROUTER_MODEL
    settings.openrouter_fallback_model = backend["fallback_model"]


def adopt_configured_key(conn: sqlite3.Connection, configured: str) -> bool:
    """Moves a key from incipit.toml or the environment into the database, when none is stored yet."""
    if not configured or get(conn)["api_key_enc"]:
        return False
    save(conn, "incipit.toml", api_key=configured)
    return True


# ---------- testing the connection, free of charge ----------

def test(conn: sqlite3.Connection, http_get=None) -> dict:
    """Checks the key and the server without a chargeable call, and records the answer."""
    http_get = http_get or (lambda *a, **k: requests.get(*a, **k))
    backend = get(conn)
    key, problem = api_key(backend)
    if problem:
        result = {"ok": False, "detail": problem}
    elif backend["kind"] == "openrouter" and not key:
        result = {"ok": False, "detail": "Enter an OpenRouter API key first."}
    else:
        result = _probe(backend, key, http_get)
    conn.execute("UPDATE model_backends SET last_tested_at = ?, last_test_ok = ?, last_test_detail = ? WHERE id = 1",
                 (_now(), int(result["ok"]), result["detail"]))
    return result


def _probe(backend: dict, key: str, http_get) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        if backend["kind"] == "openrouter":
            reply = http_get(f"{OPENROUTER_BASE_URL}/key", headers=headers, timeout=15)
            if reply.status_code in (401, 403):
                return {"ok": False, "detail": f"OpenRouter refused the key ({reply.status_code})."}
            reply.raise_for_status()
            data = reply.json().get("data", {})
            remaining = data.get("limit_remaining")
            used = data.get("usage")
            detail = "The key works."
            if remaining is not None:
                detail += f" ${remaining:.2f} left of its limit."
            elif used is not None:
                detail += f" ${used:.2f} used so far on OpenRouter."
            return {"ok": True, "detail": detail, "free_tier": bool(data.get("is_free_tier"))}
        reply = http_get(f"{base_url(backend)}/models", headers=headers, timeout=15)
        if reply.status_code in (401, 403):
            return {"ok": False, "detail": f"The server refused the key ({reply.status_code})."}
        reply.raise_for_status()
        names = [m.get("id") for m in reply.json().get("data", []) if m.get("id")]
        return {"ok": True, "detail": f"The server answered with {len(names)} model(s).", "models": names}
    except requests.RequestException as exc:
        where = OPENROUTER_BASE_URL if backend["kind"] == "openrouter" else base_url(backend)
        return {"ok": False, "detail": f"Could not reach {where} ({exc.__class__.__name__})."}
    except ValueError:
        return {"ok": False, "detail": "The server's answer was not what a model service sends."}


# ---------- the catalogue the model picker shows ----------

_catalogue: dict = {"at": 0.0, "models": []}
CATALOGUE_SECONDS = 3600


def catalogue(http_get=None) -> list[dict]:
    """OpenRouter's models that read images, with their prices per million tokens, free ones first."""
    http_get = http_get or (lambda *a, **k: requests.get(*a, **k))
    if time.monotonic() - _catalogue["at"] < CATALOGUE_SECONDS and _catalogue["models"]:
        return _catalogue["models"]
    reply = http_get(f"{OPENROUTER_BASE_URL}/models", timeout=20)
    reply.raise_for_status()
    models = []
    for item in reply.json().get("data", []):
        if "image" not in (item.get("architecture") or {}).get("input_modalities", []):
            continue
        pricing = item.get("pricing") or {}
        try:
            prompt, completion = float(pricing.get("prompt") or 0), float(pricing.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        models.append({"id": item["id"], "name": item.get("name") or item["id"],
                       "input_per_million": round(prompt * 1_000_000, 4),
                       "output_per_million": round(completion * 1_000_000, 4),
                       "free": prompt == 0 and completion == 0,
                       "context": item.get("context_length")})
    models.sort(key=lambda m: (not m["free"], m["input_per_million"] + m["output_per_million"], m["id"]))
    _catalogue.update(at=time.monotonic(), models=models)
    return models
