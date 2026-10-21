"""The Settings window: its panels scroll, and a sticky column beside a form never stands taller
than the window (user report 2026-10-21: the Prompt preview and its save button were cut off at the
window's foot, with no way to scroll to them)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_the_settings_panel_is_the_one_that_scrolls():
    panel = _rule(".settings-panel")
    assert "overflow-y: auto" in panel and "min-height: 0" in panel


def test_sticky_preview_columns_are_sized_to_the_window_and_scroll_inside():
    assert "--settings-chrome:" in _rule(".settings-modal")
    for selector in (".pb-preview", ".appearance-preview"):
        rule = _rule(selector)
        assert "position: sticky" in rule and "var(--settings-chrome)" in rule and "overflow-y: auto" in rule, selector


def test_the_prompt_text_gives_way_so_the_save_button_stays_in_reach():
    prompt = _rule(".pb-prompt")
    assert "flex: 1 1 auto" in prompt and "min-height: 140px" in prompt
    assert "- 64px" in _rule(".pb-preview")


def test_only_the_prompts_card_shrinks_in_a_short_column():
    assert "flex-shrink: 0" in _rule(".pb-preview > *") and "flex-shrink: 1" in _rule(".pb-preview > .pb-preview-card")


def test_a_narrow_layout_gets_a_static_preview_after_the_sticky_rule_it_overrides():
    for sticky, narrow in ((".pb-preview { position: sticky", "@container (max-width: 720px)"),
                           (".appearance-preview { position: sticky", "@media (max-width: 900px) { .appearance-layout")):
        assert CSS.index(narrow) > CSS.index(sticky), narrow
        assert "position: static; max-height: none" in CSS[CSS.index(narrow):CSS.index(narrow) + 220]
