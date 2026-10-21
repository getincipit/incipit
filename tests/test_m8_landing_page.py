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


def test_ac1_the_hero_has_the_app_icon_the_headline_and_the_download():
    hero = PAGE[PAGE.index('<section class="hero">'):PAGE.index("</section>")]
    assert 'class="app-icon' in hero and "Download Incipit" in hero and "Download for Windows" in hero


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


def test_ac3_every_screenshot_exists_in_light_and_dark():
    missing = [f"{name}-{theme}.webp" for name in _referenced() for theme in ("light", "dark")
               if not (SITE / "screens" / f"{name}-{theme}.webp").is_file()]
    assert _referenced() and missing == []


def test_ac3_the_reader_s_theme_picks_the_screenshot():
    pictures = re.findall(r"<picture[^>]*>(.*?)</picture>", PAGE, re.S)
    assert pictures and all('media="(prefers-color-scheme: dark)"' in p and "-dark.webp" in p and "-light.webp" in p
                            for p in pictures)


def test_ac3_every_screenshot_shown_is_one_the_capture_script_makes():
    script = (ROOT / "scripts" / "capture_screens.py").read_text(encoding="utf-8")
    made = set(re.findall(r'shot\("([a-z-]+)"', script)) | {f"settings-{t}" for t in re.findall(
        r'for tab in \(([^)]*)\)', script)[0].replace('"', "").replace(" ", "").split(",")}
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
    assert buttons and all("github.com/parthaped/incipit/releases/latest" in b for b in buttons)


def test_ac5_the_page_reads_the_newest_release_from_githubs_api():
    assert "api.github.com/repos/${REPOSITORY}/releases/latest" in SCRIPT and 'REPOSITORY = "parthaped/incipit"' in SCRIPT


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


def test_ac7_motion_uses_only_transforms_and_opacity():
    animated = re.findall(r"transition:\s*([^;]+);", CSS) + re.findall(r"@keyframes [a-z-]+ \{([^}]*\})", CSS)
    properties = {m.split()[0] for t in animated for m in t.split(",") if m.strip() and not m.strip().startswith("to")}
    assert properties <= {"opacity", "transform", "background", "box-shadow", "border-color", "none"}


def test_ac7_without_scripts_nothing_is_hidden():
    bare = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    rules = re.findall(r"^(\S[^{]*\.(?:reveal|rise)[^{]*)\{", bare, re.M)
    assert rules and all(rule.strip().startswith(".js ") for rule in rules)


# ---------- AC-9: publishing ----------


def test_ac9_a_workflow_publishes_the_site_folder_to_pages():
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "actions/upload-pages-artifact" in workflow and "path: site" in workflow and "actions/deploy-pages" in workflow
