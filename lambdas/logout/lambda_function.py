import time
import jwt
import logging

from common import config, body, response, emit, correlation, auth_table, token_hash


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info("Logout Lambda execution started")

    ### reads the req
    cid = correlation(event)

    logger.info("Processing logout request: correlation_id=%s", cid)

    try:
        data = body(event)
        raw = data.get("refreshToken") if isinstance(data, dict) else None

        ###Check the Authorization header
        headers = event.get("headers") or {}
        auth = headers.get("authorization") or headers.get("Authorization", "")

        if not auth.startswith("Bearer "):
            logger.warning("Logout rejected: Bearer access token missing")
            return response(401, {
                "message": "Bearer access token required",
                "correlationId": cid
            })

        logger.info("Bearer access token received")

        ###Validate the access token
        c = config()
        claims = jwt.decode(
            auth[7:],
            c["jwt"],
            algorithms=["HS256"],
            issuer="medislot",
            audience="medislot-api"
        )

        logger.info(
            "Access token validated: user_id=%s",
            claims["sub"]
        )

        ###Revoke the refresh token
        if isinstance(raw, str) and raw:
            logger.info("Processing refresh token revocation")

            key = "REFRESH#" + token_hash(raw)

            try:
                item = auth_table().get_item(
                    Key={
                        "token_key": key,
                        "token_type": "REFRESH"
                    }
                ).get("Item")

                if item and item.get("user_id") == claims["sub"]:
                    auth_table().update_item(
                        Key={
                            "token_key": key,
                            "token_type": "REFRESH"
                        },
                        UpdateExpression="SET #s=:r",
                        ConditionExpression="attribute_exists(token_key)",
                        ExpressionAttributeNames={
                            "#s": "status"
                        },
                        ExpressionAttributeValues={
                            ":r": "REVOKED"
                        }
                    )

                    logger.info("Refresh token successfully revoked")
                else:
                    logger.info(
                        "Refresh token not found or does not belong to user"
                    )

            except Exception:
                logger.exception("Refresh token revocation failed")

        ###Add the access token to the denylist
        logger.info("Adding access token to DynamoDB denylist")

        auth_table().put_item(
            Item={
                "token_key": "JTI#" + claims["jti"],
                "token_type": "DENYLIST",
                "user_id": claims["sub"],
                "created_at": int(time.time()),
                "expires_at": int(claims["exp"]),
                "status": "REVOKED"
            }
        )

        logger.info(
            "Access token added to denylist successfully: user_id=%s",
            claims["sub"]
        )

        logger.info("Logout completed successfully")

        return response(200, {
            "message": "Logged out"
        })

    except Exception:
        logger.exception("Logout Lambda failed")

        return response(401, {
            "message": "Invalid access token",
            "correlationId": cid
        })