#!/usr/bin/env python3
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


CONCEPTS_PATH = Path(
    "automation_state/image_concepts.json"
)

X_DIR = Path(
    "automation_state/concept_assets/x"
)

BRAND_LOGO = Path(
    "assets/brand/the-daily-duck-emblem-128.png"
)

CARD_WIDTH = 1600
CARD_HEIGHT = 900

FONT_REGULAR = Path(
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
)

FONT_BOLD = Path(
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def font(size: int, bold: bool = False):
    path = (
        FONT_BOLD
        if bold
        else FONT_REGULAR
    )

    if not path.exists():
        raise FileNotFoundError(
            f"Font not found: {path}"
        )

    return ImageFont.truetype(
        str(path),
        size=size,
    )


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fit_image(
    image: Image.Image,
    size: tuple[int, int],
) -> Image.Image:

    return ImageOps.fit(
        image.convert("RGB"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def wrap_by_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt,
    max_width: int,
) -> list[str]:

    words = text.split()

    if not words:
        return []

    lines: list[str] = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=fnt,
        )

        if bbox[2] <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    lines.append(current)

    return lines


def draw_multiline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    lines: list[str],
    fnt,
    fill,
    spacing: int,
):
    x, y = xy

    for line in lines:
        draw.text(
            (x, y),
            line,
            font=fnt,
            fill=fill,
        )

        bbox = draw.textbbox(
            (x, y),
            line,
            font=fnt,
        )

        y += (
            bbox[3] - bbox[1]
            + spacing
        )

    return y


def create_card(
    data: dict[str, Any],
    concept: dict[str, Any],
) -> Path:

    number = int(
        concept["number"]
    )

    story = data.get("story")

    if not isinstance(story, dict):
        story = {}

    title_ja = first_text(
        story.get("title_ja"),
        story.get("title"),
    )

    reason_ja = first_text(
        story.get("reason_ja"),
        story.get("reason"),
    )

    duck_name = first_text(
        story.get("duck_name"),
        "DUCK OF THE DAY",
    )

    issue_date = first_text(
        data.get("issue_date")
    )

    web_path = Path(
        concept["web_image_path"]
    )

    if not web_path.exists():
        raise FileNotFoundError(
            web_path
        )

    canvas = Image.new(
        "RGB",
        (CARD_WIDTH, CARD_HEIGHT),
        "#f7f2e5",
    )

    draw = ImageDraw.Draw(
        canvas
    )

    # Outer border
    draw.rounded_rectangle(
        (18, 18, CARD_WIDTH - 18, CARD_HEIGHT - 18),
        radius=28,
        outline="#1b2636",
        width=3,
    )

    # Header
    draw.rectangle(
        (20, 20, CARD_WIDTH - 20, 112),
        fill="#fbf5e7",
    )

    if BRAND_LOGO.exists():
        logo = Image.open(
            BRAND_LOGO
        ).convert("RGBA")

        logo.thumbnail(
            (70, 70),
            Image.Resampling.LANCZOS,
        )

        canvas.paste(
            logo,
            (42, 30),
            logo,
        )

    draw.text(
        (125, 43),
        "THE DAILY DUCK",
        font=font(34, True),
        fill="#111827",
    )

    draw.text(
        (1185, 51),
        "ONE DAY. ONE STORY. ONE DUCK.",
        font=font(18, True),
        fill="#364152",
    )

    # Main visual on right
    visual = fit_image(
        Image.open(web_path),
        (870, 650),
    )

    canvas.paste(
        visual,
        (700, 125),
    )

    # Left information panel
    draw.rounded_rectangle(
        (55, 150, 330, 206),
        radius=16,
        fill="#ffc400",
    )

    draw.text(
        (78, 164),
        "DUCK OF THE DAY",
        font=font(23, True),
        fill="#111111",
    )

    display_title = (
        duck_name.upper()
        if duck_name
        else "DAILY DUCK"
    )

    title_font = font(
        70,
        True,
    )

    title_lines = wrap_by_pixels(
        draw,
        display_title,
        title_font,
        570,
    )

    y = draw_multiline(
        draw,
        (65, 238),
        title_lines[:3],
        title_font,
        "#111827",
        2,
    )

    draw.rectangle(
        (65, y + 10, 165, y + 18),
        fill="#ffc400",
    )

    y += 42

    draw.text(
        (65, y),
        issue_date,
        font=font(25, True),
        fill="#253044",
    )

    y += 48

    # Japanese story title
    jp_title_font = font(
        27,
        True,
    )

    # Japanese needs character-based wrapping.
    jp_title_lines = textwrap.wrap(
        title_ja,
        width=21,
    )[:3]

    y = draw_multiline(
        draw,
        (65, y),
        jp_title_lines,
        jp_title_font,
        "#111827",
        9,
    )

    y += 20

    reason_font = font(
        21,
        False,
    )

    reason_lines = textwrap.wrap(
        reason_ja,
        width=29,
    )[:5]

    draw_multiline(
        draw,
        (65, y),
        reason_lines,
        reason_font,
        "#374151",
        8,
    )

    # Footer
    draw.rectangle(
        (20, 790, CARD_WIDTH - 20, CARD_HEIGHT - 20),
        fill="#142033",
    )

    draw.text(
        (62, 823),
        "thedailyduck.ai",
        font=font(23, True),
        fill="#ffffff",
    )

    draw.text(
        (1260, 823),
        "#TheDailyDuck",
        font=font(22, True),
        fill="#ffffff",
    )

    X_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = (
        X_DIR
        / f"concept_{number}_x.png"
    )

    canvas.save(
        output,
        format="PNG",
        optimize=True,
    )

    return output


def main() -> None:
    data = load_json(
        CONCEPTS_PATH
    )

    concepts = data.get(
        "concepts"
    )

    if (
        not isinstance(concepts, list)
        or len(concepts) != 3
    ):
        raise ValueError(
            "Exactly three concepts required."
        )

    if str(
        data.get("state", "")
    ).upper() != "WEB_IMAGES_READY":
        raise ValueError(
            "Expected WEB_IMAGES_READY."
        )

    X_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for old in X_DIR.glob(
        "concept_*_x.png"
    ):
        old.unlink()

    for concept in concepts:
        output = create_card(
            data,
            concept,
        )

        concept["x_image_status"] = "GENERATED"
        concept["x_image_path"] = output.as_posix()
        concept["x_generated_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        print(
            f"Created X card: {output}"
        )

    data["state"] = "IMAGE_CONCEPT_REVIEW"
    data["x_image_count"] = 3

    save_json(
        CONCEPTS_PATH,
        data,
    )

    print("Generated exactly 3 X cards.")
    print("STATE: IMAGE_CONCEPT_REVIEW")


if __name__ == "__main__":
    main()
