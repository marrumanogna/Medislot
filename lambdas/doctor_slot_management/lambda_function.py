import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from common import config, dbconn, response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


class APIError(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def parse_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise APIError(400, "Request body must contain valid JSON")


def get_doctor_id(event):
    auth = event.get("requestContext", {}).get("authorizer", {})
    role = auth.get("role") or auth.get("userRole") or auth.get("user_role")
    doctor_id = (
        auth.get("userId") or auth.get("user_id")
        or auth.get("doctor_id") or auth.get("principalId")
    )
    if str(role or "").upper() != "DOCTOR":
        raise APIError(403, "Only authenticated doctors can manage slots")
    try:
        return int(doctor_id)
    except (TypeError, ValueError):
        raise APIError(401, "Doctor identity is missing from authorizer context")


def parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise APIError(400, "date must use YYYY-MM-DD format")


def parse_time(value):
    try:
        result = time.fromisoformat(str(value))
        if result.tzinfo is not None:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise APIError(400, "time must use HH:MM or HH:MM:SS format")


def ist_to_utc(local_dt):
    return (
        local_dt.replace(tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    )


def date_range_utc(day):
    start = ist_to_utc(datetime.combine(day, time.min))
    end = ist_to_utc(datetime.combine(day + timedelta(days=1), time.min))
    return start, end


def lock_schedule(cur, doctor_id, day):
    name = f"ms_slot_{doctor_id}_{day.strftime('%Y%m%d')}"
    cur.execute("SELECT GET_LOCK(%s, 10) AS lock_status", (name,))
    row = cur.fetchone()
    if not row or row.get("lock_status") != 1:
        raise APIError(409, "Schedule is being updated. Please try again.")
    return name


def unlock_schedule(cur, name):
    if name:
        cur.execute("SELECT RELEASE_LOCK(%s)", (name,))


def get_type(cur, doctor_id, type_id):
    cur.execute(
        """
        SELECT ct.consultation_type_id, ct.name, ct.duration_minutes, dcf.fee
        FROM consultation_catalogue ct
        JOIN doctor_consultation_fees dcf
          ON dcf.consultation_type_id = ct.consultation_type_id
        WHERE ct.consultation_type_id = %s
          AND ct.is_active = TRUE
          AND dcf.doctor_id = %s
          AND dcf.is_active = TRUE
        LIMIT 1
        """,
        (type_id, doctor_id)
    )
    row = cur.fetchone()
    if not row:
        raise APIError(400, "Consultation type is inactive or has no active fee")
    duration = int(row["duration_minutes"])
    if not 5 <= duration <= 480:
        raise APIError(400, "Consultation duration is invalid")
    return row


def get_slot(cur, doctor_id, slot_id):
    cur.execute(
        """
        SELECT slot_id, doctor_id, consultation_type_id,
               slot_start, slot_end, display_order, status
        FROM appointment_slots
        WHERE slot_id = %s AND doctor_id = %s
        FOR UPDATE
        """,
        (slot_id, doctor_id)
    )
    row = cur.fetchone()
    if not row:
        raise APIError(404, "Slot not found")
    if row["status"] != "DRAFT":
        raise APIError(409, "Only DRAFT slots can be edited")
    return row


def ensure_no_overlap(cur, doctor_id, start_utc, end_utc, exclude_id=None):
    sql = """
        SELECT slot_id
        FROM appointment_slots
        WHERE doctor_id = %s
          AND slot_start < %s
          AND slot_end > %s
    """
    params = [doctor_id, end_utc, start_utc]
    if exclude_id is not None:
        sql += " AND slot_id <> %s"
        params.append(exclude_id)
    sql += " LIMIT 1 FOR UPDATE"
    cur.execute(sql, tuple(params))
    if cur.fetchone():
        raise APIError(409, "This time overlaps another slot for the doctor")


def create_draft(cur, doctor_id, payload):
    day = parse_date(payload.get("date"))
    start_time = parse_time(payload.get("start_time"))
    try:
        type_id = int(payload.get("consultation_type_id"))
    except (TypeError, ValueError):
        raise APIError(400, "consultation_type_id is required")

    selected = get_type(cur, doctor_id, type_id)
    local_start = datetime.combine(day, start_time)
    if local_start <= datetime.now(IST).replace(tzinfo=None):
        raise APIError(400, "A new slot must start in the future")
    local_end = local_start + timedelta(minutes=int(selected["duration_minutes"]))
    start_utc, end_utc = ist_to_utc(local_start), ist_to_utc(local_end)

    lock_name = lock_schedule(cur, doctor_id, day)
    try:
        ensure_no_overlap(cur, doctor_id, start_utc, end_utc)
        day_start, day_end = date_range_utc(day)
        cur.execute(
            """
            SELECT COALESCE(MAX(display_order), 0) + 1 AS next_order
            FROM appointment_slots
            WHERE doctor_id = %s AND slot_start >= %s AND slot_start < %s
            """,
            (doctor_id, day_start, day_end)
        )
        next_order = int(cur.fetchone()["next_order"])
        cur.execute(
            """
            INSERT INTO appointment_slots
              (doctor_id, consultation_type_id, slot_start, slot_end,
               display_order, status)
            VALUES (%s, %s, %s, %s, %s, 'DRAFT')
            """,
            (doctor_id, type_id, start_utc, end_utc, next_order)
        )
        return {
            "slot_id": cur.lastrowid,
            "consultation_type_id": type_id,
            "consultation_type": selected["name"],
            "fee": str(selected["fee"]),
            "slot_start": local_start.isoformat(),
            "slot_end": local_end.isoformat(),
            "display_order": next_order,
            "status": "DRAFT"
        }
    finally:
        unlock_schedule(cur, lock_name)


def update_slot(cur, doctor_id, slot_id, payload):
    slot = get_slot(cur, doctor_id, slot_id)
    action = str(payload.get("action", "")).lower()
    old_day = slot["slot_start"].replace(tzinfo=UTC).astimezone(IST).date()
    lock_name = lock_schedule(cur, doctor_id, old_day)

    try:
        if action == "assign":
            try:
                type_id = int(payload.get("consultation_type_id"))
            except (TypeError, ValueError):
                raise APIError(400, "consultation_type_id is required")
            selected = get_type(cur, doctor_id, type_id)
            local_start = slot["slot_start"].replace(tzinfo=UTC).astimezone(IST).replace(tzinfo=None)
            local_end = local_start + timedelta(minutes=int(selected["duration_minutes"]))
            start_utc, end_utc = ist_to_utc(local_start), ist_to_utc(local_end)
            ensure_no_overlap(cur, doctor_id, start_utc, end_utc, slot_id)
            cur.execute(
                """
                UPDATE appointment_slots
                SET consultation_type_id = %s, slot_end = %s
                WHERE slot_id = %s AND doctor_id = %s AND status = 'DRAFT'
                """,
                (type_id, end_utc, slot_id, doctor_id)
            )
            return {
                "message": "Consultation type assigned",
                "slot_id": slot_id,
                "consultation_type": selected["name"],
                "slot_start": local_start.isoformat(),
                "slot_end": local_end.isoformat()
            }

        if action == "reschedule":
            if slot["consultation_type_id"] is None:
                raise APIError(400, "Assign a consultation type before rescheduling")
            selected = get_type(cur, doctor_id, int(slot["consultation_type_id"]))
            day = parse_date(payload.get("date"))
            start_time = parse_time(payload.get("start_time"))
            local_start = datetime.combine(day, start_time)
            if local_start <= datetime.now(IST).replace(tzinfo=None):
                raise APIError(400, "A slot cannot be rescheduled into the past")
            local_end = local_start + timedelta(minutes=int(selected["duration_minutes"]))
            start_utc, end_utc = ist_to_utc(local_start), ist_to_utc(local_end)

            new_lock = None
            if day != old_day:
                new_lock = lock_schedule(cur, doctor_id, day)
            try:
                ensure_no_overlap(cur, doctor_id, start_utc, end_utc, slot_id)
                cur.execute(
                    """
                    UPDATE appointment_slots
                    SET slot_start = %s, slot_end = %s
                    WHERE slot_id = %s AND doctor_id = %s AND status = 'DRAFT'
                    """,
                    (start_utc, end_utc, slot_id, doctor_id)
                )
            finally:
                if new_lock:
                    unlock_schedule(cur, new_lock)
            return {
                "message": "Slot rescheduled",
                "slot_id": slot_id,
                "slot_start": local_start.isoformat(),
                "slot_end": local_end.isoformat()
            }

        if action == "reorder":
            try:
                order = int(payload.get("display_order"))
            except (TypeError, ValueError):
                raise APIError(400, "display_order must be an integer")
            if order < 1:
                raise APIError(400, "display_order must be at least 1")
            cur.execute(
                """
                UPDATE appointment_slots
                SET display_order = %s
                WHERE slot_id = %s AND doctor_id = %s AND status = 'DRAFT'
                """,
                (order, slot_id, doctor_id)
            )
            return {"message": "Display order updated", "slot_id": slot_id, "display_order": order}

        if action == "delete":
            cur.execute(
                """
                DELETE FROM appointment_slots
                WHERE slot_id = %s AND doctor_id = %s AND status = 'DRAFT'
                """,
                (slot_id, doctor_id)
            )
            return {"message": "Draft slot deleted", "slot_id": slot_id}

        raise APIError(400, "action must be assign, reschedule, reorder, or delete")
    finally:
        unlock_schedule(cur, lock_name)


def reorder_schedule(cur, doctor_id, payload):
    day = parse_date(payload.get("date"))
    slot_ids = payload.get("slot_ids")
    if not isinstance(slot_ids, list) or not slot_ids:
        raise APIError(400, "slot_ids must be a non-empty ordered array")
    try:
        slot_ids = [int(value) for value in slot_ids]
    except (TypeError, ValueError):
        raise APIError(400, "Every slot_id must be an integer")
    if len(slot_ids) != len(set(slot_ids)):
        raise APIError(400, "slot_ids contains duplicate IDs")

    lock_name = lock_schedule(cur, doctor_id, day)
    try:
        day_start, day_end = date_range_utc(day)
        cur.execute(
            """
            SELECT slot_id, slot_start, slot_end
            FROM appointment_slots
            WHERE doctor_id = %s AND slot_start >= %s AND slot_start < %s
              AND status = 'DRAFT'
            ORDER BY slot_start
            FOR UPDATE
            """,
            (doctor_id, day_start, day_end)
        )
        rows = {int(row["slot_id"]): row for row in cur.fetchall()}
        existing = set(rows)
        if set(slot_ids) != existing:
            raise APIError(400, "Provide every DRAFT slot ID for this date exactly once")

        # Re-time the drafts so the new order is also the time order:
        # keep the day's first start and the gaps (breaks), but lay the slots
        # out back to back in the new order, each keeping its own duration.
        chron = sorted(rows.values(), key=lambda row: row["slot_start"])
        gaps = [
            max(chron[i + 1]["slot_start"] - chron[i]["slot_end"], timedelta(0))
            for i in range(len(chron) - 1)
        ]
        new_times = {}
        cursor_time = chron[0]["slot_start"]
        for k, sid in enumerate(slot_ids):
            length = rows[sid]["slot_end"] - rows[sid]["slot_start"]
            new_times[sid] = (cursor_time, cursor_time + length)
            cursor_time = cursor_time + length + (gaps[k] if k < len(gaps) else timedelta(0))

        # Park the drafts far away first so unique start times never clash.
        marks = ", ".join(["%s"] * len(slot_ids))
        cur.execute(
            "UPDATE appointment_slots "
            "SET slot_start = DATE_ADD(slot_start, INTERVAL 100 YEAR), "
            "slot_end = DATE_ADD(slot_end, INTERVAL 100 YEAR) "
            "WHERE doctor_id = %s AND status = 'DRAFT' AND slot_id IN (" + marks + ")",
            (doctor_id, *slot_ids)
        )
        for sid in slot_ids:
            ensure_no_overlap(cur, doctor_id, new_times[sid][0], new_times[sid][1])
        for order, sid in enumerate(slot_ids, start=1):
            cur.execute(
                """
                UPDATE appointment_slots
                SET slot_start = %s, slot_end = %s, display_order = %s
                WHERE slot_id = %s AND doctor_id = %s AND status = 'DRAFT'
                """,
                (new_times[sid][0], new_times[sid][1], order, sid, doctor_id)
            )
        return {"message": "Draft slots reordered and times updated", "date": day.isoformat(), "slot_ids": slot_ids}
    finally:
        unlock_schedule(cur, lock_name)


def publish_schedule(cur, doctor_id, payload):
    day = parse_date(payload.get("date"))
    lock_name = lock_schedule(cur, doctor_id, day)
    try:
        day_start, day_end = date_range_utc(day)
        cur.execute(
            """
            SELECT slot_id, consultation_type_id, slot_start, slot_end
            FROM appointment_slots
            WHERE doctor_id = %s AND slot_start >= %s AND slot_start < %s
              AND status = 'DRAFT'
            ORDER BY slot_start
            FOR UPDATE
            """,
            (doctor_id, day_start, day_end)
        )
        slots = cur.fetchall()
        if not slots:
            raise APIError(400, "There are no DRAFT slots to publish")

        previous_end = None
        for slot in slots:
            if slot["consultation_type_id"] is None:
                raise APIError(400, f"Slot {slot['slot_id']} has no consultation type")
            selected = get_type(cur, doctor_id, int(slot["consultation_type_id"]))
            expected_end = slot["slot_start"] + timedelta(minutes=int(selected["duration_minutes"]))
            if slot["slot_end"] != expected_end:
                raise APIError(400, f"Slot {slot['slot_id']} duration does not match its consultation type")
            if previous_end is not None and slot["slot_start"] < previous_end:
                raise APIError(409, "Draft schedule contains overlapping slots")
            previous_end = slot["slot_end"]

        cur.execute(
            """
            UPDATE appointment_slots SET status = 'AVAILABLE'
            WHERE doctor_id = %s AND slot_start >= %s AND slot_start < %s
              AND status = 'DRAFT'
            """,
            (doctor_id, day_start, day_end)
        )
        return {
            "message": "Schedule published successfully",
            "date": day.isoformat(),
            "slots_published": cur.rowcount,
            "status": "AVAILABLE"
        }
    finally:
        unlock_schedule(cur, lock_name)


def handler(event, context):
    conn = None
    try:
        event = event or {}
        auth = event.get("requestContext", {}).get("authorizer", {})
        role = auth.get("role") or auth.get("userRole") or auth.get("user_role")
        doctor_id = auth.get("userId") or auth.get("user_id") or auth.get("doctor_id") or auth.get("principalId")
        if str(role or "").upper() != "DOCTOR":
            raise APIError(403, "Only authenticated doctors can manage slots")
        try:
            doctor_id = int(doctor_id)
        except (TypeError, ValueError):
            raise APIError(401, "Doctor identity is missing from authorizer context")

        raw_body = event.get("body")
        if not raw_body:
            payload = {}
        elif isinstance(raw_body, dict):
            payload = raw_body
        else:
            try:
                payload = json.loads(raw_body)
            except (TypeError, json.JSONDecodeError):
                raise APIError(400, "Request body must contain valid JSON")

        method = (
            event.get("httpMethod")
            or event.get("requestContext", {}).get("http", {}).get("method")
            or ""
        ).upper()
        slot_id = (event.get("pathParameters") or {}).get("slot_id")

        if method == "POST" and str(payload.get("action", "create")).lower() == "generate":
            # Generate DRAFT slots for this doctor only (same logic as the nightly job).
            import slot_materialiser_impl
            try:
                days = max(1, min(int(payload.get("days_ahead", 14)), 30))
            except (TypeError, ValueError):
                raise APIError(400, "days_ahead must be a number")
            out = slot_materialiser_impl.handler(
                {"days_ahead": days, "doctor_id": doctor_id}, context
            )
            return response(out.get("statusCode", 200), json.loads(out.get("body") or "{}"))

        conn = dbconn(config())
        with conn.cursor() as cur:
            if method == "POST":
                action = str(payload.get("action", "create")).lower()
                if action == "publish":
                    result = publish_schedule(cur, doctor_id, payload)
                    status_code = 200
                elif action == "create":
                    result = create_draft(cur, doctor_id, payload)
                    status_code = 201
                else:
                    raise APIError(400, "POST action must be create or publish")

            elif method == "PUT":
                result = reorder_schedule(cur, doctor_id, payload)
                status_code = 200

            elif method == "PATCH":
                if not slot_id:
                    raise APIError(400, "slot_id is required in the URL")
                try:
                    slot_id = int(slot_id)
                except (TypeError, ValueError):
                    raise APIError(400, "slot_id must be an integer")
                result = update_slot(cur, doctor_id, slot_id, payload)
                status_code = 200

            else:
                raise APIError(405, "Method not allowed")

        conn.commit()
        return response(status_code, result)

    except APIError as exc:
        if conn is not None:
            conn.rollback()
        return response(exc.status_code, {"message": exc.message})

    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.exception("Database rollback failed")
        logger.exception("Doctor slot management failed")
        return response(500, {"message": "Slot management request failed"})

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("Database connection close failed")