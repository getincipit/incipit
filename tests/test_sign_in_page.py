"""The sign-in page: a card with a form that works as it always did (15 October). Covers M0 AC-6."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incipit import review_server
from incipit.config import Settings

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture
def visitor(tmp_path):
    settings = Settings(bucket_uri=(tmp_path / "bucket").as_uri(), db_path=str(tmp_path / "extraction.db"))
    application = review_server.create_app(settings)
    return TestClient(application)


def test_the_page_asks_for_no_executable_assets_from_another_host():
    """External attribution links are fine; executable and image assets remain local."""
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    assert not re.search(r"""src=["']https?://""", page)


def test_the_form_still_posts_the_same_fields():
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    names = set(re.findall(r'name="([^"]+)"', page))
    assert {"next", "username", "password", "display_name", "join_code"} <= names


@pytest.mark.parametrize("placeholder", ["{{PRODUCT}}", "{{ERROR}}", "{{NEXT}}", "{{TAB}}"])
def test_the_server_still_has_somewhere_to_write_its_answer(placeholder):
    assert placeholder in (STATIC / "login.html").read_text(encoding="utf-8")


def test_short_desktop_windows_can_scroll_the_whole_sign_up_card():
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    panel = re.search(r"\.panel \{([^}]+)\}", page).group(1).replace(" ", "")
    card = re.search(r"\.card \{([^}]+)\}", page).group(1).replace(" ", "")
    assert "overflow-y:auto" in panel
    assert "flex-direction:column" in panel
    assert "margin-block:auto" in card
    assert "flex-shrink:0" in card




def test_ac6_the_card_names_the_site_and_leaves_no_placeholder_behind(visitor):
    """M0 AC-6: the page is served with the product name filled in, on the title and the card."""
    page = visitor.get("/login").text
    assert "Incipit" in page
    assert "{{" not in page


def test_ac6_the_scene_assets_are_gone(visitor):
    """M0 AC-6: the sign-in page is the card alone."""
    # The names are built from parts so the residue test does not mistake this check for a caller.
    scene, projector = "login-" + "scene", "login-" + "projector"
    page = visitor.get("/login").text
    assert "<canvas" not in page and scene not in page
    assert visitor.get(f"/{scene}.js").status_code == 404
    assert visitor.get(f"/{projector}.bin").status_code == 404
