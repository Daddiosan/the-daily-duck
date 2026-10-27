#!/usr/bin/env python3
"""Isolated final-selection recovery for the 2026-10-26 Daily Duck issue."""

from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from typing import Any

try:
    from scripts import check_design_selection as normal_selection
    from scripts import publish_website
    from scripts.approval_token import extract_approval_token, validate_approval_token
except ImportError:
    import check_design_selection as normal_selection  # type: ignore[no-redef]
    import publish_website  # type: ignore[no-redef]
    from approval_token import (  # type: ignore[no-redef]
        extract_approval_token,
        validate_approval_token,
    )


TARGET_DATE = "2026-10-26"
TARGET_BATCH = 1
TARGET_IMAGE = 3
TARGET_TITLE = 3
TARGET_CONCEPT = "Escape by Rolling"
TARGET_TITLE_TEXT = "ESCAPE BY ROLLING!"
TARGET_ARTICLE = "Tiny Snakes Roll Four Times Faster Than They Slither"
TARGET_REPLY = "3 3"
AUTHORIZED_SENDER = "kobakoba_jp2002@yahoo.co.jp"
GMAIL_MESSAGE_ID = "1a17d0b3329da4d5"
GMAIL_THREAD_ID = "1a17d0644c52ebc2"
EXPECTED_REPLY_AT = "2026-10-27T20:07:24+09:00"
SOURCE_RUN_ID = 40492737035
SOURCE_ARTIFACT_ID = 12729029530
SOURCE_ARTIFACT_NAME = "daily-duck-backfill-2026-10-26-final"
SOURCE_ARTIFACT_DIGEST = (
    "sha256:47c9ef4e5afcc5f510a7cead914a3f2ff0a1749f211d7c1d9960fdc8c0ea7697"
)
SOURCE_COMMIT = "850a95b270621d59030e7630612bf62230c1d318"
SOURCE_WORKFLOW_PATH = ".github/workflows/backfill-2026-10-26-design-options.yml"
OUTPUT_ARTIFACT_NAME = "daily-duck-backfill-2026-10-26-ready-to-publish"
SUBJECT_PREFIX = "The Daily Duck — Choose Image + Title — 2026-10-26 — Batch 1"
SELECTED_PREVIEW = Path(
    "automation_images/design_previews/2026-10-26/batch_01/preview_3.png"
)
CANONICAL_IMAGE = Path("automation_images/canonical/2026-10-26.png")
OPTIONS_PATH = Path("automation_state/design_options.json")
APPROVED_PATH = Path("automation_state/approved_story.json")
READY_PATH = Path("automation_state/ready_to_publish.json")
RESULT_PATH = Path("automation_state/design_selection_result.json")
REPORT_PATH = Path("automation_state/final_selection_backfill_report.json")
EXPECTED_FILES = {
    "automation_state/approved_story.json":
        "e6421c2041c10c044e4d6e857ef06fd4b99a77b2601e861b77027272a2917139",
    "automation_state/design_options.json":
        "ab0fbf5632f5710c365355fe763927b7142e5ad703edf344825a26a01276097e",
    "automation_images/design_previews/2026-10-26/batch_01/preview_1.png":
        "aaaed8bf2185ad605a65f7810791ebbb6fb5eb112432c66a6a845c1039bfaac6",
    "automation_images/design_previews/2026-10-26/batch_01/preview_2.png":
        "5144cb277025f8bc48cf96cd3761844a36cb76e4f6e2f653cbab5087685b41e5",
    "automation_images/design_previews/2026-10-26/batch_01/preview_3.png":
        "1774c9be3c89ad3c8050be2ab1532dadae81f0fc2633eb7afca55cafe8686ae4",
}


class FinalSelectionError(RuntimeError):
    """Recovery evidence is missing, inconsistent, or out of scope."""


