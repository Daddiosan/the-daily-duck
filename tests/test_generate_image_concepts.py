import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import generate_image_concepts, llm_provider


def gemini_http_error(code, message="error"):
    return urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/v1beta/models/test:generateContent",
        code=code,
        msg=message,
        hdrs=None,
        fp=io.BytesIO(message.encode("utf-8")),
    )


def gemini_http_error_with_body(code, body, msg="error"):
    body_bytes = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url=(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "test:generateContent?key=FAKE_TEST_KEY"
        ),
        code=code,
        msg=msg,
        hdrs=None,
        fp=io.BytesIO(body_bytes),
    )


# Confirmed production evidence (Phase B): the exact 403 body Google returns
# when a project has been denied access. Same classifier as news_ranking and
# editorial_generation -- see llm_provider._is_confirmed_project_access_denied().
PROJECT_ACCESS_DENIED_BODY = {
    "error": {
        "code": 403,
        "message": "Your project has been denied access. Please contact support.",
        "status": "PERMISSION_DENIED",
    }
}

# An ordinary 403 that must NOT be reclassified -- must keep failing closed.
GENERIC_PERMISSION_DENIED_BODY = {
    "error": {
        "code": 403,
        "message": "Permission denied on resource project default-gemini-project.",
        "status": "PERMISSION_DENIED",
    }
}


APPROVED_STORY = {
    "id": "1",
    "title_en": "Local Bakery Wins Regional Award",
    "reason_en": "A small community bakery earned a surprise regional prize.",
}

APPROVED_STATE = {
    "state": "APPROVED_STORY",
    "issue_date": "2026-09-18",
    "approved_story": APPROVED_STORY,
}


def valid_concept(number):
    return {
        "number": number,
        "title_en": f"Concept {number} title",
        "concept_en": f"Concept {number} visual concept",
        "composition_en": f"Concept {number} composition",
        "generation_prompt_en": f"Concept {number} production-ready prompt",
        "alt_en": f"Concept {number} alt text",
        "title_ja": f"コンセプト{number}タイトル",
        "concept_ja": f"コンセプト{number}説明",
        "composition_ja": f"コンセプト{number}構図",
        "alt_ja": f"コンセプト{number}alt",
    }


def valid_title(number):
    return {
        "number": number,
        "title": f"TITLE IDEA {number}",
        "meaning_ja": f"タイトル{number}の意味",
    }


def valid_design_options_payload():
    return {
        "image_concepts": [valid_concept(i) for i in range(1, 4)],
        "title_ideas": [valid_title(i) for i in range(1, 4)],
    }


def api_keys_present():
    return patch.dict(
        "os.environ",
        {"GEMINI_API_KEY": "test-gemini-key", "OPENAI_API_KEY": "test-openai-key"},
        clear=False,
    )


