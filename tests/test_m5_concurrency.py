"""MANDATORY TEST 1: 20+ simultaneous bookings at a capacity-3 slot, exactly 3 succeed."""

import pytest

from m5_fakes import FakeRepo, FakeSlots, make_engine, run_parallel


def book(eng, repo, i, slot_id=1):
    status, body = eng.book(repo, 1000 + i, slot_id, f"concurrency-key-{i:04d}", f"slot:{slot_id}")
    return (status, body["appointment"]["appointment_id"])


@pytest.mark.parametrize("round_", range(3))  # repeat: a race that passes once proves little
def test_24_parallel_bookings_capacity_3_exactly_3_succeed(round_):
    repo = FakeRepo(capacity=3)
    slots = FakeSlots()
    eng, repo = make_engine(repo, slots)

    results = run_parallel(24, lambda i: book(eng, repo, i))

    wins = [r for r in results if r[0] == 201]
    losses = [r for r in results if r[0] == "ERR"]
    assert len(wins) == 3
    assert len(losses) == 21
    assert {r[2] for r in losses} == {"SLOT_FULL"}          # 409 Conflict, not a 5xx or a hang
    assert {r[1] for r in losses} == {409}
    assert len(repo.appointments) == 3                       # RDS agrees with the counter
    assert slots.items["1"]["booked_count"] == 3             # never oversold
    assert len({r[1] for r in wins}) == 3
    # Contention was real and was handled by retrying, not avoided by serialising.
    assert eng.metrics.total("BookingConflictRetries") > 0
    assert len(eng.events.of("AppointmentBooked")) == 3


def test_multi_capacity_health_camp_exactly_capacity_succeeds():
    repo = FakeRepo(capacity=10)
    eng, repo = make_engine(repo)
    results = run_parallel(30, lambda i: book(eng, repo, i))
    assert len([r for r in results if r[0] == 201]) == 10
    assert {r[2] for r in results if r[0] == "ERR"} == {"SLOT_FULL"}
    assert len(repo.appointments) == 10


def test_single_seat_slot_has_exactly_one_winner():
    eng, repo = make_engine(FakeRepo(capacity=1))
    results = run_parallel(20, lambda i: book(eng, repo, i))
    assert len([r for r in results if r[0] == 201]) == 1
    assert len(repo.appointments) == 1


def test_seat_is_released_if_rds_insert_fails():
    eng, repo = make_engine(FakeRepo(capacity=3, fail_insert=True))
    with pytest.raises(RuntimeError):
        eng.book(repo, 1, 1, "compensation-key-1", "slot:1")
    assert eng.slots.items["1"]["booked_count"] == 0         # compensated
    assert "1:compensation-key-1" not in eng.idem.items      # key free for a retry
