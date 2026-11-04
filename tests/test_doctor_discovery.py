import itertools
import json
from datetime import date, datetime, timezone

from fakes import load_lambda, wire

module = load_lambda("doctor_discovery")
UTC = timezone.utc


def body(result):
    return json.loads(result["body"])


# ---------------------------------------------------------------- params ----
def test_like_prefix_escapes_wildcards():
    assert module.like_prefix("abc") == "abc%"
    assert module.like_prefix("50%_x!") == "50!%!_x!!%"


def test_defaults_and_trimming():
    parsed = module.parse_search_params({"city": "  Hyd ", "name": " "})
    assert parsed == {"filters": {"city": "Hyd"}, "page": 1, "page_size": 10}


def test_specialization_spelling_is_accepted():
    parsed = module.parse_search_params({"specialization": "ENT"})
    assert parsed["filters"] == {"specialisation": "ENT"}


def test_page_bounds():
    for bad in ({"page": "0"}, {"page": "x"}, {"page_size": "51"}, {"page_size": "0"}):
        try:
            module.parse_search_params(bad)
        except module.BadRequest:
            continue
        raise AssertionError(f"accepted {bad}")
    assert module.parse_search_params({"page": "3", "page_size": "50"})["page"] == 3


def test_overlong_filter_rejected():
    try:
        module.parse_search_params({"city": "x" * 101})
    except module.BadRequest:
        return
    raise AssertionError


# ------------------------------------------------------------------- SQL ----
SAMPLE = {"specialisation": "Cardiology", "city": "Hyd", "hospital": "Apollo", "name": "Sharma"}


def test_every_filter_combination_builds_valid_parameterised_sql():
    keys = list(SAMPLE)
    count = 0
    for size in range(0, 5):
        for combo in itertools.combinations(keys, size):
            filters = {k: SAMPLE[k] for k in combo}
            where, params = module.build_search_where(filters)
            assert where.count("%s") == len(params)
            # user-supplied text is only ever a bound parameter
            for value in SAMPLE.values():
                assert value not in where
            assert ("dp.specialization = %s" in where) == ("specialisation" in combo)
            assert ("hospital_city LIKE" in where) == ("city" in combo)
            assert ("hospital_name LIKE" in where) == ("hospital" in combo)
            assert ("full_name LIKE" in where) == ("name" in combo)
            count += 1
    assert count == 16  # 15 filter combinations + no filter


def test_no_leading_wildcards_so_indexes_stay_usable():
    where, params = module.build_search_where(SAMPLE)
    like_params = [p for p in params if p.endswith("%")]
    assert like_params and all(not p.startswith("%") for p in like_params)


def test_injection_text_stays_in_parameters():
    evil = "x'; DROP TABLE users; --"
    where, params = module.build_search_where({"hospital": evil})
    assert "DROP" not in where
    assert evil.replace("_", "!_") + "%" in params or any("DROP" in p for p in params)


def test_pagination_offset_and_limit():
    _, (sql, params) = module.build_search_queries({}, page=3, page_size=20)
    assert sql.endswith("ORDER BY dp.full_name, dp.user_id LIMIT %s OFFSET %s")
    assert params[-2:] == [20, 40]


# ---------------------------------------------------------------- search ----
DOCS = [
    {"doctor_id": 5, "full_name": "Anil Sharma", "specialization": "Cardiology",
     "qualifications": '["MBBS", "MD"]', "experience_years": 12,
     "hospital_name": "Apollo", "hospital_city": "Hyderabad",
     "languages_spoken": '["English"]'},
    {"doctor_id": 9, "full_name": "Bela Shah", "specialization": "Cardiology",
     "qualifications": ["MBBS"], "experience_years": 3,
     "hospital_name": "Apollo", "hospital_city": "Hyderabad", "languages_spoken": []},
]


def search_event(**params):
    return {"httpMethod": "GET", "resource": "/doctors", "queryStringParameters": params or None}


def test_search_returns_page_metadata_fees_and_next_slot():
    wire(module, [
        ("COUNT(*)", [{"total": 23}]),
        ("FROM doctor_profiles dp JOIN users", DOCS),
        ("FROM doctor_consultation_fees", [{"doctor_id": 5, "fee_from": "300.00", "fee_to": "900.00"}]),
        ("FROM appointment_slots", [{"doctor_id": 5, "next_slot": datetime(2026, 11, 5, 4, 30)}]),
    ])
    result = module.handler(search_event(city="hyd", page="2", page_size="10"), None)
    data = body(result)
    assert result["statusCode"] == 200
    assert (data["page"], data["page_size"], data["total"], data["total_pages"]) == (2, 10, 23, 3)
    assert data["has_next"] is True
    first, second = data["items"]
    assert first["qualifications"] == ["MBBS", "MD"]
    assert (first["fee_from"], first["fee_to"]) == ("300.00", "900.00")
    assert first["next_available"] == "2026-11-05T10:00:00+05:30"
    assert second["fee_from"] is None and second["next_available"] is None


