"""Doctor marks a visit COMPLETED or NO_SHOW."""
from datetime import timedelta

import pytest
from m5_fakes import NOW, ApiError, appts


class Repo:
    def __init__(self, status="CONFIRMED", start=NOW - timedelta(minutes=10)):
        self.appt = {"appointment_id": 1, "patient_id": 9, "doctor_id": 3, "slot_id": 4, "status": status,
                     "slot_start": start, "slot_end": start + timedelta(minutes=30), "fee": 500, "consultation_type_id": 1,
                     "reschedule_count": 0}
        self.set_calls = []

    def get_appointment(self, aid, patient_id=None, doctor_id=None, for_update=False):
        return None if doctor_id not in (None, 3) else dict(self.appt)

    def set_outcome(self, appt, outcome):
        self.set_calls.append(outcome)
        self.appt["status"] = outcome

    def commit(self):
        pass


def engine():
    e = appts.Engine.__new__(appts.Engine)
    e.clock = lambda: NOW
    return e


@pytest.mark.parametrize("outcome", ["COMPLETED", "NO_SHOW"])
def test_mark_started_visit(outcome):
    repo = Repo()
    status, out = engine().mark_outcome(repo, 3, 1, outcome)
    assert status == 200 and repo.set_calls == [outcome] and out["appointment"]["status"] == outcome


def test_cannot_mark_before_start():
    with pytest.raises(ApiError) as e:
        engine().mark_outcome(Repo(start=NOW + timedelta(hours=1)), 3, 1, "COMPLETED")
    assert e.value.code == "TOO_EARLY"


def test_other_doctor_gets_404():
    with pytest.raises(ApiError) as e:
        engine().mark_outcome(Repo(), 8, 1, "COMPLETED")
    assert e.value.status == 404


def test_repeat_is_idempotent_and_cancelled_rejected():
    repo = Repo(status="COMPLETED")
    assert engine().mark_outcome(repo, 3, 1, "COMPLETED")[1]["already_marked"] is True
    assert repo.set_calls == []
    with pytest.raises(ApiError):
        engine().mark_outcome(Repo(status="CANCELLED_BY_PATIENT"), 3, 1, "NO_SHOW")
