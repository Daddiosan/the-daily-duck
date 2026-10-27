import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from scripts import backfill_2026_10_26 as backfill


def story():
    return {
        "id": backfill.TARGET_STORY_ID,
        "candidate_number": backfill.TARGET_STORY_NUMBER,
        "title_en": backfill.TARGET_TITLE_EN,
        "title_ja": backfill.TARGET_TITLE_JA,
        "url": backfill.TARGET_SOURCE_URL,
    }


def approved_state():
    selected = story()
    options = [selected] + [{"id": number, "candidate_number": number} for number in range(2, 6)]
    return {
        "date": backfill.TARGET_ISSUE_DATE,
        "issue_date": backfill.TARGET_ISSUE_DATE,
        "state": "APPROVED_STORY",
        "approval_reply": "1",
        "selected_story_number": 1,
        "selected_story": selected,
        "gate_a_package": {
            "date": backfill.TARGET_ISSUE_DATE,
            "issue_date": backfill.TARGET_ISSUE_DATE,
            "approval_token_digest": backfill.TARGET_GATE_A_DIGEST,
            "story_options": options,
        },
    }


class FakeImap:
    def __init__(self, messages):
        self.messages = messages
        self.selected = None

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(str(i).encode() for i in range(1, len(self.messages) + 1))]

    def fetch(self, msg_id, query):
        index = int(msg_id) - 1
        return "OK", [(b"header", self.messages[index].as_bytes())]


class BackfillApprovalTests(unittest.TestCase):
    def test_exact_approved_story_is_accepted(self):
        selected = backfill.validate_approved_story(approved_state())
        self.assertEqual(selected["id"], 33)

    def test_wrong_issue_date_is_rejected(self):
        data = approved_state()
        data["date"] = data["issue_date"] = "2026-10-12"
        with self.assertRaisesRegex(backfill.BackfillValidationError, "refuses"):
            backfill.validate_approved_story(data)

    def test_non_exact_reply_is_rejected(self):
        data = approved_state()
        data["approval_reply"] = "1 thanks"
        with self.assertRaisesRegex(backfill.BackfillValidationError, "exact"):
            backfill.validate_approved_story(data)

    def test_wrong_token_digest_is_rejected(self):
        data = approved_state()
        data["gate_a_package"]["approval_token_digest"] = "0" * 64
        with self.assertRaisesRegex(backfill.BackfillValidationError, "digest"):
            backfill.validate_approved_story(data)


class DuplicateEmailTests(unittest.TestCase):
    def make_message(
        self,
        subject,
        *,
        recipient="reader@example.com",
        include_backfill_evidence=True,
    ):
        message = EmailMessage()
        message["From"] = "thedailyduckai@gmail.com"
        message["To"] = recipient
        message["Date"] = "Mon, 12 Oct 2026 10:00:00 +0900"
        message["Message-ID"] = "<design-evidence@example.com>"
        message["Subject"] = subject
        if include_backfill_evidence:
            message.set_content(
                "\n".join(
                    (
                        backfill.BACKFILL_APPROVAL_ID,
                        backfill.TARGET_TITLE_EN,
                        backfill.TARGET_TITLE_JA,
                        backfill.TARGET_SOURCE_URL,
                        "IMAGE_NUMBER TITLE_NUMBER",
                    )
                )
            )
            for number in range(1, 4):
                message.add_attachment(
                    b"png",
                    maintype="image",
                    subtype="png",
                    filename=f"DailyDuck_Image_{number}.png",
                )
        return message

    def test_matching_sent_email_blocks_duplicate(self):
        subject = backfill.DESIGN_SUBJECT_PREFIX + " — Approval Token " + "A" * 32
        evidence = backfill.find_existing_design_email(
            FakeImap([self.make_message(subject)]),
            sender="thedailyduckai@gmail.com",
            expected_recipients={"reader@example.com"},
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["message_id"], "<design-evidence@example.com>")

    def test_other_issue_does_not_block(self):
        subject = (
            "The Daily Duck — Choose Image + Title — 2026-10-12 — Batch 1"
            " — Approval Token " + "A" * 32
        )
        evidence = backfill.find_existing_design_email(
            FakeImap([self.make_message(subject)]),
            sender="thedailyduckai@gmail.com",
            expected_recipients={"reader@example.com"},
        )
        self.assertIsNone(evidence)

    def test_matching_email_is_found_beyond_250_messages(self):
        target = backfill.DESIGN_SUBJECT_PREFIX + " — Approval Token " + "A" * 32
        other = "Unrelated email"
        messages = [self.make_message(target)] + [
            self.make_message(other, include_backfill_evidence=False) for _ in range(300)
        ]
        evidence = backfill.find_existing_design_email(
            FakeImap(messages),
            sender="thedailyduckai@gmail.com",
            expected_recipients={"reader@example.com"},
        )
        self.assertIsNotNone(evidence)

    def test_wrong_recipient_or_body_does_not_block(self):
        subject = backfill.DESIGN_SUBJECT_PREFIX + " — Approval Token " + "A" * 32
        messages = [
            self.make_message(subject, recipient="wrong@example.com"),
            self.make_message(subject, include_backfill_evidence=False),
        ]
        evidence = backfill.find_existing_design_email(
            FakeImap(messages),
            sender="thedailyduckai@gmail.com",
            expected_recipients={"reader@example.com"},
        )
        self.assertIsNone(evidence)

    def test_post_send_verification_requires_sent_evidence(self):
        with mock.patch.object(backfill, "check_duplicate_via_gmail", return_value=None):
            with self.assertRaisesRegex(backfill.BackfillValidationError, "not found"):
                backfill.verify_sent_email(Path("missing-options.json"))

    def test_post_send_verification_returns_redacted_evidence(self):
        expected = {
            "message_id": "<sent@example.com>",
            "date": "today",
            "approval_token": "A" * 32,
        }
        with mock.patch.object(
            backfill, "check_duplicate_via_gmail", return_value=expected
        ):
            self.assertEqual(
                backfill.verify_sent_email(Path("missing-options.json")), expected
            )


class BackfillEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        previews = []
        for number in range(1, 4):
            path = Path("automation_images") / f"preview_{number}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"png")
            previews.append({"number": number, "image_path": path.as_posix()})
        self.package = {
            "state": "DESIGN_OPTIONS_READY",
            "issue_date": backfill.TARGET_ISSUE_DATE,
            "approved_story": approved_state(),
            "preview_batch_number": 1,
            "image_concepts": [
                {
                    "number": number,
                    "title_en": f"Concept {number}",
                    "concept_en": "Concept",
                    "composition_en": "Composition",
                    "title_ja": f"案 {number}",
                    "concept_ja": "案",
                    "composition_ja": "構図",
                }
                for number in range(1, 4)
            ],
            "title_ideas": [
                {"number": number, "title": f"TITLE {number}", "meaning_ja": "意味"}
                for number in range(1, 4)
            ],
            "design_previews": previews,
        }

    def test_email_contains_story_reply_format_and_backfill_id(self):
        subject, body, previews = backfill.build_backfill_email(self.package, "A" * 32)
        self.assertIn(backfill.TARGET_ISSUE_DATE, subject)
        self.assertIn(backfill.TARGET_TITLE_EN, body)
        self.assertIn(backfill.TARGET_TITLE_JA, body)
        self.assertIn("IMAGE_NUMBER TITLE_NUMBER", body)
        self.assertIn(backfill.BACKFILL_APPROVAL_ID, body)
        self.assertEqual(len(previews), 3)

    def test_send_refuses_when_duplicate_exists(self):
        with mock.patch.object(backfill, "check_duplicate_via_gmail", return_value={"message_id": "<x>"}):
            with mock.patch.object(backfill.design_email, "send_message") as send:
                result = backfill.send_email(Path("missing.json"))
        self.assertEqual(result, "ALREADY_SENT")
        send.assert_not_called()

    def test_dry_run_never_sends(self):
        path = Path("automation_state/design_options.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.package, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(backfill.design_email, "send_message") as send:
            backfill.dry_run_email(path)
        send.assert_not_called()

    def test_successful_send_persists_waiting_state_without_raw_token(self):
        path = Path("automation_state/design_options.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.package, ensure_ascii=False), encoding="utf-8")
        env = {
            "GMAIL_ADDRESS": "thedailyduckai@gmail.com",
            "GMAIL_APP_PASSWORD": "not-used",
            "EMAIL_TO": "reader@example.com",
            "GITHUB_RUN_ID": "123",
            "GITHUB_REPOSITORY": "Daddiosan/the-daily-duck",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(backfill, "check_duplicate_via_gmail", return_value=None):
                with mock.patch.object(
                    backfill, "generate_approval_token", return_value="A" * 32
                ):
                    with mock.patch.object(
                        backfill.design_email, "send_message", return_value=1
                    ):
                        result = backfill.send_email(path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result, "SENT")
        self.assertEqual(saved["state"], "WAITING_FINAL_SELECTION")
        self.assertEqual(saved["backfill"]["recovery_workflow_run_id"], "123")
        self.assertNotIn("A" * 32, path.read_text(encoding="utf-8"))

