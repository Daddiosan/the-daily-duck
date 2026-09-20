#!/usr/bin/env python3
"""Read-only reconciliation for Phase 3B-1 Shadow observations."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from scripts.approval_domain import (
        ApprovalValidationError,
        normalize_design_command,
        normalize_gate_command,
    )
    from scripts.approval_shadow import SAFETY_FIELDS, _load_json, _safe_output_path
except ModuleNotFoundError:  # pragma: no cover - workflow script invocation
    from approval_domain import (  # type: ignore
        ApprovalValidationError,
        normalize_design_command,
        normalize_gate_command,
    )
    from approval_shadow import SAFETY_FIELDS, _load_json, _safe_output_path  # type: ignore


SCHEMA_VERSION = "phase3b-shadow-comparison/v1"
CLASSIFICATIONS = (
    "MATCH",
    "EVENT_ACCEPT_POLLER_REJECT",
    "EVENT_REJECT_POLLER_ACCEPT",
    "COMMAND_MISMATCH",
    "BATCH_MISMATCH",
    "TIMING_ONLY",
    "UNKNOWN",
)
WORKFLOW_STAGE = {
    "The Daily Duck - Gate A Approval Check": "GATE_A",
    "The Daily Duck - Design Selection Check": "DESIGN_SELECTION",
}
_ACTIONS_TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\s"
)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )


def _all_log_text(log_root: Path) -> str:
    parts: list[str] = []
    if not log_root.exists():
        return ""
    for path in sorted(log_root.rglob("*")):
        if path.is_file() and path.stat().st_size <= 10_000_000:
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(parts)


def runtime_log_lines(log_text: str) -> tuple[str, ...]:
    """Return runtime payload lines, excluding GitHub Runner command echoes.

    Downloaded Actions logs prefix payload with an ISO timestamp. Before a
    ``run`` step executes, the Runner also emits the complete shell source
    between ``##[group]Run ...`` and ``##[endgroup]``. Those source lines are
    not execution evidence and are deliberately discarded. Marker matching
    remains whole-line and unindented, so standalone shell/Python/quoted source
    cannot become evidence merely because it contains marker text. Unknown or
    malformed prefixes remain intact and therefore fail closed.
    """

    lines: list[str] = []
    in_run_source = False
    for raw_line in log_text.splitlines():
        payload = _ACTIONS_TIMESTAMP_PREFIX.sub("", raw_line, count=1)
        if in_run_source:
            if payload == "##[endgroup]":
                in_run_source = False
            continue
        if payload.startswith("##[group]Run "):
            in_run_source = True
            continue
        if payload.startswith("##["):
            continue
        lines.append(payload)
    return tuple(lines)


def _canonical(stage: str, raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return (
            normalize_gate_command(raw)
            if stage == "GATE_A"
            else normalize_design_command(raw)
        )
    except ApprovalValidationError:
        return None


def production_evidence(
    *,
    workflow_name: str,
    conclusion: str,
    log_text: str,
    repo_root: Path,
) -> dict[str, Any]:
    """Combine committed state, stable log markers, and conclusion in priority order."""

    stage = WORKFLOW_STAGE.get(workflow_name)
    if stage is None:
        return {"stage": None, "outcome": "UNKNOWN", "conflict": True}

    runtime_text = "\n".join(runtime_log_lines(log_text))
    issue_matches = re.findall(
        r"^(?:issue_date|Issue date):\s*(\d{4}-\d{2}-\d{2})\s*$",
        runtime_text,
        flags=re.MULTILINE,
    )
    issue_date = issue_matches[-1] if issue_matches else None
    state_dir = repo_root / "automation_state"
    sources: list[str] = []
    state_outcome: str | None = None
    state_command: str | None = None
    state_batch: str | None = None

    if stage == "GATE_A":
        approved = _load_json(state_dir / "approved_story.json")
        if approved and (approved.get("issue_date") or approved.get("date")):
            state_issue = str(approved.get("issue_date") or approved.get("date"))
            if issue_date is None:
                issue_date = state_issue
            if state_issue == issue_date:
                state_outcome = "ACCEPT"
                state_command = _canonical(stage, approved.get("approval_reply"))
                sources.append("COMMITTED_STATE")
        accepted_command = None
        found = re.findall(
            r"^EXACT GATE A STORY SELECTION FOUND:\s*([1-5])\s*$",
            runtime_text,
            flags=re.MULTILINE,
        )
        if found:
            accepted_command = _canonical(stage, found[-1])
        log_accept = bool(
            accepted_command
            or re.search(
                r"^STATE:\s*APPROVED_STORY\s*$",
                runtime_text,
                flags=re.MULTILINE,
            )
        )
        log_reject = bool(
            re.search(
                r"^STATE:\s*WAITING_STORY_SELECTION\s*$",
                runtime_text,
                flags=re.MULTILINE,
            )
        )
        upstream_matches = re.findall(
            r"^Latest successful Daily Duck run ID:\s*([0-9]+)\s*$",
            runtime_text,
            flags=re.MULTILINE,
        )
        upstream_run_id = upstream_matches[-1] if upstream_matches else None
    else:
        design = _load_json(state_dir / "design_options.json")
        result = _load_json(state_dir / "design_selection_result.json")
        ready = _load_json(state_dir / "ready_to_publish.json")
        state_issue = None
        if design:
            state_issue = str(design.get("issue_date") or design.get("date") or "")
        if not state_issue and result:
            state_issue = str(result.get("issue_date") or "")
        if issue_date is None and state_issue:
            issue_date = state_issue

        action = str(result.get("action") or "") if result else ""
        if state_issue and state_issue == issue_date:
            if action == "NEXT_3_GENERATED" and design:
                current_batch = int(design.get("preview_batch_number") or 0)
                if current_batch > 1:
                    state_batch = str(current_batch - 1)
                    state_command = "NEXT_3"
                    state_outcome = "ACCEPT"
                    sources.append("COMMITTED_STATE")
            elif ready and str(ready.get("issue_date") or "") == issue_date:
                state_batch = str(ready.get("preview_batch_number") or "") or None
                state_command = _canonical(
                    stage, ready.get("design_approval_reply")
                )
                state_outcome = "ACCEPT"
                sources.append("COMMITTED_STATE")
            elif str(design.get("state") or "") == "DESIGN_SELECTED_READY_TO_PUBLISH":
                state_batch = str(design.get("preview_batch_number") or "") or None
                state_command = _canonical(stage, design.get("final_selection_reply"))
                state_outcome = "ACCEPT"
                sources.append("COMMITTED_STATE")

        next_three = bool(
            re.search(r"^NEXT 3 accepted\.\s*$", runtime_text, flags=re.MULTILINE)
        )
        final_image = re.findall(
            r"^FINAL IMAGE / CONCEPT:\s*([1-3])\s*$",
            runtime_text,
            flags=re.MULTILINE,
        )
        final_title = re.findall(
            r"^FINAL TITLE:\s*([1-3])\s*$",
            runtime_text,
            flags=re.MULTILINE,
        )
        accepted_command = None
        if next_three:
            accepted_command = "NEXT_3"
        elif final_image and final_title:
            accepted_command = _canonical(stage, f"{final_image[-1]} {final_title[-1]}")
        log_accept = bool(
            next_three
            or accepted_command
            or re.search(
                r"^STATE:\s*(?:READY_TO_PUBLISH|ALREADY_SELECTED)\s*$",
                runtime_text,
                flags=re.MULTILINE,
            )
        )
        log_reject = bool(
            re.search(
                r"^(?:Design action:\s*WAIT|STATE:\s*WAITING_FINAL_SELECTION)\s*$",
                runtime_text,
                flags=re.MULTILINE,
            )
        ) and not next_three
        upstream_run_id = None

    log_outcome = "ACCEPT" if log_accept and not log_reject else None
    if log_reject and not log_accept:
        log_outcome = "REJECT"
    if log_accept and log_reject:
        log_outcome = "CONFLICT"
    if log_outcome:
        sources.append("POLLER_LOG")

    conflict = log_outcome == "CONFLICT"
    if state_outcome and log_outcome in {"ACCEPT", "REJECT"} and state_outcome != log_outcome:
        conflict = True
    if state_command and accepted_command and state_command != accepted_command:
        conflict = True
    if conclusion != "success":
        conflict = True

    outcome = "UNKNOWN"
    command = state_command or accepted_command
    if not conflict:
        if state_outcome:
            outcome = state_outcome
        elif log_outcome in {"ACCEPT", "REJECT"}:
            outcome = log_outcome
        elif conclusion == "success":
            outcome = "UNKNOWN"

    return {
        "stage": stage,
        "issue_date": issue_date,
        "design_batch_id": state_batch,
        "canonical_command": command,
        "outcome": outcome,
        "conflict": conflict,
        "evidence_sources": sources,
        "workflow_conclusion": conclusion,
        "upstream_run_id": upstream_run_id,
    }


def load_observations(root: Path) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    if not root.exists():
        return observations
    for path in sorted(root.rglob("observation.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("observation_type") == "SHADOW_EVENT_DECISION"
        ):
            observations.append(value)
    return observations


def _event_outcome(observation: Mapping[str, Any]) -> str:
    decision = str(observation.get("decision") or "")
    if decision in {"APPLY", "NO_OP_ALREADY_APPLIED"}:
        return "ACCEPT"
    if decision.startswith("REJECT"):
        return "REJECT"
    return "UNKNOWN"


def correlate(
    observations: Iterable[dict[str, Any]],
    evidence: Mapping[str, Any],
    *,
    poller_created_at: datetime,
    poller_completed_at: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    lower = poller_created_at - timedelta(hours=24)
    upper = poller_completed_at + timedelta(hours=1)
    candidates: list[dict[str, Any]] = []
    for item in observations:
        when = parse_time(item.get("observed_at"))
        if when is None or not (lower <= when <= upper):
            continue
        if item.get("stage") != evidence.get("stage"):
            continue
        evidence_issue = evidence.get("issue_date")
        if evidence_issue and item.get("issue_date") != evidence_issue:
            continue
        candidates.append(item)
    if not candidates:
        return None, "NO_CORRELATED_SHADOW_ARTIFACT"
    if evidence.get("stage") == "DESIGN_SELECTION" and evidence.get(
        "design_batch_id"
    ):
        exact_batch = [
            item
            for item in candidates
            if str(item.get("design_batch_id") or "")
            == str(evidence.get("design_batch_id"))
        ]
        if exact_batch:
            candidates = exact_batch
    upstream = str(evidence.get("upstream_run_id") or "")
    if upstream:
        exact_upstream = [
            item
            for item in candidates
            if str(
                item.get("observed_upstream_run_id")
                or item.get("upstream_run_id")
                or ""
            )
            == upstream
        ]
        if exact_upstream:
            candidates = exact_upstream
    if len(candidates) != 1:
        return None, "AMBIGUOUS_SHADOW_ARTIFACTS"
    return candidates[0], None


def classify(
    observation: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
    *,
    poller_completed_at: datetime,
    correlation_error: str | None = None,
) -> tuple[str, str]:
    if correlation_error or observation is None:
        return "UNKNOWN", correlation_error or "MISSING_OBSERVATION"
    observed_at = parse_time(observation.get("observed_at"))
    if observed_at is None:
        return "UNKNOWN", "INVALID_OBSERVATION_TIME"
    if poller_completed_at < observed_at:
        return "TIMING_ONLY", "Poller completed before the Shadow event."
    if evidence.get("conflict") or evidence.get("outcome") == "UNKNOWN":
        return "UNKNOWN", "Production evidence is incomplete or contradictory."
    event_batch = observation.get("design_batch_id")
    poller_batch = evidence.get("design_batch_id")
    if event_batch and poller_batch and str(event_batch) != str(poller_batch):
        return "BATCH_MISMATCH", "Design batch identifiers differ."
    event_command = observation.get("canonical_command")
    poller_command = evidence.get("canonical_command")
    if event_command and poller_command and event_command != poller_command:
        return "COMMAND_MISMATCH", "Canonical commands differ."
    event_outcome = _event_outcome(observation)
    poller_outcome = str(evidence.get("outcome") or "UNKNOWN")
    if event_outcome == "UNKNOWN":
        return "UNKNOWN", "Shadow outcome is not comparable."
    if event_outcome == poller_outcome:
        return "MATCH", "Shadow and production outcomes are equivalent."
    if event_outcome == "ACCEPT" and poller_outcome == "REJECT":
        return "EVENT_ACCEPT_POLLER_REJECT", "Only the Shadow path accepted."
    if event_outcome == "REJECT" and poller_outcome == "ACCEPT":
        return "EVENT_REJECT_POLLER_ACCEPT", "Only the poller path accepted."
    return "UNKNOWN", "Outcomes could not be classified."


def compare(
    observations: Iterable[dict[str, Any]],
    evidence: dict[str, Any],
    *,
    workflow_name: str,
    poller_run_id: str,
    poller_run_attempt: str,
    poller_created_at: datetime,
    poller_completed_at: datetime,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    selected, error = correlate(
        observations,
        evidence,
        poller_created_at=poller_created_at,
        poller_completed_at=poller_completed_at,
    )
    classification, reason = classify(
        selected,
        evidence,
        poller_completed_at=poller_completed_at,
        correlation_error=error,
    )
    instant = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "observation_type": "SHADOW_RECONCILIATION",
        "observed_at": instant.isoformat().replace("+00:00", "Z"),
        "poller_workflow": workflow_name,
        "poller_run_id": poller_run_id,
        "poller_run_attempt": poller_run_attempt,
        "poller_created_at": poller_created_at.isoformat().replace("+00:00", "Z"),
        "poller_completed_at": poller_completed_at.isoformat().replace("+00:00", "Z"),
        "shadow_observation_id": selected.get("observation_id") if selected else None,
        "stage": evidence.get("stage"),
        "issue_date": evidence.get("issue_date"),
        "design_batch_id": evidence.get("design_batch_id"),
        "shadow_command": selected.get("canonical_command") if selected else None,
        "poller_command": evidence.get("canonical_command"),
        "shadow_outcome": _event_outcome(selected) if selected else None,
        "poller_outcome": evidence.get("outcome"),
        "production_evidence": evidence,
        "classification": classification,
        "reason": reason,
        **SAFETY_FIELDS,
    }


def render_summary(result: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "## Phase 3B-1 Shadow reconciliation",
            "",
            f"- Classification: `{result.get('classification')}`",
            f"- Stage: `{result.get('stage')}`",
            f"- Issue: `{result.get('issue_date')}`",
            f"- Reason: {result.get('reason')}",
            "- Production side effects: **NONE**",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    env = os.environ
    workflow_name = str(env.get("POLLER_WORKFLOW_NAME", ""))
    created = parse_time(env.get("POLLER_CREATED_AT"))
    completed = parse_time(env.get("POLLER_COMPLETED_AT"))
    if created is None or completed is None:
        raise SystemExit("Valid poller timestamps are required.")
    evidence = production_evidence(
        workflow_name=workflow_name,
        conclusion=str(env.get("POLLER_CONCLUSION", "")),
        log_text=_all_log_text(args.logs),
        repo_root=Path(str(env.get("GITHUB_WORKSPACE", "."))),
    )
    result = compare(
        load_observations(args.artifacts),
        evidence,
        workflow_name=workflow_name,
        poller_run_id=str(env.get("POLLER_RUN_ID", "")),
        poller_run_attempt=str(env.get("POLLER_RUN_ATTEMPT", "")),
        poller_created_at=created,
        poller_completed_at=completed,
    )
    runner_temp = Path(str(env.get("RUNNER_TEMP", "")))
    output = _safe_output_path(args.output, runner_temp)
    summary = _safe_output_path(args.summary, runner_temp)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary.write_text(render_summary(result), encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["classification"] != "UNKNOWN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
