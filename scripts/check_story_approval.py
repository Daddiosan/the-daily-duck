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
    if message.is_multipart():
        texts = []

        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(
                part.get("Content-Disposition", "")
            )

            if (
                content_type == "text/plain"
                and "attachment" not in disposition.lower()
            ):
                payload = part.get_payload(decode=True)

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

    payload = message.get_payload(decode=True)

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


def extract_new_reply(text):
    """
    Try to remove quoted copies of the original email.

    We only want the text newly written by the user.
    """

    text = text.replace("\r\n", "\n")

    cleaned_lines = []

    for line in text.split("\n"):
        stripped = line.strip()

        # Standard quoted email lines
        if stripped.startswith(">"):
            break

        # Common Gmail / mail-client reply separators
        if re.match(
            r"^On .+ wrote:$",
            stripped,
            flags=re.IGNORECASE,
        ):
            break

        if stripped.startswith("-----Original Message-----"):
            break

        if stripped.startswith("差出人:"):
            break

        if stripped.startswith("From:"):
            break

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def normalize_reply(text):
    text = extract_new_reply(text)

    # Remove surrounding whitespace.
    text = text.strip()

    # Collapse whitespace/newlines.
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def is_exact_approval(text):
    """
    Gate A approval rule:

    Only the exact word OK is approval.
    """

    normalized = normalize_reply(text)

    return normalized.upper() == "OK"


def load_gate_package():
    if not os.path.exists(GATE_FILE):
        raise RuntimeError(
            f"{GATE_FILE} was not found."
        )

    with open(
        GATE_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


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
        "approved_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "approval_channel": "email",
        "approval_message_id": message_id,
        "approval_sender": sender,
        "approval_subject": subject,
        "recommended_id": package.get(
            "recommended_id"
        ),
        "story": package.get(
            "recommended"
        ),
        "top_five": package.get(
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
        f"Saved approved story to {APPROVED_FILE}"
    )


def main():
    print()
    print("THE DAILY DUCK — GATE A APPROVAL CHECK")
    print("=" * 55)

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
            "GMAIL_APP_PASSWORD is not configured."
        )

    app_password = app_password.replace(
        " ",
        "",
    )

    package = load_gate_package()

    print("Connecting to Gmail IMAP...")

    mail = imaplib.IMAP4_SSL(
        GMAIL_IMAP_SERVER,
        GMAIL_IMAP_PORT,
    )

    try:
        mail.login(
            gmail_address,
            app_password,
        )

        print("Gmail login successful.")

        status, _ = mail.select("INBOX")

        if status != "OK":
            raise RuntimeError(
                "Could not open Gmail INBOX."
            )

        # Search recent messages whose subject contains
        # The Daily Duck.
        status, data = mail.search(
            None,
            'SUBJECT',
            '"The Daily Duck"',
        )

        if status != "OK":
            raise RuntimeError(
                "Gmail search failed."
            )

        message_ids = data[0].split()

        print(
            f"Matching messages found: "
            f"{len(message_ids)}"
        )

        if not message_ids:
            print("No Gate A replies found.")
            return

        # Check newest messages first.
        for raw_id in reversed(
            message_ids[-30:]
        ):
            status, message_data = mail.fetch(
                raw_id,
                "(RFC822)",
            )

            if status != "OK":
                continue

            raw_email = message_data[0][1]

            message = email.message_from_bytes(
                raw_email
            )

            subject = decode_text(
                message.get("Subject", "")
            )

            sender = decode_text(
                message.get("From", "")
            )

            message_id = (
                message.get("Message-ID", "")
            )

            # Only Gate A mail threads.
            if "gate a" not in subject.lower():
                continue

            # Never approve our own outgoing email.
            if gmail_address.lower() in sender.lower():
                continue

            body = get_plain_text(
                message
            )

            reply = normalize_reply(
                body
            )

            print()
            print(
                f"Checking reply from: {sender}"
            )
            print(
                f"Subject: {subject}"
            )

            # Do not print the full private email body.
            print(
                f"Normalized reply length: "
                f"{len(reply)}"
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
                    sender,
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
