"""Every acceptance criterion in docs/specs has a test, unless its spec says it is checked elsewhere."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECS = sorted((ROOT / "docs" / "specs").glob("M*-*.md"))
CRITERION = re.compile(r"^- AC-(\d+):(.*)$", re.M)
# The two ways a spec says a criterion is not a test's to prove.
EXEMPT = re.compile(r"\*(Deferred to M\d+|Verified outside the suite)", re.IGNORECASE)


def covering_sources(module: str) -> list[str]:
    """Test files whose opening docstring says they cover `module` ("M1")."""
    sources = []
    for path in (ROOT / "tests").glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(rf"\b{module}\b", text[:400]):
            sources.append(text)
    return sources


@pytest.mark.parametrize("spec", SPECS, ids=lambda p: p.stem)
def test_every_criterion_has_a_test_named_for_it(spec):
    module = spec.name.split("-")[0]
    sources = "\n".join(covering_sources(module))
    missing = [f"AC-{number}" for number, line in CRITERION.findall(spec.read_text(encoding="utf-8"))
               if not EXEMPT.search(line)
               and not re.search(rf"def test_(?:ac\d+_)*ac{number}_", sources)]
    assert missing == [], f"{spec.name}: no test for {', '.join(missing)}"
