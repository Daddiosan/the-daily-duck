from pathlib import Path

scripts = Path("scripts")
helper = scripts / "gemini_retry.py"
if not helper.exists():
    raise SystemExit("MISSING scripts/gemini_retry.py")

errors = []
gemini_scripts = []
for p in scripts.glob("*.py"):
    if p.name in {"gemini_retry.py", "gemini_retry_audit.py"}:
        continue
    text = p.read_text(encoding="utf-8", errors="ignore")
    if "client.models.generate_content(" in text:
        gemini_scripts.append(p)
        if "call_with_retry(lambda: client.models.generate_content(" not in text:
            errors.append(f"{p}: unwrapped Gemini generate_content call")
        if "from gemini_retry import call_with_retry" not in text:
            errors.append(f"{p}: retry helper import missing")

if errors:
    print("GEMINI RETRY AUDIT FAILED")
    for e in errors:
        print("-", e)
    raise SystemExit(1)

print("GEMINI RETRY AUDIT PASSED")
print("Gemini scripts checked:")
for p in gemini_scripts:
    print("-", p)
