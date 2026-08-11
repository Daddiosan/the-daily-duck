import os
import json
import smtplib
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ============================================================
# The Daily Duck — Gate A Email
# ============================================================

INPUT_FILE = "ai_ranked_news.json"
GATE_FILE = "gate_a_package.json"
EMAIL_FILE = "daily_duck_email.txt"

MODEL = "gemini-3.6-flash"


# ============================================================
# Load ranked news
# ============================================================

def load_ranked_news():
    if not os.path.exists(INPUT_FILE):
        raise RuntimeError(
            f"{INPUT_FILE} was not found."
        )

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


# ============================================================
# Clean JSON returned from Gemini
# ============================================================

def clean_json_response(text):
    text = text.strip()

    if text.startswith("```json"):
        text = text[7:]

    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    return text.strip()


# ============================================================
# Generate Gate A editorial package
# ============================================================

def create_gate_a_package(news_data):
    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured."
        )

    top_five = news_data.get(
        "top_five",
        []
    )

    recommended_id = news_data.get(
        "recommended_id"
    )

    recommended_reason = news_data.get(
        "recommended_reason",
        ""
    )

    if not top_five:
        raise RuntimeError(
            "No TOP 5 stories were found."
        )

    prompt = f"""
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes one uplifting, interesting,
accurate and enjoyable news story every day.

The main goal is to make readers feel a little happier,
more hopeful, amused, inspired or warmly curious.

There is no preferred subject category.

Prepare the official Gate A editorial package for
human approval.

IMPORTANT RULES:

- Never invent facts.
- Use only information in the supplied candidate data.
- Preserve uncertainty.
- Do not exaggerate.
- Do not turn correlation into causation.
- Keep names, dates and numbers accurate.
- Do not use clickbait.
- Write clearly for ordinary readers.
- Keep Japanese and English factually consistent.
- Keep the source URL unchanged.
- The tone should be intelligent, warm and lightly playful.
- Do not make the writing childish.

For the recommended story create:

1. Japanese headline
2. Japanese article
3. English headline
4. English article
5. Japanese Duck commentary
6. English Duck commentary
7. Japanese X post
8. English X post
9. Short Japanese recommendation reason
10. Image concept

Japanese article:
approximately 250-450 Japanese characters.

English article:
approximately 120-200 words.

The X post should be concise.

For all five candidates create:
- rank
- id
- original title
- Japanese title
- short Japanese summary
- source
- URL

Return JSON only.

Required structure:

{{
  "recommended_id": 1,

  "recommended": {{
    "source": "string",
    "source_url": "string",

    "headline_ja": "string",
    "article_ja": "string",

    "headline_en": "string",
    "article_en": "string",

    "duck_ja": "string",
    "duck_en": "string",

    "x_post_ja": "string",
    "x_post_en": "string",

    "recommendation_ja": "string",
    "image_concept": "string"
  }},

  "top_five": [
    {{
      "rank": 1,
      "id": 1,
      "title_original": "string",
      "title_ja": "string",
      "summary_ja": "string",
      "source": "string",
      "url": "string"
    }}
  ]
}}

Previously selected recommendation:

recommended_id:
{recommended_id}

recommended_reason:
{recommended_reason}

TOP FIVE DATA:

{json.dumps(
    top_five,
    ensure_ascii=False,
    indent=2
)}
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL}:generateContent"
        f"?key={api_key}"
    )

    request_body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            request_body
        ).encode("utf-8"),
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            response_data = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Gemini API HTTP "
            f"{error.code}: {details}"
        )

    try:
        text = (
            response_data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

    except (
        KeyError,
        IndexError,
        TypeError,
    ) as error:

        raise RuntimeError(
            "Unexpected Gemini response format."
        ) from error

    text = clean_json_response(
        text
    )

    try:
        package = json.loads(
            text
        )

    except json.JSONDecodeError as error:
        print(
            "Gemini returned invalid JSON."
        )

        print(
            text[:2000]
        )

        raise RuntimeError(
            "Could not parse Gate A JSON."
        ) from error

    return package


# ============================================================
# Validate Gate A package
# ============================================================

def validate_gate_a_package(package):
    required_top = [
        "recommended_id",
        "recommended",
        "top_five",
    ]

    for key in required_top:
        if key not in package:
            raise RuntimeError(
                f"Gate A package missing: {key}"
            )

    recommended = package[
        "recommended"
    ]

    required_recommended = [
        "source",
        "source_url",
        "headline_ja",
        "article_ja",
        "headline_en",
        "article_en",
        "duck_ja",
        "duck_en",
        "x_post_ja",
        "x_post_en",
        "recommendation_ja",
        "image_concept",
    ]

    for key in required_recommended:
        if key not in recommended:
            raise RuntimeError(
                "Gate A recommended story "
                f"missing: {key}"
            )

    if len(package["top_five"]) != 5:
        raise RuntimeError(
            "Gate A TOP 5 does not contain "
            "exactly five stories."
        )


# ============================================================
# Save Gate A JSON
# ============================================================

def save_gate_a_package(package):
    with open(
        GATE_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            package,
            file,
            ensure_ascii=False,
            indent=2,
        )

    if not os.path.exists(
        GATE_FILE
    ):
        raise RuntimeError(
            "gate_a_package.json "
            "was not created."
        )

    print(
        f"Saved: {GATE_FILE}"
    )


# ============================================================
# Build human-readable approval email
# ============================================================

def build_email_body(package):
    recommended = package[
        "recommended"
    ]

    top_five = package[
        "top_five"
    ]

    lines = []

    lines.append(
        "🦆 THE DAILY DUCK"
    )

    lines.append(
        "GATE A — ARTICLE APPROVAL"
    )

    lines.append("")
    lines.append("=" * 50)
    lines.append("")
    lines.append("【今日のおすすめ】")
    lines.append("")

    lines.append(
        recommended[
            "headline_ja"
        ]
    )

    lines.append("")

    lines.append(
        recommended[
            "recommendation_ja"
        ]
    )

    lines.append("")
    lines.append("■ 日本語原稿")
    lines.append("")

    lines.append(
        recommended[
            "article_ja"
        ]
    )

    lines.append("")
    lines.append("■ Duck")
    lines.append("")

    lines.append(
        recommended[
            "duck_ja"
        ]
    )

    lines.append("")
    lines.append("■ English")
    lines.append("")

    lines.append(
        recommended[
            "headline_en"
        ]
    )

    lines.append("")

    lines.append(
        recommended[
            "article_en"
        ]
    )

    lines.append("")
    lines.append("■ Duck — English")
    lines.append("")

    lines.append(
        recommended[
            "duck_en"
        ]
    )

    lines.append("")
    lines.append("■ X 日本語案")
    lines.append("")

    lines.append(
        recommended[
            "x_post_ja"
        ]
    )

    lines.append("")
    lines.append("■ X English draft")
    lines.append("")

    lines.append(
        recommended[
            "x_post_en"
        ]
    )

    lines.append("")
    lines.append("■ 画像コンセプト")
    lines.append("")

    lines.append(
        recommended[
            "image_concept"
        ]
    )

    lines.append("")
    lines.append("■ Source")
    lines.append("")

    lines.append(
        recommended[
            "source"
        ]
    )

    lines.append(
        recommended[
            "source_url"
        ]
    )

    lines.append("")
    lines.append("=" * 50)
    lines.append("")
    lines.append("【今日の候補 TOP 5】")
    lines.append("")

    for story in top_five:
        lines.append(
            f"{story['rank']}. "
            f"{story['title_ja']}"
        )

        lines.append(
            story[
                "summary_ja"
            ]
        )

        lines.append(
            f"Source: "
            f"{story['source']}"
        )

        lines.append(
            story[
                "url"
            ]
        )

        lines.append("")

    lines.append("=" * 50)
    lines.append("")
    lines.append("【承認方法】")
    lines.append("")

    lines.append(
        "この記事で進めてよければ、"
    )

    lines.append(
        "このメールに次の1語だけ"
        "返信してください。"
    )

    lines.append("")
    lines.append("OK")
    lines.append("")

    lines.append(
        "修正したい場合は、"
        "OKとは書かずに"
        "修正内容を返信してください。"
    )

    lines.append("")

    lines.append(
        "例：候補3の記事に変更"
    )

    lines.append(
        "例：日本語タイトルを"
        "もう少し短くして"
    )

    lines.append("")

    lines.append(
        "※ OK以外は承認として"
        "扱いません。"
    )

    lines.append("")
    lines.append("QUACK! 🦆")

    return "\n".join(
        lines
    )


# ============================================================
# Save human-readable email
# ============================================================

def save_email_body(body):
    with open(
        EMAIL_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            body
        )

    if not os.path.exists(
        EMAIL_FILE
    ):
        raise RuntimeError(
            "daily_duck_email.txt "
            "was not created."
        )

    print(
        f"Saved: {EMAIL_FILE}"
    )


# ============================================================
# Recipients
# EMAIL_TO supports comma-separated addresses.
# ============================================================

def get_recipients():
    email_to = os.environ.get(
        "EMAIL_TO"
    )

    if not email_to:
        raise RuntimeError(
            "EMAIL_TO is not configured."
        )

    recipients = [
        address.strip()
        for address
        in email_to.split(",")
        if address.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "No valid email recipients."
        )

    return recipients


# ============================================================
# Send approval email
# ============================================================

def send_email(body):
    gmail_address = os.environ.get(
        "GMAIL_ADDRESS"
    )

    app_password = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )

    if not gmail_address:
        raise RuntimeError(
            "GMAIL_ADDRESS is not configured."
        )

    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD "
            "is not configured."
        )

    recipients = get_recipients()

    app_password = (
        app_password
        .replace(" ", "")
    )

    now_jst = datetime.now(
        ZoneInfo("Asia/Tokyo")
    )

    date_text = now_jst.strftime(
        "%Y-%m-%d"
    )

    subject = (
        "The Daily Duck — "
        "Story Approval — "
        f"{date_text}"
    )

    message = MIMEMultipart()

    message["From"] = (
        gmail_address
    )

    message["To"] = (
        ", ".join(
            recipients
        )
    )

    message[
        "Subject"
    ] = subject

    message.attach(
        MIMEText(
            body,
            "plain",
            "utf-8",
        )
    )

    print(
        "Connecting to Gmail SMTP..."
    )

    print(
        f"Recipients: "
        f"{len(recipients)}"
    )

    for recipient in recipients:
        print(
            f" -> {recipient}"
        )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as server:

        server.login(
            gmail_address,
            app_password,
        )

        server.sendmail(
            gmail_address,
            recipients,
            message.as_string(),
        )

    print(
        "Gate A email sent successfully."
    )


# ============================================================
# Main
# ============================================================

def main():
    print()
    print(
        "THE DAILY DUCK — GATE A"
    )
    print(
        "=" * 55
    )

    news_data = (
        load_ranked_news()
    )

    print(
        "Creating Gate A package..."
    )

    package = (
        create_gate_a_package(
            news_data
        )
    )

    print(
        "Validating Gate A package..."
    )

    validate_gate_a_package(
        package
    )

    # IMPORTANT:
    # Save the machine-readable state
    # BEFORE attempting email delivery.
    save_gate_a_package(
        package
    )

    email_body = (
        build_email_body(
            package
        )
    )

    save_email_body(
        email_body
    )

    print()
    print(
        "Generated files:"
    )
    print(
        f" - {GATE_FILE}"
    )
    print(
        f" - {EMAIL_FILE}"
    )

    print()

    send_email(
        email_body
    )

    print()
    print(
        "GATE A COMPLETE"
    )
    print(
        "=" * 55
    )


if __name__ == "__main__":
    main()
