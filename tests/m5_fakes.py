"""In-memory stand-ins for DynamoDB, RDS, EventBridge and CloudWatch (Milestone 5 tests).

FakeSlots reproduces DynamoDB conditional-write semantics (atomic compare on
`version`, `booked_count < capacity`, `status = OPEN`). Reads and writes sleep
briefly OUTSIDE the lock so concurrent callers genuinely interleave and lose
races, exactly like parallel Lambda invocations do.
"""

import copy
import random
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

from fakes import load_lambda

appts = load_lambda("appointments")
ApiError = appts.ApiError

NOW = datetime(2026, 11, 9, 6, 0, 0)  # naive UTC, like the database
SLOT_START = NOW + timedelta(hours=48)


class FakeSlots:
    def __init__(self, latency=0.003):
        self.lock, self.items, self.latency = threading.Lock(), {}, latency

    def get(self, slot_id):
        time.sleep(random.uniform(0, self.latency))
        with self.lock:
            item = self.items.get(str(slot_id))
            return dict(item) if item else None

    def create(self, slot):
        with self.lock:
            self.items.setdefault(str(slot["slot_id"]), {
                "slot_id": str(slot["slot_id"]), "capacity": int(slot["capacity"]),
                "booked_count": 0, "version": 0, "status": "OPEN"})

    def reserve(self, slot_id, seen_version):
        time.sleep(self.latency * 2)  # widen the read-to-write window
        with self.lock:
            it = self.items[str(slot_id)]
            if it["version"] != seen_version or it["booked_count"] >= it["capacity"] or it["status"] != "OPEN":
                raise appts.VersionConflict()
            it["booked_count"] += 1
            it["version"] += 1
            return dict(it)

    def release(self, slot_id, seen_version):
        with self.lock:
            it = self.items[str(slot_id)]
            if it["version"] != seen_version or it["booked_count"] <= 0:
                raise appts.VersionConflict()
            it["booked_count"] -= 1
            it["version"] += 1
            return dict(it)

    def close(self, slot_id):
        with self.lock:
            it = self.items.setdefault(str(slot_id), {"slot_id": str(slot_id), "capacity": 0, "booked_count": 0, "version": 0})
            it.update(status="CLOSED", booked_count=0, version=it["version"] + 1)


class FakeIdem:
    def __init__(self):
        self.lock, self.items = threading.Lock(), {}

    def begin(self, key, request_hash):
        with self.lock:
            if key in self.items:
                return dict(self.items[key])
            self.items[key] = {"state": "IN_PROGRESS", "request_hash": request_hash}
            return None

    def complete(self, key, result):
        import json
        with self.lock:
            self.items[key].update(state="COMPLETED", result=json.dumps(result, default=str))

    def abandon(self, key):
        with self.lock:
            self.items.pop(key, None)


class FakeEvents:
    def __init__(self):
        self.sent = []

    def publish(self, detail_type, detail):
        self.sent.append((detail_type, detail))

    def of(self, detail_type):
        return [d for t, d in self.sent if t == detail_type]


class FakeMetrics:
    def __init__(self):
        self.lock, self.points = threading.Lock(), []

    def count(self, name, value=1, unit="Count", **dims):
        with self.lock:
            self.points.append((name, value, dims))

    def total(self, name, **dims):
        return sum(v for n, v, d in self.points if n == name and all(d.get(k) == x for k, x in dims.items()))


class FakeRepo:
    """Shared across threads; the lock stands in for RDS unique constraints."""

    def __init__(self, capacity=3, fee="500.00", fail_insert=False):
        self.lock, self.appointments, self.ledger = threading.Lock(), {}, {}
        self.fail_insert, self.next_id = fail_insert, 1
        self.slot = {"slot_id": 1, "doctor_id": 7, "consultation_type_id": 4, "slot_start": SLOT_START,
                     "slot_end": SLOT_START + timedelta(hours=1), "status": "AVAILABLE",
                     "consultation_type": "Health Camp Session", "capacity": capacity,
                     "fee": Decimal(fee), "doctor_name": "Dr Test"}

    def commit(self): pass
    def rollback(self): pass
    def patient_id(self, user_id): return user_id
    def get_slot(self, slot_id): return dict(self.slot) if int(slot_id) == 1 else None

    def upcoming(self, patient_id, now):
        with self.lock:
            return [dict(a) for a in self.appointments.values()
                    if a["patient_id"] == patient_id and a["status"] == "CONFIRMED"]

    def find_by_key(self, patient_id, key):
        with self.lock:
            for a in self.appointments.values():
                if a["patient_id"] == patient_id and a["idempotency_key"] == key:
                    return self._view(a)
        return None

    def insert_appointment(self, patient_id, slot, key):
        if self.fail_insert:
            raise RuntimeError("RDS unavailable")
        with self.lock:
            appt = {"appointment_id": self.next_id, "slot_id": slot["slot_id"], "doctor_id": slot["doctor_id"],
                    "patient_id": patient_id, "consultation_type_id": slot["consultation_type_id"],
                    "slot_start": slot["slot_start"], "slot_end": slot["slot_end"], "fee": slot["fee"],
                    "status": "CONFIRMED", "idempotency_key": key, "cancel_reason": None}
            self.appointments[self.next_id] = appt
            self.next_id += 1
            return self._view(appt), True

    def set_slot_status(self, slot_id, new, old):
        if self.slot["status"] == old:
            self.slot["status"] = new

    def _view(self, a):
        out = dict(a, consultation_type="Health Camp Session", doctor_name="Dr Test", patient_name="Pat")
        refund = self.ledger.get(a["appointment_id"])
        if refund:
            out.update(refund_tier=refund["tier"], refund_percent=refund["percent"], refund_amount=refund["amount"])
        return out

    def get_appointment(self, appointment_id, patient_id=None, doctor_id=None, for_update=False):
        a = self.appointments.get(appointment_id)
        if not a or (patient_id is not None and a["patient_id"] != patient_id):
            return None
        return self._view(a)

    def cancel(self, appt, status, reason, tier, percent, amount):
        with self.lock:
            a = self.appointments[appt["appointment_id"]]
            if a["status"] != "CONFIRMED":
                raise ApiError(409, "ALREADY_CANCELLED", "no")
            a.update(status=status, cancel_reason=reason)
            self.ledger[a["appointment_id"]] = {"tier": tier, "percent": percent, "amount": amount, "fee": a["fee"]}

    def get_doctor_slot(self, slot_id, doctor_id):
        return dict(self.slot) if doctor_id == 7 else None

    def booked_for_slot(self, slot_id):
        return [self._view(a) for a in self.appointments.values() if a["status"] == "CONFIRMED"]


def make_engine(repo=None, slots=None, clock=lambda: NOW):
    repo = repo or FakeRepo()
    eng = appts.Engine(slots or FakeSlots(), FakeIdem(), FakeEvents(), FakeMetrics(), clock=clock)
    appts.limit_config = lambda: {"max_upcoming_per_doctor": 50}
    appts.refund_config = lambda: dict(appts.DEFAULT_REFUND)
    return eng, repo


def run_parallel(n, fn):
    """Release n threads at the same instant. fn(i) -> result. Returns results."""
    barrier, results, lock = threading.Barrier(n), [], threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            out = fn(i)
        except ApiError as exc:
            out = ("ERR", exc.status, exc.code)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results
