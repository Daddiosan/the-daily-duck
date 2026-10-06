#!/usr/bin/env python3
"""Decide whether the current issue still needs a Gate A approval check.

This preflight reads only committed canonical state.  A no-op is allowed only
when a recognized state for the expected issue proves that Gate A has already
completed.  Malformed, unknown, mixed-date, and future state fail closed.

A coherent snapshot belonging entirely to an older issue is not completion
evidence for the expected issue.  It therefore returns CHECK_REQUIRED so the
normal artifact and approval path can evaluate the new issue.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping


class PreflightAction(str, Enum):
    CHECK_REQUIRED = "CHECK_REQUIRED"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"


class GateAPreflightError(RuntimeError):
    """Canonical state cannot safely support a Gate A decision."""


class _DuplicateJsonKeyError(ValueError):
    """A JSON object contains an ambiguous duplicate key."""


@dataclass(frozen=True)
class PreflightDecision:
    action: PreflightAction
    reason: str
    evidence: str | None = None


@dataclass(frozen=True)
class _StateSpec:
    filename: str
    field: str
    allowed_values: frozenset[str]
    reason: str
    rank: int


_STATE_SPECS = (
    _StateSpec(
        "approved_story.json",
        "state",
        frozenset({"APPROVED_STORY"}),
        "ALREADY_APPROVED",
        1,
    ),
    _StateSpec(
        "design_options.json",
        "state",
        frozenset(
            {
                "DESIGN_OPTIONS_READY",
                "WAITING_FINAL_SELECTION",
                "DESIGN_SELECTED_READY_TO_PUBLISH",
            }
        ),
        "DESIGN_ALREADY_COMPLETED",
        2,
    ),
    _StateSpec(
        "ready_to_publish.json",
        "state",
        frozenset({"READY_TO_PUBLISH", "PUBLISHED", "X_POSTED"}),
        "PUBLICATION_ALREADY_STARTED",
        3,
    ),
    _StateSpec(
        "website_publish_result.json",
        "action",
        frozenset({"PUBLISHED"}),
        "WEBSITE_ALREADY_PUBLISHED",
        4,
    ),
    _StateSpec(
        "x_publish_result.json",
        "action",
        frozenset({"X_POSTED"}),
        "TERMINAL_X_POSTED",
        5,
    ),
)


def _canonical_issue_date(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise GateAPreflightError(f"{context} issue_date is missing or invalid.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise GateAPreflightError(f"{context} issue_date is invalid.") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise GateAPreflightError(f"{context} issue_date is invalid.")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _load_state(path: Path, spec: _StateSpec) -> tuple[str, str]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateAPreflightError(f"{spec.filename} is unreadable or corrupt.") from exc
    except _DuplicateJsonKeyError as exc:
        raise GateAPreflightError(
            f"{spec.filename} contains duplicate JSON key {exc.args[0]!r}."
        ) from exc
    if not isinstance(payload, Mapping):
        raise GateAPreflightError(f"{spec.filename} must contain a JSON object.")

    issue_date = payload.get("issue_date")
    compatibility_date = payload.get("date")
    canonical_date = _canonical_issue_date(
        issue_date,
        context=spec.filename,
    )
    if compatibility_date is not None and compatibility_date != issue_date:
        raise GateAPreflightError(
            f"{spec.filename} issue_date and date aliases disagree."
        )

    value = payload.get(spec.field)
    if not isinstance(value, str) or value not in spec.allowed_values:
        raise GateAPreflightError(
            f"{spec.filename} has unknown {spec.field}: {value!r}."
        )
    return canonical_date, value


def evaluate_gate_a_relevance(
    *,
    state_dir: Path,
    expected_issue_date: str,
) -> PreflightDecision:
    expected = _canonical_issue_date(
        expected_issue_date,
        context="expected",
    )
    evidence: list[tuple[_StateSpec, str, str]] = []
    for spec in _STATE_SPECS:
        path = state_dir / spec.filename
        if path.exists():
            issue_date, value = _load_state(path, spec)
            evidence.append((spec, issue_date, value))

    if not evidence:
        return PreflightDecision(
            PreflightAction.CHECK_REQUIRED,
            "NO_CURRENT_GATE_A_COMPLETION_STATE",
        )

    dates = {issue_date for _, issue_date, _ in evidence}
    if len(dates) != 1:
        detail = ", ".join(sorted(dates))
        raise GateAPreflightError(
            f"Canonical state has contradictory issue dates: {detail}."
        )

    [state_issue_date] = dates
    if state_issue_date != expected:
        if state_issue_date < expected:
            return PreflightDecision(
                PreflightAction.CHECK_REQUIRED,
                "PRIOR_ISSUE_STATE_ONLY",
            )
        raise GateAPreflightError(
            "Canonical state issue_date does not match the expected issue_date."
        )

    spec, _, value = max(evidence, key=lambda item: item[0].rank)
    return PreflightDecision(
        PreflightAction.NO_ACTION_REQUIRED,
        spec.reason,
        f"{spec.filename}:{value}",
    )


def _append_github_output(path: Path, decision: PreflightDecision) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"action={decision.action.value}\n")
        output.write(f"reason={decision.reason}\n")
        output.write(f"evidence={decision.evidence or ''}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-issue-date", required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("automation_state"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    try:
        decision = evaluate_gate_a_relevance(
            state_dir=args.state_dir,
            expected_issue_date=args.expected_issue_date,
        )
    except GateAPreflightError as exc:
        print(f"ERROR: Gate A preflight failed closed: {exc}", file=sys.stderr)
        return 1

    if args.github_output is not None:
        _append_github_output(args.github_output, decision)
    print(f"Gate A preflight action: {decision.action.value}")
    print(f"Gate A preflight reason: {decision.reason}")
    if decision.evidence is not None:
        print(f"Gate A completion evidence: {decision.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
