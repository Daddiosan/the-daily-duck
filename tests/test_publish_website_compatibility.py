import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import gate_a_preflight, publish_website


ISSUE = "2026-10-11"
SELECTED_TITLE = "SAFE, SET, PREP!"
LEGACY_TITLE = "A Microbiologist’s Guide to Safer Meal Prep"
JAPANESE_TITLE = "微生物学者が教える、より安全な作り置きガイド"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_content(path, today, archive):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "window.DAILY_DUCK_DATA = "
        + json.dumps(
            {"today": today, "archive": archive},
            ensure_ascii=False,
            indent=2,
        )
        + ";\n",
        encoding="utf-8",
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approved_story():
    return {
        "issue_date": ISSUE,
        "title_en": LEGACY_TITLE,
        "title_ja": JAPANESE_TITLE,
        "en_copy": "Wash hands, separate ingredients, and chill food promptly.",
        "jp_copy": "手を洗い、食材を分け、すぐに冷やします。",
        "duck_en": "A tidy prep rhythm makes safer meals.",
        "duck_jp": "整った準備のリズムが安全な食事につながります。",
        "x_en": "A practical guide to safer meal preparation.",
        "x_jp": "より安全な食事準備の実用ガイドです。",
        "source": "Phys.org",
        "source_url": "https://phys.org/example",
        "duck_name": "Meal Prep Duck",
        "selected_title_number": 2,
        "selected_title": SELECTED_TITLE,
    }


def ready_package(canonical):
    return {
        "state": "READY_TO_PUBLISH",
        "issue_date": ISSUE,
        "canonical_image_path": str(canonical),
        "gate_a_approved_story": approved_story(),
        "selected_image_number": 2,
        "selected_title_number": 2,
        "selected_title": SELECTED_TITLE,
        "selected_title_detail": {
            "number": 2,
            "title": SELECTED_TITLE,
            "meaning_ja": "承認済みの既存説明",
        },
        "design_approval_reply": "2 2",
        "publish_started": False,
        "final_selection_backfill": {
            "target_issue_date": ISSUE,
            "isolated": True,
            "publication_allowed": False,
            "source_image_sha256": digest(canonical),
            "canonical_image_sha256": digest(canonical),
        },
    }


class PublishWebsiteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.ready_path = self.root / "automation_state" / "ready_to_publish.json"
        self.result_path = self.root / "automation_state" / "website_publish_result.json"
        self.archive_ready_path = self.root / "artifact" / "automation_state" / "ready_to_publish.json"
        self.archive_result_path = self.root / "automation_state" / "archive_only_publish_result.json"
        self.today_path = self.root / "data" / "today.json"
        self.archive_path = self.root / "data" / "archive.json"
        self.content_path = self.root / "data" / "content.js"
        self.home_path = self.root / "index.html"
        self.asset_dir = self.root / "assets" / "ducks"
        self.ducks_dir = self.root / "ducks"
        self.canonical = self.root / "automation_images" / "canonical" / f"{ISSUE}.png"
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_bytes(b"approved-image-2")
        self.artifact_canonical = (
            self.root
            / "artifact"
            / "automation_images"
            / "canonical"
            / f"{ISSUE}.png"
        )
        self.artifact_canonical.parent.mkdir(parents=True)
        self.artifact_canonical.write_bytes(self.canonical.read_bytes())

        self.current_today = {
            "date": "2026-10-10",
            "storyTitleEn": "Existing Today",
        }
        self.existing_archive = [
            {"date": "2026-10-10", "storyTitleEn": "Existing Today"},
            {"date": "2026-10-09", "storyTitleEn": "Earlier Story"},
        ]
        write_json(self.ready_path, ready_package(self.canonical))
        archive_ready = ready_package(self.artifact_canonical)
        archive_ready["canonical_image_path"] = f"automation_images/canonical/{ISSUE}.png"
        write_json(self.archive_ready_path, archive_ready)
        write_json(self.today_path, self.current_today)
        write_json(self.archive_path, self.existing_archive)
        write_content(self.content_path, self.current_today, self.existing_archive)
        self.home_path.write_bytes(b"existing-home-page\r\n")

        self.path_patch = patch.multiple(
            publish_website,
            READY=self.ready_path,
            RESULT=self.result_path,
            TODAY=self.today_path,
            ARCHIVE=self.archive_path,
            CONTENT=self.content_path,
            HOME=self.home_path,
            ASSET_DIR=self.asset_dir,
            DUCKS_DIR=self.ducks_dir,
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def run_archive(self, issue=ISSUE):
        return publish_website.main(
            publish_website.ARCHIVE_ONLY_MODE,
            issue,
            self.archive_ready_path,
            self.archive_result_path,
        )

    def test_selected_title_propagates_to_every_article_surface(self):
        ready = ready_package(self.canonical)
        item = publish_website.build_item(ready, "assets/ducks/example.png")
        page = publish_website.render_page(item)

        self.assertEqual(item["storyTitleEn"], SELECTED_TITLE)
        self.assertEqual(item["storyTitleJa"], JAPANESE_TITLE)
        self.assertIn(f"<title>{SELECTED_TITLE} — The Daily Duck</title>", page)
        self.assertIn(f'<meta property="og:title" content="{SELECTED_TITLE} — The Daily Duck">', page)
        self.assertIn(f'<meta name="twitter:title" content="{SELECTED_TITLE} — The Daily Duck">', page)
        self.assertIn(f'<div class="story-title">{SELECTED_TITLE}</div>', page)
        self.assertIn(f'"headline":"{SELECTED_TITLE}"', page)
        self.assertIn(f"<h2>{JAPANESE_TITLE}</h2>", page)

    def test_legacy_package_uses_existing_gate_a_title(self):
        ready = ready_package(self.canonical)
        ready.pop("selected_title")
        ready.pop("selected_title_number")
        ready.pop("selected_title_detail")
        ready.pop("design_approval_reply")
        ready["gate_a_approved_story"].pop("selected_title")
        ready["gate_a_approved_story"].pop("selected_title_number")

        item = publish_website.build_item(ready, "assets/ducks/example.png")

        self.assertEqual(item["storyTitleEn"], LEGACY_TITLE)

    def test_nested_approved_japanese_title_is_preserved(self):
        ready = ready_package(self.canonical)
        ready["gate_a_approved_story"].pop("title_ja")
        ready["gate_a_approved_story"]["selected_story"] = {
            "title_ja": JAPANESE_TITLE,
        }

        item = publish_website.build_item(ready, "assets/ducks/example.png")

        self.assertEqual(item["storyTitleEn"], SELECTED_TITLE)
        self.assertEqual(item["storyTitleJa"], JAPANESE_TITLE)

    def test_invalid_or_conflicting_selected_title_is_rejected(self):
        invalid_values = ("", " padded ", 2, "line\nbreak", "</script>")
        for invalid in invalid_values:
            with self.subTest(value=invalid):
                ready = ready_package(self.canonical)
                ready["selected_title"] = invalid
                with self.assertRaises(ValueError):
                    publish_website.build_item(ready, "assets/ducks/example.png")

        ready = ready_package(self.canonical)
        ready["selected_title_detail"]["title"] = "CONFLICT"
        with self.assertRaises(ValueError):
            publish_website.build_item(ready, "assets/ducks/example.png")

        ready = ready_package(self.canonical)
        ready["design_approval_reply"] = "2 3"
        with self.assertRaises(ValueError):
            publish_website.build_item(ready, "assets/ducks/example.png")

    def test_normal_mode_retains_today_and_home_publication_behavior(self):
        self.home_path.write_text(
            '<meta property="og:title" content="old">\n'
            '<meta property="og:description" content="old">\n'
            '<meta property="og:image" content="old">\n'
            '<meta property="og:image:alt" content="old">\n'
            '<meta name="twitter:title" content="old">\n'
            '<meta name="twitter:description" content="old">\n'
            '<meta name="twitter:image" content="old">\n',
            encoding="utf-8",
        )
        ready = ready_package(self.canonical)
        ready.pop("final_selection_backfill")
        write_json(self.ready_path, ready)

        self.assertEqual(publish_website.main(), 0)

        today = json.loads(self.today_path.read_text(encoding="utf-8"))
        archive = json.loads(self.archive_path.read_text(encoding="utf-8"))
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(today["date"], ISSUE)
        self.assertEqual(today["storyTitleEn"], SELECTED_TITLE)
        self.assertEqual(archive[0]["storyTitleEn"], SELECTED_TITLE)
        self.assertIn(SELECTED_TITLE, self.home_path.read_text(encoding="utf-8"))
        self.assertEqual(result["action"], "PUBLISHED")

    def test_archive_only_updates_archive_in_date_order_without_top_page_pollution(self):
        write_json(
            self.ready_path,
            {"state": "PUBLISHED", "issue_date": "2026-10-10"},
        )
        existing_article = self.ducks_dir / "2026-10-09" / "index.html"
        existing_article.parent.mkdir(parents=True)
        existing_article.write_bytes(b"existing article")
        existing_asset = self.asset_dir / "2026-10-09-earlier-duck.png"
        existing_asset.parent.mkdir(parents=True)
        existing_asset.write_bytes(b"existing image")

        before = {
            "today": self.today_path.read_bytes(),
            "content_today": publish_website._load_content_data()["today"],
            "home": self.home_path.read_bytes(),
            "article": existing_article.read_bytes(),
            "asset": existing_asset.read_bytes(),
        }

        self.assertEqual(
            self.run_archive(),
            0,
        )

        archive = json.loads(self.archive_path.read_text(encoding="utf-8"))
        content = publish_website._load_content_data()
        result = json.loads(self.archive_result_path.read_text(encoding="utf-8"))
        page = self.ducks_dir / ISSUE / "index.html"
        selected_asset = self.asset_dir / f"{ISSUE}-meal-prep-duck.png"

        self.assertEqual([entry["date"] for entry in archive], [ISSUE, "2026-10-10", "2026-10-09"])
        self.assertEqual(archive[0]["storyTitleEn"], SELECTED_TITLE)
        self.assertEqual(content["archive"], archive)
        self.assertEqual(content["today"], before["content_today"])
        self.assertEqual(self.today_path.read_bytes(), before["today"])
        self.assertEqual(self.home_path.read_bytes(), before["home"])
        self.assertEqual(existing_article.read_bytes(), before["article"])
        self.assertEqual(existing_asset.read_bytes(), before["asset"])
        self.assertEqual(selected_asset.read_bytes(), self.canonical.read_bytes())
        self.assertIn(SELECTED_TITLE, page.read_text(encoding="utf-8"))
        self.assertEqual(result["action"], "ARCHIVE_ONLY_PUBLISHED")
        self.assertFalse(result["x_publish_requested"])
        self.assertEqual(
            json.loads(self.archive_ready_path.read_text(encoding="utf-8"))["state"],
            "READY_TO_PUBLISH",
        )
        decision = gate_a_preflight.evaluate_gate_a_relevance(
            state_dir=self.root / "automation_state",
            expected_issue_date=ISSUE,
        )
        self.assertEqual(
            decision.action,
            gate_a_preflight.PreflightAction.CHECK_REQUIRED,
        )

    def test_archive_only_rejects_duplicate_and_rerun_without_content_changes(self):
        self.assertEqual(
            self.run_archive(),
            0,
        )
        protected = {
            path: digest(path)
            for path in (
                self.today_path,
                self.archive_path,
                self.content_path,
                self.home_path,
                self.asset_dir / f"{ISSUE}-meal-prep-duck.png",
                self.ducks_dir / ISSUE / "index.html",
            )
        }
        write_json(self.archive_ready_path, ready_package(self.canonical))

        self.assertEqual(
            self.run_archive(),
            0,
        )

        self.assertEqual(
            json.loads(self.archive_result_path.read_text(encoding="utf-8"))["action"],
            "DUPLICATE_DATE_BLOCKED",
        )
        self.assertEqual({path: digest(path) for path in protected}, protected)

    def test_archive_only_fails_closed_on_bad_date_or_existing_image(self):
        ready = ready_package(self.canonical)
        ready["issue_date"] = "2026-02-30"
        ready["final_selection_backfill"]["target_issue_date"] = "2026-02-30"
        write_json(self.archive_ready_path, ready)
        with self.assertRaises(ValueError):
            self.run_archive("2026-02-30")
        self.assertEqual(
            json.loads(self.archive_path.read_text(encoding="utf-8")),
            self.existing_archive,
        )

        write_json(self.archive_ready_path, ready_package(self.canonical))
        conflict = self.asset_dir / f"{ISSUE}-different-duck.png"
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"do-not-overwrite")
        before = conflict.read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_archive()
        self.assertEqual(conflict.read_bytes(), before)
        self.assertEqual(
            json.loads(self.archive_path.read_text(encoding="utf-8")),
            self.existing_archive,
        )

    def test_archive_only_does_not_depend_on_today_date_order(self):
        self.current_today["date"] = "2026-10-01"
        write_json(self.today_path, self.current_today)
        write_content(self.content_path, self.current_today, self.existing_archive)

        self.assertEqual(
            self.run_archive(),
            0,
        )
        self.assertEqual(
            json.loads(self.today_path.read_text(encoding="utf-8"))["date"],
            "2026-10-01",
        )

    def test_archive_only_sorts_below_a_newer_published_issue(self):
        archive = [
            {"date": "2026-10-12", "storyTitleEn": "Newer Story"},
            *self.existing_archive,
        ]
        write_json(self.archive_path, archive)
        write_content(self.content_path, self.current_today, archive)

        self.assertEqual(self.run_archive(), 0)

        published = json.loads(self.archive_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["date"] for entry in published],
            ["2026-10-12", ISSUE, "2026-10-10", "2026-10-09"],
        )
        target = [entry for entry in published if entry["date"] == ISSUE]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["storyTitleEn"], SELECTED_TITLE)

    def test_invalid_mode_is_rejected_before_reading_or_writing(self):
        before = self.ready_path.read_bytes()
        with self.assertRaises(ValueError):
            publish_website.main("AUTO")
        self.assertEqual(self.ready_path.read_bytes(), before)

    def test_mode_and_issue_binding_fail_closed(self):
        with self.assertRaises(ValueError):
            publish_website.main(publish_website.NORMAL_MODE)
        with self.assertRaises(ValueError):
            publish_website.main(publish_website.ARCHIVE_ONLY_MODE)
        with self.assertRaises(ValueError):
            publish_website.main(
                publish_website.ARCHIVE_ONLY_MODE,
                "2026-10-12",
                self.archive_ready_path,
                self.archive_result_path,
            )

        ready = ready_package(self.canonical)
        ready.pop("final_selection_backfill")
        write_json(self.archive_ready_path, ready)
        with self.assertRaises(ValueError):
            self.run_archive()

        write_json(self.archive_ready_path, ready_package(self.canonical))
        with self.assertRaises(ValueError):
            publish_website.main(
                publish_website.ARCHIVE_ONLY_MODE,
                ISSUE,
                self.ready_path,
                self.archive_result_path,
            )

    def test_archive_only_rejects_image_hash_mismatch(self):
        ready = ready_package(self.canonical)
        ready["final_selection_backfill"]["canonical_image_sha256"] = "0" * 64
        write_json(self.archive_ready_path, ready)

        with self.assertRaises(ValueError):
            self.run_archive()

        self.assertFalse((self.ducks_dir / ISSUE).exists())
        self.assertEqual(
            json.loads(self.archive_path.read_text(encoding="utf-8")),
            self.existing_archive,
        )


