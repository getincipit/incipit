"""Builds the packaged application: the icon set, then a one-folder PyInstaller build of `Incipit`.

    .venv/Scripts/python.exe scripts/build_app.py [--dist dist]

The result, dist/Incipit/Incipit.exe (Incipit on other systems), runs every CLI command; the
installer (installer/incipit.iss) wraps the folder. Cloudflare's tunnel program (cloudflared) is
copied in beside it from the build host, so a client needs nothing else installed. Inno Setup,
cloudflared and the build tools are the build host's business: the script names whatever is missing
and stops.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_icons  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))
from incipit import tunnel  # noqa: E402

ENTRY = ROOT / "scripts" / "incipit_entry.py"


def require(module: str, package: str) -> None:
    try:
        __import__(module)
    except ImportError:
        raise SystemExit(f"{package} is not installed: pip install incipit[build]") from None


def build(dist: Path, icons: Path, clean: bool = True) -> Path:
    require("PyInstaller", "PyInstaller")
    require("resvg_py", "resvg-py")
    cloudflared = tunnel.find_program()
    if not cloudflared or not Path(cloudflared).is_file():
        raise SystemExit("cloudflared is not installed on this build host: winget install Cloudflare.cloudflared "
                         "(or set INCIPIT_CLOUDFLARED to its path)")
    build_icons.build(out_dir=icons)
    problems = build_icons.check(icons)
    if problems:
        raise SystemExit("icon set: " + "; ".join(problems))
    import PyInstaller.__main__ as pyinstaller

    feed_file = ROOT / "build" / "UPDATE_FEED"
    feed_file.parent.mkdir(parents=True, exist_ok=True)
    feed_file.write_text(os.environ.get("INCIPIT_UPDATE_FEED", "").strip(), encoding="utf-8")

    separator = ";" if os.name == "nt" else ":"
    arguments = [
        str(ENTRY), "--name", "Incipit", "--onedir", "--noconfirm", "--windowed",
        "--icon", str(icons / "incipit.ico"),
        "--distpath", str(dist), "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        # The pages and scripts, found by assets.py through sys._MEIPASS.
        "--add-data", f"{ROOT / 'app' / 'static'}{separator}app/static",
        "--add-data", f"{ROOT / 'VERSION'}{separator}.",
        # Where the program looks for new versions; empty means updates are not set up in this build.
        "--add-data", f"{feed_file}{separator}.",
        "--collect-submodules", "incipit", "--collect-submodules", "zeroconf",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
    ]
    if clean:
        arguments.append("--clean")
    pyinstaller.run(arguments)
    built = dist / "Incipit"
    shutil.copy(icons / "incipit.ico", built / "incipit.ico")
    # Found there by tunnel.find_program in the packaged program.
    shutil.copy(cloudflared, built / tunnel.EXE)
    licence = (ROOT / "installer" / "licenses" / "Apache-2.0.txt").read_text(encoding="utf-8")
    (built / "THIRD-PARTY-NOTICES.txt").write_text(NOTICES + "\n" + licence, encoding="utf-8")
    return built


NOTICES = """cloudflared (Cloudflare Tunnel client), copied unmodified into this program's folder.
Copyright Cloudflare, Inc. Licensed under the Apache License, Version 2.0, whose full text follows.
Source: https://github.com/cloudflare/cloudflared
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    parser.add_argument("--icons", default=str(ROOT / "build" / "icons"))
    args = parser.parse_args()
    built = build(Path(args.dist), Path(args.icons))
    print(f"built {built}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
