import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.approval_shadow_compare import (
    CLASSIFICATIONS,
    classify,
    compare,
    correlate,
    load_observations,
    production_evidence,
)


BASE = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)


def observation(**changes):
    value = {
        "observation_type": "SHADOW_EVENT_DECISION",
        "observation_id": "obs-1",
        "observed_at": (BASE + timedelta(minutes=1)).isoformat(),
        "stage": "GATE_A",
        "issue_date": "2026-09-19",
        "design_batch_id": None,
        "canonical_command": "SELECT_STORY:3",
        "decision": "APPLY",
    }
    value.update(changes)
    return value


def evidence(**changes):
    value = {
        "stage": "GATE_A",
        "issue_date": "2026-09-19",
        "design_batch_id": None,
        "canonical_command": "SELECT_STORY:3",
        "outcome": "ACCEPT",
        "conflict": False,
        "evidence_sources": ["COMMITTED_STATE"],
    }
    value.update(changes)
    return value


class ApprovalShadowCompareTests(unittest.TestCase):
    def test_01_all_required_classifications_exist(self):
        self.assertEqual(len(CLASSIFICATIONS), 7)

    def test_02_accept_accept_is_match(self):
        result, _ = classify(observation(), evidence(), poller_completed_at=BASE + timedelta(minutes=2))
        self.assertEqual(result, "MATCH")

    def test_03_reject_reject_is_match(self):
        result, _ = classify(
            observation(decision="REJECT_CONFLICT", canonical_command=None),
            evidence(outcome="REJECT", canonical_command=None),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "MATCH")

    def test_04_event_accept_poller_reject(self):
        result, _ = classify(
            observation(),
            evidence(outcome="REJECT", canonical_command=None),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "EVENT_ACCEPT_POLLER_REJECT")

    def test_05_event_reject_poller_accept(self):
        result, _ = classify(
            observation(decision="REJECT_CONFLICT", canonical_command=None),
            evidence(),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "EVENT_REJECT_POLLER_ACCEPT")

    def test_06_command_mismatch(self):
        result, _ = classify(
            observation(),
            evidence(canonical_command="SELECT_STORY:2"),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "COMMAND_MISMATCH")

    def test_07_batch_mismatch(self):
        result, _ = classify(
            observation(stage="DESIGN_SELECTION", design_batch_id="4"),
            evidence(stage="DESIGN_SELECTION", design_batch_id="5"),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "BATCH_MISMATCH")

    def test_08_poller_before_event_is_timing_only(self):
        result, _ = classify(
            observation(observed_at=(BASE + timedelta(minutes=3)).isoformat()),
            evidence(),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "TIMING_ONLY")

    def test_09_conflicting_evidence_is_unknown(self):
        result, _ = classify(
            observation(),
            evidence(conflict=True),
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(result, "UNKNOWN")

    def test_10_missing_artifact_is_unknown(self):
        result = compare(
            [], evidence(), workflow_name="The Daily Duck - Gate A Approval Check",
            poller_run_id="8", poller_run_attempt="1", poller_created_at=BASE,
            poller_completed_at=BASE + timedelta(minutes=2), observed_at=BASE,
        )
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["reason"], "NO_CORRELATED_SHADOW_ARTIFACT")

    def test_11_ambiguous_artifacts_are_unknown(self):
        duplicate = observation(observation_id="obs-2")
        selected, error = correlate(
            [observation(), duplicate], evidence(),
            poller_created_at=BASE, poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertIsNone(selected)
        self.assertEqual(error, "AMBIGUOUS_SHADOW_ARTIFACTS")

    def test_12_out_of_window_artifact_is_not_correlated(self):
        old = observation(observed_at=(BASE - timedelta(hours=25)).isoformat())
        selected, error = correlate(
            [old], evidence(), poller_created_at=BASE,
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertIsNone(selected)
        self.assertEqual(error, "NO_CORRELATED_SHADOW_ARTIFACT")

    def test_13_gate_wait_success_is_reject_not_accept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text="issue_date: 2026-09-19\nSTATE: WAITING_STORY_SELECTION",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "REJECT")

    def test_14_gate_committed_state_is_accept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            (state / "approved_story.json").write_text(json.dumps({
                "issue_date": "2026-09-19", "state": "APPROVED_STORY", "approval_reply": "3"
            }))
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text="issue_date: 2026-09-19\nSTATE: APPROVED_STORY\nEXACT GATE A STORY SELECTION FOUND: 3",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "ACCEPT")
        self.assertEqual(result["canonical_command"], "SELECT_STORY:3")

    def test_15_state_log_conflict_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            (state / "approved_story.json").write_text(json.dumps({
                "issue_date": "2026-09-19", "state": "APPROVED_STORY", "approval_reply": "3"
            }))
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text="issue_date: 2026-09-19\nSTATE: WAITING_STORY_SELECTION",
                repo_root=root,
            )
        self.assertTrue(result["conflict"])
        self.assertEqual(result["outcome"], "UNKNOWN")

    def test_16_failed_poller_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="failure",
                log_text="issue_date: 2026-09-19\nSTATE: WAITING_STORY_SELECTION",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "UNKNOWN")

    def test_17_design_next_three_uses_consumed_batch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            (state / "design_options.json").write_text(json.dumps({
                "issue_date": "2026-09-19", "state": "WAITING_FINAL_SELECTION", "preview_batch_number": 5
            }))
            (state / "design_selection_result.json").write_text(json.dumps({
                "issue_date": "2026-09-19", "action": "NEXT_3_GENERATED"
            }))
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success", log_text="NEXT 3 accepted.", repo_root=root,
            )
        self.assertEqual(result["canonical_command"], "NEXT_3")
        self.assertEqual(result["design_batch_id"], "4")

    def test_18_unknown_workflow_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            result = production_evidence(
                workflow_name="Other", conclusion="success", log_text="",
                repo_root=Path(td),
            )
        self.assertEqual(result["outcome"], "UNKNOWN")

    def test_19_loader_ignores_malformed_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "a" / "observation.json").write_text("not json")
            (root / "b").mkdir()
            (root / "b" / "observation.json").write_text(json.dumps(observation()))
            loaded = load_observations(root)
        self.assertEqual(len(loaded), 1)

    def test_20_design_batch_narrows_multiple_candidates(self):
        selected, error = correlate(
            [
                observation(stage="DESIGN_SELECTION", design_batch_id="3"),
                observation(
                    observation_id="obs-4",
                    stage="DESIGN_SELECTION",
                    design_batch_id="4",
                ),
            ],
            evidence(stage="DESIGN_SELECTION", design_batch_id="4"),
            poller_created_at=BASE,
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertIsNone(error)
        self.assertEqual(selected["observation_id"], "obs-4")

    def test_21_observed_upstream_run_narrows_gate_candidates(self):
        selected, error = correlate(
            [
                observation(observed_upstream_run_id="10"),
                observation(observation_id="obs-2", observed_upstream_run_id="20"),
            ],
            evidence(upstream_run_id="20"),
            poller_created_at=BASE,
            poller_completed_at=BASE + timedelta(minutes=2),
        )
        self.assertIsNone(error)
        self.assertEqual(selected["observation_id"], "obs-2")

    def test_22_production_evidence_reader_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            path = state / "approved_story.json"
            path.write_text(json.dumps({
                "issue_date": "2026-09-19", "state": "APPROVED_STORY", "approval_reply": "3"
            }))
            before = path.read_bytes()
            production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text="issue_date: 2026-09-19\nSTATE: APPROVED_STORY",
                repo_root=root,
            )
            self.assertEqual(path.read_bytes(), before)

    def test_23_real_ready_to_publish_marker_is_accept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success",
                log_text="STATE: READY_TO_PUBLISH",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "ACCEPT")

    def test_24_real_already_selected_marker_is_accept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success",
                log_text="STATE: ALREADY_SELECTED",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "ACCEPT")

    def test_25_live_gate_already_approved_ignores_inactive_wait_source(self):
        raw_log = """\
2026-09-19T14:48:29.5347891Z ##[group]Run echo "Gate A approval check finished."
2026-09-19T14:48:29.5348323Z echo "Gate A approval check finished."
2026-09-19T14:48:29.5355641Z else
2026-09-19T14:48:29.5356236Z   echo "STATE: WAITING_STORY_SELECTION"
2026-09-19T14:48:29.5356539Z fi
2026-09-19T14:48:29.5398981Z ##[endgroup]
2026-09-19T14:48:29.5461101Z Gate A approval check finished.
2026-09-19T14:48:29.5024961Z This Gate A issue is already approved.
2026-09-19T14:48:29.5646423Z STATE: APPROVED_STORY
2026-09-19T14:48:29.5646958Z Selected story: 1
2026-09-19T14:48:29.5647295Z Issue date: 2026-09-19
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            (state / "approved_story.json").write_text(json.dumps({
                "issue_date": "2026-09-19",
                "state": "APPROVED_STORY",
                "approval_reply": "1",
            }))
            (state / "ready_to_publish.json").write_text(json.dumps({
                "issue_date": "2026-09-19",
                "state": "X_POSTED",
            }))
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )

        self.assertEqual(result["canonical_command"], "SELECT_STORY:1")
        self.assertEqual(result["outcome"], "ACCEPT")
        self.assertFalse(result["conflict"])
        comparison = compare(
            [observation(
                canonical_command="SELECT_STORY:1",
                decision="NO_OP_ALREADY_APPLIED",
            )],
            result,
            workflow_name="The Daily Duck - Gate A Approval Check",
            poller_run_id="35449839356",
            poller_run_attempt="1",
            poller_created_at=BASE,
            poller_completed_at=BASE + timedelta(minutes=2),
            observed_at=BASE + timedelta(minutes=2),
        )
        self.assertEqual(comparison["classification"], "MATCH")

    def test_26_new_gate_acceptance_ignores_inactive_wait_source(self):
        raw_log = """\
