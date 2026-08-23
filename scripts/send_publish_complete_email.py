#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
import urllib.error
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
        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return ""


def openai_cost_range(
    start_jst: datetime,
    end_jst: datetime,
) -> tuple[
    str,
    float | None,
    str,
]:
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

    project_id = optional_env(
        "OPENAI_PROJECT_ID"
    )

    params: list[
        tuple[str, str]
    ] = [
        (
            "start_time",
            str(
                int(
                    start_jst.timestamp()
                )
            ),
        ),
        (
            "end_time",
            str(
                int(
                    end_jst.timestamp()
                )
            ),
        ),
        (
            "bucket_width",
            "1d",
        ),
        (
            "limit",
            "31",
        ),
    ]

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

    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            error_body = ""

        return (
            "ERROR",
            None,
            (
                f"OpenAI Costs API HTTP {exc.code}. "
                f"{error_body}"
            ).strip(),
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
        "OpenAI project cost"
        if project_id
        else
        "OpenAI organization cost"
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

    gmail_app_password = (
        required_env(
            "GMAIL_APP_PASSWORD"
        )
        .replace(
            " ",
            "",
        )
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

    print(
        "Connecting to Gmail SMTP..."
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:

        print(
            "Gmail SMTP connected."
        )

        smtp.login(
            gmail_address,
            gmail_app_password,
        )

        print(
            "Gmail authentication succeeded."
        )

        refused = smtp.send_message(
            message
        )

        if refused:
            raise RuntimeError(
                f"Gmail refused recipients: {refused}"
            )

        print(
            "Gmail accepted the message for delivery."
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

    month_start_jst = (
        issue_start_jst.replace(
            day=1
        )
    )

    (
        daily_status,
        daily_cost,
        daily_note,
    ) = openai_cost_range(
        issue_start_jst,
        issue_end_jst,
    )

    if daily_cost is None:
        openai_daily_text = (
            f"Unavailable ({daily_note})"
        )
    else:
        openai_daily_text = (
            f"${daily_cost:.6f} USD"
        )

    (
        monthly_status,
        monthly_cost,
        monthly_note,
    ) = openai_cost_range(
        month_start_jst,
        issue_end_jst,
    )

    if monthly_cost is None:
        openai_monthly_text = (
            f"Unavailable ({monthly_note})"
        )
    else:
        openai_monthly_text = (
            f"${monthly_cost:.6f} USD"
        )

    budget_raw = optional_env(
        "OPENAI_MONTHLY_BUDGET_USD"
    )

    monthly_budget: float | None = None

    if budget_raw:
        try:
            monthly_budget = float(
                budget_raw
            )
        except ValueError:
            monthly_budget = None

    budget_percent: float | None = None
    remaining_budget: float | None = None

    if (
        monthly_budget is not None
        and monthly_budget > 0
        and monthly_cost is not None
    ):
        budget_percent = (
            monthly_cost
            / monthly_budget
            * 100
        )

        remaining_budget = (
            monthly_budget
            - monthly_cost
        )

        budget_text = (
            f"${monthly_budget:.2f} USD"
        )

        budget_usage_text = (
            f"{budget_percent:.1f}%"
        )

        if remaining_budget >= 0:
            remaining_text = (
                f"${remaining_budget:.2f} USD"
            )
        else:
            remaining_text = (
                f"-${abs(remaining_budget):.2f} USD"
            )

        if budget_percent >= 100:
            budget_status = (
                "CRITICAL — Monthly budget exceeded"
            )

        elif budget_percent >= 80:
            budget_status = (
                "WARNING — 80% of monthly budget reached"
            )

        elif budget_percent >= 50:
            budget_status = (
                "NOTICE — 50% of monthly budget reached"
            )

        else:
            budget_status = (
                "OK"
            )

    else:
        budget_text = (
            "Not configured"
        )

        budget_usage_text = (
            "Unavailable"
        )

        remaining_text = (
            "Unavailable"
        )

        budget_status = (
            "Budget monitoring not configured"
        )

    openai_balance_text = (
        "Unavailable via supported public API; "
        "check OpenAI Billing / Usage dashboard."
    )

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

    subject_suffix = optional_env(
        "EMAIL_SUBJECT_SUFFIX"
    )

    subject = (
        "The Daily Duck — Publish Complete — "
        f"{issue_date}"
    )

    if subject_suffix:
        subject += (
            f" — {subject_suffix}"
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


OPENAI API COST
----------------------------------------

Today:
{openai_daily_text}

Month to date:
{openai_monthly_text}

Cost scope:
{daily_note}


MONTHLY BUDGET
----------------------------------------

Budget:
{budget_text}

Used:
{budget_usage_text}

Remaining:
{remaining_text}

Status:
{budget_status}


GEMINI
----------------------------------------

{gemini_cost_text}


CREDIT / BILLING BALANCE
----------------------------------------

OpenAI:
{openai_balance_text}

Gemini:
{gemini_balance_text}


IMPORTANT
----------------------------------------

OpenAI Costs API reports daily cost buckets.

"Today" is the cost for the Daily Duck issue date.

"Month to date" is calculated from the first day of the
month through the end of the issue date.

For accurate Daily Duck accounting,
OPENAI_PROJECT_ID should point to an OpenAI project used
only by The Daily Duck.


Publication completed successfully.
""".strip()

    print(
        "Email subject:"
    )

    print(
        subject
    )

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

        "email_subject":
            subject,

        "website":
            "PUBLISHED",

        "x":
            "X_POSTED",

        "openai_daily_cost_status":
            daily_status,

        "openai_issue_day_cost_usd":
            daily_cost,

        "openai_daily_cost_note":
            daily_note,

        "openai_monthly_cost_status":
            monthly_status,

        "openai_month_to_date_cost_usd":
            monthly_cost,

        "openai_monthly_cost_note":
            monthly_note,

        "openai_monthly_budget_usd":
            monthly_budget,

        "openai_monthly_budget_used_percent":
            budget_percent,

        "openai_monthly_budget_remaining_usd":
            remaining_budget,

        "openai_budget_status":
            budget_status,

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
        f"OpenAI daily cost: "
        f"{openai_daily_text}"
    )

    print(
        f"OpenAI month-to-date: "
        f"{openai_monthly_text}"
    )

    print(
        f"Budget status: "
        f"{budget_status}"
    )

    print(
        f"Email subject: "
        f"{subject}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
