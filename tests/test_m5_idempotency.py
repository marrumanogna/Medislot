"""MANDATORY TEST 2: the same Idempotency-Key replayed creates exactly one appointment."""

import pytest

from m5_fakes import ApiError, FakeRepo, make_engine, run_parallel


def test_replayed_key_returns_original_appointment_and_creates_one():
    eng, repo = make_engine(FakeRepo(capacity=3))
    s1, b1 = eng.book(repo, 5, 1, "replay-key-0001", "slot:1")
    s2, b2 = eng.book(repo, 5, 1, "replay-key-0001", "slot:1")
    s3, b3 = eng.book(repo, 5, 1, "replay-key-0001", "slot:1")

    assert (s1, s2, s3) == (201, 200, 200)
    assert b1["idempotent_replay"] is False and b2["idempotent_replay"] is True
    assert b1["appointment"]["appointment_id"] == b2["appointment"]["appointment_id"] == b3["appointment"]["appointment_id"]
    assert len(repo.appointments) == 1
    assert eng.slots.items["1"]["booked_count"] == 1          # only one seat consumed
    assert len(eng.events.of("AppointmentBooked")) == 1       # and one event


def test_parallel_replays_of_one_key_create_exactly_one_appointment():
    eng, repo = make_engine(FakeRepo(capacity=3))

    def call(i):
        status, body = eng.book(repo, 5, 1, "parallel-replay-01", "slot:1")
        return (status, body["appointment"]["appointment_id"])

    results = run_parallel(10, call)
    ok = [r for r in results if r[0] in (200, 201)]
    assert len(repo.appointments) == 1
    assert len({r[1] for r in ok}) == 1                       # everyone who got an answer got the same appointment
    assert [r[0] for r in ok].count(201) == 1                 # exactly one real creation
    assert {r[2] for r in results if r[0] == "ERR"} <= {"REQUEST_IN_PROGRESS"}


def test_same_key_with_a_different_request_is_rejected():
    eng, repo = make_engine(FakeRepo(capacity=3))
    eng.book(repo, 5, 1, "reuse-key-00001", "slot:1")
    with pytest.raises(ApiError) as err:
        eng.book(repo, 5, 1, "reuse-key-00001", "slot:2")
    assert err.value.status == 422 and err.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_keys_are_scoped_per_patient():
    eng, repo = make_engine(FakeRepo(capacity=3))
    eng.book(repo, 5, 1, "shared-key-00001", "slot:1")
    status, _ = eng.book(repo, 6, 1, "shared-key-00001", "slot:1")
    assert status == 201 and len(repo.appointments) == 2


def test_failed_booking_does_not_poison_the_key():
    eng, repo = make_engine(FakeRepo(capacity=1))
    eng.book(repo, 5, 1, "first-key-000001", "slot:1")
    with pytest.raises(ApiError) as err:
        eng.book(repo, 6, 1, "retry-key-000001", "slot:1")
    assert err.value.status == 409 and err.value.code == "SLOT_FULL"
    assert "6:retry-key-000001" not in eng.idem.items


def test_rds_fallback_when_idempotency_record_is_lost():
    eng, repo = make_engine(FakeRepo(capacity=3))
    eng.book(repo, 5, 1, "lost-record-0001", "slot:1")
    eng.idem.items.clear()                                    # DynamoDB record expired or lost
    status, body = eng.book(repo, 5, 1, "lost-record-0001", "slot:1")
    assert status == 200 and body["idempotent_replay"] is True
    assert len(repo.appointments) == 1 and eng.slots.items["1"]["booked_count"] == 1
