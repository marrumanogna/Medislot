import json, os, re, uuid, logging, boto3, pymysql, hashlib, time
from functools import lru_cache
from botocore.exceptions import ClientError


log = logging.getLogger()
log.setLevel(logging.INFO)

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")


###JSON log entries------------
def emit(level, message, correlation_id=None, **fields):
    payload = {
        "level": level,
        "message": message,
        "correlationId": correlation_id or str(uuid.uuid4()),
        **fields
    }

    getattr(
        log,
        level if level in ("info", "warning", "error") else "info"
    )(json.dumps(payload))


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store"
        },
        "body": json.dumps(body)
    }


###reads SSM values----------
@lru_cache(maxsize=64)
def get_param(name, decrypt=False):
    return ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )["Parameter"]["Value"]


###collects configuration---------------
def config():
    env = os.environ.get("ENVIRONMENT", "dev")

    return {
        "env": env,
        "prefix": f"/medislot/{env}",
        "jwt": get_param(f"/medislot/{env}/auth/jwt-secret", True),
        "dbhost": get_param(f"/medislot/{env}/db/host"),
        "dbport": int(get_param(f"/medislot/{env}/db/port")),
        "dbname": get_param(f"/medislot/{env}/db/name"),
        "dbuser": get_param(f"/medislot/{env}/db/username"),
        "dbpass": get_param(f"/medislot/{env}/db/password", True)
    }


def dbconn(c):
    return pymysql.connect(
        host=c["dbhost"],
        port=c["dbport"],
        user=c["dbuser"],
        password=c["dbpass"],
        database=c["dbname"],
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

def table(name):
    return ddb.Table(name)


def auth_table():
    return table(
        f"MediSlot-{os.environ.get('ENVIRONMENT', 'dev')}-AuthTokens"
    )


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def body(event):
    try:
        value = event.get("body") or "{}"

        if event.get("isBase64Encoded"):
            import base64
            value = base64.b64decode(value).decode()

        return json.loads(value)

    except Exception:
        return None


def correlation(event):
    return (
        (event.get("headers") or {}).get("x-correlation-id")
        or (event.get("requestContext") or {}).get("requestId")
        or str(uuid.uuid4())
    )


###best-effort city extraction (doctor search city filter)------------
# Indian states / UTs that are NOT also commonly used as a city name.
# Delhi, Chandigarh and Puducherry are deliberately absent: people use them
# as the city, so "Connaught Place, Delhi" should yield "Delhi".
_NON_CITY_TAILS = {
    "india", "bharat",
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "orissa", "punjab", "rajasthan",
    "sikkim", "tamil nadu", "telangana", "tripura", "uttar pradesh",
    "uttarakhand", "west bengal", "andaman and nicobar islands",
    "dadra and nagar haveli and daman and diu", "jammu and kashmir",
    "ladakh", "lakshadweep",
    "ap", "ts", "tg", "tn", "ka", "kl", "mh", "up", "mp", "wb", "gj", "rj",
    "hr", "pb", "br", "od",
}
# Words that mean the segment is a street fragment, not a city name.
_STREET_WORDS = re.compile(
    r"\b(road|rd|street|st|lane|near|opposite|opp|plot|floor|block|sector"
    r"|building|hospital|clinic)\b",
    re.IGNORECASE,
)
_PINCODE_TAIL = re.compile(r"[\s\-\u2013:,.]*\b\d{5,6}\b\s*$")


def derive_city(address):
    """Guess the city from a free-text address, or None when unsure.

    Works on the usual Indian format "..., Area, City, State 500001":
    trailing pincode / state / country segments are dropped and the last
    remaining comma-separated segment is returned. This is a best-effort
    backfill and default; doctors can always set hospital_city themselves.
    """
    if not isinstance(address, str):
        return None

    parts = [p.strip() for p in re.split(r"[,\n]", address) if p.strip()]
    candidate = None

    while parts:
        segment = _PINCODE_TAIL.sub("", parts.pop()).strip(" .-\u2013")
        # "Telangana 500034" -> "Telangana"; a bare pincode -> "".
        if not segment or segment.lower() in _NON_CITY_TAILS:
            continue
        candidate = segment
        break

    if (
        not candidate
        or len(candidate) > 100
        or re.search(r"\d", candidate)
        or _STREET_WORDS.search(candidate)
    ):
        return None

    # A lone long segment is a street address, not a city name.
    if not parts and len(candidate) > 50:
        return None

    return candidate
