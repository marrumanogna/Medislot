"""Milestone 6 unit tests: reschedule saga tasks (Mandatory Test 3 logic), limits, cutoff."""

from datetime import timedelta

import pytest

import m5_fakes as f
from m5_fakes import FakeRepo, FakeSlots, appts, make_engine


class Repo2(FakeRepo):
    """Two slots (1 and 2), the appointment is a single row that moves between them."""

    def __init__(self):
        super().__init__(capacity=1)
        self.slots = {1: dict(self.slot, slot_id=1, slot_start=f.NOW + timedelta(hours=72)),
                      2: dict(self.slot, slot_id=2, slot_start=f.NOW + timedelta(hours=96))}
        for s in self.slots.values():
            s["slot_end"] = s["slot_start"] + timedelta(hours=1)
        self.status_calls = []

    def get_slot(self, slot_id): return dict(self.slots[int(slot_id)]) if int(slot_id) in self.slots else None
    def set_slot_status(self, slot_id, new, old): self.status_calls.append((slot_id, new, old))

    def _view(self, a):
        return dict(super()._view(a), reschedule_count=a.get("reschedule_count", 0))

    def move_appointment(self, aid, frm, to, delta):
        a = self.appointments[aid]
        if a["status"] != "CONFIRMED" or a["slot_id"] != frm["slot_id"]:
            return False
        a.update(slot_id=to["slot_id"], slot_start=to["slot_start"], slot_end=to["slot_end"],
                 reschedule_count=a.get("reschedule_count", 0) + delta)
        return True


def setup(patient=5):
    repo = Repo2()
    eng, _ = make_engine(repo)
    appts.reschedule_config = lambda: {"max-count": 2, "cutoff-hours": 6}
    book = lambda slot: eng.book(repo, patient, slot, f"key-{slot}-{patient}-xx", f"slot:{slot}")
    status, body = book(1)
    return eng, repo, appts.RescheduleTasks(eng), body["appointment"]["appointment_id"]


def saga(tasks, repo, aid, target, fail_release=False, patient=5):
    """Mirror of the Step Functions state machine in auth-stack.yaml."""
    inp = {"user_id": patient, "appointment_id": aid, "target_slot_id": target, "fail_release": fail_release, "correlationId": "c"}
    c = tasks.claim(repo, inp)
    if not c["ok"]:
        return "REJECTED", c
    st = {**inp, "claim": {"value": c}}
    try:
        tasks.commit(repo, st)
        tasks.release_original(repo, st)
    except Exception:
        tasks.compensate(repo, st)
        return "COMPENSATED", None
    tasks.finalize(repo, st)
    return "RESCHEDULED", None


def live(repo): return [a for a in repo.appointments.values() if a["status"] == "CONFIRMED"]


def test_success_moves_the_single_appointment_and_swaps_seats():
    eng, repo, tasks, aid = setup()
    assert saga(tasks, repo, aid, 2)[0] == "RESCHEDULED"
    assert len(live(repo)) == 1 and live(repo)[0]["slot_id"] == 2 and live(repo)[0]["reschedule_count"] == 1
    assert eng.slots.items["1"]["booked_count"] == 0 and eng.slots.items["2"]["booked_count"] == 1
    assert len(eng.events.of("AppointmentRescheduled")) == 1 and len(eng.events.of("SlotSeatFreed")) == 1
    assert eng.metrics.total("ReschedulesSucceeded") == 1


def test_forced_failure_during_release_patient_holds_exactly_one_appointment():
    """MANDATORY TEST 3 (logic level)."""
    eng, repo, tasks, aid = setup()
    assert saga(tasks, repo, aid, 2, fail_release=True)[0] == "COMPENSATED"
    assert len(live(repo)) == 1 and live(repo)[0]["slot_id"] == 1      # exactly one, back on the original
    assert live(repo)[0]["reschedule_count"] == 0
    assert eng.slots.items["1"]["booked_count"] == 1                    # original seat still held
    assert eng.slots.items["2"]["booked_count"] == 0                    # claimed seat given back
    assert eng.metrics.total("ReschedulesCompensated") == 1
    assert eng.events.of("AppointmentRescheduled") == []


def test_target_full_rejects_and_original_stays_confirmed():
    eng, repo, tasks, aid = setup()
    eng.book(repo, 6, 2, "other-key-0001", "slot:2")                   # capacity 1 -> slot 2 is now full
    kind, out = saga(tasks, repo, aid, 2)
    assert kind == "REJECTED" and out["code"] == "SLOT_FULL" and out["status"] == 409
    assert live(repo)[0]["slot_id"] == 1 or len([a for a in live(repo) if a["patient_id"] == 5]) == 1


def test_attempt_limit_two_then_rejected():
    eng, repo, tasks, aid = setup()
    repo.slots[3] = dict(repo.slots[2], slot_id=3)
    repo.slots[4] = dict(repo.slots[2], slot_id=4)
    assert saga(tasks, repo, aid, 2)[0] == "RESCHEDULED"
    assert saga(tasks, repo, aid, 3)[0] == "RESCHEDULED"
    kind, out = saga(tasks, repo, aid, 4)
    assert kind == "REJECTED" and out["code"] == "RESCHEDULE_LIMIT"


def test_cutoff_six_hours_boundary():
    eng, repo, tasks, aid = setup()
    repo.appointments[aid]["slot_start"] = f.NOW + timedelta(hours=6, seconds=-1)
    assert saga(tasks, repo, aid, 2)[1]["code"] == "RESCHEDULE_TOO_CLOSE"
    repo.appointments[aid]["slot_start"] = f.NOW + timedelta(hours=6)  # exactly 6 h is allowed
    assert saga(tasks, repo, aid, 2)[0] == "RESCHEDULED"


def test_cannot_move_someone_elses_appointment():
    eng, repo, tasks, aid = setup()
    kind, out = saga(tasks, repo, aid, 2, patient=99)
    assert kind == "REJECTED" and out["code"] == "APPOINTMENT_NOT_FOUND"
