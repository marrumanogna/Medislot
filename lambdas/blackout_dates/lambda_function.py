import logging
from datetime import date, datetime, timedelta

from common import config, dbconn, response, body

logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST_OFFSET = timedelta(hours=5, minutes=30)


def booked_slots_on(cur, doctor_id, day):
    """Slots (IST calendar day) that still hold confirmed patients, grouped per slot."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time()) - IST_OFFSET
    cur.execute(
        """
        SELECT a.slot_id, a.slot_start, a.slot_end, pt.full_name AS patient_name
        FROM appointments a
        LEFT JOIN patients pt ON pt.patient_id = a.patient_id
        WHERE a.doctor_id = %s AND a.status = 'CONFIRMED'
          AND a.slot_start >= %s AND a.slot_start < %s
        ORDER BY a.slot_start, a.appointment_id
        """,
        (doctor_id, start, start + timedelta(days=1)),
    )
    slots = {}
    for r in cur.fetchall() or []:
        item = slots.setdefault(int(r["slot_id"]), {
            "slot_id": int(r["slot_id"]),
            "slot_start": (r["slot_start"] + IST_OFFSET).strftime("%H:%M"),
            "slot_end": (r["slot_end"] + IST_OFFSET).strftime("%H:%M"),
            "patients": [],
        })
        item["patients"].append(r.get("patient_name") or "Patient")
    return list(slots.values())


def blocked_by_bookings(cur, doctor_id, day):
    slots = booked_slots_on(cur, doctor_id, day)
    if not slots:
        return None
    return response(409, {
        "code": "BLACKOUT_HAS_BOOKINGS",
        "message": "Patients are booked on this date. Cancel those slots first, then add the blackout date.",
        "date": day,
        "booked_slots": slots,
    })


def handler(event, context):
    logger.info("Blackout dates Lambda execution started")

    # Get authenticated doctor details from API Gateway authorizer
    auth = (
        (event.get("requestContext") or {}).get("authorizer") or {}
    )

    logger.info("Checking doctor authorization")

    if auth.get("role") != "DOCTOR" or not auth.get("userId"):
        logger.warning("Access denied: Doctor authorization required")
        return response(403, {
            "message": "Doctor access required"
        })

    conn = None

    try:
        doctor_id = int(auth["userId"])

        # Get HTTP method (REST API and HTTP API compatible)
        method = (
            event.get("httpMethod")
            or (event.get("requestContext", {}).get("http") or {}).get("method")
            or "GET"
        ).upper()

        params = event.get("pathParameters") or {}
        blackout_id = params.get("blackout_id")

        logger.info(
            "Request received: doctor_id=%s, method=%s, blackout_id=%s",
            doctor_id, method, blackout_id
        )

        # Validate blackout ID
        if blackout_id is not None:
            try:
                blackout_id = int(blackout_id)
                if blackout_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Invalid blackout_id received")
                return response(400, {
                    "message": "Invalid blackout_id"
                })

        # Read request body
        data = {}

        if method in ("POST", "PUT"):
            try:
                data = body(event)
            except (ValueError, TypeError):
                logger.warning("Invalid JSON body received")
                return response(400, {
                    "message": "Invalid JSON body"
                })

            if not isinstance(data, dict):
                logger.warning("Request body must be a JSON object")
                return response(400, {
                    "message": "Request body must be a JSON object"
                })

            # Validate blackout date
            blackout_date = data.get("blackout_date")

            try:
                parsed_date = date.fromisoformat(blackout_date)

                if parsed_date.isoformat() != blackout_date:
                    raise ValueError

            except (TypeError, ValueError):
                logger.warning("Invalid blackout_date format received")
                return response(400, {
                    "message": "blackout_date must use YYYY-MM-DD format"
                })

            # Validate reason
            reason = data.get("reason")

            if reason is not None:
                if not isinstance(reason, str) or len(reason) > 255:
                    logger.warning("Invalid blackout reason received")
                    return response(400, {
                        "message": "reason must be 255 characters or fewer"
                    })

                reason = reason.strip() or None

        # Validate supported methods
        if method not in ("GET", "POST", "PUT", "DELETE"):
            logger.warning("Method not allowed: %s", method)
            return response(405, {
                "message": "Method not allowed"
            })

        # Validate path parameters
        if method in ("PUT", "DELETE") and not blackout_id:
            logger.warning("blackout_id is required for %s", method)
            return response(400, {
                "message": "blackout_id is required"
            })

        c = config()
        conn = dbconn(c)

        logger.info("Database connection established")

        with conn.cursor() as cur:

            # --------------------------------
            # GET ALL BLACKOUT DATES
            # --------------------------------
            check_date = (event.get("queryStringParameters") or {}).get("check_date")
            if method == "GET" and check_date:
                try:
                    if date.fromisoformat(check_date).isoformat() != check_date:
                        raise ValueError
                except ValueError:
                    return response(400, {"message": "check_date must use YYYY-MM-DD format"})
                return response(200, {"date": check_date,
                                      "booked_slots": booked_slots_on(cur, doctor_id, check_date)})

            if method == "GET" and not blackout_id:

                logger.info("Fetching all blackout dates for doctor_id=%s", doctor_id)

                cur.execute(
                    """
                    SELECT blackout_id, blackout_date, reason, created_at
                    FROM doctor_blackout_dates
                    WHERE doctor_id = %s
                    ORDER BY blackout_date
                    """,
                    (doctor_id,)
                )

                rows = cur.fetchall()

                logger.info("Fetched %s blackout date(s)", len(rows))

                for row in rows:
                    if row.get("blackout_date"):
                        row["blackout_date"] = (
                            row["blackout_date"].isoformat()
                        )

                    if row.get("created_at"):
                        row["created_at"] = (
                            row["created_at"].isoformat()
                        )

                logger.info("Blackout dates list returned successfully")

                return response(200, {
                    "blackout_dates": rows
                })

            # --------------------------------
            # GET BLACKOUT DATE BY ID
            # --------------------------------
            if method == "GET" and blackout_id:

                logger.info("Fetching blackout_id=%s", blackout_id)

                cur.execute(
                    """
                    SELECT blackout_id, blackout_date, reason, created_at
                    FROM doctor_blackout_dates
                    WHERE doctor_id = %s
                    AND blackout_id = %s
                    """,
                    (doctor_id, blackout_id)
                )

                row = cur.fetchone()

                if not row:
                    logger.warning(
                        "Blackout date not found: blackout_id=%s",
                        blackout_id
                    )
                    return response(404, {
                        "message": "Blackout date not found"
                    })

                if row.get("blackout_date"):
                    row["blackout_date"] = (
                        row["blackout_date"].isoformat()
                    )

                if row.get("created_at"):
                    row["created_at"] = (
                        row["created_at"].isoformat()
                    )

                logger.info("Blackout date fetched successfully")

                return response(200, row)

            # --------------------------------
            # POST CREATE BLACKOUT DATE
            # --------------------------------
            if method == "POST":

                logger.info("Creating blackout date: %s", blackout_date)

                blocked = blocked_by_bookings(cur, doctor_id, blackout_date)
                if blocked:
                    return blocked

                cur.execute(
                    """
                    INSERT INTO doctor_blackout_dates
                    (doctor_id, blackout_date, reason)
                    VALUES (%s, %s, %s)
                    """,
                    (doctor_id, blackout_date, reason)
                )

                conn.commit()

                logger.info(
                    "Blackout date created successfully: blackout_id=%s",
                    cur.lastrowid
                )

                return response(201, {
                    "message": "Blackout date created successfully",
                    "blackout_id": cur.lastrowid,
                    "blackout_date": blackout_date,
                    "reason": reason
                })

            # --------------------------------
            # PUT UPDATE BLACKOUT DATE
            # --------------------------------
            if method == "PUT" and blackout_id:

                logger.info("Updating blackout_id=%s", blackout_id)

                # Check whether the blackout date exists
                # and belongs to the authenticated doctor
                cur.execute(
                    """
                    SELECT blackout_id
                    FROM doctor_blackout_dates
                    WHERE doctor_id = %s
                    AND blackout_id = %s
                    """,
                    (doctor_id, blackout_id)
                )

                existing = cur.fetchone()

                if not existing:
                    logger.warning(
                        "Blackout date not found for update: blackout_id=%s",
                        blackout_id
                    )
                    return response(404, {
                        "message": "Blackout date not found"
                    })

                blocked = blocked_by_bookings(cur, doctor_id, blackout_date)
                if blocked:
                    return blocked

                # Update existing blackout date
                cur.execute(
                    """
                    UPDATE doctor_blackout_dates
                    SET blackout_date = %s,
                        reason = %s
                    WHERE doctor_id = %s
                    AND blackout_id = %s
                    """,
                    (
                        blackout_date,
                        reason,
                        doctor_id,
                        blackout_id
                    )
                )

                conn.commit()

                logger.info(
                    "Blackout date updated successfully: blackout_id=%s",
                    blackout_id
                )

                return response(200, {
                    "message": "Blackout date updated successfully",
                    "blackout_id": blackout_id,
                    "blackout_date": blackout_date,
                    "reason": reason
                })

            # --------------------------------
            # DELETE BLACKOUT DATE
            # --------------------------------
            if method == "DELETE" and blackout_id:

                logger.info("Deleting blackout_id=%s", blackout_id)

                cur.execute(
                    """
                    DELETE FROM doctor_blackout_dates
                    WHERE doctor_id = %s
                    AND blackout_id = %s
                    """,
                    (doctor_id, blackout_id)
                )

                if cur.rowcount == 0:
                    logger.warning(
                        "Blackout date not found for deletion: blackout_id=%s",
                        blackout_id
                    )
                    return response(404, {
                        "message": "Blackout date not found"
                    })

                conn.commit()

                logger.info(
                    "Blackout date deleted successfully: blackout_id=%s",
                    blackout_id
                )

                return response(200, {
                    "message": "Blackout date deleted successfully",
                    "blackout_id": blackout_id
                })

    except Exception:
        if conn:
            conn.rollback()

        logger.exception("Blackout dates Lambda failed")

        return response(500, {
            "message": "Internal server error"
        })

    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed")