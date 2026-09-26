"""GitHub Actions dispatch abstraction for cloud/approval_relay (Thin
Relay, Phase M3A).

The protocol and test fake live here. The separately isolated production
GitHub App implementation lives in github_app_dispatch.py so the network and
credential boundary is easy to audit. No PAT or GITHUB_TOKEN fallback exists.

The workflow/ref allowlist below is the ONLY thing this relay is ever
permitted to dispatch. Both router.py's stage -> workflow mapping and this
module's own dispatch() validation enforce it independently (defense in
depth), so no caller -- not even a misbehaving or compromised upstream
component -- can make this relay target an arbitrary GitHub Actions
workflow, a different repository, or a non-`main` ref. There is no
repo/ref parameter derived from caller input anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Mapping, Protocol, Sequence


# Fixed, application-controlled constants -- never derived from any
# caller-supplied value. See module docstring.
GATE_A_WORKFLOW = "approval-check-phase2.yml"
DESIGN_SELECTION_WORKFLOW = "design-selection-check.yml"
DISPATCH_REF = "main"

ALLOWED_WORKFLOWS = frozenset({GATE_A_WORKFLOW, DESIGN_SELECTION_WORKFLOW})


class DispatchOutcome(str, Enum):
    """The four structured outcomes a dispatcher call can produce (task
    spec Sec. 7/11). SUCCESS and CLEAR_FINAL_FAILURE are definite;
    CLEAR_RETRYABLE_FAILURE is definite-but-retryable; UNKNOWN_OUTCOME
    covers every case where the request may have reached GitHub but this
    relay never got a trustworthy response (timeout, connection reset
    mid-response, 5xx with an ambiguous body, etc.) -- it is never treated
    as either a success or a failure, only as "must not be automatically
    redispatched"."""

    SUCCESS = "SUCCESS"
    CLEAR_RETRYABLE_FAILURE = "CLEAR_RETRYABLE_FAILURE"
    CLEAR_FINAL_FAILURE = "CLEAR_FINAL_FAILURE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True)
class DispatchResult:
    outcome: DispatchOutcome
    workflow_run_id: str | None = None


class WorkflowNotAllowlistedError(ValueError):
    """Raised when a dispatch() call targets a workflow or ref outside the
    fixed allowlist above. Must never happen during ordinary relay
    operation -- router.py only ever produces allowlisted values -- so
    this indicates a programming error or an attempted bypass, not an
    expected runtime condition a caller should catch and retry."""


class GitHubDispatcher(Protocol):
    """The only GitHub write operation this relay may ever perform.
    Deliberately has no method for anything else (no contents write, no
    issue/PR operation, no arbitrary REST call) -- this Protocol IS the
    entire GitHub-write surface available to the relay."""

    def dispatch(self, *, workflow: str, ref: str) -> DispatchResult: ...


@dataclass(frozen=True)
class DispatchCall:
    workflow: str
    ref: str


class FakeGitHubDispatcher:
    """Test/DRY_RUN-safe double. Never performs a real network call.

    scripted_outcomes maps a workflow name to an ordered queue of outcomes
    to return on successive dispatch() calls for that workflow; once a
    workflow's queue is exhausted (or if it was never given one),
    default_outcome is returned for every subsequent call. Thread-safe:
    the concurrency tests in tests/test_approval_relay.py call dispatch()
    from multiple real OS threads against a single shared instance.
    """

    def __init__(
        self,
        *,
        scripted_outcomes: Mapping[str, Sequence[DispatchOutcome]] | None = None,
        default_outcome: DispatchOutcome = DispatchOutcome.SUCCESS,
    ) -> None:
        self._queues: dict[str, list[DispatchOutcome]] = {
            workflow: list(outcomes)
            for workflow, outcomes in (scripted_outcomes or {}).items()
        }
        self._default_outcome = default_outcome
        self._lock = Lock()
        self.calls: list[DispatchCall] = []

    def dispatch(self, *, workflow: str, ref: str) -> DispatchResult:
        if workflow not in ALLOWED_WORKFLOWS:
            raise WorkflowNotAllowlistedError(
                f"workflow not allowlisted: {workflow!r}"
            )
        if ref != DISPATCH_REF:
            raise WorkflowNotAllowlistedError(f"ref not allowlisted: {ref!r}")
        with self._lock:
            self.calls.append(DispatchCall(workflow=workflow, ref=ref))
            call_number = len(self.calls)
            queue = self._queues.get(workflow)
            outcome = queue.pop(0) if queue else self._default_outcome
        run_id = (
            f"fake-run-{call_number}" if outcome is DispatchOutcome.SUCCESS else None
        )
        return DispatchResult(outcome=outcome, workflow_run_id=run_id)
