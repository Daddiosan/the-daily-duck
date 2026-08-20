#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any


OPTIONS_PATH = Path(
    "automation_state/design_options.json"
)


def required_env(
    name: str,
) -> str:

    value = os.getenv(
        name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def first_text(
    *values: Any,
) -> str:

    for value in values:
        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):
            return value.strip()

    return ""


def esc(
    value: Any,
) -> str:

    return (
        str(value)
        .replace(
            "&",
            "&amp;",
        )
        .replace(
            "<",
            "&lt;",
        )
        .replace(
            ">",
            "&gt;",
        )
        .replace(
            '"',
            "&quot;",
        )
    )


def mime_for(
    path: Path,
) -> tuple[str, str]:

    guessed, _ = mimetypes.guess_type(
        path.name
    )

    if guessed and "/" in guessed:
        return tuple(
            guessed.split(
                "/",
                1,
            )
        )  # type: ignore[return-value]

    return (
        "image",
        "png",
    )


def load_package() -> dict[str, Any]:

    if not OPTIONS_PATH.exists():
        raise FileNotFoundError(
            f"Missing required file: {OPTIONS_PATH}"
        )

    package = json.loads(
        OPTIONS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        package,
        dict,
    ):
        raise ValueError(
            "design_options.json "
            "must contain an object."
        )

    return package


def get_story_title(
    package: dict[str, Any],
) -> str:

    compact = package.get(
        "approved_story_compact"
    )

    if isinstance(
        compact,
        dict,
    ):
        return first_text(
            compact.get(
                "title_en"
            ),
            compact.get(
                "title"
            ),
            compact.get(
                "title_ja"
            ),
            "Approved Daily Duck Story",
        )

    return (
        "Approved Daily Duck Story"
    )


def parse_recipients(
    raw: str,
) -> list[str]:

    recipients = [
        x.strip()
        for x
        in raw.split(",")
        if x.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains "
            "no valid recipients."
        )

    return recipients