2026-09-19T01:00:00.0000000Z ##[group]Run if approved; then
2026-09-19T01:00:00.0000001Z   echo "STATE: WAITING_STORY_SELECTION"
2026-09-19T01:00:00.0000002Z fi
2026-09-19T01:00:00.0000003Z ##[endgroup]
2026-09-19T01:00:01.0000000Z issue_date: 2026-09-19
2026-09-19T01:00:01.0000001Z EXACT GATE A STORY SELECTION FOUND: 2
2026-09-19T01:00:01.0000002Z STATE: APPROVED_STORY
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )
        self.assertEqual(result["canonical_command"], "SELECT_STORY:2")
        self.assertEqual(result["outcome"], "ACCEPT")
        self.assertFalse(result["conflict"])

    def test_27_genuine_gate_wait_still_conflicts_with_committed_accept(self):
        raw_log = """\
2026-09-19T01:00:00Z issue_date: 2026-09-19
2026-09-19T01:00:01Z STATE: WAITING_STORY_SELECTION
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "automation_state"
            state.mkdir()
            (state / "approved_story.json").write_text(json.dumps({
                "issue_date": "2026-09-19",
                "state": "APPROVED_STORY",
                "approval_reply": "1",
            }))
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertTrue(result["conflict"])

    def test_28_design_runtime_wait_ignores_inactive_accept_source(self):
        raw_log = """\
