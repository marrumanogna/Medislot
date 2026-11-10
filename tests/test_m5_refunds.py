"""Refund tiers (every boundary instant), cancellation, ledger and doctor slot cancellation.

Frozen Rule Set: >48h 100 %, 24-48h 75 %, 6-24h 50 %, <6h 0 %, doctor cancel 100 %.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from m5_fakes import ApiError, FakeRepo, NOW, SLOT_START, appts, make_engine

CFG = dict(appts.DEFAULT_REFUND)
START = datetime(2026, 11, 20, 9, 0, 0)
H, S = timedelta(hours=1), timedelta(seconds=1)


@pytest.mark.parametrize("before, tier, percent", [
    (timedelta(days=30), "T1", 100),
    (48 * H + S, "T1", 100),
    (48 * H, "T2", 75),                      # exactly 48 h is NOT "more than 48 h"
    (48 * H - S, "T2", 75),
    (30 * H, "T2", 75),
    (24 * H + S, "T2", 75),
    (24 * H, "T2", 75),                      # exact lower cutoff stays in the higher tier
    (24 * H - S, "T3", 50),
    (12 * H, "T3", 50),
    (6 * H + S, "T3", 50),
    (6 * H, "T3", 50),                       # exactly 6 h is not "less than 6 h"
    (6 * H - S, "T4", 0),
    (1 * H, "T4", 0),
    (S, "T4", 0),
    (timedelta(0), "T4", 0),
])
def test_refund_tier_boundaries(before, tier, percent):
    assert appts.refund_tier(START, START - before, CFG) == (tier, percent)


def test_windows_and_percentages_are_configurable():
    cfg = {"t1_hours": 72, "t2_hours": 36, "t3_hours": 12,
           "t1_percent": 90, "t2_percent": 60, "t3_percent": 30, "t4_percent": 10}
    assert appts.refund_tier(START, START - (72 * H + S), cfg) == ("T1", 90)
    assert appts.refund_tier(START, START - 72 * H, cfg) == ("T2", 60)
    assert appts.refund_tier(START, START - 36 * H, cfg) == ("T2", 60)
    assert appts.refund_tier(START, START - (36 * H - S), cfg) == ("T3", 30)
    assert appts.refund_tier(START, START - 12 * H, cfg) == ("T3", 30)
    assert appts.refund_tier(START, START - (12 * H - S), cfg) == ("T4", 10)


def test_refund_amount_rounds_half_up_to_paise():
    assert appts.refund_amount("500.00", 75) == Decimal("375.00")
    assert appts.refund_amount("333.33", 75) == Decimal("250.00")   # 249.9975
    assert appts.refund_amount("333.33", 50) == Decimal("166.67")   # 166.665
    assert appts.refund_amount("199.99", 100) == Decimal("199.99")
    assert appts.refund_amount("500.00", 0) == Decimal("0.00")


def booked(eng, repo, patient=5):
    return eng.book(repo, patient, 1, f"cancel-key-{patient:05d}", "slot:1")[1]["appointment"]["appointment_id"]


@pytest.mark.parametrize("hours_before, tier, amount", [
    (60, "T1", "500.00"), (30, "T2", "375.00"), (12, "T3", "250.00"), (3, "T4", "0.00")])
def test_patient_cancellation_writes_ledger_metric_and_events(hours_before, tier, amount):
    eng, repo = make_engine(FakeRepo(capacity=3))
    aid = booked(eng, repo)
    eng.clock = lambda: SLOT_START - timedelta(hours=hours_before)

    status, body = eng.cancel_by_patient(repo, 5, aid)

    assert status == 200 and body["appointment"]["status"] == "CANCELLED_BY_PATIENT"
    assert body["appointment"]["refund"]["tier"] == tier and body["appointment"]["refund"]["amount"] == amount
    assert repo.ledger[aid]["tier"] == tier and repo.ledger[aid]["amount"] == Decimal(amount)
    assert eng.metrics.total("CancellationsByTier", Tier=tier) == 1
    assert eng.metrics.total("RefundAmountTotal") == float(amount)
    assert eng.slots.items["1"]["booked_count"] == 0           # seat freed
    assert eng.events.of("AppointmentCancelled")[0]["tier"] == tier
    assert eng.events.of("SlotSeatFreed")[0]["seats_free"] == 3


def test_cancelling_twice_never_refunds_twice():
    eng, repo = make_engine(FakeRepo(capacity=3))
    aid = booked(eng, repo)
    eng.cancel_by_patient(repo, 5, aid)
    status, body = eng.cancel_by_patient(repo, 5, aid)
    assert status == 200 and body["already_cancelled"] is True
    assert len(repo.ledger) == 1 and len(eng.events.of("AppointmentCancelled")) == 1


def test_cannot_cancel_someone_elses_appointment_or_after_start():
    eng, repo = make_engine(FakeRepo(capacity=3))
    aid = booked(eng, repo)
    with pytest.raises(ApiError) as err:
        eng.cancel_by_patient(repo, 99, aid)
    assert err.value.status == 404
    eng.clock = lambda: SLOT_START + S
    with pytest.raises(ApiError) as err:
        eng.cancel_by_patient(repo, 5, aid)
    assert err.value.code == "TOO_LATE"


def test_doctor_slot_cancellation_refunds_every_patient_in_full_regardless_of_timing():
    eng, repo = make_engine(FakeRepo(capacity=3))
    ids = [booked(eng, repo, p) for p in (1, 2, 3)]
    eng.clock = lambda: SLOT_START - timedelta(hours=1)        # would be T4 (0 %) for a patient

    status, body = eng.cancel_slot_by_doctor(repo, 7, 1, "Doctor unwell")

    assert body["cancelled_appointments"] == 3
    for aid in ids:
        assert repo.ledger[aid]["tier"] == "DOCTOR_CANCELLED"
        assert repo.ledger[aid]["amount"] == Decimal("500.00") and repo.ledger[aid]["percent"] == 100
    assert eng.metrics.total("CancellationsByTier", Tier="DOCTOR_CANCELLED") == 3
    assert eng.metrics.total("RefundAmountTotal") == 1500.0
    assert len(eng.events.of("AppointmentCancelled")) == 3
    assert eng.slots.items["1"]["status"] == "CLOSED"
    eng.clock = lambda: NOW
    with pytest.raises(ApiError):                              # closed slot takes no new bookings
        eng.book(repo, 9, 1, "after-close-00001", "slot:1")


def test_only_the_owning_doctor_can_cancel_a_slot():
    eng, repo = make_engine(FakeRepo(capacity=3))
    with pytest.raises(ApiError) as err:
        eng.cancel_slot_by_doctor(repo, 8, 1)
    assert err.value.status == 404


def test_frozen_patient_limits():
    """One appointment per slot; at most 3 upcoming with the same doctor. Nothing else."""
    cfg = {"max_upcoming_per_doctor": 3}
    slot = {"slot_id": 9, "doctor_id": 7}
    mine = lambda sid, doc: {"slot_id": sid, "doctor_id": doc}
    appts.enforce_limits([], slot, cfg)
    appts.enforce_limits([mine(1, 7), mine(2, 7), mine(3, 8), mine(4, 8), mine(5, 8)], slot, cfg)  # 2 with doc 7
    with pytest.raises(ApiError) as e:
        appts.enforce_limits([mine(9, 7)], slot, cfg)
    assert (e.value.status, e.value.code) == (409, "ALREADY_BOOKED")
    with pytest.raises(ApiError) as e:
        appts.enforce_limits([mine(1, 7), mine(2, 7), mine(3, 7)], slot, cfg)
    assert (e.value.status, e.value.code) == (422, "LIMIT_PER_DOCTOR")


def test_booking_enforces_the_per_doctor_limit(monkeypatch=None):
    eng, repo = make_engine(FakeRepo(capacity=10))
    appts.limit_config = lambda: {"max_upcoming_per_doctor": 1}
    eng.book(repo, 5, 1, "limit-key-000001", "slot:1")
    repo.slot = dict(repo.slot, slot_id=2)
    repo.get_slot = lambda sid: dict(repo.slot)
    with pytest.raises(ApiError) as e:
        eng.book(repo, 5, 2, "limit-key-000002", "slot:2")
    assert e.value.code == "LIMIT_PER_DOCTOR"
