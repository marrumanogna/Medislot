
from common import config, dbconn, emit


def handler(event, context):
    cid = (event or {}).get("correlationId")
    conn = None

    try:
        c = config()
        conn = dbconn(c)

        with conn.cursor() as cur:
            # Keep the existing connectivity check.
            cur.execute("SELECT 1 AS connected")
            connection_row = cur.fetchone()

            # Read-only check of the live database index.
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

        constraint_exists = (
            index_row is not None
            and index_row["NON_UNIQUE"] == 0
        )

        emit(
            "info",
            "rds_constraint_check_completed",
            cid
        )

        return {
            "statusCode": 200,
            "body": {
                "connected": bool(
                    connection_row
                    and connection_row["connected"] == 1
                ),
                "unique_constraint_exists": constraint_exists,
                "constraint_name": (
                    index_row["INDEX_NAME"] if index_row else None
                ),
                "is_unique": (
                    index_row["NON_UNIQUE"] == 0
                    if index_row else False
                ),
                "columns": (
                    index_row["indexed_columns"] if index_row else None
                )
            }
        }

    except Exception:
        emit("error", "rds_constraint_check_failed", cid)
        raise

    finally:
        if conn:
            conn.close()

