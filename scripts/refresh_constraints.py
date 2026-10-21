"""Rewrites constraints.txt from what this checkout's venv currently resolves.

Run it after deliberately upgrading a dependency, then run the suite before committing: the point
of the file is that a green pipeline speaks for the versions the workstation actually runs, which
it cannot do if the two are refreshed at different times.
"""
import sys
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent

# The declared dependencies of incipit[dev]; everything else below is reached from these.
ROOTS = [
    "requests", "fsspec", "python-dotenv", "Pillow", "fastapi", "uvicorn", "python-multipart",
    "pytest", "pytest-xdist", "pytest-timeout", "httpx", "pyflakes",
    # The data folder's file, and the tray and build extras.
    "tomli-w", "zeroconf", "cryptography", "pystray", "pyinstaller", "resvg-py",
]

HEADER = """\
# The exact versions this project is known to work at, for `pip install -c constraints.txt`.
#
# pyproject.toml declares ranges, which is right for a library and wrong for a deployment: CI
# resolved them anew on every run, so the host and CI could differ on any dependency without
# anyone choosing that. This file pins the whole closure so a green pipeline says something about
# the versions the workstation actually runs.
#
# A constraint is a ceiling, not an install list -- a platform-specific entry that is not
# installed on a given machine (colorama, tzdata) is simply unused there.
#
# Regenerate with scripts/refresh_constraints.py after an upgrade, then run the suite.

"""


def closure() -> dict[str, tuple[str, str]]:
    """Every distribution the declared dependencies pull in, at its installed version."""
    found: dict[str, tuple[str, str]] = {}

    def walk(name: str) -> None:
        key = name.lower().replace("_", "-")
        if key in found:
            return
        try:
            installed = version(name)
        except PackageNotFoundError:
            return          # an optional dependency this platform does not install
        found[key] = (metadata(name)["Name"], installed)
        for raw in metadata(name).get_all("Requires-Dist") or []:
            requirement = Requirement(raw)
            if requirement.extras:
                continue
            marker = str(requirement.marker or "")
            # Platform-specific dependencies are pinned too, so the file covers every host.
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                if "platform_system" not in marker and "sys_platform" not in marker:
                    continue
            walk(requirement.name)

    for root in ROOTS:
        walk(root)
    return found


def main() -> int:
    found = closure()
    missing = [r for r in ROOTS if r.lower().replace("_", "-") not in found]
    if missing:
        print(f"not installed in this environment: {', '.join(missing)}", file=sys.stderr)
        return 1
    body = "".join(f"{found[key][0]}=={found[key][1]}\n" for key in sorted(found))
    (REPO_ROOT / "constraints.txt").write_text(HEADER + body, encoding="utf-8")
    print(f"wrote constraints.txt ({len(found)} pinned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
