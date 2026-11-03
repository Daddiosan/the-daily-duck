"""Restore the verified November 2 story into this isolated evidence folder only.

No network calls, production state changes, model calls, or publication.
The Gmail token was checked in memory before approval_evidence.json was written.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent
REPO = TASK.parents[1]
sys.path.insert(0, str(REPO))
from scripts import check_story_approval as gate

evidence = json.loads((TASK / 'approval_evidence.json').read_text(encoding='utf-8'))
source = TASK / 'source/gate_a_package.json'
package = json.loads(source.read_text(encoding='utf-8'))
base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
assert base == evidence['base_sha'], 'Unexpected code baseline'
assert package['issue_date'] == evidence['issue_date'] == '2026-11-02'
assert package['state'] == 'WAITING_STORY_SELECTION'
assert package['approval_token_digest'] == evidence['approval_token_digest']
assert evidence['token_valid'] and evidence['subject_matches'] and evidence['candidate_fields_match']
assert evidence['approval_command'] == '3' and evidence['story_number'] == 3
assert len(package['story_options']) == 5
story = package['story_options'][2]
assert story['candidate_number'] == 3 and story['id'] == 46
assert story['url'] == 'https://science.nasa.gov/earth/earth-observatory/vega-island-frozen-in-fright/'

gate.STATE_DIR = TASK / 'candidate/automation_state'
gate.APPROVED_PATH = gate.STATE_DIR / 'approved_story.json'
assert not gate.APPROVED_PATH.exists(), 'Candidate already exists; inspect instead of overwriting'
gate.save_approved_story(package, 3, evidence['approval_sender'], '3')
approved = json.loads(gate.APPROVED_PATH.read_text(encoding='utf-8'))
assert approved['selected_story'] == story
assert approved['issue_date'] == '2026-11-02' and approved['selected_story_number'] == 3
assert approved['state'] == 'APPROVED_STORY' and approved['image_concept_status'] == 'NOT_GENERATED'
report = {
    'result': 'PASS', 'kind': 'offline_restoration_verification',
    'issue_date': '2026-11-02', 'selected_story_number': 3, 'selected_story_id': 46,
    'code_base_sha': base, 'source_run_id': evidence['source_run_id'],
    'artifact_id': evidence['artifact_id'],
    'source_package_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'candidate_sha256': hashlib.sha256(gate.APPROVED_PATH.read_bytes()).hexdigest(),
    'approved_story_unchanged_from_source': True,
    'live_model_integration': 'NOT_RUN_COST_HOLD',
    'design_email_sent': False, 'website_published': False, 'x_posted': False,
}
(TASK / 'verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, indent=2))
