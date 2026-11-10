
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
    module = load_lambda("availability_patterns")
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


def event(method, body=None, pattern_id=None, role="DOCTOR"):
    return {
        "httpMethod": method,
        "body": body or {},
        "pathParameters": {"pattern_id": pattern_id} if pattern_id else {},
        "requestContext": {
            "authorizer": {"role": role, "userId": "12"}
        },
    }


def test_requires_doctor(api):
    module, _, _ = api
    result = module.handler(event("GET", role="PATIENT"), None)
    assert result["statusCode"] == 403


def test_list_patterns(api):
    module, _, cursor = api
    cursor.fetchall.return_value = []
    result = module.handler(event("GET"), None)
    assert result["statusCode"] == 200
    cursor.execute.assert_called_once()


def test_create_pattern(api):
    module, conn, cursor = api
    cursor.lastrowid = 4
    result = module.handler(
        event("POST", {
            "day_of_week": 0,
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "timezone": "Asia/Kolkata",
        }),
        None,
    )
    assert result["statusCode"] == 201
    conn.commit.assert_called_once()


def test_invalid_day_rejected(api):
    module, conn, _ = api
    result = module.handler(
        event("POST", {
            "day_of_week": 8,
            "start_time": "09:00:00",
            "end_time": "17:00:00",
        }),
        None,
    )
    assert result["statusCode"] == 400
    conn.commit.assert_not_called()
