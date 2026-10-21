"""Send back and Unassign: one shape, a colour each, in both places they appear."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
WORKSPACE = (STATIC / "workspace.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

ACTIONS = ("btn-sendback", "btn-unassign")


def rule(selector):
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("}", start)]


def test_each_action_carries_its_own_colour():
    """Send back and Unassign were both the same outlined blue."""
    fills = {name: rule("." + name) for name in ACTIONS}

    assert "--amber" in fills["btn-sendback"]
    assert "--gray-bg" in fills["btn-unassign"]
    assert len({f.split("color:")[1] for f in fills.values()}) == 2, "both actions share a colour"


def test_they_share_one_shape():
    """The colour is the only thing that differs; size and weight are stated once."""
    shape = rule(".btn-lifecycle")

    assert "font-size" in shape and "padding" in shape and "font-weight" in shape
    for name in ACTIONS:
        assert "color" not in rule("." + name).split("color:")[0].replace("border-color", "")


def test_the_team_view_and_the_folder_card_use_the_same_classes():
    for source in (APP, WORKSPACE):
        assert "btn-lifecycle btn-sendback" in source
    assert "btn-lifecycle btn-unassign" in WORKSPACE


def test_the_button_says_send_back_everywhere():
    """One action was called Return in the Team view and Send back on the card."""
    assert ">Send back</button>" in WORKSPACE
    assert ">Return</button>" not in WORKSPACE
    assert ">Return<" not in INDEX


def test_the_text_is_dark_enough_to_read_on_its_own_fill():
    """Measured: muted grey on the grey fill came out at 4.1:1, under the 4.5 a label needs."""
    assert "color: var(--text-soft)" in rule(".btn-unassign")
    assert "--text-soft: #4a4a52" in CSS
