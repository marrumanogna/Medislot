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
from datetime import datetime

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


def when(value):
    """Human readable time with the offset it was issued in, e.g. 'Thu 26 Nov 2026, 10:00 AM (UTC+05:30)'."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    off = dt.strftime("%z")
    return dt.strftime("%a %d %b %Y, %I:%M %p") + (f" (UTC{off[:3]}:{off[3:]})" if off else "")


def compose(detail_type, rtype, d):
    """Returns (subject, one-line text, formatted e-mail body)."""
    patient = rtype == "patient"
    rows = []
    if d.get("appointment_id") is not None:
        rows.append(("Appointment", f"#{d['appointment_id']}"))
    if d.get("slot_start"):
        rows.append(("When", when(d["slot_start"])))
    closing = ""
    if detail_type == "AppointmentBooked":
        title = "Appointment booked"
        lead = "Your appointment is confirmed." if patient else "A patient has booked an appointment with you."
    elif detail_type == "AppointmentRescheduled":
        title = "Appointment rescheduled"
        lead = "Your appointment has been moved to the time below." if patient else "An appointment on your schedule was rescheduled."
    elif detail_type == "AppointmentCancelled":
        by_doctor = d.get("status") == "CANCELLED_BY_DOCTOR"
        title = "Appointment cancelled"
        lead = ("Your appointment was cancelled by the doctor." if by_doctor else
                "Your appointment has been cancelled." if patient else "A patient cancelled an appointment.")
        if patient and d.get("refund_amount") is not None:
            rows.append(("Refund", f"{d.get('refund_amount')} ({d.get('refund_percent')}%)"))
    elif detail_type == "WaitlistOfferIssued":
        title = "A seat is held for you"
        lead = "A seat has opened up for a slot you were waiting for, and it is being held for you."
        rows.append(("Offer", str(d.get("offer_id"))))
        if d.get("expires_at"):
            rows.append(("Respond by", when(d["expires_at"])))
        closing = "Please accept the offer before it expires, otherwise the seat passes to the next patient."
    elif detail_type == "WaitlistOfferExpired":
        title = "Your waitlist offer expired"
        lead = "The seat that was held for you was not accepted in time and has been released."
        rows.append(("Offer", str(d.get("offer_id"))))
    else:
        hours = d.get("hours_before")
        title = "Appointment reminder"
        lead = f"Your appointment starts in about {hours} hours." if hours else "Your appointment is coming up."
    width = max([len(k) for k, _ in rows] or [0])
    details = "\n".join(f"  {k.ljust(width)} : {v}" for k, v in rows)
    body = "Hello,\n\n" + lead + "\n" + (f"\n{details}\n" if details else "") + (f"\n{closing}\n" if closing else "") + \
        "\nRegards,\nThe MediSlot team\n\nThis is an automated message, please do not reply."
    return f"MediSlot: {title}", lead + (" " + " | ".join(f"{k}: {v}" for k, v in rows) if rows else ""), body


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
            logger.info("Duplicate notification skipped: key=%s", key)
            return False
        raise
    subject, text, email_body = compose(detail_type, rtype, detail)
    try:
        sns().publish(
            TopicArn=os.environ["PATIENT_TOPIC_ARN" if rtype == "patient" else "DOCTOR_TOPIC_ARN"],
            Subject=subject[:100],
            MessageStructure="json",
            Message=json.dumps({
                "default": json.dumps({"event": detail_type, "recipient": rtype, "recipient_id": rid, "text": text,
                                       "detail": detail}, default=str),
                "email": email_body}),
            MessageAttributes={"event_type": {"DataType": "String", "StringValue": detail_type},
                               "recipient_id": {"DataType": "String", "StringValue": str(rid)}})
    except Exception:
        dispatch_log().delete_item(Key={"dispatch_key": key})  # let the retry send it
        raise
    metric("NotificationsDispatched")
    logger.info("Notification sent: event=%s recipient=%s#%s key=%s", detail_type, rtype, rid, key)
    return True


def handler(event, context):
    failures = []
    logger.info("Notification batch received: request_id=%s records=%s",
                getattr(context, "aws_request_id", "-"), len(event.get("Records", [])))
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
            logger.exception("Notification failed: message_id=%s", rec.get("messageId"))
            failures.append({"itemIdentifier": rec["messageId"]})
    logger.info("Notification batch done: failed=%s", len(failures))
    return {"batchItemFailures": failures}
