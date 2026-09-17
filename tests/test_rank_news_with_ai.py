import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import llm_provider
from scripts.rank_news_with_ai import (
    build_schema,
    main,
    validate_result,
)


def make_candidates(count=5):
    return [{"title": f"c{i}"} for i in range(1, count + 1)]


def make_top_five(overrides_by_index=None):
    overrides_by_index = overrides_by_index or {}
    stories = []
    for i in range(1, 6):
        story = {
            "id": i,
            "title": f"Story {i}",
            "source": "Example",
            "url": f"https://example.test/{i}",
            "category": "community",
            "happiness": 8,
            "hope": 8,
            "general_interest": 7,
            "surprise": 6,
            "duck_visual": 7,
            "source_quality": 8,
            "freshness": 7,
            "broad_appeal": 8,
            "novelty_vs_archive": 7,
            "total_score": 80,
            "reason": "Uplifting.",
        }
        story.update(overrides_by_index.get(i, {}))
        stories.append(story)
    return stories


def make_result(overrides_by_index=None):
    return {
        "recommended_id": 1,
        "recommended_reason": "Warm story.",
        "top_five": make_top_five(overrides_by_index),
    }


class ValidateResultRegressionTests(unittest.TestCase):
    """Pins the pre-existing, provider-independent contract that
    llm_provider.py relies on being unchanged (Step 5 of the approved plan)."""

    def test_valid_result_passes(self):
        validate_result(make_result(), make_candidates(), [])

    def test_wrong_story_count_rejected_four(self):
        result = make_result()
        result["top_five"] = result["top_five"][:4]
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])

    def test_wrong_story_count_rejected_six(self):
        result = make_result()
        extra = dict(result["top_five"][0])
        extra["id"] = 6
        result["top_five"] = result["top_five"] + [extra]
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(6), [])

    def test_invalid_candidate_id_rejected(self):
        result = make_result({1: {"id": 999}})
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])

    def test_duplicate_candidate_id_rejected(self):
        result = make_result({2: {"id": 1}})
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])

    def test_already_published_url_rejected(self):
        archive = [{"sourceUrl": "https://example.test/1", "published": True}]
        with self.assertRaises(RuntimeError):
            validate_result(make_result(), make_candidates(), archive)

    def test_duplicate_url_within_top_five_rejected(self):
        result = make_result({2: {"url": "https://example.test/1"}})
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])

    def test_recommended_id_not_in_selection_rejected(self):
        result = make_result()
        result["recommended_id"] = 999
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])


class ExactFiveDiagnosticsAndWordingTests(unittest.TestCase):
    """Root-cause fix for the OpenAI-fallback incident (response_length=4359,
    "Gemini must return exactly five stories."): the message is now
    provider-neutral and carries safe diagnostics (expected/actual count,
    bounded top-level keys) instead of naming Gemini regardless of which
    provider actually produced the result."""

    def test_diagnostics_expose_expected_and_actual_count_on_short_list(self):
        result = make_result()
        result["top_five"] = result["top_five"][:3]
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])

        message = str(ctx.exception)
        self.assertIn("EXPECTED_STORY_COUNT=5", message)
        self.assertIn("ACTUAL_STORY_COUNT=3", message)
        self.assertIn("TOP_LEVEL_KEYS=", message)
        self.assertIn("top_five", message)

    def test_diagnostics_report_unknown_count_when_top_five_is_not_a_list(self):
        result = make_result()
        result["top_five"] = {"unexpected": "wrapper"}
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])

        message = str(ctx.exception)
        self.assertIn("ACTUAL_STORY_COUNT=UNKNOWN", message)

    def test_diagnostics_reveal_wrong_top_level_key(self):
        # Simulates the plausible real-world cause: the model wrapped its
        # five stories under a different key instead of "top_five".
        result = {
            "recommended_id": 1,
            "recommended_reason": "Warm story.",
            "stories": make_top_five(),
        }
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])

        message = str(ctx.exception)
        self.assertIn("ACTUAL_STORY_COUNT=UNKNOWN", message)
        self.assertIn("stories", message)

    def test_diagnostics_do_not_leak_full_news_content(self):
        result = make_result()
        result["top_five"] = result["top_five"][:2]
        result["a_very_long_unexpected_field"] = "x" * 10_000
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])

        # The bounded key list must not blow up into the full field value.
        self.assertLess(len(str(ctx.exception)), 1000)

    def test_validation_messages_are_provider_neutral(self):
        result = make_result()
        result["top_five"] = result["top_five"][:4]
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])
        self.assertNotIn("Gemini", str(ctx.exception))
        self.assertIn("Ranking result", str(ctx.exception))

        result = make_result({1: {"id": 999}})
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])
        self.assertNotIn("Gemini", str(ctx.exception))

        result = make_result({2: {"id": 1}})
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])
        self.assertNotIn("Gemini", str(ctx.exception))

        archive = [{"sourceUrl": "https://example.test/1", "published": True}]
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(make_result(), make_candidates(), archive)
        self.assertNotIn("Gemini", str(ctx.exception))

        result = make_result({2: {"url": "https://example.test/1"}})
        with self.assertRaises(RuntimeError) as ctx:
            validate_result(result, make_candidates(), [])
        self.assertNotIn("Gemini", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            validate_result(["not", "a", "dict"], make_candidates(), [])
        self.assertNotIn("Gemini", str(ctx.exception))


class SadStoryGuardTests(unittest.TestCase):
    """Human Decision 1: deterministic, provider-independent guard."""

    def test_prohibited_categories_rejected(self):
        prohibited = [
            "war", "crime", "criminal", "tragedy", "tragic", "death",
            "disaster", "fear", "suffering", "outrage",
            "political conflict", "severe illness",
        ]
        for category in prohibited:
            with self.subTest(category=category):
                result = make_result({1: {"category": category}})
                with self.assertRaises(RuntimeError):
                    validate_result(result, make_candidates(), [])

    def test_category_is_case_insensitive(self):
        result = make_result({1: {"category": "WAR"}})
        with self.assertRaises(RuntimeError):
            validate_result(result, make_candidates(), [])

    def test_legitimate_category_not_falsely_rejected(self):
        # "warm community story" contains "war" as a substring but must not
        # match -- the guard uses word-boundary matching, not bare `in`.
        result = make_result({1: {"category": "warm community story"}})
        validate_result(result, make_candidates(), [])

    def test_recovery_story_prose_not_scanned(self):
        # The guard is scoped to `category` only, deliberately not `reason`,
        # because build_prompt()'s own instructions explicitly allow a
        # recovery story whose prose mentions "illness" or "tragedy" while
        # being hopeful overall. Scanning `reason` would create a false
        # positive against that carve-out.
        result = make_result(
            {
                1: {
                    "category": "community",
                    "reason": "A town rallies after tragedy to support a "
                    "neighbor recovering from severe illness.",
                }
            }
        )
        validate_result(result, make_candidates(), [])


class BuildSchemaRegressionTests(unittest.TestCase):
    def test_schema_shape_unchanged(self):
        schema = build_schema()
        self.assertEqual(schema["properties"]["top_five"]["minItems"], 5)
        self.assertEqual(schema["properties"]["top_five"]["maxItems"], 5)
        self.assertIn("recommended_id", schema["required"])
        self.assertIn("recommended_reason", schema["required"])
        self.assertIn("top_five", schema["required"])


class MainDelegatesToLlmProviderTests(unittest.TestCase):
    def test_main_calls_llm_provider_exactly_once_and_writes_output(self):
        candidates_payload = {"candidates": make_candidates(6)}
        fixed_result = make_result()

        with tempfile.TemporaryDirectory() as tmp_dir:
            original_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                Path("news_candidates.json").write_text(
                    json.dumps(candidates_payload), encoding="utf-8"
                )
                Path("data").mkdir()
                Path("data/archive.json").write_text("[]", encoding="utf-8")

                with patch.object(
                    llm_provider,
                    "generate_ranking",
                    return_value=(fixed_result, "openai"),
                ) as generate_ranking_mock:
                    main()

                self.assertEqual(generate_ranking_mock.call_count, 1)
                written = json.loads(Path("ai_ranked_news.json").read_text(encoding="utf-8"))
                self.assertEqual(written, fixed_result)
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
