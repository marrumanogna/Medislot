"""Milestone 6 unit tests: waitlist ordering, hold exclusivity (Mandatory Test 4 logic), expiry, accept."""

import threading
from datetime import datetime, timedelta

import pytest

from fakes import load_lambda
from m5_fakes import FakeEvents, FakeMetrics, run_parallel

wl = load_lambda("waitlist")
NOW = datetime(2026, 11, 20, 6, 0, 0)
START = NOW + timedelta(days=2)


class Seats:
    def __init__(self, capacity=1, booked=1):
        self.lock = threading.Lock()
        self.item = {"slot_id": "1", "capacity": capacity, "booked_count": booked, "held_count": 0, "version": 3,
                     "status": "OPEN", "start_at": START.isoformat()}

    def get(self, _):
        with self.lock:
            return dict(self.item)

    def hold(self, _):
        with self.lock:
            i = self.item
            if i["booked_count"] >= i["capacity"] or i["status"] != "OPEN":
                raise wl.SeatUnavailable()
            i["booked_count"] += 1; i["held_count"] += 1
            return dict(i)

    def convert(self, _):
        with self.lock:
            if self.item["held_count"] <= 0:
                raise wl.SeatUnavailable()
            self.item["held_count"] -= 1

    def free_hold(self, _):
        with self.lock:
            i = self.item
            if i["held_count"] <= 0 or i["booked_count"] <= 0:
                raise wl.SeatUnavailable()
            i["held_count"] -= 1; i["booked_count"] -= 1


class WL:
    def __init__(self):
        self.lock, self.rows = threading.Lock(), {}

    def join(self, slot_id, pid, now, slot_start=None):
        e = {"slot_id": str(slot_id), "waitlist_entry": f"{now.isoformat(timespec='microseconds')}#{pid:012d}",
             "patient_id": str(pid), "joined_at": now.isoformat(timespec="microseconds"), "status": "WAITING"}
        if slot_start:
            e["slot_start_epoch"] = int(slot_start.replace(tzinfo=wl.timezone.utc).timestamp())
        self.rows[(e["slot_id"], e["waitlist_entry"])] = e
        return e

    def entries(self, slot_id):
        with self.lock:
            return sorted((dict(e) for (s, _), e in self.rows.items() if s == str(slot_id)), key=lambda e: e["waitlist_entry"])

    def waiting(self, slot_id): return [e for e in self.entries(slot_id) if e["status"] == "WAITING"]
    def by_patient(self, pid): return sorted((dict(e) for e in self.rows.values() if e["patient_id"] == str(pid)), key=lambda e: e["joined_at"])

    def _move(self, e, new, old, **extra):
        with self.lock:
            row = self.rows.get((e["slot_id"], e["waitlist_entry"]))
            if not row or row["status"] != old:
                return False
            row.update(status=new, **extra)
            return True

    def claim(self, e, oid): return self._move(e, "OFFERED", "WAITING", offer_id=oid)
    def revert(self, e): return self._move(e, "WAITING", "OFFERED")

    def remove(self, e):
        with self.lock:
            self.rows.pop((e["slot_id"], e["waitlist_entry"]), None)


class Offers:
    def __init__(self):
        self.lock, self.rows = threading.Lock(), {}

    def create(self, o): self.rows[o["offer_id"]] = dict(o)
    def get(self, oid): return dict(self.rows[oid]) if oid in self.rows else None

    def transition(self, o, new, old="LIVE"):
        with self.lock:
            row = self.rows[o["offer_id"]]
            if row["status"] != old:
                return False
            row["status"] = new
            return True

    def due(self, now_epoch, limit=50):
        return [dict(o) for o in self.rows.values() if o["status"] == "LIVE" and o["expires_at"] <= now_epoch]

    def live(self): return [o for o in self.rows.values() if o["status"] == "LIVE"]


class Repo:
    def __init__(self):
        self.appts, self.slot_status = [], "AVAILABLE"
        self.slot = {"slot_id": 1, "doctor_id": 7, "consultation_type_id": 1, "slot_start": START,
                     "slot_end": START + timedelta(minutes=30), "status": "AVAILABLE", "fee": 500,
                     "consultation_type": "Quick", "doctor_name": "Dr T"}

    def patient_id(self, uid): return uid
    def get_slot(self, _): return dict(self.slot)
    def upcoming(self, pid, now): return [a for a in self.appts if a["patient_id"] == pid]
    def commit(self): pass
    def rollback(self): pass
    def set_slot_status(self, *a): pass
    def find_by_key(self, pid, key): return next((a["appointment_id"] for a in self.appts if a["key"] == key), None)

    def insert_appointment(self, pid, slot, key):
        self.appts.append({"appointment_id": len(self.appts) + 1, "patient_id": pid, "slot_id": slot["slot_id"], "doctor_id": 7, "key": key})
        return len(self.appts)


def build(capacity=1, booked=1, patients=(11, 12, 13), clock=None):
    clock = clock or {"now": NOW}
    wl.ssm_int = lambda name, default=None: 3 if "max-upcoming" in name else {"waitlist/max-per-patient": 5, "waitlist/hold-minutes": 15}.get(name, default)
    seats, lst, off = Seats(capacity, booked), WL(), Offers()
    eng = wl.WaitlistEngine(lst, off, seats, FakeEvents(), FakeMetrics(), clock=lambda: clock["now"],
                            hold_seconds=lambda: 900, max_lists=lambda: 5)
    repo = Repo()
    for i, p in enumerate(patients):
        clock["now"] = NOW + timedelta(seconds=i)       # join order = patients order
        eng.join(repo, p, 1)
    clock["now"] = NOW + timedelta(minutes=1)
    return eng, repo, seats, lst, off, clock


