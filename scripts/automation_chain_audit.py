#!/usr/bin/env python3
"""Audit scheduled and downstream Daily Duck automation without mutating it."""
from __future__ import annotations

import argparse, json, os, subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))
TIMEOUT = timedelta(minutes=60)
WORKFLOWS = {
    "The Daily Duck Automation": ("daily-duck.yml", "Daily Automation"),
    "The Daily Duck - Gate A Approval Check": ("approval-check-phase2.yml", "Gate A Approval Check"),
    "The Daily Duck - Design Options": ("design-options.yml", "Design Options"),
    "The Daily Duck - Design Selection Check": ("design-selection-check.yml", "Design Selection Check"),
    "The Daily Duck - Website Publish": ("website-publish.yml", "Website Publish"),
    "The Daily Duck - X Publish": ("x-publish.yml", "X Publish"),
}

@dataclass(frozen=True)
class Problem:
    failure_stage: str
    issue_date: str
    workflow_name: str
    run_id: int
    run_url: str
    run_status: str
    attempt: int
    detected_at: str
    suggested_action: str

    @property
    def key(self) -> str:
        return ":".join((self.workflow_name, str(self.run_id), str(self.attempt), self.failure_stage))

def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

def issue_date_from(states: dict[str, Any], now: datetime) -> str:
    for name in ("approved_story", "design_options", "ready_to_publish"):
        data = states.get(name)
        if isinstance(data, dict):
            value = data.get("issue_date") or data.get("date")
            if isinstance(value, str) and value:
                return value
    return now.astimezone(JST).date().isoformat()

def state_time(data: Any) -> datetime | None:
    if not isinstance(data, dict):
        return None
    for key in ("x_posted_at", "published_at", "checked_at", "final_email_sent_at", "generated_at", "approved_at", "gate_a_package_created_at"):
        value = parse_time(data.get(key))
        if value:
            return value
    return None

def latest(runs: dict[str, list[dict[str, Any]]], workflow: str) -> dict[str, Any] | None:
    items = runs.get(workflow, [])
    return max(items, key=lambda r: str(r.get("created_at") or "")) if items else None

def problem(stage: str, date: str, now: datetime, run: dict[str, Any] | None, status: str, action: str) -> Problem:
    run = run or {}
    return Problem(stage, date, str(run.get("name") or stage), int(run.get("id") or 0), str(run.get("html_url") or run.get("url") or ""), status, int(run.get("run_attempt") or run.get("attempt") or 0), now.astimezone(JST).isoformat(), action)

