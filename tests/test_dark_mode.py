"""Dark mode: chosen under the gear by either role, kept in the browser, and legible without strain (15 October)."""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def tokens(block: str) -> dict:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def light_tokens() -> dict:
    return tokens(CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))])


def dark_tokens() -> dict:
    start = CSS.index(':root[data-theme="dark"] {')
    return tokens(CSS[start:CSS.index("}", start)])


def luminance(colour: str) -> float:
    """Relative luminance of a hex colour, as the contrast formula wants it."""
    colour = colour.strip().lstrip("#")
    channels = [int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(ink: str, ground: str) -> float:
    a, b = luminance(ink), luminance(ground)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_the_dark_theme_answers_both_the_chosen_setting_and_the_system_preference():
    assert ':root[data-theme="dark"] {' in CSS
    assert ':root:not([data-theme="light"])' in CSS[CSS.index("@media (prefers-color-scheme: dark)"):]


def test_the_dark_theme_redefines_every_colour_the_light_one_names():
    """A token left out keeps its light value in the dark, and that is where a white box comes from."""
    colours = {name for name, value in light_tokens().items()
               if re.match(r"#|rgba?\(", value.strip()) and name not in ("--on-colour",)}
    missing = colours - set(dark_tokens()) - {"--surface", "--surface-muted", "--red"}
    assert not missing, sorted(missing)


@pytest.mark.parametrize("ink, ground, floor", [
    ("--text", "--bg", 7.0),
    ("--text", "--panel", 7.0),
    ("--text-muted", "--panel", 4.5),
    ("--accent-ink", "--panel", 4.5),
    ("--green", "--panel", 4.5),
    ("--amber", "--panel", 4.5),
    ("--danger", "--panel", 4.5),
    ("--accent", "--bg", 4.5),
])
def test_dark_text_clears_the_contrast_it_needs(ink, ground, floor):
    dark = dark_tokens()
    assert contrast(dark[ink], dark[ground]) >= floor


def test_dark_ink_is_not_pure_white_on_pure_black():
    """Full white on full black is the glare a dark theme exists to avoid."""
    dark = dark_tokens()
    assert dark["--text"].lower() != "#ffffff" and dark["--text"].lower() != "#fff"
    assert luminance(dark["--bg"]) > 0.004


def test_text_on_a_filled_control_is_dark_in_the_dark_theme():
    """The lifted accent, green and amber are too light for white text, so the text turns dark with them."""
    assert contrast(dark_tokens()["--on-colour"], dark_tokens()["--accent"]) >= 4.5
    assert contrast(dark_tokens()["--on-colour"], dark_tokens()["--green"]) >= 4.5
    assert "color: var(--on-colour)" in CSS[CSS.index(".btn-accept {"):CSS.index(".btn-accept {") + 120]


def test_no_panel_is_still_painted_white_by_hand():
    """Every `background: #fff` outside the token block would stay white in the dark."""
    body = CSS[CSS.index("/* Anyone who has asked"):]
    assert not re.search(r"background:\s*#fff\b", body)


def test_the_style_tab_offers_system_light_and_dark():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-settings-tab="style"' in page and 'id="settings-panel-style"' in page
    assert set(re.findall(r'data-theme-choice="([a-z]+)"', page)) == {"system", "light", "dark"}


def test_a_reviewer_keeps_the_style_tab():
    """The reviewer view removes the admin's panels by name; Style is not among them."""
    source = (STATIC / "workspace.js").read_text(encoding="utf-8")
    removed = source[source.index('["settings", "deletions", "site", "appearance", "prompt", "tabs", "models", "usage", "backups", "updates", "access"].forEach'):]
    assert '"style"' not in removed[:200]


def test_sign_in_keeps_dark_card_and_legible_button_on_narrow_screens():
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    mobile = page[page.index("@media (max-width: 860px)"):]
    assert ".card { background:color-mix(in srgb, var(--card) 92%, transparent); }" in mobile
    assert "color:var(--on-accent)" in page
    assert "--on-accent:#111318" in page
