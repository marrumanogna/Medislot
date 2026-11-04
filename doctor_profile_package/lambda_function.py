import os
import json
import uuid
import logging
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from botocore.exceptions import ClientError

from common import config, dbconn, body, response, emit, correlation


logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# These are the only fields this Lambda permits clients to write.
STRING_LIMITS = {
    "licence_number": 100,
    "specialization": 120,
    "hospital_name": 200,
    "hospital_address": 500,
    "bio": 5000,
}

REQUIRED_FIELDS = {
    "licence_number",
    "specialization",
    "qualifications",
    "experience_years",
    "hospital_name",
    "hospital_address",
    "languages_spoken",
}

ALLOWED_FIELDS = REQUIRED_FIELDS | {
    "bio",
    "consultation_fee",
    "timezone",
    "photo_key",
}


def authenticated_user(event):
    authorizer = (
        event.get("requestContext", {})
        .get("authorizer", {})
    )

    user_id = authorizer.get("userId")
    role = authorizer.get("role")

    if not user_id or role != "DOCTOR":
        return None

    try:
        parsed_id = int(user_id)
        return parsed_id if parsed_id > 0 else None
    except (TypeError, ValueError):
        return None


def error_response(status_code, message, cid, **extra):
    payload = {
        "message": message,
        "correlationId": cid,
    }
    payload.update(extra)
    return response(status_code, payload)


def validate_string(field, value, limit, cid, required=True):
    if value is None and not required:
        return None, None

    if not isinstance(value, str) or not value.strip():
        return None, error_response(
            400, f"{field} is required and must be text", cid
        )

    value = value.strip()

    if len(value) > limit:
        return None, error_response(
            400, f"{field} must not exceed {limit} characters", cid
        )

    return value, None


def validate_string_array(field, value, cid):
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        return None, error_response(
            400, f"{field} must be a non-empty array of strings", cid
        )

    cleaned = list(dict.fromkeys(item.strip() for item in value))

    if len(cleaned) > 30 or any(len(item) > 100 for item in cleaned):
        return None, error_response(400, f"Invalid {field}", cid)

    return cleaned, None


def validate_profile_data(data, method, cid):
    if not isinstance(data, dict):
        return None, error_response(400, "Invalid JSON body", cid)

    unsupported = set(data) - ALLOWED_FIELDS
    if unsupported:
        return None, error_response(
            400,
            "Request contains unsupported fields",
            cid,
            fields=sorted(unsupported),
        )

    if method in ("POST", "PUT"):
        missing = REQUIRED_FIELDS - set(data)
        if missing:
            return None, error_response(
                400,
                "Missing required profile fields",
                cid,
                fields=sorted(missing),
            )

    if method == "PATCH" and not data:
        return None, error_response(
            400, "At least one profile field is required", cid
        )

    values = {}

    for field, limit in STRING_LIMITS.items():
        if field not in data:
            continue

        cleaned, error = validate_string(
            field,
            data[field],
            limit,
            cid,
            required=(field in REQUIRED_FIELDS),
        )
        if error:
            return None, error
        values[field] = cleaned

    for field in ("qualifications", "languages_spoken"):
        if field in data:
            cleaned, error = validate_string_array(field, data[field], cid)
            if error:
                return None, error
            # MySQL JSON columns accept a JSON-encoded string.
            values[field] = json.dumps(cleaned)

    if "experience_years" in data:
        value = data["experience_years"]
        if type(value) is not int or not 0 <= value <= 80:
            return None, error_response(
                400, "experience_years must be an integer from 0 to 80", cid
            )
        values["experience_years"] = value

    if "consultation_fee" in data:
        value = data["consultation_fee"]
        if isinstance(value, bool) or value is None:
            return None, error_response(
                400, "consultation_fee must be a non-negative amount", cid
            )

        try:
            fee = Decimal(str(value))
            if (
                not fee.is_finite()
                or fee < 0
                or fee > Decimal("99999999.99")
                or fee.as_tuple().exponent < -2
            ):
                raise InvalidOperation
            values["consultation_fee"] = fee
        except (InvalidOperation, ValueError, TypeError):
            return None, error_response(
                400,
                "consultation_fee must be non-negative with at most 2 decimals",
                cid,
            )

    if "timezone" in data:
        value = data["timezone"]
        if not isinstance(value, str) or not value.strip():
            return None, error_response(
                400, "timezone must be an IANA timezone name", cid
            )
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return None, error_response(400, "Unknown timezone", cid)
        values["timezone"] = value

    if "photo_key" in data:
        value = data["photo_key"]
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            return None, error_response(
                400, "photo_key must be a valid key or null", cid
            )
        values["photo_key"] = value.strip() if isinstance(value, str) else None

    if method in ("POST", "PUT"):
        values.setdefault("bio", None)
        values.setdefault("consultation_fee", None)
        values.setdefault("timezone", "UTC")

    return values, None


