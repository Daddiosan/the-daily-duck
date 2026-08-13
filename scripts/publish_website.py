#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import shutil
import sys

from datetime import datetime, timezone
from pathlib import Path


READY = Path(
    "automation_state/ready_to_publish.json"
)

RESULT = Path(
    "automation_state/website_publish_result.json"
)

TODAY = Path(
    "data/today.json"
)

ARCHIVE = Path(
    "data/archive.json"
)

CONTENT = Path(
    "data/content.js"
)

HOME = Path(
    "index.html"
)

ASSET_DIR = Path(
    "assets/ducks"
)

DUCKS_DIR = Path(
    "ducks"
)


# ============================================================
# Helpers
# ============================================================

def load_json(path: Path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing required file: {path}"
        )

    return json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )


def write_json(
    path: Path,
    value,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def result(
    action,
    **extra,
):
    write_json(
        RESULT,
        {
            "action": action,
            "at": now(),
            **extra,
        },
    )


def text(*values):
    for value in values:
        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


def slugify(value):
    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value.lower().strip(),
    ).strip("-")

    return (
        value
        or "daily-duck"
    )


def e(value):
    return html.escape(
        str(value),
        quote=True,
    )


# ============================================================
# Build website item
# ============================================================

def build_item(
    ready,
    image_rel,
):
    approved = ready.get(
        "gate_a_approved_story"
    )

    if not isinstance(
        approved,
        dict,
    ):
        raise ValueError(
            "gate_a_approved_story is missing."
        )

    story = (
        approved.get(
            "recommended_story"
        )
        if isinstance(
            approved.get(
                "recommended_story"
            ),
            dict,
        )
        else {}
    )

    date = text(
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

    duck_name = text(
        approved.get(
            "duck_name"
        ),
        "Daily Duck",
    )

    story_ja = text(
        approved.get(
            "jp_copy"
        )
    )

    story_en = text(
        approved.get(
            "en_copy"
        ),
        story.get(
            "reason"
        ),
        approved.get(
            "recommended_reason"
        ),
    )

    duck_ja = text(
        approved.get(
            "duck_jp"
        )
    )

    duck_en = text(
        approved.get(
            "duck_en"
        )
    )

    source = text(
        approved.get(
            "source"
        ),
        story.get(
            "source"
        ),
    )

    source_url = text(
        approved.get(
            "source_url"
        ),
        story.get(
            "url"
        ),
    )

    if not all(
        [
            date,
            story_ja,
            story_en,
            duck_ja,
            duck_en,
            source,
            source_url,
        ]
    ):
        raise ValueError(
            "Approved story is missing website text fields."
        )

    selected = (
        ready.get(
            "selected_candidate"
        )
        if isinstance(
            ready.get(
                "selected_candidate"
            ),
            dict,
        )
        else {}
    )

    alt_ja = text(
        selected.get(
            "alt_ja"
        ),
        selected.get(
            "concept_ja"
        ),
        (
            f"{duck_name}をテーマにした"
            "The Daily Duckの黄色いダック"
        ),
    )

    alt_en = text(
        selected.get(
            "alt_en"
        ),
        selected.get(
            "concept_en"
        ),
        (
            "The Daily Duck yellow duck mascot "
            f"for {duck_name}"
        ),
    )

    dt = datetime.strptime(
        date,
        "%Y-%m-%d",
    )

    return {
        "date":
            date,

        "displayDate":
            dt.strftime(
                "%B %d, %Y"
            ).replace(
                " 0",
                " ",
            ),

        "slug":
            slugify(
                duck_name
            ),

        "title":
            duck_name.upper(),

        "image":
            image_rel,

        "imageAltJa":
            alt_ja,

        "imageAltEn":
            alt_en,

        "storyJa":
            story_ja,

        "storyEn":
            story_en,

        "duckJa":
            duck_ja,

        "duckEn":
            duck_en,

        "sourceLabel":
            source,

        "sourceUrl":
            source_url,

        "archiveSummaryJa":
            text(
                approved.get(
                    "x_jp"
                ),
                story_ja,
            ),

        "archiveSummaryEn":
            text(
                approved.get(
                    "x_en"
                ),
                story_en,
            ),

        "published":
            True,
    }


# ============================================================
# Render archive article page
# ============================================================

def render_page(item):
    page_url = (
        "https://www.thedailyduck.ai/"
        f"ducks/{item['date']}/"
    )

    image_url = (
        "https://www.thedailyduck.ai/"
        f"{item['image']}"
    )

    schema = json.dumps(
        {
            "@context":
                "https://schema.org",

            "@type":
                "Article",

            "headline":
                item["title"],

            "datePublished":
                item["date"],

            "image":
                [
                    image_url
                ],

            "mainEntityOfPage":
                page_url,

            "publisher":
                {
                    "@type":
                        "Organization",

                    "name":
                        "The Daily Duck",

                    "url":
                        "https://www.thedailyduck.ai/",
                },
        },
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    )

    return """<!doctype html>
<html lang="en">
<head>

<script async src="https://www.googletagmanager.com/gtag/js?id=G-ZY7DDS7PRO"></script>

<script>
window.dataLayer = window.dataLayer || [];
function gtag(){{dataLayer.push(arguments);}}
gtag('js', new Date());
gtag('config', 'G-ZY7DDS7PRO');
</script>

<meta charset="utf-8">

<meta
  name="viewport"
  content="width=device-width,initial-scale=1"
>

<title>{title} — The Daily Duck</title>

<meta
  name="description"
  content="{summary_en}"
>

<meta
  name="robots"
  content="index,follow,max-image-preview:large"
>

<link
  rel="canonical"
  href="{page_url}"
>

<meta
  property="og:type"
  content="article"
>

<meta
  property="og:site_name"
  content="The Daily Duck"
>

<meta
  property="og:title"
  content="{title} — The Daily Duck"
>

<meta
  property="og:description"
  content="{summary_en}"
>

<meta
  property="og:url"
  content="{page_url}"
>

<meta
  property="og:image"
  content="{image_url}"
>

<meta
  property="og:image:alt"
  content="{alt_en}"
>

<meta
  name="twitter:card"
  content="summary_large_image"
>

<meta
  name="twitter:title"
  content="{title} — The Daily Duck"
>

<meta
  name="twitter:description"
  content="{summary_en}"
>

<meta
  name="twitter:image"
  content="{image_url}"
>

<style>
*{{
  box-sizing:border-box
}}

body{{
  margin:0;
  background:#fffdf8;
  color:#111;
  font-family:Arial,Helvetica,sans-serif
}}

header{{
  max-width:1100px;
  margin:auto;
  padding:22px 24px;
  border-bottom:1px solid #e8e0d2;
  display:flex;
  justify-content:space-between;
  align-items:center
}}

a{{
  color:inherit
}}

.brand{{
  font-weight:900;
  text-decoration:none;
  font-size:20px
}}

.brand-official{{
  display:inline-flex;
  align-items:center;
  gap:10px
}}

.brand-official img{{
  display:block;
  width:40px;
  height:40px;
  object-fit:contain
}}

main{{
  max-width:1100px;
  margin:54px auto;
  padding:0 24px
}}

.hero{{
  display:grid;
  grid-template-columns:minmax(280px,440px) 1fr;
  gap:58px;
  align-items:center
}}

.imagebox{{
  border:1px solid #e8dfd1;
  border-radius:20px;
  padding:22px;
  background:#fff
}}

.imagebox img{{
  width:100%;
  height:auto;
  display:block;
  border-radius:14px
}}

.badge{{
  display:inline-block;
  background:#ffc400;
  border-radius:999px;
  padding:8px 13px;
  font-size:12px;
  font-weight:900
}}

.date{{
  margin:18px 0 8px;
  color:#666;
  font-size:13px
}}

h1{{
  font-size:clamp(48px,7vw,84px);
  line-height:.9;
  margin:0 0 20px;
  letter-spacing:-.05em
}}

.tagline{{
  font-weight:900;
  font-size:20px
}}

.summary{{
  color:#555;
  line-height:1.7
}}

.cols{{
  display:grid;
  grid-template-columns:1fr 1fr;
  margin-top:30px;
  border-top:1px solid #ddd;
  border-bottom:1px solid #ddd
}}

.col{{
  padding:22px 24px 22px 0;
  line-height:1.8
}}

.col+.col{{
  border-left:1px solid #ddd;
  padding-left:24px
}}

.col h2{{
  font-size:15px;
  margin-top:0
}}

.source{{
  margin-top:24px;
  font-weight:700;
  font-size:13px
}}

.back{{
  display:inline-block;
  margin-top:34px;
  padding:13px 20px;
  border-radius:999px;
  background:#ffc400;
  text-decoration:none;
  font-weight:900
}}

.jp{{
  margin-top:38px;
  padding-top:28px;
  border-top:1px solid #eee
}}

.jp p{{
  line-height:1.9
}}

@media(max-width:760px){{
  .hero{{
    grid-template-columns:1fr;
    gap:28px
  }}

  h1{{
    font-size:48px
  }}

  .cols{{
    grid-template-columns:1fr
  }}

  .col+.col{{
    border-left:0;
    border-top:1px solid #ddd;
    padding-left:0
  }}
}}
</style>

<script type="application/ld+json">
{schema}
</script>

<link
  rel="icon"
  type="image/png"
  sizes="32x32"
  href="/assets/brand/favicon-32.png"
>

<link
  rel="icon"
  type="image/png"
  sizes="16x16"
  href="/assets/brand/favicon-16.png"
>

<link
  rel="apple-touch-icon"
  sizes="180x180"
  href="/assets/brand/apple-touch-icon.png"
>

</head>

<body>

<header>

<a
  class="brand brand-official"
  href="/"
>

<img
  src="/assets/brand/the-daily-duck-emblem-128.png"
  alt=""
  width="40"
  height="40"
>

<span>
The Daily Duck
</span>

</a>

<a href="/#archive">
Archive
</a>

</header>

<main>

<section class="hero">

<div class="imagebox">

<img
  src="/{image}"
  alt="{alt_en}"
>

</div>

<div>

<span class="badge">
TODAY'S DUCK ARCHIVE
</span>

<div class="date">
{date}
</div>

<h1>
{title}
</h1>

<div class="tagline">
One day. One story. One duck.
</div>

<p class="summary">
{summary_en}
</p>

<div class="cols">

<div class="col">

<h2>
Today's Story
</h2>

<p>
{story_en}
</p>

</div>

<div class="col">

<h2>
The Duck
</h2>

<p>
{duck_en}
</p>

</div>

</div>

<div class="source">

Source:
<a
  href="{source_url}"
  rel="noopener noreferrer"
>
{source}
</a>

</div>

<a
  class="back"
  href="/#archive"
>
← Duck Archive
</a>

</div>

</section>

<div class="jp">

<strong>
日本語
</strong>

<p>
{summary_ja}
</p>

<p>
{story_ja}
</p>

<p>
{duck_ja}
</p>

</div>

</main>

</body>

</html>
""".format(
        title=e(
            item["title"]
        ),

        summary_en=e(
            item[
                "archiveSummaryEn"
            ]
        ),

        page_url=e(
            page_url
        ),

        image_url=e(
            image_url
        ),

        alt_en=e(
            item[
                "imageAltEn"
            ]
        ),

        schema=schema,

        image=e(
            item["image"]
        ),

        date=e(
            item["date"]
        ),

        story_en=e(
            item["storyEn"]
        ),

        duck_en=e(
            item["duckEn"]
        ),

        source_url=e(
            item["sourceUrl"]
        ),

        source=e(
            item["sourceLabel"]
        ),

        summary_ja=e(
            item[
                "archiveSummaryJa"
            ]
        ),

        story_ja=e(
            item["storyJa"]
        ),

        duck_ja=e(
            item["duckJa"]
        ),
    )


# ============================================================
# Update homepage OG image
# ============================================================

def update_home(item):
    source = HOME.read_text(
        encoding="utf-8"
    )

    image_url = (
        "https://www.thedailyduck.ai/"
        f"{item['image']}"
    )

    source = re.sub(
        r'<meta property="og:image" content="[^"]*">',
        (
            '<meta property="og:image" '
            f'content="{e(image_url)}">'
        ),
        source,
    )

    source = re.sub(
        r'<meta property="og:image:alt" content="[^"]*">',
        (
            '<meta property="og:image:alt" '
            f'content="{e(item["imageAltEn"])}">'
        ),
        source,
    )

    source = re.sub(
        r'<meta name="twitter:image" content="[^"]*">',
        (
            '<meta name="twitter:image" '
            f'content="{e(image_url)}">'
        ),
        source,
    )

    HOME.write_text(
        source,
        encoding="utf-8",
    )


# ============================================================
# Main
# ============================================================

def main():
    ready = load_json(
        READY
    )

    if (
        ready.get("state")
        != "READY_TO_PUBLISH"
    ):
        raise RuntimeError(
            "Expected READY_TO_PUBLISH, "
            f"got {ready.get('state')!r}."
        )

    archive = load_json(
        ARCHIVE
    )

    if not isinstance(
        archive,
        list,
    ):
        raise ValueError(
            "data/archive.json must be an array."
        )

    issue = text(
        ready.get(
            "issue_date"
        )
    )

    if not issue:
        raise ValueError(
            "issue_date is missing."
        )

    # --------------------------------------------------------
    # Duplicate-date protection
    # --------------------------------------------------------

    if any(
        isinstance(item, dict)
        and str(
            item.get(
                "date",
                "",
            )
        )
        == issue
        for item in archive
    ):
        result(
            "DUPLICATE_DATE_BLOCKED",
            issue_date=issue,
            message=(
                "Date already exists. "
                "No website files changed."
            ),
        )

        print(
            f"DUPLICATE DATE BLOCKED: {issue}"
        )

        print(
            "STATE: DUPLICATE_DATE_BLOCKED"
        )

        return 0

    # --------------------------------------------------------
    # Canonical image
    # --------------------------------------------------------

    canonical = Path(
        text(
            ready.get(
                "canonical_image_path"
            )
        )
    )

    if not canonical.exists():
        raise FileNotFoundError(
            "Canonical image not found: "
            f"{canonical}"
        )

    approved = ready.get(
        "gate_a_approved_story"
    )

    duck_name = text(
        (
            approved.get(
                "duck_name"
            )
            if isinstance(
                approved,
                dict,
            )
            else ""
        ),
        "Daily Duck",
    )

    extension = (
        canonical.suffix.lower()
        or ".png"
    )

    asset = (
        ASSET_DIR
        / (
            f"{issue}-"
            f"{slugify(duck_name)}"
            f"{extension}"
        )
    )

    # --------------------------------------------------------
    # Build site content
    # --------------------------------------------------------

    item = build_item(
        ready,
        asset.as_posix(),
    )

    ASSET_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(
        canonical,
        asset,
    )

    write_json(
        TODAY,
        item,
    )

    new_archive = [
        item
    ] + archive

    write_json(
        ARCHIVE,
        new_archive,
    )

    CONTENT.write_text(
        "window.DAILY_DUCK_DATA = "
        + json.dumps(
            {
                "today":
                    item,

                "archive":
                    new_archive,
            },
            ensure_ascii=False,
            indent=2,
        )
        + ";\n",
        encoding="utf-8",
    )

    duck_directory = (
        DUCKS_DIR
        / issue
    )

    duck_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        duck_directory
        / "index.html"
    ).write_text(
        render_page(
            item
        ),
        encoding="utf-8",
    )

    update_home(
        item
    )

    # --------------------------------------------------------
    # Advance state
    # --------------------------------------------------------

    ready[
        "state"
    ] = "PUBLISHED"

    ready[
        "published_at"
    ] = now()

    ready[
        "published_image_path"
    ] = asset.as_posix()

    ready[
        "published_page_path"
    ] = (
        f"ducks/{issue}/index.html"
    )

    ready[
        "publish_started"
    ] = True

    write_json(
        READY,
        ready,
    )

    result(
        "PUBLISHED",
        issue_date=issue,
        image=asset.as_posix(),
        page=(
            f"ducks/{issue}/index.html"
        ),
        title=item["title"],
    )

    print(
        f"Published website package for {issue}"
    )

    print(
        "STATE: PUBLISHED"
    )

    return 0


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise
