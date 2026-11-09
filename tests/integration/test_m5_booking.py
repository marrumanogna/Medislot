"""Milestone 5 against the DEPLOYED API: concurrency, idempotency, cancellation and refunds.

Mandatory Test 1 (concurrency): 24 simultaneous bookings at a capacity-3 Health Camp slot, exactly 3 succeed.
Mandatory Test 2 (idempotency): the same Idempotency-Key replayed creates exactly one appointment.
Refund windows are read from SSM in production; these tests assume the documented defaults (48/24/6 h, 100/75/50/0 %).
"""

import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import requests

import conftest as cf
from conftest import IST, _register_doctor, _register_patient

CAMP, QUICK, FEE = 4, 1, Decimal("300.00")


def book(patient, slot_id, key=None):
    return requests.post(
        f"{cf.BASE_URL}/appointments",
        headers={"Authorization": f"Bearer {patient.token}", "Idempotency-Key": key or uuid.uuid4().hex},
        json={"slot_id": slot_id}, timeout=60,
    )


def expected_tier(start, now):
    hours = (start - now).total_seconds() / 3600
    if hours > 48: return "T1", 100
    if hours >= 24: return "T2", 75
    if hours >= 6: return "T3", 50
    return "T4", 0


@pytest.fixture(scope="module")
def camp(api, tag):
    """A doctor with a capacity-3 Health Camp and published slots tomorrow (IST)."""
    doctor, _ = _register_doctor(api, tag, "camp", "Cardiology", f"Camp Hospital {tag}", f"Campville{tag}")
    assert api.call("PUT", f"doctor/consultation-types/{CAMP}", doctor.token, json={"fee": str(FEE), "capacity": 3}).status_code == 200
    assert api.call("PUT", f"doctor/consultation-types/{QUICK}", doctor.token, json={"fee": "100"}).status_code == 200
    day = (datetime.now(IST) + timedelta(days=1)).date().isoformat()
    for start, type_id in (("10:00", CAMP), ("12:00", CAMP), ("14:00", CAMP), ("16:00", CAMP), ("18:00", QUICK)):
        r = api.call("POST", "doctor/slots", doctor.token,
                     json={"action": "create", "date": day, "start_time": start, "consultation_type_id": type_id})
        assert r.status_code == 201, r.text
    assert api.call("POST", "doctor/slots", doctor.token, json={"action": "publish", "date": day}).status_code == 200
    items = api.call("GET", f"doctors/{doctor.user_id}/slots", params={"from": day, "to": day}).json()["items"]
    slots = {s["slot_start"][11:16]: s for s in items}
    return {"doctor": doctor, "day": day, "slots": slots}


def seats_free(api, camp, start):
    items = api.call("GET", f"doctors/{camp['doctor'].user_id}/slots", params={"from": camp["day"], "to": camp["day"]}).json()["items"]
    return {s["slot_start"][11:16]: s["seats_free"] for s in items}[start]


def test_capacity_is_the_doctors_setting(camp):
    assert camp["slots"]["10:00"]["seats_total"] == 3        # Health Camp, doctor chose 3
    assert camp["slots"]["18:00"]["seats_total"] == 1        # normal consultation


def test_concurrency_exactly_capacity_bookings_succeed(api, tag, camp):
    """MANDATORY TEST 1."""
    slot_id = camp["slots"]["10:00"]["slot_id"]
    with ThreadPoolExecutor(8) as pool:
        patients = list(pool.map(lambda i: _register_patient(api, tag, f"cc{i}"), range(24)))

    barrier, results = threading.Barrier(len(patients)), []

    def worker(patient):
        barrier.wait()
        r = book(patient, slot_id)
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=worker, args=(p,)) for p in patients]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert Counter(s for s, _ in results) == {201: 3, 409: 21}, results
    assert {c for s, c in results if s == 409} <= {"SLOT_FULL", "SLOT_BUSY"}
    assert seats_free(api, camp, "10:00") == 0


