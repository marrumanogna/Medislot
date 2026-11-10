"""Public doctor discovery (no login needed).

    GET /doctors                                search with filters + pagination
    GET /doctors/filters                        specialisations and cities currently in use
    GET /doctors/{doctor_id}                    public profile, consultation types, fees
    GET /doctors/{doctor_id}/slots?from=&to=    free seats per slot for a date range

Only information that is safe to show publicly is returned: no email, licence
number, licence document or verification details.

Search semantics (chosen so that every filter can use a B-tree index; see
docs/milestone-4.md for the index justification):

    specialisation  equality, case-insensitive
    city            case-insensitive prefix        ("hyd"  -> Hyderabad)
    hospital        case-insensitive prefix        ("apol" -> Apollo ...)
    name            case-insensitive prefix of the full name OR of the surname
    (filters combine with AND; results are ordered by name, then id)

Seats: an appointment slot holds exactly one patient, so seats_total is 1 and
seats_free is 1 for an AVAILABLE slot and 0 for a BOOKED one.
"""

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3

from common import config, dbconn, response, emit, correlation

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
MAX_PAGE = 10000
MAX_RANGE_DAYS = 31
DEFAULT_RANGE_DAYS = 7
SEATS_PER_SLOT = 1
FILTER_LIMITS = {"name": 100, "hospital": 200, "specialisation": 120, "city": 100}


class BadRequest(Exception):
    pass


def error_response(status, message, cid, **extra):
    return response(status, {"message": message, "correlationId": cid, **extra})


# --------------------------------------------------------------------------- #
# Search: parameter parsing and SQL building (pure functions)
# --------------------------------------------------------------------------- #
def like_prefix(value):
    """Escape LIKE wildcards (escape char is '!') and append the prefix %."""
    for ch in ("!", "%", "_"):
        value = value.replace(ch, "!" + ch)
    return value + "%"


def _positive_int(params, key, default, maximum):
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be an integer")
    if not 1 <= number <= maximum:
        raise BadRequest(f"{key} must be between 1 and {maximum}")
    return number


def parse_search_params(params):
    """Validate query parameters -> {'filters': {...}, 'page': n, 'page_size': n}."""
    params = dict(params or {})
    # American spelling is accepted too; the API documents "specialisation".
    if params.get("specialisation") in (None, "") and params.get("specialization"):
        params["specialisation"] = params["specialization"]

    filters = {}
    for key, limit in FILTER_LIMITS.items():
        value = params.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        if len(value) > limit:
            raise BadRequest(f"{key} must not exceed {limit} characters")
        filters[key] = value

    return {
        "filters": filters,
        "page": _positive_int(params, "page", 1, MAX_PAGE),
        "page_size": _positive_int(
            params, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
        ),
    }


def build_search_where(filters):
    """WHERE fragment + params for the active filters.

    Each predicate is index-friendly (equality, or a LIKE with a constant
    prefix and no leading wildcard). Values are always bound parameters.
    """
    clauses = [
        "u.role = 'DOCTOR'",
        "u.is_active = 1",
    ]
    params = []

    if "specialisation" in filters:
        clauses.append("dp.specialization = %s")
        params.append(filters["specialisation"])

    if "city" in filters:
        clauses.append("dp.hospital_city LIKE %s ESCAPE '!'")
        params.append(like_prefix(filters["city"]))

    if "hospital" in filters:
        clauses.append("dp.hospital_name LIKE %s ESCAPE '!'")
        params.append(like_prefix(filters["hospital"]))

    if "name" in filters:
        pattern = like_prefix(filters["name"])
        clauses.append(
            "(dp.full_name LIKE %s ESCAPE '!' "
            "OR dp.name_last_word LIKE %s ESCAPE '!')"
        )
        params.extend([pattern, pattern])

    return " AND ".join(clauses), params


SEARCH_FROM = """
    FROM doctor_profiles dp
    JOIN users u ON u.user_id = dp.user_id
"""


def build_search_queries(filters, page, page_size):
    """Return ((count_sql, count_params), (page_sql, page_params))."""
    where, params = build_search_where(filters)

    count_sql = f"SELECT COUNT(*) AS total {SEARCH_FROM} WHERE {where}"

    page_sql = (
        "SELECT dp.user_id AS doctor_id, dp.full_name, dp.specialization, "
        "dp.qualifications, dp.experience_years, dp.hospital_name, "
        "dp.hospital_city, dp.languages_spoken "
        f"{SEARCH_FROM} WHERE {where} "
        "ORDER BY dp.full_name, dp.user_id LIMIT %s OFFSET %s"
    )
    page_params = [*params, page_size, (page - 1) * page_size]

    return (count_sql, params), (page_sql, page_params)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _json_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except ValueError:
            return []
    return []


