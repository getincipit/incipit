"""A button keeps its height whatever its label says."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_a_button_label_never_wraps():
    """Measured at 820px: a wrapped label took Accept from 35.6px to 51.6px."""
    start = CSS.index(".btn {")
    assert "white-space: nowrap;" in CSS[start:start + 200]


def test_saving_does_not_swap_in_a_longer_label():
    assert "Saving (may transcribe next batch)" not in JS


def test_saving_shows_a_busy_state_instead():
    assert 'triggerButton.classList.add("is-busy")' in JS
    assert 'triggerButton.classList.remove("is-busy")' in JS
    assert ".btn.is-busy" in CSS


def test_a_confirmation_waits_for_the_buttons_new_label():
    """Measured: the label and the confirmation land on the same frame."""
    start = JS.index("function toast(")
    assert "}, SWAP_OUT_MS);" in JS[start:start + 500]


def test_a_confirmation_moves_at_the_buttons_pace():
    assert "const SWAP_OUT_MS = 170;" in JS
    assert "transition: opacity 0.26s var(--ease), transform 0.26s var(--ease);" in CSS


def test_a_toggling_button_keeps_one_width_for_both_of_its_labels():
    """Animating the width pushed Save Draft and Flag sideways on every accept."""
    start = JS.index("function reserveLabelWidth(")
    block = JS[start:JS.index("\n  }", start)]
    assert "Math.max(" in block and "minWidth" in block
    assert "button.style.width" not in JS


def test_opening_a_reviewed_document_does_not_animate_its_button():
    """The button arrived saying Reviewed and then swapped itself to Mark Unreviewed."""
    assert "syncActionButtons(false);" in JS
    start = JS.index("function syncActionButtons(")
    block = JS[start:JS.index("\n  }", start)]
    assert "animate ? swapButtonLabel : setButtonLabel" in block


def test_a_confirmation_fades_rather_than_vanishing():
    """display:none cannot be animated, so the toast no longer uses the hidden class."""
    assert 'saveToast.classList.add("hidden")' not in JS
    assert ".save-toast.is-shown" in CSS


def test_saving_leaves_the_other_buttons_alone():
    """Disabling every action dimmed the whole bar for the length of every save."""
    start = JS.index("async function saveReview(")
    block = JS[start:start + 2500]
    assert "b.disabled = true" not in block
    # A background save does not disable the controls; an explicit click waits and is replayed.
    assert "await state.savePromise" in block


def test_a_label_already_on_its_way_is_not_swapped_again():
    """Two callers asked for the same label, so every click animated twice."""
    assert "button.swapTo === text" in JS


def test_the_measuring_copy_does_not_duplicate_the_buttons_id():
    assert 'ghost.removeAttribute("id");' in JS


def test_the_document_top_bar_wraps_rather_than_overflowing():
    """Buttons no longer wrap, so on a phone the bar itself has to: Next ran 35px off a 348px screen."""
    start = CSS.index(".detail-topbar {")
    assert "flex-wrap: wrap;" in CSS[start:start + 200]
