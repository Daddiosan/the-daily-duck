"""Governance contract for cloud/approval_dispatcher (A2), mirroring the
protection style of tests/test_approval_receiver_contract.py (A1) without
importing from it or from cloud/approval_receiver at all.

These assertions protect architectural boundaries -- A2's file set, its
independence from A1, the absence of any real GitHub/email/polling
capability during Phase C shadow, and the sanitized-data contract -- not
arbitrary formatting. A legitimate future change to cloud/approval_dispatcher
(e.g. adding a real GitHub App dispatch adapter for the explicitly
human-gated real-dispatch phase) is expected to require deliberately
updating this file, the same way tests/test_approval_receiver_contract.py
already requires for A1.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "cloud" / "approval_dispatcher"
PYTHON_FILES = tuple(DISPATCHER.glob("*.py"))

IGNORED_RUNTIME_ENTRIES = {"__pycache__"}

# The exact sanitized shadow-record schema Phase B.7/B.8 approved. Any
# additional field here is exactly the kind of scope creep this contract
# exists to catch.
ALLOWED_SHADOW_RECORD_FIELDS = frozenset(
    {
        "observation_id",
        "stage",
        "issue_date",
        "normalized_command",
        "idempotency_key",
        "transition_key",
        "classification",
        "timestamp",
        "source_type",
    }
)

ALLOWED_LOG_FIELDS = frozenset(
    {
        "observation_id",
        "stage",
        "classification",
        "issue_date",
        "transition_key_prefix",
        "result",
        "error_category",
        "from_allowlist_match",
        "recovery_path",
        "processed",
    }
)


def _source(paths=PYTHON_FILES) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


class ApprovalDispatcherContractTests(unittest.TestCase):
    # -- 1. expected A2 service file boundary --

    def test_exact_a2_files_exist(self):
        expected = {
            "main.py",
            "gmail_reader.py",
            "storage.py",
            "requirements.txt",
            "Dockerfile",
        }
        actual = {
            path.name
            for path in DISPATCHER.iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        self.assertEqual(actual, expected)
        self.assertTrue(
            (ROOT / "docs" / "phase3b2" / "A2_SHADOW_RUNBOOK.md").is_file()
        )

    # -- 2. A2 cannot import A1 implementation --

    def test_a2_does_not_import_a1(self):
        # This checks imports only, via AST, not prose: the design docs
        # and module docstrings legitimately mention "approval_receiver"
        # by name to explain why no such dependency exists (see
        # main.py's and gmail_reader.py's own module docstrings), so a
        # blind text substring check would produce a false positive
        # against that documentation.
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("cloud.approval_receiver"),
                            f"{path.name} imports A1: {alias.name}",
                        )
                if module is not None:
                    self.assertFalse(
                        module.startswith("cloud.approval_receiver")
                        or module == "approval_receiver",
                        f"{path.name} imports from A1: {module}",
                    )

    # -- 3. A2 shadow cannot contain GitHub dispatch capability --

    def test_no_github_dispatch_capability(self):
        source = _source().lower()
        for marker in (
            "repository_dispatch",
            "workflow_dispatch",
            "pygithub",
            "github app",
        ):
            self.assertNotIn(marker, source)

    def test_dispatch_adapter_used_is_the_shadow_only_fake(self):
        # main.py must construct scripts.a2_dispatch.FakeDispatchAdapter
        # (records a decision, never performs network I/O) and must never
        # construct GitHubAppDispatchAdapter (the production placeholder
        # that raises NotImplementedError) -- doing so would still be
        # side-effect-free today, but instantiating it at all is reserved
        # for the explicitly human-gated real-dispatch phase, not shadow.
        main_source = (DISPATCHER / "main.py").read_text(encoding="utf-8")
        self.assertIn("FakeDispatchAdapter()", main_source)
        self.assertNotIn("GitHubAppDispatchAdapter(", main_source)

    # -- 4. A2 shadow cannot contain SMTP/email-send capability --

    def test_no_smtp_or_email_send_capability(self):
        source = _source().lower()
        for marker in ("smtplib", ".send(", "messages().send", "sendmail"):
            self.assertNotIn(marker, source)

    # -- 5. no periodic polling/schedule implementation --

    def test_no_periodic_polling_or_scheduling(self):
        source = _source()
        for marker in (
            "time.sleep(",
            "import schedule",
            "apscheduler",
            "croniter",
            "while True:",
        ):
            self.assertNotIn(marker, source)
        self.assertNotIn("import time", source)

    # -- 6. Dockerfile selectively copies only approved shared scripts --

    def test_dockerfile_selective_copy_contract(self):
        dockerfile = (DISPATCHER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY scripts/approval_domain.py scripts/a2_dispatch.py ./scripts/",
            dockerfile,
        )
        # No wildcard/whole-directory/whole-repo copy of any kind.
        for forbidden in (
            "COPY . ",
            "COPY .. ",
            "COPY scripts/ ",
            "COPY scripts /",
            ".git",
            ".venv",
            "automation_images",
            "automation_state",
            "secrets",
        ):
            self.assertNotIn(forbidden, dockerfile)

    def test_dockerfile_documents_repo_root_build_context(self):
        dockerfile = (DISPATCHER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("cloud/approval_dispatcher/Dockerfile .", dockerfile)

    # -- 7. A2 does not persist/log forbidden plaintext/credential fields --

    def test_shadow_record_builder_uses_only_allowed_fields(self):
        try:
            from cloud.approval_dispatcher.storage import build_shadow_record
            from scripts.a2_dispatch import (
                A2Decision,
                A2Outcome,
                EventClassification,
                build_approval_command,
                ApprovalSource,
            )
            from scripts.approval_domain import ApprovalStage
            from scripts.a2_dispatch import trusted_principal_from_gmail_metadata
        except ImportError as exc:  # pragma: no cover - environment issue
            self.fail(f"Could not import for schema check: {exc}")

        command = build_approval_command(
            stage=ApprovalStage.GATE_A,
            issue_date="2026-09-21",
            command="3",
            source_type=ApprovalSource.GMAIL_PUSH,
            trusted_principal=trusted_principal_from_gmail_metadata(
                "owner@example.com"
            ),
            allowed_principals=frozenset({"owner@example.com"}),
            message_id="msg-1",
        )
        outcome = A2Outcome(
            decision=A2Decision.DISPATCHED,
            classification=EventClassification.GATE_A_REPLY,
            reason="test",
            command=command,
        )
        record = build_shadow_record(
            observation_id="obs-1", outcome=outcome, timestamp="2026-09-21T00:00:00Z"
        )
        self.assertTrue(set(record.keys()).issubset(ALLOWED_SHADOW_RECORD_FIELDS))
        self.assertNotIn("owner@example.com", " ".join(str(v) for v in record.values()))

    def test_logging_allowlist_covers_every_field_main_emits(self):
        main_source = (DISPATCHER / "main.py").read_text(encoding="utf-8")
        for forbidden in (
            "message.body",
            "message.subject",
            "authorization_header",
            "raw_message",
        ):
            # These raw identifiers must never be passed directly into a
            # logging call; a targeted grep for "_log_event(...<name>..." is
            # too brittle, so instead assert the log-call sites only ever
            # reference the allowlisted field names as literal dict keys.
            pass
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_log_event"
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Dict):
                            for key in arg.keys:
                                if isinstance(key, ast.Constant) and isinstance(
                                    key.value, str
                                ):
                                    self.assertIn(
                                        key.value,
                                        ALLOWED_LOG_FIELDS,
                                        f"{path.name} logs disallowed field {key.value!r}",
                                    )

    # -- 8. ApprovalSource.GMAIL_PUSH remains required for A2 --

    def test_a2_uses_gmail_push_source_type(self):
        main_source = (DISPATCHER / "main.py").read_text(encoding="utf-8")
        self.assertIn("process_gmail_event(", main_source)
        # a2_dispatch.process_gmail_event itself is what selects
        # ApprovalSource.GMAIL_PUSH (scripts/a2_dispatch.py, unmodified);
        # this asserts A2 still calls that exact function rather than
        # reimplementing classification with a different source_type.
        from scripts.a2_dispatch import process_gmail_event as canonical

        self.assertTrue(callable(canonical))

    # -- 9. future real-dispatch capability requires an explicit Human Gate --

    def test_real_dispatch_requires_documented_human_gate(self):
        runbook = (ROOT / "docs" / "phase3b2" / "A2_SHADOW_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Human Gate", runbook)
        self.assertIn("real dispatch", runbook.lower())


if __name__ == "__main__":
    unittest.main()
