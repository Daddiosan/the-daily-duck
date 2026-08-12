#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

CONCEPTS_PATH = Path("automation_state/image_concepts.json")
EMAIL_PREVIEW_PATH = Path("image_concept_email.txt")
SUBJECT_PREFIX = "The Daily Duck — Image Concept Selection"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def validate_package(data: dict[str, Any]) -> None:
    state = str(data.get("state", "")).strip().upper()
    if state != "IMAGE_CONCEPT_REVIEW":
        raise ValueError(
            f"Expected IMAGE_CONCEPT_REVIEW state, got {state!r}."
        )

    concepts = data.get("concepts")
    if not isinstance(concepts, list) or len(concepts) != 5:
        raise ValueError("Exactly five image concepts are required.")

    for i, concept in enumerate(concepts, start=1):
        if not isinstance(concept, dict):
            raise ValueError(f"Concept {i} must be an object.")
        if int(concept.get("number", 0)) != i:
            raise ValueError(f"Concept numbering must be exactly 1-5; problem at {i}.")


def story_title(data: dict[str, Any]) -> str:
    story = data.get("story")
    if not isinstance(story, dict):
        return "Approved Daily Duck Story"

    for key in ("title_ja", "title", "headline_ja", "headline"):
        value = story.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return "Approved Daily Duck Story"


def build_body(data: dict[str, Any]) -> str:
    concepts = data["concepts"]
    lines: list[str] = []

    lines.extend([
        "THE DAILY DUCK — 画像コンセプト選択",
        "",
        "記事の承認が完了しました。",
        "承認済みの記事について、画像コンセプトを5案作成しました。",
        "",
        f"承認済み記事: {story_title(data)}",
        "",
        "以下の5案から、最終画像に使用するコンセプトを1つ選んでください。",
        "",
        "==================================================",
        "画像コンセプト 1〜5",
        "==================================================",
        "",
    ])

    for concept in concepts:
        number = concept["number"]
        lines.extend([
            f"【{number}】{concept.get('title_ja', '').strip()}",
            "",
            f"コンセプト:",
            concept.get("concept_ja", "").strip(),
            "",
            f"構図:",
            concept.get("composition_ja", "").strip(),
            "",
            f"English: {concept.get('title_en', '').strip()}",
            concept.get("concept_en", "").strip(),
            "",
            "--------------------------------------------------",
            "",
        ])

    lines.extend([
        "選択方法",
        "",
        "このメールに、使用したいコンセプトの番号だけを返信してください。",
        "",
        "1",
        "2",
        "3",
        "4",
        "5",
        "",
        "例:",
        "3",
        "",
        "IMPORTANT:",
        "- 返信は 1〜5 の数字1文字だけにしてください。",
        "- それ以外の返信では次の工程へ進みません。",
        "- 選択されたコンセプトから最終画像を1枚生成します。",
        "- 選択後にさらに5枚の画像候補を作る工程はありません。",
        "- 最終画像が確定するまでWebサイト/Xへ公開しません。",
        "",
        "The Daily Duck",
        "One day. One story. One duck. 🐤",
    ])

    return "\n".join(lines).strip() + "\n"


def parse_recipients(raw: str) -> list[str]:
    recipients = [x.strip() for x in raw.split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO contains no valid recipients.")
    return recipients


def main() -> None:
    data = load_json(CONCEPTS_PATH)
    validate_package(data)

    gmail_address = required_env("GMAIL_ADDRESS")
    gmail_app_password = required_env("GMAIL_APP_PASSWORD")
    recipients = parse_recipients(required_env("EMAIL_TO"))

    body = build_body(data)
    EMAIL_PREVIEW_PATH.write_text(body, encoding="utf-8")

    subject = f"{SUBJECT_PREFIX} — {story_title(data)}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(msg)

    print("Image concept selection email sent.")
    print(f"Recipients: {len(recipients)}")
    print("Concept count: 5")
    print("Valid replies: 1 / 2 / 3 / 4 / 5")
    print(f"Preview saved: {EMAIL_PREVIEW_PATH}")


if __name__ == "__main__":
    main()
