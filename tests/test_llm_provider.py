import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import httpx2
import openai

from scripts import llm_provider


def gemini_http_error(code, message="error"):
    return urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/v1beta/models/test:generateContent",
        code=code,
        msg=message,
        hdrs=None,
        fp=io.BytesIO(message.encode("utf-8")),
    )


def openai_status_error(error_cls, code, message="error"):
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(code, request=request, json={"error": {"message": message}})
    return error_cls(message, response=response, body={"error": {"message": message}})


def openai_connection_error(message="connection error"):
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIConnectionError(message=message, request=request)


def openai_timeout_error():
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request=request)


def valid_ranking_result():
    return {
        "recommended_id": 1,
        "recommended_reason": "Warm, uplifting community story.",
        "top_five": [
            {
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
            for i in range(1, 6)
        ],
    }


def noop_validate(result):
    return None


class GenerateRankingTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "test-gemini-key", "OPENAI_API_KEY": "test-openai-key"},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_1_gemini_succeeds_first_attempt(self):
        with patch.object(
            llm_provider, "_call_gemini_once", return_value=json.dumps(valid_ranking_result())
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            result, provider = llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(provider, llm_provider.GEMINI)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_2_gemini_503_once_then_succeeds(self):
        with patch.object(
            llm_provider,
            "_call_gemini_once",
            side_effect=[gemini_http_error(503), json.dumps(valid_ranking_result())],
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            result, provider = llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(provider, llm_provider.GEMINI)
        self.assertEqual(gemini_mock.call_count, 2)
        self.assertEqual(openai_mock.call_count, 0)

    def test_3_gemini_repeated_temporary_unavailable_falls_back(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_ranking_result())
        ) as openai_mock:
            result, provider = llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(provider, llm_provider.OPENAI)
        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)

    def test_4_gemini_429_no_quota_burning_retry_falls_back(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(429)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_ranking_result())
        ) as openai_mock:
            result, provider = llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(provider, llm_provider.OPENAI)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 1)

    def test_5_gemini_auth_failure_fails_closed_no_fallback(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(401)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(ctx.exception.category, llm_provider.AUTH_FAILURE)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_6_gemini_invalid_request_fails_closed(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(400)
        ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(ctx.exception.category, llm_provider.INVALID_REQUEST)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_7_openai_fallback_success_same_normalized_contract(self):
        expected = valid_ranking_result()
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(llm_provider, "_call_openai_once", return_value=json.dumps(expected)):
            result, provider = llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(provider, llm_provider.OPENAI)
        self.assertEqual(result, expected)

    def test_8_openai_malformed_response_rejected_no_downstream_progression(self):
        def failing_validate(result):
            raise ValueError("must return exactly five stories")

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps({"top_five": []})
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                llm_provider.generate_ranking("prompt", {}, validate=failing_validate)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 1)

    def test_9_both_providers_fail_safely(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", side_effect=openai_connection_error()
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, llm_provider.OPENAI_MAX_ATTEMPTS)

    def test_10_sad_story_candidate_rejected_by_deterministic_guard(self):
        from scripts.rank_news_with_ai import validate_result

        candidates = [{"title": f"c{i}"} for i in range(1, 6)]
        archive = []

        sad_result = valid_ranking_result()
        sad_result["top_five"][0]["category"] = "war"

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(sad_result)
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                llm_provider.generate_ranking(
                    "prompt",
                    {},
                    validate=lambda result: validate_result(result, candidates, archive),
                )

        self.assertEqual(openai_mock.call_count, 1)

    def test_11_duplicate_already_published_candidate_rejected(self):
        from scripts.rank_news_with_ai import validate_result

        candidates = [{"title": f"c{i}"} for i in range(1, 6)]
        archive = [{"sourceUrl": "https://example.test/1", "published": True}]

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ), patch.object(
            llm_provider, "_call_openai_once", return_value=json.dumps(valid_ranking_result())
        ):
            with self.assertRaises(llm_provider.ProviderFailure):
                llm_provider.generate_ranking(
                    "prompt",
                    {},
                    validate=lambda result: validate_result(result, candidates, archive),
                )

    def test_12_hard_request_bound_is_enforced(self):
        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "_call_openai_once", side_effect=openai_connection_error()
        ) as openai_mock:
            with self.assertRaises(llm_provider.ProviderFailure):
                llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        total_calls = gemini_mock.call_count + openai_mock.call_count
        self.assertLessEqual(total_calls, llm_provider.HARD_MAX_PROVIDER_CALLS)
        self.assertEqual(total_calls, llm_provider.HARD_MAX_PROVIDER_CALLS)

    def test_13_openai_api_key_absent_fails_closed_when_fallback_required(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                    llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(ctx.exception.category, llm_provider.CONFIG_MISSING)
        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(openai_mock.call_count, 0)

    def test_14_gemini_success_does_not_require_openai_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with patch.object(
                llm_provider, "_call_gemini_once", return_value=json.dumps(valid_ranking_result())
            ) as gemini_mock, patch.object(llm_provider, "_call_openai_once") as openai_mock:
                result, provider = llm_provider.generate_ranking(
                    "prompt", {}, validate=noop_validate
                )

        self.assertEqual(provider, llm_provider.GEMINI)
        self.assertEqual(gemini_mock.call_count, 1)
        self.assertEqual(openai_mock.call_count, 0)

    def test_gemini_api_key_absent_fails_closed_before_any_call(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
            with patch.object(llm_provider, "_call_gemini_once") as gemini_mock, patch.object(
                llm_provider, "_call_openai_once"
            ) as openai_mock:
                with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                    llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(ctx.exception.category, llm_provider.CONFIG_MISSING)
        self.assertEqual(gemini_mock.call_count, 0)
        self.assertEqual(openai_mock.call_count, 0)


class ClassifyGeminiErrorTests(unittest.TestCase):
    def test_429_is_rate_limit_quota(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(gemini_http_error(429)), llm_provider.RATE_LIMIT_QUOTA
        )

    def test_503_is_temporary_unavailable(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(gemini_http_error(503)),
            llm_provider.TEMPORARY_UNAVAILABLE,
        )

    def test_401_is_auth_failure(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(gemini_http_error(401)), llm_provider.AUTH_FAILURE
        )

    def test_403_is_permission_failure(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(gemini_http_error(403)),
            llm_provider.PERMISSION_FAILURE,
        )

    def test_400_is_invalid_request(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(gemini_http_error(400)), llm_provider.INVALID_REQUEST
        )

    def test_url_error_is_network_timeout(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(urllib.error.URLError("timed out")),
            llm_provider.NETWORK_TIMEOUT,
        )

    def test_socket_timeout_is_network_timeout(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(socket.timeout()), llm_provider.NETWORK_TIMEOUT
        )

    def test_runtime_error_is_invalid_response(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(RuntimeError("bad shape")),
            llm_provider.INVALID_RESPONSE,
        )

    def test_unclassified_exception_is_unknown(self):
        self.assertEqual(
            llm_provider.classify_gemini_error(ValueError("???")),
            llm_provider.UNKNOWN_PROVIDER_ERROR,
        )


class ClassifyOpenAIErrorTests(unittest.TestCase):
    def test_rate_limit(self):
        exc = openai_status_error(openai.RateLimitError, 429)
        self.assertEqual(llm_provider.classify_openai_error(exc), llm_provider.RATE_LIMIT_QUOTA)

    def test_auth(self):
        exc = openai_status_error(openai.AuthenticationError, 401)
        self.assertEqual(llm_provider.classify_openai_error(exc), llm_provider.AUTH_FAILURE)

    def test_permission(self):
        exc = openai_status_error(openai.PermissionDeniedError, 403)
        self.assertEqual(llm_provider.classify_openai_error(exc), llm_provider.PERMISSION_FAILURE)

    def test_bad_request(self):
        exc = openai_status_error(openai.BadRequestError, 400)
        self.assertEqual(llm_provider.classify_openai_error(exc), llm_provider.INVALID_REQUEST)

    def test_internal_server_error(self):
        exc = openai_status_error(openai.InternalServerError, 500)
        self.assertEqual(llm_provider.classify_openai_error(exc), llm_provider.TEMPORARY_UNAVAILABLE)

    def test_connection_error(self):
        self.assertEqual(
            llm_provider.classify_openai_error(openai_connection_error()), llm_provider.NETWORK_TIMEOUT
        )

    def test_timeout_error(self):
        self.assertEqual(
            llm_provider.classify_openai_error(openai_timeout_error()), llm_provider.NETWORK_TIMEOUT
        )

    def test_value_error_is_invalid_response(self):
        # Raised by _call_openai_once() itself on finish_reason=length
        # truncation -- a response-quality problem, not a transport error.
        self.assertEqual(
            llm_provider.classify_openai_error(ValueError("truncated")),
            llm_provider.INVALID_RESPONSE,
        )

    def test_unclassified_exception_is_unknown(self):
        class SomeOtherLibraryError(Exception):
            pass

        self.assertEqual(
            llm_provider.classify_openai_error(SomeOtherLibraryError("???")),
            llm_provider.UNKNOWN_PROVIDER_ERROR,
        )


class ParseJsonTests(unittest.TestCase):
    def test_strips_markdown_fences(self):
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(llm_provider._parse_json(raw), {"a": 1})

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            llm_provider._parse_json("")

    def test_none_raises(self):
        with self.assertRaises(ValueError):
            llm_provider._parse_json(None)


class FakeChoice:
    def __init__(self, finish_reason, content):
        self.finish_reason = finish_reason
        self.message = type("Message", (), {"content": content})()


class FakeOpenAIResponse:
    def __init__(self, finish_reason, content):
        self.choices = [FakeChoice(finish_reason, content)]


class FakeOpenAIClient:
    def __init__(self, response):
        self.create_mock = MagicMock(return_value=response)
        self.chat = type(
            "Chat", (), {"completions": type("Completions", (), {"create": self.create_mock})()}
        )()


class CallOpenAIOnceTests(unittest.TestCase):
    """Regression tests for the actual root cause fix: no response_format,
    no explicit token budget, and no truncation check previously existed,
    so a reasoning-heavy response that hit the API's own default output
    cap could come back with empty/partial content and be misclassified
    only as a bare INVALID_RESPONSE with no diagnostic detail."""

    def test_normal_completion_returns_content(self):
        fake_client = FakeOpenAIClient(FakeOpenAIResponse("stop", '{"a": 1}'))
        with patch.object(llm_provider, "OpenAI", return_value=fake_client):
            result = llm_provider._call_openai_once("prompt", api_key="k", model="gpt-5.6-luna")
        self.assertEqual(result, '{"a": 1}')

    def test_truncated_completion_raises_value_error(self):
        fake_client = FakeOpenAIClient(FakeOpenAIResponse("length", '{"top_five": [') )
        with patch.object(llm_provider, "OpenAI", return_value=fake_client):
            with self.assertRaises(ValueError):
                llm_provider._call_openai_once("prompt", api_key="k", model="gpt-5.6-luna")

    def test_requests_json_object_mode_and_explicit_token_budget(self):
        fake_client = FakeOpenAIClient(FakeOpenAIResponse("stop", "{}"))
        with patch.object(llm_provider, "OpenAI", return_value=fake_client):
            llm_provider._call_openai_once("prompt", api_key="k", model="gpt-5.6-luna")

        _, kwargs = fake_client.create_mock.call_args
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["max_completion_tokens"], llm_provider.DEFAULT_OPENAI_MAX_COMPLETION_TOKENS)


class ReproducedIncidentTests(unittest.TestCase):
    """End-to-end reproduction of the observed controlled-run failure:
    Gemini TEMPORARY_UNAVAILABLE x2 -> fallback -> OpenAI response
    truncated (finish_reason=length) -> INVALID_RESPONSE -> both
    providers exhausted -> fail closed, with diagnosable detail now
    present instead of only the bare category label."""

    def setUp(self):
        patcher = patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "test-gemini-key", "OPENAI_API_KEY": "test-openai-key"},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_truncated_openai_response_fails_closed_with_diagnosable_detail(self):
        fake_client = FakeOpenAIClient(FakeOpenAIResponse("length", '{"top_five": ['))

        with patch.object(
            llm_provider, "_call_gemini_once", side_effect=gemini_http_error(503)
        ) as gemini_mock, patch.object(
            llm_provider, "OpenAI", return_value=fake_client
        ):
            with self.assertRaises(llm_provider.ProviderFailure) as ctx:
                llm_provider.generate_ranking("prompt", {}, validate=noop_validate)

        self.assertEqual(gemini_mock.call_count, llm_provider.GEMINI_MAX_ATTEMPTS)
        self.assertEqual(fake_client.create_mock.call_count, 1)
        self.assertIn("openai", ctx.exception.provider_errors)
        # The fix: the failure detail is no longer just the bare category --
        # it now names the exception type and carries response-length info,
        # safely, with no secret or full-content exposure.
        self.assertIn("ValueError", ctx.exception.provider_errors["openai"])


if __name__ == "__main__":
    unittest.main()
