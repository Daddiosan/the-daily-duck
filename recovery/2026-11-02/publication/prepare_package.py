"""Finalize the verified 1 1 reply using existing selection logic, in isolation."""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from scripts import check_design_selection as selection
from scripts import publish_website

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

evidence = read(HERE / 'selection_approval.json')
assert evidence['issue_date'] == '2026-11-02' and evidence['reply'] == '1 1'
assert evidence['token_binding_verified'] and evidence['image_number'] == evidence['title_number'] == 1
source = HERE.parent / 'live-run/artifact/candidate'
options = read(source / 'automation_state/design_options.json')
assert options['issue_date'] == '2026-11-02' and options['state'] == 'WAITING_FINAL_SELECTION'
assert options['approval_token_digest'] == evidence['approval_token_digest']
assert options['approved_story']['selected_story_number'] == 3
assert options['approved_story']['selected_story']['id'] == 46
assert options['image_concepts'][0]['title_en'] == evidence['concept']
assert options['title_ideas'][0]['title'] == evidence['title']
assert selection.extract_command(evidence['reply']) == ('FINAL', 1, 1, '1 1')
selected = source / options['design_previews'][0]['image_path']
assert hashlib.sha256(selected.read_bytes()).hexdigest() == evidence['image_sha256']
package = HERE / 'package'
assert not package.exists(), 'Publication package already exists; inspect instead of overwriting'
(package / 'automation_state').mkdir(parents=True)
preview = package / options['design_previews'][0]['image_path']
preview.parent.mkdir(parents=True)
shutil.copy2(selected, preview)
shutil.copy2(source / 'automation_state/approved_story.json', package / 'automation_state/approved_story.json')
os.chdir(package)
assert selection.finalize_selection(options, 1, 1, evidence['sender'], '1 1') == 0
ready_path = Path('automation_state/ready_to_publish.json')
ready = read(ready_path)
canonical = Path(ready['canonical_image_path'])
assert hashlib.sha256(canonical.read_bytes()).hexdigest() == evidence['image_sha256']
ready['final_selection_backfill'] = dict(
    target_issue_date='2026-11-02', isolated=True, publication_allowed=False,
    source_run_id=evidence['source_run_id'], source_artifact_id=evidence['source_artifact_id'],
    source_artifact_digest=evidence['source_artifact_digest'],
    gmail_message_id=evidence['gmail_reply_id'], gmail_thread_id=evidence['gmail_thread_id'],
    reply_at=evidence['reply_at_jst'], source_image_sha256=evidence['image_sha256'],
    canonical_image_sha256=evidence['image_sha256'])
assert ready['selected_title'] == 'A CHILL DOWN YOUR FEATHERS'
publish_website._validate_publication_request(ready, 'ARCHIVE_ONLY', '2026-11-02')
assert publish_website.build_item(ready, 'assets/ducks/2026-11-02-skullcape.png')['storyTitleEn'] == evidence['title']
ready_path.write_text(json.dumps(ready, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('PASS: isolated READY_TO_PUBLISH for November 2, image 1, title 1; canonical bytes unchanged')
