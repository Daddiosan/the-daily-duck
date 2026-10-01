#!/usr/bin/env python3
"""Evaluate sanitized approval-pipeline health signals without side effects."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Thresholds:
    backlog_messages: int = 5
    oldest_unacked_seconds: int = 15 * 60
    watch_expiration_warning: timedelta = timedelta(hours=48)
    pending_workflow_absence: timedelta = timedelta(minutes=30)
    relay_success_absence: timedelta = timedelta(minutes=30)


@dataclass(frozen=True)
class HealthAlert:
    code: str
    severity: str
    value: str


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _integer(snapshot: Mapping[str, Any], key: str) -> int:
    value = snapshot.get(key, 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def evaluate_health(
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    thresholds: Thresholds = Thresholds(),
) -> list[HealthAlert]:
    now = now.astimezone(timezone.utc)
    alerts: list[HealthAlert] = []

    relay_5xx = _integer(snapshot, "relay_5xx_count")
    if relay_5xx > 0:
        alerts.append(HealthAlert("RELAY_5XX", "CRITICAL", str(relay_5xx)))

    canary = str(snapshot.get("oauth_canary_status", "UNKNOWN"))
    if canary not in {"OK", "SUCCESS"}:
        alerts.append(HealthAlert("OAUTH_CANARY_FAILURE", "CRITICAL", canary))

    backlog = _integer(snapshot, "pubsub_backlog")
    if backlog > thresholds.backlog_messages:
        alerts.append(HealthAlert("PUBSUB_BACKLOG_HIGH", "CRITICAL", str(backlog)))

    oldest = _integer(snapshot, "oldest_unacked_seconds")
    if oldest > thresholds.oldest_unacked_seconds:
        alerts.append(
            HealthAlert("PUBSUB_OLDEST_UNACKED_HIGH", "CRITICAL", str(oldest))
        )

    renewal = str(snapshot.get("watch_renewal_status", "UNKNOWN"))
    if renewal not in {"OK", "SUCCESS"}:
        alerts.append(HealthAlert("WATCH_RENEWAL_FAILURE", "CRITICAL", renewal))

    expiration_ms = _integer(snapshot, "watch_expiration_ms")
    if expiration_ms <= 0:
        alerts.append(HealthAlert("WATCH_EXPIRATION_UNKNOWN", "CRITICAL", "missing"))
    else:
        expiration = datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc)
        remaining = expiration - now
        if remaining <= thresholds.watch_expiration_warning:
            alerts.append(
                HealthAlert(
                    "WATCH_EXPIRATION_APPROACHING",
                    "CRITICAL",
                    str(max(0, int(remaining.total_seconds()))),
                )
            )

    last_relay = parse_time(snapshot.get("last_relay_success_at"))
    if backlog > 0 and (
        last_relay is None or now - last_relay > thresholds.relay_success_absence
    ):
        alerts.append(
            HealthAlert("RELAY_SUCCESS_ABSENT_WITH_BACKLOG", "CRITICAL", str(backlog))
        )

    stage_checks = (
        (
            "gate_state",
            "WAITING_STORY_SELECTION",
            "last_gate_checker_run_at",
            "GATE_A_CHECKER_ABSENT_WHILE_PENDING",
        ),
        (
            "design_state",
            "WAITING_FINAL_SELECTION",
            "last_design_checker_run_at",
            "DESIGN_CHECKER_ABSENT_WHILE_PENDING",
        ),
    )
    for state_key, pending_state, run_key, code in stage_checks:
        if snapshot.get(state_key) != pending_state:
            continue
        last_run = parse_time(snapshot.get(run_key))
        if last_run is None or now - last_run > thresholds.pending_workflow_absence:
            value = "missing" if last_run is None else last_run.isoformat()
            alerts.append(HealthAlert(code, "CRITICAL", value))

    return alerts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("Invalid sanitized health snapshot.") from exc
    if not isinstance(snapshot, Mapping):
        raise SystemExit("Invalid sanitized health snapshot.")
    now = parse_time(snapshot.get("observed_at")) or datetime.now(timezone.utc)
    alerts = evaluate_health(snapshot, now=now)
    print(json.dumps({"status": "HEALTHY" if not alerts else "ALERT", "alerts": [asdict(a) for a in alerts]}, indent=2))
    return 0 if not alerts else 1


if __name__ == "__main__":
    raise SystemExit(main())
