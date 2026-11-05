
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
from pathlib import Path

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
# One-time base-schema recovery
# --------------------------------------------------------------------------- #

EXPECTED_INITIAL_TABLES = {"users", "schema_migrations"}
EXPECTED_FINAL_TABLES = {
    "users",
    "doctor_profiles",
    "patients",
    "consultation_catalogue",
    "doctor_consultation_fees",
    "availability_patterns",
    "doctor_blackout_dates",
    "appointment_slots",
    "specialization_options",
    "experience_options",
    "qualification_options",
    "language_options",
    "schema_migrations",
}
LEGACY_USERS_TABLE = "users_legacy_backup"

def _schema_statements(sql_text):
    """Split this repository's simple SQL install script into statements."""
    # The checked-in schema uses no stored procedures or semicolons inside
    # string literals. Remove full-line MySQL comments and split on ';'.
    sql_text = re.sub(r"(?m)^\s*--.*$", "", sql_text)
    statements = []
    for statement in sql_text.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        if re.match(r"(?is)^CREATE\s+DATABASE\b", statement):
            continue
        if re.match(r"(?is)^USE\s+", statement):
            continue
        statements.append(statement)
    return statements

def initialize_base_schema(cur, conn, schema_path=None):
    """Safely bootstrap the known-empty dev schema after explicit approval.

    Only accepts the verified starting state (users + schema_migrations, no
    rows, no foreign keys involving users). The old users table is renamed,
    never dropped. DDL is idempotent, so a partially completed run can be
    retried after inspecting the resulting table set.
    """
    cur.execute("SELECT DATABASE() AS database_name")
    database = cur.fetchone()["database_name"]
    if database != "medislot":
        raise RuntimeError(
            f"Refusing base-schema initialization in unexpected database: {database!r}"
        )

    cur.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )
    tables = {row["TABLE_NAME"] for row in cur.fetchall()}

    # First run: require exactly the known incomplete schema. A retry after a
    # previous interrupted run is allowed only when the legacy backup exists.
    first_run = tables == EXPECTED_INITIAL_TABLES
    retry_run = (
        LEGACY_USERS_TABLE in tables
        and "schema_migrations" in tables
        and tables.issubset(EXPECTED_FINAL_TABLES | {LEGACY_USERS_TABLE})
    )
    if not first_run and not retry_run:
        raise RuntimeError(
            "Refusing schema initialization: unexpected database tables "
            f"{sorted(tables)}. Expected {sorted(EXPECTED_INITIAL_TABLES)} "
            "or a recognizable interrupted initialization."
        )

    if "users" in tables:
        cur.execute("SELECT COUNT(*) AS users_count FROM users")
        users_count = cur.fetchone()["users_count"]
        if users_count != 0:
            raise RuntimeError(
                f"Refusing schema initialization: users table contains {users_count} rows"
            )

    cur.execute("SELECT COUNT(*) AS migration_count FROM schema_migrations")
    migration_count = cur.fetchone()["migration_count"]
    if migration_count != 0:
        raise RuntimeError(
            "Refusing schema initialization: schema_migrations is not empty"
        )

    if "users" in tables:
        cur.execute(
            """
            SELECT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND (TABLE_NAME = 'users' OR REFERENCED_TABLE_NAME = 'users')
              AND REFERENCED_TABLE_NAME IS NOT NULL
            LIMIT 1
            """
        )
        if cur.fetchone() is not None:
            raise RuntimeError(
                "Refusing schema initialization: foreign-key dependencies involve users"
            )

    if first_run:
        cur.execute(
            f"RENAME TABLE users TO {_ident(LEGACY_USERS_TABLE)}"
        )
        conn.commit()

    path = Path(schema_path) if schema_path else (
        Path(__file__).resolve().parent / "scehma" / "schema.sql"
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"Packaged base schema not found: {path}. Ensure deploy workflow copies scehma/schema.sql."
        )

    sql_text = path.read_text(encoding="utf-8")
    statements = _schema_statements(sql_text)
    if not statements:
        raise RuntimeError("Base schema file contains no executable SQL statements")

    for statement in statements:
        cur.execute(statement)

    conn.commit()

    cur.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )
    final_tables = {row["TABLE_NAME"] for row in cur.fetchall()}
    missing = EXPECTED_FINAL_TABLES - final_tables
    if missing:
        raise RuntimeError(
            f"Base schema initialization incomplete; missing tables: {sorted(missing)}"
        )

    return {
        "status": "base_schema_initialized",
        "database": database,
        "legacy_users_table": LEGACY_USERS_TABLE,
        "tables_created_or_verified": sorted(EXPECTED_FINAL_TABLES),
        "schema_statements_executed": len(statements),
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
    initialization = None

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

            # Explicit one-time recovery only. Ordinary CI migration events
            # never rename tables or initialize the base schema.
            if event.get("initialize_base_schema") is True:
                initialization = initialize_base_schema(cur, conn)
                logger.info("Base schema initialization result: %s", initialization)

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
                "base_schema_initialization": (
                    initialization if event.get("initialize_base_schema") is True else None
                ),
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
