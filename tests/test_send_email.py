import contextlib
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import llm_provider, send_email


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
# when a project has been denied access.
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


SAMPLE_TOP_FIVE = [
    {
        "id": i,
        "title": f"Story {i}",
        "source": "Example",
        "url": f"https://example.test/{i}",
        "reason": "Uplifting.",
        "total_score": 80,
    }
    for i in range(1, 6)
]

RANKED_FIXTURE = {
    "top_five": SAMPLE_TOP_FIVE,
    "recommended_id": "1",
    "recommended_reason": "Warm, uplifting community story.",
}


def valid_editorial_payload(ids=range(1, 6)):
    return {
        "stories": [
            {
                "id": i,
                "title_en": f"Title {i}",
                "reason_en": f"Reason {i}",
                "en_copy": f"Copy {i}",
                "duck_name": f"Duck {i}",
                "duck_en": f"Duck line {i}",
                "x_en": f"X {i}",
                "title_ja": f"タイトル{i}",
                "reason_ja": f"理由{i}",
                "jp_copy": f"本文{i}",
                "duck_jp": f"ダック{i}",
                "x_jp": f"X文{i}",
            }
            for i in ids
        ]
    }


def api_keys_present():
    return patch.dict(
        "os.environ",
        {"GEMINI_API_KEY": "test-gemini-key", "OPENAI_API_KEY": "test-openai-key"},
        clear=False,
    )


