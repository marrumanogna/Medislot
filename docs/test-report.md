# Test report

Run everything: `pip install -r requirements-dev.txt` then `pytest -q --ignore=tests/integration`.
CI runs this on every push (job `ci`), followed by syntax check, CloudFormation validation, secret scan and IaC scan.

**Result at submission: 196 unit tests, all passing.** Integration tests (`tests/integration`) run against the deployed API at the end of the pipeline.

| Area | File | Tests |
|---|---|---|
| Auth | test_auth_basics, test_authorizer | 7 |
| Doctor profile, consultation types, availability, blackouts | test_doctor_profile, test_consultation_types, test_availability_patterns, test_blackout_dates | 28 |
| Slot generation and schedule | test_slot_materialiser, test_doctor_schedule | 9 |
| Patient profile and doctor search | test_patient_profile, test_doctor_discovery | 55 |
| Database migrations | test_db_migrate | 13 |
| M5 booking: concurrency, idempotency, refunds | test_m5_concurrency, test_m5_idempotency, test_m5_refunds | 39 |
| **M6 Mandatory Test 3**: reschedule saga, forced failure during release | test_m6_reschedule | 6 |
| **M6 Mandatory Test 4**: waitlist exclusivity (one seat, three waiting, one live offer) | test_m6_waitlist | 15 |
| M6 notifications: exactly-once dispatch, reminders sent twice give no duplicate | test_m6_notifications | 5 |
| M7 doctor outcome (checked / no-show), slot rebuild keeps booked slots, tolerant `is_active`, e-mail format | test_m7_outcome, test_m7_regenerate | 8 |
| M7 hardening guard rails: dashboard, six alarm categories, budget tag filter, X-Ray on every Lambda, no wildcard IAM actions, logging | test_m7_templates | 13 |

Manual checks to show at the review: forced saga failure in the Step Functions execution history, three-patient waitlist with a single live offer, reminder dispatcher triggered twice with one e-mail, X-Ray service map for a booking and a reschedule.
