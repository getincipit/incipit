"""One permanent link for the team, though the tunnel's address changes at every start.

The host keeps a public Telegraph page (telegra.ph, free, no sign-up: the API hands out an access
token on request) whose text is the current tunnel address. The page's URL never changes, so the
team bookmarks it once and follows the link on it. Only the holder of the token, kept in the data
folder, can change the page, so nobody else can point the team somewhere else. An address is put
on the page only once public DNS answers for it, because a computer that asks for a brand-new name
too early remembers it as missing for several minutes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API = "https://api.telegra.ph"
DNS_OVER_HTTPS = "https://cloudflare-dns.com/dns-query"
STATE_NAME = "fixed-link.json"
# How long to wait for public DNS to answer for a new address before giving up on this one.
RESOLVE_SECONDS = 180


class LinkError(RuntimeError):
    """Telegraph could not be reached or refused; the message says which."""


def _call(post, method: str, **fields) -> dict:
    try:
        reply = post(f"{API}/{method}", data=fields, timeout=20)
        body = reply.json()
    except (requests.RequestException, ValueError) as exc:
        raise LinkError(f"Telegraph did not answer ({exc})") from None
    if not body.get("ok"):
        raise LinkError(f"Telegraph refused {method}: {body.get('error', 'no reason given')}")
    return body["result"]


def page_content(link: str, *, running: bool, when: str) -> str:
    """The page's text as Telegraph nodes: the link, or a note that the site is not running."""
    if running:
        nodes = [{"tag": "p", "children": ["Open the site: ", {"tag": "a", "attrs": {"href": link}, "children": [link]}]},
                 {"tag": "p", "children": [f"This address is updated each time the site starts. Last updated {when}."]},
                 {"tag": "p", "children": ["Bookmark this page, not the address above."]}]
    else:
        nodes = [{"tag": "p", "children": [f"The site is not running right now (since {when}). "
                                           "This page shows its new address as soon as it starts again."]}]
    return json.dumps(nodes)


def resolves(address: str, get=requests.get) -> bool:
    """Whether public DNS answers for the address's name, asked over HTTPS so this computer's own
    cache never records the name as missing."""
    name = address.split("//", 1)[-1].split("/", 1)[0]
    try:
        reply = get(DNS_OVER_HTTPS, params={"name": name, "type": "A"},
                    headers={"accept": "application/dns-json"}, timeout=10).json()
    except (requests.RequestException, ValueError):
        return False
    return reply.get("Status") == 0 and any(answer.get("type") == 1 for answer in reply.get("Answer", []))


class FixedLink:
    """The team's permanent page: made on first use, then rewritten whenever the address changes."""

    def __init__(self, state_dir: str | Path, title: str = "Incipit", post=None, get=None, sleep=time.sleep):
        self.state_path = Path(state_dir) / STATE_NAME
        self.title = title
        # Looked up at each call when not given, so a test can stand in for the network.
        self._post = post or (lambda *a, **k: requests.post(*a, **k))
        self._get = get or (lambda *a, **k: requests.get(*a, **k))
        self._sleep = sleep
        self.state = "waiting"
        self.error = ""
        # Guards the page writes; waiting on DNS happens outside it, so a stop never waits minutes.
        self._lock = threading.Lock()
        # Bumped by every announce and withdraw, so an older one still waiting on DNS gives up.
        self._generation = 0

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, values: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(values, indent=2), encoding="utf-8")

    @property
    def url(self) -> str:
        return self._load().get("url", "")

    def _write(self, link: str, running: bool) -> str:
        saved = self._load()
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = page_content(link, running=running, when=when)
        # The title the page was made with, so the tray (which knows no site name) keeps it.
        title = saved.get("title") or self.title
        if not saved.get("access_token"):
            account = _call(self._post, "createAccount", short_name="Incipit", author_name=title[:128])
            saved = {"access_token": account["access_token"], "title": title}
            self._save(saved)
        if saved.get("path"):
            page = _call(self._post, "editPage", access_token=saved["access_token"], path=saved["path"],
                         title=title, content=content)
        else:
            page = _call(self._post, "createPage", access_token=saved["access_token"], title=title,
                         content=content)
        saved.update(path=page["path"], url=page["url"], address=link if running else "", updated=when, title=title)
        self._save(saved)
        return page["url"]

    def announce(self, link: str) -> bool:
        """Puts `link` on the page once public DNS answers for it; False if it never did or Telegraph failed."""
        self._generation += 1
        mine = self._generation
        self.state, self.error = "resolving", ""
        deadline = time.monotonic() + RESOLVE_SECONDS
        while not resolves(link, self._get):
            if mine != self._generation:
                return False
            if time.monotonic() > deadline:
                self.state, self.error = "unresolved", "public DNS never answered for the new address"
                return False
            self._sleep(3)
        with self._lock:
            if mine != self._generation:
                return False
            try:
                self._write(link, running=True)
            except LinkError as exc:
                self.state, self.error = "failed", str(exc)
                logger.warning("the team's link was not updated: %s", exc)
                return False
            self.state = "current"
            logger.info("the team's link %s now leads to %s", self.url, link)
            return True

    def withdraw(self) -> None:
        """Says on the page that the site is not running, so the team is not sent to a dead address."""
        self._generation += 1
        with self._lock:
            if not self._load().get("path"):
                return
            try:
                self._write("", running=False)
                self.state = "waiting"
            except LinkError as exc:
                self.error = str(exc)

    def replace(self) -> None:
        """A new page for a link that was shared too widely; the old page says it no longer works."""
        with self._lock:
            saved = self._load()
            if saved.get("path"):
                try:
                    _call(self._post, "editPage", access_token=saved["access_token"], path=saved["path"],
                          title=self.title, content=json.dumps([{"tag": "p", "children": [
                              "This link no longer works. Ask the site's owner for the new one."]}]))
                except LinkError as exc:
                    logger.warning("the old link could not be retired: %s", exc)
            kept = {key: saved[key] for key in ("access_token", "title") if saved.get(key)}
            self._save(kept)

    def status(self) -> dict:
        saved = self._load()
        return {"state": self.state, "url": saved.get("url", ""), "address": saved.get("address", ""),
                "updated": saved.get("updated", ""), "error": self.error}


def recorded_url(state_dir: str | Path) -> str:
    """The permanent link as the running site recorded it, for the tray."""
    try:
        return json.loads((Path(state_dir) / STATE_NAME).read_text(encoding="utf-8")).get("url", "")
    except (OSError, ValueError):
        return ""
