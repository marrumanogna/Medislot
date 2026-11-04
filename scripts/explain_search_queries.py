"""Print EXPLAIN for every doctor-search filter combination.

Run against the deployed database (needs AWS credentials that can read the
SSM parameters and network access to RDS, e.g. from the EC2 workbench):

    python scripts/explain_search_queries.py

It builds the SQL with the *same functions the Lambda uses*, so the plans
shown are the real query patterns. For each of the 15 non-empty filter
combinations it prints the chosen index (key), the access type, and the
estimated rows, so the index choices can be reviewed against them.
"""

import itertools
import os
import sys

import boto3
import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas"))
from doctor_discovery.lambda_function import build_search_queries  # noqa: E402

ENV = os.environ.get("ENVIRONMENT", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SAMPLE = {
    "specialisation": "Cardiology",
    "city": "Hyd",
    "hospital": "Apollo",
    "name": "Sharma",
}

ssm = boto3.client("ssm", region_name=REGION)


def param(name, decrypt=False):
    return ssm.get_parameter(
        Name=f"/medislot/{ENV}/{name}", WithDecryption=decrypt
    )["Parameter"]["Value"]


def main():
    conn = pymysql.connect(
        host=param("db/host"),
        port=int(param("db/port")),
        user=param("db/username"),
        password=param("db/password", True),
        database=param("db/name"),
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM doctor_profiles")
            print(f"doctor_profiles rows: {cur.fetchone()['n']}\n")
            print(f"{'filters':<38} {'table':<5} {'type':<6} {'key':<26} rows")
            print("-" * 90)
            keys = list(SAMPLE)
            for size in range(1, len(keys) + 1):
                for combo in itertools.combinations(keys, size):
                    filters = {k: SAMPLE[k] for k in combo}
                    _, (page_sql, page_params) = build_search_queries(filters, 1, 10)
                    cur.execute("EXPLAIN " + page_sql, page_params)
                    for plan in cur.fetchall():
                        if plan["table"] != "dp":
                            continue
                        print(
                            f"{'+'.join(combo):<38} {plan['table']:<5} "
                            f"{str(plan['type']):<6} {str(plan['key']):<26} "
                            f"{plan['rows']}"
                        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
