
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
    module = load_lambda("blackout_dates")
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


def event(method, body=None, blackout_id=None, role="DOCTOR"):
    return {
        "httpMethod": method,
        "body": body or {},
        "pathParameters": (
            {"blackout_id": blackout_id} if blackout_id else {}
        ),
        "requestContext": {
            "authorizer": {"role": role, "userId": "12"}
        },
    }


def test_requires_doctor(api):
    module, _, _ = api
    result = module.handler(event("GET", role="PATIENT"), None)
    assert result["statusCode"] == 403


def test_list_blackout_dates(api):
    module, _, cursor = api
    cursor.fetchall.return_value = []
    result = module.handler(event("GET"), None)
    assert result["statusCode"] == 200
    cursor.execute.assert_called_once()


def test_create_blackout_date(api):
    module, conn, cursor = api
    cursor.lastrowid = 7
    cursor.fetchall.return_value = []
    result = module.handler(
        event("POST", {
            "blackout_date": "2026-12-25",
            "reason": "Holiday",
        }),
        None,
    )
    assert result["statusCode"] == 201
    conn.commit.assert_called_once()


def test_invalid_blackout_date_rejected(api):
    module, conn, _ = api
    result = module.handler(
        event("POST", {"blackout_date": "not-a-date"}),
        None,
    )
    assert result["statusCode"] == 400
    conn.commit.assert_not_called()


def test_delete_blackout_date(api):
    module, conn, cursor = api
    cursor.rowcount = 1
    result = module.handler(event("DELETE", blackout_id="7"), None)
    assert result["statusCode"] == 200
    conn.commit.assert_called_once()


def test_blackout_blocked_while_patients_booked(api):
    from datetime import datetime
    module, conn, cursor = api
    cursor.fetchall.return_value = [{
        "slot_id": 5, "slot_start": datetime(2026, 12, 25, 4, 30), "slot_end": datetime(2026, 12, 25, 5, 0),
        "patient_name": "Asha",
    }]
    result = module.handler(event("POST", {"blackout_date": "2026-12-25"}), None)
    assert result["statusCode"] == 409
    assert result["body"]["code"] == "BLACKOUT_HAS_BOOKINGS"
    assert result["body"]["booked_slots"][0]["slot_start"] == "10:00"
    assert result["body"]["booked_slots"][0]["patients"] == ["Asha"]
    conn.commit.assert_not_called()


def test_check_date_lists_booked_slots(api):
    module, _, cursor = api
    cursor.fetchall.return_value = []
    ev = event("GET")
    ev["queryStringParameters"] = {"check_date": "2026-12-25"}
    result = module.handler(ev, None)
    assert result["statusCode"] == 200
    assert result["body"]["booked_slots"] == []
