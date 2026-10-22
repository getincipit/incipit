#!/usr/bin/env python
"""Captures the landing page's screenshots from the real program, in light and dark.

Starts the site from a throwaway data folder with a fake model service, fills it with invented
material (typed letters of an invented ironworks to invented people, drawn here), takes a folder
through upload, split, draft, review and assignment, leaves a second upload waiting to be split, and
captures each screen the landing page shows with Playwright at twice the display size, saved as
WebP in site/screens/. Nothing real is used, and re-running it regenerates every image.

    .venv/Scripts/python.exe scripts/capture_screens.py
"""

from __future__ import annotations

import io
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "site" / "screens"
FONTS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
OWNER = ("eleanor", "eleanor-demo-password")
ADMIN = ("marcus", "marcus-demo-password")
REVIEWER = ("ada", "ada-demo-password")
WIDTH, HEIGHT = 1440, 900

# Invented throughout: the firm, the people, the places and the letters.
LETTERS = [
    {"date": "March 14, 1889", "to": "Mr. Howard W. Abbott", "place": "Troy, N.Y.",
     "salutation": "Dear Sir:",
     "body": ["We are in receipt of your favor of the 9th inst. regarding the castings for the new mill "
              "at Cohoes, and beg to say that the patterns are now in hand and will be forwarded to the "
              "foundry by Saturday next.",
              "As to the price named in your letter, we regret that we cannot meet it, the cost of pig "
              "iron having advanced some four dollars the ton since the first of the year. We enclose a "
              "revised estimate, which we trust you will find reasonable.",
              "Mr. Pierce will call upon you at Troy during the coming week, when he will be pleased to "
              "go over the drawings with your engineer and settle any question of detail."],
     "close": "Yours very truly,", "sign": "Harlan & Pierce Ironworks", "per": "per J. T. Harlan",
     "type": "Letter", "author": "Harlan, J. T.", "recipient": "Abbott, Howard W."},
    {"date": "April 2, 1889", "to": "Miss Clara M. Whitcomb", "place": "Schenectady, N.Y.",
     "salutation": "Dear Madam:",
     "body": ["Your letter of the 28th ult. has been received, and we have taken pleasure in showing "
              "the enclosed sample of ornamental railing to our draughtsman, who thinks the design "
              "may be executed without difficulty.",
              "The work could be completed in about six weeks from the receipt of your order, and we "
              "should require a deposit of one third of the amount upon signing of the contract.",
              "Should you wish to see similar work, a balcony of this pattern may be seen at the "
              "residence of Dr. Fairbanks on State Street."],
     "close": "Respectfully yours,", "sign": "Harlan & Pierce Ironworks", "per": "per E. R. Pierce",
     "type": "Letter", "author": "Pierce, E. R.", "recipient": "Whitcomb, Clara M."},
    {"date": "May 20, 1889", "to": "The Board of Water Commissioners", "place": "Saratoga Springs, N.Y.",
     "salutation": "Gentlemen:",
     "body": ["In accordance with your invitation of the 6th inst., we beg to submit our proposal for "
              "furnishing and laying the cast iron water pipe described in your specification.",
              "We propose to furnish pipe of the best quality, tested at the works to a pressure of "
              "three hundred pounds to the square inch, delivered at Saratoga Springs at the prices "
              "named in the enclosed schedule.",
              "We would refer you to the Commissioners of Glens Falls, for whom we laid some four miles "
              "of main during the past season, as to the character of our work."],
     "close": "Very respectfully,", "sign": "Harlan & Pierce Ironworks", "per": "per J. T. Harlan",
     "type": "Proposal", "author": "Harlan, J. T.", "recipient": "Board of Water Commissioners"},
]


