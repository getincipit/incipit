"""The support bundle: what someone helping with a problem needs, and nothing a client must keep private.

A zip of the versions, the settings file with any key blanked, the backup status, the recent logs
with keys, tokens and e-mail addresses masked, the last week's jobs and the model calls' totals,
and a README saying what is inside. Never the database, a scan, a transcription, a password or a
key. The owner looks through it and decides whether to send it.
"""

from __future__ import annotations

import io
import json
import platform
import re
import sqlite3
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tomli_w

from . import backup, datafolder, tunnel, updates

# How much of the newest logs goes in: enough to cover a bad day, small enough to e-mail.
LOG_BYTES = 2_000_000

SECRETS = [
    (re.compile(r"sk-or-[A-Za-z0-9-]{8,}"), "sk-or-…(masked)"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1…(masked)"),
    (re.compile(r"(?i)(token=)[A-Za-z0-9_-]{8,}"), r"\1…(masked)"),
    (re.compile(r"(?i)(api[_-]?key\W{1,3})[A-Za-z0-9._-]{8,}"), r"\1…(masked)"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "…@…(masked)"),
]

README = """Incipit support bundle

Made {made} on {host}.

Inside:
  versions.json        Incipit, Python, Windows and cloudflared versions
  incipit.toml         the site's settings, with the API key blanked
  backup-status.json   the last backup's result
  logs/                the newest log lines, with keys, tokens and e-mail addresses masked
  jobs.json            drafting and split jobs from the last week (ids and states only)
  model-calls.json     totals of the calls to the model service, by purpose, model and outcome

Not inside: the database, scans, transcriptions, people's passwords or any key. The logs can name
folders, documents and the people who signed in; read them before sending this to anyone.
"""


def scrub(text: str) -> str:
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _cloudflared_version() -> str:
    program = tunnel.find_program()
    if not program or not Path(program).is_file():
        return "not installed"
    try:
        done = subprocess.run([program, "--version"], capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (done.stdout or done.stderr).strip()[:120]
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _settings_file(folder: Path) -> str:
    try:
        config = tomllib.loads(datafolder.config_path(folder).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    if config.get("model", {}).get("openrouter_api_key"):
        config["model"]["openrouter_api_key"] = "(blanked)"
    return tomli_w.dumps(config)


def _newest_log_text(logs: Path) -> dict[str, str]:
    """The newest lines of each log, up to LOG_BYTES in all, masked."""
    files = sorted((p for p in logs.glob("*.log*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    out, budget = {}, LOG_BYTES
    for path in files:
        if budget <= 0:
            break
        data = path.read_bytes()[-budget:]
        budget -= len(data)
        out[path.name] = scrub(data.decode("utf-8", errors="replace"))
    return out


def _database_summaries(db_path: Path) -> tuple[list, list]:
    if not db_path.is_file():
        return [], []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        try:
            jobs = [dict(r) for r in conn.execute(
                "SELECT id, kind, state, total, done, created_at, started_at, finished_at, error_kind, error_text "
                "FROM jobs WHERE created_at >= ? ORDER BY id", (since,))]
        except sqlite3.Error:
            jobs = []
        try:
            calls = [dict(r) for r in conn.execute(
                "SELECT purpose, model, outcome, COUNT(*) AS calls, ROUND(SUM(cost_usd), 6) AS cost "
                "FROM model_calls GROUP BY purpose, model, outcome ORDER BY purpose, model, outcome")]
        except sqlite3.Error:
            calls = []
    finally:
        conn.close()
    for job in jobs:
        job["error_text"] = scrub(job.get("error_text") or "")
    return jobs, calls


def build(folder: str | Path) -> bytes:
    """The bundle for a data folder, as zip bytes."""
    folder = Path(folder)
    config = datafolder.load(folder)
    paths = datafolder.resolved_paths(folder, config)
    jobs, calls = _database_summaries(paths["database"])
    versions = {"incipit": updates.current_version(), "python": sys.version.split()[0],
                "system": f"{platform.system()} {platform.release()} ({platform.version()})",
                "cloudflared": _cloudflared_version()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("README.txt", README.format(made=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                                    host=platform.node() or "this computer"))
        bundle.writestr("versions.json", json.dumps(versions, indent=2))
        bundle.writestr("incipit.toml", _settings_file(folder))
        bundle.writestr("backup-status.json", json.dumps(backup.read_status(paths["logs"]) or {}, indent=2))
        for name, text in _newest_log_text(paths["logs"]).items():
            bundle.writestr(f"logs/{name}", text)
        bundle.writestr("jobs.json", json.dumps(jobs, indent=2))
        bundle.writestr("model-calls.json", json.dumps(calls, indent=2))
    return buffer.getvalue()


def bundle_name() -> str:
    return f"incipit-support-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.zip"
