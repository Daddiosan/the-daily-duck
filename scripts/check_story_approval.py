import os
import json
import imaplib
import email
import re
from pathlib import Path
from datetime import datetime, timezone
from email.header import decode_header


GMAIL_IMAP_SERVER = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

GATE_FILE = "gate_a_package.json"

STATE_DIR = Path("automation_state")
APPROVED_FILE = STATE_DIR / "approved_story.json"


def decode_text(value):
    if not value:
        return ""

    parts = decode_header(value)
    result = ""

    for part, encoding in parts:
        if isinstance(part, bytes):
            result += part.decode(
                encoding or "utf-8",
                errors="replace",
            )
        else:
            result += part

    return result


def get_plain_text(message):
    """
    Extract plain-text body from an email.
    """

    if message.is_multipart():
        texts = []

        for part in message.walk():
            content_type = part.get_content_type()

            disposition = str(
                part.get(
                    "Content-Disposition",
                    ""
                )
            ).lower()

            if (
                content_type == "text/plain"
                and "attachment" not in disposition
            ):
                payload = part.get_payload(
                    decode=True
                )

                if payload:
                    charset = (
                        part.get_content_charset()
                        or "utf-8"
                    )

                    texts.append(
                        payload.decode(
                            charset,
                            errors="replace",
                        )
                    )

        return "\n".join(texts)

    payload = message.get_payload(
        decode=True
    )

    if not payload:
        return ""

    charset = (
        message.get_content_charset()
        or "utf-8"
    )

    return payload.decode(
        charset,
        errors="replace",
    )


def strip_html_tags(text):
    """
    Defensive cleanup if HTML-like fragments appear
    inside plain-text content.
    """

    return re.sub(
        r"<[^>]+>",
        " ",
        text or "",
    )


def extract_new_reply(text):
    """
    Keep only the newly written reply section.

    Everything after common reply separators or
    quoted original-message markers is discarded.
    """

    if not text:
        return ""

    text = strip_html_tags(text)

    text = text.replace(
        "\r\n",
        "\n",
    )

    text = text.replace(
        "\r",
        "\n",
    )

    lines = text.split("\n")

    cleaned = []

    separator_patterns = [
        r"^On .+ wrote:$",
        r"^On .+wrote:$",
        r"^.+ wrote:$",
        r"^From:",
        r"^Sent:",
        r"^To:",
        r"^Subject:",
        r"^Date:",
        r"^差出人:",
        r"^送信日時:",
        r"^宛先:",
        r"^件名:",
        r"^日時:",
        r"^-{2,}\s*Original Message\s*-{2,}$",
        r"^-{2,}\s*元のメッセージ\s*-{2,}$",
        r"^_{5,}$",
    ]

    for line in lines:
        stripped = line.strip()

        # Gmail / mail-client quoted content
        if stripped.startswith(">"):
            break

        matched_separator = False

        for pattern in separator_patterns:
            if re.match(
                pattern,
                stripped,
                flags=re.IGNORECASE,
            ):
                matched_separator = True
                break

        if matched_separator:
            break

        cleaned.append(line)

    reply = "\n".join(cleaned)

    # Remove common signature separator.
    if "\n-- \n" in reply:
        reply = reply.split(
            "\n-- \n",
            1,
        )[0]

    return reply.strip()


def normalize_reply(text):
    """
    Normalize only the user's newly typed reply.
    """

    reply = extract_new_reply(
        text
    )

    # Remove zero-width and non-breaking spaces.
    reply = reply.replace(
        "\u200b",
        "",
    )

    reply = reply.replace(
        "\ufeff",
        "",
    )

    reply = reply.replace(
        "\xa0",
        " ",
    )

    # Collapse all whitespace.
    reply = re.sub(
        r"\s+",
        " ",
        reply,
    )

    return reply.strip()


def is_exact_approval(text):
    """
    Approval is ONLY the exact word OK.

    Accepted:
    OK
    ok
    Ok

    Not accepted:
    OK!
    OKです
    Yes
    Proceed
    """
    normalized = normalize_reply(
        text
    )

    return normalized.upper() == "OK"