def font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def wrap(draw, text: str, face, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=face) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def letter_pages(letter: dict, seed: int) -> list[bytes]:
    """Two typed pages on aged paper, slightly skewed and soft, like a scan."""
    rng = random.Random(seed)
    pages = []
    body = ImageFont.truetype(str(FONTS / "times.ttf"), 30) if (FONTS / "times.ttf").exists() else font("arial.ttf", 30)
    for number in (1, 2):
        paper = Image.new("RGB", (1240, 1640), (243, 236, 219))
        draw = ImageDraw.Draw(paper)
        for _ in range(9000):
            x, y = rng.randrange(1240), rng.randrange(1640)
            shade = rng.randrange(-14, 6)
            draw.point((x, y), fill=(236 + shade, 228 + shade, 208 + shade))
        y = 120
        if number == 1:
            draw.text((620, y), "HARLAN & PIERCE IRONWORKS", font=font("georgiab.ttf", 46), fill=(40, 34, 30), anchor="mt")
            y += 64
            draw.text((620, y), "Castings · Structural Iron · Water Pipe", font=font("georgiai.ttf", 26), fill=(70, 60, 52), anchor="mt")
            y += 40
            draw.text((620, y), "Albany, N.Y.", font=font("georgia.ttf", 26), fill=(70, 60, 52), anchor="mt")
            y += 46
            draw.line([(140, y), (1100, y)], fill=(90, 80, 70), width=2)
            y += 60
            draw.text((1100, y), letter["date"], font=body, fill=(30, 28, 26), anchor="rt")
            y += 80
            for line in (letter["to"], letter["place"]):
                draw.text((140, y), line, font=body, fill=(30, 28, 26))
                y += 40
            y += 30
            draw.text((140, y), letter["salutation"], font=body, fill=(30, 28, 26))
            y += 60
            paragraphs = letter["body"][:2]
        else:
            draw.text((620, y), f"{letter['to']}  —  2", font=body, fill=(30, 28, 26), anchor="mt")
            y += 90
            paragraphs = letter["body"][2:]
        for paragraph in paragraphs:
            for i, line in enumerate(wrap(draw, paragraph, body, 900)):
                draw.text((140 + (60 if i == 0 else 0), y), line, font=body, fill=(30, 28, 26))
                y += 42
            y += 26
        if number == 2:
            y += 30
            draw.text((640, y), letter["close"], font=body, fill=(30, 28, 26))
            y += 56
            draw.text((640, y), letter["sign"], font=font("georgiai.ttf", 32), fill=(28, 36, 70))
            y += 44
            draw.text((640, y), letter["per"], font=font("georgiai.ttf", 28), fill=(28, 36, 70))
        page = paper.rotate(rng.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=(243, 236, 219))
        page = page.filter(ImageFilter.GaussianBlur(0.6))
        buffer = io.BytesIO()
        page.save(buffer, format="JPEG", quality=88)
        pages.append(buffer.getvalue())
    return pages


def transcription(letter: dict) -> str:
    head = f"{letter['date']}\n\n{letter['to']}\n{letter['place']}\n\n{letter['salutation']}\n\n"
    first = "\n\n".join(letter["body"][:2])
    second = "\n\n".join(letter["body"][2:])
    tail = f"\n\n{letter['close']}\n{letter['sign']}\n{letter['per']}"
    return f"[Letterhead: Harlan & Pierce Ironworks, Albany, N.Y.]\n\n{head}{first}\n\n[page 2]\n\n{second}{tail}"


