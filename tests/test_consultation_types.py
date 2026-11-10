
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

def load_lambda(name):
    path = ROOT / "lambdas" / name / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch("boto3.client"), patch("boto3.resource"):
        spec.loader.exec_module(module)
    return module

@pytest.fixture
def api():
    module = load_lambda("consultation_types")
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    module.config = MagicMock(return_value={})
    module.dbconn = MagicMock(return_value=conn)
    module.response = lambda status, data: {
        "statusCode": status, "body": data
    }
    module.body = lambda event: event.get("body", {})
    return module, conn, cursor

def event(method, body=None, type_id=None, role="DOCTOR"):
    return {
        "httpMethod": method,
        "body": body or {},
        "pathParameters": {"type_id": type_id} if type_id else {},
        "requestContext": {
            "authorizer": {"role": role, "userId": "12"}
        },
    }

def test_requires_doctor(api):
    module, _, _ = api
    result = module.handler(event("GET", role="PATIENT"), None)
    assert result["statusCode"] == 403

def test_list_types(api):
    module, _, cursor = api
    cursor.fetchall.return_value = []
    result = module.handler(event("GET"), None)
    assert result["statusCode"] == 200
    cursor.execute.assert_called_once()

def test_post_method_not_allowed(api):
    module, conn, cursor = api

    result = module.handler(
        event("POST", {
            "name": "Video consultation",
            "duration_minutes": 30,
            "fee": 500,
        }),
        None,
    )

    assert result["statusCode"] == 405
    conn.commit.assert_not_called()