def build_concept_email(
    package: dict[str, Any],
) -> tuple[
    str,
    str,
    str,
]:

    concepts = package.get(
        "image_concepts"
    )

    titles = package.get(
        "title_ideas"
    )

    if (
        not isinstance(
            concepts,
            list,
        )
        or len(
            concepts
        ) != 3
    ):
        raise ValueError(
            "Exactly 3 image concepts "
            "are required."
        )

    if (
        not isinstance(
            titles,
            list,
        )
        or len(
            titles
        ) != 3
    ):
        raise ValueError(
            "Exactly 3 title ideas "
            "are required."
        )

    issue_date = first_text(
        package.get(
            "issue_date"
        )
    )

    subject = (
        "The Daily Duck — "
        "Choose Image Concept — "
        f"{issue_date}"
    )

    text_lines = [
        "THE DAILY DUCK — IMAGE CONCEPT SELECTION",
        "",
        "The Daily Duck is ENGLISH-FIRST.",
        "English is the canonical/master language.",
        "Japanese is provided as a review translation.",
        "",
        f"APPROVED STORY: {get_story_title(package)}",
        "",
        "Choose ONE image concept from the three options below.",
        "以下の3案から、使用する画像コンセプトを1つ選んでください。",
        "",
    ]

    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;color:#10254a'>",
        "<h2>The Daily Duck — Image Concept Selection</h2>",
        "<p><strong>English is the canonical/master language.</strong><br>",
        "日本語は確認用の翻訳です。</p>",
        f"<p><strong>Approved story:</strong> {esc(get_story_title(package))}</p>",
    ]

    for concept in concepts:

        n = int(
            concept[
                "number"
            ]
        )

        title_en = first_text(
            concept.get(
                "title_en"
            )
        )

        concept_en = first_text(
            concept.get(
                "concept_en"
            )
        )

        composition_en = first_text(
            concept.get(
                "composition_en"
            )
        )

        title_ja = first_text(
            concept.get(
                "title_ja"
            )
        )

        concept_ja = first_text(
            concept.get(
                "concept_ja"
            )
        )

        composition_ja = first_text(
            concept.get(
                "composition_ja"
            )
        )

        text_lines += [
            "=" * 60,
            f"[CONCEPT {n}] {title_en}",
            "",
            "ENGLISH MASTER",
            concept_en,
            "",
            "COMPOSITION",
            composition_en,
            "",
            "JAPANESE TRANSLATION / 日本語訳",
            title_ja,
            concept_ja,
            "",
            f"構図: {composition_ja}",
            "",
        ]

        html_parts += [
            "<div style='margin:24px 0;padding:18px;border:1px solid #ddd;border-radius:14px'>",
            f"<h3>CONCEPT {n} — {esc(title_en)}</h3>",
            f"<p><strong>English Master</strong><br>{esc(concept_en)}</p>",
            f"<p><strong>Composition</strong><br>{esc(composition_en)}</p>",
            "<hr style='border:0;border-top:1px solid #eee'>",
            f"<p><strong>日本語訳 — {esc(title_ja)}</strong><br>{esc(concept_ja)}</p>",
            f"<p><strong>構図</strong><br>{esc(composition_ja)}</p>",
            "</div>",
        ]

    text_lines += [
        "",
        "=" * 60,
        "TITLE IDEAS — INFORMATION ONLY",
        "タイトル候補 — この段階ではまだ選びません",
        "=" * 60,
        "",
    ]

    html_parts += [
        "<h3>Title Ideas — Information Only</h3>",
        "<p>タイトルは最終実画像の選択時に一緒に選びます。</p>",
    ]

    for item in titles:

        n = int(
            item[
                "number"
            ]
        )

        title = first_text(
            item.get(
                "title"
            )
        )

        meaning = first_text(
            item.get(
                "meaning_ja"
            )
        )

        text_lines += [
            f"[TITLE {n}] {title}",
            meaning,
            "",
        ]

        html_parts += [
            f"<p><strong>TITLE {n}: {esc(title)}</strong><br>{esc(meaning)}</p>"
        ]

    text_lines += [
        "",
        "=" * 60,
        "CONCEPT SELECTION / コンセプト選択",
        "=" * 60,
        "",
        "Reply with ONLY one number:",
        "使用するコンセプト番号だけを返信してください。",
        "",
        "1",
        "2",
        "3",
        "",
        "Example / 例:",
        "2",
        "",
        "IMPORTANT:",
        "- Only exact replies 1, 2, or 3 are valid.",
        "- 1〜3 の数字1文字だけが有効です。",
        "- This selects the VISUAL CONCEPT only.",
        "- この段階では画像コンセプトだけを選びます。",
        "- After selection, exactly FIVE real image variations will be generated from that ONE selected concept.",
        "- 選択した1コンセプトから実画像を5枚生成します。",
        "- The concept itself will remain fixed.",
        "- コンセプト自体は変更しません。",
        "- Final image + title selection happens in a later email.",
        "- 最終画像とタイトルの選択は次のメールで行います。",
        "- Nothing is published yet.",
        "- まだWeb/Xには公開されません。",
    ]

    html_parts += [
        "<div style='padding:18px;background:#f4f6f8;border-radius:12px'>",
        "<strong>Reply with ONE concept number only</strong><br>",
        "<span style='font-size:26px;font-weight:bold'>1 / 2 / 3</span><br><br>",
        "選択後、その1コンセプトから実画像を5枚生成します。<br>",
        "この段階では公開されません。",
        "</div>",
        "</body></html>",
    ]

    return (
        subject,
        "\n".join(
            text_lines
        ),
        "".join(
            html_parts
        ),
    )


