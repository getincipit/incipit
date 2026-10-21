"""The split view's own progress line keeps counting while the server is working."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def run(script):
    """startProgressIndicator out of app.js, driven by a fake clock and a fake job list."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = JS.index("  function startProgressIndicator(")
    block = JS[start:JS.index("\n  }\n", start) + 4]
    harness = (
        "const PROGRESS_GRACE_MS = 0; const PROGRESS_MIN_VISIBLE_MS = 0;"
        "let activeJobs = [];"
        + block
        + script
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_line_of_its_own_keeps_ticking_while_a_job_is_running():
    """"Confirming split and drafting... (2s elapsed)" froze the moment the server registered the
    job, because the ticker yielded to bars that never cover the split view."""
    said = run("""
        let last = '';
        const stop = startProgressIndicator((t) => { last = t; }, 'Confirming split and drafting',
                                            null, undefined, false);
        setTimeout(() => { activeJobs = [{kind: 'drafting'}]; }, 50);
        setTimeout(() => { console.log(last); stop(); }, 2500);
    """)

    assert "Confirming split and drafting" in said
    assert "(2s elapsed" in said, f"the clock stopped: {said!r}"


def test_the_shared_strip_still_stands_aside_for_the_bars():
    """The bars say the same thing with more in them, and hide that strip as they appear."""
    said = run("""
        let last = 'nothing yet';
        const stop = startProgressIndicator((t) => { last = t; }, 'Drafting');
        setTimeout(() => { activeJobs = [{kind: 'drafting'}]; }, 50);
        setTimeout(() => { console.log(last); stop(); }, 2500);
    """)

    assert "0s elapsed" in said, f"the shared strip kept writing over the bars: {said!r}"


def test_both_split_view_callers_ask_to_keep_ticking():
    """The confirm and the retry both write to split-confirm-status, which nothing hides."""
    blocks = [JS[start:JS.index(");", start)]
              for start in range(len(JS)) if JS.startswith("startProgressIndicator(\n", start)]
    split_view = [b for b in blocks if "setSplitConfirmStatus" in b]

    assert len(split_view) == 2, f"expected the confirm and the retry, found {len(split_view)}"
    for block in split_view:
        assert block.rstrip().endswith("false"), block
