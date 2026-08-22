#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any


READY_PATH = Path(
    "automation_state/ready_to_publish.json"
)

X_RESULT_PATH = Path(
    "automation_state/x_publish_result.json"
)

NOTIFY_RESULT_PATH = Path(
    "automation_state/publish_complete_notification.json"
)

JST = timezone(
    timedelta(hours=9)
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


def optional_env(
    name: str,
) -> str:
    return os.getenv(
        name,
        "",
    ).strip()


def load_json(
    path: Path,
) -> dict[str, Any]:
    if not path.exists():
        return {}

    data = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    return (
        data
        if isinstance(data, dict)
        else {}
    )


def save_result(
    payload: dict[str, Any],
) -> None:
    NOTIFY_RESULT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    NOTIFY_RESULT_PATH.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def first_text(
    *values: Any,
) -> str:
    for value in values:
        if isinstance(
            value,
            str,
        ) and value.strip():
            return value.strip()

    return ""


def openai_issue_day_cost() -> tuple[
    str,
    float | None,
    str,
]:
    """
    Uses the official OpenAI organization Costs endpoint.

    IMPORTANT:
    - Requires OPENAI_ADMIN_KEY, not a normal project API key.
    - OPENAI_PROJECT_ID is strongly recommended.
    - If the project is dedicated to The Daily Duck, the issue-day
      project cost is a useful Daily Duck daily cost figure.
    - The Costs API currently provides 1-day buckets, so this is not
      a transaction-level/per-run invoice.
    """
    admin_key = optional_env(
        "OPENAI_ADMIN_KEY"
    )

    if not admin_key:
        return (
            "UNAVAILABLE",
            None,
            (
                "OPENAI_ADMIN_KEY is not configured. "
                "Add an OpenAI organization Admin API key "
                "to enable the official Costs API."
            ),
        )

    issue_date = optional_env(
        "ISSUE_DATE"
    )

    if not issue_date:
        issue_date = datetime.now(
            JST
        ).strftime(
            "%Y-%m-%d"
        )

    issue_start_jst = datetime.strptime(
        issue_date,
        "%Y-%m-%d",
    ).replace(
        tzinfo=JST
    )

    issue_end_jst = (
        issue_start_jst
        + timedelta(days=1)
    )

    params: list[
        tuple[str, str]
    ] = [
        (
            "start_time",
            str(
                int(
                    issue_start_jst.timestamp()
                )
            ),
        ),
        (
            "end_time",
            str(
                int(
                    issue_end_jst.timestamp()
                )
            ),
        ),
        (
            "bucket_width",
            "1d",
        ),
        (
            "limit",
            "2",
        ),
    ]

    project_id = optional_env(
        "OPENAI_PROJECT_ID"
    )

    if project_id:
        params.append(
            (
                "project_ids[]",
                project_id,
            )
        )

    url = (
        "https://api.openai.com/v1/organization/costs?"
        + urllib.parse.urlencode(
            params
        )
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization":
                f"Bearer {admin_key}",

            "Content-Type":
                "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            payload = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except Exception as exc:
        return (
            "ERROR",
            None,
            f"OpenAI Costs API failed: {exc}",
        )

    total = 0.0

    for bucket in payload.get(
        "data",
        [],
    ):
        if not isinstance(
            bucket,
            dict,
        ):
            continue

        for item in bucket.get(
            "results",
            [],
        ):
            if not isinstance(
                item,
                dict,
            ):
                continue

            amount = item.get(
                "amount"
            )

            if not isinstance(
                amount,
                dict,
            ):
                continue

            try:
                total += float(
                    amount.get(
                        "value",
                        0,
                    )
                    or 0
                )
            except (
                TypeError,
                ValueError,
            ):
                pass

    scope = (
        "OpenAI project cost for issue day"
        if project_id
        else
        "OpenAI organization cost for issue day"
    )

    return (
        "OK",
        total,
        scope,
    )


def send_email(
    subject: str,
    body: str,
) -> None:
    gmail_address = required_env(
        "GMAIL_ADDRESS"
    )

    recipients = [
        item.strip()
        for item in required_env(
            "EMAIL_TO"
        ).split(",")
        if item.strip()
    ]

    if not recipients:
        raise RuntimeError(
            "EMAIL_TO contains no recipients."
        )

    message = EmailMessage()

    message[
        "From"
    ] = gmail_address

    message[
        "To"
    ] = ", ".join(
        recipients
    )

    message[
        "Subject"
    ] = subject

    message.set_content(
        body
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(
            gmail_address,
            required_env(
                "GMAIL_APP_PASSWORD"
            ),
        )

        smtp.send_message(
            message
        )


def main() -> int:
    ready = load_json(
        READY_PATH
    )

    x_result = load_json(
        X_RESULT_PATH
    )

    issue_date = first_text(
        ready.get(
            "issue_date"
        ),
        optional_env(
            "ISSUE_DATE"
        ),
    )

    if not issue_date:
        raise ValueError(
            "issue_date is missing."
        )

    x_action = first_text(
        x_result.get(
            "action"
        ),
        "UNKNOWN",
    )

    if x_action != "X_POSTED":
        raise RuntimeError(
            "Completion email may only be sent "
            f"after X_POSTED; got {x_action!r}."
        )

    cost_status, cost_value, cost_note = (
        openai_issue_day_cost()
    )

    if cost_value is None:
        openai_cost_text = (
            f"Unavailable ({cost_note})"
        )
    else:
        openai_cost_text = (
            f"${cost_value:.6f} USD"
        )

    # There is currently no documented supported endpoint in the
    # OpenAI public API for returning the user's prepaid credit balance.
    # Do not use legacy/private dashboard endpoints.
    openai_balance_text = (
        "Unavailable via supported public API; "
        "check OpenAI Billing / Usage dashboard."
    )

    # Gemini billing/credit data can be delayed and the AI Studio
    # prepay balance is managed in AI Studio. We intentionally do not
    # claim an exact real-time value here.
    gemini_cost_text = (
        "Not included in this automated total. "
        "Gemini billing data can be delayed; "
        "check Google AI Studio / Cloud Billing."
    )

    gemini_balance_text = (
        "Check Google AI Studio Billing "
        "(prepay balance, if applicable)."
    )

    selected_title = first_text(
        ready.get(
            "selected_title"
        )
    )

    subject = (
        "The Daily Duck — Publish Complete — "
        f"{issue_date}"
    )

    body = f"""
The Daily Duck — Publish Complete

Issue:
{issue_date}

Title:
{selected_title or "(not available)"}

STATUS
----------------------------------------
Website: Published
X: Published

API COST
----------------------------------------
OpenAI:
{openai_cost_text}

Cost scope:
{cost_note}

Gemini:
{gemini_cost_text}

IMPORTANT:
The OpenAI Costs endpoint currently reports daily cost buckets.
For a clean Daily Duck cost figure, OPENAI_PROJECT_ID should point
to a project used only by The Daily Duck.

CREDIT / BILLING BALANCE
----------------------------------------
OpenAI:
{openai_balance_text}

Gemini:
{gemini_balance_text}

Publication completed successfully.
""".strip()

    send_email(
        subject,
        body,
    )

    payload = {
        "action":
            "PUBLISH_COMPLETE_EMAIL_SENT",

        "issue_date":
            issue_date,

        "sent_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "website":
            "PUBLISHED",

        "x":
            "X_POSTED",

        "openai_cost_status":
            cost_status,

        "openai_issue_day_cost_usd":
            cost_value,

        "openai_cost_note":
            cost_note,

        "openai_credit_balance":
            "UNAVAILABLE_VIA_SUPPORTED_PUBLIC_API",

        "gemini_cost":
            "NOT_REALTIME_AUTOMATED",

        "gemini_credit_balance":
            "CHECK_AI_STUDIO",
    }

    save_result(
        payload
    )

    print(
        "Publish completion email sent."
    )

    print(
        f"Issue: {issue_date}"
    )

    print(
        f"OpenAI cost: {openai_cost_text}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
