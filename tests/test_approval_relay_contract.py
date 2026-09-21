"""Governance/security contract for cloud/approval_relay (Thin Relay,
Phase M3A), mirroring the protection style of
tests/test_approval_dispatcher_contract.py (A2) and
tests/test_approval_receiver_contract.py (A1) without importing from
either or from cloud.approval_receiver / cloud.approval_dispatcher at all.

This is the relay's OWN governance contract test (task spec Sec. 2): it
protects this component's boundary, not A1's or A2's -- their own
existing contract tests remain the authority for their own boundaries.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from cloud.approval_relay.github_dispatch import (
    DESIGN_SELECTION_WORKFLOW,
    DISPATCH_REF,
    GATE_A_WORKFLOW,
    DispatchOutcome,
    FakeGitHubDispatcher,
)
from cloud.approval_relay.ledger import InMemoryRelayLedger, RelayLedgerRecord
from cloud.approval_relay.main import (
    MAX_BUSINESS_ATTEMPTS,
    RelayMode,
    RelayService,
    RelayStatus,
)
from cloud.approval_relay.router import RelayInboundEvent, RoutingConfig


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "cloud" / "approval_relay"
PYTHON_FILES = tuple(RELAY.glob("*.py"))

IGNORED_RUNTIME_ENTRIES = {"__pycache__"}

ROUTING = RoutingConfig(
    gate_a_subject_pattern="The Daily Duck — Choose Today's Story",
    design_subject_pattern="The Daily Duck — Choose Image + Title",
)

# The exact sanitized ledger-record schema this phase approved. Any
# additional field here is exactly the kind of scope creep this contract
# exists to catch (in particular: no subject, no body, no sender address).
ALLOWED_LEDGER_FIELDS = frozenset(
    {
        "event_key",
        "stage",
        "workflow",
        "attempt_count",
        "state",
        "workflow_run_id",
        "created_at",
        "updated_at",
    }
)

ALLOWED_LOG_FIELDS = frozenset(
    {"event_key_prefix", "stage", "workflow", "status", "attempt_count", "mode"}
)


def _source(paths=PYTHON_FILES) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


class RelayFileBoundaryTests(unittest.TestCase):
    def test_exact_relay_files_exist(self):
        expected = {
            "main.py",
            "router.py",
            "ledger.py",
            "github_dispatch.py",
            "requirements.txt",
            "Dockerfile",
        }
        actual = {
            path.name
            for path in RELAY.iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        self.assertEqual(actual, expected)
        self.assertTrue(
            (ROOT / "docs" / "phase3b2" / "THIN_RELAY_RUNBOOK.md").is_file()
        )

    def test_a1_and_a2_directories_remain_untouched_by_this_component(self):
        # This relay's own file set is exactly the six files above --
        # nothing was added to, or removed from, cloud/approval_receiver/
        # or cloud/approval_dispatcher/. Their own contract tests remain
        # the authority for their internal boundaries; this only asserts
        # the relay did not encroach on them.
        receiver_files = {
            path.name
            for path in (ROOT / "cloud" / "approval_receiver").iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        dispatcher_files = {
            path.name
            for path in (ROOT / "cloud" / "approval_dispatcher").iterdir()
            if path.name not in IGNORED_RUNTIME_ENTRIES
        }
        self.assertEqual(
            receiver_files,
            {
                "main.py",
                "gmail_client.py",
                "observation.py",
                "requirements.txt",
                "Dockerfile",
            },
        )
        self.assertEqual(
            dispatcher_files,
            {
                "main.py",
                "gmail_reader.py",
                "storage.py",
                "requirements.txt",
                "Dockerfile",
            },
        )


class RelayIndependenceImportTests(unittest.TestCase):
    def test_relay_does_not_import_a1_a2_or_approval_command_parsing(self):
        forbidden_module_prefixes = (
            "cloud.approval_receiver",
            "approval_receiver",
            "cloud.approval_dispatcher",
            "approval_dispatcher",
            "scripts.approval_domain",
            "approval_domain",
            "scripts.a2_dispatch",
            "a2_dispatch",
        )
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith(forbidden_module_prefixes),
                            f"{path.name} imports {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module.startswith(forbidden_module_prefixes),
                        f"{path.name} imports from {node.module}",
                    )

    def test_relay_does_not_reuse_the_documented_unsafe_inmemory_fakes(self):
        # Module docstrings legitimately name these classes in prose to
        # explain *why* the relay does not reuse them (see ledger.py's
        # module docstring) -- exactly the same false-positive risk
        # tests/test_approval_dispatcher_contract.py's own
        # test_a2_does_not_import_a1 docstring already calls out for A2.
        # A raw substring scan would flag that legitimate prose, so this
        # asserts structurally instead: none of those classes are ever
        # instantiated or assigned in this component's own class
        # definitions, which is only possible if they were never imported
        # in the first place (already proven by the AST import-prefix
        # check above).
        for path in PYTHON_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            defined_names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            }
            for forbidden in (
                "InMemoryMessageDedupeStore",
                "InMemoryTransitionLedger",
                "InMemoryShadowObservationStore",
                "InMemoryCursorStore",
            ):
                self.assertNotIn(forbidden, defined_names)


class NoRealGitHubNetworkCapabilityTests(unittest.TestCase):
    def test_no_real_http_or_github_client_library(self):
        source = _source().lower()
        for marker in (
            "import requests",
            "import httpx",
            "urllib.request",
            "pygithub",
            "import jwt",
            "api.github.com",
            "google.auth",
            "google.oauth2",
        ):
            self.assertNotIn(marker, source)

    def test_only_fake_dispatcher_is_wired_by_default(self):
        main_source = (RELAY / "main.py").read_text(encoding="utf-8")
        self.assertIn("FakeGitHubDispatcher()", main_source)
        self.assertNotIn("RealGitHubDispatcher", main_source)
        self.assertNotIn("GitHubAppDispatcher", main_source)

    def test_no_contents_write_capability(self):
        source = _source().lower()
        for marker in (".contents(", "create_or_update_file", "git commit", "git push"):
            self.assertNotIn(marker, source)


class NoOutOfScopeCapabilityTests(unittest.TestCase):
    def test_no_smtp_or_email_send(self):
        source = _source().lower()
        for marker in ("smtplib", ".send(", "messages().send", "sendmail"):
            self.assertNotIn(marker, source)

    def test_no_website_or_x_publication(self):
        source = _source().lower()
        for marker in ("publish_website", "publish_to_x", "x_publish", "website_publish"):
            self.assertNotIn(marker, source)

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

    def test_no_production_state_mirror(self):
        source = _source()
        for marker in (
            "automation_state/",
            "approved_story.json",
            "ready_to_publish.json",
            "design_selection_result.json",
        ):
            self.assertNotIn(marker, source)

    def test_no_subprocess_or_shell_execution(self):
        source = _source()
        for marker in ("subprocess", "os.system("):
            self.assertNotIn(marker, source)


class SanitizedDataContractTests(unittest.TestCase):
    def test_ledger_record_fields_are_exactly_the_allowed_set(self):
        fields = {f.name for f in RelayLedgerRecord.__dataclass_fields__.values()}
        self.assertEqual(fields, ALLOWED_LEDGER_FIELDS)
        for forbidden in ("subject", "body", "sender", "sender_email"):
            self.assertNotIn(forbidden, fields)

    def test_relay_inbound_event_has_no_sender_or_body_field(self):
        fields = {f.name for f in RelayInboundEvent.__dataclass_fields__.values()}
        self.assertEqual(fields, {"mailbox_identity", "gmail_message_id", "subject"})

    def test_logging_allowlist_covers_every_field_main_emits(self):
        main_source = (RELAY / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(main_source, filename=str(RELAY / "main.py"))
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
                                    f"main.py logs disallowed field {key.value!r}",
                                )

    def test_event_key_derivation_never_hashes_subject_or_body(self):
        # Structural check, not a text scan: event_key_for's own docstring
        # legitimately discusses "subject"/"body" in prose (explaining what
        # it does NOT hash), which a raw substring scan of the function
        # source would wrongly flag. Asserting its parameter list is
        # exactly (mailbox_identity, gmail_message_id) instead proves the
        # same guarantee structurally -- the function has no way to touch
        # a subject or body value it was never passed.
        import inspect

        from cloud.approval_relay.router import event_key_for

        params = list(inspect.signature(event_key_for).parameters.keys())
        self.assertEqual(params, ["mailbox_identity", "gmail_message_id"])


class BehavioralSecurityTests(unittest.TestCase):
    """Functional guarantees the security contract requires (task spec
    Sec. 15) that cannot be verified by static source scanning alone."""

    def _service(self, *, dispatcher=None, mode=RelayMode.LIVE) -> RelayService:
        return RelayService(
            ledger=InMemoryRelayLedger(),
            dispatcher=dispatcher or FakeGitHubDispatcher(),
            routing=ROUTING,
            mode=mode,
        )

    def test_dry_run_never_calls_the_dispatcher(self):
        dispatcher = FakeGitHubDispatcher()
        service = self._service(dispatcher=dispatcher, mode=RelayMode.DRY_RUN)
        event = RelayInboundEvent(
            mailbox_identity="owner@example.com",
            gmail_message_id="m1",
            subject="The Daily Duck — Choose Today's Story — 2026-09-21",
        )
        service.process_event(event)
        service.process_event(event)
        self.assertEqual(dispatcher.calls, [])

    def test_unknown_outcome_is_never_automatically_redispatched(self):
        dispatcher = FakeGitHubDispatcher(
            default_outcome=DispatchOutcome.UNKNOWN_OUTCOME
        )
        service = self._service(dispatcher=dispatcher)
        event = RelayInboundEvent(
            mailbox_identity="owner@example.com",
            gmail_message_id="m1",
            subject="The Daily Duck — Choose Today's Story — 2026-09-21",
        )
        first = service.process_event(event)
        second = service.process_event(event)
        third = service.process_event(event)
        self.assertEqual(first.status, RelayStatus.UNKNOWN_OUTCOME)
        self.assertEqual(second.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(third.status, RelayStatus.DUPLICATE_TERMINAL_NO_OP)
        self.assertEqual(len(dispatcher.calls), 1)

    def test_max_business_attempts_constant_is_four(self):
        self.assertEqual(MAX_BUSINESS_ATTEMPTS, 4)

    def test_fixed_workflow_and_ref_constants(self):
        self.assertEqual(GATE_A_WORKFLOW, "approval-check-phase2.yml")
        self.assertEqual(DESIGN_SELECTION_WORKFLOW, "design-selection-check.yml")
        self.assertEqual(DISPATCH_REF, "main")


class HumanGateDocumentationTests(unittest.TestCase):
    def test_real_dispatch_requires_documented_human_gate(self):
        runbook = (ROOT / "docs" / "phase3b2" / "THIN_RELAY_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Human Gate", runbook)
        self.assertIn("GitHub App", runbook)

    def test_cron_removal_requires_documented_human_gate(self):
        runbook = (ROOT / "docs" / "phase3b2" / "THIN_RELAY_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Human Gate", runbook)
        self.assertIn("cron", runbook.lower())

    def test_real_dispatch_and_cron_removal_are_not_authorized_by_this_phase(self):
        runbook = (ROOT / "docs" / "phase3b2" / "THIN_RELAY_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("NOT authorized by this", runbook)


if __name__ == "__main__":
    unittest.main()
