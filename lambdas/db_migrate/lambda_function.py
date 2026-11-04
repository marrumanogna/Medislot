
"""Idempotent schema migrations, run by CI after every deployment.

Why this exists
---------------
scehma/schema.sql is a clean-install script, but the live RDS instance already
holds data and has drifted from that file over earlier milestones.

Milestone 4 needs a new column and several indexes on doctor_profiles.
The migration applies those changes defensively after verifying that the
required base table exists.

Features:
* Information-schema checks before adding columns and indexes.
* Applied migrations recorded in schema_migrations.
* MySQL advisory lock prevents concurrent migrations.
* Diagnostic mode reports the existing database schema without modifying it.
* Any migration failure raises an exception so CI fails.
 
Invoke with:
    {"dry_run": true}
to list pending migrations.

Invoke with:
    {"diagnostic": true}
to inspect the live database.
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
    _ident(table)
    _ident(column)

    if column_exists(cur, table, column):
        return False

    cur.execute(
        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
    )
    return True


def ensure_index(cur, table, index, columns):
    """CREATE INDEX unless an index of that name exists. True if created."""
    _ident(table)
    _ident(index)

    if index_exists(cur, table, index):
        return False

    cols = ", ".join(_ident(column) for column in columns)

    cur.execute(
        f"CREATE INDEX {index} ON {table} ({cols})"
    )
    return True


# --------------------------------------------------------------------------- #
# Milestone 4: Doctor discovery
# --------------------------------------------------------------------------- #

# Index choices are justified in docs/milestone-4.md.
# Every doctor search filter is either an equality or a prefix match.

DOCTOR_SEARCH_INDEXES = (
    # Specialization + city
    ("idx_doctor_spec_city", ("specialization", "hospital_city")),

    # City-only searches
    ("idx_doctor_city", ("hospital_city",)),

    # Hospital name searches
    ("idx_doctor_hospital_name", ("hospital_name",)),

    # Full-name searches
    ("idx_doctor_full_name", ("full_name",)),

    # Surname searches
    ("idx_doctor_surname", ("name_last_word",)),
)


def migrate_m4_doctor_search(cur):
    changes = {
        "columns_added": [],
        "indexes_added": [],
        "cities_backfilled": 0,
    }

    # Add hospital_city column.
    if ensure_column(
        cur,
        "doctor_profiles",
        "hospital_city",
        "VARCHAR(100) NULL AFTER hospital_address",
    ):
        changes["columns_added"].append(
            "doctor_profiles.hospital_city"
        )

    # Add generated surname column.
    if ensure_column(
        cur,
        "doctor_profiles",
        "name_last_word",
        "VARCHAR(201) GENERATED ALWAYS AS "
        "(SUBSTRING_INDEX(full_name, ' ', -1)) STORED",
    ):
        changes["columns_added"].append(
            "doctor_profiles.name_last_word"
        )

    # Backfill missing cities without overwriting existing values.
    cur.execute(
        """
        SELECT user_id, hospital_address
        FROM doctor_profiles
        WHERE hospital_city IS NULL
        """
    )

    for row in cur.fetchall():
        city = derive_city(row["hospital_address"])

        if city:
            cur.execute(
                """
                UPDATE doctor_profiles
                SET hospital_city = %s
                WHERE user_id = %s
                  AND hospital_city IS NULL
                """,
                (city, row["user_id"]),
            )

            changes["cities_backfilled"] += 1

    # Create expected search indexes.
    for index, columns in DOCTOR_SEARCH_INDEXES:
        if ensure_index(
            cur,
            "doctor_profiles",
            index,
            columns,
        ):
            changes["indexes_added"].append(index)

    return changes


MIGRATIONS = (
    ("m4_001_doctor_search", migrate_m4_doctor_search),
)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def verify_doctor_search_indexes(cur):
    """Report which expected doctor search indexes are present."""

    return {
        index: index_exists(
            cur,
            "doctor_profiles",
            index,
        )
        for index, _ in DOCTOR_SEARCH_INDEXES
    }


# --------------------------------------------------------------------------- #
# Database diagnostic
# --------------------------------------------------------------------------- #

def get_database_diagnostic(cur):
    """
    Read-only inspection of the connected database.

    This function does not create, update, or delete database objects.
    """

    # Database name
    cur.execute(
        "SELECT DATABASE() AS database_name"
    )
    database = cur.fetchone()

    # Existing tables
    cur.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )

    tables = [
        row["TABLE_NAME"]
        for row in cur.fetchall()
    ]

    doctor_profiles_exists = (
        "doctor_profiles" in tables
    )

    # Users count
    users_count = None
    user_columns = []

    if "users" in tables:
        cur.execute(
            "SELECT COUNT(*) AS users_count FROM users"
        )
        users_count = cur.fetchone()["users_count"]

        # Existing users table structure
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
            ORDER BY ORDINAL_POSITION
            """
        )

        user_columns = cur.fetchall()

    # Applied migration records
    applied_migrations = []

    if "schema_migrations" in tables:
        cur.execute(
            """
            SELECT migration_id
            FROM schema_migrations
            ORDER BY migration_id
            """
        )

        applied_migrations = [
            row["migration_id"]
            for row in cur.fetchall()
        ]

    return {
        "status": "diagnostic_completed",
        "database": database["database_name"],
        "doctor_profiles_exists": doctor_profiles_exists,
        "users_count": users_count,
        "user_columns": user_columns,
        "applied_migrations": applied_migrations,
        "tables": tables,
    }