class GenerateOptionsTests(unittest.TestCase):
    """Exercises scripts.generate_image_concepts.generate_options(), which
    now delegates all retry/fallback to
    scripts.llm_provider.generate_design_options() instead of the old
    nested EDITORIAL_MAX_ATTEMPTS(3) x GEMINI_API_MAX_ATTEMPTS(5) loops and
    its own call_gemini_with_retry()/is_retryable_gemini_error()."""

    def setUp(self):
        patcher = api_keys_present()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_1_gemini_succeeds_first_attempt(self):
        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(valid_design_options_payload())
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)

    def test_2_exactly_3_concepts_and_3_titles_numbered_sequentially(self):
        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(valid_design_options_payload())
        ), patch.object(llm_provider, "_call_openai_once"):
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(len(concepts), generate_image_concepts.IMAGE_CONCEPT_COUNT)
        self.assertEqual(len(titles), generate_image_concepts.TITLE_IDEA_COUNT)
        self.assertEqual([c["number"] for c in concepts], [1, 2, 3])
        self.assertEqual([t["number"] for t in titles], [1, 2, 3])

        for concept in concepts:
            for field in generate_image_concepts.REQUIRED_CONCEPT_FIELDS:
                self.assertIsInstance(concept[field], str)
                self.assertTrue(concept[field].strip())

        for title in titles:
            for field in generate_image_concepts.REQUIRED_TITLE_FIELDS:
                self.assertIsInstance(title[field], str)
                self.assertTrue(title[field].strip())

    def test_3_gemini_503_once_then_succeeds(self):
        with patch.object(
            llm_provider,
            "_call_gemini_once",
            side_effect=[gemini_http_error(503), json.dumps(valid_design_options_payload())],
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(gemini_mock.call_count, 2)
        self.assertEqual(openai_mock.call_count, 0)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)

    def test_4_gemini_repeated_503_falls_back_to_openai(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_design_options_payload())
        ) as openai_mock:
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)

    def test_5_gemini_429_immediate_fallback_no_quota_burning_retry(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(429)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_design_options_payload())
        ) as openai_mock:
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)

    def test_6_confirmed_project_access_denied_immediate_fallback(self):
        error = gemini_http_error_with_body(403, PROJECT_ACCESS_DENIED_BODY)

        with patch.object(
            llm_provider.urllib.request, "urlopen", side_effect=error
        ) as urlopen_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_design_options_payload())
        ) as openai_mock:
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        # Zero additional Gemini retries for a confirmed project-denied
        # condition -- exactly the one physical call that revealed it. This
        # is the exact production incident this task was raised to fix.
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)

    def test_7_generic_403_fails_closed_no_fallback(self):
        error = gemini_http_error_with_body(403, GENERIC_PERMISSION_DENIED_BODY)

        with patch.object(
            llm_provider.urllib.request, "urlopen", side_effect=error
        ) as urlopen_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(ctx.exception.category, llm_provider.PERMISSION_FAILURE)
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_8_401_fails_closed(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(401)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(ctx.exception.category, llm_provider.AUTH_FAILURE)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_9_400_fails_closed(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(400)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(ctx.exception.category, llm_provider.INVALID_REQUEST)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_10_openai_fallback_produces_same_contract_shape(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_design_options_payload())
        ):
            concepts, titles = generate_image_concepts.generate_options(
                APPROVED_STATE, APPROVED_STORY
            )

        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)
        for index, concept in enumerate(concepts, start=1):
            self.assertEqual(concept["number"], index)
            for field in generate_image_concepts.REQUIRED_CONCEPT_FIELDS:
                self.assertIsInstance(concept[field], str)
                self.assertTrue(concept[field].strip())
        for index, title in enumerate(titles, start=1):
            self.assertEqual(title["number"], index)
            for field in generate_image_concepts.REQUIRED_TITLE_FIELDS:
                self.assertIsInstance(title[field], str)
                self.assertTrue(title[field].strip())

    def test_11_openai_malformed_json_fails_safely(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value="not valid json"
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)

    def test_12_openai_wrong_concept_count_rejected(self):
        bad_payload = valid_design_options_payload()
        bad_payload["image_concepts"] = bad_payload["image_concepts"][:2]

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

    def test_13_openai_wrong_title_count_rejected(self):
        bad_payload = valid_design_options_payload()
        bad_payload["title_ideas"] = bad_payload["title_ideas"][:1]

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

    def test_14_openai_missing_required_field_rejected(self):
        bad_payload = valid_design_options_payload()
        del bad_payload["image_concepts"][0]["generation_prompt_en"]

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

    def test_15_blank_required_field_rejected(self):
        # A field present with an empty string satisfies the JSON schema's
        # "type": "string" alone -- validate_design_options_result() must
        # still reject it (first_text()-based non-empty check).
        bad_payload = valid_design_options_payload()
        bad_payload["title_ideas"][0]["meaning_ja"] = "   "

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

    def test_16_provider_hard_call_ceiling_not_exceeded(self):
        # Semantic (invalid-output) failure on both providers -- proves the
        # hard ceiling holds even outside the transport-error path, and
        # replaces the old nested EDITORIAL_MAX_ATTEMPTS(3) x
        # GEMINI_API_MAX_ATTEMPTS(5) = 15 amplification this task's incident
        # was traced to.
        bad_payload = valid_design_options_payload()
        del bad_payload["image_concepts"][0]["generation_prompt_en"]

        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(bad_payload)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        # No same-provider ping-pong retry for malformed output on the
        # fallback provider -- one attempt is enough (matches ranking's/
        # editorial's existing policy).
        self.assertEqual(openai_mock.call_count, 1)

        total_calls = gemini_mock.call_count + openai_mock.call_count
        self.assertLessEqual(total_calls, llm_provider.HARD_MAX_PROVIDER_CALLS)
        self.assertLess(total_calls, 15)

    def test_17_missing_openai_key_gemini_success(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider,
                "_call_gemini_once",
                return_value=json.dumps(valid_design_options_payload()),
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                concepts, titles = generate_image_concepts.generate_options(
                    APPROVED_STATE, APPROVED_STORY
                )

        self.assertEqual(len(concepts), 3)
        self.assertEqual(len(titles), 3)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_18_missing_openai_key_fallback_required_fails_safely(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                    generate_image_concepts.generate_options(APPROVED_STATE, APPROVED_STORY)

        self.assertEqual(ctx.exception.category, llm_provider.CONFIG_MISSING)
        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 0)


class MainSideEffectOrderingTests(unittest.TestCase):
    """Proves design-options LLM generation (incl. fallback) completes --
    successfully or not -- before any design_options.json write or concept
    image generation, and that image generation never runs when text
    generation fails closed on both providers."""

    def setUp(self):
        patcher = api_keys_present()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_19_both_providers_fail_no_package_or_image_side_effects(self):
        with patch.object(
            generate_image_concepts, "load_json", return_value=APPROVED_STATE
        ), patch.object(
            generate_image_concepts,
            "generate_options",
            side_effect=llm_provider.ProviderFailure(
                "Both Gemini and OpenAI failed to produce a valid "
                "design_options_generation result.",
                category=llm_provider.UNKNOWN_PROVIDER_ERROR,
            ),
        ), patch.object(
            generate_image_concepts, "generate_concept_images"
        ) as generate_images_mock, patch.object(
            Path, "write_text"
        ) as write_text_mock, patch.object(
            Path, "write_bytes"
        ) as write_bytes_mock, patch.object(
            Path, "mkdir"
        ) as mkdir_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                generate_image_concepts.main()

        generate_images_mock.assert_not_called()
        write_text_mock.assert_not_called()
        write_bytes_mock.assert_not_called()
        mkdir_mock.assert_not_called()

    def test_20_success_path_generates_images_and_writes_package_once(self):
        concepts, titles = generate_image_concepts.normalize_design_options(
            valid_design_options_payload()
        )

        fake_previews = [
            {
                "number": i,
                "concept_number": i,
                "image_path": f"automation_images/design_previews/2026-09-18/batch_01/preview_{i}.png",
                "sha256": f"hash{i}",
            }
            for i in range(1, 4)
        ]

        with patch.object(
            generate_image_concepts, "load_json", return_value=APPROVED_STATE
        ), patch.object(
            generate_image_concepts, "generate_options", return_value=(concepts, titles)
        ), patch.object(
            generate_image_concepts,
            "generate_concept_images",
            return_value=(fake_previews, 1, Path("automation_images/design_previews/2026-09-18/batch_01")),
        ) as generate_images_mock, patch.object(
            Path, "write_text"
        ) as write_text_mock, patch.object(
            Path, "mkdir"
        ):
            exit_code = generate_image_concepts.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(generate_images_mock.call_count, 1)
        # Exactly one design_options.json write, no duplicate.
        self.assertEqual(write_text_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
