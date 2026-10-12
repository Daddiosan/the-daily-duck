#!/usr/bin/env python3
"""Isolated X backfill support for the 2026-10-11 Daily Duck issue.

This module deliberately does not read or write the normal X publication state.
Preparation and live publication are separate commands.  A live post can only be
attempted after a complete read-only duplicate scan and a separately persisted
POST_IN_FLIGHT record.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import requests
from PIL import Image

try:  # Preparation/tests do not require the live OAuth dependency.
    import requests_oauthlib  # noqa: F401
except ModuleNotFoundError:
    oauth_stub = types.ModuleType("requests_oauthlib")

    class MissingOAuth1:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(
                "requests-oauthlib is required for live X API operations."
            )

    oauth_stub.OAuth1 = MissingOAuth1
    sys.modules["requests_oauthlib"] = oauth_stub

if __package__:
    from scripts import build_x_card, publish_x
else:  # pragma: no cover - direct script execution
    import build_x_card  # type: ignore[no-redef]
    import publish_x  # type: ignore[no-redef]


TARGET_DATE = "2026-10-11"
TARGET_TITLE = "SAFE, SET, PREP!"
TARGET_IMAGE_NUMBER = 2
TARGET_TITLE_NUMBER = 2
TARGET_IMAGE_CONCEPT = "The Prep Rhythm"
TARGET_ARTICLE_URL = "https://www.thedailyduck.ai/ducks/2026-10-11/"
TARGET_SOURCE = "Phys.org"
TARGET_SOURCE_URL = (
    "https://phys.org/news/2026-10-meal-prep-safely-microbiologist-stepbystep.html"
)

SOURCE_RUN_ID = 38201777075
SOURCE_RUN_HEAD_SHA = "67d9143452af063478a0caa7f47b90e5a74d5827"
SOURCE_ARTIFACT_ID = 11725558085
SOURCE_ARTIFACT_NAME = "daily-duck-backfill-2026-10-11-ready-to-publish"
SOURCE_ARTIFACT_DIGEST = (
    "sha256:da8c3ccbc23fbf954b8607b9b6e1fa70111ad14837a7a964dca1dc333c143ce9"
)
SOURCE_READY_SHA256 = (
    "4e1328ee300665929506e42d39025b139ef02a8f2539c021e30b45175d7944ac"
)
APPROVED_IMAGE_SHA256 = (
    "f970c8333fb46c24e5ab6362c9bbc119a52517574b5805f9a1f9e09df9e49432"
)

WEBSITE_RUN_ID = 38208086549
WEBSITE_RUN_HEAD_SHA = "d55c5de2e84f1633cff97dd57a49c30d0bb1b2fe"
WEBSITE_PUBLICATION_COMMIT = "2c07d38c82ff59ce2892b0c8250a3e997ba49aab"

SOURCE_READY_REL = Path("automation_state/ready_to_publish.json")
SOURCE_IMAGE_REL = Path("automation_images/canonical/2026-10-11.png")
SOURCE_REPORT_REL = Path("automation_state/final_selection_backfill_report.json")
PUBLISHED_IMAGE_REL = Path("assets/ducks/2026-10-11-the-meal-prep-duck.png")
ARCHIVE_RESULT_REL = Path("automation_state/archive_only_publish_result.json")
ARCHIVE_REL = Path("data/archive.json")
PUBLISHED_PAGE_REL = Path("ducks/2026-10-11/index.html")
SHARED_READY_REL = Path("automation_state/ready_to_publish.json")
SHARED_X_RESULT_REL = Path("automation_state/x_publish_result.json")

BACKFILL_STATE_REL = Path("automation_state/x_backfill_2026-10-11_result.json")
BACKFILL_CARD_REL = Path("automation_images/x_backfill/2026-10-11-x-card.png")
CONFIRMATION = "PUBLISH-X-BACKFILL-2026-10-11"
OPERATION_ID = "x-backfill-2026-10-11-11725558085-f970c8333fb4"

DUPLICATE_SCAN_START = "2026-10-10T00:00:00Z"
MAX_TIMELINE_PAGES = 40
TERMINAL_OR_BLOCKED_STATES = {
    "DUPLICATE_FOUND",
    "HOLD",
    "POST_IN_FLIGHT",
    "POST_RESULT_UNKNOWN",
    "X_POSTED",
}


class BackfillError(RuntimeError):
    """Base class for fail-closed backfill errors."""


class StateBlockedError(BackfillError):
    """The isolated state forbids automatic progress."""


class DuplicateScanError(BackfillError):
    """The X read-only history scan could not prove a complete result."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BackfillError(f"Missing required JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackfillError(f"Invalid JSON file: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackfillError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_issue_date(value: object, field: str) -> str:
    text = first_text(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise BackfillError(f"{field} must be YYYY-MM-DD; got {text!r}") from exc
    if parsed.strftime("%Y-%m-%d") != text:
        raise BackfillError(f"{field} must be canonical YYYY-MM-DD; got {text!r}")
    return text


def require_equal(actual: object, expected: object, field: str) -> None:
    if actual != expected:
        raise BackfillError(f"{field} mismatch: expected {expected!r}, got {actual!r}")


def contains_cjk(text: str) -> bool:
    return bool(
        re.search(
            r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff01-\uff60]",
            text,
        )
    )


def resolve_approved_story(ready: dict[str, Any]) -> dict[str, Any]:
    story = build_x_card.resolve_approved_story(ready)
    if not isinstance(story, dict):
        raise BackfillError("Approved story is not an object.")
    return story


def validate_artifact_metadata(
    artifact_metadata_path: Path,
    run_metadata_path: Path,
) -> dict[str, Any]:
    artifact = load_json(artifact_metadata_path)
    run = load_json(run_metadata_path)
    checks = {
        "artifact id": (artifact.get("id"), SOURCE_ARTIFACT_ID),
        "artifact name": (artifact.get("name"), SOURCE_ARTIFACT_NAME),
        "artifact digest": (artifact.get("digest"), SOURCE_ARTIFACT_DIGEST),
        "artifact expired": (artifact.get("expired"), False),
        "artifact workflow run": (
            (artifact.get("workflow_run") or {}).get("id"),
            SOURCE_RUN_ID,
        ),
        "run id": (run.get("id", run.get("databaseId")), SOURCE_RUN_ID),
        "run event": (run.get("event"), "workflow_dispatch"),
        "run status": (run.get("status"), "completed"),
        "run conclusion": (run.get("conclusion"), "success"),
        "run head SHA": (run.get("head_sha", run.get("headSha")), SOURCE_RUN_HEAD_SHA),
    }
    for field, (actual, expected) in checks.items():
        require_equal(actual, expected, field)
    return {
        "run_id": SOURCE_RUN_ID,
        "artifact_id": SOURCE_ARTIFACT_ID,
        "artifact_name": SOURCE_ARTIFACT_NAME,
        "artifact_digest": SOURCE_ARTIFACT_DIGEST,
        "run_head_sha": SOURCE_RUN_HEAD_SHA,
    }


def validate_source_artifact(source_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ready_path = source_dir / SOURCE_READY_REL
    image_path = source_dir / SOURCE_IMAGE_REL
    report_path = source_dir / SOURCE_REPORT_REL

    ready_hash = sha256_file(ready_path)
    require_equal(ready_hash, SOURCE_READY_SHA256, "source ready SHA-256")
    image_hash = sha256_file(image_path)
    require_equal(image_hash, APPROVED_IMAGE_SHA256, "source image SHA-256")

    ready = load_json(ready_path)
    report = load_json(report_path)
    issue_date = canonical_issue_date(ready.get("issue_date"), "source issue_date")
    require_equal(issue_date, TARGET_DATE, "source issue_date")
    require_equal(ready.get("state"), "READY_TO_PUBLISH", "source state")
    require_equal(ready.get("selected_title"), TARGET_TITLE, "selected title")
    require_equal(
        ready.get("selected_title_number"), TARGET_TITLE_NUMBER, "selected title number"
    )
    require_equal(
        ready.get("selected_image_number"), TARGET_IMAGE_NUMBER, "selected image number"
    )
    require_equal(
        first_text(ready.get("canonical_image_path")),
        SOURCE_IMAGE_REL.as_posix(),
        "canonical image path",
    )

    selected_preview = ready.get("selected_preview")
    if not isinstance(selected_preview, dict):
        raise BackfillError("selected_preview is missing from source ready state.")
    require_equal(selected_preview.get("number"), TARGET_IMAGE_NUMBER, "preview number")
    require_equal(
        selected_preview.get("concept_number"), TARGET_IMAGE_NUMBER, "preview concept number"
    )
    require_equal(selected_preview.get("sha256"), APPROVED_IMAGE_SHA256, "preview hash")
    selected_concept = ready.get("selected_image_concept")
    if not isinstance(selected_concept, dict):
        raise BackfillError("selected_image_concept is missing.")
    require_equal(
        selected_concept.get("title_en"), TARGET_IMAGE_CONCEPT, "selected image concept"
    )

    final_selection = ready.get("final_selection_backfill")
    if not isinstance(final_selection, dict):
        raise BackfillError("final_selection_backfill provenance is missing.")
    require_equal(
        str(final_selection.get("recovery_workflow_run_id")),
        str(SOURCE_RUN_ID),
        "recovery workflow run",
    )
    require_equal(
        final_selection.get("canonical_image_sha256"),
        APPROVED_IMAGE_SHA256,
        "final selection image hash",
    )

    require_equal(report.get("state"), "READY_TO_PUBLISH", "selection report state")
    require_equal(report.get("issue_date"), TARGET_DATE, "selection report date")
    require_equal(report.get("selected_title"), TARGET_TITLE, "selection report title")
    require_equal(report.get("selected_image_number"), 2, "selection report image")
    require_equal(
        report.get("selected_image_concept"),
        TARGET_IMAGE_CONCEPT,
        "selection report image concept",
    )
    require_equal(report.get("image_hash_match"), True, "selection report hash match")

    approved_root = ready.get("gate_a_approved_story")
    if not isinstance(approved_root, dict):
        raise BackfillError("gate_a_approved_story is missing.")
    approved = resolve_approved_story(ready)
    approved_date = canonical_issue_date(
        first_text(
            approved.get("issue_date"),
            approved.get("date"),
            approved_root.get("issue_date"),
            approved_root.get("date"),
        ),
        "approved story issue_date",
    )
    require_equal(approved_date, TARGET_DATE, "approved story issue_date")
    x_en = first_text(approved.get("x_en"))
    if not x_en:
        raise BackfillError("Approved story x_en is missing.")
    if contains_cjk(x_en):
        raise BackfillError("Approved story x_en contains non-English CJK text.")

    source_name = first_text(approved.get("source"), approved_root.get("source"))
    source_url = first_text(
        approved.get("source_url"),
        approved.get("url"),
        approved_root.get("source_url"),
        approved_root.get("url"),
    )
    require_equal(source_name, TARGET_SOURCE, "approved source")
    require_equal(source_url, TARGET_SOURCE_URL, "approved source URL")

    return ready, {
        "ready_sha256": ready_hash,
        "image_sha256": image_hash,
        "x_en": x_en,
        "source": source_name,
        "source_url": source_url,
    }


def _publication_commit_is_ancestor(repository_root: Path) -> bool | None:
    if not (repository_root / ".git").exists():
        return None
    completed = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            WEBSITE_PUBLICATION_COMMIT,
            "HEAD",
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise BackfillError(
            "Could not verify website publication commit ancestry: "
            f"{completed.stderr.strip()}"
        )
    return completed.returncode == 0


def validate_website_publication(
    repository_root: Path,
    website_run_metadata_path: Path,
) -> dict[str, Any]:
    result = load_json(repository_root / ARCHIVE_RESULT_REL)
    checks = {
        "website action": (result.get("action"), "ARCHIVE_ONLY_PUBLISHED"),
        "website issue_date": (result.get("issue_date"), TARGET_DATE),
        "website title": (result.get("title"), TARGET_TITLE),
        "website image": (result.get("image"), PUBLISHED_IMAGE_REL.as_posix()),
        "website page": (result.get("page"), PUBLISHED_PAGE_REL.as_posix()),
        "website publication mode": (result.get("publication_mode"), "ARCHIVE_ONLY"),
        "website X request": (result.get("x_publish_requested"), False),
    }
    for field, (actual, expected) in checks.items():
        require_equal(actual, expected, field)

    published_image = repository_root / PUBLISHED_IMAGE_REL
    image_hash = sha256_file(published_image)
    require_equal(image_hash, APPROVED_IMAGE_SHA256, "published image SHA-256")

    page = repository_root / PUBLISHED_PAGE_REL
    page_text = page.read_text(encoding="utf-8")
    if TARGET_TITLE not in page_text:
        raise BackfillError("Published page does not contain the selected title.")
    if TARGET_ARTICLE_URL not in page_text:
        raise BackfillError("Published page does not contain the canonical article URL.")

    archive = json.loads((repository_root / ARCHIVE_REL).read_text(encoding="utf-8"))
    if not isinstance(archive, list):
        raise BackfillError("data/archive.json must contain a list.")
    matching = [item for item in archive if isinstance(item, dict) and item.get("date") == TARGET_DATE]
    if len(matching) != 1:
        raise BackfillError(f"Expected one archive entry for {TARGET_DATE}; got {len(matching)}")
    require_equal(matching[0].get("storyTitleEn"), TARGET_TITLE, "archive selected title")
    require_equal(matching[0].get("image"), PUBLISHED_IMAGE_REL.as_posix(), "archive image")

    run = load_json(website_run_metadata_path)
    run_checks = {
        "website run id": (run.get("id", run.get("databaseId")), WEBSITE_RUN_ID),
        "website run event": (run.get("event"), "workflow_dispatch"),
        "website run status": (run.get("status"), "completed"),
        "website run conclusion": (run.get("conclusion"), "success"),
        "website run head SHA": (
            run.get("head_sha", run.get("headSha")),
            WEBSITE_RUN_HEAD_SHA,
        ),
    }
    for field, (actual, expected) in run_checks.items():
        require_equal(actual, expected, field)

    ancestor = _publication_commit_is_ancestor(repository_root)
    if ancestor is False:
        raise BackfillError(
            f"Website publication commit {WEBSITE_PUBLICATION_COMMIT} is not in HEAD."
        )
    return {
        "workflow_run_id": WEBSITE_RUN_ID,
        "publication_commit": WEBSITE_PUBLICATION_COMMIT,
        "publication_commit_is_ancestor": ancestor,
        "action": "ARCHIVE_ONLY_PUBLISHED",
        "article_url": TARGET_ARTICLE_URL,
        "published_image_sha256": image_hash,
    }


def snapshot_shared_x_state(repository_root: Path) -> dict[str, Any]:
    ready_path = repository_root / SHARED_READY_REL
    result_path = repository_root / SHARED_X_RESULT_REL
    ready = load_json(ready_path)
    result = load_json(result_path)
    ready_date = canonical_issue_date(ready.get("issue_date"), "shared ready issue_date")
    result_date = canonical_issue_date(result.get("issue_date"), "shared X result issue_date")
    result_action = first_text(result.get("action"))
    ready_posted = ready.get("x_posted") is True or bool(first_text(ready.get("x_post_id")))
    result_posted = result_action == "X_POSTED"
    if result_posted and not first_text(result.get("x_post_id")):
        raise BackfillError("Shared X_POSTED result is missing x_post_id.")
    return {
        "ready_path": SHARED_READY_REL.as_posix(),
        "ready_sha256": sha256_file(ready_path),
        "result_path": SHARED_X_RESULT_REL.as_posix(),
        "result_sha256": sha256_file(result_path),
        "ready_issue_date": ready_date,
        "ready_state": first_text(ready.get("state")),
        "result_issue_date": result_date,
        "result_action": result_action,
        "target_terminal_post_exists": (
            (ready_date == TARGET_DATE and ready_posted)
            or (result_date == TARGET_DATE and result_posted)
        ),
    }


def assert_shared_x_state_unchanged(repository_root: Path, before: dict[str, Any]) -> None:
    after = snapshot_shared_x_state(repository_root)
    if after != before:
        raise BackfillError("Shared X state changed during backfill preparation.")


@contextmanager
def operation_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise StateBlockedError(f"Concurrent backfill operation detected: {lock_path}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\noperation_id={OPERATION_ID}\n".encode())
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def validate_existing_state_for_prepare(state_path: Path) -> None:
    if not state_path.exists():
        return
    state = load_json(state_path)
    require_equal(state.get("issue_date"), TARGET_DATE, "existing state issue_date")
    require_equal(state.get("operation_id"), OPERATION_ID, "existing operation_id")
    status = first_text(state.get("state"))
    if status in TERMINAL_OR_BLOCKED_STATES:
        raise StateBlockedError(
            f"Existing isolated state {status} forbids automatic preparation or retry."
        )
    if status != "READY_FOR_REVIEW":
        raise StateBlockedError(f"Unknown existing isolated state: {status!r}")


def render_card_isolated(
    ready: dict[str, Any],
    source_image_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    card_ready = copy.deepcopy(ready)
    card_ready["canonical_image_path"] = str(source_image_path.resolve())
    card_ready.pop("published_image_path", None)
    previous_output_dir = build_x_card.OUT_DIR
    try:
        build_x_card.OUT_DIR = output_path.parent
        rendered, layout = build_x_card.render_card(card_ready)
    finally:
        build_x_card.OUT_DIR = previous_output_dir
    if rendered.resolve() != output_path.resolve():
        raise BackfillError(f"Unexpected X card path: {rendered}")
    with Image.open(rendered) as image:
        size = image.size
        image_format = image.format
    require_equal(size, (1500, 1200), "X card dimensions")
    require_equal(image_format, "PNG", "X card format")
    if size[0] * 4 != size[1] * 5:
        raise BackfillError(f"X card ratio is not exactly 5:4: {size}")
    return {
        "path": BACKFILL_CARD_REL.as_posix(),
        "size": "1500x1200",
        "ratio": "5:4",
        "format": "PNG",
        "sha256": sha256_file(rendered),
        "title": TARGET_TITLE,
        "layout": layout,
        "source_image_sha256": APPROVED_IMAGE_SHA256,
    }


def build_post_text(ready: dict[str, Any]) -> tuple[str, int]:
    approved = resolve_approved_story(ready)
    x_en = first_text(approved.get("x_en"))
    if not x_en:
        raise BackfillError("Approved x_en is missing.")
    if contains_cjk(x_en):
        raise BackfillError("X post body must be English-only.")
    text = publish_x.trim_english_for_x(x_en, TARGET_ARTICLE_URL)
    if text.count(TARGET_ARTICLE_URL) != 1 or not text.endswith(TARGET_ARTICLE_URL):
        raise BackfillError("X post must end with the exact canonical article URL.")
    if contains_cjk(text):
        raise BackfillError("Generated X post is not English-only.")
    weighted = publish_x.x_weighted_length(text)
    if weighted > publish_x.X_MAX_WEIGHTED_LENGTH:
        raise BackfillError(f"X post exceeds weighted length: {weighted}")
    return text, weighted


def prepare(
    *,
    source_dir: Path,
    work_root: Path,
    repository_root: Path,
    artifact_metadata_path: Path,
    run_metadata_path: Path,
    website_run_metadata_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    state_path = work_root / BACKFILL_STATE_REL
    card_path = work_root / BACKFILL_CARD_REL
    with operation_lock(state_path):
        validate_existing_state_for_prepare(state_path)
        shared_before = snapshot_shared_x_state(repository_root)
        if shared_before["target_terminal_post_exists"]:
            raise StateBlockedError(
                "Shared X state already contains a terminal post for the target date."
            )
        provenance = validate_artifact_metadata(artifact_metadata_path, run_metadata_path)
        ready, source = validate_source_artifact(source_dir)
        website = validate_website_publication(repository_root, website_run_metadata_path)
        card = render_card_isolated(ready, source_dir / SOURCE_IMAGE_REL, card_path)
        post_text, weighted = build_post_text(ready)
        assert_shared_x_state_unchanged(repository_root, shared_before)

        state = {
            "state": "READY_FOR_REVIEW",
            "at": now_iso(),
            "issue_date": TARGET_DATE,
            "operation_id": OPERATION_ID,
            "dry_run": dry_run,
            "publication_attempted": False,
            "http_post_executed": False,
            "x_media_uploaded": False,
            "x_post_created": False,
            "human_gate": "WAITING",
            "source_artifact": {
                **provenance,
                "ready_sha256": source["ready_sha256"],
                "approved_image_sha256": source["image_sha256"],
            },
            "website_publication": website,
            "selection": {
                "selected_image_number": TARGET_IMAGE_NUMBER,
                "selected_title_number": TARGET_TITLE_NUMBER,
                "selected_title": TARGET_TITLE,
            },
            "x_card": card,
            "x_post": {
                "text": post_text,
                "weighted_length": weighted,
                "max_weighted_length": publish_x.X_MAX_WEIGHTED_LENGTH,
                "language": "en",
                "article_url": TARGET_ARTICLE_URL,
                "made_with_ai": True,
            },
            "duplicate_guard": {
                "status": "NOT_CHECKED_DRY_RUN" if dry_run else "NOT_CHECKED",
                "scan_start": DUPLICATE_SCAN_START,
                "requires_complete_live_read_before_post": True,
                "failure_policy": "HOLD",
            },
            "shared_x_state": {**shared_before, "preserved": True},
        }
        write_json_atomic(state_path, state)
        return state


def _response_json(response: requests.Response, operation: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise DuplicateScanError(f"{operation} failed: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise DuplicateScanError(f"{operation} returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise DuplicateScanError(f"{operation} response is not an object.")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        raise DuplicateScanError(f"{operation} returned partial errors.")
    if errors is not None and not isinstance(errors, list):
        raise DuplicateScanError(f"{operation} errors field is malformed.")
    return payload


def _is_target_article_url(value: object) -> bool:
    candidate = first_text(value).strip("<>.,;:!?()[]{}\"'")
    if not candidate:
        return False
    if "://" not in candidate and candidate.startswith("www."):
        candidate = f"https://{candidate}"
    if "://" not in candidate and candidate.startswith("thedailyduck.ai/"):
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        target = urlsplit(TARGET_ARTICLE_URL)
    except ValueError:
        return False
    accepted_hosts = {"thedailyduck.ai", "www.thedailyduck.ai"}
    return (
        parsed.hostname is not None
        and parsed.hostname.casefold() in accepted_hosts
        and target.hostname.casefold() in accepted_hosts
        and parsed.path.rstrip("/") == target.path.rstrip("/")
    )


def _row_contains_article_url(row: dict[str, Any]) -> bool:
    text = first_text(row.get("text"))
    if any(_is_target_article_url(url) for url in re.findall(r"https?://\S+", text)):
        return True
    entities = row.get("entities")
    if not isinstance(entities, dict):
        return False
    urls = entities.get("urls")
    if not isinstance(urls, list):
        return False
    for item in urls:
        if not isinstance(item, dict):
            continue
        for field in ("expanded_url", "unwound_url", "display_url", "url"):
            if _is_target_article_url(item.get(field)):
                return True
    return False


def find_existing_post_complete(
    auth: Any,
    *,
    session: Any = requests,
) -> dict[str, Any]:
    try:
        me_response = session.get(publish_x.ME_URL, auth=auth, timeout=30)
    except requests.RequestException as exc:
        raise DuplicateScanError(f"X /users/me request failed: {exc}") from exc
    me = _response_json(me_response, "X /users/me")
    me_data = me.get("data")
    user_id = first_text(me_data.get("id")) if isinstance(me_data, dict) else ""
    if not user_id:
        raise DuplicateScanError("X /users/me response did not contain a user id.")

    next_token = ""
    seen_tokens: set[str] = set()
    posts_scanned = 0
    pages_scanned = 0
    while True:
        if pages_scanned >= MAX_TIMELINE_PAGES:
            raise DuplicateScanError("X timeline pagination exceeded the safe page limit.")
        params = {
            "max_results": 100,
            "exclude": "retweets,replies",
            "tweet.fields": "created_at,entities",
            "start_time": DUPLICATE_SCAN_START,
        }
        if next_token:
            params["pagination_token"] = next_token
        try:
            response = session.get(
                f"https://api.x.com/2/users/{user_id}/tweets",
                auth=auth,
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise DuplicateScanError(f"X timeline request failed: {exc}") from exc
        payload = _response_json(response, "X timeline")
        rows = payload.get("data", [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise DuplicateScanError("X timeline data is not a list.")
        pages_scanned += 1
        posts_scanned += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                raise DuplicateScanError("X timeline contains a non-object row.")
            post_id = first_text(row.get("id"))
            if not post_id:
                raise DuplicateScanError("X timeline row is missing an id.")
            if _row_contains_article_url(row):
                return {
                    "found": True,
                    "post_id": post_id,
                    "post_url": f"https://x.com/i/web/status/{post_id}",
                    "pages_scanned": pages_scanned,
                    "posts_scanned": posts_scanned,
                    "scan_start": DUPLICATE_SCAN_START,
                }
        meta = payload.get("meta")
        if meta is None:
            raise DuplicateScanError("X timeline response omitted pagination metadata.")
        if not isinstance(meta, dict):
            raise DuplicateScanError("X timeline metadata is not an object.")
        next_token = first_text(meta.get("next_token"))
        if not next_token:
            return {
                "found": False,
                "pages_scanned": pages_scanned,
                "posts_scanned": posts_scanned,
                "scan_start": DUPLICATE_SCAN_START,
                "complete": True,
            }
        if next_token in seen_tokens:
            raise DuplicateScanError("X timeline returned a repeated pagination token.")
        seen_tokens.add(next_token)


def load_isolated_state(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path)
    require_equal(state.get("issue_date"), TARGET_DATE, "isolated state issue_date")
    require_equal(state.get("operation_id"), OPERATION_ID, "isolated operation_id")
    return state


def check_duplicate(
    state_path: Path,
    *,
    auth: Any,
    session: Any = requests,
    reconcile: bool = False,
) -> tuple[dict[str, Any], bool]:
    with operation_lock(state_path):
        state = load_isolated_state(state_path)
        allowed = {"READY_FOR_REVIEW"}
        if reconcile:
            allowed.update({"POST_IN_FLIGHT", "POST_RESULT_UNKNOWN", "HOLD"})
        if state.get("state") not in allowed:
            raise StateBlockedError(
                f"Duplicate check is not allowed from state {state.get('state')!r}."
            )
        previous_state = first_text(state.get("state"))
        try:
            result = find_existing_post_complete(auth, session=session)
        except Exception as exc:
            state.update(
                {
                    "state": "HOLD",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "duplicate_guard": {
                        "status": "READ_FAILED_HOLD",
                        "error": str(exc),
                        "scan_start": DUPLICATE_SCAN_START,
                    },
                }
            )
            write_json_atomic(state_path, state)
            return state, False

        if result["found"]:
            state.update(
                {
                    "state": "DUPLICATE_FOUND",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "duplicate_guard": {
                        "status": "DUPLICATE_FOUND",
                        **result,
                    },
                }
            )
            write_json_atomic(state_path, state)
            return state, False

        if reconcile and previous_state != "READY_FOR_REVIEW":
            state.update(
                {
                    "state": "HOLD",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "duplicate_guard": {
                        "status": "CLEAR_BUT_PRIOR_RESULT_UNCERTAIN_HOLD",
                        **result,
                    },
                }
            )
            write_json_atomic(state_path, state)
            return state, False

        state.update(
            {
                "state": "READY_FOR_REVIEW",
                "at": now_iso(),
                "duplicate_guard": {
                    "status": "CLEAR_COMPLETE",
                    "checked_at": now_iso(),
                    **result,
                },
            }
        )
        write_json_atomic(state_path, state)
        return state, True


def arm_post(state_path: Path, *, confirmation: str, actor: str, owner: str) -> dict[str, Any]:
    with operation_lock(state_path):
        state = load_isolated_state(state_path)
        require_equal(confirmation, CONFIRMATION, "human gate confirmation")
        if not actor or not owner or actor.casefold() != owner.casefold():
            raise StateBlockedError(
                f"GitHub actor {actor!r} is not repository owner {owner!r}."
            )
        require_equal(state.get("state"), "READY_FOR_REVIEW", "state before arm")
        duplicate = state.get("duplicate_guard")
        if not isinstance(duplicate, dict):
            raise StateBlockedError("Duplicate guard evidence is missing.")
        require_equal(duplicate.get("status"), "CLEAR_COMPLETE", "duplicate guard status")
        require_equal(duplicate.get("complete"), True, "duplicate guard completeness")
        state.update(
            {
                "state": "POST_IN_FLIGHT",
                "at": now_iso(),
                "armed_at": now_iso(),
                "armed_by": actor,
                "human_gate": "APPROVED",
                "publication_attempted": False,
                "http_post_executed": False,
            }
        )
        write_json_atomic(state_path, state)
        return state


def validate_armed_payload(state: dict[str, Any]) -> str:
    source = state.get("source_artifact")
    selection = state.get("selection")
    post = state.get("x_post")
    if not isinstance(source, dict) or not isinstance(selection, dict):
        raise StateBlockedError("Prepared provenance or selection is missing.")
    if not isinstance(post, dict):
        raise StateBlockedError("Prepared X post is missing.")
    require_equal(source.get("artifact_id"), SOURCE_ARTIFACT_ID, "armed artifact id")
    require_equal(
        source.get("approved_image_sha256"),
        APPROVED_IMAGE_SHA256,
        "armed image SHA-256",
    )
    require_equal(selection.get("selected_title"), TARGET_TITLE, "armed title")
    require_equal(
        selection.get("selected_image_number"), TARGET_IMAGE_NUMBER, "armed image number"
    )
    text = first_text(post.get("text"))
    if text.count(TARGET_ARTICLE_URL) != 1 or not text.endswith(TARGET_ARTICLE_URL):
        raise StateBlockedError("Armed X text does not end with the exact article URL.")
    if contains_cjk(text):
        raise StateBlockedError("Armed X text is not English-only.")
    weighted = publish_x.x_weighted_length(text)
    require_equal(post.get("weighted_length"), weighted, "armed weighted length")
    if weighted > publish_x.X_MAX_WEIGHTED_LENGTH:
        raise StateBlockedError("Armed X text exceeds the weighted length limit.")
    require_equal(post.get("made_with_ai"), True, "armed made_with_ai policy")
    return text


def publish_armed_post(
    state_path: Path,
    card_path: Path,
    *,
    confirmation: str,
    actor: str,
    owner: str,
    auth: Any,
) -> dict[str, Any]:
    with operation_lock(state_path):
        state = load_isolated_state(state_path)
        require_equal(confirmation, CONFIRMATION, "human gate confirmation")
        if not actor or not owner or actor.casefold() != owner.casefold():
            raise StateBlockedError("GitHub actor validation failed.")
        require_equal(state.get("state"), "POST_IN_FLIGHT", "state before live post")
        if state.get("publication_attempted") is True:
            raise StateBlockedError("This operation already attempted publication; retry is forbidden.")
        require_equal(sha256_file(card_path), state["x_card"]["sha256"], "X card SHA-256")
        post_text = validate_armed_payload(state)

        # Recheck immediately before the first write.  The earlier workflow
        # preflight is required to arm the operation, while this second scan
        # closes the gap introduced by persisting POST_IN_FLIGHT to git.
        try:
            duplicate = find_existing_post_complete(auth)
        except Exception as exc:
            state.update(
                {
                    "state": "HOLD",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "duplicate_guard": {
                        "status": "FINAL_READ_FAILED_HOLD",
                        "error": str(exc),
                        "scan_start": DUPLICATE_SCAN_START,
                    },
                    "automatic_retry_allowed": False,
                }
            )
            write_json_atomic(state_path, state)
            raise
        if duplicate["found"]:
            state.update(
                {
                    "state": "DUPLICATE_FOUND",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "duplicate_guard": {
                        "status": "DUPLICATE_FOUND_FINAL_RECHECK",
                        **duplicate,
                    },
                    "automatic_retry_allowed": False,
                }
            )
            write_json_atomic(state_path, state)
            return state
        state["duplicate_guard"] = {
            "status": "CLEAR_COMPLETE_FINAL_RECHECK",
            "checked_at": now_iso(),
            **duplicate,
        }

        state["publication_attempted"] = True
        state["http_post_executed"] = True
        state["attempted_at"] = now_iso()
        write_json_atomic(state_path, state)
        try:
            media_id = publish_x.upload_image(card_path, auth)
        except Exception as exc:
            state.update(
                {
                    "state": "HOLD",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "x_media_uploaded": False,
                    "x_post_created": False,
                    "failure_stage": "MEDIA_UPLOAD",
                    "error": str(exc),
                    "automatic_retry_allowed": False,
                }
            )
            write_json_atomic(state_path, state)
            raise

        state["x_media_uploaded"] = True
        state["x_media_id"] = media_id
        write_json_atomic(state_path, state)
        try:
            post_id, response = publish_x.create_post(post_text, media_id, auth)
        except Exception as exc:
            state.update(
                {
                    "state": "POST_RESULT_UNKNOWN",
                    "at": now_iso(),
                    "human_gate": "WAITING",
                    "x_post_created": False,
                    "failure_stage": "CREATE_POST",
                    "error": str(exc),
                    "automatic_retry_allowed": False,
                }
            )
            write_json_atomic(state_path, state)
            raise

        state.update(
            {
                "state": "X_POSTED",
                "at": now_iso(),
                "posted_at": now_iso(),
                "human_gate": "COMPLETE",
                "x_post_created": True,
                "x_post_id": post_id,
                "x_post_url": f"https://x.com/i/web/status/{post_id}",
                "response": response,
                "automatic_retry_allowed": False,
            }
        )
        write_json_atomic(state_path, state)
        return state


def _write_github_output(path: Path | None, can_post: bool, state: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"can_post={'true' if can_post else 'false'}\n")
        handle.write(f"state={state}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--source-dir", type=Path, required=True)
    prepare_parser.add_argument("--work-root", type=Path, required=True)
    prepare_parser.add_argument("--repository-root", type=Path, required=True)
    prepare_parser.add_argument("--artifact-metadata", type=Path, required=True)
    prepare_parser.add_argument("--run-metadata", type=Path, required=True)
    prepare_parser.add_argument("--website-run-metadata", type=Path, required=True)
    prepare_parser.add_argument("--dry-run", action="store_true")

    duplicate = sub.add_parser("check-duplicate")
    duplicate.add_argument("--state-path", type=Path, default=BACKFILL_STATE_REL)
    duplicate.add_argument("--github-output", type=Path)
    duplicate.add_argument("--reconcile", action="store_true")

    arm = sub.add_parser("arm")
    arm.add_argument("--state-path", type=Path, default=BACKFILL_STATE_REL)
    arm.add_argument("--confirmation", required=True)
    arm.add_argument("--actor", required=True)
    arm.add_argument("--owner", required=True)

    post = sub.add_parser("post")
    post.add_argument("--state-path", type=Path, default=BACKFILL_STATE_REL)
    post.add_argument("--card-path", type=Path, default=BACKFILL_CARD_REL)
    post.add_argument("--confirmation", required=True)
    post.add_argument("--actor", required=True)
    post.add_argument("--owner", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        state = prepare(
            source_dir=args.source_dir.resolve(),
            work_root=args.work_root.resolve(),
            repository_root=args.repository_root.resolve(),
            artifact_metadata_path=args.artifact_metadata.resolve(),
            run_metadata_path=args.run_metadata.resolve(),
            website_run_metadata_path=args.website_run_metadata.resolve(),
            dry_run=args.dry_run,
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.command == "check-duplicate":
        try:
            auth = publish_x.oauth()
            state, can_post = check_duplicate(
                args.state_path.resolve(),
                auth=auth,
                reconcile=args.reconcile,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        _write_github_output(args.github_output, can_post, first_text(state.get("state")))
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.command == "arm":
        state = arm_post(
            args.state_path.resolve(),
            confirmation=args.confirmation,
            actor=args.actor,
            owner=args.owner,
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0


    if args.command == "post":
        try:
            state = publish_armed_post(
                args.state_path.resolve(),
                args.card_path.resolve(),
                confirmation=args.confirmation,
                actor=args.actor,
                owner=args.owner,
                auth=publish_x.oauth(),
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
