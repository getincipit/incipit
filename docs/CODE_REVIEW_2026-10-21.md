# Code review — 2026-10-21

Scope: queue and document navigation, split ordering, Team modal lifecycle, pending-work polling,
repository boundaries, and the existing automated suite. This was a code and automated-behaviour
review; it did not include a fresh visual browser pass or production load test.

## Findings addressed

| Priority | Finding | Resolution | Owner |
|---|---|---|---|
| P1 | Overlapping queue requests could install an earlier tab's documents and folders after the current tab loaded. | Commit both lists only for the newest request; clear previous-tab data at tab changes. | `app/static/app.js` |
| P1 | Returning to the queue while a document fetch was pending could reopen the document after navigation. | Invalidate pending opens on return to queue or folder grid. | `app/static/app.js` |
| P2 | A document-list HTTP error was treated as a document array; folder errors could produce a partially updated queue. | Check both response statuses, commit atomically, and report failed loads. | `app/static/app.js` |
| P2 | Pending-upload responses could appear after their tab or screen was left; HTTP failures could throw unhandled errors. | Check request generation, tab and view before rendering, and handle failed requests. | `app/static/app.js` |
| P2 | Split ordering repeatedly used array searches and insertion while traversing groups, making ordering quadratic. | Use a successor map for linear ordering and constant-time insertion of new boundaries. | `app/static/app.js` |
| P2 | Reviewer polling loaded and sorted all pending document IDs, then performed one folder query per document. | Aggregate pending counts by folder in one query and apply the existing visibility set. Empty visibility does no document query. | `src/incipit/db.py`; route orchestration in `system_routes.py` |
| P2 | Statistics dialogs left document-level Escape handlers attached when closed by button, backdrop, or replacement. | One cleanup function owns removal of the panel and its keyboard listener on every close path. | `app/static/workspace.js` |
| P2 | Switching workflow tabs invoked two complete queue loads. | Await the load already started by `showQueue()`. | `app/static/app.js` |

## Ownership

`ARCHITECTURE.md` now covers browser files and repository path families as well as backend
modules. Request generations belong to their screen owner, modal cleanup to the creator, SQL
aggregation to the repository, and permission decisions to the scoping layer. No personal or
team CODEOWNERS identities were invented.

## Verification

Six new executable regression tests cover out-of-order queue responses, HTTP failure handling,
cancelled document opens, stale upload responses, modal listener cleanup, scoped pending counts,
and split ordering across 20,000 groups. The ordering test prohibits linear array-search and
splice operations in the ordering function, rather than relying on a machine-dependent timer.
The existing panel test now recognizes the cleanup function instead of requiring direct DOM
removal that leaked the listener.

Python compilation, Pyflakes, JavaScript syntax checks and whitespace checks passed. The final full
suite passed: 2,240 tests in 86.33 seconds. Existing Starlette/httpx
TestClient deprecation warnings remain. Automated layout tests do not establish that every
viewport and modal combination is visually correct.
