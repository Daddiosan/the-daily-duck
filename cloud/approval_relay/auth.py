"""Application authentication for an authenticated Pub/Sub push."""

from __future__ import annotations

from typing import Callable, Mapping


class AuthenticationError(ValueError):
    """The bearer token or verified caller identity is unacceptable."""


TokenVerifier = Callable[[str, str], Mapping[str, object]]


class PushAuthenticator:
    def __init__(
        self,
        *,
        expected_issuer: str,
        expected_audience: str,
        expected_principals: frozenset[str],
        token_verifier: TokenVerifier,
    ) -> None:
        self._expected_issuer = expected_issuer
        self._expected_audience = expected_audience
        self._expected_principals = expected_principals
        self._token_verifier = token_verifier

    def verify(self, authorization_header: str | None) -> None:
        if not isinstance(authorization_header, str):
            raise AuthenticationError("Missing or malformed Authorization header.")
        scheme, separator, token = authorization_header.partition(" ")
        if scheme != "Bearer" or separator != " " or not token or any(
            char.isspace() for char in token
        ):
            raise AuthenticationError("Missing or malformed Authorization header.")
        try:
            claims = self._token_verifier(token, self._expected_audience)
        except Exception as exc:  # noqa: BLE001 - every verifier failure fails closed
            raise AuthenticationError("Token verification failed.") from exc
        if not isinstance(claims, Mapping):
            raise AuthenticationError("Token verification returned malformed claims.")
        if str(claims.get("iss", "")) != self._expected_issuer:
            raise AuthenticationError("Token issuer is not authorized.")
        if claims.get("aud") != self._expected_audience:
            raise AuthenticationError("Token audience is not authorized.")
        if claims.get("email_verified") is not True:
            raise AuthenticationError("Caller email is not verified.")
        principal = str(claims.get("email", "")).strip().casefold()
        if principal not in self._expected_principals:
            raise AuthenticationError("Caller is not an authorized push identity.")


def google_oidc_token_verifier() -> TokenVerifier:
    """Build the Google verifier without verifying a token or doing I/O."""

    try:
        from google.auth.transport import requests as google_auth_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as exc:  # pragma: no cover - container dependency
        raise RuntimeError("Google auth dependencies are unavailable.") from exc
    request_adapter = google_auth_requests.Request()

    def verify(token: str, audience: str) -> Mapping[str, object]:
        return google_id_token.verify_oauth2_token(token, request_adapter, audience)

    return verify
