---
description: Start the review server and hand over the dev logins
---

Start the site with the desktop preview (`preview_start` with the `incipit` configuration from
`.claude/launch.json`), or from a terminal:

```bash
.venv/Scripts/python.exe -m incipit.cli review --db data/incipit.db --bucket-uri file://./data/bucket --host 127.0.0.1 --port 8011
```

A fresh database has no owner: run `incipit init --db data/incipit.db` first and open the
`/setup?token=…` link it prints to create the owner. Make an admin join code and a reviewer join
code in Team → Invite, join with each from `/login?join=1`, and report the URL and the logins.
