
"""Thin server-side client for the MediSlot API Gateway.

The browser never talks to API Gateway directly.
Every call is made from Flask using tokens held in the server-side session.
"""

import logging
import os
import threading
import time

import requests

API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
TIMEOUT = 15

# Configure logging to display errors in the Flask terminal.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

# Refresh tokens are single-use.
_refresh_lock = threading.Lock()
_recent_refreshes = {}
_REUSE_WINDOW = 120


def _parse(resp):
    try:
        data = resp.json()
    except ValueError:
        data = {
            "message": (resp.text or "")[:200] or "Unexpected response"
        }

    if (
        resp.status_code == 403
        and isinstance(data, dict)
        and "Missing Authentication Token" in str(data.get("message", ""))
    ):
        return 404, {
            "message": "This feature is not available on the backend yet.",
            "notAvailable": True
        }

    return resp.status_code, data


def raw(method, path, *, json=None, params=None, token=None, headers=None):
    """Single HTTP call. Returns (status, data). Never raises on HTTP errors."""

    hdrs = {"Accept": "application/json"}

    if token:
        hdrs["Authorization"] = f"Bearer {token}"

    if headers:
        hdrs.update(headers)

    url = f"{API_BASE_URL}/{path.lstrip('/')}"

    try:
        logger.info("Calling MediSlot API: %s %s", method, url)

        resp = requests.request(
            method,
            url,
            json=json,
            params=params,
            headers=hdrs,
            timeout=TIMEOUT,
        )

        logger.info(
            "MediSlot API response: %s %s -> HTTP %s",
            method,
            path,
            resp.status_code
        )

    except requests.RequestException:
        logger.exception(
            "MediSlot API connection failed: %s %s",
            method,
            url
        )

        return 502, {
            "message": "Unable to reach the MediSlot service. Please try again."
        }

    return _parse(resp)


def store_tokens(session, data):
    session["access_token"] = data["accessToken"]

    if data.get("refreshToken"):
        session["refresh_token"] = data["refreshToken"]


def _refresh(session):
    old = session.get("refresh_token")

    if not old:
        return False

    with _refresh_lock:
        now = time.time()

        for key in [
            k for k, (_, ts) in _recent_refreshes.items()
            if now - ts > _REUSE_WINDOW
        ]:
            _recent_refreshes.pop(key, None)

        if old in _recent_refreshes:
            store_tokens(session, _recent_refreshes[old][0])
            return True

        status, data = raw(
            "POST",
            "auth/refresh",
            json={"refreshToken": old}
        )

        if status != 200 or "accessToken" not in data:
            return False

        _recent_refreshes[old] = (data, now)
        store_tokens(session, data)

        return True


def call(session, method, path, *, json=None, params=None, headers=None):
    """Authenticated API call, refreshing once if access token expires."""

    status, data = raw(
        method,
        path,
        json=json,
        params=params,
        token=session.get("access_token"),
        headers=headers
    )

    if status == 401 and _refresh(session):
        status, data = raw(
            method,
            path,
            json=json,
            params=params,
            token=session.get("access_token"),
            headers=headers
        )

    return status, data
