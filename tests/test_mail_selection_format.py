import tempfile
import unittest
from pathlib import Path

from scripts import send_design_approval_email, send_email


class MailSelectionFormatTests(unittest.TestCase):
    def test_story_mail_shows_only_titles_in_japanese_then_english(self):
        _, body = send_email.build_email(
            {"issue_date": "2026-11-23", "recommended_number": 1,
             "story_options": [{"candidate_number": 1, "title_ja": "日本語の記事",
                                "title_en": "English story", "reason_en": "Details",
                                "jp_copy": "詳しい説明"}]},
            "A" * 32,
        )
        self.assertIn("1. 日本語の記事（おすすめ）\n   English story", body)
        self.assertNotIn("Details", body)
        self.assertNotIn("詳しい説明", body)
        self.assertIn("例：2（全角の２でも可）", body)

    def test_design_mail_starts_with_selected_story_and_pairs_options_by_number(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preview.png"
            path.touch()
            package = {
                "issue_date": "2026-11-23",
                "state": "DESIGN_OPTIONS_READY",
                "approved_story": {
                    "selected_story": {"title_ja": "選択記事", "title_en": "Selected story"}
                },
                "image_concepts": [{"number": n} for n in (1, 2, 3)],
                "design_previews": [{"number": n, "image_path": str(path)} for n in (1, 2, 3)],
                "title_ideas": [{"number": n, "title": f"Publication title {n}"} for n in (1, 2, 3)],
            }
            _, body = send_design_approval_email.build_email(package, "A" * 32)
            self.assertIn("選んだ記事\n選択記事\nSelected story", body)
            self.assertLess(body.index("Selected story"), body.index("IMAGE / CONCEPT 1"))
            self.assertIn("TITLE 2\n\nPublication title 2", body)
            self.assertLess(body.index("Publication title 2"), body.index("DailyDuck_Image_2.png"))
            self.assertLess(body.index("DailyDuck_Image_2.png"), body.index("IMAGE / CONCEPT 3"))
            self.assertIn("例：2（全角の２でも可）", body)
            self.assertIn("NEXT 3", body)
