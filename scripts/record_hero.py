#!/usr/bin/env python
"""Records the download page's hero: the real review screen, driven in a real browser, as an owner
opens a folder, opens a letter, corrects a field and marks it reviewed.

The site is the same demo capture_screens.py builds, with one addition: a real page from the Thomas
A. Edison Papers (site/documents/letter.webp, CC0) uploaded as its own folder and drafted from a
transcription made by hand, so what the viewer watches is the program at work on a real document.
Writes site/hero.mp4, site/hero.webm and site/screens/hero-poster-dark.webp.

    .venv/Scripts/python.exe scripts/record_hero.py
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import capture_screens as demo  # noqa: E402

SITE = ROOT / "site"
WIDTH, HEIGHT = 1440, 900

# Mary Emily Miller to Mina Miller Edison, 5 March 1911 (X018C9-0915), read from the scan by hand.
MILLER = {
    "type": "Letter", "date": "1911-03-05", "author": "Mary Emily Miller",
    "recipient": "Edison, Mina Miller", "place": "Battle Creek, Mich.", "written_at": "",
    "places": ["Battle Creek, Mich.", "Washington, D.C.", "South Carolina"], "subjects": ["Family", "Health", "Travel"],
    "challenges": ["handwritten"], "uncertain": ["Carlisle"],
    "text": ("Mina darling—\n\nThank you for your lovely letters and the invitations. I know they are real, "
             "that you really want me to come. I am much better now. I am going to Battle Creek Sunday. Lewis goes "
             "with me. I'll stay a couple of weeks. My doctor wants me to come. Then I may go on the 22nd to be with "
             "Rachel & Lewis in Washington, but I'm not sure— down to see Mrs. H. in South Carolina— and out "
             "to see Anne Carlisle— the last two also just invitations to"),
}

# A cursor the recording can show: headless Chromium draws none of its own.
CURSOR = """
(() => {
  const dot = document.createElement("div");
  dot.id = "demo-cursor";
  dot.style.cssText = "position:fixed;left:-40px;top:-40px;width:18px;height:18px;border-radius:50%;" +
    "background:rgba(255,255,255,.92);box-shadow:0 0 0 2px rgba(0,0,0,.55),0 2px 10px rgba(0,0,0,.5);" +
    "pointer-events:none;z-index:99999;transform:translate(-50%,-50%);transition:transform .12s ease";
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(dot));
  document.addEventListener("mousemove", (e) => { dot.style.left = e.clientX + "px"; dot.style.top = e.clientY + "px"; });
  document.addEventListener("mousedown", () => { dot.style.transform = "translate(-50%,-50%) scale(.7)"; });
  document.addEventListener("mouseup", () => { dot.style.transform = "translate(-50%,-50%)"; });
})();
"""


def letter_jpeg() -> bytes:
    image = Image.open(SITE / "documents" / "letter.webp").convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    return out.getvalue()


def glide(page, locator, steps: int = 28) -> None:
    """Moves the cursor to the middle of `locator` the way a hand would, then rests a moment."""
    box = locator.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
    time.sleep(0.35)


def record(base: str, folder_id: str, created: list[str], token: str, swaps: dict) -> None:
    from playwright.sync_api import sync_playwright
    owner = requests.Session()
    owner.post(f"{base}/login", data={"username": demo.OWNER[0], "password": demo.OWNER[1]}, allow_redirects=False)
    for step in ("site", "prompt", "model", "tabs", "people", "access"):
        owner.post(f"{base}/api/setup/steps/{step}", json={"done": True})
    owner.post(f"{base}/api/setup/finish")

    # The real letter, as its own folder, drafted from the hand-made transcription.
    demo.FakeModel.drafts = [MILLER]
    batch = owner.post(f"{base}/api/new/batches", files=[("files", ("X018C9_0915.jpg", letter_jpeg(), "image/jpeg"))],
                       data={"folder_display_id": "X018C9"}).json()
    owner.post(f"{base}/api/new/batches/{batch['batch_id']}/retry-proposal")
    confirmed = owner.post(f"{base}/api/new/batches/{batch['batch_id']}/confirm",
                           json={"groups": [{"display_id": "X018C9-0915", "sequence_indices": [0]}]})
    if confirmed.status_code != 200:
        raise SystemExit(f"the letter's folder could not be made: {confirmed.text[:200]}")
    letter_id = confirmed.json()["created"][0]
    for _ in range(100):
        if owner.get(f"{base}/api/new/documents/{letter_id}").json().get("transcription", {}).get("text"):
            break
        time.sleep(0.3)

    with tempfile.TemporaryDirectory(prefix="incipit-hero-", ignore_cleanup_errors=True) as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, color_scheme="dark",
                                      record_video_dir=tmp, record_video_size={"width": WIDTH, "height": HEIGHT})
        context.add_init_script("try { localStorage.setItem('incipit-theme', 'dark'); } catch (e) {}")
        context.add_init_script(CURSOR)
        cookie = owner.cookies.get("incipit_session")
        context.add_cookies([{"name": "incipit_session", "value": cookie, "url": base}])
        page = context.new_page()

        def check():
            left = page.evaluate(demo.LEFT, list(swaps))
            if left:
                raise SystemExit(f"refusing to record a screen that shows {left}")

        page.goto(f"{base}/#documents", wait_until="networkidle")
        page.mouse.move(WIDTH * 0.55, HEIGHT * 0.9)
        time.sleep(1.6)
        check()
        # Scoped to the queue: the Team tab keeps its own folder cards in the page, hidden.
        card = page.locator("#queue-view .folder-card", has_text="X018C9").first
        glide(page, card)
        card.click()
        time.sleep(1.8)
        check()
        row = page.locator("#queue-view").get_by_text("Letter from Mary Emily Miller").first
        glide(page, row)
        row.click()
        page.wait_for_selector("#field-input-author", timeout=15000)
        time.sleep(2.2)
        check()
        # The draft wrote the name as the page has it; the reviewer puts it the way the index wants it.
        author = page.locator("#field-input-author")
        glide(page, author)
        author.click()
        page.keyboard.press("Control+A")
        time.sleep(0.3)
        page.keyboard.type("Miller, Mary Emily", delay=70)
        time.sleep(0.9)
        # A glance over the draft before deciding; a visible element only, since hidden ones have no box.
        text = page.locator("#detail-view").get_by_text("Mina darling").first
        if text.count() and text.is_visible():
            glide(page, text, steps=20)
            time.sleep(0.8)
        accept = page.locator("#btn-accept")
        glide(page, accept)
        accept.click()
        # The folder's only document is now reviewed, so the program offers the export; not today.
        later = page.get_by_role("button", name="Not yet")
        later.wait_for(timeout=10000)
        time.sleep(1.6)
        glide(page, later)
        later.click()
        time.sleep(2.2)
        check()
        poster = page.screenshot()
        time.sleep(0.6)
        video = page.video
        context.close()
        source = Path(video.path())
        browser.close()

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SystemExit("ffmpeg is needed to encode the recording (winget install Gyan.FFmpeg)")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-an", "-vf", "scale=1440:-2",
                        "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        str(SITE / "hero.mp4")], check=True)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-an", "-vf", "scale=1440:-2",
                        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-row-mt", "1", str(SITE / "hero.webm")], check=True)
    demo.save(poster, "hero-poster-dark")
    for name in ("hero.mp4", "hero.webm"):
        print(f"wrote {SITE / name} ({(SITE / name).stat().st_size // 1024} KB)")


def main() -> int:
    return demo.run(record)


if __name__ == "__main__":
    sys.exit(main())
