#!/usr/bin/env python3
"""
The Daily Duck - X social card builder

Creates a fixed 5:4 X image (1500x1200) from the already-approved
canonical website/hero image and the approved story copy.

Final X card layout:

    DUCK OF THE DAY

    TITLE

    [space]
    YELLOW LINE
    [space]
    DATE

    JAPANESE TEASER

    Source: ...

Hero image:
    - no gradient
    - no fade
    - clear to the bottom

Yellow line:
    - positioned below the title
    - width = 92% of the rendered date width

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


READY_PATH = Path(
    "automation_state/ready_to_publish.json"
)

OUT_DIR = Path(
    "automation_images/x"
)

# ============================================================
# Canvas
# ============================================================

W = 1500
H = 1200

# ============================================================
# Brand colors
# ============================================================

NAVY = "#0F172A"
YELLOW = "#FEC400"
CREAM = "#FFF9ED"
WHITE = "#FFFFFF"
MUTED = "#334155"
BORDER = "#CBD5E1"

# ============================================================
# Layout
# ============================================================

LEFT = 58
PANEL_W = 505

HERO_X = 600
HERO_Y = 140

# Title -> yellow line
TITLE_TO_LINE_GAP = 28

# Yellow line thickness
YELLOW_LINE_HEIGHT = 10

# Yellow line -> date
LINE_TO_DATE_GAP = 22

# Date -> Japanese teaser
DATE_TO_TEASER_GAP = 34

# Yellow line length relative to rendered date width
YELLOW_LINE_WIDTH_RATIO = 0.92


# ============================================================
# Helpers
# ============================================================

def first_text(
    *values: Any,
) -> str:

    for value in values:
        if (
            isinstance(value, str)
            and value.strip()
        ):
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

    if not isinstance(
        data,
        dict,
    ):
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


# ============================================================
# Fonts
# ============================================================

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


# ============================================================
# Image helper
# ============================================================

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

    nw = int(
        round(sw * scale)
    )

    nh = int(
        round(sh * scale)
    )

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


# ============================================================
# Text helpers
# ============================================================

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

        width = (
            box[2]
            - box[0]
        )

        if (
            width <= max_width
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

        while last:

            bbox = draw.textbbox(
                (0, 0),
                last + "…",
                font=fnt,
            )

            width = (
                bbox[2]
                - bbox[0]
            )

            if width <= max_width:
                break

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


def format_issue_date(
    issue_date: str,
) -> str:

    try:
        dt = datetime.strptime(
            issue_date,
            "%Y-%m-%d",
        )

        return dt.strftime(
            "%B %d, %Y"
        ).upper()

    except Exception:
        return issue_date.replace(
            "-",
            ".",
        )


# ============================================================
# Main renderer
# ============================================================

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

    # --------------------------------------------------------
    # Issue date
    # --------------------------------------------------------

    issue_date = first_text(
        ready.get(
            "issue_date"
        ),
        approved.get(
            "issue_date"
        ),
        approved.get(
            "date"
        ),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing"
        )

    date_text = format_issue_date(
        issue_date
    )

    # --------------------------------------------------------
    # Hero image
    # --------------------------------------------------------

    hero_path_text = first_text(
        ready.get(
            "published_image_path"
        ),
        ready.get(
            "canonical_image_path"
        ),
    )

    if not hero_path_text:
        raise ValueError(
            "Canonical/website image path is missing"
        )

    hero_path = Path(
        hero_path_text
    )

    if not hero_path.exists():
        raise FileNotFoundError(
            "Canonical/website image missing: "
            f"{hero_path}"
        )

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Japanese teaser
    # --------------------------------------------------------

    teaser = first_text(
        approved.get(
            "x_jp"
        ),
        approved.get(
            "jp_copy"
        ),
        approved.get(
            "story_ja"
        ),
    )

    # --------------------------------------------------------
    # Source
    # --------------------------------------------------------

    source = first_text(
        approved.get(
            "source"
        ),
        approved.get(
            "source_name"
        ),
        approved.get(
            "publisher"
        ),
        "Official source",
    )

    # ========================================================
    # Canvas
    # ========================================================

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

    # ========================================================
    # Header
    # ========================================================

    logo_path = Path(
        "assets/brand/"
        "the-daily-duck-emblem-128.png"
    )

    if logo_path.exists():

        logo = (
            Image.open(
                logo_path
            )
            .convert(
                "RGBA"
            )
            .resize(
                (
                    92,
                    92,
                ),
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

    tagline_width = (
        tb[2]
        - tb[0]
    )

    draw.text(
        (
            W
            - 55
            - tagline_width,
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

    # ========================================================
    # Footer position
    # ========================================================

    footer_y = (
        H - 72
    )

    # ========================================================
    # Hero image
    # ========================================================

    hero_w = (
        W
        - HERO_X
        - 16
    )

    hero_bottom = (
        footer_y - 8
    )

    hero_h = (
        hero_bottom
        - HERO_Y
    )

    hero = (
        Image.open(
            hero_path
        )
        .convert(
            "RGB"
        )
    )

    hero = fit_cover(
        hero,
        (
            hero_w,
            hero_h,
        ),
    )

    # No gradient, fade or overlay.
    card.paste(
        hero,
        (
            HERO_X,
            HERO_Y,
        ),
    )

    draw = ImageDraw.Draw(
        card
    )

    # ========================================================
    # DUCK OF THE DAY
    # ========================================================

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
            LEFT,
            190,
            LEFT + pill_w,
            244,
        ),
        radius=18,
        fill=YELLOW,
    )

    draw.text(
        (
            LEFT + 18,
            201,
        ),
        pill_text,
        font=pill_font,
        fill=NAVY,
    )

    # ========================================================
    # Headline
    # ========================================================

    name_font_size = 110
    name_lines: list[str] = []

    nf = font(
        name_font_size,
        True,
    )

    while (
        name_font_size >= 64
    ):

        nf = font(
            name_font_size,
            True,
        )

        name_lines = wrap_by_pixels(
            draw,
            duck_name,
            nf,
            PANEL_W,
        )

        if (
            len(name_lines) <= 2
        ):
            break

        name_font_size -= 4

    title_y = 276
    title_bottom = title_y
    line_y = title_y

    for line in name_lines[:2]:

        draw.text(
            (
                LEFT,
                line_y,
            ),
            line,
            font=nf,
            fill=NAVY,
        )

        bbox = draw.textbbox(
            (
                LEFT,
                line_y,
            ),
            line,
            font=nf,
        )

        title_bottom = max(
            title_bottom,
            bbox[3],
        )

        line_y += int(
            name_font_size
            * 0.92
        )

    # ========================================================
    # Yellow line position
    # ========================================================

    yellow_y = (
        title_bottom
        + TITLE_TO_LINE_GAP
    )

    # ========================================================
    # Date font / rendered date width
    # ========================================================

    date_font = font(
        30,
        True,
    )

    date_bbox = draw.textbbox(
        (
            0,
            0,
        ),
        date_text,
        font=date_font,
    )

    date_text_width = (
        date_bbox[2]
        - date_bbox[0]
    )

    # Final visual rule:
    # yellow line = 92% of rendered date width
    yellow_line_width = int(
        date_text_width
        * YELLOW_LINE_WIDTH_RATIO
    )

    # ========================================================
    # Yellow line
    # ========================================================

    draw.rounded_rectangle(
        (
            LEFT,
            yellow_y,
            LEFT + yellow_line_width,
            yellow_y
            + YELLOW_LINE_HEIGHT,
        ),
        radius=(
            YELLOW_LINE_HEIGHT
            // 2
        ),
        fill=YELLOW,
    )

    # ========================================================
    # Date
    # ========================================================

    date_y = (
        yellow_y
        + YELLOW_LINE_HEIGHT
        + LINE_TO_DATE_GAP
    )

    draw.text(
        (
            LEFT,
            date_y,
        ),
        date_text,
        font=date_font,
        fill=NAVY,
    )

    actual_date_bbox = (
        draw.textbbox(
            (
                LEFT,
                date_y,
            ),
            date_text,
            font=date_font,
        )
    )

    date_bottom = (
        actual_date_bbox[3]
    )

    # ========================================================
    # Japanese teaser
    # ========================================================

    teaser_y = (
        date_bottom
        + DATE_TO_TEASER_GAP
    )

    if teaser:

        draw_multiline_limited(
            draw,
            (
                LEFT,
                teaser_y,
            ),
            teaser,
            font(
                31,
                True,
            ),
            NAVY,
            max_width=PANEL_W,
            max_lines=4,
            spacing=1.42,
        )

    # ========================================================
    # Source
    # ========================================================

    source_text = (
        f"Source: {source}"
    )

    source_y = (
        footer_y - 73
    )

    draw.text(
        (
            LEFT,
            source_y,
        ),
        source_text,
        font=font(
            20,
            False,
        ),
        fill=MUTED,
    )

    # ========================================================
    # Footer
    # ========================================================

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

    # ========================================================
    # Save
    # ========================================================

    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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


# ============================================================
# Main
# ============================================================

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

    ready[
        "x_card_layout_version"
    ] = "2026-08-16-v4"

    ready[
        "x_yellow_line_width_ratio"
    ] = YELLOW_LINE_WIDTH_RATIO

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

    print(
        "LAYOUT:"
        " title -> space -> yellow line"
        " -> space -> date -> teaser"
    )

    print(
        "YELLOW LINE:"
        f" {YELLOW_LINE_WIDTH_RATIO:.0%}"
        " of rendered date width"
    )

    print(
        "HERO IMAGE:"
        " clear / no gradient"
    )

    print(
        "SOURCE:"
        " enabled"
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
