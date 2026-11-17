"""The Plant Parlour brochure: two pages, A4, with the maintenance prices - to send to a business that asks
for rates.

    python marketing/build_brochure.py --phone "+91 98xxx xxxxx"            # -> marketing/own/Plant-Parlour-brochure.pdf
    python marketing/build_brochure.py --phone "+91 ..." --png              # also page images to check the layout

The look is taken over from the owner's clinic brochure (clinic-growth, marketing/build_brochure.py): a serif
headline face (Playfair Display) with a clean sans body (Inter), deep navy with a muted gold accent, warm white,
generous space, very little text, and drawn illustrations (inline SVG, made here: no stock images, nothing to
license) - here with leaf greens for the plants. The words and prices live in marketing/brochure.json.

The phone number is passed on the command line and never stored: this repository is public. The PDF with the
number goes to marketing/own/, which git ignores.

Printed to PDF by Chromium (Playwright, through node), with the fonts embedded (marketing/fonts, SIL OFL).
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INK, NAVY, NAVY2 = "#0E1A2B", "#13294B", "#1F3A63"
GOLD, GOLD_L, GOLD_XL = "#B08D57", "#E6D7BC", "#F4ECDD"
IVORY, PANEL = "#FBF9F4", "#F3EEE4"
SKY, SKY_D = "#D3DDEA", "#8EA6C6"
LEAF, LEAF_L, LEAF_D = "#4E8A5E", "#9CC5A5", "#2E5E3E"
POT, SOIL = "#B9774E", "#5A3B28"
SKIN, HAIR = "#E7B48E", "#3B2A22"
GREY, LINE = "#566070", "#E4DDCF"
WA = "#25A35A"


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def fonts_css() -> str:
    out = []
    for f in sorted((HERE / "fonts").glob("*.woff2")):
        family = "Serif" if f.name.startswith("playfair") else "Sans"
        parts = f.stem.split("-")
        weight, style = parts[-2], parts[-1]
        data = base64.b64encode(f.read_bytes()).decode("ascii")
        out.append(f"@font-face {{ font-family: B{family}; font-weight: {weight}; font-style: {style}; "
                   f"src: url(data:font/woff2;base64,{data}) format('woff2'); }}")
    return "\n".join(out)


def rupees(n: int) -> str:
    """Indian grouping: 3,500 / 14,000 / 1,40,000."""
    s = str(int(n))
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join(groups + [tail])


# ------------------------------------------------------------------------------------------- illustrations
def svg(body: str, w: int = 160, h: int = 120, bg: bool = True) -> str:
    back = (f'<ellipse cx="{w / 2}" cy="{h / 2 + 4}" rx="{w * 0.36}" ry="{h * 0.43}" fill="{PANEL}"/>'
            f'<ellipse cx="{w / 2}" cy="{h - 10}" rx="{w * 0.33}" ry="5" fill="{GOLD_L}" opacity=".55"/>' if bg else "")
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" stroke-linecap="round" '
            f'stroke-linejoin="round">{back}{body}</svg>')


def pot(x: float, y: float, w: float = 30, h: float = 24, color: str = POT) -> str:
    """A pot whose rim's top-left corner is at (x, y)."""
    return (f'<path d="M{x + 3} {y + 6} L{x + 7} {y + h} H{x + w - 7} L{x + w - 3} {y + 6}z" fill="{color}"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="7" rx="2" fill="{color}"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="2.5" rx="1.2" fill="{SOIL}" opacity=".35"/>')


def leaf(cx: float, cy: float, length: float, angle: float, fill: str = LEAF) -> str:
    return (f'<g transform="translate({cx} {cy}) rotate({angle})"><path d="M0 0 C{length * .35} {-length * .32} '
            f'{length * .75} {-length * .3} {length} 0 C{length * .75} {length * .3} {length * .35} {length * .32} 0 0z" '
            f'fill="{fill}"/><path d="M2 0 H{length * .85}" stroke="{LEAF_D}" stroke-width="1" opacity=".45"/></g>')


def bush(x: float, y: float, s: float = 1.0) -> str:
    """A leafy potted plant; (x, y) = middle of the pot rim."""
    leaves = "".join(leaf(x, y - 4, 26 * s, a, LEAF if i % 2 else LEAF_D)
                     for i, a in enumerate((-160, -130, -105, -80, -55, -25)))
    return f'<path d="M{x} {y} V{y - 14 * s}" stroke="{LEAF_D}" stroke-width="2"/>{leaves}' + pot(x - 15 * s, y, 30 * s, 24 * s)


