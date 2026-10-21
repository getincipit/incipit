"""Draws logo.svg (black pen) and favicon.svg (white pen on an ink tile): one slim upright pen through
the ages of writing, the I of Incipit, with a quill plume drawn barb by barb."""
import math
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
W, H = 20, 70
CX = 10


def bez(p0, p1, p2, p3, t):
    u = 1 - t
    return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def bez_tangent(p0, p1, p2, p3, t):
    u = 1 - t
    return (3 * u * u * (p1[0] - p0[0]) + 6 * u * t * (p2[0] - p1[0]) + 3 * t * t * (p3[0] - p2[0]),
            3 * u * u * (p1[1] - p0[1]) + 6 * u * t * (p2[1] - p1[1]) + 3 * t * t * (p3[1] - p2[1]))


# The rachis: from the holder's mouth up to the tip, leaning a little to the right as a cut quill does.
R0, R1, R2, R3 = (CX, 23.6), (CX - 0.6, 16), (CX + 0.4, 8), (CX + 2.4, 1.2)


def plume(ink: str) -> str:
    parts = [f'<path d="M{R0[0]} {R0[1]} C{R1[0]} {R1[1]} {R2[0]} {R2[1]} {R3[0]} {R3[1]}" fill="none" '
             f'stroke="{ink}" stroke-width="1.05" stroke-linecap="round"/>',
             f'<path d="M{CX} 23.8 C{CX - 0.3} 20 {CX - 0.2} 17 {CX} 14" fill="none" stroke="{ink}" '
             f'stroke-width="0.5" stroke-linecap="round" opacity="0.5"/>']
    # Barbs leave the shaft angled towards the tip; the outer vane (left) is the wide one. Two gaps in the
    # outer vane are where the barbs have parted, as they do on a used quill.
    gaps = [(0.36, 0.41), (0.62, 0.65)]
    n = 30
    for i in range(n):
        t = 0.06 + 0.9 * i / (n - 1)
        x, y = bez(R0, R1, R2, R3, t)
        tx, ty = bez_tangent(R0, R1, R2, R3, t)
        length = math.hypot(tx, ty)
        tx, ty = tx / length, ty / length
        # Normal pointing to the page's left of the shaft, which leans the other way.
        nx, ny = -ty, tx
        envelope = math.sin(math.pi * min(1.0, (t + 0.05) / 1.05)) ** 0.7
        taper = 1 - 0.55 * max(0.0, (t - 0.7) / 0.3)
        lean = 0.62  # how far each barb leans towards the tip
        for side, reach in ((-1, 6.4), (1, 3.1)):
            if side == -1 and any(a <= t <= b for a, b in gaps):
                continue
            L = reach * envelope * taper
            if L < 0.4:
                continue
            ex = x + side * nx * L + tx * L * lean
            ey = y + side * ny * L + ty * L * lean
            # A slight curve: the barb leaves the shaft a little flatter than it ends.
            mx = x + side * nx * L * 0.55 + tx * L * 0.18
            my = y + side * ny * L * 0.55 + ty * L * 0.18
            width = 0.42 if i % 3 else 0.5
            parts.append(f'<path d="M{x:.2f} {y:.2f} Q{mx:.2f} {my:.2f} {ex:.2f} {ey:.2f}" fill="none" '
                         f'stroke="{ink}" stroke-width="{width}" stroke-linecap="round"/>')
    # A few wisps of afterfeather at the base, pointing down and out.
    for dx, dy, L in ((-1, 1, 2.4), (-1, 1.6, 1.7), (1, 1.1, 1.8), (1, 1.8, 1.2)):
        x, y = CX, 22.2
        parts.append(f'<path d="M{x} {y} Q{x + dx * L * 0.6:.2f} {y + dy * 0.6:.2f} {x + dx * L:.2f} {y + dy * 1.4:.2f}" '
                     f'fill="none" stroke="{ink}" stroke-width="0.4" stroke-linecap="round" opacity="0.8"/>')
    return "\n    ".join(parts)


