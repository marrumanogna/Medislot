
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


def test_patch_times_only_accepts_mysql_int_is_active(api):
    """MySQL returns TINYINT 1/0; editing only the hours must not trip the is_active check."""
    from datetime import timedelta
    module, conn, cursor = api
    module.find_availability_conflict = MagicMock(return_value=None)
    cursor.fetchone.return_value = {
        "day_of_week": 1, "start_time": timedelta(hours=9), "end_time": timedelta(hours=12),
        "timezone": "Asia/Kolkata", "is_active": 1,
    }
    result = module.handler(
        event("PATCH", {"start_time": "10:00:00", "end_time": "13:00:00"}, pattern_id="5"), None
    )
    assert result["statusCode"] == 200, result
    conn.commit.assert_called_once()
