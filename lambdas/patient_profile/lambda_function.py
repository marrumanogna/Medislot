import logging
import re
from datetime import date
from common import config, dbconn, body, response, emit, correlation

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# JSON (camelCase, Connecting frontend names to database names)
FIELD_COLUMNS = {
    "fullName": "full_name",
    "dateOfBirth": "date_of_birth",
    "gender": "gender",
    "phone": "phone",
    "address": "address",
    "city": "city",
    "emergencyContact": "emergency_contact",
}
STRING_LIMITS = {
    "fullName": 150,
    "gender": 20,
    "phone": 20,
    "address": 1000,
    "city": 100,
    "emergencyContact": 20,
}
PHONE_FIELDS = ("phone", "emergencyContact"
"")
# It can contain spaces, brackets and hyphens,+ or (,digits.
PHONE_RE = re.compile(r"^[+(]?[0-9][0-9 ()\-]{5,17}[0-9]$")
MIN_DOB = date(1900, 1, 1)

PROFILE_SELECT = """
    SELECT p.patient_id, p.user_id, p.full_name, p.date_of_birth, p.gender,
           p.phone, p.address, p.city, p.emergency_contact,
           p.created_at, p.updated_at, u.email
    FROM patients p
    JOIN users u ON u.user_id = p.user_id
    WHERE p.user_id = %s
"""


class ValidationError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.message = message
        self.fields = fields or []


def error_response(status, message, cid, **extra):
    return response(status, {"message": message, "correlationId": cid, **extra})


def authenticated_patient(event):
    """User id from the authorizer context, or None if not a patient."""
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    if auth.get("role") != "PATIENT":
        return None
    try:
        user_id = int(auth.get("userId"))
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


