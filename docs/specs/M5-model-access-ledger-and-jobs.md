# M5 — Model access, the call ledger and recoverable jobs

## Purpose

A client must be able to connect Incipit to the model service themselves, see what it costs, and
trust that work keeps going when nobody is watching. Today the OpenRouter key can only be typed
into `incipit.toml` or an environment variable; spend is summed from per-document cost columns that
miss every billed attempt that failed, and drafting runs inside the HTTP request that asked for it,
so it stops when the browser does and is lost when the site restarts.

M5 gives the owner a Models page (and a wizard step) to enter and test the key and choose the
models; records every call to the model service, once and forever, in a ledger that the caps and a
Usage page read; and runs drafting and split proposals as jobs that survive a closed browser and a
restart.

## Scope

- **Keys.** `secrets.py` encrypts the API key (Fernet) with a key file kept in the user's settings
  folder (`%LOCALAPPDATA%\Incipit\secret.key`, the same folder as the location file), outside the
  data folder, so a copied data folder or a backup never carries a usable key; on Windows the key
  file is protected with DPAPI for the Windows account. A key found in `incipit.toml` or
  `OPENROUTER_API_KEY` at start is moved into the database and blanked in the file.
- **Backends.** One active model backend: OpenRouter (the default) or any OpenAI-compatible server
  (a self-hosted model on the client's own network is allowed, over http on a private address).
  The owner sets the key, the drafting model and the fallback model; the site uses them at once.
  "Test the connection" costs nothing: OpenRouter's key endpoint (`GET /api/v1/key`) or the
  server's `GET /models`. The model picker lists the service's models with their prices, free ones
  marked.
- **Ledger.** `model_calls` gets one row for every HTTP response (or timeout) from the model
  service, written by `openrouter.call_openrouter` through a hook, with the purpose, job, document,
  batch and prompt version taken from the calling context. Rows are insert-only (triggers refuse
  UPDATE and DELETE). Spend is the ledger's sum; per-document cost columns stay for display only.
- **Caps.** The all-time cap (existing) and a new optional monthly cap are checked against ledger
  spend plus what in-flight work has reserved, before each document and each split window.
- **Usage.** Settings → Usage (owner and admin): this month, last month, all time, by purpose and by
  model, the caps, and the latest calls. Reviewers never receive cost (the M2 boundary holds).
- **Jobs.** `jobs` table and an in-process worker: drafting a folder, Draft pending, drafting after
  a confirmed split, and a split proposal are recorded as jobs before they start. The route runs its
  job in its own thread and answers when it ends, as it always did (so the page's flows are
  unchanged); the thread carries on if the browser goes away; `GET /api/jobs/{id}` reports
  progress; an admin can cancel or retry. At start, jobs left running are queued again and the
  site's worker finishes them.
  Merge, re-split and redrafting a single document stay as they are (one or two calls, done while
  the person waits) but are ledgered and capped like the rest.
- **Wizard.** A "model" step after "prompt": enter the key, test it, pick the models.

## Non-goals

Several active backends at once, per-tab models, a hosted gateway or platform-funded calls (the
hub is withdrawn), OpenRouter's generation lookup for lost calls (a lost call is marked ambiguous
and estimated instead), a separate worker process (M10).

## Data model

```sql
model_backends(id, kind CHECK in ('openrouter','openai_compatible'), base_url, api_key_enc,
               key_hint, model, fallback_model, active, last_tested_at, last_test_ok,
               last_test_detail, updated_by, updated_at)
model_calls(id, started_at, finished_at, purpose CHECK in ('draft','split','merge','prompt_test',
            'carried_over'), job_id, document_id, batch_id, prompt_version_id, backend_kind, model,
            is_fallback, input_tokens, output_tokens, cost_usd, cost_source CHECK in
            ('provider','estimated','none'), outcome CHECK in ('ok','malformed','refused','error',
            'timeout','ambiguous'), http_status, error_text)   -- insert-only (triggers)
jobs(id, kind CHECK in ('draft','split'), tab_id, folder_id, batch_id, document_ids_json, state
     CHECK in ('queued','running','succeeded','failed','cancelled'), total, done, requested_by,
     created_at, started_at, finished_at, heartbeat_at, cancel_requested, error_text, result_json)
app_settings + monthly_cap REAL NULL
```

## Routes and roles

| Route | Owner | Admin | Reviewer |
|---|---|---|---|
| `GET /api/models` (backend without the key: kind, url, models, key hint, test result) | yes | yes | 403 |
| `PUT /api/models` `{kind?, base_url?, api_key?, model?, fallback_model?}` | yes | 403 | 403 |
| `POST /api/models/test` | yes | yes | 403 |
| `GET /api/models/catalogue` (the service's model list with prices) | yes | yes | 403 |
| `GET /api/usage` | yes | yes | 403 |
| `PUT /api/usage/caps` `{max_total_cost?, monthly_cap?}` | yes | yes | 403 |
| `GET /api/jobs?folder_id=`, `GET /api/jobs/{id}` | yes | yes | own folders' jobs, without cost |
| `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/retry` | yes | yes | 403 |
| Draft and split routes | enqueue, return `{job}` | same | as today (403 where it was) |

## Acceptance criteria

- AC-1: The owner enters an API key on the Models page or in the wizard; it is stored encrypted, never returned (only its last four characters), and an admin cannot change it; a key left in `incipit.toml` or the environment is moved into the database at start and removed from the file.
- AC-2: The encryption key lives outside the data folder: a database copied to another computer cannot decrypt the key, and the Models page then says the key must be entered again.
- AC-3: "Test the connection" reports a working key with its credit balance, a refused key with the service's reason, and an unreachable server, without spending anything.
- AC-4: The owner chooses the drafting and fallback models (and, for a self-hosted server, its address); the next call uses them without a restart.
- AC-5: Every response from the model service, including failed, refused and malformed attempts, is recorded once in `model_calls` with its purpose, model, tokens, cost and outcome; a timed-out call is recorded as ambiguous with an estimated cost; rows cannot be changed or deleted.
- AC-6: Spend is the ledger's sum: deleting or redrafting a document never lowers it, and a malformed attempt's cost counts.
- AC-7: The all-time cap and the monthly cap stop new documents and split windows once ledger spend plus in-flight reservations reach them, with a message naming the cap.
- AC-8: The Usage page shows this month, last month and all-time spend, by purpose and by model, and the latest calls; a reviewer is refused it and no reviewer payload carries cost.
- AC-9: Drafting a folder, Draft pending, drafting after a confirmed split and proposing a split each run as a recorded job that keeps running when the browser closes (the route waits for it and answers as before) and reports progress through `GET /api/jobs/{id}`; asking again for a folder that already has a draft job joins that job instead of starting another.
- AC-10: A site restarted with jobs running queues them again; documents already drafted are not drafted again, and a document that was in flight is drafted once more with its lost call recorded as ambiguous.
- AC-11: An admin can cancel a job (it stops after the current document) and retry a failed one; an account problem (no key, a refused key, no credits) fails the job with the reason and drafts nothing more.
- AC-12: The wizard has a "model" step after "prompt" that is done once a key has passed its test.
- AC-13: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m5.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| No key entered | "Connect the model service in Settings → Models" on any draft | queues nothing | enters the key |
| Key refused (401) or no credits (402) | the job fails with the service's reason | stops the job, drafts nothing more | fixes the key or adds credit, then Retry |
| Encryption key file missing (another computer, a wiped profile) | "The saved key cannot be read on this computer; enter it again" | treats the key as absent | enters the key again |
| Service down, 429 or 5xx | progress pauses; the job carries on with retries | retries with backoff, then the fallback model | waits, or cancels |
| A call times out after it was sent | nothing extra | records it as ambiguous with an estimate; retries | checks the service's own usage page if it matters |
| The site stops mid-job | the job shows as queued again after the restart | re-queues; drafts only what is still pending | nothing |
| Cap reached | "Stopped: the monthly (or all-time) cap of $X is reached" | finishes the document in flight, starts no more | raises the cap on the Usage page |
| The model chosen is withdrawn by the service | the job fails: "no provider for MODEL" | tries the fallback first | picks another model |
| A self-hosted server is unreachable | "Could not reach http://…" on the test and on the job | fails the job | checks the server |

## Smoke checklist (`scripts/smoke_m5.py`)

Start a site from a fresh data folder with a fake model service (a local HTTP server speaking the
chat-completions and key endpoints); as the owner enter a key and see only its hint back; test the
connection; pick models; upload drawn pages and get a split job that finishes; confirm and get a
draft job; watch it finish through `/api/jobs`; see the calls on the Usage page with their cost;
delete a document and see spend unchanged; set a monthly cap below spend and see the next draft job
stop with the cap's message; kill the site mid-job, start it again and see the job finish with no
document drafted twice; as a reviewer get 403 on Models and Usage and no cost in any payload.

## Decisions made while building it

- **The ledger is written by a background writer.** A call is made while the drafting run may hold
  the database's write lock (it commits after each document); a second connection inserting there
  would wait on the very thread making the call. Rows are queued and written as soon as the lock
  allows; `ledger.spent` flushes the queue first unless its own connection holds a write.
- **One row per HTTP attempt, from inside `call_openrouter`.** The client already retries malformed,
  truncated and degenerate replies; each of those attempts was billed and was invisible to the old
  per-document cost columns. Transport retries for 429 and 5xx inside urllib3 are not billed and
  are not separate rows.
- **A route runs its own job.** Rather than enqueue-and-poll, which would have changed every flow on
  the page, a route records the job and runs it in the request's own thread (Starlette lets a
  synchronous handler finish after the browser goes); the site's worker runs only what the last run
  left and what a request leaves queued. The page's flows and messages are unchanged; the folder
  draft still arrives in the page's chunks, each its own job.