def flower_plant(x: float, y: float, s: float = 1.0, petal: str = "#E07A7A") -> str:
    out = [f'<path d="M{x} {y} C{x - 2} {y - 14} {x - 10} {y - 24} {x - 12} {y - 34} M{x} {y} V{y - 40} '
           f'M{x} {y} C{x + 2} {y - 14} {x + 10} {y - 22} {x + 13} {y - 30}" stroke="{LEAF_D}" stroke-width="2" fill="none"/>',
           leaf(x, y - 10, 16 * s, -150, LEAF), leaf(x, y - 14, 16 * s, -30, LEAF)]
    for fx, fy in ((x - 12, y - 36), (x, y - 43), (x + 13, y - 32)):
        out += [f'<circle cx="{fx + dx}" cy="{fy + dy}" r="{4.2 * s}" fill="{petal}"/>'
                for dx, dy in ((0, -4.5), (4.5, 0), (0, 4.5), (-4.5, 0))]
        out.append(f'<circle cx="{fx}" cy="{fy}" r="{2.8 * s}" fill="{GOLD}"/>')
    return "".join(out) + pot(x - 14 * s, y, 28 * s, 22 * s, GOLD)


def snake_plant(x: float, y: float) -> str:
    blades = "".join(f'<path d="M{x + dx} {y} C{x + dx - 3} {y - h * .5} {x + dx + tilt} {y - h * .8} {x + dx + tilt} {y - h} '
                     f'C{x + dx + tilt + 3} {y - h * .7} {x + dx + 6} {y - h * .4} {x + dx + 5} {y}z" fill="{col}"/>'
                     for dx, h, tilt, col in ((-8, 44, -6, LEAF_D), (-2, 54, 0, LEAF), (4, 46, 6, LEAF_D)))
    return blades + pot(x - 13, y, 26, 22, NAVY2)


def drops(x: float, y: float) -> str:
    return "".join(f'<path d="M{x + dx} {y + dy} c-2 3 -2.5 5 0 6 c2.5 -1 2 -3 0 -6z" fill="{SKY_D}"/>'
                   for dx, dy in ((0, 0), (7, 6), (-4, 10), (4, 16)))