def build_final_email(
    package: dict[str, Any],
) -> tuple[
    str,
    str,
    str,
    dict[
        int,
        Path,
    ],
]:

    previews = package.get(
        "design_previews"
    )

    titles = package.get(
        "title_ideas"
    )

    selected_concept = package.get(
        "selected_image_concept"
    )

    if (
        not isinstance(
            previews,
            list,
        )
        or len(
            previews
        ) != 5
    ):
        raise ValueError(
            "Exactly 5 real preview "
            "images are required."
        )

    if (
        not isinstance(
            titles,
            list,
        )
        or len(
            titles
        ) != 3
    ):
        raise ValueError(
            "Exactly 3 title ideas "
            "are required."
        )

    if not isinstance(
        selected_concept,
        dict,
    ):
        raise ValueError(
            "selected_image_concept "
            "is missing."
        )

    by_number = {
        int(
            item[
                "number"
            ]
        ):
            Path(
                item[
                    "image_path"
                ]
            )
        for item in previews
    }

    for n in range(
        1,
        6,
    ):
        p = by_number.get(
            n
        )

        if (
            p is None
            or not p.exists()
        ):
            raise FileNotFoundError(
                f"Preview image {n} "
                "is missing."
            )

    issue_date = first_text(
        package.get(
            "issue_date"
        )
    )

    batch_number = int(
        package.get(
            "preview_batch_number",
            1,
        )
        or 1
    )

    subject = (
        "The Daily Duck — "
        "Final Image + Title Approval — "
        f"{issue_date} — "
        f"Batch {batch_number}"
    )

    concept_title_en = first_text(
        selected_concept.get(
            "title_en"
        )
    )

    concept_title_ja = first_text(
        selected_concept.get(
            "title_ja"
        )
    )

    text_lines = [
        "THE DAILY DUCK — FINAL IMAGE + TITLE APPROVAL",
        "",
        "The visual concept is now LOCKED.",
        f"Selected concept: {concept_title_en}",
        f"日本語: {concept_title_ja}",
        "",
        "All five images below were generated from this SAME selected concept.",
        "以下の5枚はすべて、選択済みの同じコンセプトから生成されています。",
        "",
    ]

    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;color:#10254a'>",
        "<h2>The Daily Duck — Final Image + Title Approval</h2>",
        f"<p><strong>Locked concept:</strong> {esc(concept_title_en)}<br>",
        f"日本語: {esc(concept_title_ja)}</p>",
        "<p>All five images are variations of the same selected concept.</p>",
    ]

    for n in range(
        1,
        6,
    ):
        p = by_number[
            n
        ]

        cid = (
            f"dailyduck-final-{n}"
        )

        text_lines += [
            f"[IMAGE {n}]",
            p.as_posix(),
            "",
        ]

        html_parts += [
            "<div style='margin:24px 0;padding:16px;border:1px solid #ddd;border-radius:14px'>",
            f"<h3>IMAGE {n}</h3>",
            f"<img src='cid:{cid}' style='max-width:700px;width:100%;height:auto;border-radius:12px'>",
            "</div>",
        ]

    text_lines += [
        "",
        "TITLE / タイトル候補",
        "",
    ]

    html_parts += [
        "<h3>TITLE / タイトル候補</h3>",
    ]

    for item in titles:

        n = int(
            item[
                "number"
            ]
        )

        title = first_text(
            item.get(
                "title"
            )
        )

        meaning = first_text(
            item.get(
                "meaning_ja"
            )
        )

        text_lines += [
            f"[TITLE {n}] {title}",
            meaning,
            "",
        ]

        html_parts += [
            f"<p><strong>TITLE {n}: {esc(title)}</strong><br>{esc(meaning)}</p>"
        ]

    text_lines += [
        "",
        "=" * 60,
        "FINAL SELECTION / 最終選択",
        "=" * 60,
        "",
        "Reply:",
        "IMAGE_NUMBER + space + TITLE_NUMBER",
        "",
        "Example:",
        "4 1",
        "",
        "4 1 = final image 4 + title 1",
        "",
        "Valid image numbers: 1-5",
        "Valid title numbers: 1-3",
        "",
        "If none of the five images are good, reply:",
        "NEXT 5",
        "",
        "NEXT 5 keeps the SAME selected concept and generates five new real images.",
        "NEXT 5では、選択済みコンセプトは変更せず、新しい実画像5枚だけを再生成します。",
        "",
        "Nothing is published until a final image + title pair is selected.",
    ]

    html_parts += [
        "<div style='padding:18px;background:#f4f6f8;border-radius:12px'>",
        "<strong>Final selection</strong><br>",
        "画像番号 + 半角スペース + タイトル番号<br><br>",
        "<span style='font-size:26px;font-weight:bold'>4 1</span><br><br>",
        "気に入らない場合: <strong>NEXT 5</strong><br>",
        "NEXT 5でも選択済みコンセプトは維持されます。",
        "</div>",
        "</body></html>",
    ]

    return (
        subject,
        "\n".join(
            text_lines
        ),
        "".join(
            html_parts
        ),
        by_number,
    )