- **A document in flight at a crash is drafted again**, once, with the lost call recorded as
  ambiguous at the recent average cost: cheaper and clearer for one organisation than holding the
  document until someone confirms, which the plan's hub design called for.
- **No egress policy.** The plan's private-address checks protected a shared hub; a client pointing
  its own install at a model on its own network is the point, so http to a private address is
  allowed for an OpenAI-compatible server.
- **The encryption key sits beside the location file** (`%LOCALAPPDATA%\Incipit`), sealed by DPAPI
  on Windows, so the data folder, its backups and a moved copy never carry a usable key.
- **The connection test is free**: OpenRouter's key endpoint, or a server's model list; the model
  picker reads OpenRouter's public model list, which needs no key.

## Redundancy review

- One owner per rule: `keyvault.py` alone encrypts and decrypts; `backends.py` alone reads and
  writes the active service and applies it to Settings; `ledger.py` alone records and sums spend
  (`db.get_total_cost` now delegates to it); `spend.py` alone decides whether a cap has room, for
  drafting (`room_under_caps`) and splitting (`cap_message`); `jobs.py` alone moves a job between
  states.
- One source per fact: spend is the ledger's sum; the per-document and per-batch cost columns stay
  as labels for the review screen and the folder analytics and no longer feed any cap. The deletion
  log's cost copy is no longer read for spend.
- Removed: the summed-columns query in `get_total_cost`; the cap check inline in
  `run_propose_split`.
- Left for the module that owns it: a separate worker process (M10); the job list on the page beyond
  what `/api/jobs` serves (the activity bars still narrate a running job).

## Results (2026-10-21)

- Suite: 2,067 passed (`-p no:randomly -n auto`), including 42 M5 spec tests
  (`tests/test_m5_model_access.py`) and the spec-coverage check; eleven earlier tests that seeded
  spend through cost columns, or faked the whole client, now put their spend in the ledger
  (`conftest.bill`). `pyflakes` and `node --check` are clean.
- Smoke: `scripts/smoke_m5.py` 19 of 19 against a fake OpenAI-compatible service, including killing
  the site while a document was being drafted: after the restart the job finished, each document
  had one successful call, and the lost one was in the ledger as ambiguous with an estimate.
  `smoke_m0` to `smoke_m4b` still pass (`smoke_m2` gained the new wizard step).
- Windows DPAPI sealed and opened the key file in the suite; a database read with another
  settings folder reported the key as needing to be entered again.
- Browser: the wizard's new step drew the service switch, the key field and the model picker, which
  loaded OpenRouter's live list of 308 image-reading models with their prices; the Usage page's
  latest-calls table first overflowed its card at phone width (the timestamp and model columns),
  and now wraps inside it. No console errors.

- Release review (2026-10-21), each fixed with a regression test that names it:
  switching to another service or server address without a new key now drops the stored key, so
  an OpenRouter key is never sent to a server it was not made for; a server on the client's own
  network may draft without a key (OpenRouter still requires one, and no Authorization header is
  sent when there is none); billed calls still queued when the process ends are written to the
  ledger first (an exit hook, and a flush when the site stops); caps refuse NaN and infinity, which
  had been stored as "no cap"; split jobs carry their batch's folder, so a reviewer of that folder
  can follow them; and the Models, Usage, Backups, Access and Tabs panels no longer add a click
  handler every time Settings reopens them (`pageKit.remount`). The exit-flush test was checked to
  fail without its fix. Suite 2,126 passed; all eight smokes pass.

## Status

Complete.
