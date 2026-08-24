#!/usr/bin/env python3

import os
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1


CREATE_POST_URL = "https://api.x.com/2/tweets"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    auth = OAuth1(
        required_env("X_API_KEY"),
        required_env("X_API_SECRET"),
        required_env("X_ACCESS_TOKEN"),
        required_env("X_ACCESS_TOKEN_SECRET"),
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    response = requests.post(
        CREATE_POST_URL,
        auth=auth,
        json={
            "text": f"The Daily Duck X API test — {timestamp}"
        },
        timeout=60,
    )

    print("STATUS:", response.status_code)
    print("RESPONSE:", response.text)

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"X text-only test failed: HTTP {response.status_code}: {response.text}"
        )

    print("X TEXT TEST SUCCESS")


if __name__ == "__main__":
    main()
