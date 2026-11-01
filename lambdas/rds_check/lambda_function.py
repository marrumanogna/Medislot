from common import config, dbconn, emit


def handler(event, context):
    cid = (event or {}).get("correlationId")
    conn = None

    try:
        conn = dbconn(config())

        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS connected")
            connection_row = cur.fetchone()

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

        emit("info", "rds_constraint_check_completed", cid, **result)

        return {"statusCode": 200, "body": result}

    except Exception:
        emit("error", "rds_constraint_check_failed", cid)
        raise

    finally:
        if conn:
            conn.close()
