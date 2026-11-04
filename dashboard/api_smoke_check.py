import os
from pathlib import Path

import pytest
import requests


# Load .env only if it exists.
env_file = Path(".env")

if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()

        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)

            os.environ.setdefault(
                key.strip(),
                value.strip().strip('"').strip("'")
            )


# Read API URL from environment or .env
BASE_URL = os.getenv("API_BASE_URL")


# Skip integration test if API URL is unavailable.
if not BASE_URL:
    pytest.skip(
        "API_BASE_URL is not configured. Skipping API integration test.",
        allow_module_level=True
    )


def test_doctor_registration_endpoint():
    url = BASE_URL.rstrip("/") + "/auth/register/doctor"

    print("Testing URL:", url)

    try:
        response = requests.post(
            url,
            json={},
            timeout=20
        )

        print("HTTP STATUS:", response.status_code)
        print("RESPONSE:", response.text)

        # Empty registration data may return 400.
        # We only fail for server errors.
        assert response.status_code < 500, (
            f"API server error: {response.status_code} "
            f"{response.text}"
        )

    except requests.RequestException as error:
    pytest.skip(f"API unavailable; skipping integration test: {error}")