def send_message(
    subject: str,
    text_body: str,
    html_body: str,
    inline_images: (
        dict[
            int,
            Path,
        ]
        | None
    ) = None,
) -> int:

    gmail = required_env(
        "GMAIL_ADDRESS"
    )

    recipients = parse_recipients(
        required_env(
            "EMAIL_TO"
        )
    )

    msg = EmailMessage()

    msg[
        "Subject"
    ] = subject

    msg[
        "From"
    ] = gmail

    msg[
        "To"
    ] = ", ".join(
        recipients
    )

    msg.set_content(
        text_body
    )

    msg.add_alternative(
        html_body,
        subtype="html",
    )

    if inline_images:

        html_part = (
            msg.get_payload()[-1]
        )

        for (
            n,
            path,
        ) in inline_images.items():

            maintype, subtype = mime_for(
                path
            )

            html_part.add_related(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                cid=(
                    f"<dailyduck-final-{n}>"
                ),
                filename=path.name,
                disposition="inline",
            )

        for (
            n,
            path,
        ) in inline_images.items():

            maintype, subtype = mime_for(
                path
            )

            msg.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:

        smtp.login(
            gmail,
            required_env(
                "GMAIL_APP_PASSWORD"
            ),
        )

        smtp.send_message(
            msg
        )

    return len(
        recipients
    )


def main() -> int:

    package = load_package()

    state = first_text(
        package.get(
            "state"
        )
    ).upper()

    if state == "CONCEPTS_READY":

        (
            subject,
            text_body,
            html_body,
        ) = build_concept_email(
            package
        )

        count = send_message(
            subject,
            text_body,
            html_body,
        )

        package[
            "concept_email_subject"
        ] = subject

        package[
            "email_subject"
        ] = subject

        package[
            "state"
        ] = "WAITING_CONCEPT_SELECTION"

        OPTIONS_PATH.write_text(
            json.dumps(
                package,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            "Concept selection email "
            f"sent to {count} recipient(s)."
        )

        print(
            "Exactly 3 concepts presented."
        )

        print(
            "Valid replies: 1 / 2 / 3"
        )

        print(
            "STATE: WAITING_CONCEPT_SELECTION"
        )

        return 0

    if state == "DESIGN_PREVIEWS_READY":

        (
            subject,
            text_body,
            html_body,
            by_number,
        ) = build_final_email(
            package
        )

        count = send_message(
            subject,
            text_body,
            html_body,
            inline_images=by_number,
        )

        package[
            "final_email_subject"
        ] = subject

        package[
            "email_subject"
        ] = subject

        package[
            "state"
        ] = "WAITING_FINAL_SELECTION"

        OPTIONS_PATH.write_text(
            json.dumps(
                package,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            "Final image + title email "
            f"sent to {count} recipient(s)."
        )

        print(
            "Exactly 5 real images "
            "+ 3 titles presented."
        )

        print(
            "Valid selection: "
            "image 1-5 + title 1-3"
        )

        print(
            "Regeneration command: NEXT 5"
        )

        print(
            "STATE: WAITING_FINAL_SELECTION"
        )

        return 0

    raise RuntimeError(
        "send_design_approval_email.py "
        "cannot send mail from state "
        f"{state!r}. "
        "Expected CONCEPTS_READY "
        "or DESIGN_PREVIEWS_READY."
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