def test_search_with_no_results_skips_follow_up_queries():
    conn = wire(module, [("COUNT(*)", [{"total": 0}])])
    data = body(module.handler(search_event(name="zzz"), None))
    assert data["items"] == [] and data["total_pages"] == 0 and data["has_next"] is False
    assert not conn.cur.sql_containing("doctor_consultation_fees")


def test_search_hides_private_fields():
    wire(module, [("COUNT(*)", [{"total": 1}]), ("FROM doctor_profiles dp JOIN users", DOCS[:1])])
    text = module.handler(search_event(), None)["body"]
    for private in ("licence", "email", "password", "verification"):
        assert private not in text.lower()


def test_search_validation_error_is_400_without_db():
    conn = wire(module, [])
    assert module.handler(search_event(page="abc"), None)["statusCode"] == 400
    assert conn.cur.executed == []


def test_only_get_is_allowed():
    assert module.handler({"httpMethod": "POST", "resource": "/doctors"}, None)["statusCode"] == 405


# --------------------------------------------------------------- profile ----
PROFILE_ROW = {
    "doctor_id": 5, "full_name": "Anil Sharma", "specialization": "Cardiology",
    "qualifications": '["MBBS"]', "experience_years": 12, "hospital_name": "Apollo",
    "hospital_address": "Jubilee Hills, Hyderabad", "hospital_city": "Hyderabad",
    "languages_spoken": '["English", "Hindi"]', "bio": "Hello", "photo_key": None,
    "timezone": "Asia/Kolkata",
}


def profile_event(doctor_id="5"):
    return {"httpMethod": "GET", "resource": "/doctors/{doctor_id}",
            "pathParameters": {"doctor_id": doctor_id}}


def test_public_profile_lists_types_and_fees():
    wire(module, [
        ("FROM doctor_profiles dp JOIN users", [PROFILE_ROW]),
        ("FROM doctor_consultation_fees f", [
            {"consultation_type_id": 1, "name": "Quick Consult", "duration_minutes": 15, "fee": "300.00"},
            {"consultation_type_id": 2, "name": "Standard Consult", "duration_minutes": 30, "fee": "500.00"},
        ]),
    ])
    result = module.handler(profile_event(), None)
    data = body(result)
    assert result["statusCode"] == 200
    assert [t["fee"] for t in data["consultation_types"]] == ["300.00", "500.00"]
    assert data["fee_from"] == "300.00"
    assert data["languages_spoken"] == ["English", "Hindi"]
    for private in ("licence", "email", "photo_key", "verification"):
        assert private not in result["body"].lower()


def test_unknown_doctor_is_404_and_bad_id_is_400():
    wire(module, [])
    assert module.handler(profile_event("77"), None)["statusCode"] == 404
    for bad in ("abc", "0", "-1"):
        assert module.handler(profile_event(bad), None)["statusCode"] == 400


# ------------------------------------------------------------ availability ----
TODAY = date(2026, 11, 4)


def test_default_range_is_next_seven_days():
    start, end, type_id = module.parse_slot_range({}, TODAY)
    assert (start, end, type_id) == (date(2026, 11, 4), date(2026, 11, 10), None)


def test_range_validation():
    for bad in ({"from": "04-11-2026"}, {"from": "2026-11-10", "to": "2026-11-01"},
                {"from": "2026-11-01", "to": "2026-12-15"}, {"consultation_type_id": "x"}):
        try:
            module.parse_slot_range(bad, TODAY)
        except module.BadRequest:
            continue
        raise AssertionError(f"accepted {bad}")
    start, end, _ = module.parse_slot_range({"from": "2026-11-01", "to": "2026-12-01"}, TODAY)
    assert (end - start).days + 1 == 31  # exactly the maximum is allowed


def test_ist_day_bounds_convert_to_utc():
    lo, hi = module.ist_day_to_utc_bounds(date(2026, 11, 5), date(2026, 11, 5))
    assert lo == datetime(2026, 11, 4, 18, 30) and hi == datetime(2026, 11, 5, 18, 30)


NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def slot(slot_id, start_utc, status, type_id=2):
    return {"slot_id": slot_id, "consultation_type_id": type_id,
            "consultation_type": "Standard Consult", "duration_minutes": 30,
            "fee": "500.00", "slot_start": start_utc, "slot_end": start_utc.replace(minute=30),
            "status": status}