ART = {
    "care": lambda: svg(
        bush(62, 76, 1.05)
        + f'<path d="M98 40 h26 l6 22 h-34z" fill="{NAVY}"/>'
          f'<path d="M96 46 L74 34" stroke="{NAVY}" stroke-width="4"/><rect x="68" y="30" width="9" height="5" rx="2" '
          f'transform="rotate(-28 72 32)" fill="{NAVY}"/>'
          f'<path d="M124 46 q12 2 10 14" fill="none" stroke="{NAVY}" stroke-width="3.5"/>'
        + drops(66, 40)
        + f'<g transform="translate(112 74)"><path d="M0 0 L16 22 M16 0 L0 22" stroke="{GOLD}" stroke-width="3"/>'
          f'<circle cx="0" cy="26" r="4.5" fill="none" stroke="{GOLD}" stroke-width="2.6"/>'
          f'<circle cx="16" cy="26" r="4.5" fill="none" stroke="{GOLD}" stroke-width="2.6"/></g>'),
    "supply": lambda: svg(
        flower_plant(44, 80, 1.0) + bush(82, 82, 1.15) + snake_plant(120, 82)
        + f'<path d="M22 104 H140" stroke="{GOLD}" stroke-width="2.4"/>'),
    "feed": lambda: svg(
        f'<path d="M30 40 h40 l4 62 h-48z" fill="{GOLD_XL}" stroke="{NAVY}" stroke-width="2.4"/>'
        f'<path d="M30 40 q20 -10 40 0" fill="{GOLD_L}" stroke="{NAVY}" stroke-width="2.4"/>'
        + leaf(42, 70, 16, -45, LEAF) + leaf(52, 70, 16, -135, LEAF_D)
        + f'<rect x="37" y="82" width="26" height="4" rx="2" fill="{SKY_D}"/><rect x="40" y="90" width="20" height="3.5" rx="1.7" fill="{SKY}"/>'
        + bush(110, 82, 0.95)
        + "".join(f'<circle cx="{cx}" cy="{cy}" r="2.2" fill="{GOLD}"/>' for cx, cy in ((82, 52), (88, 62), (79, 66), (92, 48)))
        + f'<path d="M136 26 l1.6 4.6 4.6 1.6 -4.6 1.6 -1.6 4.6 -1.6 -4.6 -4.6 -1.6 4.6 -1.6z" fill="{GOLD}"/>'),
    "chat": lambda: svg(
        f'<rect x="52" y="12" width="56" height="96" rx="10" fill="{NAVY}"/>'
        f'<rect x="57" y="21" width="46" height="78" rx="4" fill="#fff"/>'
        f'<rect x="61" y="30" width="30" height="13" rx="5" fill="{SKY}"/>'
        f'<rect x="68" y="48" width="31" height="24" rx="5" fill="#DCF3E3"/>'
        + bush(83, 66, 0.42)
        + f'<rect x="61" y="77" width="26" height="13" rx="5" fill="{SKY}"/>'
          f'<circle cx="124" cy="34" r="17" fill="{WA}"/>'
          f'<g transform="translate(115 25) scale(0.75)" fill="none" stroke="#fff" stroke-width="2.6">{ICONS["phone"]}</g>'
          f'<path d="M110 46 l-6 8 l10 -3z" fill="{WA}"/>'),
    "pick": lambda: svg(
        bush(58, 78, 1.0)
        + f'<circle cx="114" cy="34" r="13" fill="{GOLD}"/>'
        + "".join(f'<path d="M{114 + 19 * c} {34 + 19 * s} L{114 + 25 * c} {34 + 25 * s}" stroke="{GOLD}" stroke-width="2.6"/>'
                  for c, s in ((1, 0), (-1, 0), (0, 1), (0, -1), (.7, .7), (-.7, .7), (.7, -.7), (-.7, -.7)))
        + f'<rect x="96" y="66" width="40" height="18" rx="4" fill="#fff" stroke="{NAVY}" stroke-width="2.2"/>'
          f'<path d="M100 78 h32" stroke="{SKY_D}" stroke-width="2"/>'
          f'<path d="M104 90 v6 M116 90 v8 M128 90 v6" stroke="{SKY_D}" stroke-width="2.4"/>'
          f'<circle cx="84" cy="96" r="10" fill="{LEAF}"/><path d="M79 96 l3.6 3.6 l6.4 -7" fill="none" stroke="#fff" stroke-width="2.6"/>'),
    "deliver": lambda: svg(
        f'<rect x="20" y="44" width="76" height="46" rx="4" fill="{NAVY}"/>'
        f'<path d="M96 56 h22 l14 16 v18 h-36z" fill="{NAVY2}"/><path d="M102 60 h14 l9 11 h-23z" fill="{SKY}"/>'
        + "".join(f'<circle cx="{cx}" cy="92" r="9" fill="{INK}"/><circle cx="{cx}" cy="92" r="3.5" fill="{GOLD_L}"/>'
                  for cx in (42, 112))
        + bush(42, 38, 0.6) + flower_plant(66, 38, 0.7)
        + f'<path d="M28 70 h58" stroke="{GOLD}" stroke-width="2.4"/>'
          f'<path d="M6 60 h10 M2 70 h12 M8 80 h8" stroke="{GOLD_L}" stroke-width="2.6"/>'),
}

ICONS = {
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 '
             '2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 '
             '0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "flower": '<circle cx="12" cy="8" r="2.4"/><path d="M12 5.6c0-2 2.6-2 2.6 0M14.4 8c2 0 2 2.6 0 2.6M12 10.4c0 2-2.6 2-2.6 0'
              'M9.6 8c-2 0-2-2.6 0-2.6M12 10.4V21M12 16c2-3 5-3 6-2-1 1-3 3-6 2zM12 18c-2-2-4.5-2-5.5-1 1 1 3 2.5 5.5 1z"/>',
    "leaf": '<path d="M5 19c0-9 6-14 15-14 0 9-5 15-14 15"/><path d="M5 19c3-5 7-8 11-10"/>',
    "pot": '<path d="M6 13h12l-1.5 8h-9zM5 11h14v2H5zM12 11V6M12 8c-3-1-5-3-5-5 3 0 5 2 5 5zM12 8c3-1 5-3 5-5-3 0-5 2-5 5z"/>',
    "snow": '<path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7M9 4l3 2 3-2M9 20l3-2 3 2"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8'
           'M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/>',
    "rain": '<path d="M7 15a4.5 4.5 0 0 1 .5-9 6 6 0 0 1 11 2.5A3.5 3.5 0 0 1 18 15z"/><path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
}


