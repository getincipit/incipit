#!/usr/bin/env python
"""Fetches the real documents the download page shows, from the Thomas A. Edison Papers' public
digital edition, and writes the credits the page prints beside them.

Each is published under CC0 1.0 (checked here before anything is saved, since that is what makes
them usable on a public page). The images are saved as WebP in site/documents/ with a credits file
naming each document, its date and the page it came from. Re-running fetches them again.

    .venv/Scripts/python.exe scripts/fetch_documents.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "documents"
EDITION = "https://edisondigital.rutgers.edu"
CREDIT = "Thomas A. Edison Papers, Rutgers University"
WIDTH = 720

# The document the page shows: the handwritten letter the hero recording reviews.
DOCUMENTS = [
    {"key": "letter", "id": "X018C9-0915", "page": 0, "kind": "Handwritten letter",
     "title": "Mary Emily Miller to Mina Miller Edison", "date": "5 March 1911"},
]
NOTEBOOK_SERVICE = ("https://iiif.archive.org/image/iiif/3/taepnotebook-{nb}%2Ftaepnotebook-{nb}_jp2.zip"
                    "%2Ftaepnotebook-{nb}_jp2%2Ftaepnotebook-{nb}_{canvas:04d}.jp2/full/{width},/0/default.jpg")


def fetch(document: dict) -> tuple[bytes, str]:
    """The page's image bytes and the edition page it came from; refuses anything not CC0."""
    if "id" in document:
        manifest = requests.get(f"{EDITION}/iiif/2/{document['id']}/manifest", timeout=60).json()
        if "CC0" not in str(manifest.get("attribution", "")) + str(manifest.get("license", "")):
            raise SystemExit(f"{document['id']} is not published under CC0; not using it")
        canvas = manifest["sequences"][0]["canvases"][document["page"]]
        image = requests.get(canvas["images"][0]["resource"]["@id"], timeout=120).content
        return image, f"{EDITION}/document/{document['id']}"
    # The notebooks are served from the Internet Archive; the edition's notebook pages carry the same CC0 terms.
    url = NOTEBOOK_SERVICE.format(nb=document["notebook"], canvas=document["canvas"], width=WIDTH * 2)
    for attempt in range(4):
        reply = requests.get(url, timeout=120)
        if reply.status_code == 200:
            return reply.content, f"{EDITION}/notebook/{document['notebook']}"
    raise SystemExit(f"the notebook page {document['notebook']} canvas {document['canvas']} could not be fetched")


def tidy(image: bytes, landscape_crop: bool) -> Image.Image:
    """A page as a card: the scanner's black margins trimmed, warmed from microfilm white to paper."""
    page = Image.open(io.BytesIO(image)).convert("L")
    # Trim the dark frame some scans carry.
    box = page.point(lambda v: 255 if v > 40 else 0).getbbox() or (0, 0, page.width, page.height)
    page = page.crop(box)
    paper = Image.merge("RGB", (page.point(lambda v: min(255, int(v * 0.96) + 8)),
                                page.point(lambda v: min(255, int(v * 0.94) + 6)),
                                page.point(lambda v: min(255, int(v * 0.88) + 4))))
    scale = WIDTH / paper.width
    return paper.resize((WIDTH, max(1, int(paper.height * scale))), Image.LANCZOS)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    credits = []
    for document in DOCUMENTS:
        image, link = fetch(document)
        card = tidy(image, landscape_crop=False)
        path = OUT / f"{document['key']}.webp"
        card.save(path, format="WEBP", quality=80, method=6)
        credits.append({"key": document["key"], "kind": document["kind"], "title": document["title"],
                        "date": document["date"], "link": link, "credit": CREDIT, "license": "CC0 1.0",
                        "width": card.width, "height": card.height})
        print(f"{document['key']:10} {card.width}x{card.height} {path.stat().st_size // 1024} KB  {link}")
    (OUT / "credits.json").write_text(json.dumps(credits, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
