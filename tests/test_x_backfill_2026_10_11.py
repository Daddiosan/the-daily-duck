import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from scripts import x_backfill_2026_10_11 as backfill


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BackfillFixture:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source"
        self.repo = root / "repo"
        self.work = root / "work"
        self.artifact_metadata = root / "artifact.json"
        self.run_metadata = root / "run.json"
        self.website_run_metadata = root / "website-run.json"

        image_path = self.source / backfill.SOURCE_IMAGE_REL
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 900), "#f6c343").save(image_path, "PNG")
        self.image_hash = file_hash(image_path)

        ready = {
            "state": "READY_TO_PUBLISH",
            "issue_date": backfill.TARGET_DATE,
            "selected_title": backfill.TARGET_TITLE,
            "selected_title_number": backfill.TARGET_TITLE_NUMBER,
            "selected_image_number": backfill.TARGET_IMAGE_NUMBER,
            "selected_image_concept": {
                "number": backfill.TARGET_IMAGE_NUMBER,
                "title_en": backfill.TARGET_IMAGE_CONCEPT,
            },
            "canonical_image_path": backfill.SOURCE_IMAGE_REL.as_posix(),
            "selected_preview": {
                "number": backfill.TARGET_IMAGE_NUMBER,
                "concept_number": backfill.TARGET_IMAGE_NUMBER,
                "sha256": self.image_hash,
            },
            "final_selection_backfill": {
                "recovery_workflow_run_id": str(backfill.SOURCE_RUN_ID),
                "canonical_image_sha256": self.image_hash,
            },
            "gate_a_approved_story": {
                "issue_date": backfill.TARGET_DATE,
                "duck_name": "The Meal-Prep Duck",
                "x_en": (
                    "Meal prep made safer: a microbiologist shares a step-by-step "
                    "guide for everyday cooks. Practical advice can make daily routines "
                    "feel a little easier."
                ),
                "source": "Phys.org",
                "source_url": backfill.TARGET_SOURCE_URL,
            },
        }
        write_json(self.source / backfill.SOURCE_READY_REL, ready)
        self.ready_hash = file_hash(self.source / backfill.SOURCE_READY_REL)
        write_json(
            self.source / backfill.SOURCE_REPORT_REL,
            {
                "state": "READY_TO_PUBLISH",
                "issue_date": backfill.TARGET_DATE,
                "selected_title": backfill.TARGET_TITLE,
                "selected_image_number": backfill.TARGET_IMAGE_NUMBER,
                "selected_image_concept": backfill.TARGET_IMAGE_CONCEPT,
                "image_hash_match": True,
            },
        )
        write_json(
            self.artifact_metadata,
            {
                "id": backfill.SOURCE_ARTIFACT_ID,
                "name": backfill.SOURCE_ARTIFACT_NAME,
                "digest": backfill.SOURCE_ARTIFACT_DIGEST,
                "expired": False,
                "workflow_run": {"id": backfill.SOURCE_RUN_ID},
            },
        )
        write_json(
            self.run_metadata,
            {
                "id": backfill.SOURCE_RUN_ID,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": backfill.SOURCE_RUN_HEAD_SHA,
            },
        )
        write_json(
            self.website_run_metadata,
            {
                "id": backfill.WEBSITE_RUN_ID,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": backfill.WEBSITE_RUN_HEAD_SHA,
            },
        )

        published = self.repo / backfill.PUBLISHED_IMAGE_REL
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_bytes(image_path.read_bytes())
        write_json(
            self.repo / backfill.ARCHIVE_RESULT_REL,
            {
                "action": "ARCHIVE_ONLY_PUBLISHED",
                "issue_date": backfill.TARGET_DATE,
                "image": backfill.PUBLISHED_IMAGE_REL.as_posix(),
                "page": backfill.PUBLISHED_PAGE_REL.as_posix(),
                "title": backfill.TARGET_TITLE,
                "publication_mode": "ARCHIVE_ONLY",
                "x_publish_requested": False,
            },
        )
        write_json(
            self.repo / backfill.ARCHIVE_REL,
            [
                {
                    "date": backfill.TARGET_DATE,
                    "storyTitleEn": backfill.TARGET_TITLE,
                    "image": backfill.PUBLISHED_IMAGE_REL.as_posix(),
                }
            ],
        )
        page = self.repo / backfill.PUBLISHED_PAGE_REL
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"<title>{backfill.TARGET_TITLE}</title>{backfill.TARGET_ARTICLE_URL}",
            encoding="utf-8",
        )
        write_json(
            self.repo / backfill.SHARED_READY_REL,
            {
                "issue_date": "2026-10-10",
                "x_posted": True,
                "state": "X_POSTED",
            },
        )
        write_json(
            self.repo / backfill.SHARED_X_RESULT_REL,
            {
                "issue_date": "2026-10-10",
                "action": "X_POSTED",
                "x_post_id": "existing-10-10",
            },
        )

    def patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(backfill, "SOURCE_READY_SHA256", self.ready_hash))
        stack.enter_context(patch.object(backfill, "APPROVED_IMAGE_SHA256", self.image_hash))
        return stack

    def prepare(self):
        with self.patches():
            return backfill.prepare(
                source_dir=self.source,
                work_root=self.work,
                repository_root=self.repo,
                artifact_metadata_path=self.artifact_metadata,
                run_metadata_path=self.run_metadata,
                website_run_metadata_path=self.website_run_metadata,
                dry_run=True,
            )


class XBackfillPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.fixture = BackfillFixture(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_dry_run_builds_exact_card_and_text_without_http_post(self):
        shared_ready_before = file_hash(
            self.fixture.repo / backfill.SHARED_READY_REL
        )
        shared_result_before = file_hash(
            self.fixture.repo / backfill.SHARED_X_RESULT_REL
        )
        with patch.object(backfill.requests, "post") as http_post, patch.object(
            backfill.requests, "get"
        ) as http_get:
            state = self.fixture.prepare()
        http_post.assert_not_called()
        http_get.assert_not_called()
        self.assertEqual(state["state"], "READY_FOR_REVIEW")
        self.assertEqual(state["selection"]["selected_title"], backfill.TARGET_TITLE)
        self.assertEqual(state["x_card"]["size"], "1500x1200")
        self.assertEqual(state["x_card"]["ratio"], "5:4")
        self.assertEqual(state["x_card"]["title"], backfill.TARGET_TITLE)
        self.assertTrue((self.fixture.work / backfill.BACKFILL_CARD_REL).is_file())
        self.assertEqual(
            state["x_post"]["article_url"], backfill.TARGET_ARTICLE_URL
        )
        self.assertTrue(state["x_post"]["text"].endswith(backfill.TARGET_ARTICLE_URL))
        self.assertLessEqual(state["x_post"]["weighted_length"], 280)
        self.assertFalse(backfill.contains_cjk(state["x_post"]["text"]))
        self.assertEqual(
            file_hash(self.fixture.repo / backfill.SHARED_READY_REL),
            shared_ready_before,
        )
        self.assertEqual(
            file_hash(self.fixture.repo / backfill.SHARED_X_RESULT_REL),
            shared_result_before,
        )

    def test_exact_artifact_provenance_is_required(self):
        metadata = json.loads(self.fixture.artifact_metadata.read_text(encoding="utf-8"))
        metadata["id"] += 1
        write_json(self.fixture.artifact_metadata, metadata)
        with self.fixture.patches(), self.assertRaises(backfill.BackfillError):
            backfill.prepare(
                source_dir=self.fixture.source,
                work_root=self.fixture.work,
                repository_root=self.fixture.repo,
                artifact_metadata_path=self.fixture.artifact_metadata,
                run_metadata_path=self.fixture.run_metadata,
                website_run_metadata_path=self.fixture.website_run_metadata,
                dry_run=True,
            )

    def test_cross_date_source_is_rejected(self):
        ready_path = self.fixture.source / backfill.SOURCE_READY_REL
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["issue_date"] = "2026-10-12"
        write_json(ready_path, ready)
        self.fixture.ready_hash = file_hash(ready_path)
        with self.fixture.patches(), self.assertRaises(backfill.BackfillError):
            backfill.validate_source_artifact(self.fixture.source)

    def test_nested_approved_story_accepts_date_from_approved_root(self):
        ready_path = self.fixture.source / backfill.SOURCE_READY_REL
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        story = ready["gate_a_approved_story"]
        story.pop("issue_date")
        ready["gate_a_approved_story"] = {
            "issue_date": backfill.TARGET_DATE,
            "selected_story": story,
        }
        write_json(ready_path, ready)
        self.fixture.ready_hash = file_hash(ready_path)
        with self.fixture.patches():
            validated, source = backfill.validate_source_artifact(self.fixture.source)
        self.assertEqual(validated["issue_date"], backfill.TARGET_DATE)
        self.assertIn("Meal prep made safer", source["x_en"])

    def test_wrong_published_image_is_rejected(self):
        (self.fixture.repo / backfill.PUBLISHED_IMAGE_REL).write_bytes(b"wrong")
        with self.fixture.patches(), self.assertRaises(backfill.BackfillError):
            backfill.validate_website_publication(
                self.fixture.repo, self.fixture.website_run_metadata
            )

    def test_existing_terminal_state_is_preserved_and_blocks_rerun(self):
        state_path = self.fixture.work / backfill.BACKFILL_STATE_REL
        terminal = {
            "state": "X_POSTED",
            "issue_date": backfill.TARGET_DATE,
            "operation_id": backfill.OPERATION_ID,
            "x_post_id": "post-1",
        }
        write_json(state_path, terminal)
        with self.assertRaises(backfill.StateBlockedError):
            self.fixture.prepare()
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")), terminal
        )

    def test_future_shared_state_is_preserved_without_date_pin(self):
        write_json(
            self.fixture.repo / backfill.SHARED_READY_REL,
            {"issue_date": "2026-10-12", "state": "PUBLISHED", "x_posted": False},
        )
        write_json(
            self.fixture.repo / backfill.SHARED_X_RESULT_REL,
            {
                "issue_date": "2026-10-12",
                "action": "WEBSITE_STATE_NOT_PUBLISHED_BLOCKED",
            },
        )
        state = self.fixture.prepare()
        self.assertEqual(state["shared_x_state"]["ready_issue_date"], "2026-10-12")
        self.assertEqual(state["shared_x_state"]["result_issue_date"], "2026-10-12")
        self.assertTrue(state["shared_x_state"]["preserved"])

    def test_target_terminal_shared_state_blocks_backfill(self):
        write_json(
            self.fixture.repo / backfill.SHARED_X_RESULT_REL,
            {
                "issue_date": backfill.TARGET_DATE,
                "action": "X_POSTED",
                "x_post_id": "already-posted",
            },
        )
        with self.assertRaises(backfill.StateBlockedError):
            self.fixture.prepare()

    def test_target_terminal_shared_ready_blocks_backfill(self):
        write_json(
            self.fixture.repo / backfill.SHARED_READY_REL,
            {
                "issue_date": backfill.TARGET_DATE,
                "state": "X_POSTED",
                "x_posted": True,
                "x_post_id": "already-posted",
            },
        )
        with self.assertRaises(backfill.StateBlockedError):
            self.fixture.prepare()

    def test_concurrent_execution_lock_fails_closed(self):
        state_path = self.fixture.work / backfill.BACKFILL_STATE_REL
        lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("held", encoding="utf-8")
        with self.assertRaises(backfill.StateBlockedError):
            with backfill.operation_lock(state_path):
                self.fail("lock should not have been acquired")


