"""The browser's Back and Forward move between the site's own screens (asked for 16 October).

Before, the site never told the browser it had moved, so Back left the site altogether.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

# history.js in a stand-in window: enough of `window`, `history` and `location` to run it.
HARNESS = """
const assert = require('node:assert/strict');
const listeners = {};
const entries = [{ state: null, url: '/' }];
let at = 0;
global.location = { pathname: '/', search: '', get hash() { const u = entries[at].url; const i = u.indexOf('#'); return i < 0 ? '' : u.slice(i); } };
global.history = {
  get state() { return entries[at].state; },
  get length() { return entries.length; },
  pushState(state, _, url) { entries.splice(at + 1); entries.push({ state, url }); at += 1; },
  replaceState(state, _, url) { entries[at] = { state, url }; },
};
global.document = { body: { className: 'role-admin' },
  addEventListener: (name, f) => { listeners['document:' + name] = f; } };
global.window = { addEventListener: (name, f) => { listeners[name] = f; } };
const back = async () => { at -= 1; await listeners.popstate({ state: entries[at].state }); };
"""


def _run(script: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = (STATIC / "history.js").read_text(encoding="utf-8")
    program = HARNESS + source + "\nconst R = window.__route__;\n(async () => {" + script + "})().catch((e) => { console.error(e); process.exit(1); });"
    return subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=30)


def test_an_address_that_names_no_screen_is_ignored():
    result = _run("""
      assert.equal(R.routeOf('#garbage/../x'), null);
      assert.equal(R.routeOf(''), null);""")
    assert result.returncode == 0, result.stderr


