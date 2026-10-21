"""Closing your own account, from the click on your name to the row in the database."""

import json
import pathlib
import shutil
import subprocess

import pytest

WORKSPACE_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "workspace.js"
APP_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"


def _function(name: str) -> str:
    src = WORKSPACE_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = src.index(f"async function {name}() {{")
    return src[start:src.index("\n  }", start) + len("\n  }")]


def _run(call: str, answers: list, status: int = 200) -> str:
    """Runs one of the account functions with the prompts answered in order."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = f"""
    const me = {{ username: "john", display_name: "John" }};
    const answers = {json.dumps(answers)};
    global.window = {{
      prompt: () => answers.shift(),
      location: {{ set href(v) {{ console.log("WENT:" + v); }} }},
    }};
    global.alert = (m) => console.log("ALERT:" + m);
    global.fetch = async (url) => {{
      console.log("POSTED:" + url);
      return {{ ok: {json.dumps(status == 200)}, status: {status},
                json: async () => ({{ detail: "refused" }}) }};
    }};
    {_function("changePassword")}
    {_function("closeAccount")}
    {call}
    """
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# ---------- closing an account ----------


def test_typing_the_username_closes_the_account():
    out = _run("closeAccount();", ["john"])
    assert "POSTED:/api/me/close" in out
    assert "WENT:/login" in out


def test_cancelling_the_prompt_posts_nothing():
    out = _run("closeAccount();", [None])
    assert "POSTED" not in out


def test_typing_the_wrong_name_posts_nothing():
    """The whole point of asking for it typed."""
    out = _run("closeAccount();", ["jack"])
    assert "POSTED" not in out
    assert "ALERT:" in out


def test_a_refusal_leaves_the_person_signed_in():
    """The last admin is refused server-side."""
    out = _run("closeAccount();", ["john"], status=403)
    assert "ALERT:refused" in out
    assert "WENT:" not in out


# ---------- changing a password ----------


def test_a_password_change_posts_both_passwords_and_signs_out():
    out = _run("changePassword();", ["john-password", "a-longer-new-password"])
    assert "POSTED:/api/me/password" in out
    assert "WENT:/login" in out


def test_cancelling_either_prompt_changes_nothing():
    assert "POSTED" not in _run("changePassword();", [None, "whatever"])
    assert "POSTED" not in _run("changePassword();", ["john-password", None])


def test_a_rejected_password_is_reported_rather_than_swallowed():
    out = _run("changePassword();", ["wrong", "a-longer-new-password"], status=400)
    assert "ALERT:refused" in out
    assert "WENT:" not in out


# ---------- the way in ----------


def test_the_name_in_the_bar_is_something_you_can_click():
    """A <span> with a click handler is not reachable by keyboard, and the name is the only way
    into this menu."""
    src = WORKSPACE_JS.read_text(encoding="utf-8")
    assert '<button type="button" class="who-name" id="who-name">' in src
    assert 'addEventListener("click", toggleAccountMenu)' in src


def test_the_pipeline_switcher_leaves_the_team_tab_to_its_owner():
    """Team shares the strip but is no tab of folders; the tab handler must not consume it."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "if (!btn?.dataset.tab) return;" in src


def test_active_status_is_a_secondary_collapsible_team_section():
    src = WORKSPACE_JS.read_text(encoding="utf-8")
    assert 'class="team-presence-toggle"' in src
    assert 'aria-expanded="false"' in src
    assert 'class="team-presence-list" hidden' in src
    assert 'setPresenceExpanded(readPresencePreference())' in src
