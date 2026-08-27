#!/usr/bin/env python3
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def required(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


def main() -> int:
    stage = os.getenv(
        "FAILURE_STAGE",
        "The Daily Duck automation",
    ).strip()

    run_url = os.getenv(
        "GITHUB_RUN_URL",
        "",
    ).strip()

    repository = os.getenv(
        "GITHUB_REPOSITORY",
        "",
    ).strip()

    audit_details = os.getenv(
        "AUDIT_DETAILS",
        "",
    ).strip()

    recipients = [
        x.strip()
        for x in required("EMAIL_TO").split(",")
        if x.strip()
    ]

    subject = (
        "The Daily Duck — Automation alert — "
        f"{stage}"
    )

    body = f"""The Daily Duck automation monitor detected a problem.

Stage:
{stage}

Repository:
{repository}

"""

    if audit_details:
        body += f"""Detected problem:

{audit_details}

"""

    body += f"""GitHub Actions run:
{run_url}

No later publication step should be assumed to have completed.

Please check the failed or missing scheduled workflow.
"""

    msg = EmailMessage()

    msg["From"] = required(
        "GMAIL_ADDRESS"
    )

    msg["To"] = ", ".join(
        recipients
    )

    msg["Subject"] = subject

    msg.set_content(
        body
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as smtp:

        smtp.login(
            required("GMAIL_ADDRESS"),
            required("GMAIL_APP_PASSWORD"),
        )

        smtp.send_message(
            msg
        )

    print(
        "Automation alert email sent."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
