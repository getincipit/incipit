# M8 — Download and landing page

## Purpose

People who have never seen Incipit need one page that shows what it does, where its name comes
from, what it asks of them, and how to get it. M8 builds that page as a static site in `site/`,
published from the repository, in the restrained idiom the plan's brief set (one idea per screen,
large type, the product itself as the only imagery, light and dark), with screenshots taken from the
real program, an estimator of the time it saves, and the download.

## Scope

- **`site/`**: `index.html`, `site.css`, `site.js`, `estimator.js`, the logo and favicon (copied
  from `app/static/` so there is one drawing), and `screens/` (light and dark captures, WebP at
  twice the display size).
- **Real screens.** `scripts/capture_screens.py` starts the real site from a throwaway data folder
  with a fake model service, fills it with invented material (typed letters with a letterhead, an
  invented firm and invented people, drawn here), takes the folder through upload, split, draft,
  review and assignment, and captures with Playwright: the review screen, the queue, the split
  being confirmed, the Team tab, the prompt builder, Models, Usage, Backups, Access and the setup
  wizard, each in light and dark. Re-running it regenerates them, so the page cannot drift from
  the product.
- **Sections.** Hero (the headline, the download button, the Windows line in monospace, then the
  stage: six real documents from the Thomas A. Edison Papers' public edition, fetched by
  `scripts/fetch_documents.py` and credited under the stage, lie scattered on a table, settle one by
  one into a stylised Incipit window, and the handwritten letter among them is typed out as a draft
  with its fields filled) · From scans to a finished folder (the screens follow the copy as it scrolls: upload,
  split, draft, review, export) · Review instead of transcribe · A prompt that knows your period ·
  Your team · On your own computer (one install, the data in a folder you choose, backups, reaching
  it from outside, updates) · Time, end to end (the estimator) · The name · Download (requirements,
  the newest release and its notes, the administrator's guide).
- **Estimator.** A pure function in `estimator.js` (node-tested) that keeps **people time** (typing
  and indexing by hand, against reviewing a draft) apart from **machine time** (drafting, which
  runs unattended) and **model cost**; every default is labelled as an assumption to edit, and the
  machine figures say what they were measured on and how many documents.
- **Download.** The button goes to the newest release on GitHub; the page reads the release's
  version, date and notes from GitHub's public API, and falls back to the releases page when it
  cannot.
- **Motion.** CSS transforms and opacity only, driven by IntersectionObserver; `prefers-reduced-motion`
  shows everything at rest.
- **Publishing.** `scripts/publish_site.py` pushes `site/` to the `main` branch of a second repository,
  `getincipit/getincipit.github.io`, which GitHub Pages serves at `https://getincipit.github.io/`; no
  workflow, so nothing beyond push access is needed.

## Non-goals

Pricing and sign-up (there is no hosted service), analytics or any third-party script, a CMS,
translations.

## Acceptance criteria

- AC-1: `site/index.html` has the hero with the logo, a one-sentence description and a download button, and sections for the workflow, reviewing, the prompt, the team, running on your own computer, the estimator, the name and the download.
- AC-2: The name section says where "Incipit" comes from: Latin for "here begins", the opening words by which a text was known and catalogued before titles were usual.
- AC-3: The page is dark by design; every screenshot it shows is a dark capture from the real program by `scripts/capture_screens.py` (light captures are made too, for the README and other uses), and every document in the hero is a real page from a public-domain edition, fetched by `scripts/fetch_documents.py`, which refuses anything not published under CC0, and credited on the page with a link to its edition page.
- AC-4: The estimator keeps people time, machine time and model cost apart, never sums people and machine time into one figure, labels every default as an assumption, and computes them as its formulas say (node-tested).
- AC-5: The download button leads to the newest GitHub release; the page shows the newest version, date and notes from GitHub's API and falls back to the releases page when the API cannot be reached.
- AC-6: The page loads no script, style or font from another site, uses no analytics, and every local file it references exists.
- AC-7: With `prefers-reduced-motion` the page shows every section at rest; otherwise its motion uses only transforms and opacity.
- AC-8: At phone width (375 px) and desktop width the page has no horizontal overflow and no console errors. *Verified outside the suite, in the browser.*
- AC-9: `scripts/publish_site.py` publishes `site/` to GitHub Pages through the organisation's `getincipit.github.io` repository.
- AC-10: The suite, residue test, spec coverage, `node --check` and `scripts/smoke_m8.py` pass. *Verified outside the suite, by the module's checks and smoke run.*

## Failure table

| Failure | User sees | System does | Operator does |
|---|---|---|---|
| GitHub's API cannot be reached or rate-limits the reader | "See the newest release on GitHub" with the link | keeps the download button pointing at the newest release | nothing |
| A screenshot is missing | the build's check fails before publishing | the test names the file | runs `scripts/capture_screens.py` |
| The reader prefers reduced motion | every section at rest | no scroll-driven motion | nothing |
| JavaScript is off | every section and the download link, without the release notes or the estimator's figures | nothing | nothing |

## Smoke checklist (`scripts/smoke_m8.py`)

Serve `site/` from a local server and open it in Playwright at 375 px and 1440 px, light and dark:
no horizontal overflow, no console errors, every image loads, the download link points at the newest
release, the estimator answers when its inputs change, the name section is there, the hero ends
with the six documents in the queue and the letter transcribed, and with reduced motion every
section is visible without scrolling into it and the hero is already at rest.

## Results (2026-10-21)

- Suite: 2,212 passed (`-p no:randomly -n auto`); residue and spec-coverage tests included.
- `scripts/smoke_m8.py`: 18 checks passed (375 px and 1440 px, light and dark, reduced motion).
- Looked at by hand in Chromium at 1440 px and 375 px: the pile, the settle, the finished draft; no
  horizontal overflow, no console errors. The published page is at
  `https://getincipit.github.io/`.
- Found along the way: the steps section already owned the class `.stage`, so the hero's became
  `.desk`; the smoke's overflow check caught it.

## Decisions made while building it

- **Dark by design, in Raycast's direction** (user direction 2026-10-21: the page must read as a
  made thing, not a generic AI product page). The identity is an archivist's: paper-cream type on
  an ink-dark table, a serif display face, monospace for the small facts (the Windows line, the
  step numbers, the measured figures), and the pen. No gradients, no glass, no stock art.