# --------------------------------------------------------------------------- #
# Validation (pure functions; no I/O)
# --------------------------------------------------------------------------- #
def _clean_optional_text(field, value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text", [field])
    value = value.strip()
    if not value:
        return None
    if len(value) > STRING_LIMITS[field]:
        raise ValidationError(
            f"{field} must not exceed {STRING_LIMITS[field]} characters", [field]
        )
    if field in PHONE_FIELDS and not PHONE_RE.match(value):
        raise ValidationError(
            f"{field} must be a valid phone number (7-20 characters)", [field]
        )
    return value


def validate_profile(data, method, today=None):
    if not isinstance(data, dict):
        raise ValidationError("Invalid JSON body")

    unsupported = sorted(set(data) - set(FIELD_COLUMNS))
    if unsupported:
        raise ValidationError("Request contains unsupported fields", unsupported)

    if method == "PATCH" and not data:
        raise ValidationError("At least one profile field is required")

    if method == "POST":
        name = data.get("fullName")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("fullName is required", ["fullName"])

    values = {}

    if "fullName" in data:
        name = data["fullName"]
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("fullName cannot be empty", ["fullName"])
        values["full_name"] = _clean_optional_text("fullName", name)

    for field in ("gender", "phone", "address", "city", "emergencyContact"):
        if field in data:
            values[FIELD_COLUMNS[field]] = _clean_optional_text(field, data[field])

    if "dateOfBirth" in data:
        raw = data["dateOfBirth"]
        if raw is None or raw == "":
            values["date_of_birth"] = None
        else:
            try:
                dob = date.fromisoformat(raw)
                if dob.isoformat() != raw:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValidationError(
                    "dateOfBirth must use YYYY-MM-DD format", ["dateOfBirth"]
                )
            if dob > (today or date.today()):
                raise ValidationError(
                    "dateOfBirth cannot be in the future", ["dateOfBirth"]
                )
            if dob < MIN_DOB:
                raise ValidationError("dateOfBirth is not valid", ["dateOfBirth"])
            values["date_of_birth"] = dob

    if method in ("POST", "PUT"):
        # Full-replace semantics: anything not supplied is cleared.
        for field, column in FIELD_COLUMNS.items():
            if field != "fullName":
                values.setdefault(column, None)

    return values


def serialize(row):
    def iso(value):
        return value.isoformat() if value is not None else None

    return {
        "patientId": int(row["patient_id"]),
        "userId": int(row["user_id"]),
        "fullName": row["full_name"],
        "email": row["email"],
        "dateOfBirth": iso(row["date_of_birth"]),
        "gender": row["gender"],
        "phone": row["phone"],
        "address": row["address"],
        "city": row["city"],
        "emergencyContact": row["emergency_contact"],
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row["updated_at"]),
    }


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def handler(event, context):
    cid = correlation(event)
    method = (event.get("httpMethod") or "").upper()
    user_id = authenticated_patient(event)

    if user_id is None:
        return error_response(
            403, "A valid authenticated patient account is required", cid
        )

    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return error_response(405, "Method not allowed", cid)

    path_patient_id = (event.get("pathParameters") or {}).get("patient_id")
    if path_patient_id is not None:
        if method != "GET":
            return error_response(405, "Method not allowed", cid)
        try:
            path_patient_id = int(path_patient_id)
            if path_patient_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return error_response(400, "patient_id must be a positive integer", cid)

    values = {}
    if method in ("POST", "PUT", "PATCH"):
        data = body(event)
        try:
            values = validate_profile(data, method)
        except ValidationError as exc:
            extra = {"fields": exc.fields} if exc.fields else {}
            return error_response(400, exc.message, cid, **extra)

    conn = None
    try:
        conn = dbconn(config())

        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM users "
                "WHERE user_id = %s AND role = 'PATIENT' AND is_active = 1",
                (user_id,),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return error_response(403, "Active patient account not found", cid)

            cur.execute(PROFILE_SELECT, (user_id,))
            own = cur.fetchone()

            # ---- ownership check for /patient/profile/{patient_id} ----------
            if path_patient_id is not None:
                conn.rollback()
                if own is None or int(own["patient_id"]) != path_patient_id:
                    # Same answer whether the id exists or not: no enumeration.
                    emit("warning", "patient_profile_access_denied", cid,
                         userId=str(user_id))
                    return error_response(
                        403, "You can only access your own profile", cid
                    )
                return response(200, serialize(own))

            if method == "GET":
                conn.rollback()
                if own is None:
                    return error_response(
                        404, "Patient profile not found; create it with POST", cid
                    )
                return response(200, serialize(own))

            if method == "DELETE":
                if own is None:
                    conn.rollback()
                    return error_response(404, "Patient profile not found", cid)
                cur.execute("DELETE FROM patients WHERE user_id = %s", (user_id,))
                conn.commit()
                emit("info", "patient_profile_deleted", cid, userId=str(user_id))
                return response(200, {"message": "Patient profile deleted"})

            if method == "POST":
                if own is not None:
                    conn.rollback()
                    return error_response(
                        409, "Patient profile already exists; use PUT or PATCH", cid
                    )
                columns = ["user_id", *values.keys()]
                cur.execute(
                    f"INSERT INTO patients ({', '.join(columns)}) "
                    f"VALUES ({', '.join(['%s'] * len(columns))})",
                    [user_id, *values.values()],
                )
            else:  # PUT / PATCH
                if own is None:
                    conn.rollback()
                    return error_response(
                        404, "Patient profile not found; create it with POST", cid
                    )
                if not values:
                    conn.rollback()
                    return error_response(400, "No profile fields supplied", cid)
                assignments = ", ".join(f"{column} = %s" for column in values)
                cur.execute(
                    f"UPDATE patients SET {assignments} WHERE user_id = %s",
                    [*values.values(), user_id],
                )

            conn.commit()

            cur.execute(PROFILE_SELECT, (user_id,))
            saved = cur.fetchone()
            conn.rollback()  # end the read transaction

        emit("info", "patient_profile_saved", cid, userId=str(user_id),
             method=method)
        return response(
            201 if method == "POST" else 200,
            {
                "message": (
                    "Patient profile created" if method == "POST"
                    else "Patient profile updated"
                ),
                **serialize(saved),
            },
        )

    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if "Duplicate entry" in str(exc):
            # Two concurrent POSTs: uq_patients_user_id caught the second one.
            return error_response(
                409, "Patient profile already exists; use PUT or PATCH", cid
            )
        logger.exception("Patient profile request failed")
        emit("error", "patient_profile_failed", cid, method=method,
             userId=str(user_id))
        return error_response(500, "Internal server error", cid)

    finally:
        if conn is not None:
            conn.close()
