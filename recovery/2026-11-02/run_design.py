"""One-date recovery adapter; reuse the existing generator and design-email formatter.

Only --offline is permitted outside the explicitly confirmed recovery Actions run.
No production files, Gemini calls, text API calls, publication, or automatic retry.
"""
import email
import hashlib
import imaplib
import json
import os
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, make_msgid, parseaddr
from pathlib import Path
from types import SimpleNamespace

TASK = Path(__file__).resolve().parent
REPO = TASK.parents[1]
CANDIDATE = TASK / "candidate"
EVIDENCE = TASK / "execution"
sys.path.insert(0, str(REPO))
from scripts import generate_image_concepts as generator
from scripts import send_design_approval_email as mailer
from scripts.approval_token import (
    approval_token_digest, extract_approval_token, generate_approval_token,
)

DATE = "2026-11-02"
BRANCH = "refs/heads/recovery/2026-11-02"
SUBJECT = f"The Daily Duck — Choose Image + Title — {DATE} — Batch 1"
CAP = 0.50
RESERVE = 0.13
MAX_PROMPT_BYTES = 6000
SDK = generator.OpenAI


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def live_guard():
    expected = {
        "GITHUB_REF": BRANCH, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "Daddiosan/the-daily-duck",
        "GITHUB_ACTOR": "Daddiosan", "GITHUB_TRIGGERING_ACTOR": "Daddiosan",
        "GITHUB_RUN_ATTEMPT": "1",
        "RECOVERY_CONFIRMATION": "GENERATE-AND-EMAIL-2026-11-02-USD050",
    }
    require(all(os.getenv(k) == v for k, v in expected.items()), "Recovery execution identity mismatch")


def inputs():
    approved = read(CANDIDATE / "automation_state/approved_story.json")
    verified = read(TASK / "verification.json")
    require(approved["issue_date"] == DATE and approved["selected_story_number"] == 3,
            "Wrong issue or story")
    require(approved["selected_story"]["id"] == 46 and approved["approval_reply"] == "3",
            "Wrong source story")
    require(hashlib.sha256((CANDIDATE / "automation_state/approved_story.json").read_bytes()).hexdigest()
            == verified["candidate_sha256"], "Restored approval bytes changed")
    brief = read(TASK / "design_brief.json")
    require(brief["issue_date"] == DATE and brief["selected_story_id"] == 46, "Wrong brief")
    generator.validate_design_options_result(brief)
    concepts, titles = generator.normalize_design_options(brief)
    prompts = [generator.build_image_prompt(approved["selected_story"], c, i, 0)
               for i, c in enumerate(concepts, 1)]
    sizes = [len(prompt.encode("utf-8")) for prompt in prompts]
    require(all(size <= MAX_PROMPT_BYTES for size in sizes), "Prompt exceeds cost envelope")
    return approved, concepts, titles, prompts


def offline():
    _, _, titles, prompts = inputs()
    # Conservatively use UTF-8 byte count as a text-token ceiling.
    ceiling = sum(len(p.encode("utf-8")) for p in prompts) * 5 / 1_000_000
    plan = {
        "issue_date": DATE, "kind": "ESTIMATE_NOT_INVOICE",
        "image_model": "gpt-image-2", "quality": "medium", "size": "1536x1024",
        "image_requests": 3, "automatic_retries": 0, "text_api_requests": 0,
        "image_output_estimate_usd": 0.123, "input_upper_estimate_usd": round(ceiling, 6),
        "total_estimate_usd": round(0.123 + ceiling, 6),
        "conservative_request_reserve_usd": RESERVE,
        "three_request_reserve_usd": round(3 * RESERVE, 2), "user_cap_usd": CAP,
        "prompt_utf8_bytes": [len(p.encode("utf-8")) for p in prompts],
        "pricing_source": "https://developers.openai.com/api/docs/pricing",
        "image_estimate_source": "https://developers.openai.com/api/docs/guides/image-generation",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "paid_api_calls_made_by_offline_check": 0,
    }
    require(plan["total_estimate_usd"] <= 3 * RESERVE <= CAP, "Cost hold")
    write(EVIDENCE / "cost_plan.json", plan)
    for i, prompt in enumerate(prompts, 1):
        (EVIDENCE / f"image_prompt_{i}.txt").write_text(prompt + "\n", encoding="utf-8")
    print(json.dumps(plan, indent=2))
    print("Titles:", " / ".join(t["title"] for t in titles))