@dataclass(frozen=True)
class ReplyEvidence:
    sender: str
    normalized_reply: str
    gmail_message_id: str
    gmail_thread_id: str
    rfc_message_id: str
    reply_at: str


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalSelectionError(f"Cannot read valid JSON from {path}.") from exc
    if not isinstance(value, dict):
        raise FinalSelectionError(f"{path} must contain a JSON object.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FinalSelectionError(f"Cannot hash {path}.") from exc
    return digest.hexdigest()


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalSelectionError(f"{field} is missing.")
    return value.strip()


def validate_artifact_metadata(path: Path) -> dict[str, Any]:
    metadata = load_json(path)
    run = metadata.get("workflow_run")
    checks = {
        "artifact id": metadata.get("id") == SOURCE_ARTIFACT_ID,
        "artifact name": metadata.get("name") == SOURCE_ARTIFACT_NAME,
        "artifact digest": metadata.get("digest") == SOURCE_ARTIFACT_DIGEST,
        "artifact active": metadata.get("expired") is False,
        "source run": isinstance(run, dict) and run.get("id") == SOURCE_RUN_ID,
        "source commit": isinstance(run, dict) and run.get("head_sha") == SOURCE_COMMIT,
        "source branch": isinstance(run, dict) and run.get("head_branch") == "main",
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise FinalSelectionError("Invalid source metadata: " + ", ".join(failed))
    return metadata


def validate_run_metadata(path: Path) -> dict[str, Any]:
    metadata = load_json(path)
    checks = {
        "run id": metadata.get("id") == SOURCE_RUN_ID,
        "run status": metadata.get("status") == "completed",
        "run conclusion": metadata.get("conclusion") == "success",
        "run event": metadata.get("event") == "workflow_dispatch",
        "run workflow": metadata.get("path") == SOURCE_WORKFLOW_PATH,
        "run commit": metadata.get("head_sha") == SOURCE_COMMIT,
        "run branch": metadata.get("head_branch") == "main",
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise FinalSelectionError("Invalid source run: " + ", ".join(failed))
    return metadata


def _numbered(items: object, number: int, field: str) -> dict[str, Any]:
    if not isinstance(items, list) or len(items) != 3:
        raise FinalSelectionError(f"{field} must contain exactly 3 entries.")
    found = [item for item in items if isinstance(item, dict) and item.get("number") == number]
    if len(found) != 1:
        raise FinalSelectionError(f"{field} number {number} is missing or duplicated.")
    return found[0]


def validate_source(
    root: Path,
    metadata_path: Path,
    run_metadata_path: Path,
) -> dict[str, Any]:
    validate_artifact_metadata(metadata_path)
    validate_run_metadata(run_metadata_path)
    for relative, expected in EXPECTED_FILES.items():
        actual = sha256_file(root / relative)
        if actual != expected:
            raise FinalSelectionError(f"Source hash mismatch: {relative}.")

    options = load_json(root / OPTIONS_PATH)
    approved = load_json(root / APPROVED_PATH)
    if options.get("state") != "WAITING_FINAL_SELECTION":
        raise FinalSelectionError("Source is not waiting for final selection.")
    if options.get("issue_date") != TARGET_DATE or approved.get("issue_date") != TARGET_DATE:
        raise FinalSelectionError("Source issue date is not the fixed target date.")
    if int(options.get("preview_batch_number", 0) or 0) != TARGET_BATCH:
        raise FinalSelectionError("Source preview batch is not 1.")
    if options.get("final_email_subject_prefix") != SUBJECT_PREFIX:
        raise FinalSelectionError("Source subject prefix is not the approved Batch 1 subject.")
    if approved.get("selected_story_number") != 1:
        raise FinalSelectionError("Gate A story number is not 1.")
    story = approved.get("selected_story")
    if not isinstance(story, dict) or story.get("title_en") != TARGET_ARTICLE:
        raise FinalSelectionError("Gate A article identity does not match.")
    backfill = options.get("backfill")
    if not isinstance(backfill, dict):
        raise FinalSelectionError("Source backfill provenance is missing.")
    if (
        str(backfill.get("recovery_workflow_run_id")) != str(SOURCE_RUN_ID)
        or backfill.get("isolated") is not True
        or backfill.get("publication_allowed") is not False
    ):
        raise FinalSelectionError("Source backfill provenance is invalid.")

    concept = _numbered(options.get("image_concepts"), TARGET_IMAGE, "image_concepts")
    preview = _numbered(options.get("design_previews"), TARGET_IMAGE, "design_previews")
    title = _numbered(options.get("title_ideas"), TARGET_TITLE, "title_ideas")
    if concept.get("title_en") != TARGET_CONCEPT:
        raise FinalSelectionError("Selected image concept is not Escape by Rolling.")
    if preview.get("image_path") != SELECTED_PREVIEW.as_posix():
        raise FinalSelectionError("Selected preview path is invalid.")
    if preview.get("sha256") != EXPECTED_FILES[SELECTED_PREVIEW.as_posix()]:
        raise FinalSelectionError("Selected preview recorded hash is invalid.")
    if title.get("title") != TARGET_TITLE_TEXT:
        raise FinalSelectionError("Selected title text is invalid.")
    return options


def validate_message(
    message: email.message.Message,
    options: dict[str, Any],
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    allowed_senders: set[str],
) -> ReplyEvidence:
    if gmail_message_id != GMAIL_MESSAGE_ID or gmail_thread_id != GMAIL_THREAD_ID:
        raise FinalSelectionError("Gmail message or thread identity does not match.")
    subject = normal_selection.decode_mime(message.get("Subject"))
    if SUBJECT_PREFIX not in subject:
        raise FinalSelectionError("Reply subject does not identify the target issue and batch.")
    sender = parseaddr(normal_selection.decode_mime(message.get("From")))[1].lower()
    if sender != AUTHORIZED_SENDER or sender not in allowed_senders:
        raise FinalSelectionError("Reply sender is not authorized.")
    token = extract_approval_token(subject)
    if not validate_approval_token(
        stored_digest=options.get("approval_token_digest"),
        stage="DESIGN_SELECTION",
        issue_date=TARGET_DATE,
        batch=TARGET_BATCH,
        token=token,
    ):
        raise FinalSelectionError("DESIGN_SELECTION approval token is invalid.")
    command = normal_selection.extract_command(normal_selection.message_text(message))
    if command != ("FINAL", TARGET_IMAGE, TARGET_TITLE, TARGET_REPLY):
        raise FinalSelectionError("Fresh reply is not exactly the approved 3 3 selection.")
    try:
        reply_at = parsedate_to_datetime(required_text(message.get("Date"), "Date"))
    except (TypeError, ValueError) as exc:
        raise FinalSelectionError("Reply Date header is invalid.") from exc
    if reply_at.isoformat() != EXPECTED_REPLY_AT:
        raise FinalSelectionError("Reply timestamp does not match the approved evidence.")
    return ReplyEvidence(
        sender=sender,
        normalized_reply=TARGET_REPLY,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        rfc_message_id=str(message.get("Message-ID") or "").strip(),
        reply_at=reply_at.isoformat(),
    )


def fetch_and_validate_reply(options: dict[str, Any]) -> ReplyEvidence:
    allowed = normal_selection.allowed_senders()
    decimal_message = str(int(GMAIL_MESSAGE_ID, 16))
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(
            normal_selection.required_env("GMAIL_ADDRESS"),
            normal_selection.required_env("GMAIL_APP_PASSWORD"),
        )
        imap.select("INBOX")
        status, data = imap.search(None, "X-GM-MSGID", decimal_message)
        ids = data[0].split() if status == "OK" and data else []
        if len(ids) != 1:
            raise FinalSelectionError("Exact Gmail reply was not found uniquely.")
        status, payload = imap.fetch(ids[0], "(X-GM-MSGID X-GM-THRID RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            raise FinalSelectionError("Exact Gmail reply could not be fetched.")
        metadata = payload[0][0].decode("ascii", errors="replace")
        message_match = re.search(r"X-GM-MSGID\s+(\d+)", metadata)
        thread_match = re.search(r"X-GM-THRID\s+(\d+)", metadata)
        if not message_match or not thread_match:
            raise FinalSelectionError("Gmail identity metadata is missing.")
        gmail_message_id = format(int(message_match.group(1)), "x")
        gmail_thread_id = format(int(thread_match.group(1)), "x")
        message = email.message_from_bytes(payload[0][1])
    return validate_message(
        message,
        options,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        allowed_senders=allowed,
    )


def publication_compatibility(ready: dict[str, Any]) -> dict[str, Any]:
    try:
        item = publish_website.build_item(ready, "assets/ducks/2026-10-26.png")
    except (TypeError, ValueError) as exc:
        raise FinalSelectionError(
            f"Website publication contract rejected the ready package: {exc}"
        ) from exc
    actual = item.get("storyTitleEn")
    compatible = actual == TARGET_TITLE_TEXT
    return {
        "status": "PASS" if compatible else "BLOCKED_SELECTED_TITLE_NOT_USED_BY_WEBSITE",
        "selected_title": TARGET_TITLE_TEXT,
        "website_story_title": actual,
        "archive_order": "DATE_DESCENDING",
        "x_input_contract": "READY_FIELDS_PRESENT_X_NOT_EXECUTED",
    }


def _normal_finalize(root: Path, options: dict[str, Any], evidence: ReplyEvidence) -> None:
    previous = Path.cwd()
    os.chdir(root)
    try:
        normal_selection.finalize_selection(
            options,
            TARGET_IMAGE,
            TARGET_TITLE,
            evidence.sender,
            evidence.normalized_reply,
        )
    finally:
        os.chdir(previous)


def finalize(
    root: Path,
    metadata_path: Path,
    run_metadata_path: Path,
    evidence: ReplyEvidence | None = None,
) -> str:
    if (root / READY_PATH).exists() or (root / REPORT_PATH).exists():
        validate_ready(root)
        print("STATE: ALREADY_READY")
        return "ALREADY_READY"
    options = validate_source(root, metadata_path, run_metadata_path)
    evidence = evidence or fetch_and_validate_reply(options)
    _normal_finalize(root, options, evidence)

    source_hash = sha256_file(root / SELECTED_PREVIEW)
    canonical_hash = sha256_file(root / CANONICAL_IMAGE)
    if source_hash != canonical_hash:
        raise FinalSelectionError("Canonical image hash differs from selected preview.")

    ready = load_json(root / READY_PATH)
    selected_options = load_json(root / OPTIONS_PATH)
    provenance = {
        "target_issue_date": TARGET_DATE,
        "source_run_id": SOURCE_RUN_ID,
        "source_artifact_id": SOURCE_ARTIFACT_ID,
        "source_artifact_digest": SOURCE_ARTIFACT_DIGEST,
        "gmail_message_id": evidence.gmail_message_id,
        "gmail_thread_id": evidence.gmail_thread_id,
        "gmail_rfc_message_id": evidence.rfc_message_id,
        "reply_at": evidence.reply_at,
        "source_image_sha256": source_hash,
        "canonical_image_sha256": canonical_hash,
        "isolated": True,
        "publication_allowed": False,
        "recovery_workflow_run_id": os.getenv("GITHUB_RUN_ID", "LOCAL_DRY_RUN"),
        "recovery_commit_sha": os.getenv("GITHUB_SHA", "LOCAL_DRY_RUN"),
    }
    ready["canonical_image_path"] = CANONICAL_IMAGE.as_posix()
    ready["publish_started"] = False
    ready["final_selection_backfill"] = provenance
    selected_options["final_selection_backfill"] = provenance
    compatibility = publication_compatibility(ready)
    report = {
        "state": "READY_TO_PUBLISH",
        "issue_date": TARGET_DATE,
        "selected_image_number": TARGET_IMAGE,
        "selected_image_concept_number": TARGET_IMAGE,
        "selected_image_concept": TARGET_CONCEPT,
        "selected_title_number": TARGET_TITLE,
        "selected_title": TARGET_TITLE_TEXT,
        "design_approval_reply": TARGET_REPLY,
        "gmail_reply_verified": True,
        "approval_token_verified": True,
        "source_image_sha256": source_hash,
        "canonical_image_sha256": canonical_hash,
        "image_hash_match": True,
        "publish_started": False,
        "website_publish_executed": False,
        "x_publish_executed": False,
        "publication_compatibility": compatibility,
        "provenance": provenance,
    }
    write_json(root / READY_PATH, ready)
    write_json(root / OPTIONS_PATH, selected_options)
    write_json(root / REPORT_PATH, report)
    validate_ready(root)
    print("GMAIL_REPLY_VERIFIED: YES")
    print("APPROVAL_TOKEN_VERIFIED: YES")
    print(f"SOURCE_IMAGE_SHA256: {source_hash}")
    print(f"CANONICAL_IMAGE_SHA256: {canonical_hash}")
    print("IMAGE_HASH_MATCH: YES")
    print("STATE: READY_TO_PUBLISH")
    return "CREATED"


def validate_ready(root: Path) -> dict[str, Any]:
    ready = load_json(root / READY_PATH)
    options = load_json(root / OPTIONS_PATH)
    report = load_json(root / REPORT_PATH)
    source_hash = sha256_file(root / SELECTED_PREVIEW)
    canonical_hash = sha256_file(root / CANONICAL_IMAGE)
    report_compatibility = report.get("publication_compatibility")
    report_compatibility_status = (
        report_compatibility.get("status")
        if isinstance(report_compatibility, dict)
        else None
    )
    checks = {
        "ready state": ready.get("state") == "READY_TO_PUBLISH",
        "ready date": ready.get("issue_date") == TARGET_DATE,
        "options state": options.get("state") == "DESIGN_SELECTED_READY_TO_PUBLISH",
        "image number": ready.get("selected_image_number") == TARGET_IMAGE,
        "concept number": ready.get("selected_image_concept_number") == TARGET_IMAGE,
        "title number": ready.get("selected_title_number") == TARGET_TITLE,
        "title text": ready.get("selected_title") == TARGET_TITLE_TEXT,
        "reply": ready.get("design_approval_reply") == TARGET_REPLY,
        "publish not started": ready.get("publish_started") is False,
        "canonical path": ready.get("canonical_image_path") == CANONICAL_IMAGE.as_posix(),
        "image hash": source_hash == canonical_hash,
        "report state": report.get("state") == "READY_TO_PUBLISH",
        "report hash": report.get("image_hash_match") is True,
        # Preserve the already-issued PR #7 report as immutable evidence. Its
        # historical blocker is accepted here; the current website contract is
        # recomputed below and must now pass.
        "report compatibility evidence": report_compatibility_status in {
            "PASS",
            "BLOCKED_SELECTED_TITLE_NOT_USED_BY_WEBSITE",
        },
        "website not run": report.get("website_publish_executed") is False,
        "x not run": report.get("x_publish_executed") is False,
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise FinalSelectionError("Invalid ready package: " + ", ".join(failed))
    compatibility = publication_compatibility(ready)
    if compatibility["status"] != "PASS":
        raise FinalSelectionError("Selected title is not publication-compatible.")
    return report


def check_duplicate_artifacts(path: Path, github_output: Path | None) -> bool:
    payload = load_json(path)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise FinalSelectionError("Artifact listing is invalid.")
    existing = [
        item for item in artifacts
        if isinstance(item, dict)
        and item.get("name") == OUTPUT_ARTIFACT_NAME
        and item.get("expired") is False
    ]
    should_create = not existing
    if github_output:
        with github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"should_create={'true' if should_create else 'false'}\n")
            stream.write(
                "existing_artifact_id="
                + (str(existing[0].get("id")) if existing else "")
                + "\n"
            )
    print("READY_ARTIFACT_ALREADY_EXISTS: " + ("YES" if existing else "NO"))
    return should_create


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-source")
    validate.add_argument("--root", type=Path, default=Path("."))
    validate.add_argument("--artifact-metadata", type=Path, required=True)
    validate.add_argument("--run-metadata", type=Path, required=True)
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--root", type=Path, default=Path("."))
    finalize_parser.add_argument("--artifact-metadata", type=Path, required=True)
    finalize_parser.add_argument("--run-metadata", type=Path, required=True)
    ready = sub.add_parser("validate-ready")
    ready.add_argument("--root", type=Path, default=Path("."))
    duplicate = sub.add_parser("check-duplicate-artifacts")
    duplicate.add_argument("--listing", type=Path, required=True)
    duplicate.add_argument("--github-output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "validate-source":
        validate_source(
            args.root.resolve(),
            args.artifact_metadata.resolve(),
            args.run_metadata.resolve(),
        )
        print("SOURCE_ARTIFACT_VERIFIED: YES")
    elif args.command == "finalize":
        finalize(
            args.root.resolve(),
            args.artifact_metadata.resolve(),
            args.run_metadata.resolve(),
        )
    elif args.command == "validate-ready":
        validate_ready(args.root.resolve())
        print("SCHEMA_VALIDATION: PASS")
    elif args.command == "check-duplicate-artifacts":
        check_duplicate_artifacts(args.listing, args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

