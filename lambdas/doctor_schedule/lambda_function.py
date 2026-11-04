
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from common import config, dbconn, response, correlation, emit

logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def authenticated_doctor(event):
    auth = (
        (event.get("requestContext") or {})
        .get("authorizer") or {}
    )

    try:
        user_id = int(auth.get("userId"))
    except (TypeError, ValueError):
        return None

    if user_id <= 0 or auth.get("role") != "DOCTOR":
        return None

    return user_id


def to_ist(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(IST)


def handler(event, context):
    logger.info("Doctor Schedule Lambda started")

    cid = correlation(event)
    user_id = authenticated_doctor(event)

    if user_id is None:
        return response(403, {
            "message": "An authenticated doctor account is required",
            "correlationId": cid
        })

    params = event.get("queryStringParameters") or {}
    requested_date = params.get("date")

    if not requested_date:
        return response(400, {
            "message": "date is required in YYYY-MM-DD format",
            "correlationId": cid
        })

    try:
        day = date.fromisoformat(requested_date)

        if day.isoformat() != requested_date:
            raise ValueError

    except (TypeError, ValueError):
        return response(400, {
            "message": "date must use YYYY-MM-DD format",
            "correlationId": cid
        })

    conn = None

    try:
        conn = dbconn(config())

        local_start = datetime.combine(
            day, time.min
        ).replace(tzinfo=IST)

        local_end = datetime.combine(
            day + timedelta(days=1), time.min
        ).replace(tzinfo=IST)

        utc_start = (
            local_start.astimezone(UTC).replace(tzinfo=None)
        )

        utc_end = (
            local_end.astimezone(UTC).replace(tzinfo=None)
        )

        with conn.cursor() as cur:

            logger.info(
                "Fetching schedule for doctor=%s date=%s",
                user_id,
                day
            )

            # LEFT JOIN is important:
            # DRAFT slots can have NULL consultation_type_id.
            cur.execute("""
                SELECT
                    s.slot_id,
                    s.consultation_type_id,
                    ct.name AS consultation_type,
                    ct.duration_minutes,
                    dcf.fee,
                    s.slot_start,
                    s.slot_end,
                    s.status,
                    s.display_order

                FROM appointment_slots s

                LEFT JOIN consultation_catalogue ct
                    ON ct.consultation_type_id =
                       s.consultation_type_id

                LEFT JOIN doctor_consultation_fees dcf
                    ON dcf.doctor_id = s.doctor_id
                   AND dcf.consultation_type_id =
                       s.consultation_type_id
                   AND dcf.is_active = TRUE

                WHERE s.doctor_id = %s
                  AND s.slot_start < %s
                  AND s.slot_end > %s

                ORDER BY
                    s.display_order ASC,
                    s.slot_start ASC,
                    s.slot_id ASC
            """, (
                user_id,
                utc_end,
                utc_start
            ))

            slots = cur.fetchall()

        # Build response objects.
        result = []

        for slot in slots:
            start_ist = to_ist(slot["slot_start"])
            end_ist = to_ist(slot["slot_end"])

            type_id = slot["consultation_type_id"]

            result.append({
                "slot_id": int(slot["slot_id"]),

                "consultation_type_id": (
                    int(type_id) if type_id is not None else None
                ),

                "consultation_type": slot["consultation_type"],

                "duration_minutes": (
                    int(slot["duration_minutes"])
                    if slot["duration_minutes"] is not None
                    else None
                ),

                "fee": (
                    str(slot["fee"])
                    if slot["fee"] is not None
                    else None
                ),

                "slot_start": start_ist.isoformat(),
                "slot_end": end_ist.isoformat(),

                "status": slot["status"],

                "display_order": int(
                    slot["display_order"]
                ),

                "is_configured": type_id is not None
            })

        # Detect overlapping intervals.
        overlap_map = {
            slot["slot_id"]: []
            for slot in result
        }

        for i in range(len(result)):
            for j in range(i + 1, len(result)):

                first = result[i]
                second = result[j]

                first_start = datetime.fromisoformat(
                    first["slot_start"]
                )

                first_end = datetime.fromisoformat(
                    first["slot_end"]
                )

                second_start = datetime.fromisoformat(
                    second["slot_start"]
                )

                second_end = datetime.fromisoformat(
                    second["slot_end"]
                )

                if (
                    first_start < second_end
                    and first_end > second_start
                ):
                    overlap_map[first["slot_id"]].append(
                        second["slot_id"]
                    )

                    overlap_map[second["slot_id"]].append(
                        first["slot_id"]
                    )

        for slot in result:
            conflicts = overlap_map[slot["slot_id"]]

            slot["has_overlap"] = bool(conflicts)
            slot["overlaps_with"] = conflicts

        overlap_count = sum(
            1 for slot in result if slot["has_overlap"]
        )

        logger.info(
            "Schedule retrieved: doctor=%s slots=%s overlaps=%s",
            user_id,
            len(result),
            overlap_count
        )

        return response(200, {
            "doctor_id": user_id,
            "date": day.isoformat(),
            "timezone": "Asia/Kolkata",
            "slots": result,
            "count": len(result),
            "overlapping_slots": overlap_count
        })

    except Exception:
        logger.exception("Doctor Schedule Lambda failed")

        emit(
            "error",
            "doctor_schedule_failed",
            cid,
            userId=str(user_id)
        )

        return response(500, {
            "message": "Unable to retrieve doctor schedule",
            "correlationId": cid
        })

    finally:
        if conn is not None:
            conn.close()