class BudgetImages:
    def __init__(self, endpoint, ledger):
        self.endpoint = endpoint
        self.ledger = ledger

    def generate(self, **request):
        require(request.get("model") == "gpt-image-2" and request.get("n") == 1
                and request.get("size") == "1536x1024"
                and request.get("quality") == "medium"
                and request.get("output_format") == "png", "Unapproved image settings")
        require(len(request["prompt"].encode("utf-8")) <= MAX_PROMPT_BYTES, "Prompt cost hold")
        rows = self.ledger["requests"]
        require(len(rows) < 3 and all(r["state"] == "RETURNED" for r in rows),
                "No additional call after limit, failure, or uncertain request")
        charged = sum(r["accounted_usd"] for r in rows)
        require(charged + (3 - len(rows)) * RESERVE <= CAP, "Projected total exceeds USD0.50")
        row = {"number": len(rows) + 1, "state": "IN_FLIGHT", "accounted_usd": RESERVE}
        rows.append(row)
        write(EVIDENCE / "cost_ledger.json", self.ledger)
        try:
            response = self.endpoint.generate(**request)
        except Exception:
            row["state"] = "UNKNOWN_OR_FAILED_NO_RETRY"
            write(EVIDENCE / "cost_ledger.json", self.ledger)
            raise RuntimeError("Image API failed or is uncertain; no retry") from None
        usage = getattr(response, "usage", None)
        if usage is not None:
            u = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
            it, ot = u.get("input_tokens"), u.get("output_tokens")
            if isinstance(it, int) and isinstance(ot, int) and it >= 0 and ot >= 0:
                # All input is text; charge total input at the higher image rate
                # as a conservative estimate even if detail fields are absent.
                row["usage"] = u
                row["usage_cost_upper_estimate_usd"] = round(it * 8e-6 + ot * 30e-6, 8)
                row["accounted_usd"] = max(RESERVE, row["usage_cost_upper_estimate_usd"])
        row["state"] = "RETURNED"
        write(EVIDENCE / "cost_ledger.json", self.ledger)
        require(sum(r["accounted_usd"] for r in rows) + (3 - len(rows)) * RESERVE <= CAP,
                "Updated cost projection exceeds USD0.50; stop before another call")
        return response


