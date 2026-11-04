
# IMPORTS
import os, time, jwt
from common import get_param, auth_table, emit
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):

    logger.info("Authorizer Lambda execution started")
    logger.info("Request ID: %s", getattr(context, "aws_request_id", "unknown"))

    method_arn = event.get("methodArn", "*")
    token = (event.get("authorizationToken") or "").strip()
    cid = (event.get("headers") or {}).get("x-correlation-id")

    logger.info("Authorization request received")
    logger.info("Method ARN: %s", method_arn)
    logger.info("Correlation ID: %s", cid)

    if token.lower().startswith("bearer "):
        token = token[7:].strip()
        logger.info("Bearer token prefix detected and removed")

    try:

        if not token:
            logger.warning("Authorization failed: Missing token")
            raise ValueError("missing")

        logger.info("Access token received")

        env = os.environ.get("ENVIRONMENT", "dev")
        logger.info("Environment: %s", env)

        signing_secret = get_param(
            f"/medislot/{env}/auth/jwt-secret", True
        )

        logger.info("JWT signing secret retrieved successfully")

        claims = jwt.decode(
            token,
            signing_secret,
            algorithms=["HS256"],
            issuer="medislot",
            audience="medislot-api",
            options={"require": ["exp", "iat", "sub", "jti", "role"]}
        )

        logger.info("JWT validation successful")
        logger.info("Token subject: %s", claims["sub"])
        logger.info("User role: %s", claims["role"])

        if claims["role"] not in ("PATIENT", "DOCTOR"):
            logger.warning("Authorization failed: Invalid user role")
            raise ValueError("role")

        # IDENTIFY THE REQUESTED API ROUTE
        arn_parts = method_arn.split("/")
        route = "/".join(arn_parts[2:]) if len(arn_parts) > 2 else ""

        logger.info("Requested API route: %s", route)

        # CHECK WHETHER ROUTE REQUIRES PATIENT OR DOCTOR ROLE
        expected = "DOCTOR" if "/doctor/" in ("/" + route + "/") else (
            "PATIENT" if "/patient/" in ("/" + route + "/") else None
        )

        logger.info("Expected role for route: %s", expected)

        # REJECT USER IF ROLE DOES NOT MATCH THE ROUTE
        if expected and claims["role"] != expected:
            logger.warning(
                "Role mismatch: user role=%s, expected role=%s",
                claims["role"],
                expected
            )
            raise PermissionError("wrong_role")

        logger.info("User role validation successful")

        # CHECK TOKEN EXPIRATION TIME
        now = int(time.time())
        if int(claims["exp"]) <= now:
            logger.warning("Authorization failed: Token expired")
            raise ValueError("expired")

        logger.info("Token expiration check successful")

        # CREATE DENYLIST LOOKUP KEY USING JWT ID (JTI)
        key = "JTI#" + str(claims["jti"])

        # CHECK DYNAMODB FOR A REVOKED TOKEN
        logger.info("Checking DynamoDB denylist")

        item = auth_table().get_item(
            Key={"token_key": key, "token_type": "DENYLIST"}
        ).get("Item")

        logger.info("Denylist lookup completed")

        # REJECT TOKEN IF AN ACTIVE DENYLIST ENTRY EXISTS
        if item and int(item.get("expires_at", 0)) > now:
            logger.warning("Authorization failed: Token has been revoked")
            raise ValueError("revoked")

        logger.info("Token is not revoked")

        # RETURN ALLOW POLICY TO API GATEWAY
        # PASS USER ID, ROLE AND JTI TO THE BACKEND LAMBDA
        logger.info(
            "Authorization successful for user %s with role %s",
            claims["sub"],
            claims["role"]
        )

        logger.info("Returning ALLOW policy to API Gateway")

        return {
            "principalId": str(claims["sub"]),
            "policyDocument": {
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": method_arn
                }]
            },
            "context": {
                "userId": str(claims["sub"]),
                "role": claims["role"],
                "jti": str(claims["jti"])
            }
        }

    # HANDLE WRONG ROLE
    except PermissionError:
        logger.warning("Authorization denied: Wrong user role")
        emit("warning", "authorization_wrong_role", cid)

        return {
            "principalId": "forbidden",
            "policyDocument": {
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": "execute-api:Invoke",
                    "Effect": "Deny",
                    "Resource": method_arn
                }]
            }
        }

    # HANDLE OTHER AUTHORIZATION FAILURES
    except Exception:
        logger.exception("Authorization rejected due to validation or processing error")
        emit("warning", "authorization_rejected", cid)
        raise Exception("Unauthorized")
