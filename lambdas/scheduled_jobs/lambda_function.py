"""MediSlot Milestone 6: scheduled jobs. Every action is safe to run twice.

    {"action": "reminders"}  every 5 min  -> ReminderDue events at 24 h and 2 h before the start
    {"action": "no_show"}    every 15 min -> CONFIRMED appointments 30 min past slot end become NO_SHOW

Reminders are exactly-once: a conditional put on `appointmentId#interval` in the dispatch log is made
before the event is published; a second run finds the row and skips. Thresholds come from SSM.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from common import config, dbconn

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ENV = os.environ.get("ENVIRONMENT", "dev")
DEFAULTS = {"reminder/first-hours": 24, "reminder/second-hours": 2, "reminder/catchup-minutes": 30,
            "noshow/grace-minutes": 30}
_cache = {}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ssm_int(name):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        from common import ssm
        value = int(ssm.get_parameter(Name=f"/medislot/{ENV}/{name}")["Parameter"]["Value"])
    except Exception:
        value = DEFAULTS[name]
    _cache[name] = (time.time(), value)
    return value


def metric(name, value=1, unit="Count"):
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": "medislot", "Dimensions": [["Environment"]], "Metrics": [{"Name": name, "Unit": unit}]}]},
        "Environment": ENV, name: value}))


class DispatchLog:
    def __init__(self):
        from common import table
        self.t = table(f"MediSlot-{ENV}-NotificationLog")

    def claim(self, key):
        try:
            self.t.put_item(Item={"dispatch_key": key, "created_at": int(time.time()),
                                  "expires_at": int(time.time()) + 30 * 24 * 3600},
                            ConditionExpression="attribute_not_exists(dispatch_key)")
            return True
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, key):
        self.t.delete_item(Key={"dispatch_key": key})


def publish(detail):
    import boto3
    out = boto3.client("events").put_events(Entries=[{
        "Source": "medislot.jobs", "DetailType": "ReminderDue", "Detail": json.dumps(detail, default=str),
        "EventBusName": "default"}])
    if out.get("FailedEntryCount"):
        raise RuntimeError("EventBridge rejected the reminder")


def run_reminders(cur, log, now=None, publisher=publish, hours=None, catchup=None):
    now = now or utcnow()
    hours = hours or (ssm_int("reminder/first-hours"), ssm_int("reminder/second-hours"))
    catchup = catchup if catchup is not None else ssm_int("reminder/catchup-minutes")
    sent = 0
    for h in hours:
        upper, lower = now + timedelta(hours=h), now + timedelta(hours=h) - timedelta(minutes=catchup)
        cur.execute("SELECT appointment_id, patient_id, doctor_id, slot_start FROM appointments "
                    "WHERE status = 'CONFIRMED' AND slot_start > %s AND slot_start <= %s AND slot_start > %s",
                    (now, upper, lower))
        for a in cur.fetchall():
            interval = f"REMINDER_{h}H"
            key = f"{a['appointment_id']}#{interval}"
            if not log.claim(key):
                continue
            try:
                publisher({"appointment_id": int(a["appointment_id"]), "patient_id": int(a["patient_id"]),
                           "doctor_id": int(a["doctor_id"]), "slot_start": a["slot_start"].isoformat() + "Z",
                           "hours_before": h, "interval": interval})
            except Exception:
                log.release(key)
                raise
            sent += 1
    return sent


def run_no_show(conn, cur, now=None, grace=None):
    now = now or utcnow()
    grace = grace if grace is not None else ssm_int("noshow/grace-minutes")
    cur.execute("SELECT appointment_id, patient_id, fee FROM appointments WHERE status = 'CONFIRMED' AND slot_end < %s",
                (now - timedelta(minutes=grace),))
    marked = 0
    for a in cur.fetchall():
        cur.execute("UPDATE appointments SET status = 'NO_SHOW', active_slot_id = NULL "
                    "WHERE appointment_id = %s AND status = 'CONFIRMED'", (a["appointment_id"],))
        if cur.rowcount == 1:
            cur.execute("INSERT IGNORE INTO refund_ledger (appointment_id, patient_id, fee, amount, refund_percent, tier, reason) "
                        "VALUES (%s, %s, %s, 0, 0, 'NO_SHOW', 'Patient did not attend')",
                        (a["appointment_id"], a["patient_id"], a["fee"]))
            marked += 1
    conn.commit()
    cur.execute("SELECT SUM(status = 'NO_SHOW') AS ns, SUM(status IN ('NO_SHOW', 'COMPLETED')) AS done FROM appointments")
    row = cur.fetchone() or {}
    if row.get("done"):
        metric("NoShowRate", round(100.0 * float(row["ns"] or 0) / float(row["done"]), 2), "Percent")
    return marked


def handler(event, context):
    action = (event or {}).get("action")
    conn = dbconn(config())
    try:
        cur = conn.cursor()
        if action == "reminders":
            return {"reminders_sent": run_reminders(cur, DispatchLog())}
        if action == "no_show":
            return {"no_show_marked": run_no_show(conn, cur)}
        raise ValueError(f"Unknown action {action!r}")
    finally:
        conn.close()