def load_gate_package():
    if not os.path.exists(
        GATE_FILE
    ):
        raise RuntimeError(
            f"{GATE_FILE} was not found."
        )

    with open(
        GATE_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def sender_email_address(sender_text):
    """
    Extract email address from From header.
    """

    match = re.search(
        r"<([^>]+)>",
        sender_text,
    )

    if match:
        return match.group(1).strip().lower()

    return sender_text.strip().lower()


def save_approved_story(
    package,
    message_id,
    sender,
    subject,
):
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    approved = {
        "state": "APPROVED_STORY",

        "approved_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "approval_channel":
            "email",

        "approval_message_id":
            message_id,

        "approval_sender":
            sender,

        "approval_subject":
            subject,

        "recommended_id":
            package.get(
                "recommended_id"
            ),

        "story":
            package.get(
                "recommended"
            ),

        "top_five":
            package.get(
                "top_five",
                [],
            ),
    }

    with open(
        APPROVED_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            approved,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"Saved approved story to "
        f"{APPROVED_FILE}"
    )


def main():
    print()
    print(
        "THE DAILY DUCK — "
        "GATE A APPROVAL CHECK"
    )

    print(
        "=" * 55
    )

    gmail_address = os.environ.get(
        "GMAIL_ADDRESS"
    )

    app_password = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )

    if not gmail_address:
        raise RuntimeError(
            "GMAIL_ADDRESS "
            "is not configured."
        )

    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD "
            "is not configured."
        )

    gmail_address = (
        gmail_address
        .strip()
        .lower()
    )

    app_password = (
        app_password
        .replace(" ", "")
    )

    package = load_gate_package()

    print(
        "Connecting to Gmail IMAP..."
    )

    mail = imaplib.IMAP4_SSL(
        GMAIL_IMAP_SERVER,
        GMAIL_IMAP_PORT,
    )

    try:
        mail.login(
            gmail_address,
            app_password,
        )

        print(
            "Gmail login successful."
        )

        status, _ = mail.select(
            "INBOX"
        )

        if status != "OK":
            raise RuntimeError(
                "Could not open Gmail INBOX."
            )

        # Search messages containing
        # The Daily Duck in subject.
        status, data = mail.search(
            None,
            'SUBJECT',
            '"The Daily Duck"',
        )

        if status != "OK":
            raise RuntimeError(
                "Gmail search failed."
            )

        message_ids = (
            data[0].split()
            if data and data[0]
            else []
        )

        print(
            f"Matching messages found: "
            f"{len(message_ids)}"
        )

        if not message_ids:
            print(
                "No Gate A replies found."
            )
            return

        # Check newest messages first.
        recent_ids = (
            message_ids[-50:]
        )

        for raw_id in reversed(
            recent_ids
        ):
            status, message_data = (
                mail.fetch(
                    raw_id,
                    "(RFC822)",
                )
            )

            if status != "OK":
                continue

            if not message_data:
                continue

            raw_email = None

            for item in message_data:
                if (
                    isinstance(item, tuple)
                    and len(item) >= 2
                ):
                    raw_email = item[1]
                    break

            if not raw_email:
                continue

            message = (
                email.message_from_bytes(
                    raw_email
                )
            )

            subject = decode_text(
                message.get(
                    "Subject",
                    ""
                )
            )

            sender = decode_text(
                message.get(
                    "From",
                    ""
                )
            )

            message_id = (
                message.get(
                    "Message-ID",
                    ""
                )
            )

            # Only Gate A thread.
            subject_lower = (
                subject.lower()
            )

            if (
                "the daily duck"
                not in subject_lower
            ):
                continue

            if (
                "story approval"
                not in subject_lower
                and "gate a"
                not in subject_lower
            ):
                continue

            sender_address = (
                sender_email_address(
                    sender
                )
            )

            # Ignore our own outgoing mail.
            if (
                sender_address
                == gmail_address
            ):
                continue

            body = get_plain_text(
                message
            )

            normalized = normalize_reply(
                body
            )

            print()
            print(
                f"Checking reply from: "
                f"{sender_address}"
            )

            print(
                f"Subject: {subject}"
            )

            print(
                "New reply normalized as:"
            )

            # Safe debug output:
            # only normalized short reply.
            print(
                repr(
                    normalized[:200]
                )
            )

            if is_exact_approval(
                body
            ):
                print()
                print(
                    "EXACT OK APPROVAL FOUND."
                )

                save_approved_story(
                    package,
                    message_id,
                    sender_address,
                    subject,
                )

                print()
                print(
                    "STATE: APPROVED_STORY"
                )

                return

            print(
                "Not an exact OK approval."
            )

        print()
        print(
            "No exact OK approval found."
        )

    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
