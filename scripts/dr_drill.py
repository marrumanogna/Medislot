"""Disaster-recovery drill: restore RDS and one DynamoDB table from backups, time it, clean up.

Usage:  python scripts/dr_drill.py dev            (run locally with administrator credentials)
Restores medislot-<env>-mysql to a point in time as a temporary instance and MediSlot-<env>-Slots
to a temporary table, measures how long each takes, deletes both, and prints a markdown result row
to paste into docs/dr-drill.md.
"""
import sys
import time

import boto3

env = sys.argv[1] if len(sys.argv) > 1 else "dev"
rds, ddb = boto3.client("rds"), boto3.client("dynamodb")
src_db, tmp_db = f"medislot-{env}-mysql", f"medislot-{env}-mysql-drill"
src_table, tmp_table = f"MediSlot-{env}-Slots", f"MediSlot-{env}-Slots-drill"

# ---- RDS point-in-time restore
src = rds.describe_db_instances(DBInstanceIdentifier=src_db)["DBInstances"][0]
restorable = src["LatestRestorableTime"]
began = time.time()
rds.restore_db_instance_to_point_in_time(
    SourceDBInstanceIdentifier=src_db, TargetDBInstanceIdentifier=tmp_db, UseLatestRestorableTime=True,
    DBSubnetGroupName=src["DBSubnetGroup"]["DBSubnetGroupName"],
    VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]],
    DBInstanceClass=src["DBInstanceClass"], MultiAZ=False, PubliclyAccessible=False,
    Tags=[{"Key": "Project", "Value": "MediSlot"}, {"Key": "Purpose", "Value": "dr-drill"}])
rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=tmp_db, WaiterConfig={"Delay": 30, "MaxAttempts": 120})
rds_seconds = time.time() - began
rds.delete_db_instance(DBInstanceIdentifier=tmp_db, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)

# ---- DynamoDB point-in-time restore
items = ddb.describe_table(TableName=src_table)["Table"].get("ItemCount", 0)
began = time.time()
ddb.restore_table_to_point_in_time(SourceTableName=src_table, TargetTableName=tmp_table, UseLatestRestorableTime=True)
ddb.get_waiter("table_exists").wait(TableName=tmp_table, WaiterConfig={"Delay": 10, "MaxAttempts": 120})
while ddb.describe_table(TableName=tmp_table)["Table"]["TableStatus"] != "ACTIVE":
    time.sleep(10)
ddb_seconds = time.time() - began
ddb.delete_table(TableName=tmp_table)

print(f"| {time.strftime('%Y-%m-%d')} | RDS restored to {restorable:%Y-%m-%d %H:%M} UTC | {rds_seconds / 60:.1f} min "
      f"| DynamoDB `{src_table}` ({items} items) | {ddb_seconds / 60:.1f} min | temporary resources deleted |")
