"""Milestone 4 checkpoint, automated: profiles, isolation, search, slots."""

import itertools
import re


def ids_for(api, **params):
    """Every doctor id matching the filters, walking all pages."""
    found, page = [], 1
    while page <= 50:
        r = api.call("GET", "doctors", params={**params, "page": page, "page_size": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        found += [d["doctor_id"] for d in body["items"]]
        if not body["has_next"]:
            return found
        page += 1
    raise AssertionError("too many pages")


# --------------------------------------------------------------- profile ----
def test_patient_profile_crud(api, fresh_patient):
    p = fresh_patient
    # Registration already created a profile.
    r = api.call("GET", "patient/profile", p.token)
    assert r.status_code == 200, r.text
    assert r.json()["email"] == p.email

    r = api.call("PUT", "patient/profile", p.token,
                 json={"dateOfBirth": "1990-04-02", "gender": "Female", "city": "Mysuru",
                       "phone": "+91 98765 43210"})
    assert r.status_code == 200, r.text
    assert r.json()["city"] == "Mysuru" and r.json()["dateOfBirth"] == "1990-04-02"

    r = api.call("PATCH", "patient/profile", p.token, json={"address": "1 MG Road"})
    assert r.status_code == 200 and r.json()["address"] == "1 MG Road"
    assert r.json()["city"] == "Mysuru"  # untouched by PATCH

    # PUT replaces: omitted optional fields are cleared
    r = api.call("PUT", "patient/profile", p.token, json={"city": "Pune"})
    assert r.json()["address"] is None and r.json()["city"] == "Pune"

    assert api.call("PATCH", "patient/profile", p.token, json={"phone": "abc"}).status_code == 400
    assert api.call("PATCH", "patient/profile", p.token, json={"userId": 1}).status_code == 400
    assert api.call("PATCH", "patient/profile", p.token, json={"dateOfBirth": "2999-01-01"}).status_code == 400

    assert api.call("DELETE", "patient/profile", p.token).status_code == 200
    assert api.call("GET", "patient/profile", p.token).status_code == 404
    assert api.call("DELETE", "patient/profile", p.token).status_code == 404

    r = api.call("POST", "patient/profile", p.token, json={"fullName": "Pat Again", "city": "Delhi"})
    assert r.status_code == 201 and r.json()["fullName"] == "Pat Again"
    assert api.call("POST", "patient/profile", p.token, json={"fullName": "x"}).status_code == 409


# ------------------------------------------------------------- isolation ----
def test_patient_cannot_read_another_patients_profile(api, patient_a, patient_b):
    a = api.call("GET", "patient/profile", patient_a.token).json()
    b = api.call("GET", "patient/profile", patient_b.token).json()
    assert a["patientId"] != b["patientId"]

    # B asks for A's profile by id: rejected, and nothing about A is leaked.
    r = api.call("GET", f"patient/profile/{a['patientId']}", patient_b.token)
    assert r.status_code == 403
    assert a["email"] not in r.text and a["fullName"] not in r.text

    # An id that does not exist looks exactly the same (no enumeration).
    r2 = api.call("GET", "patient/profile/99999999", patient_b.token)
    assert r2.status_code == 403 and r2.json()["message"] == r.json()["message"]

    # Own id works.
    assert api.call("GET", f"patient/profile/{b['patientId']}", patient_b.token).status_code == 200

    # There is no way to write to another patient's row by id.
    for method in ("PUT", "PATCH", "DELETE"):
        r = api.call(method, f"patient/profile/{a['patientId']}", patient_b.token,
                     json={"city": "Hacked"})
        assert r.status_code in (403, 405), (method, r.status_code)
    assert api.call("GET", "patient/profile", patient_a.token).json()["city"] != "Hacked"


def test_profile_requires_a_valid_patient_token(api, doctors):
    assert api.call("GET", "patient/profile").status_code == 401
    assert api.call("GET", "patient/profile", "not-a-token").status_code in (401, 403)
    # a doctor's token is rejected by the authorizer on /patient/ routes
    assert api.call("GET", "patient/profile", doctors["one"].token).status_code == 403


def test_revoked_token_is_rejected(api, fresh_patient):
    p = fresh_patient
    assert api.call("GET", "patient/profile", p.token).status_code == 200
    r = api.call("POST", "auth/logout", p.token, json={"refreshToken": p.refresh})
    assert r.status_code in (200, 204), r.text
    assert api.call("GET", "patient/profile", p.token).status_code == 401


# ---------------------------------------------------------------- search ----
def test_search_is_public_and_paginated(api, doctors):
    r = api.call("GET", "doctors", params={"city": doctors["city"], "page_size": 1})
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["total_pages"], body["page"], body["has_next"]) == (2, 2, 1, True)
    assert len(body["items"]) == 1

    page2 = api.call("GET", "doctors", params={"city": doctors["city"], "page_size": 1, "page": 2}).json()
    assert len(page2["items"]) == 1 and page2["has_next"] is False
    assert page2["items"][0]["doctor_id"] != body["items"][0]["doctor_id"]

    beyond = api.call("GET", "doctors", params={"city": doctors["city"], "page_size": 1, "page": 9}).json()
    assert beyond["items"] == []


def test_search_never_exposes_private_fields(api, doctors):
    text = api.call("GET", "doctors", params={"city": doctors["city"]}).text.lower()
    for private in ("licence", "email", "password", "it-"):
        assert private not in text


def test_every_filter_combination(api, doctors):
    one, two = doctors["one"], doctors["two"]
    d1 = {"specialisation": "Cardiology", "city": doctors["city"][:10],
          "hospital": doctors["p1"]["hospitalName"][:8], "name": f"Surname{doctors['p1']['licenceNumber'][3:11]}"}
    # which doctor each filter value matches (semantics: spec=equals, rest=prefix, case-insensitive)
    def matches(doc_payload, spec_label, key, value):
        v = value.lower()
        if key == "specialisation": return doc_payload["specialization"].lower() == v
        if key == "city": return doc_payload["hospitalCity"].lower().startswith(v)
        if key == "hospital": return doc_payload["hospitalName"].lower().startswith(v)
        if key == "name":
            full = f"{doc_payload['firstName']} {doc_payload['lastName']}".lower()
            return full.startswith(v) or doc_payload["lastName"].lower().startswith(v)

    keys = list(d1)
    combos = [c for n in range(1, 5) for c in itertools.combinations(keys, n)]
    assert len(combos) == 15
    for combo in combos:
        filters = {k: d1[k] for k in combo}
        result = ids_for(api, **filters)
        for doc, payload in ((one, doctors["p1"]), (two, doctors["p2"])):
            expected = all(matches(payload, None, k, v) for k, v in filters.items())
            assert (int(doc.user_id) in result) == expected, (combo, doc.email)


def test_search_is_case_insensitive_and_name_matches_surname(api, doctors):
    one = int(doctors["one"].user_id)
    assert one in ids_for(api, city=doctors["city"].upper())
    assert one in ids_for(api, name=doctors["p1"]["lastName"][:7].lower())  # surname prefix
    assert one in ids_for(api, name="docone")                               # first-name prefix
    assert one in ids_for(api, specialization="cardiology", city=doctors["city"])  # US spelling


def test_wildcards_are_literal_and_bad_input_is_rejected(api, doctors):
    r = api.call("GET", "doctors", params={"city": "%"})
    assert r.status_code == 200 and r.json()["total"] == 0
    assert api.call("GET", "doctors", params={"city": "x'; DROP TABLE users; --"}).status_code == 200
    for bad in ({"page": "0"}, {"page_size": "0"}, {"page_size": "500"}, {"page": "abc"}):
        assert api.call("GET", "doctors", params=bad).status_code == 400


def test_search_result_shows_fee_and_next_slot(api, doctors):
    items = api.call("GET", "doctors", params={"city": doctors["city"], "specialisation": "Cardiology"}).json()["items"]
    assert len(items) == 1
    assert items[0]["fee_from"] == "500.00"
    assert items[0]["next_available"] and doctors["day"] in items[0]["next_available"]


# -------------------------------------------------------- public profile ----
def test_public_doctor_profile(api, doctors):
    doc_id = doctors["one"].user_id
    r = api.call("GET", f"doctors/{doc_id}")  # no token
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["full_name"].endswith(doctors["p1"]["lastName"])
    assert d["hospital_city"] == doctors["city"]
    types = {t["consultation_type_id"]: t for t in d["consultation_types"]}
    assert types[1]["fee"] == "500.00" and types[1]["duration_minutes"] > 0
    for private in ("licence", "email", "photo_key", "verification"):
        assert private not in r.text.lower()

    assert api.call("GET", "doctors/999999999").status_code == 404
    assert api.call("GET", "doctors/abc").status_code == 400


# ---------------------------------------------------------- availability ----
def test_slot_availability_shows_free_seats(api, doctors):
    doc_id, day = doctors["one"].user_id, doctors["day"]
    r = api.call("GET", f"doctors/{doc_id}/slots", params={"from": day, "to": day})  # no token
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total_slots"] == 2 and body["summary"]["free_seats"] == 2
    assert body["days"] == [{"date": day, "total_slots": 2, "free_seats": 2}]
    for slot in body["items"]:
        assert (slot["seats_total"], slot["seats_free"]) == (1, 1)
        assert slot["fee"] == "500.00" and re.search(r"\+05:30$", slot["slot_start"])
    assert [s["slot_start"][11:16] for s in body["items"]] == ["10:00", "11:00"]

    # filters and validation
    assert api.call("GET", f"doctors/{doc_id}/slots",
                    params={"from": day, "to": day, "consultation_type_id": 3}).json()["items"] == []
    assert api.call("GET", f"doctors/{doc_id}/slots", params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 400
    assert api.call("GET", f"doctors/{doc_id}/slots", params={"from": "nope"}).status_code == 400
    assert api.call("GET", "doctors/999999999/slots").status_code == 404


def test_doctor_without_slots_has_empty_availability(api, doctors):
    r = api.call("GET", f"doctors/{doctors['two'].user_id}/slots")
    assert r.status_code == 200 and r.json()["items"] == []


def test_filter_options_list_what_exists(api, doctors):
    r = api.call("GET", "doctors/filters")  # public
    assert r.status_code == 200
    body = r.json()
    assert "Cardiology" in body["specializations"] and "Dermatology" in body["specializations"]
    assert doctors["city"] in body["cities"]