class GenerateFiveEditorialPackagesTests(unittest.TestCase):
    """Exercises scripts.send_email.generate_five_editorial_packages(),
    which now delegates all retry/fallback to
    scripts.llm_provider.generate_editorial() instead of the old nested
    EDITORIAL_MAX_ATTEMPTS(3) x GEMINI_API_MAX_ATTEMPTS(5) loops."""

    def setUp(self):
        patcher = api_keys_present()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_1_gemini_succeeds_first_attempt(self):
        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(valid_editorial_payload())
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["candidate_number"], 1)
        self.assertEqual(result[0]["title_en"], "Title 1")
        # Original ranking fields (id/title/source/url/reason/total_score)
        # must survive the combine step untouched.
        self.assertEqual(result[0]["url"], "https://example.test/1")

    def test_2_gemini_503_once_then_succeeds(self):
        with patch.object(
            llm_provider,
            "_call_gemini_once",
            side_effect=[gemini_http_error(503), json.dumps(valid_editorial_payload())],
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, 2)
        self.assertEqual(openai_mock.call_count, 0)
        self.assertEqual(len(result), 5)

    def test_3_gemini_repeated_503_falls_back_to_openai(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_editorial_payload())
        ) as openai_mock:
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(result), 5)

    def test_4_gemini_429_immediate_fallback_no_quota_burning_retry(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(429)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_editorial_payload())
        ) as openai_mock:
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(result), 5)

    def test_5_confirmed_project_access_denied_immediate_fallback(self):
        error = gemini_http_error_with_body(403, PROJECT_ACCESS_DENIED_BODY)

        with patch.object(
            llm_provider.urllib.request, "urlopen", side_effect=error
        ) as urlopen_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_editorial_payload())
        ) as openai_mock:
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        # Zero additional Gemini retries for a confirmed project-denied
        # condition -- exactly the one physical call that revealed it.
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 1)
        self.assertEqual(len(result), 5)

    def test_6_generic_403_fails_closed_no_fallback(self):
        error = gemini_http_error_with_body(403, GENERIC_PERMISSION_DENIED_BODY)

        with patch.object(
            llm_provider.urllib.request, "urlopen", side_effect=error
        ) as urlopen_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(ctx.exception.category, llm_provider.PERMISSION_FAILURE)
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_7_401_fails_closed(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(401)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(ctx.exception.category, llm_provider.AUTH_FAILURE)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_8_400_fails_closed(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(400)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(ctx.exception.category, llm_provider.INVALID_REQUEST)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_9_openai_fallback_produces_same_contract_shape(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_editorial_payload())
        ):
            result = send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(len(result), 5)
        for index, story in enumerate(result, start=1):
            self.assertEqual(story["candidate_number"], index)
            for field in send_email.REQUIRED_EDITORIAL_FIELDS:
                self.assertIsInstance(story[field], str)
                self.assertTrue(story[field].strip())

    def test_10_openai_malformed_json_fails_safely(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value="not valid json"
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)

    def test_11_openai_schema_invalid_output_rejected(self):
        bad_payload = valid_editorial_payload()
        del bad_payload["stories"][0]["en_copy"]

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

    def test_19_missing_openai_key_gemini_success(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider,
                "_call_gemini_once",
                return_value=json.dumps(valid_editorial_payload()),
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                result = send_email.generate_five_editorial_packages(
                    RANKED_FIXTURE, SAMPLE_TOP_FIVE
                )

        self.assertEqual(len(result), 5)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_20_missing_openai_key_fallback_required_fails_safely(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                    send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(ctx.exception.category, llm_provider.CONFIG_MISSING)
        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 0)


class HardRequestCeilingTests(unittest.TestCase):
    """Proves the new architecture cannot repeat the observed 15-request
    Gemini amplification: semantic (invalid-output) failures share the same
    bounded per-provider attempt budget as transport failures."""

    def setUp(self):
        patcher = api_keys_present()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_13_14_15_semantic_invalid_output_respects_hard_ceiling(self):
        bad_payload = valid_editorial_payload()
        del bad_payload["stories"][0]["en_copy"]

        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(bad_payload)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(bad_payload)
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        # No same-provider ping-pong retry for malformed output on the
        # fallback provider -- one attempt is enough (matches ranking's
        # existing policy, see llm_provider._generate_with_fallback()).
        self.assertEqual(openai_mock.call_count, 1)

        total_calls = gemini_mock.call_count + openai_mock.call_count
        self.assertLessEqual(total_calls, llm_provider.HARD_MAX_PROVIDER_CALLS)
        # Explicit proof against the old 3 (EDITORIAL_MAX_ATTEMPTS) x 5
        # (GEMINI_API_MAX_ATTEMPTS) = 15 nested-retry amplification.
        self.assertLess(total_calls, 15)

    def test_editorial_uses_larger_completion_token_budget_than_ranking(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_editorial_payload())
        ) as openai_mock:
            send_email.generate_five_editorial_packages(RANKED_FIXTURE, SAMPLE_TOP_FIVE)

        _, kwargs = openai_mock.call_args
        self.assertEqual(
            kwargs["max_completion_tokens"],
            llm_provider.DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS,
        )
        self.assertGreater(
            llm_provider.DEFAULT_OPENAI_EDITORIAL_MAX_COMPLETION_TOKENS,
            llm_provider.DEFAULT_OPENAI_MAX_COMPLETION_TOKENS,
        )


class ValidateEditorialResultTests(unittest.TestCase):
    """Direct coverage of the extracted, provider-independent validator --
    proves existing editorial constraints (item 18) are unweakened."""

    def test_18a_rejects_missing_field(self):
        payload = valid_editorial_payload()
        del payload["stories"][2]["x_jp"]
        with self.assertRaises(ValueError):
            send_email.validate_editorial_result(payload, [str(i) for i in range(1, 6)])

    def test_18b_rejects_id_reorder(self):
        payload = valid_editorial_payload()
        payload["stories"][0], payload["stories"][1] = payload["stories"][1], payload["stories"][0]
        with self.assertRaises(ValueError):
            send_email.validate_editorial_result(payload, [str(i) for i in range(1, 6)])

    def test_18c_rejects_wrong_story_count(self):
        payload = {"stories": valid_editorial_payload()["stories"][:4]}
        with self.assertRaises(ValueError):
            send_email.validate_editorial_result(payload, [str(i) for i in range(1, 6)])

    def test_18d_rejects_non_object_response(self):
        with self.assertRaises(ValueError):
            send_email.validate_editorial_result(["not", "a", "dict"], ["1"])

    def test_18e_accepts_valid_payload(self):
        payload = valid_editorial_payload()
        # Must not raise.
        send_email.validate_editorial_result(payload, [str(i) for i in range(1, 6)])


class MainSideEffectOrderingTests(unittest.TestCase):
    """Proves LLM generation (incl. fallback) completes -- successfully or
    not -- before any package write, email-text write, or SMTP send, and
    that each happens at most once per run."""

    def setUp(self):
        patcher = api_keys_present()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_12_both_providers_fail_no_side_effects(self):
        with patch.object(
            send_email, "load_json", return_value=RANKED_FIXTURE
        ), patch.object(
            send_email, "load_top_five", return_value=(SAMPLE_TOP_FIVE, "1")
        ), patch.object(
            send_email,
            "generate_five_editorial_packages",
            side_effect=llm_provider.ProviderFailure(
                "Both Gemini and OpenAI failed to produce a valid editorial_generation result.",
                category=llm_provider.UNKNOWN_PROVIDER_ERROR,
            ),
        ), patch.object(
            Path, "write_text"
        ) as write_text_mock, patch.object(
            send_email, "send_email"
        ) as send_email_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                send_email.main()

        write_text_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_16_17_success_path_writes_and_sends_exactly_once(self):
        story_options = send_email.combine_editorial_output(
            SAMPLE_TOP_FIVE, valid_editorial_payload()["stories"]
        )

        # main() prints the email subject, which contains an em dash;
        # redirected to an in-memory buffer so the assertion doesn't depend
        # on the local terminal's stdout codec (irrelevant to the retry/
        # fallback/side-effect behavior under test).
        with contextlib.redirect_stdout(io.StringIO()), patch.object(
            send_email, "load_json", return_value=RANKED_FIXTURE
        ), patch.object(
            send_email, "load_top_five", return_value=(SAMPLE_TOP_FIVE, "1")
        ), patch.object(
            send_email, "generate_five_editorial_packages", return_value=story_options
        ), patch.object(
            Path, "write_text"
        ) as write_text_mock, patch.object(
            send_email, "send_email", return_value=1
        ) as send_email_mock:
            exit_code = send_email.main()

        self.assertEqual(exit_code, 0)
        # Exactly PACKAGE_PATH + EMAIL_TEXT_PATH, no duplicate Gate A package.
        self.assertEqual(write_text_mock.call_count, 2)
        # No duplicate SMTP send.
        self.assertEqual(send_email_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
