# Milestone 5: Booking engine, concurrency, cancellation and refunds

Design reasoning: [`concurrency-design-note.md`](concurrency-design-note.md).

## What was added

| Deliverable | Where |
|---|---|
| Booking endpoint, validation, capacity arithmetic | `lambdas/appointments/lambda_function.py` (`Engine.book`) |
| Optimistic concurrency, conditional writes, `version` | `SlotStore` (DynamoDB `MediSlot-<env>-Slots`) |
| Bounded retry with backoff, retry metric | `Engine.reserve_seat`, metric `BookingConflictRetries` |
| 409 when full | `SLOT_FULL` (and `SLOT_BUSY` if the retry bound is exhausted) |
| Multi-capacity Health Camp Sessions | doctor sets 2 to 10 seats (`doctor_consultation_fees.capacity`), default is the catalogue's 10 |
| Idempotency store | `IdempotencyStore` (DynamoDB `MediSlot-<env>-Idempotency`) |
| Appointments in RDS | table `appointments` (migration `m5_001_booking_engine`, also in `scehma/schema.sql`) |
| `AppointmentBooked` / `AppointmentCancelled` / `SlotSeatFreed` | EventBridge default bus, source `medislot.appointments` |
| List and detail, scoped to the caller | `GET /appointments`, `GET /appointments/{id}`, `GET /doctor/appointments` |
| Cancellation with refund tiers from SSM | `DELETE /appointments/{id}`, `refund_tier()` |
| Doctor slot cancellation, full refunds | `POST /doctor/slots/{slot_id}/cancel` |
| Refund ledger and patient endpoint | table `refund_ledger`, `GET /patient/refunds` |
| Business metrics (namespace `medislot`) | `BookingConflictRetries`, `CancellationsByTier` (`Tier` = `T1`..`T4`, `DOCTOR_CANCELLED`), `AppointmentsBooked`, `BookingsRejectedFull`, `RefundAmountTotal` |
| Tests | unit: `tests/test_m5_*.py`. Deployed-API: `tests/integration/test_m5_booking.py` (mandatory tests 1 and 2 against real infrastructure) |

## API

| Method and path | Role | Notes |
|---|---|---|
| `POST /appointments` | patient | Body `{"slot_id": 12}`. **Idempotency-Key** header (or `idempotency_key` in the body), 8 to 128 chars. 201 created, 200 replay |
| `GET /appointments` | patient or doctor | The caller's appointments only |
| `GET /appointments/{id}` | patient or doctor | 404 for anyone else's appointment |
| `DELETE /appointments/{id}` | patient | Cancels and returns the computed refund (tier, percent, amount). Repeat is a harmless 200 |
| `GET /patient/refunds` | patient | Ledger and `total_refunded` |
| `GET /doctor/appointments` | doctor | Includes `patient_name` |
| `POST /doctor/slots/{slot_id}/cancel` | doctor | Optional `{"reason": "..."}`. 100 % refund for everyone booked |

Error codes: `SLOT_NOT_FOUND` 404, `SLOT_IN_PAST` 422, `SLOT_UNAVAILABLE` 409, `SLOT_FULL` 409, `SLOT_BUSY` 409,
`ALREADY_BOOKED` 409, `LIMIT_PER_DOCTOR` 422, `TYPE_NOT_OFFERED` 409,
`PROFILE_REQUIRED` 409, `IDEMPOTENCY_KEY_REQUIRED` 400, `IDEMPOTENCY_KEY_REUSED` 422, `REQUEST_IN_PROGRESS` 409,
`TOO_LATE` 409.

## Refund tiers (Frozen Rule Set, SSM-configured)

| Cancelled | Tier | Refund |
|---|---|---|
| More than 48 h before the start | `T1` | 100 % |
| 24 h up to and including 48 h before | `T2` | 75 % |
| 6 h up to just under 24 h before | `T3` | 50 % |
| Less than 6 h before | `T4` | 0 % |
| Doctor cancelled the slot (any timing) | `DOCTOR_CANCELLED` | 100 % |
| Patient marked NO_SHOW (Milestone 6 sweeper) | `NO_SHOW` | 0 % |

Boundary instants: exactly 48 h is `T2` (the brief says "more than 48"), exactly 24 h is `T2`, exactly 6 h is `T3`;
one second less than 6 h is `T4`. Amounts round half-up to 0.01.

SSM parameters (created by `data-stack.yaml`, cached 60 s in the Lambda): `/medislot/<env>/refund/t1-hours` (48),
`t2-hours` (24), `t3-hours` (6), `t1-percent` (100), `t2-percent` (75), `t3-percent` (50), `t4-percent` (0), and
`booking/max-upcoming-per-doctor` (3).

Patient limits are exactly the frozen ones: one appointment per slot, at most 3 upcoming with the same doctor, no past slots.
Appointment states: `CONFIRMED`, `COMPLETED`, `CANCELLED_BY_PATIENT`, `CANCELLED_BY_DOCTOR`, `NO_SHOW`.

## Infrastructure changes

* `data-stack.yaml`: the five SSM parameters above.
* `network-stack.yaml`: an `events` interface endpoint (the Lambdas have no NAT, so `PutEvents` needs it).
* `auth-stack.yaml`: `AppointmentsFunction` + least-privilege `AppointmentsRole` (DynamoDB on the two tables, `events:PutEvents`,
  SSM read), API routes, a new deployment `ApiDeploymentM5`.
* `deploy.yaml`: packages the `appointments` Lambda. Migration `m5_001_booking_engine` runs in the existing migrate step.
* Discovery (`GET /doctors/{id}/slots`) now reports `seats_total = capacity` and `seats_free = capacity - active bookings`.

## Dashboard

Patient: **Book this slot** in the doctor dialog (one Idempotency-Key per slot selection), **Cancel** on upcoming appointments
(shows the refund), a **Refunds** section. Doctor: **Cancel slot** on published slots in the Schedule page.

## Running the mandatory tests

```
pip install -r requirements-dev.txt
python -m pytest -q tests/test_m5_concurrency.py tests/test_m5_idempotency.py tests/test_m5_refunds.py
```

## Checkpoint walkthrough for a reviewer

1. Run the concurrency tests (unit: above. Live: `MEDISLOT_API_URL=<stage url> python -m pytest tests/integration/test_m5_booking.py`), then fire your own parallel `POST /appointments` at a capacity-3 slot using 20+
   different patient tokens (one patient can hold a slot only once).
2. CloudWatch, namespace `medislot`, metric `BookingConflictRetries`: non-zero during the burst.
3. `SELECT COUNT(*) FROM appointments WHERE slot_id = ? AND status = 'CONFIRMED'` equals the capacity.
4. Boundaries: set the `refund/*` parameters, book a slot that starts at a known instant, cancel exactly at the cutoff.

## Known limits

* A Lambda crash between the DynamoDB reservation and the RDS commit can leak a seat (no reconciliation job yet).
* Events are best-effort (no outbox). No payment gateway: the ledger records the refund owed, it does not move money.
* Waitlist offers (`SlotSeatFreed` consumers) are a later milestone.
* No foreign keys on `appointments` so that deleting a profile cannot fail or erase financial history.