def icon(name: str, size: int = 22, color: str = GOLD, width: float = 1.7) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


def wa_badge(size: int = 22) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="{WA}"/>'
            f'<g transform="translate(5.2 5.2) scale(0.57)" fill="none" stroke="#fff" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS["phone"]}</g></svg>')


# ------------------------------------------------------------------------------------------- the pages
CSS = f"""
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: BSans, "DejaVu Sans", Arial, sans-serif; color: {INK}; font-size: 10.5pt; line-height: 1.45; background: #fff; }}
.page {{ width: 210mm; height: 297mm; padding: 15mm 17mm 12mm; position: relative; overflow: hidden;
         page-break-after: always; display: flex; flex-direction: column; background: #fff; }}
.top {{ display: flex; justify-content: space-between; align-items: baseline; padding-bottom: 10px;
        border-bottom: 1px solid {GOLD}; }}
.mark {{ font-family: BSerif; font-weight: 600; font-size: 13pt; letter-spacing: 3.5px; color: {NAVY}; }}
.mark i {{ color: {LEAF}; font-style: normal; }}
.tag {{ font-size: 7.8pt; letter-spacing: 2.2px; text-transform: uppercase; color: {GREY}; font-weight: 600; }}
h1 {{ font-family: BSerif; font-weight: 500; font-size: 36pt; line-height: 1.08; letter-spacing: -0.6px;
      color: {INK}; margin-top: 9mm; max-width: 170mm; }}
h1 em {{ font-style: italic; font-weight: 400; color: {GOLD}; }}
.sub {{ font-size: 11.8pt; color: {GREY}; margin-top: 12px; max-width: 165mm; line-height: 1.5; }}
.label {{ display: flex; align-items: center; gap: 12px; margin: 8mm 0 13px; font-size: 8pt; letter-spacing: 2.6px;
          text-transform: uppercase; color: {GOLD}; font-weight: 700; }}
.label::after {{ content: ""; flex: 1; height: 1px; background: {LINE}; }}
.grid {{ display: grid; grid-template-columns: repeat(3, 1fr); column-gap: 20px; }}
.tile .art {{ background: {IVORY}; border-radius: 16px; height: 31mm; display: flex; align-items: center;
              justify-content: center; }}
.tile .art svg {{ height: 28mm; width: auto; }}
.tile b {{ display: block; font-family: BSerif; font-weight: 600; font-size: 14pt; line-height: 1.2; color: {NAVY};
           margin-top: 10px; }}
.tile p {{ color: {GREY}; font-size: 9.8pt; margin-top: 4px; line-height: 1.4; }}
.price {{ display: inline-block; margin-top: 9px; background: {NAVY}; color: #fff; border-radius: 20px;
          padding: 4px 13px; font-weight: 700; font-size: 10.5pt; }}
.price.soft {{ background: {GOLD_XL}; color: {NAVY}; }}
.rates {{ border: 1px solid {LINE}; border-radius: 16px; overflow: hidden; }}
.rates table {{ width: 100%; border-collapse: collapse; }}
.rates th {{ background: {NAVY}; color: #fff; font-weight: 600; font-size: 9.6pt; padding: 10px 14px; text-align: left; }}
.rates th span {{ display: inline-flex; align-items: center; gap: 7px; }}
.rates td {{ padding: 11px 14px; border-top: 1px solid {LINE}; font-size: 10.5pt; }}
.rates td.plan {{ font-weight: 600; color: {NAVY}; }}
.rates td.plan .dots {{ display: inline-flex; gap: 3px; margin-left: 10px; vertical-align: middle; }}
.rates td.plan .dots i {{ width: 8px; height: 8px; border-radius: 50%; background: {LINE}; display: inline-block; }}
.rates td.plan .dots i.on {{ background: {LEAF}; }}
.rates td.amt {{ font-family: BSerif; font-weight: 600; font-size: 14pt; color: {INK}; }}
.rates td.amt small {{ font-family: BSans; font-weight: 400; font-size: 8.6pt; color: {GREY}; }}
.rates tr:nth-child(even) td {{ background: {IVORY}; }}
.note {{ color: {GREY}; font-size: 9.2pt; margin-top: 8px; }}
.strip {{ margin-top: auto; display: flex; justify-content: space-between; align-items: center; gap: 16px;
          border-top: 1px solid {LINE}; padding-top: 13px; }}
.strip .ask {{ font-family: BSerif; font-size: 13.5pt; color: {NAVY}; font-weight: 500; }}
.contact {{ display: flex; gap: 10px; align-items: center; font-weight: 700; font-size: 12pt; color: {INK}; }}
.contact small {{ font-weight: 600; font-size: 8pt; letter-spacing: 1.6px; text-transform: uppercase; color: {WA}; }}
.steps {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 15px; }}
.step .art {{ background: {IVORY}; border-radius: 16px; height: 40mm; display: flex; align-items: center;
              justify-content: center; position: relative; }}
.step .art svg {{ height: 33mm; max-width: 100%; width: auto; margin-top: 8px; }}
.step .n {{ position: absolute; top: 7px; left: 11px; font-family: BSerif; font-style: italic; font-size: 15pt; color: {GOLD}; }}
.step b {{ display: block; font-family: BSerif; font-weight: 600; font-size: 12.4pt; color: {NAVY}; margin-top: 10px; line-height: 1.25; }}
.step p {{ color: {GREY}; font-size: 9.5pt; margin-top: 3px; }}
.inds {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.ind {{ display: flex; align-items: center; gap: 10px; border: 1px solid {LINE}; border-radius: 12px; padding: 13px 12px;
        font-size: 9.8pt; font-weight: 600; color: {NAVY}; }}
.ind .i {{ width: 34px; height: 34px; border-radius: 50%; background: #E3F0E6; display: flex; align-items: center;
           justify-content: center; flex: none; }}
.supply {{ margin-top: 12px; background: {GOLD_XL}; border-radius: 12px; padding: 11px 15px; font-size: 9.8pt; color: {NAVY}; }}
.promise {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; margin-bottom: 8mm; }}
.promise div {{ display: flex; gap: 10px; align-items: flex-start; font-size: 10.4pt; color: {INK}; font-weight: 600;
                line-height: 1.35; }}
.promise svg {{ flex: none; margin-top: 1px; }}
.cta {{ margin-top: auto; margin-bottom: 4px; background: {NAVY}; border-radius: 18px; color: #fff; padding: 26px 30px;
        display: flex; justify-content: space-between; align-items: center; gap: 24px; }}
.cta h2 {{ font-family: BSerif; font-weight: 500; font-size: 24pt; line-height: 1.1; }}
.cta p {{ font-size: 10.4pt; color: #C9D3E2; margin-top: 8px; max-width: 95mm; }}
.cta .wa {{ background: #fff; color: {INK}; border-radius: 14px; padding: 14px 18px; text-align: left; flex: none; }}
.cta .wa small {{ display: block; font-size: 8pt; letter-spacing: 1.8px; text-transform: uppercase; color: {WA}; font-weight: 700; }}
.cta .wa div {{ display: flex; align-items: center; gap: 9px; font-weight: 800; font-size: 15pt; margin-top: 4px; white-space: nowrap; }}
.foot {{ display: flex; justify-content: space-between; color: #8A92A0; font-size: 7.8pt; letter-spacing: 1px;
         margin-top: 13px; text-transform: uppercase; }}
"""


