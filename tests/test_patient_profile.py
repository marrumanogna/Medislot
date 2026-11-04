import json
from datetime import date, datetime

from fakes import load_lambda, wire

module = load_lambda("patient_profile")

ME = {
    "patient_id": 7, "user_id": 12, "full_name": "Asha Rao",
    "date_of_birth": date(1990, 4, 2), "gender": "Female",
    "phone": "+91 98765 43210", "address": "1 MG Road", "city": "Mysuru",
    "emergency_contact": "9876500000",
    "created_at": datetime(2026, 11, 2, 9, 0), "updated_at": datetime(2026, 11, 3, 9, 0),
    "email": "asha@example.com",
}
ACTIVE = ("FROM users WHERE user_id", [{"user_id": 12}])


def event(method, body=None, patient_id=None, user_id="12", role="PATIENT"):
    ev = {
        "httpMethod": method,
        "requestContext": {"authorizer": {"userId": user_id, "role": role}},
        "body": json.dumps(body) if body is not None else None,
    }
    if patient_id is not None:
        ev["pathParameters"] = {"patient_id": str(patient_id)}
    return ev


def status(result):
    return result["statusCode"]


def payload(result):
    return json.loads(result["body"])


# ---------------------------------------------------------------- access ----
def test_rejects_missing_authorizer_context():
    assert status(module.handler({"httpMethod": "GET"}, None)) == 403


def test_rejects_doctor_role():
    assert status(module.handler(event("GET", role="DOCTOR"), None)) == 403


def test_rejects_unsupported_method():
    assert status(module.handler(event("OPTIONS"), None)) == 405


# ------------------------------------------------------------ validation ----
def v(data, method="PUT"):
    return module.validate_profile(data, method, today=date(2026, 11, 4))


def expect_error(data, method, field=None):
    try:
        v(data, method)
    except module.ValidationError as exc:
        assert field is None or field in exc.fields
        return
    raise AssertionError("expected ValidationError")


def test_unknown_and_identity_fields_are_rejected():
    # A client must never be able to name the row it writes to.
    for bad in ({"userId": 99}, {"patientId": 99}, {"user_id": 99}, {"email": "x@y.z"}):
        expect_error(bad, "PATCH")


def test_post_requires_full_name():
    expect_error({"city": "Pune"}, "POST", "fullName")
    expect_error({"fullName": "  "}, "POST", "fullName")


def test_patch_requires_a_field():
    expect_error({}, "PATCH")


def test_put_clears_omitted_optional_fields_but_keeps_name():
    values = v({"city": "Pune"}, "PUT")
    assert values["city"] == "Pune"
    assert values["phone"] is None and values["address"] is None
    assert "full_name" not in values


def test_patch_only_touches_supplied_fields():
    assert v({"city": " Pune "}, "PATCH") == {"city": "Pune"}


def test_blank_strings_become_null():
    assert v({"phone": "  "}, "PATCH") == {"phone": None}


def test_date_of_birth_rules():
    assert v({"dateOfBirth": "1990-04-02"}, "PATCH")["date_of_birth"] == date(1990, 4, 2)
    for bad in ("02-04-1990", "1990-4-2", "2027-01-01", "1850-01-01", 19900402):
        expect_error({"dateOfBirth": bad}, "PATCH", "dateOfBirth")


def test_phone_rules():
    for good in ("9876543210", "+91 98765-43210", "(080) 2345 6789"):
        assert v({"phone": good}, "PATCH")["phone"] == good
    for bad in ("abc", "12", "98765x43210", "9" * 25):
        expect_error({"phone": bad}, "PATCH", "phone")


def test_non_text_values_rejected():
    expect_error({"city": 5}, "PATCH", "city")


# ----------------------------------------------------------------- reads ----
def test_get_own_profile():
    wire(module, [ACTIVE, ("FROM patients p", [ME])])
    result = module.handler(event("GET"), None)
    body = payload(result)
    assert status(result) == 200
    assert body["patientId"] == 7 and body["email"] == "asha@example.com"
    assert body["dateOfBirth"] == "1990-04-02"


def test_get_without_profile_is_404():
    wire(module, [ACTIVE])
    assert status(module.handler(event("GET"), None)) == 404


