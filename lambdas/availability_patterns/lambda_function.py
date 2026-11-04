import logging
import time as time_module
from datetime import time, timedelta

from common import config, dbconn, response, body


logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST_TIMEZONE = "Asia/Kolkata"


def normalize_time(value):
    """Convert MySQL TIME or string values to HH:MM:SS."""
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        return (
            f"{(seconds // 3600) % 24:02d}:"
            f"{(seconds % 3600) // 60:02d}:"
            f"{seconds % 60:02d}"
        )

    if isinstance(value, time):
        return value.strftime("%H:%M:%S")

    if not isinstance(value, str):
        raise ValueError("Time must be a string")

    value = value.strip()

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            time_module.strptime(value, fmt)

            if fmt == "%H:%M":
                return f"{value}:00"

            return value

        except ValueError:
            continue

    raise ValueError("Time must use HH:MM or HH:MM:SS format")


def time_to_seconds(value):
    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def validate_time_range(start, end):
    try:
        start = normalize_time(start)
        end = normalize_time(end)
    except (TypeError, ValueError):
        return None, None, False

    valid = time_to_seconds(start) < time_to_seconds(end)
    return start, end, valid


def find_availability_conflict(
    cur, doctor_id, day, start, end, exclude_pattern_id=None
):
    """
    Reject duplicate or overlapping active availability windows
    for the same doctor and weekday.
    """
    query = """
        SELECT pattern_id, start_time, end_time
        FROM availability_patterns
        WHERE doctor_id = %s
          AND day_of_week = %s
          AND is_active = TRUE
    """
    params = [doctor_id, day]

    if exclude_pattern_id is not None:
        query += " AND pattern_id <> %s"
        params.append(exclude_pattern_id)

    cur.execute(query, tuple(params))
    existing_patterns = cur.fetchall()

    new_start = time_to_seconds(start)
    new_end = time_to_seconds(end)

    for existing in existing_patterns:
        old_start, old_end, valid = validate_time_range(
            existing["start_time"],
            existing["end_time"],
        )

        if not valid:
            continue

        old_start_seconds = time_to_seconds(old_start)
        old_end_seconds = time_to_seconds(old_end)

        # Includes exact duplicates and partial overlaps.
        if new_start < old_end_seconds and new_end > old_start_seconds:
            return existing["pattern_id"]

    return None


