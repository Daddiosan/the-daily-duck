import ast
import hashlib
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECEIVER = ROOT / "cloud" / "approval_receiver"
PYTHON_FILES = tuple(RECEIVER.glob("*.py"))

# Runtime artifacts that legitimately appear alongside the source tree but
# are never part of the reviewed file set. Only names that a Python import
# itself creates belong here; anything else (a new source file, a stray
# config file, an unexpected subdirectory) must still fail the exact-set
# check below.
IGNORED_RUNTIME_ENTRIES = {"__pycache__"}

# The Phase 3B-2A1 approval baseline: the commit at which Human Gate
# approval was granted to push the canonical-status-v2 checkpoint while
# Phase 3B-2A1 existed only as untracked local work-in-progress (see that
# checkpoint commit's own message and the session's push-approval record).
# This is a fixed commit, not "current HEAD": pinning it here is what
# makes a protected-file change remain detectable by this test even after
# that change has been committed (and even if it has already been merged
# or pushed), which comparing against HEAD cannot do, because HEAD moves
# together with any such change.
#
# Everything reachable from this commit -- including any pre-existing,
# unrelated, already-legitimate history on these protected paths -- is
# baked into the baseline and never flagged; only a change to a protected
# path landing after this specific commit is a real question this test
# needs to surface. If a separate, legitimate workstream needs to modify
# one of these paths while Phase 3B-2A1 is still open, that is a genuine
# cross-workstream conflict for a human to resolve explicitly (by
# re-approving and updating this constant with its own approval record),
# not something this test should paper over by silently following HEAD.
#
# REAPPROVAL RECORD (Phase 3B-2 GMAIL_PUSH): this test correctly fired
# exactly as designed above when commit
# 23f54a4234bcb332f8f0b97bedb255b749235dd0
# ("Phase 3B-2: add explicit GMAIL_PUSH trust source") modified
# scripts/approval_domain.py and scripts/a2_dispatch.py -- a separate,
# legitimate workstream from Phase 3B-2A1, changing a protected path while
# this baseline was still pinned to the older commit above. The human
# explicitly reviewed that diff (ApprovalSource.GMAIL_PUSH added, mapped
# only to TrustedPrincipalSource.GMAIL_MESSAGE_METADATA; EVENT's and
# GMAIL_POLL's own authorization contracts left byte-for-byte unchanged;
# no workflow, cron, cloud, Dockerfile, or secret touched) and explicitly
# approved re-pinning the baseline to that commit. The baseline below is
# updated to it; the mechanism itself -- and its requirement that any
# further change to a protected path past this new baseline be surfaced
# for the same explicit human review -- is unchanged.
#
# POST-R2 P1 REAPPROVAL RECORD (2026-10-04): the stability remediation
# explicitly owns only the former section 4 of approval-check-phase2.yml so it
# can synchronize current main, establish the authoritative issue day, and
# replace the blind latest-success lookup with validated artifact selection.
# The generic workflow diff below excludes that one file, while the dedicated
# test immediately below compares its prefix and post-fetch suffix byte-for-byte
# with this baseline. The exception therefore does not permit unrelated edits.
#
# GATE A TERMINAL-NOOP REAPPROVAL RECORD (2026-10-06): the human explicitly
# approved a preflight before artifact retrieval and exact guards on Prepare,
# Gmail approval, and approval commit, plus a fixed no-op report block. The
# preflight/fetch section is pinned by digest and the suffix is reconstructed
# from the baseline with only those exact additions, so schedule, permissions,
# dispatch, failure notification, and all unrelated workflow behavior remain
# protected.
#
# GATE A MORNING-WAIT REAPPROVAL RECORD (2026-10-08): the human approved a
# 12:00 JST artifact deadline, an explicit non-failing waiting result before
# that deadline, ARTIFACT_READY guards on the existing Gate A side effects,
# and a fixed waiting report block. The failure notification remains the
# original unconditional failure() policy, and schedule/permissions are still
# pinned by the unchanged prefix/suffix reconstruction below.
PHASE_3B_2A1_APPROVED_BASELINE = "23f54a4234bcb332f8f0b97bedb255b749235dd0"
GATE_A_PREFLIGHT_SECTION_SHA256 = (
    "bae110d1e7c0e22dfa54a23bcc5a2a65d7f8699fb22dd98a6171c3d0f8900785"
)
GATE_A_PREFLIGHT_GUARD = (
    "        if: steps.preflight.outputs.action == 'CHECK_REQUIRED' && "
    "steps.dailyduck.outputs.artifact_result == 'ARTIFACT_READY'"
)
GATE_A_NOOP_REPORT = (
    '          if [ "${{ steps.preflight.outputs.action }}" = '
    '"NO_ACTION_REQUIRED" ]; then\n'
    '            echo "STATE: NO_ACTION_REQUIRED"\n'
    '            echo "Reason: ${{ steps.preflight.outputs.reason }}"\n'
    '            echo "Evidence: ${{ steps.preflight.outputs.evidence }}"\n'
    "            exit 0\n"
    "          fi\n\n"
)
GATE_A_WAIT_REPORT = (
    '          if [ "${{ steps.dailyduck.outputs.artifact_result }}" = '
    '"WAITING_FOR_CURRENT_ISSUE_ARTIFACT" ]; then\n'
    '            echo "STATE: WAITING_FOR_CURRENT_ISSUE_ARTIFACT"\n'
    '            echo "Reason: ${{ steps.dailyduck.outputs.reason }}"\n'
    "            exit 0\n"
    "          fi\n\n"
)