def test_free_seats_per_slot_and_summary():
    conn = wire(module, [
        ("FROM appointment_slots s", [
            slot(1, datetime(2026, 11, 5, 4, 0), "AVAILABLE"),   # 09:30 IST
            slot(2, datetime(2026, 11, 5, 5, 0), "BOOKED"),
            slot(3, datetime(2026, 11, 6, 4, 0), "AVAILABLE"),
        ]),
        ("FROM doctor_blackout_dates", []),
    ])
    cur = conn.cur
    data = module.slot_availability(cur, 5, date(2026, 11, 4), date(2026, 11, 10), None, NOW)
    free = {i["slot_id"]: i["seats_free"] for i in data["items"]}
    assert free == {1: 1, 2: 0, 3: 1}
    assert all(i["seats_total"] == 1 for i in data["items"])
    assert data["summary"] == {"total_slots": 3, "slots_with_free_seats": 2, "free_seats": 2}
    assert data["days"] == [
        {"date": "2026-11-05", "total_slots": 2, "free_seats": 1},
        {"date": "2026-11-06", "total_slots": 1, "free_seats": 1},
    ]
    assert data["items"][0]["slot_start"] == "2026-11-05T09:30:00+05:30"


def test_only_available_and_booked_slots_are_queried_and_past_is_clipped():
    conn = wire(module, [("FROM appointment_slots s", []), ("FROM doctor_blackout_dates", [])])
    module.slot_availability(conn.cur, 5, date(2026, 11, 4), date(2026, 11, 4), None, NOW)
    sql, params = conn.cur.sql_containing("FROM appointment_slots s")[0]
    assert "IN ('AVAILABLE', 'BOOKED')" in sql
    # day starts 2026-11-03 18:30 UTC but "now" is 06:00 UTC: never offer started slots
    assert params[1] == datetime(2026, 11, 4, 6, 0)


def test_blackout_days_are_hidden():
    wire_conn = wire(module, [
        ("FROM appointment_slots s", [slot(1, datetime(2026, 11, 5, 4, 0), "AVAILABLE"),
                                      slot(2, datetime(2026, 11, 6, 4, 0), "AVAILABLE")]),
        ("FROM doctor_blackout_dates", [{"blackout_date": date(2026, 11, 5)}]),
    ])
    data = module.slot_availability(wire_conn.cur, 5, date(2026, 11, 4), date(2026, 11, 10), None, NOW)
    assert [i["slot_id"] for i in data["items"]] == [2]


def test_consultation_type_filter_is_a_bound_parameter():
    conn = wire(module, [("FROM appointment_slots s", []), ("FROM doctor_blackout_dates", [])])
    module.slot_availability(conn.cur, 5, date(2026, 11, 4), date(2026, 11, 10), 2, NOW)
    sql, params = conn.cur.sql_containing("FROM appointment_slots s")[0]
    assert "s.consultation_type_id = %s" in sql and params[-1] == 2


def test_slots_for_unknown_doctor_is_404():
    wire(module, [])
    ev = {"httpMethod": "GET", "resource": "/doctors/{doctor_id}/slots",
          "pathParameters": {"doctor_id": "5"}, "queryStringParameters": None}
    assert module.handler(ev, None)["statusCode"] == 404


def test_slots_bad_range_is_400():
    ev = {"httpMethod": "GET", "resource": "/doctors/{doctor_id}/slots",
          "pathParameters": {"doctor_id": "5"},
          "queryStringParameters": {"from": "2026-01-01", "to": "2026-06-01"}}
    assert module.handler(ev, None)["statusCode"] == 400


# ----------------------------------------------------------------- filters ----
def test_filter_options_lists_values_in_use():
    wire(module, [
        ("SELECT DISTINCT dp.specialization", [{"value": "Cardiology"}, {"value": "Orthopedics"}]),
        ("SELECT DISTINCT dp.hospital_city", [{"value": "Hyderabad"}, {"value": "Mysuru"}]),
    ])
    result = module.handler({"httpMethod": "GET", "resource": "/doctors/filters"}, None)
    assert result["statusCode"] == 200
    assert body(result) == {"specializations": ["Cardiology", "Orthopedics"],
                            "cities": ["Hyderabad", "Mysuru"]}


def test_filters_route_is_not_mistaken_for_a_doctor_id():
    # "filters" must not hit the {doctor_id} validation (which would 400)
    wire(module, [])
    ev = {"httpMethod": "GET", "path": "/dev/doctors/filters"}
    assert module.handler(ev, None)["statusCode"] == 200
