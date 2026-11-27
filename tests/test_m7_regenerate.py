"""Pattern changes rebuild unbooked slots only; formatted e-mails; tolerant is_active."""
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1] / "lambdas"
sys.path.insert(0, str(ROOT))


def load(name):
    spec = importlib.util.spec_from_file_location(name + "_m7", ROOT / name / "lambda_function.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_regenerate_removes_only_unbooked_slots_and_restores_published():
    mod = load("doctor_slot_management")
    cur = MagicMock()
    start = datetime(2026, 12, 1, 4, 30)
    cur.fetchall.return_value = [
        {"slot_id": 1, "slot_start": start, "slot_end": start + timedelta(minutes=30), "status": "AVAILABLE",
         "consultation_type_id": 5, "has_booking": 0},
        {"slot_id": 2, "slot_start": start + timedelta(hours=1), "slot_end": start + timedelta(hours=1, minutes=30),
         "status": "AVAILABLE", "consultation_type_id": 5, "has_booking": 1},
        {"slot_id": 3, "slot_start": start + timedelta(hours=2), "slot_end": start + timedelta(hours=2, minutes=30),
         "status": "DRAFT", "consultation_type_id": None, "has_booking": 0},
    ]
    count, published = mod.clear_unbooked_future_slots(cur, 9, datetime.utcnow() + timedelta(days=15))
    assert count == 2 and [r["slot_id"] for r in published] == [1]
    delete_args = cur.execute.call_args_list[-1][0][1]
    assert delete_args == [9, 1, 3]  # slot 2 has a confirmed patient and is kept
    cur.rowcount = 1
    assert mod.restore_published(cur, 9, published) == 1


def test_is_active_accepts_numbers_and_strings():
    mod = load("availability_patterns")
    assert mod.parse_bool(1) is True and mod.parse_bool(0) is False
    assert mod.parse_bool("false") is False and mod.parse_bool("true") is True
    assert mod.parse_bool(None) is True and mod.parse_bool("maybe") is None and mod.parse_bool(7) is None


def test_email_is_formatted_text():
    mod = load("notifications")
    subject, text, body = mod.compose("AppointmentCancelled", "patient", {
        "appointment_id": 7, "slot_start": "2026-12-01T10:00:00+05:30", "status": "CANCELLED_BY_DOCTOR",
        "refund_amount": "500.00", "refund_percent": 100})
    assert subject == "MediSlot: Appointment cancelled"
    assert body.startswith("Hello,") and "Tue 01 Dec 2026, 10:00 AM (UTC+05:30)" in body
    assert "Refund" in body and body.rstrip().endswith("please do not reply.")
    assert "{" not in body
