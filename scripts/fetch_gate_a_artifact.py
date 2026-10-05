#!/usr/bin/env python3
"""Select and download the newest usable Daily Duck Gate A artifact.

Only read-only GitHub API requests are made. Transient retries are deliberately
bounded and never wrap workflow dispatch or any other write operation.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import requests


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
DEFAULT_WORKFLOW = "daily-duck.yml"
DEFAULT_BRANCH = "main"
DEFAULT_ARTIFACT = "daily-duck-results"
DEFAULT_REQUIRED_FILE = "gate_a_package.json"
DEFAULT_MAX_AGE_HOURS = 36.0
DEFAULT_MAX_CANDIDATES = 20
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_ARCHIVE_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200


class GitHubReadError(RuntimeError):
    """A sanitized GitHub read failure with no response body or credentials."""

    def __init__(self, operation: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        if status_code is None:
            detail = "transport failure"
        else:
            detail = f"HTTP {status_code}"
        super().__init__(f"GitHub read failed during {operation}: {detail}.")


class ArtifactSelectionError(RuntimeError):
    """No authoritative, fresh, usable artifact could be selected."""


@dataclass(frozen=True)
class Selection:
    run_id: int
    artifact_id: int
    package_path: Path


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone is required")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(parsed)
    except ValueError:
        return None


def _retry_delay(
    header: object,
    *,
    attempt: int,
    now: datetime,
) -> float | None:
    default = float(2 ** (attempt - 1))
    if not isinstance(header, str) or not header.strip():
        return default
    value = header.strip()
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = _utc(parsedate_to_datetime(value))
            delay = max(0.0, (retry_at - _utc(now)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default
    delay = max(0.0, delay)
    if delay > MAX_RETRY_DELAY_SECONDS:
        return None
    return delay


def _rate_limit_reset_delay(
    header: object,
    *,
    now: datetime,
) -> float | None:
    if not isinstance(header, str) or not header.strip():
        return None
    try:
        reset_at = float(header.strip())
    except ValueError:
        return None
    delay = max(0.0, reset_at - _utc(now).timestamp())
    if delay > MAX_RETRY_DELAY_SECONDS:
        return None
    return delay


def _rate_limited_403_delay(
    headers: Mapping[str, str],
    *,
    attempt: int,
    now: datetime,
) -> float | None:
    if "retry-after" in headers:
        return _retry_delay(
            headers.get("retry-after"),
            attempt=attempt,
            now=now,
        )
    if headers.get("x-ratelimit-remaining") == "0":
        return _rate_limit_reset_delay(
            headers.get("x-ratelimit-reset"),
            now=now,
        )
    return None


class GitHubReadClient:
    """Narrow GET-only GitHub client with bounded transient retry."""

    def __init__(
        self,
        *,
        token: str,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("A GitHub token is required.")
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "daily-duck-gate-a-artifact-reader",
        }

    def get(
        self,
        path: str,
        *,
        operation: str,
        params: Mapping[str, object] | None = None,
        accept: str | None = None,
    ) -> Any:
        headers = dict(self._headers)
        if accept is not None:
            headers["Accept"] = accept
        url = f"{GITHUB_API_ROOT}/{path.lstrip('/')}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.get(
                    url,
                    headers=headers,
                    params=dict(params or {}),
                    timeout=(5.0, 30.0),
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise GitHubReadError(operation) from exc
                self._sleep(float(2 ** (attempt - 1)))
                continue

            status_code = int(response.status_code)
            if 200 <= status_code < 300:
                return response

            headers_map = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            rate_limited_403_delay = (
                _rate_limited_403_delay(
                    headers_map,
                    attempt=attempt,
                    now=self._clock(),
                )
                if status_code == 403
                else None
            )
            retryable = (
                status_code == 429
                or 500 <= status_code <= 599
                or rate_limited_403_delay is not None
            )
            if not retryable or attempt == MAX_ATTEMPTS:
                raise GitHubReadError(operation, status_code)

            delay = (
                rate_limited_403_delay
                if status_code == 403
                else _retry_delay(
                    headers_map.get("retry-after"),
                    attempt=attempt,
                    now=self._clock(),
                )
            )
            if delay is None:
                raise GitHubReadError(operation, status_code)
            self._sleep(delay)
        raise AssertionError("bounded retry loop exhausted unexpectedly")

    def get_json(
        self,
        path: str,
        *,
        operation: str,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        response = self.get(path, operation=operation, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubReadError(operation) from exc
        if not isinstance(payload, Mapping):
            raise GitHubReadError(operation)
        return payload

    def get_bytes(
        self,
        path: str,
        *,
        operation: str,
        max_bytes: int = MAX_ARCHIVE_RESPONSE_BYTES,
    ) -> bytes:
        # GitHub's artifact archive endpoint only accepts the JSON media type
        # and answers with a 302 to a short-lived archive URL. Requesting
        # application/octet-stream is rejected with HTTP 415 before redirect.
        response = self.get(path, operation=operation)
        headers_map = {
            str(key).lower(): str(value)
            for key, value in response.headers.items()
        }
        content_length = headers_map.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise ValueError("artifact Content-Length is invalid") from exc
            if declared_length < 0 or declared_length > max_bytes:
                raise ValueError("artifact response is too large")
        content = response.content
        if not isinstance(content, bytes):
            raise GitHubReadError(operation)
        if len(content) > max_bytes:
            raise ValueError("artifact response is too large")
        return content


def _canonical_issue_date(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"{field} is invalid")
    return value


def _validated_archive(
    payload: bytes,
    required_file: str,
    expected_issue_date: str,
) -> list[tuple[Path, bytes]]:
    if not payload:
        raise ValueError("empty archive")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid archive") from exc

    files: list[tuple[Path, bytes]] = []
    packages: list[bytes] = []
    seen_paths: set[str] = set()
    total_size = 0
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("archive contains too many entries")
        for info in entries:
            member = PurePosixPath(info.filename)
            if (
                not info.filename
                or member.is_absolute()
                or ".." in member.parts
                or "\\" in info.filename
                # Colons are not valid in the expected artifact layout. Reject
                # them lexically on every host so Windows drive-relative paths
                # and alternate data stream names can never reach extraction.
                or ":" in info.filename
            ):
                raise ValueError("unsafe archive member")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("archive symlink is not allowed")
            if info.is_dir():
                continue
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError("archive member size is invalid")
            total_size += int(info.file_size)
            if total_size > MAX_ARCHIVE_BYTES:
                raise ValueError("archive is too large")
            path = Path(*member.parts)
            path_key = path.as_posix().casefold()
            if path_key in seen_paths:
                raise ValueError("duplicate archive member")
            seen_paths.add(path_key)
            try:
                data = archive.read(info)
            except (
                EOFError,
                NotImplementedError,
                RuntimeError,
                zipfile.BadZipFile,
            ) as exc:
                raise ValueError("archive member is invalid") from exc
            files.append((path, data))
            if member.name == required_file:
                packages.append(data)

    if len(packages) != 1:
        raise ValueError("required package count is not one")
    try:
        package = json.loads(packages[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("package is not valid JSON") from exc
    if not isinstance(package, dict):
        raise ValueError("package is not an object")
    issue_date = _canonical_issue_date(
        package.get("issue_date"),
        field="package issue_date",
    )
    if issue_date != expected_issue_date:
        raise ValueError("package issue date does not match expected issue")
    compatibility_date = package.get("date")
    if compatibility_date is not None and compatibility_date != issue_date:
        raise ValueError("package date aliases disagree")
    if package.get("phase") != 2:
        raise ValueError("package phase is invalid")
    if package.get("state") != "WAITING_STORY_SELECTION":
        raise ValueError("package state is invalid")
    approval_token_digest = package.get("approval_token_digest")
    if (
        not isinstance(approval_token_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", approval_token_digest) is None
    ):
        raise ValueError("package approval token digest is invalid")
    stories = package.get("story_options")
    if (
        not isinstance(stories, list)
        or len(stories) != 5
        or not all(isinstance(story, dict) for story in stories)
        or [story.get("candidate_number") for story in stories] != list(range(1, 6))
    ):
        raise ValueError("package story options are invalid")
    return files


def _write_archive(
    files: list[tuple[Path, bytes]], output_dir: Path, required_file: str
) -> Path:
    if output_dir.exists():
        raise ArtifactSelectionError(
            "Artifact output directory already exists; refusing to overwrite it."
        )
    output_dir.mkdir(parents=True)
    package_path: Path | None = None
    for relative_path, data in files:
        target = output_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if relative_path.name == required_file:
            package_path = target
    if package_path is None:
        raise AssertionError("validated package missing during extraction")
    return package_path


def fetch_gate_a_artifact(
    *,
    client: GitHubReadClient,
    repository: str,
    output_dir: Path,
    workflow: str = DEFAULT_WORKFLOW,
    branch: str = DEFAULT_BRANCH,
    artifact_name: str = DEFAULT_ARTIFACT,
    required_file: str = DEFAULT_REQUIRED_FILE,
    expected_issue_date: str,
    max_age: timedelta = timedelta(hours=DEFAULT_MAX_AGE_HOURS),
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    now: datetime | None = None,
) -> Selection:
    current = _utc(now or datetime.now(timezone.utc))
    expected_issue_date = _canonical_issue_date(
        expected_issue_date,
        field="expected issue date",
    )
    if output_dir.exists():
        raise ArtifactSelectionError(
            "Artifact output directory already exists; refusing to overwrite it."
        )
    payload = client.get_json(
        f"repos/{repository}/actions/workflows/{workflow}/runs",
        operation="list successful Daily Duck runs",
        params={
            "branch": branch,
            "status": "success",
            "per_page": max_candidates,
        },
    )
    raw_runs = payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        raise GitHubReadError("list successful Daily Duck runs")

    reasons: Counter[str] = Counter()
    considered = 0
    newest_first = sorted(
        raw_runs[:max_candidates],
        key=lambda run: (
            _parse_timestamp(run.get("created_at"))
            if isinstance(run, Mapping)
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for run in newest_first:
        if not isinstance(run, Mapping):
            reasons["malformed_run"] += 1
            continue
        run_id = run.get("id")
        created_at = _parse_timestamp(run.get("created_at"))
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or created_at is None
            or run.get("head_branch") != branch
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
        ):
            reasons["non_authoritative_run"] += 1
            continue
        if created_at > current + timedelta(minutes=5) or current - created_at > max_age:
            reasons["stale_run"] += 1
            continue

        considered += 1
        try:
            artifacts_payload = client.get_json(
                f"repos/{repository}/actions/runs/{run_id}/artifacts",
                operation="list artifacts for candidate run",
                params={"per_page": 100},
            )
        except GitHubReadError as exc:
            if exc.status_code == 404:
                reasons["run_artifacts_unavailable"] += 1
                continue
            raise
        raw_artifacts = artifacts_payload.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise GitHubReadError("list artifacts for candidate run")

        matching = [
            artifact
            for artifact in raw_artifacts
            if isinstance(artifact, Mapping) and artifact.get("name") == artifact_name
        ]
        if not matching:
            reasons["artifact_missing"] += 1
            continue

        selected_files: list[tuple[Path, bytes]] | None = None
        selected_artifact_id: int | None = None
        for artifact in matching:
            artifact_id = artifact.get("id")
            expires_at = _parse_timestamp(artifact.get("expires_at"))
            if (
                artifact.get("expired") is not False
                or expires_at is None
                or expires_at <= current
                or not isinstance(artifact_id, int)
                or isinstance(artifact_id, bool)
                or artifact_id <= 0
            ):
                reasons["artifact_expired_or_invalid"] += 1
                continue
            try:
                archive_payload = client.get_bytes(
                    f"repos/{repository}/actions/artifacts/{artifact_id}/zip",
                    operation="download candidate artifact",
                    max_bytes=MAX_ARCHIVE_RESPONSE_BYTES,
                )
            except GitHubReadError as exc:
                if exc.status_code == 404:
                    reasons["artifact_unavailable"] += 1
                    continue
                raise
            except ValueError:
                reasons["artifact_content_invalid"] += 1
                continue
            try:
                selected_files = _validated_archive(
                    archive_payload,
                    required_file,
                    expected_issue_date,
                )
            except ValueError:
                reasons["artifact_content_invalid"] += 1
                continue
            selected_artifact_id = artifact_id
            break

        if selected_files is None or selected_artifact_id is None:
            continue
        package_path = _write_archive(selected_files, output_dir, required_file)
        return Selection(
            run_id=run_id,
            artifact_id=selected_artifact_id,
            package_path=package_path,
        )

    reason_text = ", ".join(
        f"{name}={count}" for name, count in sorted(reasons.items())
    ) or "no_candidates=1"
    raise ArtifactSelectionError(
        "No usable fresh Daily Duck Gate A artifact was found "
        f"({considered} fresh successful candidate(s); {reason_text})."
    )


def _append_github_output(path: Path, selection: Selection) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"run_id={selection.run_id}\n")
        output.write(f"artifact_id={selection.artifact_id}\n")
        output.write(f"package_path={selection.package_path.as_posix()}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--expected-issue-date", required=True)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = parser.parse_args(argv)

    token = os.getenv("GH_TOKEN", "").strip()
    if not args.repository or "/" not in args.repository:
        parser.error("--repository or GITHUB_REPOSITORY must be owner/repository")
    if not token:
        parser.error("GH_TOKEN is required")
    if args.max_age_hours <= 0:
        parser.error("--max-age-hours must be positive")

    selection = fetch_gate_a_artifact(
        client=GitHubReadClient(token=token),
        repository=args.repository,
        output_dir=args.output_dir,
        expected_issue_date=args.expected_issue_date,
        max_age=timedelta(hours=args.max_age_hours),
    )
    if args.github_output is not None:
        _append_github_output(args.github_output, selection)
    print(
        "Selected usable Daily Duck artifact: "
        f"run={selection.run_id}, artifact={selection.artifact_id}"
    )
    print(f"Validated Gate A package: {selection.package_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ArtifactSelectionError, GitHubReadError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
