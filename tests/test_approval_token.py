from __future__ import annotations

import base64
import email
import json
import os
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from scripts.approval_token import (
    approval_token_digest,
    extract_approval_token,
    generate_approval_token,
    subject_with_approval_token,
    validate_approval_token,
)
from scripts.check_design_selection import find_reply as find_design_reply
from scripts.check_story_approval import find_valid_approval


ROOT = Path(__file__).resolve().parents[1]
ISSUE = "2026-09-26"
SENDER = "owner@example.com"


def raw_message(subject: str, body: str, *, sender: str = SENDER) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "daily@example.com"
    message["Subject"] = subject
    message.set_content(body)
    return message.as_bytes()


class FakeImap:
    def __init__(self, message: bytes):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, *args):
        return "OK", []

    def select(self, *args):
        return "OK", []

    def search(self, *args):
        return "OK", [b"1"]

    def fetch(self, *args):
        return "OK", [(b"1", self.message)]


class ApprovalTokenPrimitiveTests(unittest.TestCase):
    def test_token_contains_at_least_192_random_bits(self):
        token = generate_approval_token()
        decoded = base64.urlsafe_b64decode(token + "==")
        self.assertEqual(len(decoded), 24)
        self.assertEqual(len(token), 32)

    def test_digest_is_bound_to_stage_issue_and_batch(self):
        token = generate_approval_token()
        digest = approval_token_digest(
            stage="DESIGN_SELECTION", issue_date=ISSUE, batch=2, token=token
        )
        self.assertTrue(
            validate_approval_token(
                stored_digest=digest,
                stage="DESIGN_SELECTION",
                issue_date=ISSUE,
                batch=2,
                token=token,
            )
        )
        for stage, issue, batch in (
            ("GATE_A", ISSUE, None),
            ("DESIGN_SELECTION", "2026-09-25", 2),
            ("DESIGN_SELECTION", ISSUE, 1),
            ("DESIGN_SELECTION", ISSUE, 3),
        ):
            with self.subTest(stage=stage, issue=issue, batch=batch):
                self.assertFalse(
                    validate_approval_token(
                        stored_digest=digest,
                        stage=stage,
                        issue_date=issue,
                        batch=batch,
                        token=token,
                    )
                )

    def test_subject_round_trip_and_missing_or_malformed_token(self):
        token = generate_approval_token()
        subject = subject_with_approval_token("Approval prefix", token)
        self.assertEqual(extract_approval_token(subject), token)
        self.assertEqual(extract_approval_token(f"Re: {subject}"), token)
        self.assertIsNone(extract_approval_token("Approval prefix"))
        self.assertIsNone(extract_approval_token("Approval prefix — Approval Token short"))

    def test_public_state_contains_digest_not_raw_token(self):
        token = generate_approval_token()
        prefix = f"The Daily Duck — Choose Image + Title — {ISSUE} — Batch 1"
        state = {
            "issue_date": ISSUE,
            "preview_batch_number": 1,
            "final_email_subject_prefix": prefix,
            "approval_token_digest": approval_token_digest(
                stage="DESIGN_SELECTION", issue_date=ISSUE, batch=1, token=token
            ),
        }
        persisted = json.dumps(state)
        self.assertNotIn(token, persisted)
        self.assertNotIn(subject_with_approval_token(prefix, token), persisted)

    def test_sender_scripts_do_not_log_or_store_tokenized_subject(self):
        for relative in (
            "scripts/send_email.py",
            "scripts/send_design_approval_email.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('f"Subject: {subject}"', source)
        design_source = (ROOT / "scripts/send_design_approval_email.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('package["final_email_subject"] = subject', design_source)
        self.assertNotIn('package["email_subject"] = subject', design_source)


class ApprovalWorkflowTokenTests(unittest.TestCase):
    def test_gate_a_requires_current_token_sender_and_command(self):
        token = generate_approval_token()
        prefix = f"The Daily Duck — Choose Today's Story — {ISSUE}"
        subject = subject_with_approval_token(prefix, token)
        package = {
            "issue_date": ISSUE,
            "approval_token_digest": approval_token_digest(
                stage="GATE_A", issue_date=ISSUE, batch=None, token=token
            ),
        }
        with patch.dict(os.environ, {"EMAIL_TO": SENDER}, clear=False):
            found = find_valid_approval(FakeImap(raw_message(f"Re: {subject}", "3")), package)
            self.assertEqual(found, (3, SENDER, "3"))
            self.assertIsNone(
                find_valid_approval(FakeImap(raw_message(f"Re: {prefix}", "3")), package)
            )
            wrong = subject_with_approval_token(prefix, generate_approval_token())
            self.assertIsNone(
                find_valid_approval(FakeImap(raw_message(f"Re: {wrong}", "3")), package)
            )
            self.assertIsNone(
                find_valid_approval(FakeImap(raw_message(f"Re: {subject}", "Thanks")), package)
            )
            self.assertIsNone(
                find_valid_approval(
                    FakeImap(
                        raw_message(
                            f"Re: {subject}",
                            "3",
                            sender="attacker@example.com",
                        )
                    ),
                    package,
                )
            )

    def test_design_requires_current_batch_token_and_rotates_for_next_batch(self):
        token = generate_approval_token()
        prefix = f"The Daily Duck — Choose Image + Title — {ISSUE} — Batch 2"
        subject = subject_with_approval_token(prefix, token)
        digest = approval_token_digest(
            stage="DESIGN_SELECTION", issue_date=ISSUE, batch=2, token=token
        )
        message = raw_message(f"Re: {subject}", "1 3")
        env = {
            "EMAIL_TO": SENDER,
            "GMAIL_ADDRESS": "daily@example.com",
            "GMAIL_APP_PASSWORD": "not-a-real-password",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "scripts.check_design_selection.imaplib.IMAP4_SSL",
            return_value=FakeImap(message),
        ):
            found = find_design_reply(
                prefix,
                issue_date=ISSUE,
                batch_number=2,
                token_digest=digest,
            )
            self.assertEqual(found[:4], ("FINAL", 1, 3, SENDER))
            self.assertIsNone(
                find_design_reply(
                    prefix,
                    issue_date=ISSUE,
                    batch_number=1,
                    token_digest=digest,
                )
            )
        next_token = generate_approval_token()
        next_digest = approval_token_digest(
            stage="DESIGN_SELECTION", issue_date=ISSUE, batch=3, token=next_token
        )
        self.assertFalse(
            validate_approval_token(
                stored_digest=next_digest,
                stage="DESIGN_SELECTION",
                issue_date=ISSUE,
                batch=3,
                token=token,
            )
        )


if __name__ == "__main__":
    unittest.main()