def _as_utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _to_ist(value):
    return _as_utc(value).astimezone(IST)


def _money(value):
    return str(value) if value is not None else None


def _parse_doctor_id(event):
    raw = (event.get("pathParameters") or {}).get("doctor_id")
    try:
        doctor_id = int(raw)
        if doctor_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise BadRequest("doctor_id must be a positive integer")
    return doctor_id


def _doctor_exists(cur, doctor_id):
    cur.execute(
        "SELECT dp.user_id FROM doctor_profiles dp "
        "JOIN users u ON u.user_id = dp.user_id "
        "WHERE dp.user_id = %s AND u.role = 'DOCTOR' AND u.is_active = 1",
        (doctor_id,),
    )
    return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# GET /doctors
# --------------------------------------------------------------------------- #
def search_doctors(cur, parsed, now_utc):
    (count_sql, count_params), (page_sql, page_params) = build_search_queries(
        parsed["filters"], parsed["page"], parsed["page_size"]
    )

    cur.execute(count_sql, count_params)
    total = int(cur.fetchone()["total"])

    cur.execute(page_sql, page_params)
    rows = cur.fetchall()

    ids = [int(r["doctor_id"]) for r in rows]
    fees, next_slots = {}, {}

    if ids:
        marks = ", ".join(["%s"] * len(ids))

        cur.execute(
            "SELECT f.doctor_id, MIN(f.fee) AS fee_from, MAX(f.fee) AS fee_to "
            "FROM doctor_consultation_fees f "
            "JOIN consultation_catalogue ct "
            "  ON ct.consultation_type_id = f.consultation_type_id "
            " AND ct.is_active = 1 "
            f"WHERE f.doctor_id IN ({marks}) AND f.is_active = 1 "
            "GROUP BY f.doctor_id",
            ids,
        )
        fees = {int(r["doctor_id"]): r for r in cur.fetchall()}

        cur.execute(
            "SELECT doctor_id, MIN(slot_start) AS next_slot "
            "FROM appointment_slots "
            f"WHERE doctor_id IN ({marks}) AND status = 'AVAILABLE' "
            "  AND slot_start > %s "
            "GROUP BY doctor_id",
            [*ids, now_utc.replace(tzinfo=None)],
        )
        next_slots = {int(r["doctor_id"]): r["next_slot"] for r in cur.fetchall()}

    items = []
    for row in rows:
        doctor_id = int(row["doctor_id"])
        fee = fees.get(doctor_id) or {}
        next_slot = next_slots.get(doctor_id)
        items.append({
            "doctor_id": doctor_id,
            "full_name": row["full_name"],
            "specialization": row["specialization"],
            "qualifications": _json_list(row["qualifications"]),
            "experience_years": int(row["experience_years"] or 0),
            "hospital_name": row["hospital_name"],
            "hospital_city": row["hospital_city"],
            "languages_spoken": _json_list(row["languages_spoken"]),
            "fee_from": _money(fee.get("fee_from")),
            "fee_to": _money(fee.get("fee_to")),
            "next_available": _to_ist(next_slot).isoformat() if next_slot else None,
        })

    page_size = parsed["page_size"]
    total_pages = (total + page_size - 1) // page_size
    return {
        "items": items,
        "page": parsed["page"],
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": parsed["page"] < total_pages,
        "filters": parsed["filters"],
    }


# --------------------------------------------------------------------------- #
# GET /doctors/filters
# --------------------------------------------------------------------------- #
MAX_FILTER_OPTIONS = 200


def filter_options(cur):
    """Values that actually exist, so the UI only offers filters that match.

    Specialisation is matched exactly, and doctors type it freely at
    registration, so a fixed list would silently miss some of them.
    """
    cur.execute(
        "SELECT DISTINCT dp.specialization AS value "
        "FROM doctor_profiles dp JOIN users u ON u.user_id = dp.user_id "
        "WHERE u.role = 'DOCTOR' AND u.is_active = 1 "
        "ORDER BY dp.specialization LIMIT %s",
        (MAX_FILTER_OPTIONS,),
    )
    specializations = [r["value"] for r in cur.fetchall()]

    cur.execute(
        "SELECT DISTINCT dp.hospital_city AS value "
        "FROM doctor_profiles dp JOIN users u ON u.user_id = dp.user_id "
        "WHERE u.role = 'DOCTOR' AND u.is_active = 1 "
        "AND dp.hospital_city IS NOT NULL "
        "ORDER BY dp.hospital_city LIMIT %s",
        (MAX_FILTER_OPTIONS,),
    )
    cities = [r["value"] for r in cur.fetchall()]
    return {"specializations": specializations, "cities": cities}