class ApprovalReceiverContractTests(unittest.TestCase):
    def test_exact_a1_files_exist(self):
        expected = {
            "main.py",
            "gmail_client.py",
            "observation.py",
            "requirements.txt",
            "Dockerfile",
        }
        actual = {
            path.name
            for path in RECEIVER.iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        self.assertEqual(actual, expected)
        self.assertTrue((ROOT / "docs" / "phase3b2" / "A1_RUNBOOK.md").is_file())

    def test_receiver_has_no_forbidden_imports(self):
        forbidden_roots = {
            "github",
            "smtplib",
            "imaplib",
            "subprocess",
            "approval_domain",
            "check_story_approval",
            "check_design_selection",
        }
        found = set()
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module.split(".")[0])
        self.assertTrue(forbidden_roots.isdisjoint(found), found)

    def test_no_git_or_subprocess_execution(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES)
        forbidden = ("subprocess", "os.system", "git commit", "git push")
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_no_github_client_or_ingress(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES).lower()
        for marker in ("repository_dispatch", "workflow_dispatch", "pygithub", "github app"):
            self.assertNotIn(marker, source)

    def test_no_smtp_imap_or_email_send(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8").lower()
        for marker in ("smtplib", "imaplib", ".send(", "messages().send"):
            self.assertNotIn(marker, source)

    def test_gmail_client_contains_no_mutation_api(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8")
        for marker in (
            ".modify(",
            ".delete(",
            ".trash(",
            ".untrash(",
            ".insert(",
            ".import_(",
            ".batchModify(",
            ".batchDelete(",
        ):
            self.assertNotIn(marker, source)

    def test_only_gmail_readonly_scope_declared(self):
        source = (RECEIVER / "gmail_client.py").read_text(encoding="utf-8")
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", source)
        self.assertNotIn("https://mail.google.com/", source)
        self.assertNotIn("gmail.modify", source)

    def test_no_production_repository_state_paths(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES)
        for marker in (
            "automation_state/",
            "ready_to_publish.json",
            "website_publish_result.json",
            "x_publish_result.json",
        ):
            self.assertNotIn(marker, source)

    def test_no_publish_or_downstream_capability(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in PYTHON_FILES).lower()
        for marker in ("publish_website", "publish_to_x", "x_publish", "website_publish"):
            self.assertNotIn(marker, source)

    def test_auth_does_not_trust_identity_headers(self):
        source = (RECEIVER / "main.py").read_text(encoding="utf-8")
        self.assertIn("verify_oauth2_token", source)
        self.assertNotIn("X-Goog-Authenticated-User", source)
        self.assertNotIn("X-Forwarded-Email", source)

    def test_existing_protected_files_match_baseline(self):
        """Phase 3B-2A1 must not modify its still-owned protected paths.

        R2A deliberately moved ``check_story_approval.py`` and
        ``check_design_selection.py`` into the one-time-token authorization
        boundary.  Their new invariants are enforced by
        ``test_approval_token.py``; the remaining A1-owned paths stay pinned
        here. This is checked against PHASE_3B_2A1_APPROVED_BASELINE, a
        fixed, named, human-approved commit -- never against "current
        HEAD". Comparing against HEAD would make a protected-file change
        invisible the moment it is committed, since HEAD and the change
        move together; that defeats the purpose of a post-commit,
        independent check, which must still be able to catch a change
        that already landed.

        This also intentionally does not use the repository's very first
        Phase 3B-1 commit as the baseline (an earlier version of this test
        pinned one). That would flag Phase 3B-1's own later, legitimate
        continuation of its own work on these same paths as a violation --
        exactly the false failure that pin produced. Anchoring at the
        commit where Phase 3B-2A1 itself was approved for its next
        checkpoint correctly excludes history that predates it (including
        that legitimate Phase 3B-1 continuation) while still catching any
        change to these paths from that point forward, committed or not.
        """
        command = [
            "git",
            "diff",
            "--name-only",
            PHASE_3B_2A1_APPROVED_BASELINE,
            "--",
            ".github/workflows",
            ":(exclude).github/workflows/approval-check-phase2.yml",
            "scripts/approval_domain.py",
            "scripts/approval_shadow.py",
            "scripts/approval_shadow_compare.py",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_gate_a_workflow_change_is_limited_to_approved_sections(self):
        path = ".github/workflows/approval-check-phase2.yml"
        result = subprocess.run(
            ["git", "show", f"{PHASE_3B_2A1_APPROVED_BASELINE}:{path}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        baseline = result.stdout
        current = (ROOT / path).read_text(encoding="utf-8")
        baseline_start = "      # 4. Find the latest successful Daily Duck Gate A run"
        current_start = (
            "      # 4. Synchronize authoritative state and determine the expected issue"
        )
        common_end = "      - name: Prepare Gate A package"
        self.assertIn(baseline_start, baseline)
        self.assertIn(current_start, current)
        self.assertIn(common_end, baseline)
        self.assertIn(common_end, current)
        self.assertEqual(
            baseline.split(baseline_start, 1)[0],
            current.split(current_start, 1)[0],
        )

        current_middle = current.split(current_start, 1)[1].split(common_end, 1)[0]
        self.assertEqual(
            hashlib.sha256(current_middle.encode()).hexdigest(),
            GATE_A_PREFLIGHT_SECTION_SHA256,
        )

        approved_suffix = baseline.split(common_end, 1)[1]
        approved_suffix = "\n" + GATE_A_PREFLIGHT_GUARD + approved_suffix
        for step_name in (
            "Check Gate A story selection",
            "Commit APPROVED_STORY when created",
        ):
            marker = f"      - name: {step_name}\n"
            self.assertEqual(approved_suffix.count(marker), 1)
            approved_suffix = approved_suffix.replace(
                marker,
                marker + GATE_A_PREFLIGHT_GUARD + "\n",
            )

        report_anchor = '          echo "Gate A approval check finished."\n\n'
        self.assertEqual(approved_suffix.count(report_anchor), 1)
        approved_suffix = approved_suffix.replace(
            report_anchor,
            report_anchor + GATE_A_NOOP_REPORT + GATE_A_WAIT_REPORT,
        )
        self.assertEqual(
            approved_suffix,
            current.split(common_end, 1)[1],
        )

    def test_dockerfile_runs_as_non_root(self):
        source = (RECEIVER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER receiver", source)
        self.assertNotIn("GMAIL_OAUTH_REFRESH_TOKEN=", source)


if __name__ == "__main__":
    unittest.main()