def body(ink: str, paper: str) -> str:
    return f'''<!-- The wooden holder: a turned shape, fuller at the grip, with grain and a highlight. -->
    <path d="M7.5 23.6 C7.1 26.2 6.8 28.4 6.9 30.6 C7 33.4 7.2 35.8 7.3 38 L12.7 38 C12.8 35.8 13 33.4 13.1 30.6 C13.2 28.4 12.9 26.2 12.5 23.6 Z" fill="{ink}"/>
    <path d="M8.3 25 C8 28 7.9 32 8.1 37" fill="none" stroke="{paper}" stroke-width="0.55" stroke-linecap="round" opacity="0.9"/>
    <path d="M11.3 26 C11.5 29 11.6 33 11.5 36.6" fill="none" stroke="{paper}" stroke-width="0.3" stroke-linecap="round" opacity="0.6"/>
    <path d="M7.1 26.4 H12.9" stroke="{paper}" stroke-width="0.4" opacity="0.7"/>
    <!-- The ferrule: a brass sleeve with two turned rings. -->
    <path d="M6.6 38.4 H13.4 L13.2 43 H6.8 Z" fill="{ink}"/>
    <path d="M6.6 38 H13.4" stroke="{paper}" stroke-width="0.5"/>
    <path d="M6.7 39.9 H13.3 M6.8 41.6 H13.2" stroke="{paper}" stroke-width="0.4" opacity="0.8"/>
    <path d="M7.9 38.6 V42.8" stroke="{paper}" stroke-width="0.5" stroke-linecap="round" opacity="0.7"/>
    <!-- The fountain pen's section, tapering to the nib, with the light down one side. -->
    <path d="M6.9 43.5 H13.1 L12.5 48.6 H7.5 Z" fill="{ink}"/>
    <path d="M6.9 43.2 H13.1" stroke="{paper}" stroke-width="0.5"/>
    <path d="M8.1 44.2 L8.4 48" stroke="{paper}" stroke-width="0.55" stroke-linecap="round" opacity="0.8"/>
    <!-- The nib: shoulders, a breather hole, the slit between the tines, and a tipped point. -->
    <path d="M7.1 48.9 C6.9 52.8 8.3 57.6 10 62.4 C11.7 57.6 13.1 52.8 12.9 48.9 Z" fill="{ink}"/>
    <path d="M7.5 48.6 H12.5" stroke="{paper}" stroke-width="0.5"/>
    <circle cx="10" cy="52" r="0.8" fill="{paper}"/>
    <path d="M10 53 V60.2" stroke="{paper}" stroke-width="0.55" stroke-linecap="round"/>
    <path d="M7.9 50 C7.8 52.6 8.5 55.4 9.3 57.8" fill="none" stroke="{paper}" stroke-width="0.35" stroke-linecap="round" opacity="0.7"/>
    <!-- The mark it makes now: a pixel. -->
    <rect x="8.6" y="65" width="2.8" height="2.8" rx="0.5" fill="{ink}"/>'''


def svg(ink: str, paper: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Incipit">
  <!-- One slim upright pen, which is also the I of Incipit, built from the ages of writing: a quill's
       plume drawn barb by barb, a turned dip-pen holder, a brass ferrule, a fountain pen's section, a
       steel nib, and the pixel its line becomes. Generated by scripts/draw_logo.py; edit that, not this. -->
  <g>
    {plume(ink)}
    {body(ink, paper)}
  </g>
</svg>
'''


(STATIC / "logo.svg").write_text(svg("#111111", "#ffffff"), encoding="utf-8")
inner = svg("#ffffff", "#111111")
inner = inner[inner.index("<g>"):inner.rindex("</svg>")]
(STATIC / "favicon.svg").write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <!-- The pen of logo.svg in white on an ink tile, the shape a browser tab can show. -->
  <rect width="64" height="64" rx="15" fill="#111111"/>
  <g transform="translate(23.2 2.5) scale(0.88)">
  {inner}
  </g>
</svg>
''', encoding="utf-8")
print("drawn")