# --------------------------------------------------------------------------- #
# Migration runner
# --------------------------------------------------------------------------- #

def handler(event, context):
    event = event or {}

    cid = event.get("correlationId")
    dry_run = bool(event.get("dry_run"))

    conn = None
    locked = False

    try:
        conn = dbconn(config())

        # Diagnostic mode: read-only inspection.
        if event.get("diagnostic"):
            with conn.cursor() as cur:
                result = get_database_diagnostic(cur)

            return result

        # Normal migration mode.
        with conn.cursor() as cur:

            # Acquire advisory lock.
            cur.execute(
                "SELECT GET_LOCK(%s, 30) AS got",
                (LOCK_NAME,),
            )

            locked = cur.fetchone()["got"] == 1

            if not locked:
                raise RuntimeError(
                    "Another migration is already running"
                )

            # Ensure migration tracking table exists.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id VARCHAR(100) NOT NULL,
                    applied_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (migration_id)
                ) ENGINE=InnoDB
                """
            )

            # Read completed migrations.
            cur.execute(
                "SELECT migration_id FROM schema_migrations"
            )

            done = {
                row["migration_id"]
                for row in cur.fetchall()
            }

            pending = [
                migration
                for migration in MIGRATIONS
                if migration[0] not in done
            ]

            result = {
                "status": "ok",
                "dry_run": dry_run,
                "already_applied": sorted(done),
                "applied": [],
                "pending": (
                    [name for name, _ in pending]
                    if dry_run
                    else []
                ),
                "changes": {},
            }

            # Apply pending migrations.
            if not dry_run:
                for name, migrate in pending:
                    logger.info(
                        "Applying migration %s",
                        name,
                    )

                    result["changes"][name] = migrate(cur)

                    cur.execute(
                        """
                        INSERT INTO schema_migrations (migration_id)
                        VALUES (%s)
                        """,
                        (name,),
                    )

                    conn.commit()

                    result["applied"].append(name)

            # Verify expected doctor search indexes.
            result["doctor_search_indexes"] = (
                verify_doctor_search_indexes(cur)
            )

        # Verify completed migration.
        if (
            not dry_run
            and not all(
                result["doctor_search_indexes"].values()
            )
        ):
            raise RuntimeError(
                "Migration finished but expected indexes are missing: "
                f"{result['doctor_search_indexes']}"
            )

        emit(
            "info",
            "schema_migration_completed",
            cid,
            **{
                "applied": result["applied"],
                "dry_run": dry_run,
            },
        )

        return result

    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        emit(
            "error",
            "schema_migration_failed",
            cid,
        )

        raise

    finally:
        if conn:
            try:
                if locked:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT RELEASE_LOCK(%s)",
                            (LOCK_NAME,),
                        )
            except Exception:
                pass

            conn.close()
