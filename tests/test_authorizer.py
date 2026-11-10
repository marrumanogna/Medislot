
import os
import sys
import time

import boto3
import jwt
import pytest
from botocore.stub import Stubber
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lambdas")
)
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "authorizer")
)

# Mock boto3 client/resource creation before importing common.py.
with patch("boto3.client") as mock_client, patch("boto3.resource") as mock_resource:
    mock_client.return_value = MagicMock()
    mock_resource.return_value = MagicMock()

    import lambda_function as authorizer


SECRET = "test-secret-not-for-production"


class FakeTable:
    def __init__(self, denied=False):
        self.denied = denied

    def get_item(self, Key):
        if self.denied:
            return {
                "Item": {
                    "expires_at": int(time.time()) + 100,
                    "status": "REVOKED",
                }
            }
        return {}


def token(role="PATIENT", exp=None):
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "12",
            "userId": "12",
            "role": role,
            "iss": "medislot",
            "aud": "medislot-api",
            "iat": now - 1,
            "exp": exp or now + 300,
            "jti": "test-jti",
        },
        SECRET,
        algorithm="HS256",
    )


def setup(monkeypatch, denied=False):
    monkeypatch.setattr(
        authorizer, "get_param", lambda *a, **k: SECRET
    )
    monkeypatch.setattr(
        authorizer, "auth_table", lambda: FakeTable(denied)
    )


def event(tok, route="patient"):
    return {
        "authorizationToken": "Bearer " + tok,
        "methodArn": (
            f"arn:aws:execute-api:us-east-1:123456789012:"
            f"abc/dev/GET/{route}"
        ),
    }


def test_valid_patient_token_allowed(monkeypatch):
    setup(monkeypatch)
    result = authorizer.handler(event(token()), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"]["role"] == "PATIENT"


def test_wrong_role_denied(monkeypatch):
    setup(monkeypatch)
    result = authorizer.handler(event(token("PATIENT"), "doctor"), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_tampered_token_unauthorized(monkeypatch):
    setup(monkeypatch)
    t = token()

    with pytest.raises(Exception, match="Unauthorized"):
        authorizer.handler(event(t[:-2] + "xx"), None)


def test_expired_token_unauthorized(monkeypatch):
    setup(monkeypatch)

    with pytest.raises(Exception, match="Unauthorized"):
        authorizer.handler(
            event(token(exp=int(time.time()) - 60)), None
        )


def test_revoked_token_unauthorized(monkeypatch):
    setup(monkeypatch, denied=True)

    with pytest.raises(Exception, match="Unauthorized"):
        authorizer.handler(event(token()), None)