# --------------------------------------------------------------------------- #
# GET /doctors/{doctor_id}
# --------------------------------------------------------------------------- #
def doctor_profile(cur, doctor_id):
    cur.execute(
        "SELECT dp.user_id AS doctor_id, dp.full_name, dp.specialization, "
        "dp.qualifications, dp.experience_years, dp.hospital_name, "
        "dp.hospital_address, dp.hospital_city, dp.languages_spoken, dp.bio, "
        "dp.photo_key, dp.timezone "
        "FROM doctor_profiles dp JOIN users u ON u.user_id = dp.user_id "
        "WHERE dp.user_id = %s AND u.role = 'DOCTOR' AND u.is_active = 1",
        (doctor_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    cur.execute(
        "SELECT ct.consultation_type_id, ct.name, ct.duration_minutes, f.fee "
        "FROM doctor_consultation_fees f "
        "JOIN consultation_catalogue ct "
        "  ON ct.consultation_type_id = f.consultation_type_id "
        "WHERE f.doctor_id = %s AND f.is_active = 1 AND ct.is_active = 1 "
        "ORDER BY ct.duration_minutes, ct.consultation_type_id",
        (doctor_id,),
    )
    types = [
        {
            "consultation_type_id": int(t["consultation_type_id"]),
            "name": t["name"],
            "duration_minutes": int(t["duration_minutes"]),
            "fee": _money(t["fee"]),
        }
        for t in cur.fetchall()
    ]

    photo_url = None
    bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")
    photo_key = row.get("photo_key")
    if photo_key and bucket and photo_key.startswith(f"doctors/{doctor_id}/"):
        photo_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": photo_key,
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=300,
        )

    fees = [t["fee"] for t in types if t["fee"] is not None]
    return {
        "doctor_id": int(row["doctor_id"]),
        "full_name": row["full_name"],
        "specialization": row["specialization"],
        "qualifications": _json_list(row["qualifications"]),
        "experience_years": int(row["experience_years"] or 0),
        "hospital_name": row["hospital_name"],
        "hospital_address": row["hospital_address"],
        "hospital_city": row["hospital_city"],
        "languages_spoken": _json_list(row["languages_spoken"]),
        "bio": row["bio"],
        "photo_url": photo_url,
        "timezone": "Asia/Kolkata",  # the application schedules everything in IST
        "consultation_types": types,
        "fee_from": min(fees, key=float) if fees else None,
    }


# --------------------------------------------------------------------------- #
# GET /doctors/{doctor_id}/slots
# --------------------------------------------------------------------------- #
def parse_date_param(params, key):
    raw = params.get(key)
    if raw in (None, ""):
        return None
    try:
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() != raw:
            raise ValueError
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must use YYYY-MM-DD format")
    return parsed


def parse_slot_range(params, today):
    """Inclusive IST date range; defaults to the next 7 days."""
    start = parse_date_param(params, "from") or today
    end = parse_date_param(params, "to") or (start + timedelta(days=DEFAULT_RANGE_DAYS - 1))

    if end < start:
        raise BadRequest("to must not be earlier than from")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise BadRequest(f"The date range cannot exceed {MAX_RANGE_DAYS} days")

    type_id = None
    if params.get("consultation_type_id") not in (None, ""):
        try:
            type_id = int(params["consultation_type_id"])
            if type_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise BadRequest("consultation_type_id must be a positive integer")

    return start, end, type_id


def ist_day_to_utc_bounds(start, end):
    """[00:00 IST of start, 00:00 IST of end+1) as naive UTC datetimes."""
    lo = datetime.combine(start, time.min).replace(tzinfo=IST).astimezone(UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min).replace(tzinfo=IST).astimezone(UTC)
    return lo.replace(tzinfo=None), hi.replace(tzinfo=None)


def slot_availability(cur, doctor_id, start, end, type_id, now_utc):
    lo, hi = ist_day_to_utc_bounds(start, end)
    # Never offer a slot that has already started.
    lo = max(lo, now_utc.replace(tzinfo=None))

    sql = (
        "SELECT s.slot_id, s.consultation_type_id, ct.name AS consultation_type, "
        "ct.duration_minutes, COALESCE(dcf.capacity, ct.capacity) AS capacity, dcf.fee, s.slot_start, s.slot_end, s.status, "
        "(SELECT COUNT(*) FROM appointments ap WHERE ap.slot_id = s.slot_id "
        " AND ap.status = 'CONFIRMED') AS booked_count "
        "FROM appointment_slots s "
        "JOIN consultation_catalogue ct "
        "  ON ct.consultation_type_id = s.consultation_type_id "
        "LEFT JOIN doctor_consultation_fees dcf "
        "  ON dcf.doctor_id = s.doctor_id "
        " AND dcf.consultation_type_id = s.consultation_type_id "
        " AND dcf.is_active = 1 "
        "WHERE s.doctor_id = %s AND s.status IN ('AVAILABLE', 'BOOKED') "
        "  AND s.slot_start >= %s AND s.slot_start < %s "
    )
    params = [doctor_id, lo, hi]
    if type_id is not None:
        sql += "AND s.consultation_type_id = %s "
        params.append(type_id)
    sql += "ORDER BY s.slot_start, s.slot_id"

    cur.execute(sql, params)
    slots = cur.fetchall()

    cur.execute(
        "SELECT blackout_date FROM doctor_blackout_dates "
        "WHERE doctor_id = %s AND blackout_date BETWEEN %s AND %s",
        (doctor_id, start, end),
    )
    blackouts = {r["blackout_date"] for r in cur.fetchall()}

    items, days = [], {}
    for slot in slots:
        start_ist = _to_ist(slot["slot_start"])
        local_day = start_ist.date()
        if local_day in blackouts:
            continue  # doctor is away that day

        total = int(slot.get("capacity") or SEATS_PER_SLOT)
        if slot.get("booked_count") is None:
            free = total if slot["status"] == "AVAILABLE" else 0
        else:
            free = max(total - int(slot["booked_count"]), 0)
        items.append({
            "slot_id": int(slot["slot_id"]),
            "consultation_type_id": int(slot["consultation_type_id"]),
            "consultation_type": slot["consultation_type"],
            "duration_minutes": int(slot["duration_minutes"]),
            "fee": _money(slot["fee"]),
            "date": local_day.isoformat(),
            "slot_start": start_ist.isoformat(),
            "slot_end": _to_ist(slot["slot_end"]).isoformat(),
            "seats_total": total,
            "seats_free": free,
            "status": slot["status"],
        })
        bucket = days.setdefault(
            local_day.isoformat(), {"date": local_day.isoformat(), "total_slots": 0, "free_seats": 0}
        )
        bucket["total_slots"] += 1
        bucket["free_seats"] += free

    return {
        "doctor_id": doctor_id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timezone": "Asia/Kolkata",
        "summary": {
            "total_slots": len(items),
            "slots_with_free_seats": sum(1 for i in items if i["seats_free"] > 0),
            "free_seats": sum(i["seats_free"] for i in items),
        },
        "days": [days[k] for k in sorted(days)],
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def _route(event):
    resource = (event.get("resource") or "").rstrip("/")
    if resource in ("/doctors", "/doctors/filters", "/doctors/{doctor_id}",
                    "/doctors/{doctor_id}/slots"):
        return resource
    # Fallback for direct invocations / tests.
    path = (event.get("path") or "").rstrip("/")
    if path.endswith("/doctors/filters"):
        return "/doctors/filters"
    if path.endswith("/slots"):
        return "/doctors/{doctor_id}/slots"
    if (event.get("pathParameters") or {}).get("doctor_id"):
        return "/doctors/{doctor_id}"
    return "/doctors"


def handler(event, context):
    cid = correlation(event)
    method = (event.get("httpMethod") or "GET").upper()

    if method != "GET":
        return error_response(405, "Method not allowed", cid)

    route = _route(event)
    params = event.get("queryStringParameters") or {}
    now_utc = datetime.now(UTC)

    try:
        if route == "/doctors":
            parsed = parse_search_params(params)
        elif route == "/doctors/filters":
            pass
        else:
            doctor_id = _parse_doctor_id(event)
            if route.endswith("/slots"):
                start, end, type_id = parse_slot_range(
                    params, now_utc.astimezone(IST).date()
                )
    except BadRequest as exc:
        return error_response(400, str(exc), cid)

    conn = None
    try:
        conn = dbconn(config())
        with conn.cursor() as cur:
            if route == "/doctors":
                result = search_doctors(cur, parsed, now_utc)
            elif route == "/doctors/filters":
                result = filter_options(cur)
            elif route == "/doctors/{doctor_id}":
                result = doctor_profile(cur, doctor_id)
            else:
                result = (
                    slot_availability(cur, doctor_id, start, end, type_id, now_utc)
                    if _doctor_exists(cur, doctor_id) else None
                )
        conn.rollback()  # read-only: close the transaction cleanly

        if result is None:
            return error_response(404, "Doctor not found", cid)
        return response(200, result)

    except Exception:
        logger.exception("Doctor discovery request failed")
        emit("error", "doctor_discovery_failed", cid, route=route)
        return error_response(500, "Internal server error", cid)

    finally:
        if conn is not None:
            conn.close()
