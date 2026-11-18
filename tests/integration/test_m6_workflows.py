"""Milestone 6 against the DEPLOYED API.

Mandatory Test 3 (reschedule saga): a forced failure while releasing the original seat leaves the patient
holding exactly one appointment (the original) and the target seat free again.
Mandatory Test 4 (waitlist exclusivity): one freed seat, three waitlisted patients, only ONE live offer, and the
held seat cannot be booked by anyone else.

The forced failure needs the SSM flag /medislot/<env>/testing/allow-fault-injection = 1 (set automatically for non-prod).
"""

import time
import uuid
from datetime import datetime, timedelta

import pytest
import requests

import conftest as cf
from conftest import IST, _register_doctor, _register_patient

QUICK = 1
TIMES = ("10:00", "11:00", "12:00", "13:00", "14:00", "15:00")


def call(user, method, path, headers=None, **kw):
    h = {"Authorization": f"Bearer {user.token}", **(headers or {})}
    return requests.request(method, f"{cf.BASE_URL}/{path}", headers=h, timeout=60, **kw)


def book(user, slot_id):
    return call(user, "POST", "appointments", {"Idempotency-Key": uuid.uuid4().hex}, json={"slot_id": slot_id})


def mine(user):
    items = call(user, "GET", "appointments").json()["items"]
    return [a for a in items if a["status"] == "CONFIRMED"]


@pytest.fixture(scope="module")
def slots(api, tag):
    doctor, _ = _register_doctor(api, tag, "m6", "Cardiology", f"M6 Hospital {tag}", f"M6ville{tag}")
    assert api.call("PUT", f"doctor/consultation-types/{QUICK}", doctor.token, json={"fee": "100"}).status_code == 200
    day = (datetime.now(IST) + timedelta(days=1)).date().isoformat()
    for t in TIMES:
        r = api.call("POST", "doctor/slots", doctor.token,
                     json={"action": "create", "date": day, "start_time": t, "consultation_type_id": QUICK})
        assert r.status_code == 201, r.text
    assert api.call("POST", "doctor/slots", doctor.token, json={"action": "publish", "date": day}).status_code == 200
    items = api.call("GET", f"doctors/{doctor.user_id}/slots", params={"from": day, "to": day}).json()["items"]
    by = {s["slot_start"][11:16]: s["slot_id"] for s in items}
    return [by[t] for t in TIMES]


def test_reschedule_moves_the_single_appointment(api, tag, slots):
    p = _register_patient(api, tag, "rs1")
    a = book(p, slots[0]).json()["appointment"]
    r = call(p, "PATCH", f"appointments/{a['appointment_id']}", json={"slot_id": slots[1]})
    assert r.status_code == 200, r.text
    held = mine(p)
    assert len(held) == 1 and held[0]["slot_id"] == slots[1] and held[0]["reschedules_used"] == 1
    # the original seat is free again
    assert book(_register_patient(api, tag, "rs1b"), slots[0]).status_code == 201


def test_reschedule_to_full_slot_fails_and_original_stays(api, tag, slots):
    p, other = _register_patient(api, tag, "rs2"), _register_patient(api, tag, "rs2b")
    a = book(p, slots[2]).json()["appointment"]
    assert book(other, slots[3]).status_code == 201                     # slot 3 is now full (capacity 1)
    r = call(p, "PATCH", f"appointments/{a['appointment_id']}", json={"slot_id": slots[3]})
    assert r.status_code == 409 and r.json()["code"] == "SLOT_FULL"
    held = mine(p)
    assert len(held) == 1 and held[0]["slot_id"] == slots[2]


def test_mandatory_3_forced_failure_during_release_leaves_exactly_one_appointment(api, tag, slots):
    p = _register_patient(api, tag, "rs3")
    a = book(p, slots[4]).json()["appointment"]
    r = call(p, "PATCH", f"appointments/{a['appointment_id']}", {"X-Medislot-Fail-Release": "true"},
             json={"slot_id": slots[5]})
    assert r.status_code == 409 and r.json()["code"] == "RESCHEDULE_COMPENSATED", r.text
    held = mine(p)
    assert len(held) == 1 and held[0]["slot_id"] == slots[4] and held[0]["reschedules_used"] == 0
    # the claimed target seat was handed back, so another patient can book it
    assert book(_register_patient(api, tag, "rs3b"), slots[5]).status_code == 201


def test_mandatory_4_waitlist_exclusivity(api, tag):
    doctor, _ = _register_doctor(api, tag, "wl", "Cardiology", f"WL Hospital {tag}", f"WLville{tag}")
    assert api.call("PUT", f"doctor/consultation-types/{QUICK}", doctor.token, json={"fee": "100"}).status_code == 200
    day = (datetime.now(IST) + timedelta(days=2)).date().isoformat()
    assert api.call("POST", "doctor/slots", doctor.token, json={"action": "create", "date": day, "start_time": "10:00", "consultation_type_id": QUICK}).status_code == 201
    assert api.call("POST", "doctor/slots", doctor.token, json={"action": "publish", "date": day}).status_code == 200
    slot = api.call("GET", f"doctors/{doctor.user_id}/slots", params={"from": day, "to": day}).json()["items"][0]["slot_id"]

    holder = _register_patient(api, tag, "wlh")
    appt = book(holder, slot).json()["appointment"]                      # the only seat is taken
    waiters = [_register_patient(api, tag, f"wl{i}") for i in range(3)]
    for w in waiters:
        assert call(w, "POST", f"slots/{slot}/waitlist").status_code == 201
        time.sleep(0.05)                                                  # distinct join times -> stable order
    assert call(holder, "POST", f"slots/{slot}/waitlist").status_code == 409   # holds a seat already

    assert call(holder, "DELETE", f"appointments/{appt['appointment_id']}").status_code == 200   # frees ONE seat

    def offers():
        out = []
        for w in waiters:
            for e in call(w, "GET", "patient/waitlists").json()["items"]:
                if e["slot_id"] == slot and e["offer"]:
                    out.append((w, e["offer"]["offer_id"]))
        return out

    deadline, live = time.time() + 60, []
    while time.time() < deadline and not live:
        time.sleep(2)
        live = offers()
    assert len(live) == 1, live                                           # exactly one live offer
    time.sleep(5)
    assert len(offers()) == 1                                             # still exactly one
    assert live[0][0].email == waiters[0].email                           # first in line got it

    outsider = _register_patient(api, tag, "wlx")
    assert book(outsider, slot).status_code == 409                        # held seat is not bookable

    r = call(live[0][0], "POST", f"waitlist-offers/{live[0][1]}/accept")
    assert r.status_code == 201, r.text
    assert len(mine(live[0][0])) == 1