2026-09-19T01:00:00Z ##[group]Run echo "Design action: WAIT"
2026-09-19T01:00:00Z echo "NEXT 3 accepted."
2026-09-19T01:00:00Z echo "STATE: READY_TO_PUBLISH"
2026-09-19T01:00:00Z echo "STATE: ALREADY_SELECTED"
2026-09-19T01:00:00Z echo "STATE: WAITING_FINAL_SELECTION"
2026-09-19T01:00:00Z ##[endgroup]
2026-09-19T01:00:01Z Design action: WAIT
2026-09-19T01:00:01Z STATE: WAITING_FINAL_SELECTION
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "REJECT")
        self.assertFalse(result["conflict"])

    def test_29_success_without_state_or_runtime_markers_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text="2026-09-19T01:00:00Z check completed",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertFalse(result["conflict"])

    def test_30_gate_source_code_only_cannot_create_acceptance(self):
        raw_log = """\
echo "STATE: APPROVED_STORY"
print("EXACT GATE A STORY SELECTION FOUND: 3")
    "STATE: APPROVED_STORY"
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Gate A Approval Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertNotIn("POLLER_LOG", result["evidence_sources"])

    def test_31_design_source_code_only_cannot_create_acceptance(self):
        raw_log = """\
echo "NEXT 3 accepted."
print("STATE: READY_TO_PUBLISH")
    "STATE: ALREADY_SELECTED"
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success",
                log_text=raw_log,
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertNotIn("POLLER_LOG", result["evidence_sources"])

    def test_32_real_design_action_wait_marker_is_reject(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "automation_state").mkdir()
            result = production_evidence(
                workflow_name="The Daily Duck - Design Selection Check",
                conclusion="success",
                log_text="Design action: WAIT",
                repo_root=root,
            )
        self.assertEqual(result["outcome"], "REJECT")
        self.assertFalse(result["conflict"])


if __name__ == "__main__":
    unittest.main()