def test_idempotent_replay_creates_exactly_one_appointment(api, tag, camp):
    """MANDATORY TEST 2."""
    patient, slot_id, key = _register_patient(api, tag, "idem"), camp["slots"]["12:00"]["slot_id"], uuid.uuid4().hex
    first, second, third = book(patient, slot_id, key), book(patient, slot_id, key), book(patient, slot_id, key)

    assert (first.status_code, second.status_code, third.status_code) == (201, 200, 200)
    ids = {r.json()["appointment"]["appointment_id"] for r in (first, second, third)}
    assert len(ids) == 1 and second.json()["idempotent_replay"] is True
    mine = api.call("GET", "appointments", patient.token).json()["items"]
    assert len([a for a in mine if a["slot_id"] == slot_id]) == 1
    assert seats_free(api, camp, "12:00") == 2                # one seat consumed, not three
    other = book(patient, camp["slots"]["14:00"]["slot_id"], key)   # same key, different request
    assert other.status_code == 422 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_cancel_applies_the_refund_tier_and_frees_the_seat(api, tag, camp):
    patient, slot = _register_patient(api, tag, "cancel"), camp["slots"]["14:00"]
    created = book(patient, slot["slot_id"])
    assert created.status_code == 201, created.text
    appt = created.json()["appointment"]
    assert seats_free(api, camp, "14:00") == 2

    start = datetime.fromisoformat(slot["slot_start"])
    tier, percent = expected_tier(start, datetime.now(IST))
    r = api.call("DELETE", f"appointments/{appt['appointment_id']}", patient.token)
    assert r.status_code == 200, r.text
    refund = r.json()["appointment"]["refund"]
    assert (refund["tier"], refund["percent"]) == (tier, percent)
    assert Decimal(refund["amount"]) == (FEE * percent / 100).quantize(Decimal("0.01"))
    assert seats_free(api, camp, "14:00") == 3

    again = api.call("DELETE", f"appointments/{appt['appointment_id']}", patient.token)
    assert again.status_code == 200 and again.json()["already_cancelled"] is True
    ledger = api.call("GET", "patient/refunds", patient.token).json()
    assert len(ledger["items"]) == 1 and ledger["total_refunded"] == refund["amount"]


def test_doctor_cancelling_a_slot_refunds_everyone_in_full(api, tag, camp):
    slot, patients = camp["slots"]["16:00"], [_register_patient(api, tag, f"dc{i}") for i in range(2)]
    for p in patients:
        assert book(p, slot["slot_id"]).status_code == 201
    r = api.call("POST", f"doctor/slots/{slot['slot_id']}/cancel", camp["doctor"].token, json={"reason": "Doctor unavailable"})
    assert r.status_code == 200 and r.json()["cancelled_appointments"] == 2
    for p in patients:
        item = api.call("GET", "patient/refunds", p.token).json()["items"][0]
        assert (item["tier"], item["percent"], item["amount"]) == ("DOCTOR_CANCELLED", 100, str(FEE))
    late = book(_register_patient(api, tag, "late"), slot["slot_id"])
    assert late.status_code == 409


def test_users_only_see_and_change_their_own_appointments(api, tag, camp):
    owner, other = _register_patient(api, tag, "own"), _register_patient(api, tag, "oth")
    appt = book(owner, camp["slots"]["18:00"]["slot_id"]).json()["appointment"]
    assert api.call("GET", f"appointments/{appt['appointment_id']}", other.token).status_code == 404
    assert api.call("DELETE", f"appointments/{appt['appointment_id']}", other.token).status_code == 404
    assert api.call("GET", "appointments", other.token).json()["items"] == []
    assert api.call("POST", "appointments", camp["doctor"].token, json={"slot_id": 1}).status_code == 403
    assert book(owner, camp["slots"]["18:00"]["slot_id"], "k" * 12).status_code in (409,)   # one seat per patient per slot