class WebsiteWorkflowModeContractTests(unittest.TestCase):
    def test_archive_only_is_explicit_and_cannot_trigger_x(self):
        normal = Path(".github/workflows/website-publish.yml").read_text(encoding="utf-8")
        archive = Path(
            ".github/workflows/archive-only-publish-2026-10-11.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("ARCHIVE_ONLY", normal)
        self.assertIn("PUBLISH-ARCHIVE-2026-10-11", archive)
        self.assertIn('ARTIFACT_RUN_ID: "38201777075"', archive)
        self.assertIn('ARTIFACT_ID: "11725558085"', archive)
        self.assertIn("ARTIFACT_DIGEST: sha256:da8c3c", archive)
        self.assertIn("APPROVED_IMAGE_SHA256: f970c833", archive)
        self.assertIn("--mode ARCHIVE_ONLY", archive)
        self.assertIn("--ready-path", archive)
        self.assertIn("archive_only_publish_result.json", archive)
        self.assertIn('git add "$PUBLISHED_IMAGE_PATH"', archive)
        self.assertNotIn('archive[0]["storyTitleEn"]', archive)
        self.assertNotIn("x-publish.yml", archive)
        self.assertNotIn("ready_to_publish.json\n          git add", archive)


if __name__ == "__main__":
    unittest.main()
