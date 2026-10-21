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
- **Sections.** Hero (the pen as the I, one sentence, the download button, the review screen rising
  into place) · From scans to a finished folder (the screens follow the copy as it scrolls: upload,
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
- **Publishing.** `scripts/publish_site.py` pushes `site/` to the repository's `gh-pages` branch, which
  GitHub Pages serves at `https://parthaped.github.io/incipit/`; no workflow, so nothing beyond push
  access is needed.

## Non-goals

Pricing and sign-up (there is no hosted service), analytics or any third-party script, a CMS,
translations.

## Acceptance criteria

- AC-1: `site/index.html` has the hero with the logo, a one-sentence description and a download button, and sections for the workflow, reviewing, the prompt, the team, running on your own computer, the estimator, the name and the download.
- AC-2: The name section says where "Incipit" comes from: Latin for "here begins", the opening words by which a text was known and catalogued before titles were usual.
- AC-3: Every screenshot the page shows exists in light and dark, was captured from the real program by `scripts/capture_screens.py`, and is referenced through `<picture>` so the reader's theme picks it.
- AC-4: The estimator keeps people time, machine time and model cost apart, never sums people and machine time into one figure, labels every default as an assumption, and computes them as its formulas say (node-tested).
- AC-5: The download button leads to the newest GitHub release; the page shows the newest version, date and notes from GitHub's API and falls back to the releases page when the API cannot be reached.
- AC-6: The page loads no script, style or font from another site, uses no analytics, and every local file it references exists.
- AC-7: With `prefers-reduced-motion` the page shows every section at rest; otherwise its motion uses only transforms and opacity.
- AC-8: At phone width (375 px) and desktop width the page has no horizontal overflow and no console errors. *Verified outside the suite, in the browser.*
- AC-9: `scripts/publish_site.py` publishes `site/` to GitHub Pages through the `gh-pages` branch.
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
release, the estimator answers when its inputs change, the name section is there, and with reduced
motion every section is visible without scrolling into it.
