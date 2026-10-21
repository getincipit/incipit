"""constraints.txt is a second copy of a fact, so it has to be checked against the first."""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Skipped rather than failed on 3.10, which the project still declares support for and which has no
# tomllib; CI and the workstation both run 3.12, where these checks do run.
tomllib = pytest.importorskip("tomllib")


def normalised(name: str) -> str:
    return re.split(r"[<>=!~;\[ ]", name.strip(), 1)[0].lower().replace("_", "-")


def declared() -> set[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    listed = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        listed += list(extra)
    return {normalised(item) for item in listed}


def pinned() -> dict[str, str]:
    pins = {}
    for line in (REPO_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            pins[normalised(name)] = version
    return pins


def test_every_declared_dependency_is_pinned():
    """A dependency added to pyproject.toml without refreshing the pins would be the one thing CI
    still resolved for itself."""
    missing = sorted(declared() - set(pinned()))

    assert not missing, (
        f"not in constraints.txt: {', '.join(missing)} -- run scripts/refresh_constraints.py"
    )


def test_every_pin_names_one_exact_version():
    loose = sorted(name for name, version in pinned().items() if not version)

    assert not loose, f"constraints.txt entries without a version: {', '.join(loose)}"


def test_the_install_everybody_uses_applies_the_pins():
    """README, the host's MAINTENANCE steps and CI all install through requirements.txt, so the
    constraint has to live there rather than in any one of them."""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "-c constraints.txt" in text
