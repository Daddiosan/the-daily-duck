"""Issue-bound approval tokens shared by the existing Gmail workflows."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets


TOKEN_BYTES = 24
TOKEN_LABEL = "Approval Token"
ALLOWED_STAGES = frozenset({"GATE_A", "DESIGN_SELECTION"})
_TOKEN_RE = re.compile(r" — Approval Token ([A-Za-z0-9_-]{32})$")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def generate_approval_token() -> str:
    """Return an unpadded URL-safe token containing exactly 192 random bits."""

    token = secrets.token_urlsafe(TOKEN_BYTES)
    if len(token) != 32 or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise RuntimeError("Approval token generation returned an invalid value.")
    return token


def subject_with_approval_token(subject_prefix: str, token: str) -> str:
    if not isinstance(subject_prefix, str) or not subject_prefix.strip():
        raise ValueError("Approval subject prefix is required.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Approval token is malformed.")
    return f"{subject_prefix} — {TOKEN_LABEL} {token}"


def extract_approval_token(subject: object) -> str | None:
    if not isinstance(subject, str):
        return None
    match = _TOKEN_RE.search(subject)
    return match.group(1) if match else None


def approval_token_digest(
    *, stage: str, issue_date: str, batch: int | None, token: str
) -> str:
    if stage not in ALLOWED_STAGES:
        raise ValueError("Approval stage is invalid.")
    if not isinstance(issue_date, str) or not issue_date.strip():
        raise ValueError("Approval issue date is required.")
    if batch is not None and (
        isinstance(batch, bool) or not isinstance(batch, int) or batch < 1
    ):
        raise ValueError("Approval batch must be a positive integer.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Approval token is malformed.")
    material = "\0".join(
        (
            "daily-duck-approval-token:v1",
            stage,
            issue_date.strip(),
            str(batch) if batch is not None else "-",
            token,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_approval_token(
    *,
    stored_digest: object,
    stage: str,
    issue_date: str,
    batch: int | None,
    token: object,
) -> bool:
    if not isinstance(stored_digest, str) or not _DIGEST_RE.fullmatch(stored_digest):
        return False
    if not isinstance(token, str):
        return False
    try:
        candidate = approval_token_digest(
            stage=stage,
            issue_date=issue_date,
            batch=batch,
            token=token,
        )
    except ValueError:
        return False
    return hmac.compare_digest(stored_digest, candidate)
