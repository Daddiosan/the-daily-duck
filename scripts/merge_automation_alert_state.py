#!/usr/bin/env python3
"""Record successfully emailed alert keys in cache-backed monitor state."""
import json
from pathlib import Path

state_path = Path("monitor_state/seen_alerts.json")
pending_path = Path("monitor_state/pending_alerts.json")
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    state = {"keys": []}
pending = json.loads(pending_path.read_text(encoding="utf-8"))
keys = set(state.get("keys", []))
keys.update(alert["key"] for alert in pending.get("alerts", []))
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps({"keys": sorted(keys)}, indent=2), encoding="utf-8")
print(f"Recorded {len(keys)} delivered alert key(s).")
