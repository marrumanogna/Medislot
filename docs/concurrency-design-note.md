# Concurrency Design Note (Milestone 5)

**Question this note answers:** how does MediSlot guarantee that a slot with *N* seats is never sold more than *N* times,
while many patients book it at once, and how can a reviewer tell that contention is real and handled rather than hidden?

## 1. Where the truth lives

| Concern | Store | Why |
|---|---|---|
| Seat counter, the thing that is contended | DynamoDB `MediSlot-<env>-Slots`, one item per slot | Single-item conditional writes are atomic and need no lock |
| Appointments, refund ledger | RDS MySQL | System of record, relational queries, unique constraints |
| Replay protection | DynamoDB `MediSlot-<env>-Idempotency` (TTL 24 h) | Cheap conditional put per key |

The slot item is created lazily on the first booking attempt from the RDS slot row:
`{slot_id, doctor_id, start_at, capacity, booked_count, version, status}`. `capacity` is 1 for normal consultation types and
set by the doctor (2 to 10) for Health Camp Sessions; the effective value is read when the record is first created.

## 2. The optimistic write

```
read item                                   (ConsistentRead)
if status != OPEN        -> 409 SLOT_UNAVAILABLE
if booked_count >= cap   -> 409 SLOT_FULL
UpdateItem  SET booked_count = booked_count + 1, version = version + 1
  ConditionExpression  version = :seen  AND  booked_count < capacity  AND  status = OPEN
on ConditionalCheckFailed -> someone else changed the record first: retry
```

There is **no lock and no queue**. The condition is evaluated by DynamoDB atomically with the write, so two writers can never
both succeed from the same `version`. `booked_count < capacity` is inside the same condition, so even a writer holding a
stale `version` cannot push the count past capacity. Capacity is therefore enforced by the datastore, not by application code.

## 3. Bounded retry with backoff

* On a lost race the Lambda re-reads and retries after **full-jitter exponential backoff**:
  `sleep(uniform(0, min(0.25 s, 0.02 s * 2^attempt)))`.
* **Bound:** `max(6, capacity + 2)` attempts. Each failed attempt means another booking succeeded, so one request can lose
  at most `capacity` times before it observes a full slot (`SLOT_FULL`). The bound therefore never turns a genuinely
  available seat into a failure for capacity-3 slots, and it still terminates for large capacities.
* If the bound is ever exhausted the caller gets **409 `SLOT_BUSY`** and may retry with the same Idempotency-Key.
* **Metric:** every retry is counted. `BookingConflictRetries` (namespace `medislot`, dimension `Environment`) is
  written in CloudWatch Embedded Metric Format, i.e. one log line, no network call (the Lambdas have no NAT).

## 4. Why this is not accidental serialisation

* No reserved concurrency of 1, no table lock, no SQL `GET_LOCK`, no `SELECT ... FOR UPDATE` on the seat counter.
* Requests genuinely overlap: the unit test releases 24 threads on a barrier and widens the read-to-write window, and
  asserts `BookingConflictRetries > 0` as well as "exactly 3 succeed". A serialised system would report zero retries.
* In the deployed system the same evidence is the `BookingConflictRetries` graph: during a parallel burst it is non-zero,
  while the booked count still never exceeds capacity.

## 5. Order of side effects and failure handling

1. Idempotency key claimed (`IN_PROGRESS`).
2. Validation against RDS: slot exists, not in the past, published, doctor offers the type, one seat per patient per slot, at most 3 upcoming with the same doctor.
3. **Reserve the seat** (DynamoDB OCC loop).
4. **Insert the appointment** in RDS and commit.
5. Store the response on the idempotency record (`COMPLETED`), publish `AppointmentBooked`.

| Failure | Handling |
|---|---|
| RDS insert/commit fails after the seat was taken | Seat is released (compensation), idempotency key is abandoned so the client can retry |
| Lambda dies between step 3 and 4 | One seat can leak. Visible as `booked_count` > rows in `appointments`. A reconciliation job is the next step (not in scope) |
| Lambda dies between step 4 and 5 | Replay finds the appointment through `UNIQUE (patient_id, idempotency_key)` in RDS and returns it, so it is never booked twice |
| EventBridge unavailable | Logged and swallowed: the booking is already committed. An outbox is the production answer |
| Seat release after cancel fails 8 times | Metric `SeatReleaseFailures`, error log. The refund is already committed |

Cancellation commits the status change **and** the refund ledger row in one RDS transaction first (money is the priority),
then frees the seat. `refund_ledger.appointment_id` is `UNIQUE`, so a refund can never be paid twice even if the cancel
request is replayed or raced.

## 6. Idempotency

* Key scope is `<user id>:<Idempotency-Key>`, so keys cannot collide across patients.
* The stored record holds a hash of the request. Same key, different slot gives **422 `IDEMPOTENCY_KEY_REUSED`**.
* Replay of a completed key returns the **original appointment** (HTTP 200, `idempotent_replay: true`) and consumes no seat.
* A concurrent replay while the first request runs gets **409 `REQUEST_IN_PROGRESS`**; an `IN_PROGRESS` record older than
  60 s is treated as abandoned.
* Second line of defence in RDS: `UNIQUE (patient_id, idempotency_key)` and `UNIQUE (patient_id, active_slot_id)`
  (`active_slot_id` is NULL once cancelled, so a patient can rebook after cancelling but never hold a slot twice).

## 7. Doctor cancels a slot

All booked appointments are cancelled and refunded 100 % in one RDS transaction, the RDS slot becomes `BLOCKED`, then the
DynamoDB item is set to `CLOSED` with `version + 1`. Any reservation in flight holds an old version and
`status = OPEN` fails in its condition, so it loses cleanly.

## 8. Evidence

* `tests/test_m5_concurrency.py`: 24 parallel bookings on a capacity-3 slot, exactly 3 succeed, 21 get 409, RDS rows = 3,
  counter = 3, retries > 0; repeated 3 times. Also capacity 10 with 30 callers, capacity 1 with 20 callers, compensation.
* `tests/test_m5_idempotency.py`: sequential and parallel replays, key reuse, per-patient scoping, lost record fallback.
* `tests/test_m5_refunds.py`: every refund boundary instant (exact cutoff, one second either side), ledger, metrics, events.
