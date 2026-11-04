"""Shared fixtures for the integration tests (run against the deployed API).

Set MEDISLOT_API_URL to the stage URL, e.g.
    https://abc123.execute-api.us-east-1.amazonaws.com/dev
CI reads it from the auth stack's ApiUrl output. Without it these tests skip,
so a plain `pytest` on a laptop never touches AWS.

Every run creates its own uniquely tagged accounts (emails it-<tag>-...@example.test),
so tests never depend on - or clash with - data already in RDS.
"""

import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

BASE_URL = os.environ.get("MEDISLOT_API_URL", "").rstrip("/")
PASSWORD = "Passw0rd-" + uuid.uuid4().hex[:6]
IST = ZoneInfo("Asia/Kolkata")

if not BASE_URL:
    pytest.skip("MEDISLOT_API_URL not set; skipping integration tests",
                allow_module_level=True)


class Api:
    def call(self, method, path, token=None, json=None, params=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return requests.request(method, f"{BASE_URL}/{path.lstrip('/')}",
                                headers=headers, json=json, params=params, timeout=30)


@pytest.fixture(scope="session")
def api():
    return Api()


@pytest.fixture(scope="session")
def tag():
    return uuid.uuid4().hex[:8]


class Account:
    def __init__(self, email, token, refresh, user_id):
        self.email, self.token, self.refresh, self.user_id = email, token, refresh, user_id


def _login(api, email):
    r = api.call("POST", "auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["accessToken"], d.get("refreshToken"), d["userId"]


def _register_patient(api, tag, label, **extra):
    email = f"it-{tag}-{label}@example.test"
    payload = {"firstName": "Pat", "lastName": f"{label.title()}{tag}", "email": email,
               "password": PASSWORD, "confirmPassword": PASSWORD, **extra}
    r = api.call("POST", "auth/register/patient", json=payload)
    assert r.status_code == 201, r.text
    return Account(email, *_login(api, email))


@pytest.fixture(scope="session")
def patient_a(api, tag):
    return _register_patient(api, tag, "a", city="Mysuru", phone="9876543210")


@pytest.fixture(scope="session")
def patient_b(api, tag):
    return _register_patient(api, tag, "b", city="Pune")


@pytest.fixture
def fresh_patient(api, tag):
    """A throwaway patient for tests that log out or delete things."""
    return _register_patient(api, tag, "tmp" + uuid.uuid4().hex[:4])


def _register_doctor(api, tag, label, specialization, hospital, city):
    email = f"it-{tag}-{label}@example.test"
    payload = {
        "firstName": f"Doc{label.title()}", "lastName": f"Surname{tag}",
        "email": email, "password": PASSWORD, "confirmPassword": PASSWORD,
        "licenceNumber": f"IT-{tag}-{label}", "specialization": specialization,
        "qualifications": ["MBBS"], "experienceYears": 7,
        "hospitalName": hospital, "hospitalAddress": f"1 Test Road, {city}, Karnataka 560001",
        "hospitalCity": city, "languagesSpoken": ["English"],
    }
    r = api.call("POST", "auth/register/doctor", json=payload)
    assert r.status_code == 201, r.text
    token, refresh, user_id = _login(api, email)
    return Account(email, token, refresh, user_id), payload


@pytest.fixture(scope="session")
def doctors(api, tag):
    """Two doctors sharing a (unique) city, differing in specialisation/hospital."""
    city = f"Testville{tag}"
    d1, p1 = _register_doctor(api, tag, "one", "Cardiology", f"Alpha Heart {tag}", city)
    d2, p2 = _register_doctor(api, tag, "two", "Dermatology", f"Beta Skin {tag}", city)

    # Doctor one gets a fee and two published slots tomorrow (IST).
    r = api.call("PUT", "doctor/consultation-types/1", d1.token, json={"fee": 500})
    assert r.status_code == 200, r.text
    day = (datetime.now(IST) + timedelta(days=1)).date().isoformat()
    for start in ("10:00", "11:00"):
        r = api.call("POST", "doctor/slots", d1.token,
                     json={"action": "create", "date": day,
                           "start_time": start, "consultation_type_id": 1})
        assert r.status_code == 201, r.text
    r = api.call("POST", "doctor/slots", d1.token, json={"action": "publish", "date": day})
    assert r.status_code == 200, r.text

    return {"city": city, "day": day, "one": d1, "two": d2, "p1": p1, "p2": p2}
