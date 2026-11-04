# Milestone 4 — Patient domain: profiles, search and discovery

Everything a patient needs before booking: manage a profile, find doctors, and
see free slots. Booking itself is Milestone 5.

| Deliverable | Where |
|---|---|
| Patient profile create / read / update / delete | `lambdas/patient_profile`, routes under `/patient/profile` |
| Doctor search: name, hospital, specialisation, city + pagination | `lambdas/doctor_discovery`, `GET /doctors` |
| RDS indexes, documented and justified | [Indexes](#indexes), `lambdas/db_migrate`, `scehma/schema.sql` |
| Public doctor profile with consultation types and fees | `GET /doctors/{doctor_id}` |
| Free seats per slot for a date range | `GET /doctors/{doctor_id}/slots?from=&to=` |
| Patient data isolation | [Isolation](#patient-data-isolation) |
| Patient dashboard on EC2 | `dashboard/` (profile editor, search, slot browsing) |
| Integration tests in CI against the deployed stack | `tests/integration/`, `.github/workflows/deploy.yaml` |

## API

Patient endpoints need `Authorization: Bearer <accessToken>` from a **patient** login.
The doctor endpoints are public (no token), so they never expose email, licence
number, licence document or verification status.

### Patient profile (camelCase, same field names as patient registration)

| Method | Path | Behaviour |
|---|---|---|
| GET | `/patient/profile` | Own profile. `404` if none (create with POST). |
| POST | `/patient/profile` | Create. `fullName` required. `409` if one exists. |
| PUT | `/patient/profile` | Replace: omitted optional fields are cleared; omitted `fullName` is kept. |
| PATCH | `/patient/profile` | Change only the fields sent (at least one). |
| DELETE | `/patient/profile` | Delete the profile. The login account stays; POST creates a new profile. |
| GET | `/patient/profile/{patient_id}` | `200` only if it is your own id, otherwise `403`. |

Fields: `fullName`, `dateOfBirth` (`YYYY-MM-DD`, not in the future), `gender`, `phone`
and `emergencyContact` (7–20 chars: digits, spaces, `+`, `-`, brackets), `address`, `city`.
Responses add `patientId`, `userId`, `email`, `createdAt`, `updatedAt`.
Unknown fields (including `userId` / `patientId`) are rejected with `400`.

### Doctor discovery (public, snake_case like the doctor APIs)

`GET /doctors` — query parameters, all optional, combined with AND:

| Parameter | Match |
|---|---|
| `specialisation` (also `specialization`) | exact, case-insensitive |
| `city` | case-insensitive **prefix** (`hyd` → Hyderabad) |
| `hospital` | case-insensitive **prefix** |
| `name` | case-insensitive prefix of the full name **or** of the surname (`sharma` finds "Anil Sharma") |
| `page` (default 1), `page_size` (default 10, max 50) | pagination |

Result: `{items, page, page_size, total, total_pages, has_next, filters}`. Each item has
`doctor_id, full_name, specialization, qualifications, experience_years, hospital_name,
hospital_city, languages_spoken, fee_from, fee_to, next_available`.
Ordered by name then id, so pages are stable. Invalid input → `400`.

`GET /doctors/filters` — the specialisations and cities currently in use (powers the dashboard
dropdown; specialisation is typed freely at registration, so a fixed list would miss some).

`GET /doctors/{doctor_id}` — public profile: name, specialisation, qualifications, experience,
hospital name/address/city, languages, bio, photo (short-lived link), and
`consultation_types: [{consultation_type_id, name, duration_minutes, fee}]` plus `fee_from`.
`404` for an unknown or inactive doctor.

`GET /doctors/{doctor_id}/slots?from=YYYY-MM-DD&to=YYYY-MM-DD[&consultation_type_id=]` —
dates are inclusive and in IST; default is the next 7 days, maximum 31 days. Returns
`items` (each with `slot_id, consultation_type, duration_minutes, fee, slot_start, slot_end,
seats_total, seats_free, status`), a per-day `days` summary and a `summary`.
Slots that already started, DRAFT/BLOCKED slots and blackout days are excluded.

**Seats.** A slot holds exactly one patient, so `seats_total` is 1 and `seats_free` is 1 for an
`AVAILABLE` slot and 0 for a `BOOKED` one (shown as "Full"). If Milestone 5 introduces
multi-seat slots, this is the one place to change.

## Indexes

All four search filters were designed to be either an equality or a **prefix** match
(`LIKE 'abc%'`, never `'%abc%'`), because a B-tree can range-scan a constant prefix but cannot
use a leading wildcard. Wildcards typed by users (`%`, `_`) are escaped so they stay literal.

| Query pattern | Index used | Why |
|---|---|---|
| specialisation | `idx_doctor_spec_city (specialization, hospital_city)` | Left-most column; the common first filter. |
| specialisation + city | same index | Equality on the first column, prefix range on the second: both columns seek. |
| city only | `idx_doctor_city (hospital_city)` | City is not the leading column of the composite, so it needs its own index. |
| hospital | `idx_doctor_hospital_name (hospital_name)` | Prefix range scan. |
| name | `idx_doctor_full_name (full_name)` and `idx_doctor_surname (name_last_word)` | The `OR` of two prefix predicates on the same table lets MySQL merge two index ranges. |
| doctor → user check | `users` primary key | `is_active` / role check is a PK lookup. |
| slot availability | existing `idx_doctor_slot_status (doctor_id, status, slot_start)` | `doctor_id =`, `status IN (…)`, `slot_start` range: no new index needed. |
| fees / next slot (per page) | existing fee unique key, `idx_doctor_slot_status` | One batched `IN (…)` query per page, not one per doctor. |

Two schema additions make this possible (migration `m4_001_doctor_search`):

* `doctor_profiles.hospital_city` — the table only had a free-text `hospital_address`, which no index can
  serve for "city". Doctors set it at registration or in their profile; if they don't, it is derived
  from the address (`derive_city` in `lambdas/common.py`). Existing rows are backfilled the same way,
  never overwriting a city already set, and ambiguous addresses are left empty rather than guessed.
* `doctor_profiles.name_last_word` — a `STORED` generated column (`SUBSTRING_INDEX(full_name,' ',-1)`) so
  the surname can be indexed. Inserts and updates never need to set it.

**Rejected alternatives.** `LIKE '%x%'` (contains) cannot use an index and would scan every row.
`FULLTEXT` ignores words shorter than 3 characters by default and behaves surprisingly for short
queries. Separate single-column indexes for every filter would let the optimiser pick only one.

**Trade-offs, stated plainly.** Five more indexes cost a little on doctor writes, which are rare
compared with searches. Name matching covers the start of the full name or the *last* word, not a
middle name. `LIMIT/OFFSET` pagination is fine for a directory of this size; if it grows to
tens of thousands, switch to keyset pagination on `(full_name, user_id)`.

**Checking it against reality.** `scripts/explain_search_queries.py` builds the SQL with the same
functions the Lambda runs and prints `EXPLAIN` (index chosen, rows examined) for all 15 filter
combinations. Run it from the EC2 workbench, which can reach RDS:

```bash
ENVIRONMENT=dev python scripts/explain_search_queries.py
```

On a table with only a handful of rows MySQL may prefer a full scan whatever the indexes are;
judge the plans on a realistically sized table. I could not run `EXPLAIN` against your RDS from here,
so this output is the thing to review with the mentor.

## Patient data isolation

1. The only identity any patient query uses is `userId` from the API Gateway authorizer context, which
   comes from the verified JWT. It is never taken from the URL, query string or body, and `userId` /
   `patientId` in a body are rejected.
2. The authorizer already refuses non-patient roles on `/patient/*` routes (a doctor token gets `403`).
3. `GET /patient/profile/{patient_id}` first looks up the *caller's own* patient id and returns `403` on
   any mismatch **without querying the requested record**. A nonexistent id returns the identical `403`, so
   ids cannot be enumerated.
4. There are no write routes that take an id, so another patient's row cannot be addressed for update or
   delete at all (`PUT/PATCH/DELETE` on `/{patient_id}` → `403`/`405`).
5. A revoked token (after logout) is rejected by the authorizer's DynamoDB denylist, so a stolen, logged-out
   token stops working.

Proven by `tests/test_patient_profile.py` (unit; asserts the other id is never used in any SQL) and
`tests/integration/test_m4_api.py::test_patient_cannot_read_another_patients_profile` (real API).

## Deploying

CI (`.github/workflows/deploy.yaml`) now: packages the new Lambdas → deploys the auth stack → **invokes
`db-migrate`** (idempotent; adds the column and indexes, fails the pipeline on error) → connectivity
check → **runs `tests/integration` against the deployed API**.

Things to know before the first run:

* **`cloudformation/iam-stack.yaml` changed** (CI role may invoke `medislot-<env>-db-migrate`). If your CI role
  cannot update its own policy, an administrator must deploy that stack once first.
* The API Gateway deployment resource was renamed (`ApiDeploymentM4`). An unchanged Deployment resource is
  never re-created, so without this the new routes would exist in the template but not on the live stage.
* `doctor_slot_management` is now packaged by CI (it was in the stack but missing from the package loop), and
  its role can read the JWT secret, which `common.config()` needs.
* Schema changes for a **clean** install are also in `scehma/schema.sql`; the migration Lambda detects that and
  only records them.
* Dashboard: copy `dashboard/` to the EC2 instance and restart it the way you do today (the stack's
  UserData does not deploy application code, and I did not change it, as that would replace the instance).

## Mentor checkpoint runbook

```bash
API=https://<id>.execute-api.<region>.amazonaws.com/dev

# every filter combination (public, no token)
curl "$API/doctors?specialisation=Cardiology"
curl "$API/doctors?city=Hyd"
curl "$API/doctors?hospital=Apollo"
curl "$API/doctors?name=Sharma"
curl "$API/doctors?specialisation=Cardiology&city=Hyd&hospital=Apollo&name=Sharma&page=1&page_size=5"

# isolation: log in as patient B, ask for patient A's id -> 403
curl -H "Authorization: Bearer $TOKEN_B" "$API/patient/profile/<A's patientId>"

# slots with free seats
curl "$API/doctors/<doctor_id>/slots?from=2026-11-05&to=2026-11-11"
```

Then run `scripts/explain_search_queries.py` and review the chosen index per combination, and sign in to the
dashboard (Find a doctor → View profile & slots).

## Decisions and known gaps

* **DynamoDB** is not used by the new endpoints for data. It is in the request path through the token
  denylist (revoked tokens are refused on every patient call). The `Slots` table and its
  `GSI_DoctorDate` index already exist but nothing writes to them; they are the natural home for seat
  holds when booking arrives in Milestone 5.
* **Verification is not a search filter.** Nothing in the system sets `verification_status` to VERIFIED
  yet, so filtering on it would hide every doctor. Add the filter when an admin approval flow exists.
* **Booking** is deliberately absent; the dashboard's Book button is disabled ("Booking opens soon").
  Deleting a profile does not yet check for future appointments, because there is no appointments table.
* **Public endpoints are unauthenticated**, so consider API Gateway throttling or a usage plan before
  exposing them beyond a demo.
