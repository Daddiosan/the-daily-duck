#!/usr/bin/env python3
from __future__ import annotations

import json
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
    audit_details = os.getenv("AUDIT_DETAILS", "").strip()
    alerts_file = os.getenv("AUTOMATION_ALERTS_FILE", "").strip()
    alerts = []
    if alerts_file:
        with open(alerts_file, encoding="utf-8") as handle:
            alerts = json.load(handle).get("alerts", [])

    recipients = [x.strip() for x in required("EMAIL_TO").split(",") if x.strip()]
    subject = f"The Daily Duck - Automation alert - {stage}"
    body = f"""The Daily Duck automation monitor detected a problem.

Stage:
{stage}

Repository:
{repository}

"""
    if alerts:
        body += "Detected downstream failure(s):\n\n"
        fields = (
            "failure_stage", "issue_date", "workflow_name", "run_id",
            "run_url", "run_status", "attempt", "detected_at",
            "suggested_action",
        )
        for alert in alerts:
            for field in fields:
                body += f"{field.upper()}: {alert.get(field, '')}\n"
            body += "\n"
    elif audit_details:
        body += f"Detected problem:\n\n{audit_details}\n\n"

    body += f"""GitHub Actions audit run:
{run_url}

No later publication step should be assumed to have completed.
Please inspect the failed workflow without bypassing approval gates.
"""
    msg = EmailMessage()
    msg["From"] = required("GMAIL_ADDRESS")
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(required("GMAIL_ADDRESS"), required("GMAIL_APP_PASSWORD"))
        smtp.send_message(msg)
    print("Automation alert email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