def decode_json_columns(profile):
    for field in ("qualifications", "languages_spoken"):
        value = profile.get(field)
        if isinstance(value, str):
            try:
                profile[field] = json.loads(value)
            except (TypeError, ValueError):
                profile[field] = []
    return profile


def handler(event, context):
    cid = correlation(event)
    method = (event.get("httpMethod") or "").upper()
    user_id = authenticated_user(event)

    if user_id is None:
        return error_response(
            403, "A valid authenticated doctor account is required", cid
        )

    path = (event.get("path") or event.get("resource") or "").rstrip("/")
    is_photo_upload_route = path.endswith("/doctor/profile/photo-upload-url")

    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return error_response(405, "Method not allowed", cid)

    if is_photo_upload_route and method != "POST":
        return error_response(405, "Method not allowed", cid)

    data = {}
    if method in ("POST", "PUT", "PATCH"):
        data = body(event)
        if not isinstance(data, dict):
            return error_response(400, "Invalid JSON body", cid)

    values = {}
    if method in ("POST", "PUT", "PATCH") and not is_photo_upload_route:
        values, validation_error = validate_profile_data(data, method, cid)
        if validation_error:
            return validation_error

    conn = None

    try:
        c = config()
        conn = dbconn(c)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    user_id,
                    CONCAT(first_name, ' ', last_name) AS full_name,
                    email
                FROM users
                WHERE user_id = %s
                  AND role = 'DOCTOR'
                  AND is_active = TRUE
                """,
                (user_id,),
            )
            doctor = cur.fetchone()

            if not doctor:
                conn.rollback()
                return error_response(404, "Active doctor account not found", cid)

            if is_photo_upload_route:
                content_type = data.get("content_type")
                extension = PHOTO_TYPES.get(content_type)
                content_length = data.get("content_length")
                bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")

                if not extension:
                    conn.rollback()
                    return error_response(
                        400,
                        "content_type must be image/jpeg, image/png, or image/webp",
                        cid,
                    )

                if (
                    type(content_length) is not int
                    or not 1 <= content_length <= MAX_PHOTO_BYTES
                ):
                    conn.rollback()
                    return error_response(
                        400,
                        "content_length must be between 1 and 5242880",
                        cid,
                    )

                if not bucket:
                    conn.rollback()
                    emit("error", "doctor_photo_bucket_not_configured", cid)
                    return error_response(
                        500, "Photo storage is not configured", cid
                    )

                photo_key = f"doctors/{user_id}/{uuid.uuid4().hex}.{extension}"
                upload_url = s3.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": bucket,
                        "Key": photo_key,
                        "ContentType": content_type,
                    },
                    ExpiresIn=300,
                    HttpMethod="PUT",
                )

                conn.rollback()
                return response(200, {
                    "upload_url": upload_url,
                    "photo_key": photo_key,
                    "expires_in_seconds": 300,
                    "max_size_bytes": MAX_PHOTO_BYTES,
                    "required_headers": {"Content-Type": content_type},
                    "next_step": (
                        "Upload the image bytes to upload_url, then PATCH "
                        "/doctor/profile with photo_key"
                    ),
                })

            if method == "DELETE":
                cur.execute(
                    "SELECT photo_key FROM doctor_profiles WHERE user_id = %s",
                    (user_id,),
                )
                existing = cur.fetchone()

                if not existing:
                    conn.rollback()
                    return error_response(404, "Doctor profile not found", cid)

                photo_key = existing.get("photo_key")

                cur.execute(
                    "DELETE FROM doctor_profiles WHERE user_id = %s",
                    (user_id,),
                )
                conn.commit()

                if photo_key and os.environ.get("DOCTOR_PHOTOS_BUCKET"):
                    try:
                        s3.delete_object(
                            Bucket=os.environ["DOCTOR_PHOTOS_BUCKET"],
                            Key=photo_key,
                        )
                    except Exception:
                        emit(
                            "warning",
                            "doctor_profile_photo_cleanup_failed",
                            cid,
                            userId=str(user_id),
                        )

                emit("info", "doctor_profile_deleted", cid, userId=str(user_id))
                return response(200, {
                    "message": "Doctor profile deleted",
                    "userId": str(user_id),
                })

            if (
                "photo_key" in values
                and values["photo_key"] is not None
            ):
                photo_key = values["photo_key"]
                expected_prefix = f"doctors/{user_id}/"
                bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")

                if not photo_key.startswith(expected_prefix) or not bucket:
                    conn.rollback()
                    return error_response(
                        400,
                        "photo_key must belong to your doctor account",
                        cid,
                    )

                try:
                    photo_head = s3.head_object(
                        Bucket=bucket,
                        Key=photo_key,
                    )
                except ClientError:
                    conn.rollback()
                    return error_response(
                        400,
                        "Photo was not found; upload it before saving photo_key",
                        cid,
                    )

                if (
                    photo_head.get("ContentLength", 0) > MAX_PHOTO_BYTES
                    or photo_head.get("ContentType") not in PHOTO_TYPES
                ):
                    conn.rollback()
                    return error_response(
                        400,
                        "Photo must be JPEG, PNG, or WebP and at most 5 MB",
                        cid,
                    )

            if method == "GET":
                cur.execute(
                    """
                    SELECT
                        full_name,
                        licence_number,
                        specialization,
                        qualifications,
                        experience_years,
                        hospital_name,
                        hospital_address,
                        languages_spoken,
                        bio,
                        consultation_fee,
                        photo_key,
                        timezone,
                        created_at,
                        updated_at
                    FROM doctor_profiles
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                profile = cur.fetchone()

                if not profile:
                    conn.rollback()
                    return error_response(404, "Doctor profile not found", cid)

                result = {**doctor, **decode_json_columns(profile)}
                # Generate a temporary URL to display the doctor's profile photo
                photo_key = result.get("photo_key")
                bucket = os.environ.get("DOCTOR_PHOTOS_BUCKET")

                result["photo_url"] = None

                if (
                    photo_key
                    and bucket
                    and photo_key.startswith(f"doctors/{user_id}/")
                ):
                    result["photo_url"] = s3.generate_presigned_url(
                        "get_object",
                        Params={
                            "Bucket": bucket,
                            "Key": photo_key,
                            "ResponseContentDisposition": "inline",
                        },
                        ExpiresIn=300,
                    )

                for key in ("created_at", "updated_at"):
                    if result.get(key):
                        result[key] = result[key].isoformat()

                if result.get("consultation_fee") is not None:
                    result["consultation_fee"] = str(
                        result["consultation_fee"]
                    )

                conn.rollback()
                return response(200, result)

            cur.execute(
                "SELECT user_id FROM doctor_profiles WHERE user_id = %s",
                (user_id,),
            )
            exists = cur.fetchone() is not None

            if method == "POST" and exists:
                conn.rollback()
                return error_response(
                    409,
                    "Doctor profile already exists; use PUT or PATCH",
                    cid,
                )

            if method in ("PUT", "PATCH") and not exists:
                conn.rollback()
                return error_response(
                    404,
                    "Doctor profile does not exist; use POST to create it",
                    cid,
                )

            if method == "POST":
                columns = ["user_id", "full_name", *values.keys()]
                params = [user_id, doctor["full_name"], *values.values()]
                placeholders = ", ".join(["%s"] * len(columns))

                cur.execute(
                    f"INSERT INTO doctor_profiles "
                    f"({', '.join(columns)}) VALUES ({placeholders})",
                    params,
                )
            else:
                if not values:
                    conn.rollback()
                    return error_response(
                        400, "No profile fields supplied", cid
                    )

                assignments = ", ".join(
                    f"{field} = %s" for field in values
                )
                cur.execute(
                    f"UPDATE doctor_profiles SET {assignments} "
                    "WHERE user_id = %s",
                    [*values.values(), user_id],
                )

        conn.commit()

        emit(
            "info",
            "doctor_profile_saved",
            cid,
            userId=str(user_id),
            method=method,
        )

        return response(
            201 if method == "POST" else 200,
            {
                "message": (
                    "Doctor profile created"
                    if method == "POST"
                    else "Doctor profile updated"
                ),
                "userId": str(user_id),
            },
        )

    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass

        logger.exception("doctor_profile_failed")
        emit(
            "error",
            "doctor_profile_failed",
            cid,
            method=method,
            userId=str(user_id),
        )
        return error_response(500, "Internal server error", cid)

    finally:
        if conn is not None:
            conn.close()
