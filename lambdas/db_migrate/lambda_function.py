"""Idempotent schema migrations, run by CI after every deployment.

Why this exists
---------------
scehma/schema.sql is a clean-install script, but the live RDS instance already
holds data and has drifted from that file over earlier milestones. Milestone 4
needs a new column and several indexes on a populated table, so the change is
applied here, defensively:

* every step inspects information_schema first and is a no-op when the object
  already exists (so it is safe on a fresh install built from schema.sql, on
  the live database, and when CI re-runs it);
* applied migrations are recorded in schema_migrations;
* a MySQL advisory lock stops two deployments migrating at once;
* any failure raises, so `aws lambda invoke` reports FunctionError and CI fails.

Invoke with {"dry_run": true} to list pending migrations without changing
anything.
"""

import logging
import re

from common import config, dbconn, derive_city, emit

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LOCK_NAME = "medislot_schema_migrate"
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


# --------------------------------------------------------------------------- #
# Idempotent DDL helpers
# --------------------------------------------------------------------------- #
def _ident(name):
    """Table/column/index names are interpolated into DDL, so validate them."""
    if not _IDENT.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def column_exists(cur, table, column):
    cur.execute(
        """
        SELECT 1 AS present
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def index_exists(cur, table, index):
    cur.execute(
        """
        SELECT 1 AS present
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        LIMIT 1
        """,
        (table, index),
    )
    return cur.fetchone() is not None


def ensure_column(cur, table, column, definition):
    """ALTER TABLE ... ADD COLUMN unless it already exists. True if added."""
    _ident(table), _ident(column)
    if column_exists(cur, table, column):
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def ensure_index(cur, table, index, columns):
    """CREATE INDEX unless an index of that name exists. True if created."""
    _ident(table), _ident(index)
    if index_exists(cur, table, index):
        return False
    cols = ", ".join(_ident(c) for c in columns)
    cur.execute(f"CREATE INDEX {index} ON {table} ({cols})")
    return True


# --------------------------------------------------------------------------- #
# Milestone 4: doctor discovery
# --------------------------------------------------------------------------- #
# Index choices are justified in docs/milestone-4.md. In short, every doctor
# search filter is either an equality or a case-insensitive *prefix* match,
# so each can be served by a B-tree range scan.
DOCTOR_SEARCH_INDEXES = (
    # specialisation (equality) [+ city (prefix)] - the most common combination.
    # Its left-most column also serves specialisation-only searches.
    ("idx_doctor_spec_city", ("specialization", "hospital_city")),
    # city-only searches (specialisation is not the leading column there).
    ("idx_doctor_city", ("hospital_city",)),
    ("idx_doctor_hospital_name", ("hospital_name",)),
    # name search: prefix of the full name ...
    ("idx_doctor_full_name", ("full_name",)),
    # ... or prefix of the surname (last word of the full name).
    ("idx_doctor_surname", ("name_last_word",)),
)


def migrate_m4_doctor_search(cur):
    changes = {"columns_added": [], "indexes_added": [], "cities_backfilled": 0}

    # The city the search filters on. doctor_profiles only had a free-text
    # hospital_address, which no B-tree index can serve for "city = ?".
    if ensure_column(
        cur,
        "doctor_profiles",
        "hospital_city",
        "VARCHAR(100) NULL AFTER hospital_address",
    ):
        changes["columns_added"].append("doctor_profiles.hospital_city")

    # Surname = last word of full_name, materialised so it can be indexed.
    if ensure_column(
        cur,
        "doctor_profiles",
        "name_last_word",
        "VARCHAR(201) GENERATED ALWAYS AS "
        "(SUBSTRING_INDEX(full_name, ' ', -1)) STORED",
    ):
        changes["columns_added"].append("doctor_profiles.name_last_word")

    # Best-effort backfill for doctors registered before this column existed.
    # Never overwrites a city a doctor already set.
    cur.execute(
        "SELECT user_id, hospital_address FROM doctor_profiles "
        "WHERE hospital_city IS NULL"
    )
    for row in cur.fetchall():
        city = derive_city(row["hospital_address"])
        if city:
            cur.execute(
                "UPDATE doctor_profiles SET hospital_city = %s "
                "WHERE user_id = %s AND hospital_city IS NULL",
                (city, row["user_id"]),
            )
            changes["cities_backfilled"] += 1

    for index, columns in DOCTOR_SEARCH_INDEXES:
        if ensure_index(cur, "doctor_profiles", index, columns):
            changes["indexes_added"].append(index)

    return changes


MIGRATIONS = (("m4_001_doctor_search", migrate_m4_doctor_search),)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def verify_doctor_search_indexes(cur):
    """Report which of the expected search indexes are present."""
    return {
        index: index_exists(cur, "doctor_profiles", index)
        for index, _ in DOCTOR_SEARCH_INDEXES
    }


def handler(event, context):
    event = event or {}
    cid = event.get("correlationId")
    dry_run = bool(event.get("dry_run"))
    conn = None
    locked = False

    try:
        conn = dbconn(config())
                if event.get("diagnostic"):
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE() AS database_name")
                database = cur.fetchone()

                cur.execute("""
                    SELECT TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                    ORDER BY TABLE_NAME
                """)
                tables = [row["TABLE_NAME"] for row in cur.fetchall()]

            return {
                "database": database["database_name"],
                "doctor_profiles_exists": "doctor_profiles" in tables,
                "tables": tables
            }

        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 30) AS got", (LOCK_NAME,))
            locked = cur.fetchone()["got"] == 1
            if not locked:
                raise RuntimeError("Another migration is already running")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id VARCHAR(100) NOT NULL,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (migration_id)
                ) ENGINE=InnoDB
                """
            )
            cur.execute("SELECT migration_id FROM schema_migrations")
            done = {row["migration_id"] for row in cur.fetchall()}

            pending = [m for m in MIGRATIONS if m[0] not in done]
            result = {
                "status": "ok",
                "dry_run": dry_run,
                "already_applied": sorted(done),
                "applied": [],
                "pending": [name for name, _ in pending] if dry_run else [],
                "changes": {},
            }

            if not dry_run:
                for name, migrate in pending:
                    logger.info("Applying migration %s", name)
                    result["changes"][name] = migrate(cur)
                    cur.execute(
                        "INSERT INTO schema_migrations (migration_id) "
                        "VALUES (%s)",
                        (name,),
                    )
                    conn.commit()
                    result["applied"].append(name)

            result["doctor_search_indexes"] = verify_doctor_search_indexes(cur)

        if not dry_run and not all(result["doctor_search_indexes"].values()):
            raise RuntimeError(
                "Migration finished but expected indexes are missing: "
                f"{result['doctor_search_indexes']}"
            )

        emit("info", "schema_migration_completed", cid, **{
            "applied": result["applied"],
            "dry_run": dry_run,
        })
        return result

    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        emit("error", "schema_migration_failed", cid)
        raise

    finally:
        if conn:
            try:
                if locked:
                    with conn.cursor() as cur:
                        cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            except Exception:
                pass
            conn.close()
