import email
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts import final_selection_backfill_2026_10_11 as backfill
from scripts.approval_token import approval_token_digest


TOKEN = "A" * 32


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@contextmanager
def fixture_root():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        approved = {
            "state": "APPROVED_STORY",
            "issue_date": backfill.TARGET_DATE,
            "selected_story_number": 5,
            "selected_story": {"title_en": backfill.TARGET_ARTICLE},
            "title_en": backfill.TARGET_ARTICLE,
            "title_ja": "微生物学者が教える、より安全な作り置きガイド",
            "en_copy": "English story body.",
            "jp_copy": "日本語本文。",
            "duck_en": "A practical duck comment.",
            "duck_jp": "実用的なダックコメント。",
            "x_en": "English social copy.",
            "x_jp": "日本語SNS文。",
            "source": "Phys.org",
            "source_url": "https://phys.org/example",
            "duck_name": "Meal Prep Duck",
        }
        concepts = [
            {"number": 1, "title_en": "One"},
            {"number": 2, "title_en": backfill.TARGET_CONCEPT},
            {"number": 3, "title_en": "Three"},
        ]
        titles = [
            {"number": 1, "title": "ONE"},
            {"number": 2, "title": backfill.TARGET_TITLE_TEXT},
            {"number": 3, "title": "THREE"},
        ]
        previews = []
        for number in range(1, 4):
            relative = Path(
                f"automation_images/design_previews/2026-10-11/"
                f"batch_01/preview_{number}.png"
            )
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"image-{number}".encode())
            previews.append(
                {
                    "number": number,
                    "concept_number": number,
                    "image_path": relative.as_posix(),
                    "sha256": backfill.sha256_file(path),
                }
            )
        options = {
            "state": "WAITING_FINAL_SELECTION",
            "issue_date": backfill.TARGET_DATE,
            "preview_batch_number": 1,
            "final_email_subject_prefix": backfill.SUBJECT_PREFIX,
            "approval_token_digest": approval_token_digest(
                stage="DESIGN_SELECTION",
                issue_date=backfill.TARGET_DATE,
                batch=1,
                token=TOKEN,
            ),
            "approved_story": approved,
            "image_concepts": concepts,
            "design_previews": previews,
            "title_ideas": titles,
            "selected_image_number": None,
            "selected_title_number": None,
            "backfill": {
                "recovery_workflow_run_id": str(backfill.SOURCE_RUN_ID),
                "isolated": True,
                "publication_allowed": False,
            },
        }
        write_json(root / backfill.APPROVED_PATH, approved)
        write_json(root / backfill.OPTIONS_PATH, options)
        metadata = {
            "id": backfill.SOURCE_ARTIFACT_ID,
            "name": backfill.SOURCE_ARTIFACT_NAME,
            "digest": backfill.SOURCE_ARTIFACT_DIGEST,
            "expired": False,
            "workflow_run": {
                "id": backfill.SOURCE_RUN_ID,
                "head_sha": backfill.SOURCE_COMMIT,
                "head_branch": "main",
            },
        }
        metadata_path = root / "source-artifact-metadata.json"
        write_json(metadata_path, metadata)
        run_metadata_path = root / "source-run-metadata.json"
        write_json(
            run_metadata_path,
            {
                "id": backfill.SOURCE_RUN_ID,
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "path": backfill.SOURCE_WORKFLOW_PATH,
                "head_sha": backfill.SOURCE_COMMIT,
                "head_branch": "main",
            },
        )
        expected = {
            backfill.APPROVED_PATH.as_posix(): backfill.sha256_file(root / backfill.APPROVED_PATH),
            backfill.OPTIONS_PATH.as_posix(): backfill.sha256_file(root / backfill.OPTIONS_PATH),
        }
        for preview in previews:
            expected[preview["image_path"]] = preview["sha256"]
        with patch.object(backfill, "EXPECTED_FILES", expected):
            yield root, metadata_path, run_metadata_path, options


def reply_message(*, token=TOKEN, sender=backfill.AUTHORIZED_SENDER, body="2 2"):
    message = email.message.EmailMessage()
    message["Subject"] = f"{backfill.SUBJECT_PREFIX} — Approval Token {token}"
    message["From"] = sender
    message["To"] = "thedailyduckai@gmail.com"
    message["Date"] = "Mon, 12 Oct 2026 12:17:16 +0900 (JST)"
    message["Message-ID"] = "<reply@example.test>"
    message.set_content(body + "\n\n> quoted original")
    return message


def evidence():
    return backfill.ReplyEvidence(
        sender=backfill.AUTHORIZED_SENDER,
        normalized_reply="2 2",
        gmail_message_id=backfill.GMAIL_MESSAGE_ID,
        gmail_thread_id=backfill.GMAIL_THREAD_ID,
        rfc_message_id="<reply@example.test>",
        reply_at=backfill.EXPECTED_REPLY_AT,
    )


