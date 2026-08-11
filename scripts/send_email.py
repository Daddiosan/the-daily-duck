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
MODEL = "gemini-3.6-flash"


def load_ranked_news():
    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def create_japanese_email(news_data):
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    top_five = news_data["top_five"]
    recommended_id = news_data["recommended_id"]

    prompt = f"""
あなたは The Daily Duck の日本語編集者です。

以下は今日、AIが選んだ「読んだ人が少し幸せになるニュース」
TOP 5です。

これを日本語のメールとして読みやすく編集してください。

ルール：

- 日本語は自然で親しみやすくする
- 大げさにしない
- 原文にない事実を追加しない
- 各記事のタイトルを自然な日本語に翻訳する
- 各記事について2～3文で内容を説明する
- なぜ楽しい・希望がある記事なのかも簡潔に伝える
- 専門用語はできるだけ分かりやすくする
- URLは変更しない
- 1位の推薦記事には「今日のおすすめ」と付ける
- 絵文字は少なめに使う
- Markdownは使わない
- HTMLも使わない

以下のJSONを基に作成してください。

recommended_id:
{recommended_id}

TOP 5:
{json.dumps(top_five, ensure_ascii=False, indent=2)}

次の形式で出力してください。

🦆 THE DAILY DUCK
今日のハッピーニュース TOP 5

【今日のおすすめ】

1. 日本語タイトル
概要
おすすめポイント
URL

2. 日本語タイトル
概要
おすすめポイント
URL

3. 日本語タイトル
概要
おすすめポイント
URL

4. 日本語タイトル
概要
おすすめポイント
URL

5. 日本語タイトル
概要
おすすめポイント
URL

最後に短い一言：
「今日もいい一日を。QUACK! 🦆」

出力はメール本文だけにしてください。
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
            "temperature": 0.3
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

    return (
        response_data["candidates"][0]
        ["content"]["parts"][0]["text"]
        .strip()
    )


def send_email(body):
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    email_to = os.environ.get("EMAIL_TO")

    if not gmail_address:
        raise RuntimeError("GMAIL_ADDRESS is not configured.")

    if not app_password:
        raise RuntimeError("GMAIL_APP_PASSWORD is not configured.")

    if not email_to:
        raise RuntimeError("EMAIL_TO is not configured.")

    # Remove spaces just in case the Google app password
    # was copied in groups of four characters.
    app_password = app_password.replace(" ", "")

    now_jst = datetime.now(
        ZoneInfo("Asia/Tokyo")
    )

    date_text = now_jst.strftime("%Y/%m/%d")

    subject = (
        f"🦆 The Daily Duck — "
        f"今日のTOP 5 ({date_text})"
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

    print("Connecting to Gmail SMTP...")

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

    print("Email sent successfully.")


def main():
    print()
    print("THE DAILY DUCK EMAIL")
    print("=" * 50)

    news_data = load_ranked_news()

    print("Creating Japanese email with Gemini...")

    email_body = create_japanese_email(
        news_data
    )

    print("Japanese email created.")

    # Save a copy for debugging / artifacts.
    with open(
        "daily_duck_email.txt",
        "w",
        encoding="utf-8",
    ) as file:
        file.write(email_body)

    print("Saved to daily_duck_email.txt")

    send_email(email_body)

    print()
    print("THE DAILY DUCK EMAIL COMPLETE")
    print("=" * 50)


if __name__ == "__main__":
    main()
