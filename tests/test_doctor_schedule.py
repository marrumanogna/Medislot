
import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas"))


def load_lambda():
    path = ROOT / "lambdas" / "doctor_schedule" / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(
        "doctor_schedule", path
    )
    module = importlib.util.module_from_spec(spec)
    with patch("boto3.client"), patch("boto3.resource"):
        spec.loader.exec_module(module)
    return module


def test_rejects_unauthenticated_request():
    module = load_lambda()
    module.response = lambda status, data: {
        "statusCode": status,
        "body": data,
    }

    result = module.handler(
        {"queryStringParameters": {"date": "2026-10-29"}},
        None,
    )

    assert result["statusCode"] == 403


def test_requires_date():
    module = load_lambda()
    module.response = lambda status, data: {
        "statusCode": status,
        "body": data,
    }

    event = {
        "requestContext": {
            "authorizer": {"userId": "12", "role": "DOCTOR"}
        },
        "queryStringParameters": None,
    }

    result = module.handler(event, None)

    assert result["statusCode"] == 400


def test_rejects_invalid_date():
    module = load_lambda()
    module.response = lambda status, data: {
        "statusCode": status,
        "body": data,
    }

    event = {
        "requestContext": {
            "authorizer": {"userId": "12", "role": "DOCTOR"}
        },
        "queryStringParameters": {"date": "29-10-2026"},
    }

    result = module.handler(event, None)

    assert result["statusCode"] == 400


def test_local_day_utc_boundaries_across_dst():
    # New York's spring DST transition makes this local day 23 hours long.
    timezone = ZoneInfo("America/New_York")
    day = date(2026, 3, 8)

    local_start = datetime.combine(day, datetime.min.time()).replace(
        tzinfo=timezone
    )
    local_end = datetime.combine(
        day + timedelta(days=1), datetime.min.time()
    ).replace(tzinfo=timezone)

    utc_start = local_start.astimezone(ZoneInfo("UTC"))
    utc_end = local_end.astimezone(ZoneInfo("UTC"))

    assert (utc_end - utc_start).total_seconds() == 23 * 60 * 60

