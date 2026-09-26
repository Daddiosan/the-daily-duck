from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from cloud.approval_relay.github_app_dispatch import (
    GITHUB_API_ROOT,
    GITHUB_API_VERSION,
    GITHUB_OWNER,
    GITHUB_REPOSITORY,
    GitHubAppConfig,
    GitHubAppConfigurationError,
    GitHubAppInstallationTokenProvider,
    GitHubHttpResponse,
    GitHubRequestNotSent,
    GitHubRequestOutcomeUnknown,
    InstallationTokenFinalError,
    InstallationTokenRetryableError,
    RealGitHubDispatcher,
)
from cloud.approval_relay.github_dispatch import (
    DISPATCH_REF,
    GATE_A_WORKFLOW,
    DispatchOutcome,
    WorkflowNotAllowlistedError,
)


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def config() -> GitHubAppConfig:
    return GitHubAppConfig("client-123", 456, private_key_pem())


def token_response(value: str = "installation-token", hours: int = 1):
    return GitHubHttpResponse(
        201,
        {},
        {
            "token": value,
            "expires_at": (NOW + timedelta(hours=hours)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    )


class ScriptedTransport:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.lock = threading.Lock()

    def post_json(self, *, url, headers, payload):
        with self.lock:
            self.calls.append((url, dict(headers), dict(payload)))
            if not self.script:
                raise AssertionError("unexpected HTTP call")
            result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class StaticTokenProvider:
    def __init__(self, value="installation-token", error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def get_token(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


class GitHubAppConfigurationTests(unittest.TestCase):
    def test_live_configuration_requires_all_values_and_valid_key(self):
        key = private_key_pem()
        parsed = GitHubAppConfig.from_env(
            {
                "RELAY_GITHUB_APP_CLIENT_ID": "client-123",
                "RELAY_GITHUB_APP_INSTALLATION_ID": "456",
                "RELAY_GITHUB_APP_PRIVATE_KEY": key,
            }
        )
        self.assertEqual(parsed.client_id, "client-123")
        self.assertEqual(parsed.installation_id, 456)
        self.assertNotIn(key, repr(parsed))
        for env in (
            {},
            {
                "RELAY_GITHUB_APP_CLIENT_ID": "client-123",
                "RELAY_GITHUB_APP_INSTALLATION_ID": "not-decimal",
                "RELAY_GITHUB_APP_PRIVATE_KEY": key,
            },
            {
                "RELAY_GITHUB_APP_CLIENT_ID": "client-123",
                "RELAY_GITHUB_APP_INSTALLATION_ID": "456",
                "RELAY_GITHUB_APP_PRIVATE_KEY": "SECRET-INVALID-KEY",
            },
        ):
            with self.subTest(env=set(env)), self.assertRaises(
                GitHubAppConfigurationError
            ) as raised:
                GitHubAppConfig.from_env(env)
            self.assertNotIn("SECRET-INVALID-KEY", str(raised.exception))

    def test_live_configuration_rejects_non_rsa_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        with self.assertRaises(GitHubAppConfigurationError) as raised:
            GitHubAppConfig.from_env(
                {
                    "RELAY_GITHUB_APP_CLIENT_ID": "client-123",
                    "RELAY_GITHUB_APP_INSTALLATION_ID": "456",
                    "RELAY_GITHUB_APP_PRIVATE_KEY": key,
                }
            )
        self.assertNotIn(key, str(raised.exception))


class InstallationTokenProviderTests(unittest.TestCase):
    def test_real_jwt_is_rs256_with_bounded_claims(self):
        import jwt

        provider = GitHubAppInstallationTokenProvider(
            config=config(), transport=ScriptedTransport(), clock=lambda: NOW
        )
        encoded = provider._app_jwt(NOW)
        header = jwt.get_unverified_header(encoded)
        claims = jwt.decode(encoded, options={"verify_signature": False})
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(claims["iss"], "client-123")
        self.assertEqual(claims["iat"], int((NOW - timedelta(seconds=60)).timestamp()))
        self.assertEqual(claims["exp"], int((NOW + timedelta(minutes=9)).timestamp()))
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)

    def test_token_request_is_narrowed_and_cached(self):
        transport = ScriptedTransport(token_response())
        captured = {}

        def encode(claims, key, algorithm):
            captured.update(claims)
            captured["algorithm"] = algorithm
            return "app.jwt"

        provider = GitHubAppInstallationTokenProvider(
            config=config(),
            transport=transport,
            clock=lambda: NOW,
            jwt_encoder=encode,
        )
        self.assertEqual(provider.get_token(), "installation-token")
        self.assertEqual(provider.get_token(), "installation-token")
        self.assertEqual(len(transport.calls), 1)
        url, headers, payload = transport.calls[0]
        self.assertEqual(url, f"{GITHUB_API_ROOT}/app/installations/456/access_tokens")
        self.assertEqual(payload, {"repositories": [GITHUB_REPOSITORY], "permissions": {"actions": "write"}})
        self.assertEqual(headers["X-GitHub-Api-Version"], GITHUB_API_VERSION)
        self.assertEqual(captured["algorithm"], "RS256")
        self.assertNotIn("installation-token", repr(token_response()))

    def test_refresh_skew_and_thread_safe_single_mint(self):
        current = [NOW]
        transport = ScriptedTransport(
            token_response("one"),
            GitHubHttpResponse(
                201,
                {},
                {
                    "token": "two",
                    "expires_at": (NOW + timedelta(hours=2)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
            ),
        )
        provider = GitHubAppInstallationTokenProvider(
            config=config(),
            transport=transport,
            clock=lambda: current[0],
            jwt_encoder=lambda *args, **kwargs: "app.jwt",
        )
        barrier = threading.Barrier(8)
        results = []

        def read():
            barrier.wait(timeout=5)
            results.append(provider.get_token())

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(results, ["one"] * 8)
        self.assertEqual(len(transport.calls), 1)
        current[0] = NOW + timedelta(minutes=56)
        self.assertEqual(provider.get_token(), "two")
        self.assertEqual(len(transport.calls), 2)

    def test_token_mint_failure_matrix_has_no_internal_retry(self):
        cases = (
            (GitHubRequestNotSent("not sent"), InstallationTokenRetryableError),
            (GitHubRequestOutcomeUnknown("unknown"), InstallationTokenRetryableError),
            (GitHubHttpResponse(429, {}, {}), InstallationTokenRetryableError),
            (
                GitHubHttpResponse(403, {"retry-after": "60"}, {}),
                InstallationTokenRetryableError,
            ),
            (GitHubHttpResponse(503, {}, {}), InstallationTokenRetryableError),
            (GitHubHttpResponse(401, {}, {}), InstallationTokenFinalError),
            (GitHubHttpResponse(403, {}, {}), InstallationTokenFinalError),
        )
        for response, expected in cases:
            with self.subTest(response=type(response).__name__, expected=expected):
                transport = ScriptedTransport(response)
                provider = GitHubAppInstallationTokenProvider(
                    config=config(),
                    transport=transport,
                    clock=lambda: NOW,
                    jwt_encoder=lambda *args, **kwargs: "app.jwt",
                )
                with self.assertRaises(expected):
                    provider.get_token()
                self.assertEqual(len(transport.calls), 1)


class RealGitHubDispatcherTests(unittest.TestCase):
    def dispatcher(self, response):
        transport = ScriptedTransport(response)
        return RealGitHubDispatcher(
            token_provider=StaticTokenProvider(), transport=transport
        ), transport

    def test_success_has_fixed_target_ref_and_no_inputs(self):
        dispatcher, transport = self.dispatcher(
            GitHubHttpResponse(200, {}, {"workflow_run_id": 987})
        )
        result = dispatcher.dispatch(workflow=GATE_A_WORKFLOW, ref=DISPATCH_REF)
        self.assertEqual(result.outcome, DispatchOutcome.SUCCESS)
        self.assertEqual(result.workflow_run_id, "987")
        url, headers, payload = transport.calls[0]
        self.assertEqual(
            url,
            f"{GITHUB_API_ROOT}/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/actions/workflows/{GATE_A_WORKFLOW}/dispatches",
        )
        self.assertEqual(
            payload,
            {"ref": "main", "return_run_details": True},
        )
        self.assertEqual(set(payload), {"ref", "return_run_details"})
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(headers["Authorization"].startswith("Bearer "))

    def test_allowlist_rejects_before_auth_or_http(self):
        transport = ScriptedTransport()
        provider = StaticTokenProvider()
        dispatcher = RealGitHubDispatcher(
            token_provider=provider, transport=transport
        )
        for workflow, ref in (("website-publish.yml", "main"), (GATE_A_WORKFLOW, "dev")):
            with self.subTest(workflow=workflow, ref=ref), self.assertRaises(
                WorkflowNotAllowlistedError
            ):
                dispatcher.dispatch(workflow=workflow, ref=ref)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(transport.calls, [])

    def test_response_retry_matrix(self):
        cases = (
            (GitHubHttpResponse(429, {}, {}), DispatchOutcome.CLEAR_RETRYABLE_FAILURE),
            (GitHubHttpResponse(403, {"retry-after": "60"}, {}), DispatchOutcome.CLEAR_RETRYABLE_FAILURE),
            (GitHubHttpResponse(403, {}, {}), DispatchOutcome.CLEAR_FINAL_FAILURE),
            (GitHubHttpResponse(404, {}, {}), DispatchOutcome.CLEAR_FINAL_FAILURE),
            (GitHubHttpResponse(422, {}, {}), DispatchOutcome.CLEAR_FINAL_FAILURE),
            (GitHubHttpResponse(408, {}, {}), DispatchOutcome.UNKNOWN_OUTCOME),
            (GitHubHttpResponse(503, {}, {}), DispatchOutcome.UNKNOWN_OUTCOME),
            (GitHubHttpResponse(200, {}, {}), DispatchOutcome.UNKNOWN_OUTCOME),
            (
                GitHubHttpResponse(200, {}, {"workflow_run_id": "0"}),
                DispatchOutcome.UNKNOWN_OUTCOME,
            ),
        )
        for response, expected in cases:
            with self.subTest(status=response.status_code):
                dispatcher, _ = self.dispatcher(response)
                self.assertEqual(
                    dispatcher.dispatch(
                        workflow=GATE_A_WORKFLOW, ref=DISPATCH_REF
                    ).outcome,
                    expected,
                )

    def test_transport_failure_classification(self):
        for failure, expected in (
            (GitHubRequestNotSent("safe"), DispatchOutcome.CLEAR_RETRYABLE_FAILURE),
            (GitHubRequestOutcomeUnknown("unknown"), DispatchOutcome.UNKNOWN_OUTCOME),
        ):
            with self.subTest(expected=expected):
                dispatcher, _ = self.dispatcher(failure)
                self.assertEqual(
                    dispatcher.dispatch(
                        workflow=GATE_A_WORKFLOW, ref=DISPATCH_REF
                    ).outcome,
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
