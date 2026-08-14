THE DAILY DUCK — CURRENT REPOSITORY + GEMINI 429 AUTO-RETRY

This package was rebuilt from the repository ZIP you just supplied.

Gemini scripts detected:
- generate_image_concepts.py
- send_email.py

Patched scripts:
- generate_image_concepts.py
- send_email.py

Added:
- scripts/gemini_retry.py
- scripts/gemini_retry_audit.py
- .github/workflows/gemini-retry-audit.yml

429 behavior:
- Temporary 429 / RESOURCE_EXHAUSTED / rate-limit errors are retried automatically.
- Gemini's suggested retry delay is honored when available.
- Conservative exponential waiting is used as a floor.
- Maximum attempts: 4 total.
- Permanent/non-429 errors fail immediately.
- If all attempts fail, the workflow still fails normally so the existing failure notification can run.

IMPORTANT:
This does not remove manual Run workflow controls and does not intentionally
change the existing Website/X duplicate-post protections.

TEST:
1. Upload/replace this package in GitHub.
2. Actions -> The Daily Duck - Gemini Retry Audit -> Run workflow.
3. Confirm green: GEMINI RETRY AUDIT PASSED.
4. Also run The Daily Duck - Automation Chain Audit if present.
5. Do not republish today's already-published issue just for this test.
