#!/usr/bin/env python3
"""
The Daily Duck - X social card builder

Creates the branded Daily Duck 5:4 X card (1200x960).

Policy:
- Issues before 2026-08-22 keep the legacy card behavior.
- Issues on/after 2026-08-22 use the English-first large-photo layout:
  * selected publication title
  * English story teaser
  * hero photo at roughly 80% of the inner-frame height
  * navy footer bar
  * source credit
  * fixed 5:4 output for X

Output:
  automation_images/x/YYYY-MM-DD-x-card.png

Also writes canonical_x_image_path into:
  automation_state/ready_to_publish.json
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


READY_PATH = Path("automation_state/ready_to_publish.json")
OUT_DIR = Path("automation_images/x")

W, H = 1200, 960

NEW_LAYOUT_FROM = date(2026, 8, 22)

NAVY = "#0F172A"
YELLOW = "#FEC400"
CREAM = "#FFF9ED"
WHITE = "#FFFFFF"
MUTED = "#334155"
BORDER = "#D8D2C7"

BASE_W = 1500
BASE_H = 1200

def sx(value: int | float) -> int:
    return int(round(value * W / BASE_W))

def sy(value: int | float) -> int:
    return int(round(value * H / BASE_H))

def ss(value: int | float) -> int:
    return int(round(value * min(W / BASE_W, H / BASE_H)))



def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_ready() -> dict[str, Any]:
    if not READY_PATH.exists():
        raise FileNotFoundError(f"Missing {READY_PATH}")

    data = json.loads(
        READY_PATH.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            "ready_to_publish.json must be a JSON object"
        )

    return data


def save_ready(data: dict[str, Any]) -> None:
    READY_PATH.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def find_font(bold: bool = False) -> str:
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
        "No usable Noto/Deja font found on runner"
    )


def font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        find_font(bold),
        size=max(8, ss(size)),
    )


def fit_cover(
    img: Image.Image,
    size: tuple[int, int],
) -> Image.Image:
    """
    Scale image to completely cover target rectangle,
    cropping evenly from the excess dimension.
    """
    tw, th = size
    sw, sh = img.size

    scale = max(
        tw / sw,
        th / sh,
    )

    nw = max(1, int(round(sw * scale)))
    nh = max(1, int(round(sh * scale)))

    resized = img.resize(
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

    return resized.crop(
        (
            left,
            top,
            left + tw,
            top + th,
        )
    )


def wrap_words_by_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    English-first word-aware wrapping.
    Falls back to character wrapping for a very long token.
    """
    text = " ".join(
        text.replace("\n", " ").split()
    )

    if not text:
        return []

    words = text.split(" ")
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = (
            word
            if not current
            else f"{current} {word}"
        )

        box = draw.textbbox(
            (0, 0),
            candidate,
            font=fnt,
        )

        if (
            box[2] - box[0] <= max_width
            or not current
        ):
            current = candidate
            continue

        lines.append(current)
        current = word

        word_box = draw.textbbox(
            (0, 0),
            current,
            font=fnt,
        )

        if word_box[2] - word_box[0] > max_width:
            chunk = ""

            for ch in current:
                test = chunk + ch
                test_box = draw.textbbox(
                    (0, 0),
                    test,
                    font=fnt,
                )

                if (
                    test_box[2] - test_box[0]
                    <= max_width
                    or not chunk
                ):
                    chunk = test
                else:
                    lines.append(chunk)
                    chunk = ch

            current = chunk

    if current:
        lines.append(current)

    return lines


def truncate_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    fnt: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    if len(lines) <= max_lines:
        return lines

    lines = lines[:max_lines]
    last = lines[-1].rstrip()

    while last:
        test = last + "…"
        box = draw.textbbox(
            (0, 0),
            test,
            font=fnt,
        )

        if box[2] - box[0] <= max_width:
            lines[-1] = test
            return lines

        last = last[:-1].rstrip()

    lines[-1] = "…"
    return lines


