import os, time, uuid, bcrypt, jwt, secrets
import logging

from common import config, dbconn, body, response, emit, correlation, auth_table, token_hash


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info("Login Lambda execution started")

    cid = correlation(event)

    logger.info("Processing login request: correlation_id=%s", cid)

    try:
        data = body(event)

        if not isinstance(data, dict):
            logger.warning("Invalid JSON body received")
            return response(400, {
                "message": "Invalid JSON body",
                "correlationId": cid
            })

        ###extracts
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "")

        logger.info("Login credentials received")

        if not email or not isinstance(password, str):
            logger.warning("Email or password is missing or invalid")
            return response(400, {
                "message": "Email and password are required",
                "correlationId": cid
            })

        logger.info("Connecting to database for user lookup")

        c = config()
        conn = dbconn(c)

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id,password_hash,role,is_active FROM users WHERE email=%s",
                    (email,)
                )
                user = cur.fetchone()

                logger.info("User lookup completed")

        finally:
            conn.close()
            logger.info("Database connection closed")

        if (
            not user
            or not user["is_active"]
            or not bcrypt.checkpw(
                password.encode(),
                user["password_hash"].encode()
            )
        ):
            logger.warning("Login rejected: invalid credentials or inactive account")
            emit("warning", "login_rejected", cid)

            return response(401, {
                "message": "Invalid email or password",
                "correlationId": cid
            })

        logger.info(
            "User credentials verified: user_id=%s, role=%s",
            user["user_id"], user["role"]
        )

        ###Prepare the access token
        now = int(time.time())
        jti = str(uuid.uuid4())

        minutes = int(
            __import__("common").get_param(
                f"{c['prefix']}/auth/access-token-minutes"
            )
        )

        logger.info("Access token expiry configuration retrieved")

        ###Create the JWT access token
        claims = {
            "sub": str(user["user_id"]),
            "userId": str(user["user_id"]),
            "role": user["role"],
            "iss": "medislot",
            "aud": "medislot-api",
            "iat": now,
            "exp": now + minutes * 60,
            "jti": jti
        }

        access = jwt.encode(claims, c["jwt"], algorithm="HS256")

        logger.info("JWT access token generated successfully")

        ###Generate the refresh token
        raw = secrets.token_urlsafe(48)

        days = int(
            __import__("common").get_param(
                f"{c['prefix']}/auth/refresh-token-days"
            )
        )

        logger.info("Refresh token expiry configuration retrieved")

        ###Store the refresh-token record in DynamoDB
        auth_table().put_item(
            Item={
                "token_key": "REFRESH#" + token_hash(raw),
                "token_type": "REFRESH",
                "user_id": str(user["user_id"]),
                "role": user["role"],
                "created_at": now,
                "expires_at": now + days * 86400,
                "status": "ACTIVE"
            }
        )

        logger.info("Refresh token record stored in DynamoDB")

        emit(
            "info",
            "login_succeeded",
            cid,
            userId=str(user["user_id"]),
            role=user["role"]
        )

        logger.info(
            "Login successful: user_id=%s, role=%s",
            user["user_id"], user["role"]
        )

        return response(200, {
            "accessToken": access,
            "tokenType": "Bearer",
            "expiresIn": minutes * 60,
            "refreshToken": raw,
            "userId": str(user["user_id"]),
            "role": user["role"]
        })

    except Exception:
        logger.exception("Login Lambda failed")

        emit("error", "login_failed", cid)

        return response(500, {
            "message": "Internal server error",
            "correlationId": cid
        })