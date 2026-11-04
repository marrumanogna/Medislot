import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


def load_lambda():
    path = ROOT / "lambdas" / "slot_materialiser" / "lambda_function.py"
    spec = importlib.util.spec_from_file_location("slot_materialiser", path)
    module = importlib.util.module_from_spec(spec)
    with patch("boto3.client"), patch("boto3.resource"):
        spec.loader.exec_module(module)
    return module


def test_invalid_days_ahead():
    module = load_lambda()
    module.response = lambda status, data: {"statusCode": status, "body": data}

    result = module.handler({"days_ahead": 0}, None)

    assert result["statusCode"] == 400


def test_materialises_slots_and_skips_blackout():
    module = load_lambda()
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    module.config = MagicMock(return_value={})
    module.dbconn = MagicMock(return_value=conn)
    module.response = lambda status, data: {"statusCode": status, "body": data}

    today = date.today()
    weekday = today.weekday()
    pattern = {
        "doctor_id": 12,
        "day_of_week": weekday,
        "start_time": time(9, 0),
        "end_time": time(10, 0),
        "timezone": "UTC",
        "consultation_type_id": 5,
        "duration_minutes": 30,
    }

    cursor.fetchall.side_effect = [
        [pattern],
        [{
            "doctor_id": 12,
            "consultation_type_id": 5,
            "name": "Video consultation",
            "duration_minutes": 30,
        }],
        [{"doctor_id": 12, "blackout_date": today}],
    ]

    with patch("datetime.datetime") as mock_datetime:
        mock_datetime.now.return_value.date.return_value = today
        result = module.handler({"days_ahead": 1}, None)

    assert result["statusCode"] == 200
    assert result["body"]["blackout_dates_skipped"] >= 1
    conn.commit.assert_called_once()
    conn.close.assert_called_once()
def test_dst_spring_transition_skips_nonexistent_local_time():
    module = load_lambda()
    timezone = ZoneInfo("America/New_York")

    # On 8 March 2026, New York jumps from 01:59:59 to 03:00:00.
    # Therefore, 02:30 does not exist locally.
    nonexistent_time = datetime(2026, 3, 8, 2, 30)

    result = module.localize_slot(nonexistent_time, timezone)

    assert result is None


def test_dst_fall_transition_uses_consistent_first_occurrence():
    module = load_lambda()
    timezone = ZoneInfo("America/New_York")

    # On 1 November 2026, 01:30 occurs twice in New York.
    local_time = datetime(2026, 11, 1, 1, 30)

    result = module.localize_slot(local_time, timezone)

    assert result is not None
    assert result == datetime(2026, 11, 1, 5, 30)
def test_duplicate_insert_is_ignored():
    module = load_lambda()
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    module.config = MagicMock(return_value={})
    module.dbconn = MagicMock(return_value=conn)
    module.response = lambda status, data: {
        "statusCode": status,
        "body": data,
    }

    # Choose tomorrow, so the test always targets a future slot.
    tomorrow = datetime.now(ZoneInfo("UTC")).date() + timedelta(days=1)

    pattern = {
        "doctor_id": 12,
        "day_of_week": tomorrow.weekday(),
        "start_time": time(23, 0),
        "end_time": time(23, 30),
        "timezone": "UTC",
        "consultation_type_id": 5,
        "duration_minutes": 30,
    }

    cursor.fetchall.side_effect = [
        [pattern],
        [{
            "doctor_id": 12,
            "consultation_type_id": 5,
            "name": "Video consultation",
            "duration_minutes": 30,
        }],
        [],
    ]

    cursor.fetchone.side_effect = [
        {"lock_status": 1},
        None,
    ]

    # Simulate MySQL reporting that INSERT IGNORE skipped a duplicate.
    cursor.rowcount = 0

    result = module.handler({"days_ahead": 1}, None)

    assert result["statusCode"] == 200
    assert result["body"]["slots_created"] == 0

    # Prove the INSERT IGNORE statement was actually attempted.
    insert_calls = [
        call for call in cursor.execute.call_args_list
        if "INSERT IGNORE INTO appointment_slots" in call.args[0]
    ]
    assert len(insert_calls) == 1

    conn.commit.assert_called_once()
    conn.close.assert_called_once()
