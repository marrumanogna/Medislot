import logging

from common import config, dbconn, emit


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info("RDS Constraint Check Lambda execution started")

    cid = (event or {}).get("correlationId")
    conn = None

    logger.info("Checking RDS database connection and appointment slots constraint")

    try:
        conn = dbconn(config())

        logger.info("RDS database connection established")

        with conn.cursor() as cur:
            logger.info("Checking database connectivity")

            cur.execute("SELECT 1 AS connected")
            connection_row = cur.fetchone()

            logger.info("Database connectivity check completed")

            logger.info("Checking unique constraint on appointment_slots")

            cur.execute("""
                SELECT
                    INDEX_NAME,
                    NON_UNIQUE,
                    GROUP_CONCAT(
                        COLUMN_NAME ORDER BY SEQ_IN_INDEX
                    ) AS indexed_columns
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'appointment_slots'
                  AND INDEX_NAME = 'uq_doctor_consultation_slot'
                GROUP BY INDEX_NAME, NON_UNIQUE
            """)
            index_row = cur.fetchone()

            if index_row:
                logger.info("Unique constraint index found")
            else:
                logger.warning("Unique constraint index not found")

            logger.info("Fetching appointment slot counts")

            cur.execute("""
                SELECT
                    COUNT(*) AS total_slots,
                    COALESCE(SUM(status = 'AVAILABLE'), 0)
                        AS available_slots,
                    COALESCE(SUM(status = 'BOOKED'), 0)
                        AS booked_slots
                FROM appointment_slots
            """)
            slot_counts = cur.fetchone()

            logger.info("Appointment slot counts retrieved successfully")

        constraint_exists = (
            index_row is not None
            and index_row["NON_UNIQUE"] == 0
        )

        result = {
            "connected": bool(connection_row["connected"] == 1),
            "unique_constraint_exists": constraint_exists,
            "constraint_name": (
                index_row["INDEX_NAME"] if index_row else None
            ),
            "is_unique": (
                index_row["NON_UNIQUE"] == 0 if index_row else False
            ),
            "columns": (
                index_row["indexed_columns"] if index_row else None
            ),
            "total_slots": int(slot_counts["total_slots"]),
            "available_slots": int(slot_counts["available_slots"]),
            "booked_slots": int(slot_counts["booked_slots"]),
        }

        logger.info(
            "RDS check completed: connected=%s, unique_constraint_exists=%s",
            result["connected"],
            result["unique_constraint_exists"]
        )

        logger.info(
            "Slot counts: total=%s, available=%s, booked=%s",
            result["total_slots"],
            result["available_slots"],
            result["booked_slots"]
        )

        emit("info", "rds_constraint_check_completed", cid, **result)

        logger.info("RDS Constraint Check Lambda completed successfully")

        return {"statusCode": 200, "body": result}

    except Exception:
        logger.exception("RDS Constraint Check Lambda failed")

        emit("error", "rds_constraint_check_failed", cid)
        raise

    finally:
        if conn:
            conn.close()
            logger.info("RDS database connection closed")