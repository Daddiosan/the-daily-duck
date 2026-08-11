import os
import json
import smtplib
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


INPUT_FILE = "ai_ranked_news.json"
OUTPUT_FILE = "daily_duck_email.txt"
GATE_FILE = "gate_a_package.json"

MODEL = "gemini-3.6-flash"


# ============================================================
# Load AI-ranked news
# ============================================================

def load_ranked_news():
    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


# ============================================================
# Ask Gemini to prepare the full Gate A editorial package
# ============================================================

def create_gate_a_package(news_data):

    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured."
        )

    top_five = news_data["top_five"]
    recommended_id = news_data["recommended_id"]
    recommended_reason = news_data.get(
        "recommended_reason",
        "",
    )

    prompt = f"""
You are the editorial assistant for The Daily Duck.

The Daily Duck publishes one uplifting, interesting,
accurate news story every day.

You have already ranked today's five best candidates.

Your task now is to prepare the Gate A editorial package
for human approval.

IMPORTANT EDITORIAL RULES:

- Do not invent facts.
- Use only information contained in the supplied candidate data.
- Preserve uncertainty.
- Do not exaggerate.
- Do not turn correlation into causation.
- Keep dates, names and numbers accurate.
- The tone should be warm, intelligent and lightly playful.
- Do not make the writing childish.
- Do not use clickbait.
- The Japanese and English versions must communicate
  the same factual content.
- The X post must accurately represent the article.
- The source URL must remain unchanged.

For the recommended story, create:

1. Japanese headline
2. Japanese article
3. English headline
4. English article
5. Japanese X post
6. English X post
7. Short Japanese recommendation reason
8. A short image concept for later illustration generation

ARTICLE LENGTH:

Japanese article:
approximately 250-450 Japanese characters.

English article:
approximately 120-200 words.

X POSTS:

Keep each X post concise enough for X.
Do not include invented hashtags.
Include the source URL separately in the JSON.

Also prepare short Japanese summaries for ALL FIVE candidates.

Return JSON only.

Required JSON structure:

{{
  "recommended_id": integer,

  "recommended": {{
    "source": "string",
    "source_url": "string",
    "headline_ja": "string",
    "article_ja": "string",
    "headline_en": "string",
    "article_en": "string",
    "x_post_ja": "string",
    "x_post_en": "string",
    "recommendation_ja": "string",
    "image_concept": "string"
  }},

  "top_five": [
    {{
      "rank": integer,
      "id": integer,
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

recommendation reason:
{recommended_reason}

Today's TOP 5 candidate data:

{json.dumps(top_five, ensure_ascii=False, indent=2)}
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL}:generateContent"
        f"?key={api_key}"
    )

    body = {
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
            "responseMimeType": "application/json",
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            response_data = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:

        details = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Gemini API HTTP {error.code}: {details}"
        )

    text = (
        response_data["candidates"][0]
        ["content"]["parts"][0]["text"]
        .strip()
    )

    # Defensive cleanup in case a model wraps JSON
    # in a Markdown code fence.
    if text.startswith("```"):
        text = text.strip("`")

        if text.startswith("json"):
            text = text[4:]

        text = text.strip()

    return json.loads(text)


# ============================================================
# Build human-readable Gate A email
# ============================================================

def build_email_body(package):

    recommended = package["recommended"]
    top_five = package["top_five"]

    lines = []

    lines.append("🦆 THE DAILY DUCK")
    lines.append("GATE A — ARTICLE APPROVAL")
    lines.append("")
    lines.append("=" * 48)
    lines.append("")
    lines.append("【今日のおすすめ】")
    lines.append("")
    lines.append(
        recommended["headline_ja"]
    )
    lines.append("")
    lines.append(
        recommended["recommendation_ja"]
    )
    lines.append("")

    lines.append("■ 日本語原稿")
    lines.append("")
    lines.append(
        recommended["article_ja"]
    )
    lines.append("")

    lines.append("■ English")
    lines.append("")
    lines.append(
        recommended["headline_en"]
    )
    lines.append("")
    lines.append(
        recommended["article_en"]
    )
    lines.append("")

    lines.append("■ X 日本語案")
    lines.append("")
    lines.append(
        recommended["x_post_ja"]
    )
    lines.append("")

    lines.append("■ X English draft")
    lines.append("")
    lines.append(
        recommended["x_post_en"]
    )
    lines.append("")

    lines.append("■ 画像コンセプト")
    lines.append("")
    lines.append(
        recommended["image_concept"]
    )
    lines.append("")

    lines.append("■ Source")
    lines.append("")
    lines.append(
        recommended["source"]
    )
    lines.append(
        recommended["source_url"]
    )
    lines.append("")

    lines.append("=" * 48)
    lines.append("")
    lines.append("【今日の候補 TOP 5】")
    lines.append("")

    for story in top_five:

        lines.append(
            f"{story['rank']}. "
            f"{story['title_ja']}"
        )

        lines.append(
            story["summary_ja"]
        )

        lines.append(
            f"Source: {story['source']}"
        )

        lines.append(
            story["url"]
        )

        lines.append("")

    lines.append("=" * 48)
    lines.append("")
    lines.append("【承認方法】")
    lines.append("")
    lines.append(
        "この記事で進めてよければ、"
        "このメールに次の1語だけ返信してください。"
    )
    lines.append("")
    lines.append("OK")
    lines.append("")
    lines.append(
        "修正したい場合は、"
        "OKとは書かずに修正内容を返信してください。"
    )
    lines.append("")
    lines.append(
        "例：タイトルをもう少し短くして"
    )
    lines.append("")
    lines.append(
        "例：候補3の記事に変更"
    )
    lines.append("")
    lines.append(
        "※ OK以外は承認として扱いません。"
    )
    lines.append("")
    lines.append("QUACK! 🦆")

    return "\n".join(lines)


# ============================================================
# Send Gate A email
# ============================================================

def send_email(body):

    gmail_address = os.environ.get(
        "GMAIL_ADDRESS"
    )

    app_password = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )

    email_to = os.environ.get(
        "EMAIL_TO"
    )

    if not gmail_address:
        raise RuntimeError(
            "GMAIL_ADDRESS is not configured."
        )

    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD is not configured."
        )

    if not email_to:
        raise RuntimeError(
            "EMAIL_TO is not configured."
        )

    app_password = (
        app_password
        .replace(" ", "")
    )

    now_jst = datetime.now(
        ZoneInfo("Asia/Tokyo")
    )

    date_text = now_jst.strftime(
        "%Y/%m/%d"
    )

    subject = (
        "🦆 The Daily Duck — "
        f"記事承認 Gate A ({date_text})"
    )

    message = MIMEMultipart()

    message["From"] = gmail_address
    message["To"] = email_to
    message["Subject"] = subject

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
            [email_to],
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
        "=" * 50
    )

    news_data = load_ranked_news()

    print(
        "Creating Gate A editorial package..."
    )

    package = create_gate_a_package(
        news_data
    )

    # Save machine-readable package
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

    print(
        f"Saved to {GATE_FILE}"
    )

    email_body = build_email_body(
        package
    )

    # Save human-readable email
    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            email_body
        )

    print(
        f"Saved to {OUTPUT_FILE}"
    )

    send_email(
        email_body
    )

    print()
    print(
        "GATE A EMAIL COMPLETE"
    )
    print(
        "=" * 50
    )


if __name__ == "__main__":
    main()