- **The hero shows real documents, not invented or generated ones** (user direction 2026-10-21:
  "use real documents from the edison papers … a collage of various documents, and animate them being
  put into the platform and cleanly transcribed", and "use letter document too"). The first design
  drew an invented letter under a lamp; it was replaced because a made-up page says less than a
  real one. The six pages (two handwritten letters, a notebook sketch, a notebook page of accounts,
  typed minutes, a typed report) come from the Thomas A. Edison Papers' digital edition, whose
  manifests publish them under CC0 1.0; `scripts/fetch_documents.py` checks that before saving
  anything and writes `site/documents/credits.json`, from which the page prints the credit line with
  a link to each edition page. The residue test, which otherwise bans the original project's name,
  allows exactly these files for this reason. The letter's transcription in the animation was made
  by hand from the scan; none of this installation's drafted text is used.
- **The stage is designed at fixed units and scaled.** 1200 by 720 units (760 on a phone, framing
  the window alone), scaled to its box by one custom property, so every position in the animation
  is a plain number and the same keyframes serve every width. Motion is transforms and opacity only;
  with reduced motion the stage is drawn at rest, transcribed.
- **Screenshots come from the real program**, run against a fake model with invented material, and
  every one is checked before it is saved for anything that would name this computer (addresses,
  hostname, paths, user name); the capture refuses to save otherwise. The Settings screens are the
  panel alone, since the dimmed site behind the window only blurred the picture.
- **The download comes first.** The page's first screen is the download, as on every reference
  page looked at (Claude, Linear, Raycast); the explanation follows for those who want it.
- **GitHub Pages from a branch the script pushes.** The first plan was an Actions workflow, which the
  publishing token could not create (no `workflow` scope); a branch the publish script force-pushes
  needs nothing but push access and has one fewer moving part. First a `gh-pages` branch of the code
  repository, served at `parthaped.github.io/incipit/`; then (user direction 2026-10-21: an address
  without a person's name, at no cost) the code moved to the free organisation `getincipit` and the page
  to its own repository `getincipit.github.io`, which GitHub serves at the organisation's root.
- **No analytics and nothing loaded from elsewhere**, so the page tells no one who visited and
  works offline once loaded, except for the release lookup, which fails quietly.

## Redundancy review

- One owner per picture: the screenshots are made only by `scripts/capture_screens.py`, the hero's
  documents fetched only by `scripts/fetch_documents.py` (which also writes their credits, the one
  place the page reads them from), the shared drawings copied only by `scripts/build_site.py` (which
  the publish script calls); the residue test allows no other binary in the repository.
- One source per fact: the newest version, date, size and notes come from GitHub's release API at
  read time; the page hard-codes none of them.
- The estimator's formulas live once, in `estimator.js`, used by the page and tested by node.
- Found and fixed along the way: the Team tab had left a split being confirmed drawn beneath it
  (`workspace.js`, with a regression test).
- **A recording of the real screen was tried and set aside** (2026-10-22). For one release the hero
  was a Chromium recording of the review screen at work on the Miller letter; its quality on the page
  was judged too low, and the drawn stage with the six documents returned, resized so the window
  holds exactly its content (880 by 470 units on a 1200 by 600 desk) with nothing empty beneath the
  page or the draft, and the "review screen" section back beneath it. The review that recording
  prompted stays: a pause control on the hero, asset links versioned by content, and the export
  prompt naming the folder as people know it.