def handler(event, context):
    logger.info("Availability patterns Lambda execution started")

    auth = (
        (event.get("requestContext") or {}).get("authorizer") or {}
    )

    if auth.get("role") != "DOCTOR" or not auth.get("userId"):
        logger.warning("Access denied: Doctor authorization required")
        return response(403, {"message": "Doctor access required"})

    conn = None

    try:
        doctor_id = int(auth["userId"])
        if doctor_id <= 0:
            logger.warning("Invalid doctor user ID")
            return response(403, {"message": "Doctor access required"})

        method = (event.get("httpMethod") or "GET").upper()
        params = event.get("pathParameters") or {}
        pattern_id = params.get("pattern_id")

        if pattern_id is not None:
            try:
                pattern_id = int(pattern_id)
                if pattern_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return response(400, {"message": "Invalid pattern_id"})

        logger.info(
            "Request received: doctor_id=%s, method=%s, pattern_id=%s",
            doctor_id,
            method,
            pattern_id,
        )

        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            logger.warning("Method not allowed: %s", method)
            return response(405, {"message": "Method not allowed"})

        data = body(event) if method in ("POST", "PUT", "PATCH") else {}

        if method in ("POST", "PUT", "PATCH") and not isinstance(data, dict):
            logger.warning("Invalid JSON body received")
            return response(400, {"message": "Invalid JSON body"})

        conn = dbconn(config())
        logger.info("Database connection established")

        try:
            with conn.cursor() as cur:

                # GET: Fetch one pattern or all patterns for this doctor.
                if method == "GET":
                    logger.info("Fetching availability patterns")

                    if pattern_id:
                        cur.execute(
                            """
                            SELECT pattern_id, day_of_week, start_time,
                                   end_time, timezone, is_active
                            FROM availability_patterns
                            WHERE doctor_id = %s AND pattern_id = %s
                            """,
                            (doctor_id, pattern_id),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT pattern_id, day_of_week, start_time,
                                   end_time, timezone, is_active
                            FROM availability_patterns
                            WHERE doctor_id = %s
                            ORDER BY day_of_week, start_time
                            """,
                            (doctor_id,),
                        )

                    rows = cur.fetchall()
                    logger.info(
                        "Fetched %s availability pattern(s)",
                        len(rows),
                    )

                    if pattern_id:
                        if not rows:
                            return response(
                                404,
                                {"message": "Availability pattern not found"},
                            )

                        result = dict(rows[0])
                        for field in ("start_time", "end_time"):
                            if result.get(field) is not None:
                                result[field] = normalize_time(
                                    result[field]
                                )

                        result["timezone"] = IST_TIMEZONE
                        return response(200, result)

                    items = []
                    for row in rows:
                        item = dict(row)
                        for field in ("start_time", "end_time"):
                            if item.get(field) is not None:
                                item[field] = normalize_time(item[field])
                        item["timezone"] = IST_TIMEZONE
                        items.append(item)

                    return response(200, {"items": items})

                # DELETE: Remove a pattern belonging to this doctor.
                if method == "DELETE":
                    if not pattern_id:
                        logger.warning("pattern_id missing for deletion")
                        return response(
                            400,
                            {"message": "pattern_id is required"},
                        )

                    cur.execute(
                        """
                        DELETE FROM availability_patterns
                        WHERE doctor_id = %s AND pattern_id = %s
                        """,
                        (doctor_id, pattern_id),
                    )

                    if cur.rowcount == 0:
                        conn.rollback()
                        logger.warning(
                            "Availability pattern not found for deletion"
                        )
                        return response(
                            404,
                            {"message": "Availability pattern not found"},
                        )

                    conn.commit()
                    logger.info(
                        "Availability pattern deleted: pattern_id=%s",
                        pattern_id,
                    )
                    return response(
                        200,
                        {"message": "Availability pattern deleted"},
                    )

                # POST: Create a new pattern.
                if method == "POST":
                    required_fields = {
                        "day_of_week",
                        "start_time",
                        "end_time",
                    }
                    missing = required_fields - set(data)

                    if missing:
                        return response(
                            400,
                            {
                                "message": "Missing required fields",
                                "fields": sorted(missing),
                            },
                        )

                    merged = {
                        "day_of_week": data["day_of_week"],
                        "start_time": data["start_time"],
                        "end_time": data["end_time"],
                        "timezone": IST_TIMEZONE,
                        "is_active": data.get("is_active", True),
                    }

                # PUT/PATCH: Fetch existing pattern and merge values.
                else:
                    if not pattern_id:
                        return response(
                            400,
                            {"message": "pattern_id is required"},
                        )

                    cur.execute(
                        """
                        SELECT day_of_week, start_time, end_time,
                               timezone, is_active
                        FROM availability_patterns
                        WHERE doctor_id = %s AND pattern_id = %s
                        """,
                        (doctor_id, pattern_id),
                    )
                    old = cur.fetchone()

                    if not old:
                        return response(
                            404,
                            {"message": "Availability pattern not found"},
                        )

                    allowed_fields = {
                        "day_of_week",
                        "start_time",
                        "end_time",
                        "is_active",
                    }
                    unsupported = set(data) - allowed_fields

                    if unsupported:
                        return response(
                            400,
                            {
                                "message": "Unsupported fields",
                                "fields": sorted(unsupported),
                            },
                        )

                    if method == "PUT":
                        missing = {
                            "day_of_week",
                            "start_time",
                            "end_time",
                        } - set(data)

                        if missing:
                            return response(
                                400,
                                {
                                    "message": "PUT requires all schedule fields",
                                    "fields": sorted(missing),
                                },
                            )

                    merged = {
                        **old,
                        **data,
                        "timezone": IST_TIMEZONE,
                    }

                day = merged["day_of_week"]

                if type(day) is not int or not 0 <= day <= 6:
                    logger.warning("Invalid day_of_week value")
                    return response(
                        400,
                        {
                            "message": (
                                "day_of_week must be 0-6 "
                                "(Monday-Sunday)"
                            )
                        },
                    )

                start, end, valid_range = validate_time_range(
                    merged["start_time"],
                    merged["end_time"],
                )

                if not valid_range:
                    logger.warning("Invalid availability time range")
                    return response(
                        400,
                        {
                            "message": (
                                "Use HH:MM or HH:MM:SS and ensure "
                                "start_time is earlier than end_time"
                            )
                        },
                    )

                is_active = merged["is_active"]
                if not isinstance(is_active, bool):
                    return response(
                        400,
                        {"message": "is_active must be true or false"},
                    )

                # Check duplicate and overlapping active windows.
                if is_active:
                    conflict_id = find_availability_conflict(
                        cur,
                        doctor_id,
                        day,
                        start,
                        end,
                        exclude_pattern_id=(
                            pattern_id if method in ("PUT", "PATCH") else None
                        ),
                    )

                    if conflict_id is not None:
                        logger.warning(
                            "Availability overlaps existing pattern: "
                            "doctor_id=%s, existing_pattern_id=%s",
                            doctor_id,
                            conflict_id,
                        )
                        conn.rollback()
                        return response(
                            409,
                            {
                                "message": (
                                    "This availability overlaps an existing "
                                    "active availability pattern"
                                ),
                                "conflicting_pattern_id": conflict_id,
                            },
                        )

                if method == "POST":
                    cur.execute(
                        """
                        INSERT INTO availability_patterns
                        (
                            doctor_id,
                            day_of_week,
                            start_time,
                            end_time,
                            timezone,
                            is_active
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            doctor_id,
                            day,
                            start,
                            end,
                            IST_TIMEZONE,
                            is_active,
                        ),
                    )

                    new_pattern_id = cur.lastrowid
                    conn.commit()

                    logger.info(
                        "Availability pattern created: pattern_id=%s",
                        new_pattern_id,
                    )

                    return response(
                        201,
                        {
                            "pattern_id": new_pattern_id,
                            "message": "Availability pattern created",
                            "timezone": IST_TIMEZONE,
                        },
                    )

                cur.execute(
                    """
                    UPDATE availability_patterns
                    SET day_of_week = %s,
                        start_time = %s,
                        end_time = %s,
                        timezone = %s,
                        is_active = %s
                    WHERE doctor_id = %s AND pattern_id = %s
                    """,
                    (
                        day,
                        start,
                        end,
                        IST_TIMEZONE,
                        is_active,
                        doctor_id,
                        pattern_id,
                    ),
                )

                conn.commit()
                logger.info(
                    "Availability pattern updated: pattern_id=%s",
                    pattern_id,
                )

                return response(
                    200,
                    {
                        "message": "Availability pattern updated",
                        "timezone": IST_TIMEZONE,
                    },
                )

        except Exception:
            conn.rollback()
            logger.exception("Availability pattern database operation failed")
            raise

    except Exception:
        logger.exception("Availability patterns Lambda failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if conn is not None:
            try:
                conn.close()
                logger.info("Database connection closed")
            except Exception:
                logger.exception("Database connection close failed")
