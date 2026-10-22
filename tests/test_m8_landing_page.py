"""Covers M8 (docs/specs/M8-download-and-landing-page.md): the download page in site/, its screenshots
from the real program, the estimator, the newest release, its self-containment and motion, and the
workflow that publishes it."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PAGE = (SITE / "index.html").read_text(encoding="utf-8")
CSS = (SITE / "site.css").read_text(encoding="utf-8")
SCRIPT = (SITE / "site.js").read_text(encoding="utf-8")


def _node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _estimate(inputs: dict) -> dict:
    estimator = (SITE / "estimator.js").as_posix()
    return json.loads(_node(f"const e = require({json.dumps(estimator)}); console.log(JSON.stringify(e.estimate({json.dumps(inputs)})))"))


# ---------- AC-1: the page's parts ----------


def test_ac1_the_hero_has_the_headline_the_download_and_the_recorded_review_screen():
    hero = PAGE[PAGE.index('<section class="hero">'):PAGE.index("</section>")]
    assert "Here begins<br>the transcription." in hero and "Download for Windows" in hero
    assert '<video class="hero-video" autoplay muted loop playsinline' in hero
    assert 'src="hero.webm"' in hero and 'src="hero.mp4"' in hero and 'poster="screens/hero-poster-dark.webp"' in hero
    assert "Scan on the left. Draft on the right. Decide." in hero


def test_ac1_the_recording_its_poster_and_the_letter_it_reviews_exist_and_are_credited():
    for name in ("hero.webm", "hero.mp4", "screens/hero-poster-dark.webp", "documents/letter.webp"):
        assert (SITE / name).is_file(), name
    credits = json.loads((SITE / "documents" / "credits.json").read_text(encoding="utf-8"))
    assert [c["key"] for c in credits] == ["letter"]
    assert credits[0]["license"] == "CC0 1.0" and credits[0]["link"].startswith("https://edisondigital.rutgers.edu/")
    assert "published under CC0" in PAGE and 'data-credits' in PAGE and "documents/credits.json" in SCRIPT


def test_ac1_the_recording_is_made_by_the_record_script_from_the_real_program():
    script = (ROOT / "scripts" / "record_hero.py").read_text(encoding="utf-8")
    assert "import capture_screens as demo" in script and "record_video_dir" in script and "demo.run(record)" in script
    assert '"X018C9_0915.jpg"' in script and "#btn-accept" in script


def test_ac1_the_fetch_script_refuses_a_document_that_is_not_cc0():
    script = (ROOT / "scripts" / "fetch_documents.py").read_text(encoding="utf-8")
    assert 'if "CC0" not in' in script and "raise SystemExit" in script


@pytest.mark.parametrize("section", ['id="start"', 'id="how"', "Review instead of transcribe", "A prompt that knows your period",
                                     'id="yours"', 'id="time"', 'id="name"', 'id="download"'])
def test_ac1_every_section_is_there(section):
    assert section in PAGE


# ---------- AC-2: the name ----------


def test_ac2_the_name_section_says_where_incipit_comes_from():
    name = PAGE[PAGE.index('id="name"'):PAGE.index('id="download"')]
    assert "here begins" in name and "opening words" in name and "explicit" in name


# ---------- AC-3: the screenshots ----------


def _referenced() -> set[str]:
    return set(re.findall(r"screens/([a-z-]+)-(?:light|dark)\.webp", PAGE + CSS))


def test_ac3_every_screenshot_the_page_shows_exists():
    missing = [f"{name}-dark.webp" for name in _referenced() if not (SITE / "screens" / f"{name}-dark.webp").is_file()]
    assert _referenced() and missing == []


def test_ac3_the_page_is_dark_and_shows_the_dark_captures():
    assert "color-scheme: dark" in CSS and "-light.webp" not in PAGE and "-light.webp" not in CSS


def test_ac3_every_screenshot_shown_is_one_the_capture_script_makes():
    script = (ROOT / "scripts" / "capture_screens.py").read_text(encoding="utf-8")
    recorder = (ROOT / "scripts" / "record_hero.py").read_text(encoding="utf-8")
    made = set(re.findall(r'shot\("([a-z-]+)"', script)) | {f"settings-{t}" for t in re.findall(
        r'for tab in \(([^)]*)\)', script)[0].replace('"', "").replace(" ", "").split(",")}
    # The recording's poster is the one picture the record script saves through the same `save`.
    made |= set(re.findall(r'demo\.save\(poster, "([a-z-]+)-dark"\)', recorder))
    assert _referenced() <= made


def test_ac3_no_screenshot_shows_this_computer():
    script = (ROOT / "scripts" / "capture_screens.py").read_text(encoding="utf-8")
    assert "refusing to save a screenshot that still shows" in script and "host.lan_addresses()" in script


# ---------- AC-4: the estimator ----------


def test_ac4_people_time_machine_time_and_cost_are_kept_apart():
    result = _estimate({})
    assert set(result) == {"pages", "people", "machine", "cost"} and "totalHours" not in json.dumps(result)


def test_ac4_the_figures_follow_the_formulas():
    r = _estimate({"documents": 100, "pagesPerDocument": 2, "transcribeMinutesPerPage": 30, "indexMinutesPerDocument": 6,
                   "reviewMinutesPerPage": 6, "reviewIndexMinutesPerDocument": 3, "machineSecondsPerPage": 18,
                   "costPerPage": 0.01})
    assert (r["pages"], r["people"]["byHandHours"], r["people"]["withIncipitHours"], r["people"]["savedHours"],
            r["machine"]["hours"], round(r["cost"]["dollars"], 6)) == (200, 110, 25, 85, 1, 2)


def test_ac4_a_half_typed_number_falls_back_to_the_default():
    assert _estimate({"documents": "abc"})["pages"] == _estimate({})["pages"]


def test_ac4_every_default_is_labelled_as_an_assumption_or_a_measurement():
    form = PAGE[PAGE.index('id="estimator"'):PAGE.index("</form>")]
    labels = re.findall(r"<label>(.*?)</label>", form, re.S)
    assert all("<small>" in label for label in labels[1:]) and "never added up" in form


# ---------- AC-5: the download ----------


def test_ac5_every_download_button_leads_to_the_newest_release():
    buttons = re.findall(r'<a[^>]*data-download[^>]*>', PAGE)
    assert buttons and all("github.com/getincipit/incipit/releases/latest" in b for b in buttons)


def test_ac5_the_page_reads_the_newest_release_from_githubs_api():
    assert "api.github.com/repos/${REPOSITORY}/releases/latest" in SCRIPT and 'REPOSITORY = "getincipit/incipit"' in SCRIPT


def test_ac5_the_page_falls_back_to_the_releases_page():
    assert ".catch(" in SCRIPT and "releases/latest" in SCRIPT[SCRIPT.index(".catch("):]


# ---------- AC-6: self-contained ----------


def test_ac6_no_script_style_or_font_comes_from_another_site():
    external = re.findall(r'<(?:script|link|img|source)[^>]*(?:src|href|srcset)="(https?:)?//', PAGE)
    assert external == [] and "@import" not in CSS and "url(http" not in CSS


def test_ac6_there_is_no_analytics():
    assert not re.search(r"analytics|gtag|googletagmanager|plausible|segment\.", PAGE + SCRIPT, re.I)


def test_ac6_every_local_file_the_page_names_exists():
    local = set(re.findall(r'(?:src|href|srcset)="(?!https?:|#|mailto:)([^"]+)"', PAGE)) | set(re.findall(r'url\("([^"]+)"\)', CSS))
    assert [path for path in local if not (SITE / path).is_file()] == []


def test_ac6_the_pen_and_the_icon_are_the_programs_own_drawings():
    assert all((SITE / name).read_bytes() == (ROOT / "app" / "static" / name).read_bytes() for name in ("logo.svg", "favicon.svg"))


# ---------- AC-7: motion ----------


def test_ac7_reduced_motion_shows_everything_at_rest():
    calm = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    calm = calm[:calm.index("\n}\n")]
    assert ".js .reveal" in calm and "opacity: 1" in calm and "animation: none" in calm


def _keyframe_bodies() -> list[str]:
    bodies = []
    for match in re.finditer(r"@keyframes [a-z-]+ \{", CSS):
        depth, start = 1, match.end()
        for i in range(start, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[i], 0)
            if depth == 0:
                bodies.append(CSS[start:i])
                break
    return bodies


def test_ac7_motion_uses_only_transforms_and_opacity():
    properties = {part.split()[0] for t in re.findall(r"transition:\s*([^;]+);", CSS) for part in t.split(",") if part.strip()}
    for body in _keyframe_bodies():
        properties |= set(re.findall(r"([a-z-]+)\s*:", body))
    assert properties <= {"opacity", "transform", "background", "box-shadow", "border-color", "none"}


def test_ac7_without_scripts_nothing_is_hidden():
    bare = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    rules = re.findall(r"^(\S[^{]*\.(?:reveal|rise)[^{]*)\{", bare, re.M)
    assert rules and all(rule.strip().startswith(".js ") for rule in rules)


# ---------- AC-9: publishing ----------


def test_ac9_the_publish_script_pushes_the_site_folder_to_the_pages_repository():
    script = (ROOT / "scripts" / "publish_site.py").read_text(encoding="utf-8")
    assert 'SITE_REPOSITORY = "getincipit/getincipit.github.io"' in script and 'PAGE_URL = "https://getincipit.github.io/"' in script
    assert 'shutil.copytree(ROOT / "site", work)' in script and ".nojekyll" in script


def test_ac9_published_asset_links_are_versioned_by_their_content(tmp_path):
    """Regression (review 2026-10-22): the version was the commit, so two publishes of unsaved work shared
    addresses and a cached stylesheet could pair with a new page."""
    import hashlib
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish_site
    shutil.copytree(SITE, tmp_path / "site")
    publish_site.stamp_assets(tmp_path / "site" / "index.html")
    stamped = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    digest = hashlib.sha256((SITE / "site.css").read_bytes()).hexdigest()[:10]
    assert f'"site.css?v={digest}"' in stamped and 'href="site.css"' not in stamped
    assert "rev-parse" not in (ROOT / "scripts" / "publish_site.py").read_text(encoding="utf-8").split("def stamp_assets")[1].split("def publish")[0]


def test_ac7_the_recording_can_be_paused_by_anyone():
    hero = PAGE[PAGE.index('<section class="hero">'):PAGE.index("</section>")]
    assert 'data-hero-pause' in hero and 'aria-pressed="false"' in hero and 'aria-label="Pause the recording"' in hero
    assert "heroHeld" in SCRIPT and 'setAttribute("aria-label", paused ? "Play the recording" : "Pause the recording")' in SCRIPT
