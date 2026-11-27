# Review guide

How to use it: rehearse the demo once with a stopwatch, keep the Q&A section open, and keep the code map handy.

## A. Thirty-minute demo script
| Min | Show | Say |
|---|---|---|
| 0-3 | `docs/architecture.md` diagram | Patient and doctor portals call API Gateway; Lambdas use RDS as system of record and DynamoDB for fast state; Step Functions runs the reschedule saga; EventBridge → SQS → Lambda → SNS delivers notifications |
| 3-7 | GitHub Actions (green run), CloudFormation stacks list | Everything is infrastructure as code and deployed by the pipeline with no manual steps. Secrets (e-mails) are GitHub secrets. Stacks carry cost tags |
| 7-13 | **Doctor portal:** availability → edit hours, then Schedule; Blackout dates; Appointments; History; Refunds | Editing hours rebuilds unbooked slots but never touches booked ones. A blackout date is refused while patients are booked; the doctor cancels those slots on Appointments (full refund), then the blackout is accepted. After the start time the doctor marks Patient checked or No-show |
| 13-18 | **Patient portal:** search a doctor, book, reschedule, cancel (see refund tier), History, Refunds | Booking is idempotent and cannot double book (DynamoDB conditional update) |
| 18-22 | **Saga failure (Mandatory Test 3):** set SSM `/medislot/dev/testing/allow-fault-injection` to `1`, send `PATCH /appointments/{id}` with header `X-Medislot-Fail-Release: true`, open the Step Functions execution | The release step fails after the claim succeeded, the compensation branch frees the claimed seat, the patient still holds exactly one appointment |
| 22-25 | **Waitlist (Mandatory Test 4):** fill a slot, three patients join the waitlist, cancel the booking | One live offer only, seat hidden from normal booking, offer accept or expiry passes it on. Show the HoldOffers table |
| 25-27 | **Notifications:** show the e-mail; invoke `medislot-dev-scheduled-jobs` twice with `{"action":"reminders"}` | Dispatch log makes delivery exactly-once: the second run sends nothing new |
| 27-30 | **Operations:** CloudWatch dashboard `medislot-dev-operations`, alarm list, X-Ray service map, Budgets, drift report, DR drill row | Metrics, six alarm categories to SNS, traces end to end including the compensation branch, budget by cost tag, all stacks IN_SYNC, measured restore time |

## B. Code map
| Question | Open |
|---|---|
| How is double booking prevented? | `lambdas/appointments/lambda_function.py` class `SlotStore`, method that updates `booked_count < capacity`; `docs/concurrency-design-note.md` |
| Where is the saga? | `cloudformation/auth-stack.yaml` resource `RescheduleStateMachine`; steps call `RescheduleTasks` in the appointments Lambda |
| Where are refunds computed? | `appointments` Lambda, refund tiers read from SSM `/medislot/<env>/refund/*` |
| Doctor marks checked / no-show | `Engine.mark_outcome` in the appointments Lambda; UI `static/js/doctor.js` `pages.appointments` |
| Blackout rule | `lambdas/blackout_dates/lambda_function.py` `blocked_by_bookings` |
| Availability rebuild | `lambdas/doctor_slot_management/lambda_function.py` `clear_unbooked_future_slots`, `lambdas/slot_materialiser` (`fill_gaps`) |
| Waitlist and offers | `lambdas/waitlist/lambda_function.py` |
| Exactly-once notifications and e-mail text | `lambdas/notifications/lambda_function.py` (`compose`, dispatch log write before publish) |
| Reminders, no-show | `lambdas/scheduled_jobs/lambda_function.py` |
| Logging | `logged` decorator in `lambdas/common.py`: START / END lines with request id, role, status, duration |
| Alarms, dashboard, budget | `cloudformation/observability-stack.yaml` |
| Least privilege | `docs/iam-audit.md`, `cloudformation/iam-stack.yaml` |

## C. Likely questions
1. **Why RDS and DynamoDB?** RDS for relational data and reporting (users, appointments, refunds). DynamoDB for contention-heavy state (seat counters, offers, dispatch log) where a single conditional write gives atomic, low-latency guarantees, and TTL expires old rows.
2. **What if the release step fails midway?** Compensation frees the target seat; execution history shows the branch; `ReschedulesCompensated` metric and alarm fire.
3. **How do you guarantee only one waitlist offer?** A conditional write: hold only while `booked_count < capacity`; the hold can succeed once per freed seat.
4. **How do you avoid duplicate e-mails?** The dispatch-log row is written with a condition before publishing; a retry finds it and skips.
5. **What happens to a message that keeps failing?** It moves to the queue's DLQ; the DLQ-depth alarm notifies; it can be redriven.
6. **Is anything hard-coded?** Limits, tiers, hold time and reminder hours are SSM parameters; e-mails are secrets; resource names derive from the environment parameter.
7. **How is it secured?** JWT via a Lambda authorizer, role checks in every Lambda, per-function IAM roles, no wildcard actions, KMS decrypt limited to SSM, private RDS.
8. **What is traced and monitored?** Active X-Ray tracing on every Lambda, the state machine and the API stage; 12 alarms in six categories; structured START/END logs.
9. **Disaster recovery?** RDS automated backups with point-in-time restore, DynamoDB PITR, and the pipeline rebuilds everything; see `docs/dr-drill.md` for the measured restore time.
10. **What would you improve?** Per-user notification subscriptions (today one address per topic), a NAT-free design with fewer interface endpoints to cut cost, an Elastic IP or load balancer so the web URL never changes.

## D. Before the review
- [ ] Pipeline green on `master`, all feature branches merged, no open pull requests
- [ ] README has the real web and API URLs
- [ ] SNS subscriptions confirmed (patient, doctor, alarm e-mails)
- [ ] `python scripts/dr_drill.py dev` run and the row pasted into `docs/dr-drill.md`
- [ ] Billing: cost allocation tags `Project` and `Environment` activated
- [ ] Demo data ready: one doctor with availability, two patients, one full slot, one waitlist of three
