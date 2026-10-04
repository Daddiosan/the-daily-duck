import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import publish_website

# The production workflow installs requests-oauthlib. The local Phase 3A test
# environment does not, and dependency installation is explicitly out of scope.
# publish_x only needs the OAuth1 symbol at import time; all network/auth calls
# are mocked below.
try:
    from scripts import publish_x
except ModuleNotFoundError as exc:
    if exc.name != "requests_oauthlib":
        raise
    oauth_stub = types.ModuleType("requests_oauthlib")
    oauth_stub.OAuth1 = object
    sys.modules["requests_oauthlib"] = oauth_stub
    from scripts import publish_x


ISSUE = "2026-09-19"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class WebsitePublishIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ready = self.root / "automation_state" / "ready_to_publish.json"
        self.result = self.root / "automation_state" / "website_publish_result.json"
        self.archive = self.root / "data" / "archive.json"
        self.today = self.root / "data" / "today.json"
        self.content = self.root / "data" / "content.js"
        self.assets = self.root / "assets" / "ducks"
        self.ducks = self.root / "ducks"
        self.canonical = self.root / "canonical.png"
        self.canonical.write_bytes(b"not-a-live-image")
        write_json(self.archive, [])

    def tearDown(self):
        self.tempdir.cleanup()

    def ready_value(self):
        return {
            "state": "READY_TO_PUBLISH",
            "issue_date": ISSUE,
            "canonical_image_path": str(self.canonical),
            "gate_a_approved_story": {"duck_name": "Safety Duck"},
        }

    def run_publish(self):
        item = {
            "date": ISSUE,
            "storyTitleEn": "Safe publication",
            "duckName": "Safety Duck",
        }
        with patch.multiple(
            publish_website,
            READY=self.ready,
            RESULT=self.result,
            ARCHIVE=self.archive,
            TODAY=self.today,
            CONTENT=self.content,
            ASSET_DIR=self.assets,
            DUCKS_DIR=self.ducks,
        ), patch.object(publish_website, "build_item", return_value=item), patch.object(
            publish_website, "render_page", return_value="<html></html>"
        ), patch.object(publish_website, "update_home"):
            return publish_website.main()

    def test_first_publish_allowed(self):
        write_json(self.ready, self.ready_value())
        self.assertEqual(self.run_publish(), 0)
        archive = json.loads(self.archive.read_text(encoding="utf-8"))
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["date"], ISSUE)
        self.assertEqual(result["action"], "PUBLISHED")

    def test_duplicate_issue_date_blocked(self):
        write_json(self.ready, self.ready_value())
        write_json(self.archive, [{"date": ISSUE}])
        self.assertEqual(self.run_publish(), 0)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["action"], "DUPLICATE_DATE_BLOCKED")

    def test_retry_does_not_create_duplicate_archive_entry(self):
        write_json(self.ready, self.ready_value())
        self.assertEqual(self.run_publish(), 0)
        write_json(self.ready, self.ready_value())
        self.assertEqual(self.run_publish(), 0)
        archive = json.loads(self.archive.read_text(encoding="utf-8"))
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(result["action"], "DUPLICATE_DATE_BLOCKED")


class XPublishIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ready = self.root / "automation_state" / "ready_to_publish.json"
        self.website = self.root / "automation_state" / "website_publish_result.json"
        self.result = self.root / "automation_state" / "x_publish_result.json"
        write_json(self.website, {"action": "PUBLISHED", "issue_date": ISSUE})

    def tearDown(self):
        self.tempdir.cleanup()

    def run_main(self, ready, *, remote=None):
        write_json(self.ready, ready)
        with patch.multiple(
            publish_x,
            READY_PATH=self.ready,
            WEBSITE_RESULT_PATH=self.website,
            X_RESULT_PATH=self.result,
        ), patch.object(
            publish_x, "resolve_image_path", return_value=self.root / "card.png"
        ), patch.object(publish_x, "build_post_text", return_value="post"), patch.object(
            publish_x, "oauth", return_value=object()
        ), patch.object(
            publish_x, "find_existing_post", return_value=remote
        ) as find_existing, patch.object(
            publish_x, "upload_image"
        ) as upload, patch.object(publish_x, "create_post") as create:
            upload.return_value = "media-2"
            create.return_value = ("post-2", {"data": {"id": "post-2"}})
            return publish_x.main(), find_existing, upload, create

    def test_first_x_publish_succeeds(self):
        result, find_existing, upload, create = self.run_main(
            {"state": "PUBLISHED", "issue_date": ISSUE}
        )
        self.assertEqual(result, 0)
        find_existing.assert_called_once()
        upload.assert_called_once()
        create.assert_called_once()
        ready = json.loads(self.ready.read_text(encoding="utf-8"))
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(ready["state"], "X_POSTED")
        self.assertEqual(ready["x_post_id"], "post-2")
        self.assertEqual(saved["action"], "X_POSTED")
        self.assertEqual(saved["x_post_id"], "post-2")

    def test_already_posted_local_state_blocks_all_x_calls(self):
        result, find_existing, upload, create = self.run_main(
            {
                "state": "PUBLISHED",
                "issue_date": ISSUE,
                "x_posted": True,
                "x_post_id": "post-1",
            }
        )
        self.assertEqual(result, 0)
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(saved["action"], "ALREADY_POSTED_BLOCKED")

    def test_terminal_x_result_survives_later_duplicate_noop(self):
        terminal = {
            "action": "X_POSTED",
            "at": "2026-09-30T12:00:00+00:00",
            "issue_date": ISSUE,
            "x_post_id": "post-1",
            "x_post_url": "https://x.com/i/web/status/post-1",
        }
        write_json(self.result, terminal)
        with patch.object(
            publish_x, "now_iso", return_value="2026-09-30T12:05:00+00:00"
        ):
            result, find_existing, upload, create = self.run_main(
                {
                    "state": "X_POSTED",
                    "issue_date": ISSUE,
                    "x_posted": True,
                    "x_post_id": "post-1",
                }
            )
        self.assertEqual(result, 0)
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(saved["action"], "X_POSTED")
        self.assertEqual(saved["at"], terminal["at"])
        self.assertEqual(saved["x_post_id"], "post-1")
        self.assertEqual(
            saved["post_terminal_observations"],
            [
                {
                    "action": "ALREADY_POSTED_TERMINAL_BLOCKED",
                    "at": "2026-09-30T12:05:00+00:00",
                    "issue_date": ISSUE,
                    "ready_state": "X_POSTED",
                    "x_post_id": "post-1",
                }
            ],
        )

    def test_terminal_x_result_survives_local_duplicate_guard(self):
        write_json(
            self.result,
            {
                "action": "X_POSTED",
                "at": "2026-09-30T12:00:00+00:00",
                "issue_date": ISSUE,
                "x_post_id": "post-1",
            },
        )
        result, find_existing, upload, create = self.run_main(
            {
                "state": "PUBLISHED",
                "issue_date": ISSUE,
                "x_posted": False,
            }
        )
        self.assertEqual(result, 0)
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(saved["action"], "X_POSTED")
        self.assertEqual(
            saved["post_terminal_observations"][-1]["action"],
            "ALREADY_POSTED_TERMINAL_BLOCKED",
        )
        self.assertEqual(saved["x_post_id"], "post-1")

    def test_terminal_x_result_blocks_corrupted_weaker_ready_state(self):
        write_json(
            self.result,
            {
                "action": "X_POSTED",
                "at": "2026-09-30T12:00:00+00:00",
                "issue_date": ISSUE,
                "x_post_id": "post-1",
            },
        )
        result, find_existing, upload, create = self.run_main(
            {"state": "CORRUPTED_WEAKER_STATE", "issue_date": ISSUE}
        )
        self.assertEqual(result, 0)
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(saved["action"], "X_POSTED")
        self.assertEqual(saved["x_post_id"], "post-1")

    def test_corrupted_terminal_missing_post_id_fails_closed(self):
        write_json(
            self.result,
            {
                "action": "X_POSTED",
                "at": "2026-09-30T12:00:00+00:00",
                "issue_date": ISSUE,
            },
        )
        write_json(self.ready, {"state": "PUBLISHED", "issue_date": ISSUE})
        with patch.multiple(
            publish_x,
            READY_PATH=self.ready,
            WEBSITE_RESULT_PATH=self.website,
            X_RESULT_PATH=self.result,
        ), patch.object(publish_x, "find_existing_post") as find_existing, patch.object(
            publish_x, "upload_image"
        ) as upload, patch.object(publish_x, "create_post") as create:
            with self.assertRaises(ValueError):
                publish_x.main()
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()

    def test_old_terminal_result_does_not_block_new_issue(self):
        write_json(
            self.result,
            {
                "action": "X_POSTED",
                "at": "2026-09-18T12:00:00+00:00",
                "issue_date": "2026-09-18",
                "x_post_id": "post-old",
            },
        )
        result, find_existing, upload, create = self.run_main(
            {"state": "PUBLISHED", "issue_date": ISSUE}
        )
        self.assertEqual(result, 0)
        find_existing.assert_called_once()
        upload.assert_called_once()
        create.assert_called_once()
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(saved["issue_date"], ISSUE)
        self.assertEqual(saved["x_post_id"], "post-2")

    def test_website_issue_mismatch_fails_closed_before_x_calls(self):
        write_json(
            self.website,
            {"action": "PUBLISHED", "issue_date": "2026-09-18"},
        )
        write_json(self.ready, {"state": "PUBLISHED", "issue_date": ISSUE})
        with patch.multiple(
            publish_x,
            READY_PATH=self.ready,
            WEBSITE_RESULT_PATH=self.website,
            X_RESULT_PATH=self.result,
        ), patch.object(publish_x, "find_existing_post") as find_existing, patch.object(
            publish_x, "upload_image"
        ) as upload, patch.object(publish_x, "create_post") as create:
            with self.assertRaises(ValueError):
                publish_x.main()
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()

    def test_nested_approved_issue_mismatch_fails_closed_before_x_calls(self):
        ready = {
            "state": "PUBLISHED",
            "issue_date": ISSUE,
            "gate_a_approved_story": {"issue_date": "2026-09-18"},
        }
        write_json(self.ready, ready)
        with patch.multiple(
            publish_x,
            READY_PATH=self.ready,
            WEBSITE_RESULT_PATH=self.website,
            X_RESULT_PATH=self.result,
        ), patch.object(publish_x, "find_existing_post") as find_existing, patch.object(
            publish_x, "upload_image"
        ) as upload, patch.object(publish_x, "create_post") as create:
            with self.assertRaises(ValueError):
                publish_x.main()
        find_existing.assert_not_called()
        upload.assert_not_called()
        create.assert_not_called()

    def test_terminal_observations_are_bounded_and_preserve_original_post(self):
        write_json(
            self.result,
            {
                "action": "X_POSTED",
                "at": "2026-09-30T12:00:00+00:00",
                "issue_date": ISSUE,
                "x_post_id": "post-1",
            },
        )
        with patch.object(publish_x, "X_RESULT_PATH", self.result):
            for sequence in range(25):
                publish_x.write_result(
                    "DUPLICATE_OBSERVED",
                    issue_date=ISSUE,
                    sequence=sequence,
                )
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        observations = saved["post_terminal_observations"]
        self.assertEqual(len(observations), 20)
        self.assertEqual(observations[0]["sequence"], 5)
        self.assertEqual(observations[-1]["sequence"], 24)
        self.assertEqual(saved["action"], "X_POSTED")
        self.assertEqual(saved["x_post_id"], "post-1")

    def test_remote_duplicate_detection_finds_canonical_url(self):
        me = Mock(status_code=200)
        me.json.return_value = {"data": {"id": "user-1"}}
        recent = Mock(status_code=200)
        recent.json.return_value = {
            "data": [
                {
                    "id": "post-77",
                    "text": f"Duck https://www.thedailyduck.ai/ducks/{ISSUE}/",
                }
            ]
        }
        with patch.object(publish_x.requests, "get", side_effect=[me, recent]) as get:
            found = publish_x.find_existing_post(ISSUE, object())
        self.assertEqual(
            found, ("post-77", "https://x.com/i/web/status/post-77")
        )
        self.assertEqual(get.call_count, 2)

    def test_retry_after_external_success_repairs_local_state_without_posting(self):
        result, find_existing, upload, create = self.run_main(
            {"state": "PUBLISHED", "issue_date": ISSUE},
            remote=("post-88", "https://x.com/i/web/status/post-88"),
        )
        self.assertEqual(result, 0)
        find_existing.assert_called_once()
        upload.assert_not_called()
        create.assert_not_called()
        ready = json.loads(self.ready.read_text(encoding="utf-8"))
        saved = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertTrue(ready["x_posted"])
        self.assertEqual(ready["state"], "X_POSTED")
        self.assertEqual(ready["x_post_id"], "post-88")
        self.assertEqual(saved["action"], "ALREADY_POSTED_REMOTE_BLOCKED")


if __name__ == "__main__":
    unittest.main()
