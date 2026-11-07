"""Shared, fail-closed resolution of the human-selected English title."""
from __future__ import annotations

import re
from datetime import date
from typing import Any


def _selection_number(
    value: Any,
    field: str,
) -> int | None:

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError(
            f"{field} must be a positive integer."
        )

    return value


def _selected_title_text(
    value: Any,
    field: str,
) -> str:

    if not isinstance(value, str):
        raise ValueError(
            f"{field} must be a non-empty string."
        )

    selected = value.strip()

    if not selected or selected != value:
        raise ValueError(
            f"{field} must be a non-empty, trimmed string."
        )

    if selected != " ".join(selected.split()):
        raise ValueError(f"{field} must use single spaces between words.")

    if any(
        ord(character) < 32
        for character in selected
    ):
        raise ValueError(
            f"{field} contains control characters."
        )

    if "<" in selected or ">" in selected:
        raise ValueError(
            f"{field} contains HTML delimiter characters."
        )

    return selected


def _issue_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Publication issue_date must be YYYY-MM-DD.")
    date.fromisoformat(value)
    return value


def _check_issue_dates(issue: str, state: dict[str, Any]) -> None:
    for key in ("issue_date", "date"):
        if key in state and _issue_date(state[key]) != issue:
            raise ValueError("Publication issue dates conflict across the approval state.")
    for key in ("selected_story", "approved_story", "gate_a_approved_story",
                "story", "recommended_story", "selected_title_detail"):
        nested = state.get(key)
        if isinstance(nested, dict):
            _check_issue_dates(issue, nested)


def publication_title(
    ready: dict[str, Any],
    approved: dict[str, Any],
) -> str:
    """Resolve the approved English publication title, failing closed on conflicts."""

    issue = _issue_date(ready.get("issue_date"))
    _check_issue_dates(issue, ready)
    _check_issue_dates(issue, approved)

    if "selected_title" not in ready:
        raise ValueError(
            "Selected-title state is incomplete: selected_title is missing."
        )

    selected = _selected_title_text(
        ready.get("selected_title"),
        "selected_title",
    )
    selected_number = _selection_number(
        ready.get("selected_title_number"),
        "selected_title_number",
    )
    corroborating_numbers = [
        selected_number
    ] if selected_number is not None else []

    detail = ready.get("selected_title_detail")
    if "selected_title_detail" in ready:
        if not isinstance(detail, dict):
            raise ValueError(
                "selected_title_detail must be an object."
            )

        detail_title = _selected_title_text(
            detail.get("title"),
            "selected_title_detail.title",
        )
        if detail_title != selected:
            raise ValueError(
                "selected_title conflicts with selected_title_detail.title."
            )

        detail_number = _selection_number(
            detail.get("number"),
            "selected_title_detail.number",
        )
        if detail_number is not None:
            corroborating_numbers.append(
                detail_number
            )

    if "selected_title" in approved:
        approved_title = _selected_title_text(
            approved.get("selected_title"),
            "gate_a_approved_story.selected_title",
        )
        if approved_title != selected:
            raise ValueError(
                "selected_title conflicts with gate_a_approved_story.selected_title."
            )

    approved_number = _selection_number(
        approved.get("selected_title_number"),
        "gate_a_approved_story.selected_title_number",
    )
    if approved_number is not None:
        corroborating_numbers.append(
            approved_number
        )

    reply = ready.get("design_approval_reply")
    if reply is not None:
        if not isinstance(reply, str):
            raise ValueError(
                "design_approval_reply must be a string."
            )
        match = re.fullmatch(
            r"[1-9][0-9]*\s+([1-9][0-9]*)",
            reply.strip(),
        )
        if not match:
            raise ValueError(
                "design_approval_reply is invalid."
            )
        corroborating_numbers.append(
            int(match.group(1))
        )

    if len(set(corroborating_numbers)) > 1:
        raise ValueError(
            "Selected-title numbers conflict across the approval state."
        )

    for key in (
        "selected_story", "approved_story", "gate_a_approved_story",
        "story", "recommended_story",
    ):
        nested = approved.get(key)
        if isinstance(nested, dict) and (
            "selected_title" in nested or "selected_title_number" in nested
        ):
            publication_title(ready, nested)

    return selected
