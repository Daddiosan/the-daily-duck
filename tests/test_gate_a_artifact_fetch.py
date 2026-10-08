from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import stat
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from scripts import fetch_gate_a_artifact as artifact_fetch
from scripts.fetch_gate_a_artifact import (
    ArtifactFetchResult,
    ArtifactResultState,
    ArtifactSelectionError,
    GitHubReadClient,
    GitHubReadError,
    UpstreamArtifactTimeout,
    fetch_gate_a_artifact,
    resolve_gate_a_artifact,
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


API_HOST = "api.github.com"
ARCHIVE_HOST = "productionresultssa8.blob.core.windows.net"
SENTINEL_TOKEN = "sentinel-token-must-never-be-logged"
SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_gate_a_artifact.py"
)
RUNS_ROUTE = f"{API_HOST}/repos/{REPOSITORY}/actions/workflows/daily-duck.yml/runs"


def artifacts_route(run_id):
    return f"{API_HOST}/repos/{REPOSITORY}/actions/runs/{run_id}/artifacts"


def zip_route(artifact_id):
    return f"{API_HOST}/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"


def archive_route(artifact_id):
    return f"{ARCHIVE_HOST}/artifact-{artifact_id}.zip"


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def api_json(body, status=200, headers=None):
    return (
        status,
        {"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        json.dumps(body).encode("utf-8"),
    )


def api_redirect(artifact_id, signature="signed"):
    return (
        302,
        {"Location": f"https://{archive_route(artifact_id)}?sig={signature}"},
        b"",
    )


def archive_download(content, headers=None):
    return (
        200,
        {
            "Content-Type": "application/zip",
            "Content-Length": str(len(content)),
            **(headers or {}),
        },
        content,
    )


def _accepts_json(accept):
    for item in (accept or "*/*").split(","):
        media = item.split(";", 1)[0].strip().lower()
        if media in {"*/*", "application/*", "application/json"}:
            return True
        if media.startswith("application/vnd.github"):
            return True
    return False


class FakeGitHubTransport(BaseAdapter):
    """Offline model of GitHub's artifact archive download contract.

    Like the live API, the archive endpoint rejects a client that does not
    accept JSON with HTTP 415 before any redirect. Every other request is
    answered from the scripted routes, keyed by host and path.
    """

    def __init__(self, routes):
        super().__init__()
        self.routes = {key: list(values) for key, values in routes.items()}
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        parts = urlsplit(request.url)
        accept = request.headers.get("Accept")
        if (
            parts.netloc == API_HOST
            and "/actions/artifacts/" in parts.path
            and not _accepts_json(accept)
        ):
            status, headers, body = api_json(
                {
                    "message": (
                        f"Unsupported 'Accept' header: '{accept}'. "
                        "Must accept 'application/json'."
                    ),
                    "status": "415",
                },
                status=415,
            )
        else:
            status, headers, body = self.routes[parts.netloc + parts.path].pop(0)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response._content = body
        response._content_consumed = True
        response.url = request.url
        response.request = request
        response.connection = self
        return response

    def close(self):
        pass


def github_session(routes):
    transport = FakeGitHubTransport(routes)
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", transport)
    session.mount("http://", transport)
    return session, transport


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


class ArtifactWaitingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "artifact"

    def tearDown(self):
        self.temp.cleanup()

    def client(self, *responses, sleeps=None):
        return GitHubReadClient(
            token="test-token",
            session=ScriptedSession(*responses),
            sleep=(sleeps.append if sleeps is not None else lambda _: None),
            clock=lambda: NOW,
        )

    def resolve(self, client, *, now=NOW, output=None):
        return resolve_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=output or self.output,
            expected_issue_date="2026-10-04",
            now=now,
        )

    def test_case_1_prior_issue_only_before_noon_waits_successfully(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(299, "2026-10-03T01:00:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026-10-03")),
        )
        result = self.resolve(client)
        self.assertIs(
            result.state,
            ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT,
        )
        self.assertIsNone(result.selection)
        self.assertFalse(self.output.exists())

    def test_case_2_stale_only_response_before_noon_waits_successfully(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(200, "2026-09-24T00:00:00Z")]}
            )
        )
        result = self.resolve(client)
        self.assertIs(
            result.state,
            ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT,
        )

    def test_current_issue_run_in_progress_before_noon_waits_successfully(self):
        client = self.client(
            FakeResponse(
                body={
                    "workflow_runs": [
                        run_data(
                            300,
                            "2026-10-03T23:30:00Z",
                            status="in_progress",
                            conclusion=None,
                        )
                    ]
                }
            )
        )
        result = self.resolve(client)
        self.assertIs(
            result.state,
            ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT,
        )

    def test_case_3_current_valid_artifact_is_ready(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive()),
        )
        result = self.resolve(client)
        self.assertIs(result.state, ArtifactResultState.ARTIFACT_READY)
        self.assertEqual(result.selection.run_id, 300)

    def test_case_4_missing_current_artifact_at_noon_times_out(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(299, "2026-10-03T01:00:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=archive(issue_date="2026-10-03")),
        )
        with self.assertRaisesRegex(
            UpstreamArtifactTimeout,
            "UPSTREAM_ARTIFACT_TIMEOUT",
        ):
            self.resolve(client, now=datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc))

    def test_case_5_current_day_corrupt_artifact_fails_closed(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=b"not-a-zip"),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            self.resolve(client)
        self.assertNotIsInstance(caught.exception, UpstreamArtifactTimeout)
        self.assertFalse(caught.exception.wait_eligible)

    def test_current_day_duplicate_json_key_fails_closed(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr(
                "gate_a_package.json",
                '{"issue_date":"2026-10-04","issue_date":"2026-10-04"}',
            )
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": [artifact_data(901)]}),
            FakeResponse(content=output.getvalue()),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            self.resolve(client)
        self.assertFalse(caught.exception.wait_eligible)

    def test_case_6_current_day_run_without_artifact_fails_closed(self):
        client = self.client(
            FakeResponse(
                body={"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
            ),
            FakeResponse(body={"artifacts": []}),
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            self.resolve(client)
        self.assertFalse(caught.exception.wait_eligible)

    def test_case_7_transient_api_failure_is_retried_then_can_wait(self):
        sleeps = []
        client = self.client(
            FakeResponse(503, body={}),
            FakeResponse(body={"workflow_runs": []}),
            sleeps=sleeps,
        )
        result = self.resolve(client)
        self.assertIs(
            result.state,
            ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT,
        )
        self.assertEqual(sleeps, [1.0])

    def test_case_8_persistent_api_failure_after_retry_is_fatal(self):
        sleeps = []
        client = self.client(
            FakeResponse(503, body={}),
            FakeResponse(503, body={}),
            FakeResponse(503, body={}),
            sleeps=sleeps,
        )
        with self.assertRaises(GitHubReadError):
            self.resolve(client)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_case_10_duplicate_waiting_wakes_are_idempotent(self):
        states = []
        for number in range(2):
            client = self.client(FakeResponse(body={"workflow_runs": []}))
            result = self.resolve(
                client,
                output=Path(self.temp.name) / f"artifact-{number}",
            )
            states.append(result.state)
        self.assertEqual(
            states,
            [ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT] * 2,
        )

    def test_waiting_github_outputs_are_explicit_and_contain_no_artifact(self):
        result = ArtifactFetchResult(
            state=ArtifactResultState.WAITING_FOR_CURRENT_ISSUE_ARTIFACT,
            reason="CURRENT_ISSUE_ARTIFACT_NOT_READY",
        )
        github_output = Path(self.temp.name) / "github-output"
        artifact_fetch._append_github_output(github_output, result)
        self.assertEqual(
            github_output.read_text(encoding="utf-8"),
            "artifact_result=WAITING_FOR_CURRENT_ISSUE_ARTIFACT\n"
            "reason=CURRENT_ISSUE_ARTIFACT_NOT_READY\n",
        )


class GitHubArtifactDownloadContractTests(unittest.TestCase):
    """Regression coverage for the 2026-10-05 Gate A HTTP 415 incident."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "artifact"
        self.sleeps = []

    def tearDown(self):
        self.temp.cleanup()

    def client(self, routes):
        session, transport = github_session(routes)
        client = GitHubReadClient(
            token=SENTINEL_TOKEN,
            session=session,
            sleep=self.sleeps.append,
            clock=lambda: NOW,
        )
        return client, transport

    def download(self, client, artifact_id=901):
        return client.get_bytes(
            f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
            operation="download candidate artifact",
        )

    def select(self, client):
        return fetch_gate_a_artifact(
            client=client,
            repository=REPOSITORY,
            output_dir=self.output,
            expected_issue_date="2026-10-04",
            now=NOW,
        )

    def run_cli(self, routes, *, issue_date, github_output=None):
        session, transport = github_session(routes)
        argv = [
            str(SCRIPT_PATH),
            "--repository",
            REPOSITORY,
            "--output-dir",
            str(self.output),
            "--expected-issue-date",
            issue_date,
        ]
        if github_output is not None:
            argv.extend(["--github-output", str(github_output)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": SENTINEL_TOKEN}),
            patch.object(sys, "argv", argv),
            patch("requests.Session", return_value=session),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exited,
        ):
            runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
        return exited.exception.code, stdout.getvalue(), stderr.getvalue(), transport

    def test_archive_download_requests_github_json_media_type(self):
        session = ScriptedSession(FakeResponse(content=b"archive"))
        client = GitHubReadClient(
            token="not-logged", session=session, sleep=lambda _: None
        )
        self.assertEqual(self.download(client), b"archive")
        [(url, kwargs)] = session.calls
        self.assertEqual(
            url, f"https://{API_HOST}/repos/{REPOSITORY}/actions/artifacts/901/zip"
        )
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(
            kwargs["headers"]["X-GitHub-Api-Version"],
            artifact_fetch.GITHUB_API_VERSION,
        )
        self.assertTrue(kwargs["allow_redirects"])

    def test_api_redirect_is_followed_to_archive_bytes(self):
        payload = archive()
        client, transport = self.client(
            {
                zip_route(901): [api_redirect(901)],
                archive_route(901): [archive_download(payload)],
            }
        )
        self.assertEqual(self.download(client), payload)
        self.assertEqual(
            [urlsplit(request.url).netloc for request in transport.sent],
            [API_HOST, ARCHIVE_HOST],
        )
        self.assertEqual([request.method for request in transport.sent], ["GET", "GET"])
        self.assertEqual(
            transport.sent[0].headers["Accept"], "application/vnd.github+json"
        )
        self.assertEqual(self.sleeps, [])

    def test_archive_redirect_does_not_forward_authorization(self):
        client, transport = self.client(
            {
                zip_route(901): [api_redirect(901)],
                archive_route(901): [archive_download(archive())],
            }
        )
        self.download(client)
        api_request, archive_request = transport.sent
        self.assertTrue("Authorization" in api_request.headers)
        self.assertFalse("Authorization" in archive_request.headers)
        self.assertFalse(
            any(SENTINEL_TOKEN in value for value in archive_request.headers.values())
        )
        self.assertNotIn(SENTINEL_TOKEN, archive_request.url)

    def test_download_415_fails_closed_without_retry_or_leak(self):
        client, transport = self.client(
            {
                RUNS_ROUTE: [
                    api_json(
                        {"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
                    )
                ],
                artifacts_route(300): [api_json({"artifacts": [artifact_data(901)]})],
                zip_route(901): [
                    api_json({"message": SENTINEL_TOKEN, "status": "415"}, status=415)
                ],
            }
        )
        with self.assertRaises(GitHubReadError) as caught:
            self.select(client)
        self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(
            str(caught.exception),
            "GitHub read failed during download candidate artifact: HTTP 415.",
        )
        self.assertEqual(len(transport.sent), 3)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.output.exists())

    def test_cli_415_failure_output_is_sanitized(self):
        now = datetime.now(timezone.utc)
        issue_date = artifact_fetch._issue_date_for_run(now - timedelta(hours=1))
        code, stdout, stderr, transport = self.run_cli(
            {
                RUNS_ROUTE: [
                    api_json(
                        {
                            "workflow_runs": [
                                run_data(300, iso(now - timedelta(hours=1)))
                            ]
                        }
                    )
                ],
                artifacts_route(300): [
                    api_json(
                        {
                            "artifacts": [
                                artifact_data(
                                    901, expires_at=iso(now + timedelta(days=6))
                                )
                            ]
                        }
                    )
                ],
                zip_route(901): [
                    api_json({"message": SENTINEL_TOKEN, "status": "415"}, status=415)
                ],
            },
            issue_date=issue_date,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: GitHub read failed during download candidate artifact: "
            "HTTP 415.\n",
        )
        self.assertEqual(len(transport.sent), 3)
        self.assertFalse(self.output.exists())

    def test_cli_downloads_redirected_archive_for_expected_issue(self):
        now = datetime.now(timezone.utc)
        issue_date = artifact_fetch._issue_date_for_run(now - timedelta(hours=1))
        github_output = Path(self.temp.name) / "github_output"
        code, stdout, stderr, transport = self.run_cli(
            {
                RUNS_ROUTE: [
                    api_json(
                        {
                            "workflow_runs": [
                                run_data(300, iso(now - timedelta(hours=1)))
                            ]
                        }
                    )
                ],
                artifacts_route(300): [
                    api_json(
                        {
                            "artifacts": [
                                artifact_data(
                                    901, expires_at=iso(now + timedelta(days=6))
                                )
                            ]
                        }
                    )
                ],
                zip_route(901): [api_redirect(901)],
                archive_route(901): [
                    archive_download(archive(issue_date=issue_date))
                ],
            },
            issue_date=issue_date,
            github_output=github_output,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Gate A artifact result: ARTIFACT_READY", stdout)
        self.assertIn("run=300, artifact=901", stdout)
        self.assertNotIn(SENTINEL_TOKEN, stdout)
        self.assertTrue((self.output / "nested" / "gate_a_package.json").is_file())
        self.assertIn(
            "artifact_id=901", github_output.read_text(encoding="utf-8")
        )
        self.assertIn(
            "artifact_result=ARTIFACT_READY",
            github_output.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [request.method for request in transport.sent], ["GET"] * 4
        )
        self.assertEqual(
            [urlsplit(request.url).netloc for request in transport.sent],
            [API_HOST, API_HOST, API_HOST, ARCHIVE_HOST],
        )

    def test_transient_api_503_on_download_is_retried(self):
        payload = archive()
        client, transport = self.client(
            {
                zip_route(901): [api_json({}, status=503), api_redirect(901)],
                archive_route(901): [archive_download(payload)],
            }
        )
        self.assertEqual(self.download(client), payload)
        self.assertEqual(self.sleeps, [1.0])
        self.assertEqual(len(transport.sent), 3)

    def test_transient_archive_host_503_is_retried_with_fresh_redirect(self):
        payload = archive()
        client, transport = self.client(
            {
                zip_route(901): [
                    api_redirect(901, "first"),
                    api_redirect(901, "second"),
                ],
                archive_route(901): [(503, {}, b""), archive_download(payload)],
            }
        )
        self.assertEqual(self.download(client), payload)
        self.assertEqual(self.sleeps, [1.0])
        self.assertEqual(
            [urlsplit(request.url).netloc for request in transport.sent],
            [API_HOST, ARCHIVE_HOST, API_HOST, ARCHIVE_HOST],
        )
        self.assertIn("sig=second", transport.sent[-1].url)

    def test_rate_limited_403_on_download_is_retried(self):
        payload = archive()
        client, _ = self.client(
            {
                zip_route(901): [
                    api_json({}, status=403, headers={"Retry-After": "3"}),
                    api_redirect(901),
                ],
                archive_route(901): [archive_download(payload)],
            }
        )
        self.assertEqual(self.download(client), payload)
        self.assertEqual(self.sleeps, [3.0])

    def test_download_404_falls_back_through_redirect_contract(self):
        client, _ = self.client(
            {
                RUNS_ROUTE: [
                    api_json(
                        {
                            "workflow_runs": [
                                run_data(300, "2026-10-03T23:30:00Z"),
                                run_data(299, "2026-10-03T22:30:00Z"),
                            ]
                        }
                    )
                ],
                artifacts_route(300): [api_json({"artifacts": [artifact_data(902)]})],
                zip_route(902): [api_json({"message": "Not Found"}, status=404)],
                artifacts_route(299): [api_json({"artifacts": [artifact_data(901)]})],
                zip_route(901): [api_redirect(901)],
                archive_route(901): [archive_download(archive())],
            }
        )
        selected = self.select(client)
        self.assertEqual((selected.run_id, selected.artifact_id), (299, 901))
        self.assertEqual(self.sleeps, [])

    def test_redirected_archive_for_wrong_issue_fails_closed(self):
        client, _ = self.client(
            {
                RUNS_ROUTE: [
                    api_json(
                        {"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
                    )
                ],
                artifacts_route(300): [api_json({"artifacts": [artifact_data(901)]})],
                zip_route(901): [api_redirect(901)],
                archive_route(901): [
                    archive_download(archive(issue_date="2026-10-03"))
                ],
            }
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            self.select(client)
        self.assertIn("artifact_content_invalid=1", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_redirected_archive_declared_over_response_ceiling_is_rejected(self):
        oversized = str(artifact_fetch.MAX_ARCHIVE_RESPONSE_BYTES + 1)
        client, _ = self.client(
            {
                RUNS_ROUTE: [
                    api_json(
                        {"workflow_runs": [run_data(300, "2026-10-03T23:30:00Z")]}
                    )
                ],
                artifacts_route(300): [api_json({"artifacts": [artifact_data(901)]})],
                zip_route(901): [api_redirect(901)],
                archive_route(901): [
                    archive_download(archive(), headers={"Content-Length": oversized})
                ],
            }
        )
        with self.assertRaises(ArtifactSelectionError) as caught:
            self.select(client)
        self.assertIn("artifact_content_invalid=1", str(caught.exception))
        self.assertFalse(self.output.exists())


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
