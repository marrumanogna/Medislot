# IMPORTS
import time, secrets, jwt
import logging

from common import config, body, response, emit, correlation, auth_table, token_hash


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):

    logger.info("Refresh Token Lambda execution started")

    cid = correlation(event)

    logger.info("Processing refresh token request: correlation_id=%s", cid)

    try:
        # READ REFRESH TOKEN FROM REQUEST BODY
        data = body(event)
        raw = data.get("refreshToken") if isinstance(data, dict) else None

        # IS PROVIDED
        if not isinstance(raw, str) or not raw:
            logger.warning("Refresh token is missing")
            return response(400, {
                "message": "refreshToken is required",
                "correlationId": cid
            })

        logger.info("Refresh token received")

        # LOAD CONFIGURATION AND CREATE REFRESH TOKEN LOOKUP KEY
        c = config()
        key = "REFRESH#" + token_hash(raw)

        logger.info("Refresh token lookup key generated")

        # GET DYNAMODB AUTH TOKENS TABLE
        t = auth_table()

        logger.info("Connected to DynamoDB auth tokens table")

        # FIND REFRESH TOKEN RECORD IN DYNAMODB
        old = t.get_item(
            Key={
                "token_key": key,
                "token_type": "REFRESH"
            }
        ).get("Item")

        now = int(time.time())

        logger.info("Refresh token lookup completed")

        # VALIDATE TOKEN EXISTS, IS ACTIVE AND HAS NOT EXPIRED
        if (
            not old
            or old.get("status") != "ACTIVE"
            or int(old.get("expires_at", 0)) <= now
        ):
            logger.warning("Refresh token is invalid, revoked, or expired")
            return response(401, {
                "message": "Invalid or revoked refresh token",
                "correlationId": cid
            })

        logger.info(
            "Refresh token validated successfully: user_id=%s",
            old["user_id"]
        )

        # REVOKE OLD REFRESH TOKEN
        # CONDITIONAL UPDATE PREVENTS CONCURRENT REUSE
        logger.info("Revoking existing refresh token")

        t.update_item(
            Key={
                "token_key": key,
                "token_type": "REFRESH"
            },
            UpdateExpression="SET #s=:revoked",
            ConditionExpression="#s=:active",
            ExpressionAttributeNames={
                "#s": "status"
            },
            ExpressionAttributeValues={
                ":revoked": "REVOKED",
                ":active": "ACTIVE"
            }
        )

        logger.info("Existing refresh token revoked successfully")

        # GET ACCESS TOKEN LIFETIME FROM SSM
        minutes = int(
            __import__("common").get_param(
                f"{c['prefix']}/auth/access-token-minutes"
            )
        )

        logger.info("Access token lifetime configuration retrieved")

        # GENERATE A NEW UNIQUE JWT ID (JTI)
        jti = __import__("uuid").uuid4().hex

        logger.info("New JWT ID generated")

        # CREATE NEW ACCESS TOKEN
        access = jwt.encode(
            {
                "sub": old["user_id"],
                "userId": old["user_id"],
                "role": old["role"],
                "iss": "medislot",
                "aud": "medislot-api",
                "iat": now,
                "exp": now + minutes * 60,
                "jti": jti
            },
            c["jwt"],
            algorithm="HS256"
        )

        logger.info("New access token generated successfully")

        # GENERATE A NEW REFRESH TOKEN
        newraw = secrets.token_urlsafe(48)

        logger.info("New refresh token generated")

        # GET REFRESH TOKEN LIFETIME FROM SSM
        days = int(
            __import__("common").get_param(
                f"{c['prefix']}/auth/refresh-token-days"
            )
        )

        logger.info("Refresh token lifetime configuration retrieved")

        # STORE NEW REFRESH TOKEN HASH IN DYNAMODB
        t.put_item(
            Item={
                "token_key": "REFRESH#" + token_hash(newraw),
                "token_type": "REFRESH",
                "user_id": old["user_id"],
                "role": old["role"],
                "created_at": now,
                "expires_at": now + days * 86400,
                "status": "ACTIVE"
            }
        )

        logger.info(
            "New refresh token record stored successfully: user_id=%s",
            old["user_id"]
        )

        # RETURN NEW ACCESS TOKEN AND REFRESH TOKEN
        logger.info("Token refresh completed successfully")

        return response(200, {
            "accessToken": access,
            "tokenType": "Bearer",
            "expiresIn": minutes * 60,
            "refreshToken": newraw
        })

    # HANDLE ERRORS
    except Exception:
        logger.exception("Refresh Token Lambda failed")

        emit("error", "refresh_failed", cid)

        return response(401, {
            "message": "Invalid or revoked refresh token",
            "correlationId": cid
        })