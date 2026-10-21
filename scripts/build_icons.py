"""Renders app/static/favicon.svg into the application's icon set: a PNG at every size and one .ico.

The favicon is the application's icon wherever it is shown, so every size is rendered from the
vector by resvg rather than scaled down from one bitmap.

    .venv/Scripts/python.exe scripts/build_icons.py [--out build/icons]
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "app" / "static" / "favicon.svg"
SIZES = (16, 32, 48, 64, 128, 256, 512)
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def render(svg_text: str, size: int) -> Image.Image:
    data = resvg_py.svg_to_bytes(svg_string=svg_text, width=size, height=size)
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def build(svg_path: Path = SVG, out_dir: Path = ROOT / "build" / "icons") -> dict[str, Path]:
    """Writes incipit-{size}.png for every size and incipit.ico; returns what it wrote."""
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_text = svg_path.read_text(encoding="utf-8")
    rendered = {size: render(svg_text, size) for size in SIZES}
    written = {}
    for size, image in rendered.items():
        path = out_dir / f"incipit-{size}.png"
        image.save(path, format="PNG")
        written[f"png{size}"] = path
    ico = out_dir / "incipit.ico"
    # Each entry its own render, not a resample of the largest.
    rendered[256].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                       append_images=[rendered[s] for s in ICO_SIZES if s != 256])
    written["ico"] = ico
    return written


def check(out_dir: Path) -> list[str]:
    """What is wrong with an icon set, if anything: a missing size, a wrong shape, a blank image."""
    problems = []
    for size in SIZES:
        path = out_dir / f"incipit-{size}.png"
        if not path.is_file():
            problems.append(f"{path.name} missing")
            continue
        with Image.open(path) as image:
            if image.size != (size, size):
                problems.append(f"{path.name} is {image.size}, not {size}x{size}")
            if not image.convert("RGBA").getchannel("A").getbbox():
                problems.append(f"{path.name} is blank")
    if not (out_dir / "incipit.ico").is_file():
        problems.append("incipit.ico missing")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svg", default=str(SVG))
    parser.add_argument("--out", default=str(ROOT / "build" / "icons"))
    args = parser.parse_args()
    written = build(Path(args.svg), Path(args.out))
    problems = check(Path(args.out))
    for name, path in written.items():
        print(f"{name:7} {path}")
    for problem in problems:
        print("PROBLEM", problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
