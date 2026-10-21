---
description: Run the full test suite and report only what failed
---

```bash
.venv/Scripts/python.exe -m pytest -q -p no:randomly -n auto -ra
```

Then `python -m compileall -q src scripts`, `pyflakes src scripts tests`, and `node --check` on
`app/static/app.js`, `workspace.js`, `folder_exports.js`, `history.js`, `autosave.js`. Report
failures with their one-line reason and nothing else when everything passes.