class FinalSelectionBackfillTests(unittest.TestCase):
    def test_valid_2_2_is_accepted_with_existing_parser_and_token_validator(self):
        with fixture_root() as (_, _, _, options):
            found = backfill.validate_message(
                reply_message(),
                options,
                gmail_message_id=backfill.GMAIL_MESSAGE_ID,
                gmail_thread_id=backfill.GMAIL_THREAD_ID,
                allowed_senders={backfill.AUTHORIZED_SENDER},
            )
        self.assertEqual(found.normalized_reply, "2 2")

    def test_invalid_token_is_rejected(self):
        with fixture_root() as (_, _, _, options):
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_message(
                    reply_message(token="B" * 32), options,
                    gmail_message_id=backfill.GMAIL_MESSAGE_ID,
                    gmail_thread_id=backfill.GMAIL_THREAD_ID,
                    allowed_senders={backfill.AUTHORIZED_SENDER},
                )

    def test_wrong_sender_is_rejected(self):
        with fixture_root() as (_, _, _, options):
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_message(
                    reply_message(sender="attacker@example.test"), options,
                    gmail_message_id=backfill.GMAIL_MESSAGE_ID,
                    gmail_thread_id=backfill.GMAIL_THREAD_ID,
                    allowed_senders={backfill.AUTHORIZED_SENDER},
                )

    def test_wrong_issue_date_is_rejected(self):
        with fixture_root() as (root, metadata, run_metadata, _):
            value = json.loads((root / backfill.OPTIONS_PATH).read_text(encoding="utf-8"))
            value["issue_date"] = "2026-10-12"
            write_json(root / backfill.OPTIONS_PATH, value)
            expected = dict(backfill.EXPECTED_FILES)
            expected[backfill.OPTIONS_PATH.as_posix()] = backfill.sha256_file(root / backfill.OPTIONS_PATH)
            with patch.object(backfill, "EXPECTED_FILES", expected):
                with self.assertRaises(backfill.FinalSelectionError):
                    backfill.validate_source(root, metadata, run_metadata)

    def test_wrong_batch_is_rejected(self):
        with fixture_root() as (_, _, _, options):
            options["approval_token_digest"] = approval_token_digest(
                stage="DESIGN_SELECTION", issue_date=backfill.TARGET_DATE, batch=2, token=TOKEN
            )
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_message(
                    reply_message(), options,
                    gmail_message_id=backfill.GMAIL_MESSAGE_ID,
                    gmail_thread_id=backfill.GMAIL_THREAD_ID,
                    allowed_senders={backfill.AUTHORIZED_SENDER},
                )

    def test_wrong_gmail_identity_is_rejected(self):
        with fixture_root() as (_, _, _, options):
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_message(
                    reply_message(), options,
                    gmail_message_id="1",
                    gmail_thread_id=backfill.GMAIL_THREAD_ID,
                    allowed_senders={backfill.AUTHORIZED_SENDER},
                )

    def test_final_package_uses_image_2_title_2_and_equal_hash(self):
        with fixture_root() as (root, metadata, run_metadata, _):
            self.assertEqual(
                backfill.finalize(root, metadata, run_metadata, evidence()),
                "CREATED",
            )
            ready = json.loads((root / backfill.READY_PATH).read_text(encoding="utf-8"))
            report = backfill.validate_ready(root)
            self.assertEqual(ready["selected_image_number"], 2)
            self.assertEqual(ready["selected_title_number"], 2)
            self.assertEqual(ready["selected_title"], backfill.TARGET_TITLE_TEXT)
            self.assertEqual(
                backfill.sha256_file(root / backfill.SELECTED_PREVIEW),
                backfill.sha256_file(root / backfill.CANONICAL_IMAGE),
            )
            self.assertTrue(report["image_hash_match"])
            self.assertFalse(ready["publish_started"])

    def test_idempotent_local_rerun_does_not_change_ready_package(self):
        with fixture_root() as (root, metadata, run_metadata, _):
            backfill.finalize(root, metadata, run_metadata, evidence())
            before = (root / backfill.READY_PATH).read_bytes()
            self.assertEqual(
                backfill.finalize(root, metadata, run_metadata, evidence()),
                "ALREADY_READY",
            )
            self.assertEqual((root / backfill.READY_PATH).read_bytes(), before)

    def test_inconsistent_duplicate_selection_is_rejected(self):
        with fixture_root() as (root, metadata, run_metadata, _):
            write_json(root / backfill.READY_PATH, {"state": "READY_TO_PUBLISH"})
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.finalize(root, metadata, run_metadata, evidence())

    def test_existing_output_artifact_prevents_second_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            listing = Path(folder) / "artifacts.json"
            output = Path(folder) / "output.txt"
            write_json(listing, {"artifacts": [{
                "id": 99,
                "name": backfill.OUTPUT_ARTIFACT_NAME,
                "expired": False,
            }]})
            self.assertFalse(backfill.check_duplicate_artifacts(listing, output))
            self.assertIn("should_create=false", output.read_text(encoding="utf-8"))

    def test_website_compatibility_reports_selected_title_blocker(self):
        with fixture_root() as (root, metadata, run_metadata, _):
            backfill.finalize(root, metadata, run_metadata, evidence())
            report = json.loads((root / backfill.REPORT_PATH).read_text(encoding="utf-8"))
            finding = report["publication_compatibility"]
            self.assertEqual(finding["status"], "BLOCKED_SELECTED_TITLE_NOT_USED_BY_WEBSITE")
            self.assertEqual(finding["selected_title"], backfill.TARGET_TITLE_TEXT)
            self.assertEqual(finding["website_story_title"], backfill.TARGET_ARTICLE)

    def test_source_artifact_metadata_is_exact(self):
        with fixture_root() as (_, metadata, _, _):
            backfill.validate_artifact_metadata(metadata)
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["id"] += 1
            write_json(metadata, value)
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_artifact_metadata(metadata)

    def test_source_run_must_be_successful_expected_workflow(self):
        with fixture_root() as (_, _, run_metadata, _):
            backfill.validate_run_metadata(run_metadata)
            value = json.loads(run_metadata.read_text(encoding="utf-8"))
            value["conclusion"] = "failure"
            write_json(run_metadata, value)
            with self.assertRaises(backfill.FinalSelectionError):
                backfill.validate_run_metadata(run_metadata)


if __name__ == "__main__":
    unittest.main()