def headline(text: str) -> str:
    words = esc(text).split(" ")
    cut = max(1, len(words) - 3)
    return " ".join(words[:cut]) + " <em>" + " ".join(words[cut:]) + "</em>"


def top(d: dict) -> str:
    return (f'<div class="top"><span class="mark">{esc(d["brand"]).upper()} <i>&#10047;</i></span>'
            f'<span class="tag">{esc(d["tagline"])}</span></div>')


def whatsapp(d: dict) -> str:
    return f'<div class="contact">{wa_badge(24)}<span>{esc(d["phone"])}</span><small>WhatsApp</small></div>'


def rate_table(d: dict) -> str:
    head = "".join(f'<th><span>{icon("clock", 16, "#fff", 2)}{h} hour{"s" if h > 1 else ""} a visit</span></th>'
                   for h in d["hours"])
    rows = []
    for p in d["plans"]:
        dots = "".join(f'<i class="{"on" if i < p["days"] else ""}"></i>' for i in range(7))
        cells = "".join(f'<td class="amt">{rupees(d["rate"] * h * p["days"])} <small>/ week</small></td>' for h in d["hours"])
        rows.append(f'<tr><td class="plan">{esc(p["label"])}<span class="dots">{dots}</span></td>{cells}</tr>')
    return (f'<div class="rates"><table><tr><th><span>{icon("calendar", 16, "#fff", 2)}How often</span></th>{head}</tr>'
            f'{"".join(rows)}</table></div><p class="note">{esc(d["rate_note"])}</p>')


