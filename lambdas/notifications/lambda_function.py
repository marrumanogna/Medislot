"""MediSlot Milestone 6: notification consumer (EventBridge -> SQS -> this Lambda -> SNS).

Runs OUTSIDE the VPC: it needs no database, only the event payload, DynamoDB and SNS.
Exactly-once: a conditional put into the NotificationLog table (key = event id + recipient) is made
BEFORE publishing. A redelivered SQS message or a second dispatcher run hits the condition and is skipped.
If SNS fails the log row is deleted and the message returns to the queue (then the DLQ after retries).
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ENV = os.environ.get("ENVIRONMENT", "dev")
LOG_TTL = 30 * 24 * 3600

_sns = _log = None


def sns():
    global _sns
    _sns = _sns or boto3.client("sns")
    return _sns


def dispatch_log():
    global _log
    _log = _log or boto3.resource("dynamodb").Table(f"MediSlot-{ENV}-NotificationLog")
    return _log


def metric(name, value=1):
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": "medislot", "Dimensions": [["Environment"]], "Metrics": [{"Name": name, "Unit": "Count"}]}]},
        "Environment": ENV, name: value}))


def recipients(detail_type, d):
    """The Frozen Rule Set notification matrix."""
    patient, doctor = ("patient", d.get("patient_id")), ("doctor", d.get("doctor_id"))
    if detail_type in ("AppointmentBooked", "AppointmentRescheduled"):
        return [patient, doctor]
    if detail_type == "AppointmentCancelled":
        return [patient] if d.get("status") == "CANCELLED_BY_DOCTOR" else [patient, doctor]
    if detail_type in ("WaitlistOfferIssued", "WaitlistOfferExpired", "ReminderDue"):
        return [patient]
    return []


def compose(detail_type, rtype, d):
    start = d.get("slot_start", "")
    if detail_type == "AppointmentBooked":
        return "Appointment booked", f"Appointment #{d.get('appointment_id')} is confirmed for {start}."
    if detail_type == "AppointmentRescheduled":
        return "Appointment rescheduled", f"Appointment #{d.get('appointment_id')} moved to {start}."
    if detail_type == "AppointmentCancelled":
        who = "the doctor" if d.get("status") == "CANCELLED_BY_DOCTOR" else "the patient"
        refund = f" Refund: {d.get('refund_amount')} ({d.get('refund_percent')}%)." if rtype == "patient" else ""
        return "Appointment cancelled", f"Appointment #{d.get('appointment_id')} was cancelled by {who}.{refund}"
    if detail_type == "WaitlistOfferIssued":
        return "A seat is held for you", f"Accept offer {d.get('offer_id')} before it expires."
    if detail_type == "WaitlistOfferExpired":
        return "Your waitlist offer expired", f"Offer {d.get('offer_id')} was not accepted in time."
    return f"Appointment reminder ({d.get('hours_before')}h)", f"Appointment #{d.get('appointment_id')} starts at {start}."


def send(event_key, detail_type, rtype, rid, detail):
    """Returns True when sent, False when it was a duplicate."""
    key = f"{event_key}#{rtype}#{rid}"
    try:
        dispatch_log().put_item(
            Item={"dispatch_key": key, "event_type": detail_type, "created_at": int(time.time()),
                  "expires_at": int(time.time()) + LOG_TTL},
            ConditionExpression="attribute_not_exists(dispatch_key)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    subject, text = compose(detail_type, rtype, detail)
    try:
        sns().publish(
            TopicArn=os.environ["PATIENT_TOPIC_ARN" if rtype == "patient" else "DOCTOR_TOPIC_ARN"],
            Subject=subject[:100],
            Message=json.dumps({"event": detail_type, "recipient": rtype, "recipient_id": rid, "text": text,
                                "detail": detail}, default=str),
            MessageAttributes={"event_type": {"DataType": "String", "StringValue": detail_type},
                               "recipient_id": {"DataType": "String", "StringValue": str(rid)}})
    except Exception:
        dispatch_log().delete_item(Key={"dispatch_key": key})  # let the retry send it
        raise
    metric("NotificationsDispatched")
    return True


def handler(event, context):
    failures = []
    for rec in event.get("Records", []):
        try:
            msg = json.loads(rec["body"])
            dt, detail = msg.get("detail-type"), msg.get("detail") or {}
            if dt == "ReminderDue":
                event_key = f"{detail.get('appointment_id')}#{detail.get('interval')}"
            else:
                event_key = msg.get("id") or rec["messageId"]
            for rtype, rid in recipients(dt, detail):
                if rid is not None:
                    send(event_key, dt, rtype, rid, detail)
        except Exception:
            logger.exception("Notification failed")
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
