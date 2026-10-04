from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import requests

from scripts import fetch_gate_a_artifact as artifact_fetch
from scripts.fetch_gate_a_artifact import (
    ArtifactSelectionError,
    GitHubReadClient,
    GitHubReadError,
    fetch_gate_a_artifact,
)


NOW = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
REPOSITORY = "owner/repository"


class FakeResponse:
    def __init__(self, status_code=200, *, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class ScriptedSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def archive(
    *,
    issue_date="2026-10-04",
    stories=5,
    extra=None,
    include_package=True,
    include_issue_date=True,
    package_changes=None,
):
    package = {
        "date": issue_date,
        "phase": 2,
        "state": "WAITING_STORY_SELECTION",
        "approval_token_digest": "a" * 64,
        "story_options": [
            {"candidate_number": number} for number in range(1, stories + 1)
        ],
    }
    if include_issue_date:
        package["issue_date"] = issue_date
    if package_changes:
        package.update(package_changes)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        if include_package:
            zipped.writestr("nested/gate_a_package.json", json.dumps(package))
        zipped.writestr("daily_duck_email.txt", "sanitized fixture")
        if extra:
            for name, value in extra.items():
                zipped.writestr(name, value)
    return output.getvalue()


def run_data(run_id, created_at, **changes):
    value = {
        "id": run_id,
        "created_at": created_at,
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
    }
    value.update(changes)
    return value


def artifact_data(artifact_id, **changes):
    value = {
        "id": artifact_id,
        "name": "daily-duck-results",
        "expired": False,
        "expires_at": "2026-10-10T00:00:00Z",
    }
    value.update(changes)
    return value


class GitHubReadClientTests(unittest.TestCase):
    def test_503_recovers_with_bounded_get_retry(self):
        session = ScriptedSession(
            FakeResponse(503, body={}),
            FakeResponse(200, body={"workflow_runs": []}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged",
            session=session,
            sleep=sleeps.append,
            clock=lambda: NOW,
        )
        result = client.get_json("repos/o/r/actions/runs", operation="list runs")
        self.assertEqual(result, {"workflow_runs": []})
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1.0])
        self.assertTrue(all(call[1]["allow_redirects"] for call in session.calls))

    def test_429_honors_retry_after(self):
        session = ScriptedSession(
            FakeResponse(429, body={}, headers={"Retry-After": "7"}),
            FakeResponse(200, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append, clock=lambda: NOW
        )
        self.assertEqual(client.get_json("x", operation="read"), {})
        self.assertEqual(sleeps, [7.0])

    def test_retry_after_http_date_is_honored(self):
        retry_at = format_datetime(NOW + timedelta(seconds=9), usegmt=True)
        session = ScriptedSession(
            FakeResponse(503, body={}, headers={"Retry-After": retry_at}),
            FakeResponse(200, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append, clock=lambda: NOW
        )
        self.assertEqual(client.get_json("x", operation="read"), {})
        self.assertEqual(sleeps, [9.0])

    def test_rate_limited_403_with_retry_after_is_retried(self):
        session = ScriptedSession(
            FakeResponse(403, body={}, headers={"Retry-After": "3"}),
            FakeResponse(200, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append, clock=lambda: NOW
        )
        self.assertEqual(client.get_json("x", operation="read"), {})
        self.assertEqual(sleeps, [3.0])

    def test_rate_limited_403_with_reset_is_retried(self):
        reset = str(int(NOW.timestamp()) + 5)
        session = ScriptedSession(
            FakeResponse(
                403,
                body={},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset},
            ),
            FakeResponse(200, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append, clock=lambda: NOW
        )
        self.assertEqual(client.get_json("x", operation="read"), {})
        self.assertEqual(sleeps, [5.0])

    def test_503_retry_is_bounded(self):
        session = ScriptedSession(
            FakeResponse(503, body={}),
            FakeResponse(503, body={}),
            FakeResponse(503, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append
        )
        with self.assertRaises(GitHubReadError) as caught:
            client.get_json("x", operation="read")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertIn("HTTP 503", str(caught.exception))

    def test_retry_after_above_local_bound_fails_without_early_retry(self):
        session = ScriptedSession(
            FakeResponse(429, body={}, headers={"Retry-After": "120"})
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append, clock=lambda: NOW
        )
        with self.assertRaises(GitHubReadError) as caught:
            client.get_json("x", operation="read")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 429", str(caught.exception))

    def test_ordinary_403_fails_immediately_and_sanitizes(self):
        session = ScriptedSession(FakeResponse(403, body={"token": "secret"}))
        client = GitHubReadClient(token="not-logged", session=session, sleep=lambda _: None)
        with self.assertRaises(GitHubReadError) as caught:
            client.get_json("x", operation="read artifact")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("not-logged", str(caught.exception))

    def test_transport_retry_is_bounded(self):
        failure = requests.ConnectionError("credential-looking response")
        session = ScriptedSession(failure, failure, failure)
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append
        )
        with self.assertRaises(GitHubReadError) as caught:
            client.get_json("x", operation="read")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertNotIn("credential-looking", str(caught.exception))

    def test_transport_failure_then_success(self):
        session = ScriptedSession(
            requests.ConnectionError("temporary"),
            FakeResponse(200, body={}),
        )
        sleeps = []
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=sleeps.append
        )
        self.assertEqual(client.get_json("x", operation="read"), {})
        self.assertEqual(sleeps, [1.0])

    def test_declared_oversized_byte_response_is_rejected_before_content_use(self):
        client = GitHubReadClient(
            token="not-logged",
            session=ScriptedSession(
                FakeResponse(content=b"", headers={"Content-Length": "9"})
            ),
            sleep=lambda _: None,
        )
        with self.assertRaises(ValueError):
            client.get_bytes("x", operation="download", max_bytes=8)


class ArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "artifact"

    def tearDown(self):
        self.temp.cleanup()

    def client(self, *responses):
        return GitHubReadClient(
            token="test-token",
            session=ScriptedSession(*responses),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )

    def test_skips_newest_missing_artifact_and_uses_next_usable_run(self):
        runs = {
            "workflow_runs": [
                run_data(300, "2026-10-03T23:30:00Z"),
                run_data(299, "2026-10-03T22:30:00Z"),
            ]
        }
        client = self.client(
            FakeResponse(body=runs),
            FakeResponse(body={"artifacts": []}),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive()),
        )
        selected = fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )
        self.assertEqual(selected.run_id, 299)
        self.assertEqual(selected.artifact_id, 901)
        self.assertTrue(selected.package_path.is_file())

    def test_expired_artifact_is_rejected_before_download(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901, expired=True)]}),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertIn("artifact_expired_or_invalid=1", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_stale_success_is_rejected_without_artifact_lookup(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(200, "2026-09-24T00:00:00Z")]}
            )
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertIn("stale_run=1", str(caught.exception))

    def test_invalid_newest_package_falls_back_to_valid_candidate(self):
        runs = {
            "workflow_runs": [
                run_data(300, "2026-10-03T23:30:00Z"),
                run_data(299, "2026-10-03T22:30:00Z"),
            ]
        }
        client = self.client(
            FakeResponse(body=runs),
            FakeResponse(body={"artifacts": [artifact_data(902)]}),
            FakeResponse(content=archive(stories=4)),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive()),
        )
        selected = fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )
        self.assertEqual(selected.run_id, 299)

    def test_output_directory_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive()),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_archive_path_traversal_is_rejected(self):
        malicious = archive(extra={"../outside.txt": "no"})
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=malicious),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertIn("artifact_content_invalid=1", str(caught.exception))
        self.assertFalse((Path(self.temp.name) / "outside.txt").exists())

    def assert_archive_member_rejected(self, member_name):
        with self.assertRaises(ValueError):
            artifact_fetch._validated_archive(
                archive(extra={member_name: "unsafe"}),
                "gate_a_package.json",
                "2026-10-04",
            )

    def test_windows_drive_relative_member_is_rejected(self):
        self.assert_archive_member_rejected("C:relative.txt")

    def test_windows_drive_relative_traversal_is_rejected(self):
        self.assert_archive_member_rejected("C:../escape.txt")

    def test_windows_drive_absolute_forward_slash_is_rejected(self):
        self.assert_archive_member_rejected("C:/absolute.txt")

    def test_windows_drive_absolute_backslash_is_rejected(self):
        self.assert_archive_member_rejected("C:\\absolute.txt")

    def test_ads_filename_is_rejected(self):
        self.assert_archive_member_rejected("file.txt:stream")

    def test_nested_ads_filename_is_rejected(self):
        self.assert_archive_member_rejected("folder/file.txt:stream")

    def test_posix_absolute_member_is_rejected(self):
        self.assert_archive_member_rejected("/absolute.txt")

    def test_backslash_traversal_is_rejected(self):
        self.assert_archive_member_rejected("folder\\..\\escape.txt")

    def test_nested_relative_member_is_accepted(self):
        files = artifact_fetch._validated_archive(
            archive(extra={"folder/nested/valid.txt": "safe"}),
            "gate_a_package.json",
            "2026-10-04",
        )
        self.assertIn(
            Path("folder/nested/valid.txt"),
            [path for path, _ in files],
        )

    def test_archive_symlink_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr(
                "gate_a_package.json",
                json.dumps(
                    {
                        "date": "2026-10-04",
                        "issue_date": "2026-10-04",
                        "phase": 2,
                        "state": "WAITING_STORY_SELECTION",
                        "approval_token_digest": "a" * 64,
                        "story_options": [
                            {"candidate_number": number}
                            for number in range(1, 6)
                        ],
                    }
                ),
            )
            link = zipfile.ZipInfo("folder/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(link, "../outside.txt")
        with self.assertRaises(ValueError):
            artifact_fetch._validated_archive(
                output.getvalue(),
                "gate_a_package.json",
                "2026-10-04",
            )

    def test_duplicate_archive_member_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("duplicate.txt", "first")
            zipped.writestr("DUPLICATE.txt", "second")
        with self.assertRaises(ValueError):
            artifact_fetch._validated_archive(
                output.getvalue(),
                "gate_a_package.json",
                "2026-10-04",
            )

    def test_wrong_expected_issue_is_rejected_even_when_run_is_one_minute_old(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:59:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026-10-03")),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertIn("artifact_content_invalid=1", str(caught.exception))

    def test_current_missing_then_previous_day_valid_fails_closed(self):
        client = self.client(
            FakeResponse(
                body={
                    "workflow_runs": [
                        run_data(300, "2026-10-03T23:30:00Z"),
                        run_data(299, "2026-10-03T01:00:00Z"),
                    ]
                }
            ),
            FakeResponse(body={"artifacts": []}),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026-10-03")),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_older_fresh_candidate_for_same_expected_issue_is_accepted(self):
        client = self.client(
            FakeResponse(
                body={
                    "workflow_runs": [
                        run_data(300, "2026-10-03T23:30:00Z"),
                        run_data(299, "2026-10-03T01:00:00Z"),
                    ]
                }
            ),
            FakeResponse(body={"artifacts": []}),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026-10-04")),
        )
        selected = fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )
        self.assertEqual(selected.run_id, 299)

    def test_missing_issue_date_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(include_issue_date=False)),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_malformed_issue_date_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026/10/04")),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_corrupt_zip_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=b"not a zip"),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_missing_package_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(include_package=False)),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_malformed_package_state_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(package_changes={"state": "PUBLISHED"})),
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )

    def test_download_404_falls_back_to_same_issue_candidate(self):
        client = self.client(
            FakeResponse(
                body={
                    "workflow_runs": [
                        run_data(300, "2026-10-03T23:30:00Z"),
                        run_data(299, "2026-10-03T22:30:00Z"),
                    ]
                }
            ),
            FakeResponse(body={"artifacts": [artifact_data(902)]}),
            FakeResponse(404, body={}),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive()),
        )
        selected = fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )
        self.assertEqual(selected.run_id, 299)

    def test_twenty_candidate_exhaustion_fails_closed(self):
        runs = [
            run_data(400 - number, f"2026-10-03T23:{59-number:02d}:00Z")
            for number in range(20)
        ]
        client = self.client(
            FakeResponse(body={"workflow_runs": runs}),
            *[FakeResponse(body={"artifacts": []}) for _ in runs],
        )
        with self.assertRaises(ArtifactSelectionError):
            fetch_gate_a_artifact(
                client=client,
                repository=REPOSITORY,
                output_dir=self.output,
                expected_issue_date="2026-10-04",
                now=NOW,
            )
        self.assertEqual(len(client._session.calls), 21)

    def test_twentieth_candidate_is_inside_max_candidate_boundary(self):
        runs = [
            run_data(400 - number, f"2026-10-03T23:{59-number:02d}:00Z")
            for number in range(20)
        ]
        responses = [FakeResponse(body={"workflow_runs": runs})]
        responses.extend(FakeResponse(body={"artifacts": []}) for _ in runs[:-1])
        responses.extend(
            [
                FakeResponse(body={"artifacts": [artifact_data(901)]}),
                FakeResponse(content=archive()),
            ]
        )
        client = self.client(*responses)
        selected = fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )
        self.assertEqual(selected.run_id, runs[-1]["id"])

    def test_oversized_archive_response_is_rejected(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=b"123456789"),
        )
        with patch.object(artifact_fetch, "MAX_ARCHIVE_RESPONSE_BYTES", 8):
            with self.assertRaises(ArtifactSelectionError):
                fetch_gate_a_artifact(
                    client=client,
                    repository=REPOSITORY,
                    output_dir=self.output,
                    expected_issue_date="2026-10-04",
                    now=NOW,
                )

    def test_excessive_zip_entry_count_is_rejected(self):
        excessive = archive(extra={f"extra/{number}.txt": "" for number in range(3)})
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=excessive),
        )
        with patch.object(artifact_fetch, "MAX_ARCHIVE_ENTRIES", 4):
            with self.assertRaises(ArtifactSelectionError):
                fetch_gate_a_artifact(
                    client=client,
                    repository=REPOSITORY,
                    output_dir=self.output,
                    expected_issue_date="2026-10-04",
                    now=NOW,
                )


class WorkflowContractTests(unittest.TestCase):
    def test_gate_a_uses_read_only_validated_artifact_fetcher(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/approval-check-phase2.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python scripts/fetch_gate_a_artifact.py", workflow)
        self.assertIn("--expected-issue-date", workflow)
        self.assertLess(
            workflow.index("git reset --hard origin/main"),
            workflow.index("python scripts/fetch_gate_a_artifact.py"),
        )
        self.assertLess(
            workflow.index("git reset --hard origin/main"),
            workflow.index("python scripts/check_story_approval.py"),
        )
        self.assertNotIn("gh run list", workflow)
        self.assertNotIn("gh run download", workflow)
        self.assertEqual(workflow.count("gh workflow run design-options.yml"), 1)
        source = (root / "scripts/fetch_gate_a_artifact.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("self._session.get(", source)
        self.assertNotIn("self._session.post(", source)
        self.assertNotIn("workflow_dispatch", source)


if __name__ == "__main__":
    unittest.main()
