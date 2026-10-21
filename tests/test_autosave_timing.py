"""autosave.js driven under node: when it decides to write, and when it declines to."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
AUTOSAVE = (STATIC / "autosave.js").read_text(encoding="utf-8")

# Enough of a document for the module to attach its listeners and put its indicator somewhere.
HARNESS = """
const listeners = {};
const node = () => ({
  id: "", className: "", textContent: "", isConnected: true,
  setAttribute() {}, appendChild() {}, matches: () => true,
});
const body = node();
global.document = {
  body,
  visibilityState: "visible",
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  getElementById: () => null,
  createElement: () => node(),
};
global.window = global;

// Timers the test drives by hand, so nothing waits in real seconds.
let pending = [];
global.setTimeout = (fn, ms) => { pending.push({ fn, ms }); return pending.length; };
global.clearTimeout = (id) => { if (id) pending[id - 1] = null; };
global.setInterval = () => 0;
const runTimers = async () => {
  const due = pending.filter(Boolean);
  pending = [];
  for (const t of due) await t.fn();
};

const saves = [];
let dirty = false;
global.window.__draft__ = {
  unsaved: () => dirty,
  documentId: () => "D1-0001",
  save: async () => { saves.push(1); dirty = false; return true; },
};

%(module)s

const fire = (type) => (listeners[type] || []).forEach((fn) => fn({ target: node() }));

(async () => {
  %(body)s
})();
"""


def run(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = HARNESS % {"module": AUTOSAVE, "body": script}
    done = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_first_edit_of_a_sitting_still_schedules_a_save():
    """Found in the browser: app.js marks the document dirty from its own listener on the field,
    which runs after this module's. Gating on "is there unsaved work" therefore dropped the first
    event of every edit -- and a paste or a one-character fix is exactly one event."""
    result = run("""
      fire("input");          // the module sees this before app.js has marked anything
      dirty = true;           // ... which is when app.js's own listener runs
      await runTimers();
      console.log(JSON.stringify({ saves: saves.length }));
    """)

    assert result["saves"] == 1


def test_typing_on_does_not_write_on_every_keystroke():
    """The history records an entry per edit, so a write per keystroke is the history ruined."""
    result = run("""
      dirty = true;
      for (let i = 0; i < 20; i++) fire("input");
      await runTimers();
      console.log(JSON.stringify({ saves: saves.length }));
    """)

    assert result["saves"] == 1


def test_nothing_is_written_when_there_is_nothing_to_write():
    result = run("""
      dirty = false;
      fire("input");
      await runTimers();
      console.log(JSON.stringify({ saves: saves.length }));
    """)

    assert result["saves"] == 0


def test_leaving_a_field_writes_without_waiting_out_the_timer():
    result = run("""
      dirty = true;
      fire("blur");
      console.log(JSON.stringify({ saves: saves.length }));
    """)

    assert result["saves"] == 1


def test_hiding_the_tab_writes_what_is_there():
    """The case a reviewer never comes back from."""
    result = run("""
      dirty = true;
      document.visibilityState = "hidden";
      fire("visibilitychange");
      console.log(JSON.stringify({ saves: saves.length }));
    """)

    assert result["saves"] == 1


def test_a_save_that_fails_leaves_the_work_marked_unsaved():
    """So the reviewer's own Save, and the next attempt, still have it."""
    result = run("""
      dirty = true;
      window.__draft__.save = async () => false;
      fire("blur");
      await new Promise((r) => process.nextTick(r));
      console.log(JSON.stringify({ stillDirty: dirty }));
    """)

    assert result["stillDirty"] is True


# ---------- edits made while a save is in flight ----------


def test_words_typed_during_a_save_are_not_marked_saved():
    """Reported in review: the payload is built before the request, but a success cleared the
    dirty flag unconditionally -- so what reached the server was the text from before those words,
    and the browser then believed it had nothing left to save."""
    result = run("""
      dirty = true;
      let generation = 1;
      window.__draft__.unsaved = () => dirty;
      window.__draft__.save = async () => {
        const sent = generation;        // the payload this request carries
        generation += 1;                // ... and the reviewer types on while it flies
        if (sent === generation) dirty = false;
        return true;
      };
      fire("blur");
      await new Promise((r) => process.nextTick(r));
      console.log(JSON.stringify({ stillDirty: dirty }));
    """)

    assert result["stillDirty"] is True


def test_autosave_comes_back_for_them_without_another_keystroke():
    """The reviewer may have stopped typing, so nothing else would ever schedule the write."""
    result = run("""
      dirty = true;
      let first = true;
      window.__draft__.save = async () => {
        saves.push(1);
        if (first) { first = false; return true; }   // still dirty: typed into mid-flight
        dirty = false;
        return true;
      };
      fire("blur");
      await new Promise((r) => process.nextTick(r));
      await runTimers();                              // the follow-up it scheduled for itself
      await new Promise((r) => process.nextTick(r));
      console.log(JSON.stringify({ saves: saves.length, stillDirty: dirty }));
    """)

    assert result["saves"] == 2 and result["stillDirty"] is False
