#!/usr/bin/env python3
"""
The Daily Duck - X social card builder

Creates a fixed 5:4 X image (1500x1200) from the already-approved
canonical website/hero image and the approved story copy.

Output:
  automation_images/x/YYYY-MM-DD-x-card.png

Also writes canonical_x_image_path into:
  automation_state/ready_to_publish.json
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageFilter

READY_PATH = Path("automation_state/ready_to_publish.json")
OUT_DIR = Path("automation_images/x")

# Fixed Daily Duck X ratio: 5:4 (same family as the approved reference image)
W, H = 1500, 1200

NAVY = "#0F172A"
YELLOW = "#FEC400"
CREAM = "#FFF9ED"
WHITE = "#FFFFFF"
MUTED = "#334155"
BORDER = "#CBD5E1"


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_ready() -> dict[str, Any]:
    if not READY_PATH.exists():
        raise FileNotFoundError(f"Missing {READY_PATH}")
    data = json.loads(READY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ready_to_publish.json must be a JSON object")
    return data


def save_ready(data: dict[str, Any]) -> None:
    READY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_font(bold: bool = False) -> str:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    raise FileNotFoundError("No usable Noto/DejaVu font found on runner")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(find_font(bold), size=size)


def fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - tw) // 2)
    top = max(0, (nh - th) // 2)
    return img.crop((left, top, left + tw, top + th))


def wrap_by_pixels(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    # Works for both Japanese and Latin text by greedily measuring characters.
    lines: list[str] = []
    current = ""
    for ch in text:
        test = current + ch
        box = draw.textbbox((0, 0), test, font=fnt)
        if box[2] - box[0] <= max_width or not current:
            current = test
        else:
            lines.append(current.rstrip())
            current = ch.lstrip()
    if current:
        lines.append(current.rstrip())
    return lines


def draw_multiline_limited(draw, xy, text, fnt, fill, max_width, max_lines, spacing):
    lines = wrap_by_pixels(draw, text, fnt, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textbbox((0,0), last + "…", font=fnt)[2] > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += int(fnt.size * spacing)
    return y


def render_card(ready: dict[str, Any]) -> Path:
    approved = ready.get("gate_a_approved_story")
    if not isinstance(approved, dict):
        raise ValueError("gate_a_approved_story is missing")

    issue_date = first_text(ready.get("issue_date"), approved.get("issue_date"), approved.get("date"))
    if not issue_date:
        raise ValueError("issue_date is missing")

    hero_path = Path(first_text(ready.get("published_image_path"), ready.get("canonical_image_path")))
    if not hero_path.exists():
        raise FileNotFoundError(f"Canonical/website image missing: {hero_path}")

    duck_name = first_text(approved.get("duck_name"), "DAILY DUCK").upper()
    teaser = first_text(approved.get("x_jp"), approved.get("jp_copy"), approved.get("story_ja"))
    source = first_text(approved.get("source"), approved.get("source_name"), "Official source")

    # Base card
    card = Image.new("RGB", (W, H), CREAM)
    draw = ImageDraw.Draw(card)

    # Rounded outer frame
    draw.rounded_rectangle((8, 8, W-8, H-8), radius=38, outline=BORDER, width=3, fill=CREAM)

    # Right-side hero visual. Wider than half so the card feels image-rich, like the approved reference.
    hero_x = 600
    hero_y = 140
    hero_w = W - hero_x - 16
    hero_h = H - hero_y - 92
    hero = Image.open(hero_path).convert("RGB")
    hero = fit_cover(hero, (hero_w, hero_h))

    # Soft cream fade on the left edge of the hero for readable copy.
    overlay = Image.new("RGBA", hero.size, (255,255,255,0))
    od = ImageDraw.Draw(overlay)
    fade_w = 180
    for x in range(fade_w):
        a = int(255 * (1 - x / fade_w))
        od.line((x, 0, x, hero_h), fill=(255,249,237,a), width=1)
    hero = Image.alpha_composite(hero.convert("RGBA"), overlay).convert("RGB")
    card.paste(hero, (hero_x, hero_y))

    draw = ImageDraw.Draw(card)

    # Header — approved QUACKSTRONAUT-style system
    # Cream background / duck mark / brand / thin navy rule / tagline
    logo_path = Path("assets/brand/the-daily-duck-emblem-128.png")
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA").resize((72, 72), Image.Resampling.LANCZOS)
        card.paste(logo, (48, 38), logo)
        brand_x = 138
    else:
        # Simple duck-like brand marker fallback
        draw.ellipse((55, 48, 91, 84), fill=NAVY)
        draw.ellipse((68, 34, 88, 56), fill=NAVY)
        draw.polygon([(86, 43), (105, 49), (86, 54)], fill=NAVY)
        draw.rectangle((69, 82, 78, 98), fill=NAVY)
        draw.rectangle((84, 82, 93, 98), fill=NAVY)
        brand_x = 125

    header_brand_font = font(43, True)
    draw.text((brand_x, 52), "THE DAILY DUCK", font=header_brand_font, fill=NAVY)

    tagline = "ONE DAY. ONE STORY. ONE DUCK."
    tagline_font = font(21, True)
    tb = draw.textbbox((0, 0), tagline, font=tagline_font)
    tagline_w = tb[2] - tb[0]
    tagline_x = W - 54 - tagline_w
    draw.text((tagline_x, 61), tagline, font=tagline_font, fill=NAVY)

    # Thin line between brand and tagline
    brand_box = draw.textbbox((brand_x, 52), "THE DAILY DUCK", font=header_brand_font)
    line_x1 = brand_box[2] + 28
    line_x2 = tagline_x - 28
    if line_x2 > line_x1:
        draw.line((line_x1, 73, line_x2, 73), fill=NAVY, width=3)

    # Left copy panel
    left = 58
    panel_w = 505

    pill_text = "DUCK OF THE DAY"
    pill_font = font(28, True)
    pb = draw.textbbox((0,0), pill_text, font=pill_font)
    pill_w = pb[2]-pb[0] + 36
    draw.rounded_rectangle((left, 190, left+pill_w, 244), radius=18, fill=YELLOW)
    draw.text((left+18, 201), pill_text, font=pill_font, fill=NAVY)

    # Duck name: automatically shrink to fit and allow 2 lines if needed.
    name_font_size = 110
    while name_font_size >= 64:
        nf = font(name_font_size, True)
        name_lines = wrap_by_pixels(draw, duck_name, nf, panel_w)
        if len(name_lines) <= 2:
            break
        name_font_size -= 4
    y = 276
    for line in name_lines[:2]:
        draw.text((left, y), line, font=nf, fill=NAVY)
        y += int(name_font_size * 0.92)

    draw.rounded_rectangle((left, y+18, left+160, y+30), radius=6, fill=YELLOW)
    y += 64

    # Date
    try:
        dt = datetime.strptime(issue_date, "%Y-%m-%d")
        date_text = f"{dt.year}.{dt.month}.{dt.day}"
    except Exception:
        date_text = issue_date.replace("-", ".")
    draw.text((left, y), date_text, font=font(30, True), fill=NAVY)
    y += 58

    # Story teaser
    if teaser:
        y = draw_multiline_limited(
            draw, (left, y), teaser, font(31, True), NAVY,
            max_width=panel_w, max_lines=4, spacing=1.42
        )

    # Source near bottom-left
    source_text = f"Source: {source}"
    draw.text((left, 1015), source_text, font=font(20, False), fill=MUTED)

    # Footer — approved QUACKSTRONAUT-style system
    footer_top = H - 168
    # Keep footer cream and visually separate it with whitespace/a subtle line.
    draw.line((42, footer_top, W - 42, footer_top), fill=BORDER, width=2)

    # Duck mark at left (use emblem when available, otherwise silhouette fallback)
    if logo_path.exists():
        footer_logo = Image.open(logo_path).convert("RGBA").resize((72, 72), Image.Resampling.LANCZOS)
        card.paste(footer_logo, (58, footer_top + 42), footer_logo)
    else:
        fy = footer_top + 48
        draw.ellipse((63, fy + 12, 95, fy + 44), fill=NAVY)
        draw.ellipse((75, fy, 93, fy + 20), fill=NAVY)
        draw.polygon([(91, fy + 7), (107, fy + 12), (91, fy + 17)], fill=NAVY)
        draw.rectangle((74, fy + 42, 81, fy + 55), fill=NAVY)
        draw.rectangle((87, fy + 42, 94, fy + 55), fill=NAVY)

    footer_copy = "ONE STORY FROM THE WORLD.\nONE DUCK. EVERY DAY."
    draw.multiline_text(
        (154, footer_top + 45),
        footer_copy,
        font=font(24, True),
        fill=NAVY,
        spacing=8,
    )

    # Yellow hashtag pill at right
    hash_text = "#TheDailyDuck"
    hash_font = font(29, True)
    hb = draw.textbbox((0, 0), hash_text, font=hash_font)
    hash_w = hb[2] - hb[0]
    pill_w = hash_w + 70
    pill_h = 66
    pill_x2 = W - 58
    pill_x1 = pill_x2 - pill_w
    pill_y1 = footer_top + 48
    draw.rounded_rectangle(
        (pill_x1, pill_y1, pill_x2, pill_y1 + pill_h),
        radius=28,
        fill=YELLOW,
    )
    draw.text(
        (pill_x1 + 35, pill_y1 + 14),
        hash_text,
        font=hash_font,
        fill=NAVY,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{issue_date}-x-card.png"
    card.save(out_path, "PNG", optimize=True)
    return out_path


def main() -> int:
    ready = load_ready()
    out_path = render_card(ready)
    ready["canonical_x_image_path"] = out_path.as_posix()
    ready["x_image_ratio"] = "5:4"
    ready["x_image_size"] = f"{W}x{H}"
    save_ready(ready)
    print(f"X CARD CREATED: {out_path}")
    print(f"SIZE: {W}x{H} (5:4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
