"""The folder grid uses the whole window, and the scroll bar stays out of the way."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def rule(selector):
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert found, f"no rule for {selector}"
    return found.group(1)


# ---------- the space ----------


def test_the_queue_is_not_capped_short_of_the_window():
    assert "max-width" not in rule("#queue-view")


def test_the_queue_still_keeps_its_margins():
    """"Clean and breathable": full width is not edge to edge."""
    assert "padding: 36px 40px 80px" in rule("#queue-view")


def test_a_narrow_window_gets_narrower_margins():
    assert "#queue-view { padding: 24px 18px 64px; }" in CSS


def test_a_wider_window_gets_more_cards_not_wider_ones():
    assert "repeat(auto-fill, minmax(250px, 1fr))" in rule(".folder-grid")


# ---------- the scroll bar ----------


def test_the_pane_that_scrolls_is_the_one_that_reaches_the_edge():
    """The scroll bar is the pane's own, so pinning it right means the pane reaching right."""
    assert '<main id="queue-view" class="scroll-pane">' in HTML


def test_the_split_review_pane_behaves_the_same_way():
    assert '<main id="split-review-view" class="scroll-pane hidden">' in HTML


def test_the_thumb_is_invisible_to_begin_with():
    assert "background-color: transparent" in rule(".scroll-pane::-webkit-scrollbar-thumb")


def test_the_thumb_appears_at_the_edge():
    assert "background-color: var(--border)" in rule(
        ".scroll-pane.at-the-edge::-webkit-scrollbar-thumb")


def test_firefox_hides_and_shows_it_too():
    """scrollbar-color is the only handle Firefox gives; without this it is visible there always."""
    assert "scrollbar-color: transparent transparent" in rule(".scroll-pane")
    assert "scrollbar-color: var(--border) transparent" in rule(".scroll-pane.at-the-edge")


def test_the_track_keeps_its_width_so_nothing_reflows_under_the_pointer():
    """Only the thumb's colour changes."""
    assert "width" not in rule(".scroll-pane.at-the-edge::-webkit-scrollbar-thumb")


# ---------- what counts as "that area" ----------


def near_the_edge(x, y, box=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("  function watchTheEdge(")
    body = source[start:source.index("\n  }", start) + 4]
    box = box or {"right": 1000, "top": 0, "bottom": 800}
    harness = (
        "const EDGE_PX = 28; let on = null;"
        "global.document = { querySelectorAll: () => [{"
        f"  getBoundingClientRect: () => ({json.dumps(box)}),"
        "  classList: { toggle: (_, near) => { on = near; } } }] };"
        + body
        + f"watchTheEdge({{ clientX: {x}, clientY: {y} }});"
        + "console.log(JSON.stringify(on));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_pointer_at_the_right_edge_shows_it():
    assert near_the_edge(995, 400) is True


def test_the_pointer_over_the_cards_does_not():
    assert near_the_edge(500, 400) is False


def test_just_outside_the_reach_does_not():
    assert near_the_edge(970, 400) is False


def test_the_pointer_past_the_pane_does_not():
    """A pane that does not reach the window edge must not answer for the space beside it."""
    assert near_the_edge(1400, 400) is False


def test_the_pointer_above_the_pane_does_not():
    """The header is at the same x as the scroll bar and is nothing to do with it."""
    assert near_the_edge(995, -40) is False