def evaluate(runs: dict[str, list[dict[str, Any]]], states: dict[str, Any], now: datetime, timeout: timedelta = TIMEOUT) -> list[Problem]:
    """Return current problems; a successful current retry clears its failed run."""
    date, found = issue_date_from(states, now), []
    for workflow, (_, stage) in WORKFLOWS.items():
        run = latest(runs, workflow)
        if not run:
            if stage in ("Daily Automation", "Gate A Approval Check", "Design Selection Check"):
                found.append(problem(stage, date, now, None, "MISSING", "Check the workflow trigger and Actions availability."))
            continue
        status, conclusion = str(run.get("status") or ""), str(run.get("conclusion") or "")
        if status == "completed" and conclusion not in ("success", "neutral", "skipped"):
            found.append(problem(stage, date, now, run, f"completed/{conclusion}", f"Inspect and safely rerun {stage}; do not bypass approval gates."))
        created = parse_time(run.get("created_at"))
        if stage in ("Design Options", "Design Selection Check", "Website Publish", "X Publish") and status in ("queued", "in_progress") and created and now - created > timeout:
            found.append(problem(f"{stage} completion timeout", date, now, run, status.upper(), f"Inspect the stalled {stage} run before considering a safe rerun."))
        if stage == "Daily Automation" and created and created.astimezone(JST).date().isoformat() != now.astimezone(JST).date().isoformat():
            found.append(problem("Daily Automation scheduled trigger", date, now, run, "NO_RUN_TODAY", "Check the daily schedule and Actions availability."))
        if stage in ("Gate A Approval Check", "Design Selection Check") and created and now - created > timeout:
            found.append(problem(f"{stage} scheduled trigger", date, now, run, "SCHEDULE_STALE", "Check the frequent poller schedule and Actions availability."))

    approved, design = states.get("approved_story"), states.get("design_options")
    selection, ready = states.get("design_selection_result"), states.get("ready_to_publish")
    website, x_result = states.get("website_publish_result"), states.get("x_publish_result")
    field = lambda data, key: data.get(key) if isinstance(data, dict) else None

    if field(approved, "issue_date") == date and field(design, "issue_date") != date:
        started = state_time(approved)
        if started and now - started > timeout:
            found.append(problem("Design Options state timeout", date, now, latest(runs, "The Daily Duck - Design Options"), "STATE_NOT_GENERATED", "Rerun Design Options for this issue only."))
    if field(selection, "action") == "READY_TO_PUBLISH" and field(ready, "issue_date") != date:
        started = state_time(selection)
        if started and now - started > timeout:
            found.append(problem("Design Selection state transition", date, now, latest(runs, "The Daily Duck - Design Selection Check"), "READY_STATE_MISSING", "Inspect ready_to_publish generation; do not trigger publishing manually."))
    website_ok = field(website, "issue_date") == date and field(website, "action") == "PUBLISHED"
    if field(ready, "issue_date") == date and field(ready, "state") == "READY_TO_PUBLISH" and not website_ok:
        started = state_time(ready)
        if started and now - started > timeout:
            found.append(problem("Website Publish state inconsistency", date, now, latest(runs, "The Daily Duck - Website Publish"), "WEBSITE_NOT_PUBLISHED", "Inspect Website Publish and duplicate-date protection before rerunning."))
    x_ok = field(x_result, "issue_date") == date and field(x_result, "action") == "X_POSTED"
    if website_ok and not x_ok:
        started = state_time(website)
        if started and now - started > timeout:
            found.append(problem("X Publish final state inconsistency", date, now, latest(runs, "The Daily Duck - X Publish"), "X_NOT_POSTED", "Inspect remote duplicate protection before safely rerunning X Publish."))
    return list({item.key: item for item in found}.values())

def pending_alerts(current: list[Problem], seen: set[str]) -> list[Problem]:
    return [item for item in current if item.key not in seen]

def gh_json(endpoint: str) -> Any:
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh api failed: {endpoint}")
    return json.loads(result.stdout)

def live_inputs(repository: str, state_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    runs = {}
    for workflow, (filename, _) in WORKFLOWS.items():
        runs[workflow] = gh_json(f"repos/{repository}/actions/workflows/{filename}/runs?per_page=20").get("workflow_runs", [])
    names = ("approved_story", "design_options", "design_selection_result", "ready_to_publish", "website_publish_result", "x_publish_result")
    return runs, {name: read_json(state_dir / f"{name}.json") for name in names}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path("automation_state"))
    parser.add_argument("--seen-file", type=Path, default=Path("monitor_state/seen_alerts.json"))
    parser.add_argument("--pending-file", type=Path, default=Path("monitor_state/pending_alerts.json"))
    parser.add_argument("--result-file", type=Path, default=Path("audit_result.json"))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.fixture:
        fixture = read_json(args.fixture)
        if not isinstance(fixture, dict):
            raise SystemExit("Invalid fixture")
        now = parse_time(fixture.get("now")) or now
        runs, states = fixture.get("runs", {}), fixture.get("states", {})
    else:
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repository:
            raise SystemExit("GITHUB_REPOSITORY is required")
        runs, states = live_inputs(repository, args.state_dir)
    current = evaluate(runs, states, now)
    seen_data = read_json(args.seen_file)
    seen = set(seen_data.get("keys", [])) if isinstance(seen_data, dict) else set()
    pending = pending_alerts(current, seen)
    payload = {"detected_at": now.astimezone(JST).isoformat(), "problems": [asdict(p) | {"key": p.key} for p in current], "pending_alerts": [asdict(p) | {"key": p.key} for p in pending]}
    for path in (args.result_file, args.pending_file):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    args.pending_file.write_text(json.dumps({"alerts": payload["pending_alerts"]}, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"Current problems: {len(current)}; new alerts: {len(pending)}")
    return 1 if current else 0

if __name__ == "__main__":
    raise SystemExit(main())
