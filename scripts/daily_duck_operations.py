"""Persist reminder limits and the morning generation pause on GitHub."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ALERT_INTERVAL = timedelta(hours=3)
DAILY_LIMIT = 6
PAUSE_AFTER = timedelta(days=20)
STATE_BRANCH = "daily-duck-operations-state"
STATE_FILE = "operations.json"


def timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def advance(
    state: dict, incident_keys: set[str], now: datetime
) -> tuple[dict, bool, bool]:
    """Return the next state, whether to send, and whether a pause just began."""
    started = {
        key: state["first_seen"].get(key, now.isoformat())
        for key in sorted(incident_keys)
    }
    newly_paused = not state["paused_at"] and any(
        now - datetime.fromisoformat(value) >= PAUSE_AFTER
        for value in started.values()
    )
    state = state | {"first_seen": started}
    if newly_paused:
        state["paused_at"] = now.isoformat()

    date = now.astimezone(JST).date().isoformat()
    count = state["send_count"] if state["send_date"] == date else 0
    last_send = timestamp(state["last_send_at"])
    should_send = bool(incident_keys or state["paused_at"]) and count < DAILY_LIMIT and (
        last_send is None or now - last_send >= ALERT_INTERVAL
    )
    if should_send:
        # Reserve the slot before contacting SMTP. An ambiguous SMTP result
        # or an Actions rerun must never exceed the send limits.
        state |= {
            "send_date": date,
            "send_count": count + 1,
            "last_send_at": now.isoformat(),
        }
    return state, should_send, newly_paused


def github_api(endpoint: str, payload: dict | None = None) -> dict:
    command = ["gh", "api", endpoint]
    if payload is not None:
        command += ["--method", "PUT", "--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def read_state(repository: str) -> tuple[dict, str]:
    response = github_api(f"repos/{repository}/contents/{STATE_FILE}?ref={STATE_BRANCH}")
    state = json.loads(base64.b64decode(response["content"]))
    if state.get("version") != 1:
        raise ValueError("Unrecognized Daily Duck operations state")
    return state, response["sha"]


def write_state(repository: str, state: dict, sha: str) -> None:
    content = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode()
    github_api(
        f"repos/{repository}/contents/{STATE_FILE}",
        {
            "message": "Update Daily Duck operations state",
            "branch": STATE_BRANCH,
            "content": base64.b64encode(content).decode(),
            "sha": sha,
        },
    )


def acknowledge(state: dict) -> dict:
    return state | {"first_seen": {}, "paused_at": None}


def main() -> None:
    command, = sys.argv[1:]
    repository = os.environ["GITHUB_REPOSITORY"]
    state, sha = read_state(repository)
    if command == "acknowledge":
        updated = acknowledge(state)
        if updated != state:
            write_state(repository, updated, sha)
        print("Response recorded. Morning generation is enabled.")
    elif command == "check-generation":
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"allowed={'false' if state['paused_at'] else 'true'}", file=output)
        if state["paused_at"]:
            print(f"Morning generation paused since {state['paused_at']}.")
    else:
        raise ValueError(f"Unknown operations command: {command}")


if __name__ == "__main__":
    main()