class FakeModel(BaseHTTPRequestHandler):
    """Proposes a document every two pages and drafts the letters in the order they are asked for."""

    drafts: list[dict] = []

    def log_message(self, *args):
        pass

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "demo-vision"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        images = sum(1 for b in payload["messages"][0]["content"] if isinstance(b, dict) and b.get("type") == "image_url")
        kind = (payload.get("response_format") or {}).get("json_schema", {}).get("name", "")
        if kind == "split":
            answer = {"starts_new_document": [i % 2 == 0 for i in range(images)],
                      "is_duplicate_of_previous_page": [False] * images}
            cost = 0.0011
        else:
            letter = FakeModel.drafts.pop(0) if FakeModel.drafts else LETTERS[0]
            # A letter carrying its own text (a real document transcribed by hand) is drafted as given.
            answer = {"text": letter.get("text") or transcription(letter),
                      "uncertain_words": ["Cohoes"] if letter is LETTERS[0] else letter.get("uncertain", []),
                      "document_type": letter["type"], "date": letter["date"], "author": letter["author"],
                      "recipient": letter["recipient"], "place": letter.get("written_at", "Albany, N.Y."),
                      "names": [letter["author"], letter["recipient"]],
                      "places": letter.get("places", ["Albany, N.Y.", letter["place"]]),
                      "subjects": letter.get("subjects", ["Iron castings"]), "marginal_notes": [],
                      "challenges": letter.get("challenges", ["typed"])}
            cost = 0.0019
        self._send({"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1500 * max(images, 1), "completion_tokens": 520, "cost": cost}})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def save(png: bytes, name: str) -> None:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image.save(SCREENS / f"{name}.webp", format="WEBP", quality=82, method=6)


def main() -> int:
    return run(capture)


def run(visit) -> int:
    """Builds the demo site and hands it to `visit(base, folder_id, created, token, swaps)`; the
    screenshots and the hero recording share one site so the page never shows two different archives."""
    SCREENS.mkdir(parents=True, exist_ok=True)
    model_port = free_port()
    model = ThreadingHTTPServer(("127.0.0.1", model_port), FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="incipit-screens-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INCIPIT_SETTINGS_DIR": str(tmp / "settings")}
        env.pop("OPENROUTER_API_KEY", None)
        folder = tmp / "Harbour Street Archive"
        port = free_port()
        made = subprocess.run([sys.executable, "-m", "incipit.cli", "init-folder", str(folder), "--port", str(port),
                               "--https-port", str(free_port()), "--site-name", "harbour-street"],
                              capture_output=True, text=True, env=env)
        token = next(line.split("token=")[1].strip() for line in made.stdout.splitlines() if "token=" in line)
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "incipit.cli", "serve", "--data-folder", str(folder)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(300):
                try:
                    requests.get(f"{base}/health", timeout=1)
                    break
                except requests.RequestException:
                    time.sleep(0.2)
            owner = requests.Session()
            owner.post(f"{base}/api/setup/owner", json={"token": token, "username": OWNER[0], "password": OWNER[1],
                                                        "display_name": "Eleanor Vance"})
            owner.put(f"{base}/api/site/settings", json={"product_name": "Harbour Street Archive",
                                                         "company_name": "Harbour Street Historical Society"})
            for (name, password), role, shown in ((ADMIN, "admin", "Marcus Hale"), (REVIEWER, "reviewer", "Ada Lindqvist")):
                owner.post(f"{base}/api/team/users", json={"username": name, "password": "temporary-password-1",
                                                            "display_name": shown, "role": role})
                first = requests.Session()
                first.post(f"{base}/login", data={"username": name, "password": "temporary-password-1"}, allow_redirects=False)
                first.post(f"{base}/api/me/password", json={"current": "temporary-password-1", "new": password})
            owner.put(f"{base}/api/models", json={"kind": "openai_compatible", "api_key": "demo-key-000000",
                                                  "base_url": f"http://127.0.0.1:{model_port}/v1", "model": "demo-vision"})
            owner.put(f"{base}/api/backups", json={"destination": str(tmp / "Backup drive" / "Incipit Backups")})
            owner.post(f"{base}/api/backups/run")

            # One folder through split, draft and review; a second upload waiting to be split.
            pages = [page for i, letter in enumerate(LETTERS) for page in letter_pages(letter, i)]
            FakeModel.drafts = list(LETTERS)
            files = [("files", (f"HP1889_Page_{i + 1:02d}.jpg", data, "image/jpeg")) for i, data in enumerate(pages)]
            batch = owner.post(f"{base}/api/new/batches", files=files, data={"folder_display_id": "HP1889"}).json()
            proposal = owner.post(f"{base}/api/new/batches/{batch['batch_id']}/retry-proposal").json()
            folder_id = proposal["folder_id"]
            groups = [{"display_id": f"HP1889{suffix}", "sequence_indices": [n, n + 1]}
                      for suffix, n in (("AAA", 0), ("AAB", 2), ("AAC", 4))]
            created = owner.post(f"{base}/api/new/batches/{batch['batch_id']}/confirm", json={"groups": groups}).json()["created"]
            marcus = requests.Session()
            marcus.post(f"{base}/login", data={"username": ADMIN[0], "password": ADMIN[1]}, allow_redirects=False)
            ada_id = next(p["id"] for p in owner.get(f"{base}/api/team/users").json()["people"] if p["username"] == REVIEWER[0])
            marcus.post(f"{base}/api/team/assign", json={"folder_id": folder_id, "reviewer_id": ada_id, "workflow": "new"})
            ada = requests.Session()
            ada.post(f"{base}/login", data={"username": REVIEWER[0], "password": REVIEWER[1]}, allow_redirects=False)
            first_doc = ada.get(f"{base}/api/new/documents/{created[1]}").json()
            ada.post(f"{base}/api/new/documents/{created[1]}/review",
                     json={"reviewed_text": first_doc["transcription"]["text"], "review_status": "accepted",
                           "title": "", "fields": {}, "editors_notes": ""})
            ada.post(f"{base}/api/documents/{created[0]}/comments?workflow=new",
                     json={"body": "Is the mill at Cohoes or Cohoes Falls? The page is faint there."})
            second = [page for i, letter in enumerate(LETTERS[1:]) for page in letter_pages(letter, 10 + i)]
            files = [("files", (f"HP1890_Page_{i + 1:02d}.jpg", data, "image/jpeg")) for i, data in enumerate(second)]
            batch2 = owner.post(f"{base}/api/new/batches", files=files, data={"folder_display_id": "HP1890"}).json()
            owner.post(f"{base}/api/new/batches/{batch2['batch_id']}/retry-proposal")
            # Nothing about this computer may reach a public page: its addresses, its name, its paths.
            from incipit import host
            invented = ["192.168.1.20", "10.0.0.12", "10.0.0.13", "10.0.0.14"]
            swaps = {address: invented[i % len(invented)] for i, address in enumerate(host.lan_addresses())}
            swaps.update({host.hostname(): "ARCHIVE-PC", socket.gethostname(): "ARCHIVE-PC",
                          str(tmp / "Backup drive" / "Incipit Backups"): r"E:\Incipit Backups",
                          str(folder): r"D:\Harbour Street Archive", f"127.0.0.1:{model_port}": "192.168.1.50:8000",
                          f":{port}": ":8010"})
            if os.environ.get("USERNAME"):
                swaps[os.environ["USERNAME"]] = "archivist"
            visit(base, folder_id, created, token, swaps)
        finally:
            server.kill()
            server.wait(timeout=15)
            model.shutdown()
    print(f"wrote {len(list(SCREENS.glob('*.webp')))} screenshots to {SCREENS}")
    return 0


SCRUB = """(swaps) => {
  const pairs = Object.entries(swaps).sort((a, b) => b[0].length - a[0].length);
  const fix = (text) => pairs.reduce((t, [from, to]) => t.split(from).join(to), text);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) node.nodeValue = fix(node.nodeValue);
  document.querySelectorAll("input, textarea").forEach((el) => { el.value = fix(el.value); el.placeholder = fix(el.placeholder || ""); });
  // The demo's backups share a drive with its data only because both are in one temporary folder.
  document.querySelectorAll(".access-warning").forEach((el) => { if (el.textContent.includes("same drive")) el.remove(); });
}"""


# Which of these strings the page still shows anywhere, after SCRUB.
LEFT = """(keys) => {
  const shown = document.body.innerText + " " + [...document.querySelectorAll("input, textarea")].map((el) => el.value).join(" ");
  return keys.filter((k) => shown.includes(k));
}"""


def capture(base: str, folder_id: str, created: list[str], token: str, swaps: dict) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in ("light", "dark"):
            context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=2,
                                          color_scheme=theme)
            context.add_init_script(f"try {{ localStorage.setItem('incipit-theme', '{theme}'); }} catch (e) {{}}")
            page = context.new_page()

            def clean_page():
                page.evaluate(SCRUB, swaps)
                left = page.evaluate(LEFT, list(swaps))
                if left:
                    raise SystemExit(f"refusing to save a screenshot that still shows {left}")

            def shot(name: str, wait: float = 0.8):
                time.sleep(wait)
                clean_page()
                save(page.screenshot(), f"{name}-{theme}")

            page.goto(f"{base}/login", wait_until="networkidle")
            shot("login")
            page.fill("#li-user", OWNER[0])
            page.fill("#li-pass", OWNER[1])
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            # The wizard first, at its model step, while setup is still open.
            page.goto(f"{base}/setup", wait_until="networkidle")
            page.locator("button", has_text="Connect the transcription model").first.click()
            shot("wizard", 1.5)
            if theme == "light":
                session = requests.Session()
                session.post(f"{base}/login", data={"username": OWNER[0], "password": OWNER[1]}, allow_redirects=False)
                for step in ("site", "prompt", "model", "tabs", "people", "access"):
                    session.post(f"{base}/api/setup/steps/{step}", json={"done": True})
                session.post(f"{base}/api/setup/finish")
            page.goto(f"{base}/#documents", wait_until="networkidle")
            shot("queue", 1.5)
            page.goto(f"{base}/#documents/{folder_id}/{created[0]}", wait_until="networkidle")
            shot("review", 2.5)
            page.goto(f"{base}/#documents", wait_until="networkidle")
            resume = page.locator("button[data-resume-batch-id]").first
            if resume.count():
                resume.click()
                shot("split", 2.5)
            page.goto(f"{base}/#team", wait_until="networkidle")
            shot("team", 2)
            page.goto(f"{base}/#documents", wait_until="networkidle")
            page.click("#btn-settings")
            for tab in ("prompt", "models", "usage", "backups", "access"):
                page.click(f"[data-settings-tab='{tab}']")
                time.sleep(2.5)
                clean_page()
                # The panel alone: the dimmed site behind the window only blurs the picture, and the
                # window's row of tabs says nothing about the panel.
                save(page.locator(f"#settings-panel-{tab}").screenshot(), f"settings-{tab}-{theme}")
            context.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
