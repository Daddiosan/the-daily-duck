#!/usr/bin/env python3
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def main() -> int:
    stage = os.getenv("FAILURE_STAGE", "The Daily Duck automation").strip()
    run_url = os.getenv("GITHUB_RUN_URL", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()

    recipients = [
        x.strip()
        for x in required("EMAIL_TO").split(",")
        if x.strip()
    ]

    subject = f"The Daily Duck — Automation stopped — {stage}"
    body = f"""The Daily Duck automation stopped because a workflow step failed.

Stage:
{stage}

Repository:
{repository}

GitHub Actions run:
{run_url}

No later publication step should be assumed to have completed.
Open the Actions run above and check the failed step.
"""

    msg = EmailMessage()
    msg["From"] = required("GMAIL_ADDRESS")
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(
            required("GMAIL_ADDRESS"),
            required("GMAIL_APP_PASSWORD"),
        )
        smtp.send_message(msg)

    print("Failure notification email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