def sent_matches():
    address = mailer.required_env("GMAIL_ADDRESS")
    with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30) as box:
        box.login(address, mailer.required_env("GMAIL_APP_PASSWORD"))
        status, folders = box.list()
        require(status == "OK", "Cannot inspect Gmail mailboxes")
        sent = next((r.decode().rsplit(' "', 1)[-1].strip('"')
                     for r in folders if isinstance(r, bytes) and b"\\Sent" in r), None)
        require(sent is not None, "Sent folder not found")
        status, _ = box.select('"' + sent + '"', readonly=True)
        require(status == "OK", "Cannot inspect Sent folder")
        status, results = box.search(None, "SUBJECT", '"' + DATE + '"')
        require(status == "OK" and results is not None, "Cannot search Sent folder")
        found = []
        for ident in results[0].split():
            status, data = box.fetch(ident, "(BODY.PEEK[])")
            require(status == "OK" and data and isinstance(data[0], tuple), "Cannot read sent message")
            msg = email.message_from_bytes(data[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            if ("ChooseImage+Title" in "".join(subject.split())
                    and DATE in subject
                    and parseaddr(msg.get("From", ""))[1].lower() == address.lower()):
                found.append(msg)
        return found


def no_duplicate():
    require(not sent_matches(), "An issue-specific design email already exists; inspect and reuse")
    print("PASS: no November 2 design email in Gmail Sent")


def generate():
    live_guard()
    require(not (EVIDENCE / "cost_ledger.json").exists(), "Prior generation ledger exists; no restart")
    require(not (CANDIDATE / "automation_state/design_options.json").exists(),
            "Design package already exists; inspect and reuse")
    approved, concepts, titles, _ = inputs()
    no_duplicate()
    offline()
    ledger = {"issue_date": DATE, "cap_usd": CAP, "run_id": os.getenv("GITHUB_RUN_ID"),
              "requests": [], "automatic_retries": 0, "invoice_verified": False}
    client = SDK(api_key=mailer.required_env("OPENAI_API_KEY"), max_retries=0,
                 timeout=180, base_url="https://api.openai.com/v1")
    budget = BudgetImages(client.images, ledger)
    generator.OpenAI = lambda **_: SimpleNamespace(images=budget)
    generator.MAX_DUPLICATE_RETRIES = 0
    generator.generate_options = lambda *_: (concepts, titles)
    os.chdir(CANDIDATE)
    try:
        generator.main()
    finally:
        client.close()
    package = read(Path("automation_state/design_options.json"))
    require(package["issue_date"] == DATE and package["approved_story"] == approved, "Wrong generated package")
    mailer.validate_package(package)
    hashes = [p["sha256"] for p in package["design_previews"]]
    require(len(set(hashes)) == 3, "Images are not distinct; no retry")
    write(EVIDENCE / "generation_result.json", {
        "state": "DESIGN_OPTIONS_READY", "issue_date": DATE, "story_number": 3,
        "image_count": 3, "title_count": 3, "image_sha256": hashes,
        "api_request_count": len(ledger["requests"]), "text_api_requests": 0,
    })


def send():
    live_guard()
    os.chdir(CANDIDATE)
    require(not (EVIDENCE / "email_attempt.json").exists(), "Prior SMTP attempt exists; inspect, do not resend")
    no_duplicate()
    package = read(Path("automation_state/design_options.json"))
    require(package["issue_date"] == DATE and package["state"] == "DESIGN_OPTIONS_READY", "Wrong mail package")
    require(package["approved_story"]["selected_story_number"] == 3, "Wrong email story")
    _, _, previews = mailer.validate_package(package)
    token = generate_approval_token()
    subject, body = mailer.build_email(package, token)
    story = package["approved_story"]["selected_story"]
    body = body.replace("Gate A is complete.", (
        f"Selected story: 3\n{story['title_en']}\n{story['title_ja']}\n"
        f"Source: {story['url']}\nRecovery: 2026-11-02 only\n\nGate A is complete."
    ), 1)
    # NEXT 3 is a normal command; this recovery cannot authorize more spending.
    body += "\nRecovery budget: regeneration needs a new cost check; it is not automatic.\n"
    msg = mailer.create_message(subject, body)
    msg["Message-ID"] = make_msgid(domain="thedailyduck.ai")
    mailer.attach_preview_images(msg, previews)
    package["approval_token_digest"] = approval_token_digest(
        stage="DESIGN_SELECTION", issue_date=DATE, batch=1, token=token)
    package["final_email_subject_prefix"] = SUBJECT
    package["state"] = "EMAIL_SEND_PENDING"
    mailer.save_package(package)
    attempt = {"issue_date": DATE, "state": "ATTEMPTING_NO_RETRY",
               "message_id": msg["Message-ID"], "image_count": 3, "title_count": 3}
    write(EVIDENCE / "email_attempt.json", attempt)
    mailer.send_message(msg)
    package["state"] = "WAITING_FINAL_SELECTION"
    package["final_email_sent_at"] = datetime.now(timezone.utc).isoformat()
    package["final_selection_rule"] = {
        "image_numbers": [1, 2, 3], "title_numbers": [1, 2, 3],
        "format": "IMAGE_NUMBER TITLE_NUMBER", "example": "1 3",
        "next_state": "READY_TO_PUBLISH", "next_3_requires_budget_recheck": True,
    }
    mailer.save_package(package)
    attempt["state"] = "SMTP_ACCEPTED_WAITING_SENT_VERIFICATION"
    write(EVIDENCE / "email_attempt.json", attempt)
    matches = [m for m in sent_matches() if m.get("Message-ID") == msg["Message-ID"]]
    require(len(matches) == 1, "SMTP accepted; Sent verification unresolved, do not resend")
    verified = matches[0]
    verified_token = extract_approval_token(str(make_header(decode_header(verified["Subject"]))))
    require(verified_token == token, "Sent approval identity mismatch")
    require({a.lower() for _, a in getaddresses(verified.get_all("To", []))}
            == {a.lower() for a in mailer.recipients_from_env()}, "Sent recipients mismatch")
    attachments = list(verified.iter_attachments()) if hasattr(verified, "iter_attachments") else [
        part for part in verified.walk() if part.get_content_disposition() == "attachment"]
    expected_hashes = {p["sha256"] for p in previews}
    require(len(attachments) == 3 and
            {hashlib.sha256(p.get_payload(decode=True)).hexdigest() for p in attachments} == expected_hashes,
            "Sent attachments mismatch")
    attempt["state"] = "SENT_VERIFIED_WAITING_HUMAN_SELECTION"
    write(EVIDENCE / "email_attempt.json", attempt)
    print("PASS: design email sent and verified; three images and three titles; waiting for human approval")


if __name__ == "__main__":
    try:
        {"--offline": offline, "--generate": generate, "--send": send,
         "--check-mail": lambda: (live_guard(), no_duplicate())}[sys.argv[1]]()
    except Exception as exc:
        # Provider and mail exceptions can contain sensitive strings. Log type only.
        print("STOP:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
