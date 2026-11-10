
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Provide dummy local AWS credentials before importing boto3 clients.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

# Make lambdas/common.py importable.
LAMBDA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "lambdas")
)
sys.path.insert(0, LAMBDA_ROOT)

# Load the doctor profile Lambda under a unique module name.
# This avoids confusing it with the author's lambda_function module.
LAMBDA_PATH = os.path.join(
    LAMBDA_ROOT, "doctor_profile", "lambda_function.py"
)

with patch("boto3.client", return_value=MagicMock()), \
     patch("boto3.resource", return_value=MagicMock()):
    spec = importlib.util.spec_from_file_location(
        "doctor_profile_lambda", LAMBDA_PATH
    )
    doctor_profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor_profile)


def test_missing_doctor_authentication():
    event = {
        "httpMethod": "GET",
        "requestContext": {"authorizer": {}},
    }

    result = doctor_profile.handler(event, None)

    assert result["statusCode"] == 403


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"specialization": "", "qualifications": "MBBS"},
        {"specialization": "Cardiology", "qualifications": None},
        {"specialization": "  ", "qualifications": "MBBS"},
    ],
)
def test_required_profile_fields_rejected(data):
    values, error = doctor_profile.validate_profile_data(
        data, "POST", "test-correlation"
    )

    assert values is None
    assert error["statusCode"] == 400


@pytest.mark.parametrize(
    "fee",
    [-1, True, "not-a-number", "12.345"],
)
def test_invalid_consultation_fee_rejected(fee):
    data = {
        "specialization": "Cardiology",
        "qualifications": "MBBS",
        "consultation_fee": fee,
    }

    values, error = doctor_profile.validate_profile_data(
        data, "POST", "test-correlation"
    )

    assert values is None
    assert error["statusCode"] == 400


def test_invalid_timezone_rejected():
    data = {
        "specialization": "Cardiology",
        "qualifications": "MBBS",
        "timezone": "Not/A_Timezone",
    }

    values, error = doctor_profile.validate_profile_data(
        data, "POST", "test-correlation"
    )

    assert values is None
    assert error["statusCode"] == 400


def test_valid_profile_data_accepted():
    data = {
        "licence_number": "TEST-LIC-1001",
        "specialization": "Cardiology",
        "qualifications": ["MBBS", "MD"],
        "experience_years": 5,
        "consultation_fee": "500.00",
        "timezone": "Asia/Kolkata",
        "hospital_name": "City Hospital",
        "hospital_address": "Hyderabad, Telangana",
        "languages_spoken": ["English", "Telugu"]
    }
    values, error = doctor_profile.validate_profile_data(
        data, "POST", "test-correlation"
    )

    assert error is None
    assert values["specialization"] == "Cardiology"
    assert values["qualifications"] == '["MBBS", "MD"]'
    assert values["experience_years"] == 5
    assert values["timezone"] == "Asia/Kolkata"


def test_patch_requires_at_least_one_field():
    values, error = doctor_profile.validate_profile_data(
        {}, "PATCH", "test-correlation"
    )

    assert values is None
    assert error["statusCode"] == 400


def test_unsupported_field_rejected():
    data = {
        "specialization": "Cardiology",
        "qualifications": "MBBS",
        "user_id": 999,
    }

    values, error = doctor_profile.validate_profile_data(
        data, "POST", "test-correlation"
    )

    assert values is None
    assert error["statusCode"] == 400