# ------------------------------------------------------------- isolation ----
def test_reading_another_patients_profile_is_forbidden():
    conn = wire(module, [ACTIVE, ("FROM patients p", [ME])])
    result = module.handler(event("GET", patient_id=8), None)
    assert status(result) == 403
    assert "Asha" not in result["body"] and "asha@example.com" not in result["body"]
    # The requested id (8) is never used to look anything up.
    for _sql, params in conn.cur.executed:
        assert 8 not in (params or ())


def test_reading_own_profile_by_id_is_allowed():
    wire(module, [ACTIVE, ("FROM patients p", [ME])])
    assert status(module.handler(event("GET", patient_id=7), None)) == 200


def test_unknown_id_looks_the_same_as_someone_elses():
    wire(module, [ACTIVE, ("FROM patients p", [ME])])
    other = module.handler(event("GET", patient_id=8), None)
    wire(module, [ACTIVE, ("FROM patients p", [ME])])
    missing = module.handler(event("GET", patient_id=999999), None)
    assert status(other) == status(missing) == 403
    assert payload(other)["message"] == payload(missing)["message"]


def test_id_route_is_403_when_caller_has_no_profile():
    wire(module, [ACTIVE])
    assert status(module.handler(event("GET", patient_id=7), None)) == 403


def test_id_route_is_read_only():
    for method in ("PUT", "PATCH", "DELETE", "POST"):
        assert status(module.handler(event(method, {"city": "x"}, patient_id=8), None)) == 405


def test_bad_patient_id_is_400():
    for bad in ("abc", "0", "-3"):
        ev = event("GET")
        ev["pathParameters"] = {"patient_id": bad}
        assert status(module.handler(ev, None)) == 400


def test_writes_are_scoped_to_the_token_identity():
    conn = wire(module, [ACTIVE, ("FROM patients p", [ME])])
    module.handler(event("PATCH", {"city": "Pune"}, user_id="12"), None)
    updates = conn.cur.sql_containing("UPDATE patients")
    assert len(updates) == 1
    sql, params = updates[0]
    assert sql.endswith("WHERE user_id = %s") and params[-1] == 12


# ---------------------------------------------------------------- writes ----
def test_post_creates_profile_for_token_user():
    conn = wire(module, [ACTIVE, ("FROM patients p", [])])
    # first SELECT finds no profile; after the INSERT the row exists
    states = iter([[], [ME]])
    original = conn.cur.execute

    def execute(sql, params=None):
        original(sql, params)
        if "FROM patients p" in sql:
            conn.cur._rows = next(states)

    conn.cur.execute = execute
    result = module.handler(event("POST", {"fullName": "Asha Rao", "city": "Mysuru"}), None)
    assert status(result) == 201
    sql, params = conn.cur.sql_containing("INSERT INTO patients")[0]
    assert params[0] == 12 and "Asha Rao" in params


def test_post_when_profile_exists_is_409():
    conn = wire(module, [ACTIVE, ("FROM patients p", [ME])])
    assert status(module.handler(event("POST", {"fullName": "Asha"}), None)) == 409
    assert not conn.cur.sql_containing("INSERT")


def test_put_and_patch_without_profile_are_404():
    wire(module, [ACTIVE])
    assert status(module.handler(event("PUT", {"city": "x"}), None)) == 404
    wire(module, [ACTIVE])
    assert status(module.handler(event("PATCH", {"city": "x"}), None)) == 404


def test_invalid_body_never_reaches_the_database():
    conn = wire(module, [ACTIVE])
    assert status(module.handler(event("PATCH", {"phone": "nope"}), None)) == 400
    assert conn.cur.executed == []


def test_delete_removes_only_callers_row():
    conn = wire(module, [ACTIVE, ("FROM patients p", [ME])])
    assert status(module.handler(event("DELETE"), None)) == 200
    sql, params = conn.cur.sql_containing("DELETE FROM patients")[0]
    assert params == (12,) and conn.commits == 1


def test_delete_without_profile_is_404():
    wire(module, [ACTIVE])
    assert status(module.handler(event("DELETE"), None)) == 404


def test_inactive_account_is_rejected():
    wire(module, [])
    assert status(module.handler(event("GET"), None)) == 403
