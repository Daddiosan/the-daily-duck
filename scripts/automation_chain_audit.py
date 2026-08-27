#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone


JST = timezone(timedelta(hours=9))

REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

WORKFLOWS = {
    "Daily Duck": {
        "file": "daily-duck.yml",
        "type": "daily",
    },
    "Gate A Approval Check": {
        "file": "approval-check-phase2.yml",
        "type": "frequent",
    },
    "Design Selection Check": {
        "file": "design-selection-check.yml",
        "type": "frequent",
    },
}


def run_gh(args):
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("gh command failed:")
        print(result.stderr)
        raise SystemExit(2)

    return result.stdout


def get_runs(workflow_file, limit=20):
    output = run_gh(
        [
            "run",
            "list",
            "--workflow",
            workflow_file,
            "--limit",
            str(limit),
            "--json",
            "databaseId,event,status,conclusion,createdAt,startedAt,updatedAt,url",
        ]
    )

    return json.loads(output)


def parse_github_time(value):
    if not value:
        return None

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def fmt_jst(dt):
    if dt is None:
        return "unknown"

    return dt.astimezone(JST).strftime(
        "%Y-%m-%d %H:%M:%S JST"
    )


def latest_scheduled_run(runs):
    scheduled = [
        r for r in runs
        if r.get("event") == "schedule"
    ]

    if not scheduled:
        return None

    scheduled.sort(
        key=lambda r: r.get("createdAt", ""),
        reverse=True,
    )

    return scheduled[0]


def latest_successful_scheduled_run(runs):
    successful = [
        r for r in runs
        if (
            r.get("event") == "schedule"
            and r.get("conclusion") == "success"
        )
    ]

    if not successful:
        return None

    successful.sort(
        key=lambda r: r.get("createdAt", ""),
        reverse=True,
    )

    return successful[0]


def main():
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    print("=" * 70)
    print("THE DAILY DUCK - AUTOMATION CHAIN RUNTIME AUDIT")
    print("=" * 70)
    print(f"Audit time: {fmt_jst(now_utc)}")
    print()

    errors = []

    for name, config in WORKFLOWS.items():
        workflow_file = config["file"]
        workflow_type = config["type"]

        print(f"[CHECK] {name}")
        print(f"Workflow: {workflow_file}")

        runs = get_runs(workflow_file)

        if not runs:
            errors.append(
                f"{name}: no workflow runs found"
            )
            print("  ERROR: no runs found")
            print()
            continue

        latest = latest_scheduled_run(runs)
        latest_success = latest_successful_scheduled_run(runs)

        if latest:
            latest_time = parse_github_time(
                latest.get("createdAt")
            )

            print(
                "  Last scheduled run:",
                fmt_jst(latest_time),
            )

            print(
                "  Status:",
                latest.get("status"),
                "/",
                latest.get("conclusion"),
            )

            print(
                "  URL:",
                latest.get("url"),
            )
        else:
            latest_time = None
            print("  Last scheduled run: NONE")

        if latest_success:
            success_time = parse_github_time(
                latest_success.get("createdAt")
            )

            print(
                "  Last successful scheduled run:",
                fmt_jst(success_time),
            )
        else:
            success_time = None
            print(
                "  Last successful scheduled run: NONE"
            )

        # ---------------------------------------------------------
        # Daily Duck
        #
        # Expected once every morning.
        #
        # Audit runs at:
        #   08:20 / 12:20 / 18:20 / 22:20 JST
        #
        # At those times, today's scheduled Daily Duck run should
        # already exist.
        # ---------------------------------------------------------
        if workflow_type == "daily":

            today = now_jst.date()

            if latest_time is None:
                errors.append(
                    f"{name}: scheduled workflow has never run"
                )

            elif latest_time.astimezone(JST).date() != today:
                errors.append(
                    f"{name}: no scheduled run today. "
                    f"Last scheduled run was "
                    f"{fmt_jst(latest_time)}"
                )

            elif latest.get("conclusion") not in (
                "success",
                None,
            ):
                errors.append(
                    f"{name}: today's scheduled run "
                    f"ended with "
                    f"{latest.get('conclusion')}"
                )

        # ---------------------------------------------------------
        # Frequent pollers
        #
        # These workflows normally run every ~15 minutes.
        # Allow 60 minutes to avoid false alarms from GitHub queue
        # delays.
        # ---------------------------------------------------------
        elif workflow_type == "frequent":

            if latest_time is None:
                errors.append(
                    f"{name}: scheduled workflow has never run"
                )

            else:
                age = now_utc - latest_time

                if age > timedelta(minutes=60):
                    errors.append(
                        f"{name}: scheduled trigger appears stopped. "
                        f"Last scheduled run was "
                        f"{fmt_jst(latest_time)} "
                        f"({int(age.total_seconds() // 60)} "
                        f"minutes ago)"
                    )

        print()

    print("=" * 70)

    if errors:
        print("AUTOMATION CHAIN AUDIT FAILED")
        print()

        for error in errors:
            print(f"- {error}")

        print()
        print(
            f"Repository: "
            f"https://github.com/{REPOSITORY}/actions"
        )

        raise SystemExit(1)

    print("AUTOMATION CHAIN AUDIT PASSED")
    print()
    print("All scheduled workflow triggers appear healthy.")


if __name__ == "__main__":
    main()
