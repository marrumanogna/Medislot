import boto3
import pymysql

REGION = "us-east-1"
ENV = "dev"
PREFIX = f"/medislot/{ENV}"

ssm = boto3.client("ssm", region_name=REGION)

def get_param(name, decrypt=False):
    return ssm.get_parameter(
        Name=f"{PREFIX}/{name}",
        WithDecryption=decrypt
    )["Parameter"]["Value"]

conn = None

try:
    conn = pymysql.connect(
        host=get_param("db/host"),
        port=int(get_param("db/port")),
        user=get_param("db/username"),
        password=get_param("db/password", True),
        database=get_param("db/name"),
        connect_timeout=5,
        read_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

    with conn.cursor() as cur:
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
              AND INDEX_NAME = 'uq_doctor_slot'
            GROUP BY INDEX_NAME, NON_UNIQUE
        """)
        row = cur.fetchone()

    if row and row["NON_UNIQUE"] == 0:
        print("PASS: uq_doctor_slot exists and is unique")
        print("Columns:", row["indexed_columns"])
    else:
        print("FAIL: uq_doctor_slot is missing or not unique")

finally:
    if conn:
        conn.close()
