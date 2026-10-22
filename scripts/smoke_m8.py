#!/usr/bin/env python
"""M8 smoke: the download page opened in a real browser, at phone and desktop widths, light and dark.

Serves site/ locally and checks with Playwright: no horizontal overflow, no console errors, every
image loads, the download buttons lead to the newest release, the estimator answers when its numbers
change, the name section is there, and with reduced motion every section shows without scrolling.

    .venv/Scripts/python.exe scripts/smoke_m8.py
"""

from __future__ import annotations

import functools
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_m0 import Checks  # noqa: E402


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> int:
    from playwright.sync_api import sync_playwright
    checks = Checks()
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "site")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in ("light", "dark"):
            for width, height in ((375, 812), (1440, 900)):
                tag = f"{width}px {theme}"
                context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
                page = context.new_page()
                errors = []
                page.on("console", lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.goto(url, wait_until="networkidle")
                height_total = page.evaluate("document.body.scrollHeight")
                for y in range(0, height_total, 600):
                    page.evaluate(f"window.scrollTo(0, {y})")
                    time.sleep(0.04)
                page.wait_for_load_state("networkidle")
                time.sleep(0.8)
                checks.ok(f"1 {tag}: no horizontal overflow",
                          page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0)
                broken = page.evaluate("[...document.images].filter((i) => i.complete && i.naturalWidth === 0).map((i) => i.currentSrc || i.src)")
                checks.ok(f"1 {tag}: every image loads", broken == [], str(broken)[:200])
                checks.ok(f"1 {tag}: no console errors", errors == [], str(errors)[:200])
                if width == 1440 and theme == "light":
                    hrefs = page.eval_on_selector_all("[data-download]", "els => els.map((a) => a.href)")
                    checks.ok("2 every download button leads to the newest release",
                              hrefs and all("github.com/getincipit/incipit/releases" in h for h in hrefs), str(hrefs)[:200])
                    before = page.inner_text('[data-out="saved"]')
                    page.fill('#estimator [name="documents"]', "2000")
                    time.sleep(0.2)
                    after = page.inner_text('[data-out="saved"]')
                    checks.ok("3 the estimator answers when its numbers change", before != after and "hours" in after,
                              f"{before} -> {after}")
                    checks.ok("4 the name section says where the name comes from",
                              "here begins" in page.inner_text("#name"))
                    # The hero: the recording of the real review screen plays, and the letter is credited.
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_function("document.querySelector('.hero-video').readyState >= 2", timeout=20000)
                    before = page.evaluate("document.querySelector('.hero-video').currentTime")
                    time.sleep(1.5)
                    after = page.evaluate("document.querySelector('.hero-video').currentTime")
                    checks.ok("6 the hero's recording plays and the letter is credited",
                              after > before and page.eval_on_selector_all("[data-credits] a", "els => els.length") == 1)
                context.close()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        hidden = page.evaluate("[...document.querySelectorAll('.reveal, .rise, .rise-late')].filter((el) => getComputedStyle(el).opacity !== '1').length")
        checks.ok("5 with reduced motion every section shows without scrolling", hidden == 0, f"{hidden} hidden")
        checks.ok("5 with reduced motion the recording stands still on its poster",
                  page.evaluate("const v = document.querySelector('.hero-video'); v.paused && !v.hasAttribute('autoplay')"))
        context.close()
        browser.close()
    server.shutdown()
    print(f"\n{'all passed' if not checks.failed else f'{checks.failed} check(s) failed'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
