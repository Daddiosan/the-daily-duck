#!/usr/bin/env python3
"""
The Daily Duck - X social card builder

Creates a fixed 5:4 X image (1500x1200) from the already-approved
canonical website/hero image and the approved story copy.

Output:
  automation_images/x/YYYY-MM-DD-x-card.jpg

Also writes canonical_x_image_path into:
  automation_state/ready_to_publish.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


READY_PATH = Path("automation_state/ready_to_publish.json")
OUT_DIR = Path("automation_images/x")

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
        raise FileNotFoundError(
            f"Missing {READY_PATH}"
        )

    data = json.loads(
        READY_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, dict):
        raise ValueError(
            "ready_to_publish.json must be a JSON object"
        )

    return data


def save_ready(
    data: dict[str, Any],
) -> None:

    READY_PATH.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def find_font(
    bold: bool = False,
) -> str:

    candidates = [
        (
            "/usr/share/fonts/opentype/noto/"
            "NotoSansCJK-Bold.ttc"
            if bold
            else
            "/usr/share/fonts/opentype/noto/"
            "NotoSansCJK-Regular.ttc"
        ),
        (
            "/usr/share/fonts/truetype/noto/"
            "NotoSansCJK-Bold.ttc"
            if bold
            else
            "/usr/share/fonts/truetype/noto/"
            "NotoSansCJK-Regular.ttc"
        ),
        (
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf"
            if bold
            else
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans.ttf"
        ),
    ]

    for path in candidates:
        if Path(path).exists():
            return path

    raise FileNotFoundError(
        "No usable Noto/DejaVu font found on runner"
    )


def font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont:

    return ImageFont.truetype(
        find_font(bold),
        size=size,
    )


def fit_cover(
    img: Image.Image,
    size: tuple[int, int],
) -> Image.Image:

    tw, th = size
    sw, sh = img.size

    scale = max(
        tw / sw,
        th / sh,
    )

    nw = int(sw * scale)
    nh = int(sh * scale)

    img = img.resize(
        (nw, nh),
        Image.Resampling.LANCZOS,
    )

    left = max(
        0,
        (nw - tw) // 2,
    )

    top = max(
        0,
        (nh - th) // 2,
    )

    return img.crop(
        (
            left,
            top,
            left + tw,
            top + th,
        )
    )


def wrap_by_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:

    lines: list[str] = []
    current = ""

    for ch in text:

        test = current + ch

        box = draw.textbbox(
            (0, 0),
            test,
            font=fnt,
        )

        if (
            box[2] - box[0] <= max_width
            or not current
        ):
            current = test

        else:
            lines.append(
                current.rstrip()
            )
            current = ch.lstrip()

    if current:
        lines.append(
            current.rstrip()
        )

    return lines


def draw_multiline_limited(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fnt: ImageFont.FreeTypeFont,
    fill: str,
    max_width: int,
    max_lines: int,
    spacing: float,
) -> int:

    lines = wrap_by_pixels(
        draw,
        text,
        fnt,
        max_width,
    )

    if len(lines) > max_lines:

        lines = lines[:max_lines]

        last = lines[-1]

        while (
            last
            and draw.textbbox(
                (0, 0),
                last + "…",
                font=fnt,
            )[2]
            > max_width
        ):
            last = last[:-1]

        lines[-1] = (
            last.rstrip()
            + "…"
        )

    x, y = xy

    for line in lines:

        draw.text(
            (x, y),
            line,
            font=fnt,
            fill=fill,
        )

        y += int(
            fnt.size * spacing
        )

    return y


def render_card(
    ready: dict[str, Any],
) -> Path:

    approved = ready.get(
        "gate_a_approved_story"
    )

    if not isinstance(
        approved,
        dict,
    ):
        raise ValueError(
            "gate_a_approved_story is missing"
        )

    issue_date = first_text(
        ready.get("issue_date"),
        approved.get("issue_date"),
        approved.get("date"),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing"
        )

    hero_path = Path(
        first_text(
            ready.get(
                "published_image_path"
            ),
            ready.get(
                "canonical_image_path"
            ),
        )
    )

    if not hero_path.exists():
        raise FileNotFoundError(
            "Canonical/website image missing: "
            f"{hero_path}"
        )

    duck_name = first_text(
        ready.get(
            "selected_title"
        ),
        approved.get(
            "selected_title"
        ),
        approved.get(
            "duck_name"
        ),
        "DAILY DUCK",
    ).upper()

    teaser = first_text(
        approved.get("x_jp"),
        approved.get("jp_copy"),
        approved.get("story_ja"),
    )

    source = first_text(
        approved.get("source"),
        approved.get("source_name"),
        "Official source",
    )

    card = Image.new(
        "RGB",
        (W, H),
        CREAM,
    )

    draw = ImageDraw.Draw(
        card
    )

    draw.rounded_rectangle(
        (
            8,
            8,
            W - 8,
            H - 8,
        ),
        radius=38,
        outline=BORDER,
        width=3,
        fill=CREAM,
    )

    hero_x = 600
    hero_y = 140
    hero_w = W - hero_x - 16
    hero_h = H - hero_y - 92

    hero = (
        Image.open(hero_path)
        .convert("RGB")
    )

    hero = fit_cover(
        hero,
        (
            hero_w,
            hero_h,
        ),
    )

    overlay = Image.new(
        "RGBA",
        hero.size,
        (
            255,
            255,
            255,
            0,
        ),
    )

    od = ImageDraw.Draw(
        overlay
    )

    fade_w = 180

    for x in range(
        fade_w
    ):

        alpha = int(
            255
            * (
                1
                - x / fade_w
            )
        )

        od.line(
            (
                x,
                0,
                x,
                hero_h,
            ),
            fill=(
                255,
                249,
                237,
                alpha,
            ),
            width=1,
        )

    hero = Image.alpha_composite(
        hero.convert("RGBA"),
        overlay,
    ).convert("RGB")

    card.paste(
        hero,
        (
            hero_x,
            hero_y,
        ),
    )

    draw = ImageDraw.Draw(
        card
    )

    logo_path = Path(
        "assets/brand/"
        "the-daily-duck-emblem-128.png"
    )

    if logo_path.exists():

        logo = (
            Image.open(logo_path)
            .convert("RGBA")
            .resize(
                (92, 92),
                Image.Resampling.LANCZOS,
            )
        )

        card.paste(
            logo,
            (
                46,
                30,
            ),
            logo,
        )

        brand_x = 160

    else:

        brand_x = 56

    draw.text(
        (
            brand_x,
            51,
        ),
        "THE DAILY DUCK",
        font=font(
            54,
            True,
        ),
        fill=NAVY,
    )

    tagline = (
        "ONE DAY. ONE STORY. ONE DUCK."
    )

    tagline_font = font(
        24,
        True,
    )

    tb = draw.textbbox(
        (
            0,
            0,
        ),
        tagline,
        font=tagline_font,
    )

    draw.text(
        (
            W
            - 55
            - (
                tb[2]
                - tb[0]
            ),
            64,
        ),
        tagline,
        font=tagline_font,
        fill=NAVY,
    )

    draw.line(
        (
            45,
            138,
            W - 45,
            138,
        ),
        fill=NAVY,
        width=3,
    )

    left = 58
    panel_w = 505

    pill_text = (
        "DUCK OF THE DAY"
    )

    pill_font = font(
        28,
        True,
    )

    pb = draw.textbbox(
        (
            0,
            0,
        ),
        pill_text,
        font=pill_font,
    )

    pill_w = (
        pb[2]
        - pb[0]
        + 36
    )

    draw.rounded_rectangle(
        (
            left,
            190,
            left + pill_w,
            244,
        ),
        radius=18,
        fill=YELLOW,
    )

    draw.text(
        (
            left + 18,
            201,
        ),
        pill_text,
        font=pill_font,
        fill=NAVY,
    )

    name_font_size = 110
    name_lines: list[str] = []
    nf = font(
        name_font_size,
        True,
    )

    while (
        name_font_size
        >= 64
    ):

        nf = font(
            name_font_size,
            True,
        )

        name_lines = (
            wrap_by_pixels(
                draw,
                duck_name,
                nf,
                panel_w,
            )
        )

        if len(
            name_lines
        ) <= 2:
            break

        name_font_size -= 4

    y = 276

    for line in name_lines[:2]:

        draw.text(
            (
                left,
                y,
            ),
            line,
            font=nf,
            fill=NAVY,
        )

        y += int(
            name_font_size
            * 0.92
        )

    try:

        dt_for_line = (
            datetime.strptime(
                issue_date,
                "%Y-%m-%d",
            )
        )

        date_for_line = (
            dt_for_line.strftime(
                "%B %d, %Y"
            ).upper()
        )

    except Exception:

        date_for_line = (
            issue_date.replace(
                "-",
                ".",
            )
        )

    date_font_for_line = (
        font(
            30,
            True,
        )
    )

    db = draw.textbbox(
        (
            0,
            0,
        ),
        date_for_line,
        font=date_font_for_line,
    )

    line_w = max(
        170,
        min(
            panel_w,
            db[2]
            - db[0],
        ),
    )

    draw.rounded_rectangle(
        (
            left,
            y + 18,
            left + line_w,
            y + 30,
        ),
        radius=6,
        fill=YELLOW,
    )

    y += 64

    try:

        dt = datetime.strptime(
            issue_date,
            "%Y-%m-%d",
        )

        date_text = (
            dt.strftime(
                "%B %d, %Y"
            ).upper()
        )

    except Exception:

        date_text = (
            issue_date.replace(
                "-",
                ".",
            )
        )

    draw.text(
        (
            left,
            y,
        ),
        date_text,
        font=font(
            30,
            True,
        ),
        fill=NAVY,
    )

    y += 58

    if teaser:

        y = draw_multiline_limited(
            draw,
            (
                left,
                y,
            ),
            teaser,
            font(
                31,
                True,
            ),
            NAVY,
            max_width=panel_w,
            max_lines=4,
            spacing=1.42,
        )

    source_text = (
        f"Source: {source}"
    )

    draw.text(
        (
            left,
            1015,
        ),
        source_text,
        font=font(
            20,
            False,
        ),
        fill=MUTED,
    )

    footer_y = (
        H - 72
    )

    draw.rounded_rectangle(
        (
            42,
            footer_y,
            W - 42,
            H - 22,
        ),
        radius=18,
        fill=NAVY,
    )

    draw.text(
        (
            76,
            footer_y + 14,
        ),
        "🌐  thedailyduck.ai",
        font=font(
            26,
            True,
        ),
        fill=WHITE,
    )

    draw.text(
        (
            410,
            footer_y + 14,
        ),
        "|   #TheDailyDuck",
        font=font(
            26,
            True,
        ),
        fill=WHITE,
    )

    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # X用ブランドカードはJPEGで保存する。
    # JPEG投稿経路がX APIで実機成功しているため、
    # PNGではなくJPEGをcanonical X imageとする。
    out_path = (
        OUT_DIR
        / f"{issue_date}-x-card.jpg"
    )

    card.save(
        out_path,
        "JPEG",
        quality=95,
        optimize=True,
        progressive=False,
        subsampling=0,
    )

    return out_path


def main() -> int:

    ready = load_ready()

    out_path = render_card(
        ready
    )

    ready[
        "canonical_x_image_path"
    ] = out_path.as_posix()

    ready[
        "x_image_ratio"
    ] = "5:4"

    ready[
        "x_image_size"
    ] = f"{W}x{H}"

    ready[
        "x_image_format"
    ] = "JPEG"

    save_ready(
        ready
    )

    print(
        f"X CARD CREATED: {out_path}"
    )

    print(
        f"SIZE: {W}x{H} (5:4)"
    )

    print(
        "FORMAT: JPEG"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
