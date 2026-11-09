# MediSlot

Doctor appointment platform on AWS (API Gateway, Lambda, RDS MySQL, DynamoDB, S3, EC2).

* `docs/milestone-4.md` — patient profiles, doctor search, slot availability, index design, runbook
* `dashboard/FRONTEND.md` — dashboard notes
* `cloudformation/` — stacks; `lambdas/` — one folder per Lambda; `scehma/schema.sql` — clean-install schema
* `tests/` — unit tests (`pytest -q --ignore=tests/integration`); `tests/integration/` — run against a deployed API with `MEDISLOT_API_URL`

## Milestone 5

Booking engine, optimistic concurrency, idempotency, cancellation and refunds: see [docs/milestone-5.md](docs/milestone-5.md) and [docs/concurrency-design-note.md](docs/concurrency-design-note.md).