def page_one(d: dict) -> str:
    tiles = "".join(
        f'<div class="tile"><div class="art">{ART[s["art"]]()}</div><b>{esc(s["title"])}</b><p>{esc(s["text"])}</p>'
        f'<span class="price{"" if s["price"].startswith("₹") else " soft"}">{esc(s["price"])}</span></div>'
        for s in d["services"])
    return f"""<section class="page">{top(d)}
<h1>{headline(d["headline"])}</h1>
<p class="sub">{esc(d["subline"])}</p>
<div class="label">What we do</div>
<div class="grid">{tiles}</div>
<div class="label">{esc(d["rate_title"])}</div>
{rate_table(d)}
<div class="strip"><span class="ask">Rates, plants and visits: just ask</span>{whatsapp(d)}</div>
<div class="foot"><span>{esc(d["brand"])}</span><span>1 / 2</span></div>
</section>"""


def page_two(d: dict) -> str:
    steps = "".join(f'<div class="step"><div class="art"><span class="n">0{i}</span>{ART[s["art"]]()}</div>'
                    f'<b>{esc(s["title"])}</b><p>{esc(s["text"])}</p></div>' for i, s in enumerate(d["steps"], start=1))
    kinds = "".join(f'<div class="ind"><span class="i">{icon(x["icon"], 19, LEAF_D, 1.6)}</span>{esc(x["label"])}</div>'
                    for x in d["kinds"])
    promise = "".join(f'<div>{icon("check", 20, GOLD, 2.4)}<span>{esc(p)}</span></div>' for p in d["promises"])
    return f"""<section class="page">{top(d)}
<div class="label" style="margin-top:10mm">How it works</div>
<div class="steps">{steps}</div>
<div class="label">Plants for every space and season</div>
<div class="inds">{kinds}</div>
<p class="supply">{esc(d["supply_note"])}</p>
<div class="label">Our promise</div>
<div class="promise">{promise}</div>
<div class="cta"><div><h2>{esc(d["cta_title"])}</h2><p>{esc(d["cta_text"])}</p></div>
<div class="wa"><small>WhatsApp us</small><div>{wa_badge(26)}{esc(d["phone"])}</div></div></div>
<div class="foot"><span>{esc(d["brand"])} · {esc(d["tagline"])}</span><span>2 / 2</span></div>
</section>"""


def build_html(d: dict) -> str:
    title = f"{d['brand']} - plants and plant care"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{fonts_css()}\n{CSS}</style></head><body>{page_one(d)}{page_two(d)}</body></html>")


NODE = """
const { chromium } = require('playwright');
(async () => {
  const [html, pdf, png] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 794, height: 1123 }, deviceScaleFactor: 2 });
  await page.goto('file://' + html);
  await page.evaluate(() => document.fonts.ready);
  const over = await page.evaluate(() => [...document.querySelectorAll('section.page')]
    .map((s, i) => [i + 1, s.scrollHeight > s.clientHeight + 1]).filter(x => x[1]).map(x => x[0]));
  if (over.length) { console.error('content runs off page ' + over.join(', ')); process.exit(3); }
  await page.pdf({ path: pdf, format: 'A4', printBackground: true });
  if (png) {
    const pages = await page.$$('section.page');
    for (let i = 0; i < pages.length; i++) await pages[i].screenshot({ path: png.replace('%d', String(i + 1)) });
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phone", required=True, help='the WhatsApp number printed on the brochure, e.g. "+91 98xxx xxxxx"')
    ap.add_argument("--out", default=str(HERE / "own" / "Plant-Parlour-brochure.pdf"))
    ap.add_argument("--png", action="store_true", help="also page images next to the PDF")
    a = ap.parse_args(argv)
    d = json.loads((HERE / "brochure.json").read_text(encoding="utf-8"))
    d["phone"] = a.phone.strip()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "brochure.html"
        src.write_text(build_html(d), encoding="utf-8")
        js = Path(tmp) / "print.js"
        js.write_text(NODE, encoding="utf-8")
        png = str(out.with_name(out.stem + "-page%d.png")) if a.png else ""
        env = {**os.environ, "NODE_PATH": os.environ.get("NODE_PATH", "") or subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()}
        r = subprocess.run(["node", str(js), str(src), str(out), png], env=env)
        if r.returncode != 0:
            print("The text is too long for the page: shorten it." if r.returncode == 3 else
                  "Could not print the PDF (needs node with Playwright and Chromium).")
            return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