def draw_lines(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    lines: list[str],
    fnt: ImageFont.FreeTypeFont,
    fill: str,
    line_height: int,
) -> int:
    for line in lines:
        draw.text(
            (x, y),
            line,
            font=fnt,
            fill=fill,
        )
        y += line_height

    return y


def parse_issue_date(
    issue_date: str,
) -> date:
    try:
        return datetime.strptime(
            issue_date,
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError(
            "issue_date must use YYYY-MM-DD; "
            f"got {issue_date!r}"
        ) from exc


def display_date_english(
    issue_date: str,
) -> str:
    dt = datetime.strptime(
        issue_date,
        "%Y-%m-%d",
    )

    return (
        dt.strftime("%B %d, %Y")
        .replace(" 0", " ")
        .upper()
    )


def resolve_approved_story(
    ready: dict[str, Any],
) -> dict[str, Any]:
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

    # Current Phase 2 ready state may contain the selected story
    # nested inside the complete Gate A approved state.
    for key in (
        "selected_story",
        "approved_story",
        "gate_a_approved_story",
        "story",
        "recommended_story",
    ):
        value = approved.get(key)

        if isinstance(value, dict):
            return value

    return approved


def resolve_hero_path(
    ready: dict[str, Any],
) -> Path:
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

    return hero_path


def draw_logo_and_header(
    card: Image.Image,
    draw: ImageDraw.ImageDraw,
) -> None:
    """
    Header matching the approved 8/22-forward visual system:
    emblem + THE DAILY DUCK + right-aligned tagline + full navy rule.
    """
    logo_path = Path(
        "assets/brand/the-daily-duck-emblem-128.png"
    )

    if logo_path.exists():
        logo = (
            Image.open(logo_path)
            .convert("RGBA")
            .resize(
                (ss(76), ss(76)),
                Image.Resampling.LANCZOS,
            )
        )

        card.paste(
            logo,
            (sx(52), sy(32)),
            logo,
        )

        brand_x = sx(144)

    else:
        # Brand-safe simple marker fallback.
        draw.ellipse(
            (sx(56), sy(43), sx(104), sy(91)),
            fill=YELLOW,
            outline=NAVY,
            width=4,
        )
        draw.ellipse(
            (sx(69), sy(54), sx(91), sy(76)),
            fill=NAVY,
        )
        brand_x = sx(122)

    brand_font = font(
        48,
        True,
    )

    draw.text(
        (brand_x, sy(47)),
        "THE DAILY DUCK",
        font=brand_font,
        fill=NAVY,
    )

    tagline = (
        "ONE DAY. ONE STORY. ONE DUCK."
    )

    tagline_font = font(
        22,
        True,
    )

    box = draw.textbbox(
        (0, 0),
        tagline,
        font=tagline_font,
    )

    tagline_w = box[2] - box[0]

    draw.text(
        (
            W - sx(58) - tagline_w,
            58,
        ),
        tagline,
        font=tagline_font,
        fill=NAVY,
    )

    draw.line(
        (
            46,
            132,
            W - 46,
            132,
        ),
        fill=NAVY,
        width=max(1, ss(3)),
    )


def draw_navy_footer(
    draw: ImageDraw.ImageDraw,
) -> None:
    """
    Navy footer bar from the approved reference.
    """
    footer_y1 = H - sy(88)
    footer_y2 = H - sy(24)

    draw.rounded_rectangle(
        (
            46,
            footer_y1,
            W - 46,
            footer_y2,
        ),
        radius=ss(18),
        fill=NAVY,
    )

    footer_font = font(
        25,
        True,
    )

    draw.text(
        (
            80,
            footer_y1 + 16,
        ),
        "X  thedailyduck.ai",
        font=footer_font,
        fill=WHITE,
    )

    draw.text(
        (
            395,
            footer_y1 + 16,
        ),
        "|   #TheDailyDuck",
        font=footer_font,
        fill=WHITE,
    )


def render_legacy_card(
    ready: dict[str, Any],
) -> Path:
    """
    Legacy behavior kept only for issue dates before 2026-08-22.
    This prevents accidental visual changes if an older issue is rebuilt.
    """
    approved_root = ready.get(
        "gate_a_approved_story"
    )

    if not isinstance(
        approved_root,
        dict,
    ):
        raise ValueError(
            "gate_a_approved_story is missing"
        )

    approved = resolve_approved_story(
        ready
    )

    issue_date = first_text(
        ready.get(
            "issue_date"
        ),
        approved_root.get(
            "issue_date"
        ),
        approved_root.get(
            "date"
        ),
    )

    hero_path = resolve_hero_path(
        ready
    )

    title = first_text(
        ready.get(
            "selected_title"
        ),
        approved.get(
            "duck_name"
        ),
        "DAILY DUCK",
    ).upper()

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

    source = first_text(
        approved.get(
            "source"
        ),
        approved_root.get(
            "source"
        ),
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
        radius=ss(38),
        outline=BORDER,
        width=max(1, ss(3)),
        fill=CREAM,
    )

    # Legacy image geometry.
    hero_x = sx(600)
    hero_y = sy(140)
    hero_w = W - hero_x - sx(16)
    hero_h = H - hero_y - sy(92)

    source_hero = Image.open(
        hero_path
    ).convert("RGB")

    # 2026-08-22+ source images are 51:58 (portrait).
    # Fit proportionally inside the X-card hero region.
    # Never stretch or crop the duck.
    sw, sh = source_hero.size

    scale = min(
        hero_w / sw,
        hero_h / sh,
    )

    rw = max(
        1,
        int(round(sw * scale)),
    )

    rh = max(
        1,
        int(round(sh * scale)),
    )

    hero = source_hero.resize(
        (rw, rh),
        Image.Resampling.LANCZOS,
    )

    card.paste(
        hero,
        (
            hero_x,
            hero_y,
        ),
    )

    draw_logo_and_header(
        card,
        draw,
    )

    left = sx(58)
    panel_w = sx(505)

    pill_font = font(
        28,
        True,
    )

    pill_text = (
        "DUCK OF THE DAY"
    )

    pb = draw.textbbox(
        (0, 0),
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
            sy(244),
        ),
        radius=ss(18),
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

    title_font = font(
        72,
        True,
    )

    title_lines = truncate_lines(
        draw,
        wrap_words_by_pixels(
            draw,
            title,
            title_font,
            panel_w,
        ),
        title_font,
        panel_w,
        2,
    )

    y = draw_lines(
        draw,
        left,
        286,
        title_lines,
        title_font,
        NAVY,
        76,
    )

    draw.rounded_rectangle(
        (
            left,
            y + 8,
            left + sx(160),
            y + 20,
        ),
        radius=6,
        fill=YELLOW,
    )

    y += sy(52)

    try:
        legacy_date = (
            datetime.strptime(
                issue_date,
                "%Y-%m-%d",
            )
        )
        date_text = (
            f"{legacy_date.year}."
            f"{legacy_date.month}."
            f"{legacy_date.day}"
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

    y += sy(58)

    teaser_font = font(
        29,
        True,
    )

    teaser_lines = truncate_lines(
        draw,
        wrap_words_by_pixels(
            draw,
            teaser,
            teaser_font,
            panel_w,
        ),
        teaser_font,
        panel_w,
        4,
    )

    draw_lines(
        draw,
        left,
        y,
        teaser_lines,
        teaser_font,
        NAVY,
        41,
    )

    draw.text(
        (
            left,
            1015,
        ),
        f"Source: {source}",
        font=font(
            20,
            False,
        ),
        fill=MUTED,
    )

    draw_navy_footer(
        draw
    )

    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    out_path = (
        OUT_DIR
        / f"{issue_date}-x-card.png"
    )

    card.save(
        out_path,
        "PNG",
        optimize=True,
    )

    return out_path


def render_english_first_card(
    ready: dict[str, Any],
) -> Path:
    """
    2026-08-22+ production layout.

    Key requirements:
    - hero image fills about 80% of the usable card height
    - selected final title is used
    - story teaser is English
    - no Japanese copy appears in the X card body
    - card remains fixed 1200x960 (5:4)
    """
    approved_root = ready.get(
        "gate_a_approved_story"
    )

    if not isinstance(
        approved_root,
        dict,
    ):
        raise ValueError(
            "gate_a_approved_story is missing"
        )

    approved = resolve_approved_story(
        ready
    )

    issue_date = first_text(
        ready.get(
            "issue_date"
        ),
        approved_root.get(
            "issue_date"
        ),
        approved_root.get(
            "date"
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

    hero_path = resolve_hero_path(
        ready
    )

    # Use the human-selected publication title first.
    title = first_text(
        ready.get(
            "selected_title"
        ),
        approved_root.get(
            "selected_title"
        ),
        approved.get(
            "title_en"
        ),
        approved.get(
            "duck_name"
        ),
        "DAILY DUCK",
    ).upper()

    # English description in the previously red-boxed area.
    # x_en is the direct English equivalent of the old x_jp teaser.
    teaser = first_text(
        approved.get(
            "x_en"
        ),
        approved_root.get(
            "x_en"
        ),
        approved.get(
            "reason_en"
        ),
        approved.get(
            "en_copy"
        ),
    )

    if not teaser:
        raise ValueError(
            "No English teaser is available. "
            "Expected x_en, reason_en or en_copy."
        )

    source = first_text(
        approved.get(
            "source"
        ),
        approved_root.get(
            "source"
        ),
        approved_root.get(
            "source_name"
        ),
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

    # Outer rounded cream frame.
    draw.rounded_rectangle(
        (
            8,
            8,
            W - 8,
            H - 8,
        ),
        radius=ss(38),
        outline=BORDER,
        width=max(1, ss(3)),
        fill=CREAM,
    )

    draw_logo_and_header(
        card,
        draw,
    )

    # ========================================================
    # HERO IMAGE
    #
    # The user requested the photo to occupy about 80%
    # of the frame height. On a 1200px canvas, the 930px
    # hero is ~77.5% of total height and ~80% of the inner
    # rounded-frame height after edge padding.
    # ========================================================

    hero_x = sx(610)
    hero_y = sy(166)
    hero_w = W - hero_x - sx(42)
    hero_h = sy(930)

    hero = fit_cover(
        Image.open(
            hero_path
        ).convert("RGB"),
        (
            hero_w,
            hero_h,
        ),
    )

    # Center the 51:58 portrait source inside the hero region.
    hero_left = (
        hero_x
        + (hero_w - hero.width) // 2
    )

    hero_top = (
        hero_y
        + (hero_h - hero.height) // 2
    )

    hero_mask = Image.new(
        "L",
        hero.size,
        0,
    )

    mask_draw = ImageDraw.Draw(
        hero_mask
    )

    mask_draw.rounded_rectangle(
        (
            0,
            0,
            hero.width,
            hero.height,
        ),
        radius=ss(28),
        fill=255,
    )

    card.paste(
        hero,
        (
            hero_left,
            hero_top,
        ),
        hero_mask,
    )

    # ========================================================
    # LEFT COPY PANEL
    # ========================================================

    left = sx(54)
    panel_w = sx(500)

    pill_text = (
        "DUCK OF THE DAY"
    )

    pill_font = font(
        27,
        True,
    )

    pb = draw.textbbox(
        (0, 0),
        pill_text,
        font=pill_font,
    )

    pill_w = (
        pb[2]
        - pb[0]
        + 38
    )

    draw.rounded_rectangle(
        (
            left,
            184,
            left + pill_w,
            sy(238),
        ),
        radius=ss(18),
        fill=YELLOW,
    )

    draw.text(
        (
            left + 19,
            195,
        ),
        pill_text,
        font=pill_font,
        fill=NAVY,
    )

    # --------------------------------------------------------
    # Selected final title.
    # Fit to max 2 lines.
    # --------------------------------------------------------

    title_font_size = 72
    title_lines: list[str] = []

    while title_font_size >= 50:
        title_font = font(
            title_font_size,
            True,
        )

        candidate_lines = (
            wrap_words_by_pixels(
                draw,
                title,
                title_font,
                panel_w,
            )
        )

        if len(candidate_lines) <= 2:
            title_lines = (
                candidate_lines
            )
            break

        title_font_size -= 2

    if not title_lines:
        title_font = font(
            50,
            True,
        )

        title_lines = truncate_lines(
            draw,
            wrap_words_by_pixels(
                draw,
                title,
                title_font,
                panel_w,
            ),
            title_font,
            panel_w,
            2,
        )

    y = draw_lines(
        draw,
        left,
        282,
        title_lines[:2],
        title_font,
        NAVY,
        int(
            title_font_size
            * 1.02
        ),
    )

    # Yellow accent line aligned below title.
    accent_y = y + sy(12)

    draw.rounded_rectangle(
        (
            left,
            accent_y,
            left + sx(155),
            accent_y + 10,
        ),
        radius=5,
        fill=YELLOW,
    )

    # --------------------------------------------------------
    # English date.
    # --------------------------------------------------------

    y = accent_y + sy(43)

    draw.text(
        (
            left,
            y,
        ),
        display_date_english(
            issue_date
        ),
        font=font(
            27,
            True,
        ),
        fill=NAVY,
    )

    # --------------------------------------------------------
    # English explanation/teaser.
    # This replaces the previous Japanese red-box area.
    # --------------------------------------------------------

    y += sy(64)

    teaser_font_size = 29
    teaser_font = font(
        teaser_font_size,
        False,
    )

    teaser_lines = (
        wrap_words_by_pixels(
            draw,
            teaser,
            teaser_font,
            panel_w,
        )
    )

    teaser_lines = truncate_lines(
        draw,
        teaser_lines,
        teaser_font,
        panel_w,
        6,
    )

    draw_lines(
        draw,
        left,
        y,
        teaser_lines,
        teaser_font,
        NAVY,
        42,
    )

    # Source positioned above the footer.
    draw.text(
        (
            left,
            1022,
        ),
        f"Source: {source}",
        font=font(
            19,
            False,
        ),
        fill=MUTED,
    )

    draw_navy_footer(
        draw
    )

    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    out_path = (
        OUT_DIR
        / f"{issue_date}-x-card.png"
    )

    card.save(
        out_path,
        "PNG",
        optimize=True,
    )

    return out_path


def render_card(
    ready: dict[str, Any],
) -> tuple[Path, str]:
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

    issue = parse_issue_date(
        issue_date
    )

    if issue >= NEW_LAYOUT_FROM:
        return (
            render_english_first_card(
                ready
            ),
            "english-first-80pct-photo-v1",
        )

    return (
        render_legacy_card(
            ready
        ),
        "legacy-pre-2026-08-22",
    )


def main() -> int:
    ready = load_ready()

    out_path, layout_version = (
        render_card(
            ready
        )
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
    ] = "PNG"

    ready[
        "x_card_layout_version"
    ] = layout_version

    ready[
        "x_card_language"
    ] = (
        "en"
        if layout_version
        == "english-first-80pct-photo-v1"
        else "legacy"
    )

    ready[
        "x_card_hero_height_px"
    ] = (
        sy(930)
        if layout_version
        == "english-first-80pct-photo-v1"
        else None
    )

    save_ready(
        ready
    )

    print(
        f"X CARD CREATED: "
        f"{out_path}"
    )

    print(
        f"SIZE: {W}x{H} (5:4)"
    )

    print(
        f"LAYOUT: "
        f"{layout_version}"
    )

    if (
        layout_version
        == "english-first-80pct-photo-v1"
    ):
        print(
            "CARD LANGUAGE: ENGLISH"
        )
        print(
            "HERO PHOTO: ~80% "
            "of inner-frame height"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