def test_join_requires_full_slot_and_orders_by_join_time():
    eng, repo, seats, lst, *_ = build()
    assert [e["patient_id"] for e in lst.waiting(1)] == ["11", "12", "13"]
    seats.item["booked_count"] = 0
    with pytest.raises(wl.ApiError) as err:
        eng.join(repo, 99, 1)
    assert err.value.code == "SLOT_NOT_FULL"


def test_join_twice_and_waitlist_cap():
    eng, repo, *_ = build(patients=(11,))
    with pytest.raises(wl.ApiError) as err:
        eng.join(repo, 11, 1)
    assert err.value.code == "ALREADY_WAITLISTED"
    eng.max_lists = lambda: 1                       # patient 11 is already on one list
    with pytest.raises(wl.ApiError) as err:
        eng.join(repo, 11, 2)
    assert err.value.code in ("WAITLIST_LIMIT", "SLOT_NOT_FULL")


@pytest.mark.parametrize("round_", range(5))
def test_exclusivity_one_freed_seat_three_waiting_one_live_offer(round_):
    """MANDATORY TEST 4 (logic level): many consumers race on ONE freed seat; exactly one offer is live."""
    eng, repo, seats, lst, off, _ = build()
    seats.item["booked_count"] -= 1                                  # a cancellation frees the seat
    results = run_parallel(8, lambda i: eng.promote(1))
    assert len([r for r in results if r]) == 1
    assert len(off.live()) == 1
    assert off.live()[0]["patient_id"] == "11"                       # first in line
    assert seats.item["booked_count"] == 1 and seats.item["held_count"] == 1   # seat is held, so unbookable
    assert [e["status"] for e in lst.entries(1)] == ["OFFERED", "WAITING", "WAITING"]
    assert len(eng.events.of("WaitlistOfferIssued")) == 1


def test_expiry_passes_seat_to_next_and_is_safe_to_run_twice():
    eng, repo, seats, lst, off, clock = build()
    seats.item["booked_count"] -= 1
    eng.promote(1)
    clock["now"] += timedelta(minutes=16)
    assert eng.expire_offers() == 1
    assert eng.expire_offers() == 0                                  # second run: no duplicate effect
    assert [o["patient_id"] for o in off.live()] == ["12"]
    assert seats.item["held_count"] == 1 and seats.item["booked_count"] == 1
    assert [e["patient_id"] for e in lst.entries(1)] == ["12", "13"]  # patient 11 left the queue
    assert len(eng.events.of("WaitlistOfferExpired")) == 1


def test_accept_creates_one_appointment_and_converts_hold():
    eng, repo, seats, lst, off, _ = build()
    seats.item["booked_count"] -= 1
    offer = eng.promote(1)
    with pytest.raises(wl.ApiError) as err:                           # someone else cannot use it
        eng.accept(repo, 12, offer["offer_id"])
    assert err.value.status == 404
    status, body = eng.accept(repo, 11, offer["offer_id"])
    assert status == 201 and len(repo.appts) == 1
    assert seats.item["held_count"] == 0 and seats.item["booked_count"] == 1
    status, again = eng.accept(repo, 11, offer["offer_id"])           # replay returns the same appointment
    assert status == 200 and len(repo.appts) == 1
    assert "11" not in [e["patient_id"] for e in lst.entries(1)]


def test_accept_after_expiry_is_rejected():
    eng, repo, seats, lst, off, clock = build()
    seats.item["booked_count"] -= 1
    offer = eng.promote(1)
    clock["now"] += timedelta(minutes=20)
    with pytest.raises(wl.ApiError) as err:
        eng.accept(repo, 11, offer["offer_id"])
    assert err.value.status == 410


def test_multi_capacity_two_freed_seats_two_offers():
    eng, repo, seats, lst, off, _ = build(capacity=3, booked=3, patients=(11, 12, 13))
    seats.item["booked_count"] -= 2
    assert len([r for r in run_parallel(6, lambda i: eng.promote(1)) if r]) == 2
    assert len(off.live()) == 2 and seats.item["booked_count"] == 3


def test_leave_while_offered_releases_hold_and_passes_on():
    eng, repo, seats, lst, off, _ = build()
    seats.item["booked_count"] -= 1
    eng.promote(1)
    eng.leave(repo, 11, 1)
    assert [o["patient_id"] for o in off.live()] == ["12"]


def test_doctor_closing_the_slot_clears_queue_and_offers():
    eng, repo, seats, lst, off, _ = build()
    seats.item["booked_count"] -= 1
    eng.promote(1)
    eng.close_slot(1)
    assert lst.entries(1) == [] and off.live() == []
    assert len(eng.events.of("WaitlistOfferExpired")) == 1


def test_waitlists_for_started_slots_do_not_count_toward_cap():
    eng, repo, seats, lst, off, clock = build(patients=(11,))
    clock["now"] = START + timedelta(hours=1)          # slot 1 has started
    assert eng.listing(repo, 11)[1]["items"] == []


def test_seat_updates_only_pass_expression_attribute_names_they_use():
    """DynamoDB raises ValidationException for unused ExpressionAttributeNames (caused a 500 on accept)."""
    from unittest.mock import MagicMock
    seats = wl.Seats()
    seats._t = MagicMock()
    seats._t.update_item.return_value = {"Attributes": {"slot_id": "1"}}
    for op in (seats.hold, seats.convert, seats.free_hold):
        op(1)
        kw = seats._t.update_item.call_args.kwargs
        text = kw["UpdateExpression"] + " " + kw["ConditionExpression"]
        assert all(name in text for name in kw.get("ExpressionAttributeNames", {}))