# Milestone 6: reschedule saga, waitlist, notifications

## New / changed endpoints (all behind the JWT authorizer, PATIENT role)
| Endpoint | Notes |
|---|---|
| `PATCH /appointments/{appointment_id}` `{"slot_id": N}` | Starts the Step Functions saga and waits for the outcome. 200 done, 409 `RESCHEDULE_COMPENSATED`, 409/422 rejected (`SLOT_FULL`, `RESCHEDULE_LIMIT`, `RESCHEDULE_TOO_CLOSE`...), 202 if still running |
| `POST /slots/{slot_id}/waitlist` | Only when the slot is full (409 `SLOT_NOT_FULL`), max 5 lists (422 `WAITLIST_LIMIT`) |
| `DELETE /slots/{slot_id}/waitlist` | Leaving while holding an offer releases the seat to the next patient |
| `GET /patient/waitlists` | Position, and the live offer (`offer_id`, `expires_at`) if any |
| `POST /waitlist-offers/{offer_id}/accept` | 201 creates the appointment, 410 expired, replay-safe |

## Reschedule saga (`medislot-<env>-reschedule`)
`Claim` (validate + conditional seat reservation on target) -> `Commit` (RDS: the ONE appointment row moves, `reschedule_count + 1`)
-> `ReleaseOriginal` (seat decrement is the last side effect, so retries cannot double-release) -> `Finalize` (publishes
`AppointmentRescheduled` and `SlotSeatFreed`). A failure in `Commit` or `ReleaseOriginal` goes to `Compensate`: row moved back,
target seat released, `ReschedulesCompensated` metric. Because the appointment is a single row, the patient holds exactly one
appointment at every instant. Rules from SSM: `reschedule/max-count` (2), `reschedule/cutoff-hours` (6, exactly 6 h allowed).

Fault injection for Mandatory Test 3: header `X-Medislot-Fail-Release: true`, honoured only when SSM
`/medislot/<env>/testing/allow-fault-injection` = `1` (created as `0` in prod).

## Waitlist and hold offers
* Slot record: `booked_count` = confirmed + held seats, `held_count` = held subset. The existing booking condition
  `booked_count < capacity` therefore hides a held seat with no change to the booking engine.
* Offer = conditional entry claim `WAITING -> OFFERED` + conditional seat hold. The hold succeeds once per freed seat, so two
  live offers for one seat cannot exist.
* `SlotSeatFreed` -> EventBridge -> `waitlist` SQS queue (+DLQ) -> `medislot-<env>-waitlist` Lambda.
* Sweeper (`rate(1 minute)`): `LIVE -> EXPIRED` conditional transition, seat freed, patient leaves the queue, next patient offered.
* Known limit: between a cancellation freeing a seat and the offer being issued (milliseconds-seconds) a direct booker can take it;
  the offer then simply is not issued and the waitlist keeps its order. The seat is never double-assigned.
* DynamoDB TTL on `HoldOffers.expires_at` is only cleanup; the sweeper (every minute) drives all state changes.

## Notifications
EventBridge rule -> `notifications` SQS queue (+DLQ) -> `medislot-<env>-notifications` Lambda (outside the VPC) -> SNS topics
`patient-notifications` / `doctor-notifications`. Exactly-once: conditional put in `NotificationLog` (`eventId#recipient`) before
publishing; on SNS failure the row is removed and SQS redelivers. Matrix: booked/rescheduled -> patient+doctor, cancelled by
patient -> patient+doctor, cancelled by doctor -> patient, offer issued/expired -> patient, reminders -> patient.

## Scheduled jobs (all idempotent)
| Job | Schedule | Lambda |
|---|---|---|
| Offer expiry sweeper | every minute | `waitlist` `{"action":"expire_offers"}` |
| Reminder dispatcher (24 h, 2 h) | every 5 min | `scheduled-jobs` `{"action":"reminders"}`, dispatch log key `appointmentId#REMINDER_<h>H` |
| No-show sweeper (30 min after slot end) | every 15 min | `scheduled-jobs` `{"action":"no_show"}` |

## SSM parameters added (data stack)
`waitlist/max-per-patient`=5, `waitlist/hold-minutes`=15, `reschedule/max-count`=2, `reschedule/cutoff-hours`=6,
`noshow/grace-minutes`=30, `reminder/first-hours`=24, `reminder/second-hours`=2, `reminder/catchup-minutes`=30,
`testing/allow-fault-injection`.

## Infrastructure notes
* Network stack: new `states` interface VPC endpoint (appointments Lambda starts the saga from inside the VPC).
* Deploy order is unchanged: network -> data -> auth. DB migration `m6_001_reschedule_count` runs after deploy.
* Tests: `tests/test_m6_*.py` (unit), `tests/integration/test_m6_workflows.py` (Mandatory Tests 3 and 4).

* Doctor slot cancellation (`SlotSeatFreed` with `closed=true`) ends live offers and clears that slot's queue; waitlists for slots that already started are ignored by the 5-list cap and the listing.


## Doctor outcome and blackout rules (added 25 Nov)
* `POST /doctor/appointments/{appointment_id}/outcome` `{"outcome": "COMPLETED" | "NO_SHOW"}` (DOCTOR). Allowed once the slot has started; repeat calls are replay-safe; NO_SHOW writes a 0% refund row. Other doctors get 404.
* `POST/PUT /doctor/blackout-dates` returns 409 `BLACKOUT_HAS_BOOKINGS` plus `booked_slots` while confirmed patients exist on that IST day. The doctor cancels those slots (`/doctor/slots/{id}/cancel`, full refund, `AppointmentCancelled` notifications), then the blackout is accepted. `GET /doctor/blackout-dates?check_date=YYYY-MM-DD` lists those slots for the UI.
* Dashboard stack: new `SshCidr` parameter (default = EC2 Instance Connect range for us-east-1) opens port 22; empty value removes the rule (Session Manager only).
