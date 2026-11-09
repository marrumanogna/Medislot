import logging
from decimal import Decimal, InvalidOperation

from common import config, dbconn, response, body

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def validate_fee(value):
    """Validate fee for DECIMAL(10,2)."""

    if isinstance(value, bool) or value is None:
        raise ValueError("fee must be a valid non-negative number")

    try:
        fee = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("fee must be a valid number")

    if not fee.is_finite() or fee < 0:
        raise ValueError("fee must be non-negative")

    if fee > Decimal("99999999.99"):
        raise ValueError("fee exceeds the maximum allowed amount")

    if fee.as_tuple().exponent < -2:
        raise ValueError("fee can have at most 2 decimal places")

    return fee


def validate_type_id(type_id):
    """Validate consultation type ID."""

    try:
        type_id = int(type_id)
        if type_id <= 0:
            raise ValueError()
        return type_id
    except (TypeError, ValueError):
        raise ValueError("Invalid consultation type ID")


def handler(event, context):

    logger.info("Consultation Types Lambda execution started")

    auth = (
        (event.get("requestContext") or {}).get("authorizer") or {}
    )

    if auth.get("role") != "DOCTOR" or not auth.get("userId"):
        logger.warning("Access denied: Doctor authorization required")
        return response(403, {"message": "Doctor access required"})

    try:
        doctor_id = int(auth["userId"])

        if doctor_id <= 0:
            raise ValueError("Invalid doctor ID")

        method = event.get("httpMethod", "GET").upper()

        path_params = event.get("pathParameters") or {}
        type_id = path_params.get("type_id")

        logger.info(
            "Request received: doctor_id=%s, method=%s, type_id=%s",
            doctor_id,
            method,
            type_id
        )

        # Only GET, PUT and PATCH are supported.
        if method not in ("GET", "PUT", "PATCH"):
            logger.warning("Method not allowed: %s", method)
            return response(405, {"message": "Method not allowed"})

        data = body(event) if method in ("PUT", "PATCH") else {}

        if method in ("PUT", "PATCH"):
            if not isinstance(data, dict):
                return response(400, {"message": "Invalid JSON body"})

            # Doctors choose which catalogue types they offer and set a fee.
            allowed_fields = {"fee", "is_active", "capacity"}

            if "fee" not in data or not set(data.keys()) <= allowed_fields:
                return response(
                    400,
                    {"message": "Send fee, and optionally is_active and capacity"}
                )

            is_active = data.get("is_active", True)
            if not isinstance(is_active, bool):
                return response(
                    400,
                    {"message": "is_active must be true or false"}
                )

            capacity = data.get("capacity")
            if capacity is not None and (
                isinstance(capacity, bool) or not isinstance(capacity, int) or not 2 <= capacity <= 10
            ):
                return response(400, {"message": "capacity must be a whole number from 2 to 10"})

            try:
                fee = validate_fee(data["fee"])
            except ValueError as exc:
                return response(400, {"message": str(exc)})

            if not type_id:
                return response(
                    400,
                    {"message": "type_id is required"}
                )

            try:
                type_id = validate_type_id(type_id)
            except ValueError as exc:
                return response(400, {"message": str(exc)})

        elif type_id:
            try:
                type_id = validate_type_id(type_id)
            except ValueError as exc:
                return response(400, {"message": str(exc)})

        c = config()
        conn = dbconn(c)

        logger.info("Database connection established")

        try:
            with conn.cursor() as cur:

                # -----------------------------------------
                # GET: Fetch all consultation types
                # -----------------------------------------

                if method == "GET":

                    logger.info(
                        "Fetching consultation types for doctor_id=%s",
                        doctor_id
                    )

                    if type_id:

                        cur.execute(
                            """
                            SELECT
                                ct.consultation_type_id,
                                ct.name,
                                NULL AS description,
                                ct.duration_minutes,
                                ct.is_active,
                                ct.capacity AS max_capacity,
                                COALESCE(dcf.capacity, ct.capacity) AS capacity,
                                CAST(dcf.fee AS CHAR) AS fee,
                                dcf.is_active AS doctor_is_active
                            FROM consultation_catalogue ct

                            LEFT JOIN doctor_consultation_fees dcf
                                ON ct.consultation_type_id =
                                   dcf.consultation_type_id
                                AND dcf.doctor_id = %s

                            WHERE ct.consultation_type_id = %s
                            """,
                            (doctor_id, type_id)
                        )

                        row = cur.fetchone()

                        if not row:
                            return response(
                                404,
                                {"message": "Consultation type not found"}
                            )

                        logger.info(
                            "Consultation type fetched: type_id=%s",
                            type_id
                        )

                        return response(200, row)

                    cur.execute(
                        """
                        SELECT
                            ct.consultation_type_id,
                            ct.name,
                            NULL AS description,
                            ct.duration_minutes,
                            ct.is_active,
                            ct.capacity AS max_capacity,
                            COALESCE(dcf.capacity, ct.capacity) AS capacity,
                            CAST(dcf.fee AS CHAR) AS fee,
                            dcf.is_active AS doctor_is_active

                        FROM consultation_catalogue ct

                        LEFT JOIN doctor_consultation_fees dcf
                            ON ct.consultation_type_id =
                               dcf.consultation_type_id
                            AND dcf.doctor_id = %s

                        ORDER BY ct.consultation_type_id
                        """,
                        (doctor_id,)
                    )

                    rows = cur.fetchall()

                    logger.info(
                        "Fetched %s consultation types for doctor_id=%s",
                        len(rows),
                        doctor_id
                    )

                    return response(
                        200,
                        {"items": rows}
                    )

                # -----------------------------------------
                # PUT / PATCH: Update doctor's fee only
                # -----------------------------------------

                if method in ("PUT", "PATCH"):

                    logger.info(
                        "Updating consultation fee: doctor_id=%s, type_id=%s",
                        doctor_id,
                        type_id
                    )

                    # Verify that the consultation type exists
                    # and is active in the static catalogue.

                    cur.execute(
                        """
                        SELECT consultation_type_id, capacity
                        FROM consultation_catalogue
                        WHERE consultation_type_id = %s
                          AND is_active = 1
                        """,
                        (type_id,)
                    )

                    consultation_type = cur.fetchone()

                    if not consultation_type:
                        return response(
                            404,
                            {"message": "Active consultation type not found"}
                        )

                    if capacity is not None and int(consultation_type["capacity"]) < 2:
                        return response(
                            400,
                            {"message": "Seat capacity can only be set for Health Camp Sessions"}
                        )

                    # Insert a fee if it does not exist.
                    # Otherwise update only the existing fee.
                    # The unique key prevents duplicate doctor/type records.

                    cur.execute(
                        """
                        INSERT INTO doctor_consultation_fees
                            (doctor_id, consultation_type_id, fee, is_active, capacity)
                        VALUES (%s, %s, %s, %s, %s)

                        ON DUPLICATE KEY UPDATE
                            fee = VALUES(fee),
                            is_active = VALUES(is_active),
                            capacity = COALESCE(VALUES(capacity), capacity)
                        """,
                        (doctor_id, type_id, fee, 1 if is_active else 0, capacity)
                    )

                    conn.commit()

                    logger.info(
                        "Consultation fee saved successfully: "
                        "doctor_id=%s, type_id=%s",
                        doctor_id,
                        type_id
                    )

                    return response(
                        200,
                        {
                            "message": "Consultation fee updated successfully",
                            "doctor_id": doctor_id,
                            "consultation_type_id": type_id,
                            "fee": str(fee),
                            "is_active": is_active
                        }
                    )

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()
            logger.info("Database connection closed")

    except ValueError as exc:
        logger.warning("Invalid request: %s", str(exc))
        return response(400, {"message": str(exc)})

    except Exception:
        logger.exception("Consultation Types Lambda failed")
        return response(500, {"message": "Internal server error"})