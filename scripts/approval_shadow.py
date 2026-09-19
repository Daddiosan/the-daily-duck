#!/usr/bin/env python3
"""Read-only Phase 3B-1 approval observer.

The module evaluates one GitHub-native approval event against a repository
snapshot.  Its only permitted write is the caller-selected observation and
summary files under RUNNER_TEMP.  It deliberately contains no transport,
repository mutation, email, generation, dispatch, or publishing code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.approval_domain import (
        ApprovalSource,
        ApprovalStage,
        ApprovalValidationError,
        TransitionOutcome,
        build_approval_command,
        decide_transition,
        normalize_design_command,
        normalize_gate_command,
        trusted_principal_from_github_context,
    )
except ModuleNotFoundError:  # pragma: no cover - workflow script invocation
    from approval_domain import (  # type: ignore
        ApprovalSource,
        ApprovalStage,
        ApprovalValidationError,
        TransitionOutcome,
        build_approval_command,
        decide_transition,
        normalize_design_command,
        normalize_gate_command,
        trusted_principal_from_github_context,
    )


SCHEMA_VERSION = "phase3b-shadow-observation/v1"
STATE_FILES = (
    "approved_story.json",
    "design_options.json",
    "design_selection_result.json",
    "ready_to_publish.json",
    "website_publish_result.json",
    "x_publish_result.json",
)
SAFETY_FIELDS = {
    "state_written": False,
    "git_committed": False,
    "git_pushed": False,
    "workflow_dispatched": False,
    "email_sent": False,
    "website_published": False,
    "x_posted": False,
}


class ShadowInputError(ValueError):
    """Controlled observer error with a stable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _text(env: Mapping[str, str], name: str, *, required: bool = False) -> str:
    value = str(env.get(name, "")).strip()
    if required and not value:
        raise ShadowInputError(f"MISSING_{name}", f"{name} is required.")
    return value


def parse_allowlist(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated repository variable and fail closed."""

    if not isinstance(raw, str) or not raw.strip():
        raise ShadowInputError(
            "EMPTY_SHADOW_ALLOWLIST",
            "APPROVAL_SHADOW_ACTORS must contain comma-separated GitHub logins.",
        )
    items = [item.strip() for item in raw.split(",")]
    if any(not item for item in items):
        raise ShadowInputError(
            "MALFORMED_SHADOW_ALLOWLIST",
            "APPROVAL_SHADOW_ACTORS cannot contain empty entries.",
        )
    return tuple(dict.fromkeys(items))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowInputError(
            "INVALID_PRODUCTION_STATE",
            f"Cannot read production state file {path.name}.",
        ) from exc
    if not isinstance(value, dict):
        raise ShadowInputError(
            "INVALID_PRODUCTION_STATE",
            f"Production state file {path.name} must contain an object.",
        )
    return value


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        return _digest_bytes(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShadowInputError(
            "PRODUCTION_STATE_READ_FAILED", f"Cannot hash {path.name}."
        ) from exc


def _date(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    return str(data.get("issue_date") or data.get("date") or "").strip()


def _state(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    return str(data.get("state") or "").strip().upper()


def _canonical_existing(stage: ApprovalStage, raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return (
            normalize_gate_command(raw)
            if stage is ApprovalStage.GATE_A
            else normalize_design_command(raw)
        )
    except ApprovalValidationError:
        return None


def production_snapshot(
    repo_root: Path,
    *,
    stage: ApprovalStage,
    requested_issue: str,
    commit_sha: str,
    gate_package_path: Path | None = None,
) -> dict[str, Any]:
    """Read the minimum production state needed by the pure domain decision."""

    state_dir = repo_root / "automation_state"
    states = {name: _load_json(state_dir / name) for name in STATE_FILES}
    hashes = {
        f"automation_state/{name}": _file_digest(state_dir / name)
        for name in STATE_FILES
    }
    gate_package = None
    if gate_package_path is not None:
        gate_package = _load_json(gate_package_path)
        hashes["gate_a_package_artifact"] = _file_digest(gate_package_path)

    if stage is ApprovalStage.GATE_A:
        if gate_package is None:
            raise ShadowInputError(
                "MISSING_GATE_A_PACKAGE",
                "The latest Daily Duck Gate A artifact is required.",
            )
        active_issue = _date(gate_package)
        if not active_issue:
            raise ShadowInputError(
                "MISSING_ACTIVE_ISSUE", "Gate A package has no issue date."
            )
        current_state = "WAITING_STORY_SELECTION"
        current_command = None
        approved = states["approved_story.json"]
        design = states["design_options.json"]
        ready = states["ready_to_publish.json"]
        website = states["website_publish_result.json"]
        x_result = states["x_publish_result.json"]
        if _date(approved) == active_issue:
            current_state = _state(approved) or "APPROVED_STORY"
            current_command = _canonical_existing(
                stage, approved.get("approval_reply") if approved else None
            )
        if _date(design) == active_issue:
            current_state = _state(design) or current_state
        if _date(ready) == active_issue:
            current_state = _state(ready) or "READY_TO_PUBLISH"
        if _date(website) == active_issue and website.get("action") == "PUBLISHED":
            current_state = "PUBLISHED"
        if _date(x_result) == active_issue and x_result.get("action") == "X_POSTED":
            current_state = "X_POSTED"
        active_batch = None
    else:
        design = states["design_options.json"]
        if design is None:
            raise ShadowInputError(
                "MISSING_DESIGN_OPTIONS", "design_options.json is required."
            )
        active_issue = _date(design)
        if not active_issue:
            raise ShadowInputError(
                "MISSING_ACTIVE_ISSUE", "Design state has no issue date."
            )
        active_batch = design.get("preview_batch_number")
        current_state = _state(design)
        current_command = _canonical_existing(
            stage,
            design.get("final_selection_reply")
            or design.get("design_approval_reply"),
        )
        ready = states["ready_to_publish.json"]
        website = states["website_publish_result.json"]
        x_result = states["x_publish_result.json"]
        if _date(ready) == active_issue:
            current_state = _state(ready) or "READY_TO_PUBLISH"
            current_command = current_command or _canonical_existing(
                stage, ready.get("design_approval_reply") if ready else None
            )
        if _date(website) == active_issue and website.get("action") == "PUBLISHED":
            current_state = "PUBLISHED"
        if _date(x_result) == active_issue and x_result.get("action") == "X_POSTED":
            current_state = "X_POSTED"

    identity_payload = {
        "commit_sha": commit_sha,
        "stage": stage.value,
        "requested_issue": requested_issue,
        "file_hashes": hashes,
    }
    identity = _digest_bytes(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    )
    return {
        "identity": identity,
        "commit_sha": commit_sha,
        "active_issue_date": active_issue,
        "current_state": current_state,
        "current_command": current_command,
        "active_design_batch_id": active_batch,
        "file_hashes": hashes,
    }


def _base_observation(env: Mapping[str, str], observed_at: str) -> dict[str, Any]:
    run_id = str(env.get("GITHUB_RUN_ID", "")).strip()
    source_event_id = f"gh-run:{run_id}" if run_id else None
    identity_seed = source_event_id or f"missing-run:{observed_at}"
    return {
        "schema_version": SCHEMA_VERSION,
        "observation_id": _digest_bytes(identity_seed.encode()),
        "observation_type": "SHADOW_EVENT_DECISION",
        "observed_at": observed_at,
        "shadow_run_id": run_id or None,
        "run_attempt": str(env.get("GITHUB_RUN_ATTEMPT", "")).strip() or None,
        "commit_sha": str(
            env.get("SHADOW_COMMIT_SHA") or env.get("GITHUB_SHA", "")
        ).strip()
        or None,
        "stage": str(env.get("SHADOW_STAGE", "")).strip() or None,
        "issue_date": str(env.get("SHADOW_ISSUE_DATE", "")).strip() or None,
        "source_type": ApprovalSource.EVENT.value,
        "source_event_id": source_event_id,
        "authorized_principal": None,
        "principal_source": None,
        "claimed_principal": str(env.get("SHADOW_CLAIMED_PRINCIPAL", "")).strip()
        or None,
        "canonical_command": None,
        "design_batch_id": str(env.get("SHADOW_DESIGN_BATCH_ID", "")).strip()
        or None,
        "upstream_run_id": str(env.get("SHADOW_UPSTREAM_RUN_ID", "")).strip()
        or None,
        "observed_upstream_run_id": str(
            env.get("SHADOW_OBSERVED_UPSTREAM_RUN_ID", "")
        ).strip()
        or None,
        "transition_key": None,
        "idempotency_key": None,
        "production_state_snapshot_identity": None,
        "production_state": None,
        "validation_outcome": "REJECTED",
        "authorization_outcome": "NOT_EVALUATED",
        "decision": TransitionOutcome.REJECT_INVALID.value,
        "reason": "UNINITIALIZED",
        "proposed_next_state": None,
        "would_dispatch": None,
        **SAFETY_FIELDS,
    }


def create_observation(
    env: Mapping[str, str],
    *,
    repo_root: Path,
    gate_package_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return an observation and whether the Shadow decision was valid."""

    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observed_at = instant.isoformat().replace("+00:00", "Z")
    observation = _base_observation(env, observed_at)
    try:
        run_id = _text(env, "GITHUB_RUN_ID", required=True)
        attempt_text = _text(env, "GITHUB_RUN_ATTEMPT", required=True)
        try:
            attempt = int(attempt_text)
        except ValueError as exc:
            raise ShadowInputError(
                "INVALID_RUN_ATTEMPT", "GITHUB_RUN_ATTEMPT must be an integer."
            ) from exc
        if attempt != 1:
            raise ShadowInputError(
                "RERUN_NOT_NEW_OBSERVATION",
                "A rerun cannot represent a new human approval event.",
            )

        actor = _text(env, "GITHUB_ACTOR", required=True)
        commit_sha = _text(env, "SHADOW_COMMIT_SHA") or _text(
            env, "GITHUB_SHA", required=True
        )
        stage_text = _text(env, "SHADOW_STAGE", required=True)
        try:
            stage = ApprovalStage(stage_text)
        except ValueError as exc:
            raise ShadowInputError("INVALID_STAGE", "Unsupported Shadow stage.") from exc
        issue_date = _text(env, "SHADOW_ISSUE_DATE", required=True)
        command_text = _text(env, "SHADOW_COMMAND", required=True)
        batch_text = _text(env, "SHADOW_DESIGN_BATCH_ID") or None
        upstream_run_id = _text(env, "SHADOW_UPSTREAM_RUN_ID") or None
        claimed_principal = _text(env, "SHADOW_CLAIMED_PRINCIPAL") or None
        allowlist = parse_allowlist(_text(env, "APPROVAL_SHADOW_ACTORS"))

        snapshot = production_snapshot(
            repo_root,
            stage=stage,
            requested_issue=issue_date,
            commit_sha=commit_sha,
            gate_package_path=gate_package_path,
        )
        trusted = trusted_principal_from_github_context(actor)
        approval = build_approval_command(
            stage=stage,
            issue_date=issue_date,
            expected_issue_date=snapshot["active_issue_date"],
            command=command_text,
            source_type=ApprovalSource.EVENT,
            trusted_principal=trusted,
            allowed_principals=allowlist,
            claimed_principal=claimed_principal,
            source_event_id=f"gh-run:{run_id}",
            upstream_run_id=upstream_run_id,
            design_batch_id=batch_text,
            expected_design_batch_id=(
                snapshot["active_design_batch_id"]
                if stage is ApprovalStage.DESIGN_SELECTION
                else None
            ),
        )
        decision = decide_transition(
            approval,
            current_state=snapshot["current_state"],
            current_issue_date=snapshot["active_issue_date"],
            current_command=snapshot["current_command"],
            current_design_batch_id=snapshot["active_design_batch_id"],
        )
        observation.update(
            {
                "stage": approval.stage.value,
                "issue_date": approval.issue_date,
                "authorized_principal": approval.authorized_principal,
                "principal_source": approval.principal_source.value,
                "claimed_principal": approval.claimed_principal,
                "canonical_command": approval.command,
                "design_batch_id": approval.design_batch_id,
                "upstream_run_id": approval.upstream_run_id,
                "transition_key": approval.transition_key,
                "idempotency_key": approval.idempotency_key,
                "production_state_snapshot_identity": snapshot["identity"],
                "production_state": {
                    "active_issue_date": snapshot["active_issue_date"],
                    "current_state": snapshot["current_state"],
                    "current_command": snapshot["current_command"],
                    "active_design_batch_id": snapshot[
                        "active_design_batch_id"
                    ],
                },
                "validation_outcome": "ACCEPTED",
                "authorization_outcome": "AUTHORIZED",
                "decision": decision.outcome.value,
                "reason": decision.reason,
                "proposed_next_state": decision.next_state,
                "would_dispatch": decision.dispatch_target,
            }
        )
        return observation, True
    except (ShadowInputError, ApprovalValidationError) as exc:
        reason = getattr(exc, "reason", type(exc).__name__)
        observation["reason"] = reason
        if reason in {
            "UNAUTHORIZED_TRUSTED_PRINCIPAL",
            "PRINCIPAL_MISMATCH",
            "EMPTY_PRINCIPAL_ALLOWLIST",
            "MALFORMED_PRINCIPAL_ALLOWLIST",
            "EMPTY_SHADOW_ALLOWLIST",
            "MALFORMED_SHADOW_ALLOWLIST",
        }:
            observation["authorization_outcome"] = "REJECTED"
        return observation, False


def render_summary(observation: Mapping[str, Any]) -> str:
    fields = (
        ("Stage", observation.get("stage")),
        ("Issue", observation.get("issue_date")),
        ("Command", observation.get("canonical_command")),
        ("Decision", observation.get("decision")),
        ("Reason", observation.get("reason")),
        ("Would dispatch (data only)", observation.get("would_dispatch")),
        ("Snapshot", observation.get("production_state_snapshot_identity")),
    )
    rows = ["## Phase 3B-1 Shadow observation", "", "| Field | Value |", "|---|---|"]
    for label, value in fields:
        safe = str(value if value is not None else "NONE").replace("|", "\\|")
        rows.append(f"| {label} | `{safe}` |")
    rows.extend(
        [
            "",
            "Production side effects: **NONE**",
            "",
        ]
    )
    return "\n".join(rows)


def _safe_output_path(path: Path, runner_temp: Path) -> Path:
    target = path.resolve()
    root = runner_temp.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ShadowInputError(
            "OUTPUT_OUTSIDE_RUNNER_TEMP", "Shadow output must stay under RUNNER_TEMP."
        ) from exc
    return target


def write_outputs(
    observation: Mapping[str, Any],
    *,
    output_path: Path,
    summary_path: Path,
    runner_temp: Path,
) -> None:
    output = _safe_output_path(output_path, runner_temp)
    summary = _safe_output_path(summary_path, runner_temp)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(observation), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    summary.write_text(render_summary(observation), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--gate-package", type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    runner_temp = Path(_text(env, "RUNNER_TEMP", required=True))
    repo_root = Path(_text(env, "GITHUB_WORKSPACE", required=True))
    observation, valid = create_observation(
        env,
        repo_root=repo_root,
        gate_package_path=args.gate_package,
    )
    write_outputs(
        observation,
        output_path=args.output,
        summary_path=args.summary,
        runner_temp=runner_temp,
    )
    print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
