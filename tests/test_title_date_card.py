import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageColor, ImageDraw

from scripts import build_x_card as card, publish_website as web


class TitleDateCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hero = Path(self.temp.name) / "hero.png"
        Image.new("RGB", (80, 80), "#ABCDEF").save(self.hero)
        self.output = Path(self.temp.name) / "cards"
        self.patch = patch.object(card, "OUT_DIR", self.output)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def ready_package(self, title="A CHILL DOWN YOUR FEATHERS"):
        # Synthetic content; the public issue date/title cover the original bug.
        return {
            "state": "READY_TO_PUBLISH",
            "issue_date": "2026-11-02",
            "canonical_image_path": str(self.hero),
            "selected_title": title,
            "selected_title_number": 1,
            "selected_title_detail": {"number": 1, "title": title},
            "design_approval_reply": "1 1",
            "gate_a_approved_story": {
                "issue_date": "2026-11-02",
                "duck_name": "Test Duck",
                "selected_title": title,
                "selected_title_number": 1,
                "title_en": "Different source headline",
                "title_ja": "テスト用の見出し",
                "en_copy": "Synthetic English story text.",
                "jp_copy": "テスト用の記事本文。",
                "duck_en": "Synthetic Duck comment.",
                "duck_jp": "テスト用のコメント。",
                "x_en": "Synthetic English teaser.",
                "x_jp": "テスト用の要約。",
                "source": "Example Source",
                "source_url": "https://example.com/story",
                "selected_story": {"title_en": "Different source headline"},
            },
        }

    def render_and_measure(self, ready, expected_lines):
        item = web.build_item(ready, ready["canonical_image_path"])
        page = web.render_page(item)
        with patch.object(card, "draw_lines", wraps=card.draw_lines) as lines:
            path, _ = card.render_card(ready)
        title_call = lines.call_args_list[0].args
        _, left, title_y, title_lines, title_font, _, line_height = title_call
        self.assertEqual(len(title_lines), expected_lines)
        self.assertEqual(" ".join(title_lines), item["storyTitleEn"])
        self.assertIn(f'<div class="story-title">{web.e(item["storyTitleEn"])}</div>', page)
        with Image.open(path) as image:
            self.assertEqual(image.size, (1500, 1200))
            # Locate actual date ink independently of the accent calculations.
            date_y = title_y + len(title_lines) * line_height + 55
            date_text = card.display_date_english(ready["issue_date"])
            mask = Image.new("L", (500, 50))
            ImageDraw.Draw(mask).text((0, 0), date_text, font=card.font(27, True), fill=255)
            ink = mask.getbbox()
            date_box = (left + ink[0], date_y + ink[1], left + ink[2], date_y + ink[3])
            expected_ink = Image.new("RGB", mask.size, card.CREAM)
            ImageDraw.Draw(expected_ink).text((0, 0), date_text, font=card.font(27, True), fill=card.NAVY)
            self.assertIsNone(ImageChops.difference(
                image.crop((left, date_y, left + 500, date_y + 50)),
                expected_ink,
            ).getbbox())
            # Scan the PNG pixels; do not trust draw coordinates alone.
            region = image.crop((0, date_box[1] - 35, 600, date_box[1]))
            yellow = ImageColor.getrgb(card.YELLOW)
            points = [(x, y) for y in range(region.height) for x in range(region.width)
                      if region.getpixel((x, y)) == yellow]
            self.assertTrue(points)
            line_box = (min(x for x, y in points),
                        min(y for x, y in points) + date_box[1] - 35,
                        max(x for x, y in points) + 1,
                        max(y for x, y in points) + date_box[1] - 35 + 1)
            self.assertEqual(line_box[0], date_box[0])
            self.assertEqual(line_box[2], date_box[2])
            self.assertEqual(date_box[1] - line_box[3], 6)
            self.assertEqual(line_box[3] - line_box[1], 11)
            self.assertLess(line_box[1], line_box[3])
            title_bottom = max(
                ImageDraw.Draw(image).textbbox(
                    (left, title_y + n * line_height), text, font=title_font
                )[3] for n, text in enumerate(title_lines)
            )
            self.assertGreater(line_box[1], title_bottom)
        return {"title": item["storyTitleEn"], "lines": title_lines,
                "date_box": date_box, "line_box": line_box,
                "width_px": line_box[2] - line_box[0], "gap_px": 6}

    def test_november_2_title_and_pixels(self):
        self.render_and_measure(self.ready_package(), 2)

    def test_one_line_title_preserves_case(self):
        self.render_and_measure(self.ready_package("Chill Duck"), 1)

    def test_width_tracks_other_dates_and_two_line_titles(self):
        for issue in ("2026-09-11", "2027-05-01"):
            with self.subTest(issue=issue):
                ready = self.ready_package("A CHILL DOWN YOUR FEATHERS")
                ready["issue_date"] = issue
                self.render_and_measure(ready, 2)

    def assert_rejected_by_both(self, ready):
        before = copy.deepcopy(ready)
        with self.assertRaises(ValueError):
            web.build_item(ready, ready["canonical_image_path"])
        with self.assertRaises(ValueError):
            card.render_card(ready)
        self.assertEqual(ready, before)
        self.assertFalse(self.output.exists())

    def test_missing_title_never_uses_source_title_or_duck_name(self):
        ready = self.ready_package()
        for key in ("selected_title", "selected_title_number", "selected_title_detail", "design_approval_reply"):
            ready.pop(key)
        for key in ("selected_title", "selected_title_number"):
            ready["gate_a_approved_story"].pop(key)
        self.assert_rejected_by_both(ready)

    def test_invalid_titles_fail_closed(self):
        for title in (None, "", " padded ", "two  spaces", "line\nbreak", "<tag>", 7):
            with self.subTest(title=title):
                ready = self.ready_package()
                ready["selected_title"] = title
                self.assert_rejected_by_both(ready)

    def test_conflicting_final_titles_fail_closed(self):
        for location in ("detail", "root", "nested"):
            with self.subTest(location=location):
                ready = self.ready_package()
                if location == "detail":
                    ready["selected_title_detail"]["title"] = "CONFLICT"
                elif location == "root":
                    ready["gate_a_approved_story"]["selected_title"] = "CONFLICT"
                else:
                    ready["gate_a_approved_story"]["selected_story"]["selected_title"] = "CONFLICT"
                self.assert_rejected_by_both(ready)

    def test_conflicting_approval_numbers_fail_closed(self):
        for location in ("detail", "root", "reply", "selected"):
            with self.subTest(location=location):
                ready = self.ready_package()
                if location == "detail":
                    ready["selected_title_detail"]["number"] = 2
                elif location == "root":
                    ready["gate_a_approved_story"]["selected_title_number"] = 2
                elif location == "reply":
                    ready["design_approval_reply"] = "1 2"
                else:
                    ready["selected_title_number"] = True
                self.assert_rejected_by_both(ready)

    def test_long_title_fails_instead_of_truncating(self):
        for title in ("A very long approved title " * 20, "W" * 100):
            with self.subTest(title=title):
                ready = self.ready_package(title.rstrip())
                with self.assertRaisesRegex(ValueError, "without truncation"):
                    card.render_card(ready)
                self.assertFalse(self.output.exists())

    def test_legacy_card_rejects_missing_selected_title(self):
        ready = self.ready_package()
        ready["issue_date"] = "2026-08-21"
        ready.pop("selected_title")
        self.assert_rejected_by_both(ready)

    def test_main_does_not_write_state_or_card_on_title_conflict(self):
        ready = self.ready_package()
        ready["selected_title_detail"]["title"] = "CONFLICT"
        state = Path(self.temp.name) / "ready.json"
        state.write_text(json.dumps(ready), encoding="utf-8")
        before = state.read_bytes()
        with patch.object(card, "READY_PATH", state):
            with self.assertRaises(ValueError):
                card.main()
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
