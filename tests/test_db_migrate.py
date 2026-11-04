import sys

from fakes import FakeConn, load_lambda, wire

import common  # noqa: E402  (lambdas/ is on sys.path via fakes)

module = load_lambda("db_migrate")


# ------------------------------------------------------------ derive_city ----
def test_derive_city_from_typical_addresses():
    cases = {
        "Road No 5, Banjara Hills, Hyderabad, Telangana 500034": "Hyderabad",
        "Apollo Hospital, Jubilee Hills, Hyderabad - 500033": "Hyderabad",
        "12 MG Road, Bengaluru, Karnataka, India": "Bengaluru",
        "Connaught Place, Delhi 110001": "Delhi",
        "Navi Mumbai, Maharashtra": "Navi Mumbai",
        "Hyderabad": "Hyderabad",
    }
    for address, city in cases.items():
        assert common.derive_city(address) == city, address


def test_derive_city_returns_none_when_unsure():
    for address in ("", None, "500001", "Flat 4B, Sector 12",
                    "Shop 12, Some long street name, near the old market", 42):
        assert common.derive_city(address) is None, address


# --------------------------------------------------------------- helpers ----
def test_ensure_column_is_idempotent():
    present = FakeConn([("information_schema.COLUMNS", [{"present": 1}])])
    assert module.ensure_column(present.cur, "doctor_profiles", "hospital_city", "VARCHAR(100) NULL") is False
    assert not present.cur.sql_containing("ALTER TABLE")

    absent = FakeConn([])
    assert module.ensure_column(absent.cur, "doctor_profiles", "hospital_city", "VARCHAR(100) NULL") is True
    assert absent.cur.sql_containing("ALTER TABLE doctor_profiles ADD COLUMN hospital_city")


def test_ensure_index_is_idempotent():
    present = FakeConn([("information_schema.STATISTICS", [{"present": 1}])])
    assert module.ensure_index(present.cur, "doctor_profiles", "idx_x", ("city",)) is False
    assert not present.cur.sql_containing("CREATE INDEX")

    absent = FakeConn([])
    assert module.ensure_index(absent.cur, "doctor_profiles", "idx_x", ("a", "b")) is True
    assert absent.cur.sql_containing("CREATE INDEX idx_x ON doctor_profiles (a, b)")


def test_identifiers_are_validated():
    conn = FakeConn([])
    for bad in ("x; DROP TABLE users", "a b", "", "x`y"):
        try:
            module.ensure_column(conn.cur, "doctor_profiles", bad, "INT")
        except ValueError:
            continue
        raise AssertionError(bad)
    assert not conn.cur.sql_containing("ALTER")


# ------------------------------------------------------------- migration ----
def test_migration_adds_columns_backfills_and_creates_all_indexes():
    conn = FakeConn([
        ("WHERE hospital_city IS NULL", [
            {"user_id": 1, "hospital_address": "Jubilee Hills, Hyderabad, Telangana 500033"},
            {"user_id": 2, "hospital_address": "Flat 4B, Sector 12"},  # unsure -> skipped
        ]),
    ])
    changes = module.migrate_m4_doctor_search(conn.cur)
    assert changes["columns_added"] == ["doctor_profiles.hospital_city", "doctor_profiles.name_last_word"]
    assert changes["cities_backfilled"] == 1
    assert len(changes["indexes_added"]) == len(module.DOCTOR_SEARCH_INDEXES) == 5
    updates = conn.cur.sql_containing("UPDATE doctor_profiles SET hospital_city")
    assert len(updates) == 1 and updates[0][1] == ("Hyderabad", 1)
    # never overwrites a city a doctor already set
    assert "hospital_city IS NULL" in updates[0][0]


def test_migration_on_an_already_migrated_database_changes_nothing():
    conn = FakeConn([
        ("information_schema.COLUMNS", [{"present": 1}]),
        ("information_schema.STATISTICS", [{"present": 1}]),
    ])
    changes = module.migrate_m4_doctor_search(conn.cur)
    assert changes == {"columns_added": [], "indexes_added": [], "cities_backfilled": 0}
    assert not conn.cur.sql_containing("ALTER") and not conn.cur.sql_containing("CREATE INDEX")


def test_search_indexes_match_the_query_columns():
    names = {name: cols for name, cols in module.DOCTOR_SEARCH_INDEXES}
    assert names["idx_doctor_spec_city"] == ("specialization", "hospital_city")
    assert set(sum(names.values(), ())) >= {
        "specialization", "hospital_city", "hospital_name", "full_name", "name_last_word"}


def test_handler_applies_pending_and_records_it():
    state = {"done": []}
    conn = FakeConn([
        ("GET_LOCK", [{"got": 1}]),
        ("SELECT migration_id FROM schema_migrations", []),
        ("information_schema.COLUMNS", [{"present": 1}]),
        ("information_schema.STATISTICS", [{"present": 1}]),
    ])
    module.dbconn = lambda c: conn
    module.config = lambda: {}
    result = module.handler({}, None)
    assert result["applied"] == ["m4_001_doctor_search"]
    assert all(result["doctor_search_indexes"].values())
    assert conn.cur.sql_containing("INSERT INTO schema_migrations")
    assert conn.cur.sql_containing("RELEASE_LOCK") and conn.closed


def test_handler_skips_already_applied_and_supports_dry_run():
    conn = FakeConn([
        ("GET_LOCK", [{"got": 1}]),
        ("SELECT migration_id FROM schema_migrations", [{"migration_id": "m4_001_doctor_search"}]),
        ("information_schema.STATISTICS", [{"present": 1}]),
    ])
    module.dbconn = lambda c: conn
    module.config = lambda: {}
    result = module.handler({"dry_run": True}, None)
    assert result["applied"] == [] and result["already_applied"] == ["m4_001_doctor_search"]
    assert not conn.cur.sql_containing("ALTER")


def test_handler_refuses_when_lock_is_held():
    conn = FakeConn([("GET_LOCK", [{"got": 0}])])
    module.dbconn = lambda c: conn
    module.config = lambda: {}
    try:
        module.handler({}, None)
    except RuntimeError:
        assert conn.closed
        return
    raise AssertionError("expected RuntimeError")
