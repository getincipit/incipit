"""How the site looks: the appearance settings, their validation, and the CSS they become."""

from __future__ import annotations

import re

# Light and dark backgrounds per surface tone; the accent is checked against these.
SURFACES = {
    "neutral": {"light": {"--bg": "#fbfbfd", "--panel": "#ffffff", "--gray-bg": "#f0f0f3",
                          "--border-soft": "#ebebef"},
                "dark": {"--bg": "#141416", "--panel": "#1e1e21", "--gray-bg": "#2a2a2f",
                         "--border-soft": "#2a2a2f"}},
    "warm": {"light": {"--bg": "#fbfaf6", "--panel": "#fffefb", "--gray-bg": "#f3f0e9",
                       "--border-soft": "#ece7dc"},
             "dark": {"--bg": "#161512", "--panel": "#201e1a", "--gray-bg": "#2d2a25",
                      "--border-soft": "#2d2a25"}},
    "cool": {"light": {"--bg": "#f8fafc", "--panel": "#ffffff", "--gray-bg": "#eef2f6",
                       "--border-soft": "#e5eaf0"},
             "dark": {"--bg": "#121418", "--panel": "#1b1e23", "--gray-bg": "#272b31",
                      "--border-soft": "#272b31"}},
}

# Font menus are system stacks; nothing is downloaded, and an owner may upload a WOFF2 instead.
FONT_STACKS = {
    "system": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    "serif": 'Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif',
    "humanist": 'Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", source-sans-pro, sans-serif',
    "mono": 'ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, Consolas, monospace',
}

BACKDROP_KINDS = ("none", "colour", "album", "video", "animation", "preset")
PRESETS = ("paper", "ink", "lines")
TRANSITIONS = ("crossfade", "pan")
MAX_CUSTOM_CSS = 50 * 1024
MIN_ACCENT_CONTRAST = 3.0
# The ink style.css puts on a filled accent control (--on-colour) in each theme.
ON_ACCENT = {"light": "#ffffff", "dark": "#111318"}

DEFAULTS = {
    "tagline": "Transcription review",
    "welcome": "Sign in to see the folders assigned to you.",
    "footer": "",
    "logo_asset": None,
    "favicon_asset": None,
    "accent_light": "#0071e3",
    "accent_dark": "#4a9eff",
    "surface": "neutral",
    "heading_font": "system",
    "body_font": "system",
    "radius": 12,
    "custom_css": "",
    "backdrop": {
        "kind": "none", "colour_light": "#eef2f7", "colour_dark": "#1a1c20",
        "album": {"assets": [], "transition": "crossfade", "interval": 8, "captions": True,
                  "shuffle": False},
        "video": {"asset": None, "poster": None},
        "animation": {"asset": None},
        "preset": "paper",
    },
}
TEXT_LIMITS = {"tagline": 80, "welcome": 280, "footer": 200}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class AppearanceError(ValueError):
    """An appearance choice the person can see and fix."""


def _merged(defaults: dict, given: dict) -> dict:
    out = {}
    for key, default in defaults.items():
        value = given.get(key, default) if isinstance(given, dict) else default
        out[key] = _merged(default, value) if isinstance(default, dict) else value
    return out


def with_defaults(stored: dict) -> dict:
    """Stored appearance over the defaults, so a field added later reads as its default."""
    return _merged(DEFAULTS, stored or {})


def luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(ink: str, ground: str) -> float:
    light, dark = sorted((luminance(ink), luminance(ground)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _colour(name: str, value) -> str:
    if not isinstance(value, str) or not _HEX.match(value):
        raise AppearanceError(f"{name.replace('_', ' ')} must be a colour like #1a73e8")
    return value.lower()


def _asset_id(name: str, value):
    if value is None or value == "":
        return None
    if isinstance(value, int) and value > 0:
        return value
    raise AppearanceError(f"{name.replace('_', ' ')} must name an uploaded file")


def _font(name: str, value):
    if value in FONT_STACKS:
        return value
    if isinstance(value, int) and value > 0:
        return value
    raise AppearanceError(f"{name.replace('_', ' ')} must be one of {', '.join(FONT_STACKS)} "
                          f"or an uploaded font")


def validate(given: dict, *, owner: bool, previous: dict | None = None) -> dict:
    """A complete, checked appearance; custom CSS and uploaded fonts are the owner's to change."""
    previous = with_defaults(previous or {})
    look = with_defaults(given)
    if not owner:
        for key in ("custom_css",):
            if look[key] != previous[key]:
                raise PermissionError("only the owner can change the custom CSS")
        for key in ("heading_font", "body_font"):
            if look[key] != previous[key] and not isinstance(look[key], str):
                raise PermissionError("only the owner can choose an uploaded font")
    for key, limit in TEXT_LIMITS.items():
        text = str(look[key] or "").strip()
        if len(text) > limit:
            raise AppearanceError(f"the {key} is at most {limit} characters")
        look[key] = text
    for key in ("logo_asset", "favicon_asset"):
        look[key] = _asset_id(key, look[key])
    for key, theme in (("accent_light", "light"), ("accent_dark", "dark")):
        look[key] = _colour(key, look[key])
    if look["surface"] not in SURFACES:
        raise AppearanceError(f"the surface is one of {', '.join(SURFACES)}")
    for key, theme in (("accent_light", "light"), ("accent_dark", "dark")):
        ground = SURFACES[look["surface"]][theme]["--bg"]
        if contrast(look[key], ground) < MIN_ACCENT_CONTRAST:
            raise AppearanceError(f"that accent has not enough contrast against the {theme} "
                                  f"background ({contrast(look[key], ground):.1f}:1, needs 3:1)")
        if contrast(ON_ACCENT[theme], look[key]) < MIN_ACCENT_CONTRAST:
            raise AppearanceError(f"text on that {theme} accent would be hard to read "
                                  f"({contrast(ON_ACCENT[theme], look[key]):.1f}:1, needs 3:1)")
    look["heading_font"] = _font("heading_font", look["heading_font"])
    look["body_font"] = _font("body_font", look["body_font"])
    try:
        look["radius"] = int(look["radius"])
    except (TypeError, ValueError):
        raise AppearanceError("the corner radius is a number of pixels") from None
    if not 0 <= look["radius"] <= 24:
        raise AppearanceError("the corner radius is between 0 and 24 pixels")
    css = str(look["custom_css"] or "")
    if len(css.encode("utf-8")) > MAX_CUSTOM_CSS:
        raise AppearanceError("custom CSS is at most 50 KB")
    look["custom_css"] = css
    look["backdrop"] = _backdrop(look["backdrop"])
    return look


def _backdrop(backdrop: dict) -> dict:
    if backdrop["kind"] not in BACKDROP_KINDS:
        raise AppearanceError(f"the sign-in backdrop is one of {', '.join(BACKDROP_KINDS)}")
    backdrop["colour_light"] = _colour("backdrop colour", backdrop["colour_light"])
    backdrop["colour_dark"] = _colour("backdrop colour", backdrop["colour_dark"])
    album = backdrop["album"]
    if not isinstance(album["assets"], list) or len(album["assets"]) > 40:
        raise AppearanceError("an album holds up to 40 photographs")
    album["assets"] = [_asset_id("album photograph", a) for a in album["assets"]]
    if album["transition"] not in TRANSITIONS:
        raise AppearanceError("the album moves by crossfade or pan")
    try:
        album["interval"] = int(album["interval"])
    except (TypeError, ValueError):
        raise AppearanceError("the album interval is a number of seconds") from None
    if not 4 <= album["interval"] <= 30:
        raise AppearanceError("each photograph shows for 4 to 30 seconds")
    album["captions"], album["shuffle"] = bool(album["captions"]), bool(album["shuffle"])
    backdrop["video"]["asset"] = _asset_id("video", backdrop["video"]["asset"])
    backdrop["video"]["poster"] = _asset_id("video poster", backdrop["video"]["poster"])
    backdrop["animation"]["asset"] = _asset_id("animation", backdrop["animation"]["asset"])
    if backdrop["preset"] not in PRESETS:
        raise AppearanceError(f"the preset is one of {', '.join(PRESETS)}")
    needs = {"album": album["assets"], "video": backdrop["video"]["asset"],
             "animation": backdrop["animation"]["asset"]}.get(backdrop["kind"], True)
    if not needs:
        raise AppearanceError(f"choose a file for the {backdrop['kind']} backdrop first")
    return backdrop


# ---------- custom CSS ----------

_CSS_BLOCKED = [
    (re.compile(r"@import[^;]*;?", re.I), "@import"),
    (re.compile(r"expression\s*\(", re.I), "expression()"),
    (re.compile(r"behavior\s*:", re.I), "behavior:"),
    (re.compile(r"-moz-binding\s*:", re.I), "-moz-binding"),
    (re.compile(r"javascript\s*:", re.I), "javascript:"),
    (re.compile(r"</\s*style", re.I), "</style"),
]
# url() may point only at this site's own uploaded files.
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)


def sanitize_css(css: str) -> tuple[str, list[str]]:
    """(the CSS with active or foreign content removed, what was removed)."""
    removed = []
    for pattern, label in _CSS_BLOCKED:
        css, count = pattern.subn("", css)
        if count:
            removed.append(label)

    def keep_own(match):
        target = match.group(2).strip()
        if target.startswith("/site/assets/") and ".." not in target:
            return f'url("{target}")'
        removed.append(f"url({target[:40]})")
        return "none"

    return _CSS_URL.sub(keep_own, css), removed


# ---------- the stylesheet ----------


def _shift(colour: str, amount: float) -> str:
    """Moves a colour toward white (amount > 0) or black (amount < 0)."""
    channels = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    target = 255 if amount > 0 else 0
    return "#" + "".join(f"{round(c + (target - c) * abs(amount)):02x}" for c in channels)


def _rgba(colour: str, alpha: float) -> str:
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def _tokens(look: dict, theme: str) -> dict:
    accent = look[f"accent_{theme}"]
    light = theme == "light"
    return {
        **SURFACES[look["surface"]][theme],
        "--accent": accent,
        "--accent-hover": _shift(accent, 0.08),
        "--accent-active": _shift(accent, -0.06),
        "--accent-soft": _shift(accent, 0.9) if light else _rgba(accent, 0.16),
        "--accent-soft-hover": _shift(accent, 0.84) if light else _rgba(accent, 0.24),
        "--accent-ink": _shift(accent, -0.2) if light else _shift(accent, 0.3),
        "--radius": f"{look['radius']}px",
        "--radius-sm": f"{max(0, round(look['radius'] * 2 / 3))}px",
    }


def _font_family(choice, fonts: dict) -> str:
    if isinstance(choice, int) and choice in fonts:
        return f'"site-font-{choice}", {FONT_STACKS["system"]}'
    return FONT_STACKS.get(choice, FONT_STACKS["system"])


def _block(selector: str, tokens: dict) -> str:
    return selector + " {\n" + "".join(f"  {k}: {v};\n" for k, v in tokens.items()) + "}\n"


def stylesheet(look: dict, fonts: dict | None = None) -> str:
    """`/site.css`: the tokens for both themes, the fonts, and the owner's CSS last.

    `fonts` maps an uploaded font's asset id to its URL.
    """
    look = with_defaults(look)
    fonts = fonts or {}
    light, dark = _tokens(look, "light"), _tokens(look, "dark")
    parts = ["/* Generated from the site's appearance settings. */\n"]
    for asset_id, url in sorted(fonts.items()):
        parts.append(f'@font-face {{ font-family: "site-font-{asset_id}"; src: url("{url}") '
                     f'format("woff2"); font-display: swap; }}\n')
    parts.append(_block(":root", {**light,
                                  "--font-body": _font_family(look["body_font"], fonts),
                                  "--font-heading": _font_family(look["heading_font"], fonts)}))
    parts.append(_block(':root[data-theme="dark"]', dark))
    parts.append("@media (prefers-color-scheme: dark) {\n"
                 + _block(':root:not([data-theme="light"])', dark) + "}\n")
    parts.append("body { font-family: var(--font-body); }\n"
                 "h1, h2, h3, .brand-name { font-family: var(--font-heading); }\n")
    if look["custom_css"]:
        css, _ = sanitize_css(look["custom_css"])
        parts.append("/* The owner's own CSS. */\n" + css + "\n")
    return "".join(parts)