class XBackfillDuplicateGuardTests(unittest.TestCase):
    def response(self, payload, status=200):
        response = Mock(status_code=status, text=json.dumps(payload))
        response.json.return_value = payload
        return response

    def test_paginated_entities_detect_existing_post(self):
        session = Mock()
        session.get.side_effect = [
            self.response({"data": {"id": "user-1"}}),
            self.response(
                {
                    "data": [{"id": "old", "text": "Something else"}],
                    "meta": {"next_token": "page-2"},
                }
            ),
            self.response(
                {
                    "data": [
                        {
                            "id": "duplicate",
                            "text": "A short t.co link",
                            "entities": {
                                "urls": [
                                    {"expanded_url": backfill.TARGET_ARTICLE_URL}
                                ]
                            },
                        }
                    ],
                    "meta": {},
                }
            ),
        ]
        result = backfill.find_existing_post_complete(object(), session=session)
        self.assertTrue(result["found"])
        self.assertEqual(result["post_id"], "duplicate")
        self.assertEqual(result["pages_scanned"], 2)
        self.assertEqual(session.get.call_count, 3)

    def test_complete_scan_returns_clear(self):
        session = Mock()
        session.get.side_effect = [
            self.response({"data": {"id": "user-1"}}),
            self.response({"data": [], "meta": {"result_count": 0}}),
        ]
        result = backfill.find_existing_post_complete(object(), session=session)
        self.assertFalse(result["found"])
        self.assertTrue(result["complete"])

    def test_tracking_url_and_missing_trailing_slash_are_duplicates(self):
        row = {
            "id": "post-1",
            "text": (
                "Read it: https://www.thedailyduck.ai/ducks/2026-10-11"
                "?utm_source=x"
            ),
        }
        self.assertTrue(backfill._row_contains_article_url(row))
        row["text"] = "Read it: https://thedailyduck.ai/ducks/2026-10-11/"
        self.assertTrue(backfill._row_contains_article_url(row))

    def test_http_200_partial_errors_fail_closed(self):
        session = Mock()
        session.get.return_value = self.response(
            {"data": {"id": "user-1"}, "errors": [{"detail": "partial"}]}
        )
        with self.assertRaises(backfill.DuplicateScanError):
            backfill.find_existing_post_complete(object(), session=session)

    def test_timeline_http_200_partial_errors_fail_closed(self):
        session = Mock()
        session.get.side_effect = [
            self.response({"data": {"id": "user-1"}}),
            self.response({"data": [], "meta": {}, "errors": [{"detail": "partial"}]}),
        ]
        with self.assertRaises(backfill.DuplicateScanError):
            backfill.find_existing_post_complete(object(), session=session)

    def test_read_failure_writes_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            write_json(
                state_path,
                {
                    "state": "READY_FOR_REVIEW",
                    "issue_date": backfill.TARGET_DATE,
                    "operation_id": backfill.OPERATION_ID,
                },
            )
            session = Mock()
            session.get.side_effect = requests.Timeout("rate or network timeout")
            state, can_post = backfill.check_duplicate(
                state_path, auth=object(), session=session
            )
            self.assertFalse(can_post)
            self.assertEqual(state["state"], "HOLD")
            self.assertEqual(state["duplicate_guard"]["status"], "READ_FAILED_HOLD")

    def test_missing_pagination_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            write_json(
                state_path,
                {
                    "state": "READY_FOR_REVIEW",
                    "issue_date": backfill.TARGET_DATE,
                    "operation_id": backfill.OPERATION_ID,
                },
            )
            session = Mock()
            session.get.side_effect = [
                self.response({"data": {"id": "user-1"}}),
                self.response({"data": [{"id": "post-1", "text": "other"}]}),
            ]
            state, can_post = backfill.check_duplicate(
                state_path, auth=object(), session=session
            )
            self.assertFalse(can_post)
            self.assertEqual(state["state"], "HOLD")


class XBackfillStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_path = self.root / "state.json"
        self.card_path = self.root / "card.png"
        self.card_path.write_bytes(b"card")

    def tearDown(self):
        self.tempdir.cleanup()

    def state(self, status="READY_FOR_REVIEW"):
        text = f"English text\n\n{backfill.TARGET_ARTICLE_URL}"
        return {
            "state": status,
            "issue_date": backfill.TARGET_DATE,
            "operation_id": backfill.OPERATION_ID,
            "publication_attempted": False,
            "x_card": {"sha256": file_hash(self.card_path)},
            "source_artifact": {
                "artifact_id": backfill.SOURCE_ARTIFACT_ID,
                "approved_image_sha256": backfill.APPROVED_IMAGE_SHA256,
            },
            "selection": {
                "selected_title": backfill.TARGET_TITLE,
                "selected_image_number": backfill.TARGET_IMAGE_NUMBER,
            },
            "x_post": {
                "text": text,
                "weighted_length": backfill.publish_x.x_weighted_length(text),
                "made_with_ai": True,
            },
            "duplicate_guard": {"status": "CLEAR_COMPLETE", "complete": True},
        }

    def test_actor_and_confirmation_gate_arm(self):
        write_json(self.state_path, self.state())
        with self.assertRaises(backfill.BackfillError):
            backfill.arm_post(
                self.state_path,
                confirmation="wrong",
                actor="Daddiosan",
                owner="Daddiosan",
            )
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["state"],
            "READY_FOR_REVIEW",
        )
        armed = backfill.arm_post(
            self.state_path,
            confirmation=backfill.CONFIRMATION,
            actor="Daddiosan",
            owner="daddiosan",
        )
        self.assertEqual(armed["state"], "POST_IN_FLIGHT")

    def test_unknown_create_result_forbids_automatic_retry(self):
        write_json(self.state_path, self.state("POST_IN_FLIGHT"))
        with patch.object(backfill.publish_x, "upload_image", return_value="media-1"), patch.object(
            backfill.publish_x,
            "create_post",
            side_effect=requests.Timeout("unknown result"),
        ), patch.object(
            backfill,
            "find_existing_post_complete",
            return_value={"found": False, "complete": True},
        ):
            with self.assertRaises(requests.Timeout):
                backfill.publish_armed_post(
                    self.state_path,
                    self.card_path,
                    confirmation=backfill.CONFIRMATION,
                    actor="Daddiosan",
                    owner="Daddiosan",
                    auth=object(),
                )
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "POST_RESULT_UNKNOWN")
        self.assertFalse(saved["automatic_retry_allowed"])

        with patch.object(backfill.publish_x, "upload_image") as upload, patch.object(
            backfill.publish_x, "create_post"
        ) as create:
            with self.assertRaises(backfill.BackfillError):
                backfill.publish_armed_post(
                    self.state_path,
                    self.card_path,
                    confirmation=backfill.CONFIRMATION,
                    actor="Daddiosan",
                    owner="Daddiosan",
                    auth=object(),
                )
        upload.assert_not_called()
        create.assert_not_called()

    def test_verified_success_uses_existing_made_with_ai_policy(self):
        write_json(self.state_path, self.state("POST_IN_FLIGHT"))
        auth = object()
        with patch.object(backfill.publish_x, "upload_image", return_value="media-1"), patch.object(
            backfill.publish_x,
            "create_post",
            return_value=("post-1", {"data": {"id": "post-1"}}),
        ) as create, patch.object(
            backfill,
            "find_existing_post_complete",
            return_value={"found": False, "complete": True},
        ):
            saved = backfill.publish_armed_post(
                self.state_path,
                self.card_path,
                confirmation=backfill.CONFIRMATION,
                actor="Daddiosan",
                owner="Daddiosan",
                auth=auth,
            )
        create.assert_called_once_with(self.state()["x_post"]["text"], "media-1", auth)
        self.assertEqual(saved["state"], "X_POSTED")
        self.assertEqual(saved["x_post_id"], "post-1")

    def test_final_duplicate_recheck_blocks_all_x_writes(self):
        write_json(self.state_path, self.state("POST_IN_FLIGHT"))
        duplicate = {
            "found": True,
            "post_id": "already-there",
            "post_url": "https://x.com/i/web/status/already-there",
            "pages_scanned": 1,
            "posts_scanned": 5,
            "scan_start": backfill.DUPLICATE_SCAN_START,
        }
        with patch.object(
            backfill, "find_existing_post_complete", return_value=duplicate
        ), patch.object(backfill.publish_x, "upload_image") as upload, patch.object(
            backfill.publish_x, "create_post"
        ) as create:
            saved = backfill.publish_armed_post(
                self.state_path,
                self.card_path,
                confirmation=backfill.CONFIRMATION,
                actor="Daddiosan",
                owner="Daddiosan",
                auth=object(),
            )
        upload.assert_not_called()
        create.assert_not_called()
        self.assertEqual(saved["state"], "DUPLICATE_FOUND")
        self.assertFalse(saved["publication_attempted"])


class XBackfillWorkflowContractTests(unittest.TestCase):
    def test_workflow_is_manual_fixed_and_isolated(self):
        workflow = Path(
            ".github/workflows/x-backfill-publish-2026-10-11.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn(str(backfill.SOURCE_ARTIFACT_ID), workflow)
        self.assertIn(str(backfill.SOURCE_RUN_ID), workflow)
        self.assertIn(backfill.CONFIRMATION, workflow)
        self.assertIn("github.actor", workflow)
        self.assertIn("github.triggering_actor", workflow)
        self.assertIn("github.repository_owner", workflow)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', workflow)
        self.assertIn("sha256sum --check --strict", workflow)
        self.assertIn(
            backfill.SOURCE_ARTIFACT_DIGEST.removeprefix("sha256:"), workflow
        )
        self.assertIn("group: daily-duck-x-publish", workflow)
        self.assertIn(backfill.BACKFILL_STATE_REL.as_posix(), workflow)
        self.assertIn(backfill.BACKFILL_CARD_REL.as_posix(), workflow)
        self.assertNotIn("scripts/publish_x.py", workflow)
        self.assertNotIn("website-publish.yml", workflow)


if __name__ == "__main__":
    unittest.main()
