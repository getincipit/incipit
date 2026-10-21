---
description: Drive the site as owner, admin and reviewer over HTTP and report anything broken
---

```bash
.venv/Scripts/python.exe scripts/smoke_m0.py
.venv/Scripts/python.exe scripts/smoke_m1.py
.venv/Scripts/python.exe scripts/smoke_m2.py
```

Each serves a throwaway site with a fake model backend. M0 walks upload, split, confirm, draft,
reviewer accept and submit, send back, resubmit, admin accept, export; M1 does it in two tabs
holding same-named folders; M2 starts from a site with no owner and walks setup, branding, join
codes, a password reset, the org id and the wizard before the M0 journey. Each step prints PASS or
FAIL. Report the FAIL lines with their detail, or "all passed".
